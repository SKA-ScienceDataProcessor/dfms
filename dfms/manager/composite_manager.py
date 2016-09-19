#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia, 2015
#    Copyright by UWA (in the framework of the ICRAR)
#    All rights reserved
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston,
#    MA 02111-1307  USA
#
import abc
import collections
import functools
import logging
import multiprocessing.pool
import threading

from dfms import remote, graph_loader
from dfms.ddap_protocol import DROPRel
from dfms.exceptions import InvalidGraphException, DaliugeException, \
    SubManagerException
from dfms.manager.client import NodeManagerClient
from dfms.manager.constants import ISLAND_DEFAULT_REST_PORT, NODE_DEFAULT_REST_PORT
from dfms.manager.drop_manager import DROPManager
from dfms.utils import portIsOpen
from dfms.manager import constants


logger = logging.getLogger(__name__)

def uid_for_drop(dropSpec):
    if 'uid' in dropSpec:
        return dropSpec['uid']
    return dropSpec['oid']

def sanitize_relations(interDMRelations, graph):

    # TODO: Big change required to remove this hack here
    #
    # Values in the interDMRelations array use OIDs to identify drops.
    # This is because so far we have told users to that OIDs are required
    # in the physical graph description, while UIDs are optional
    # (and copied over from the OID if not given).
    # On the other hand, once drops are actually created in deploySession()
    # we access the values in interDMRelations as if they had UIDs inside,
    # which causes problems everywhere because everything else is indexed
    # on UIDs.
    # In order to not break the current physical graph constrains and keep
    # things simple we'll simply replace the values of the interDMRelations
    # array here to use the corresponding UID for the given OIDs.
    # Because UIDs are globally unique across drop instances it makes sense
    # to always index things by UID and not by OID. Thus, in the future we
    # should probably change the requirement on the physical graphs sent by
    # users to always require an UID, and optionally an OID, and then change
    # all this code to immediately use those UIDs instead.
    newDMRelations = []
    for rel in interDMRelations:
        lhs = uid_for_drop(graph[rel.lhs])
        rhs = uid_for_drop(graph[rel.rhs])
        new_rel = DROPRel(lhs, rel.rel, rhs)
        newDMRelations.append(new_rel)
    interDMRelations[:] = newDMRelations

def group_by_node(uids, graph):
    uids_by_node = collections.defaultdict(list)
    for uid in uids:
        uids_by_node[graph[uid]['node']].append(uid)
    return uids_by_node

class CompositeManager(DROPManager):
    """
    A DROPManager that in turn manages DROPManagers (sigh...).

    DROP Managers form a hierarchy where those at the bottom actually hold
    DROPs while those in the levels above rely commands and aggregate results,
    making the system more manageable and scalable. The CompositeManager class
    implements the upper part of this hierarchy in a generic way by holding
    references to a number of sub-DROPManagers and communicating with them to
    complete each operation. The only assumption about sub-DROPManagers is that
    they obey the DROPManager interface, and therefore this CompositeManager
    class allows for multiple levels of hierarchy seamlessly.

    Having different levels of Data Management hierarchy implies that the
    physical graph that is fed into the hierarchy needs to be partitioned at
    each level (except at the bottom of the hierarchy) in order to place each
    DROP in its correct place. The attribute used by a particular
    CompositeManager to partition the graph (from its graphSpec) is given at
    construction time.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, dmPort, partitionAttr, dmExec, subDmId, dmHosts=[], pkeyPath=None, dmCheckTimeout=10):
        """
        Creates a new CompositeManager. The sub-DMs it manages are to be located
        at `dmHosts`, and should be listening on port `dmPort`.

        :param: dmPort The port at which the sub-DMs expose themselves
        :param: partitionAttr The attribute on each dropSpec that specifies the
                partitioning of the graph at this CompositeManager level.
        :param: dmExec The name of the executable that starts a sub-DM
        :param: subDmId The sub-DM ID.
        :param: dmHosts The list of hosts under which the sub-DMs should be found.
        :param: pkeyPath The path to the SSH private key to be used when connecting
                to the remote hosts to start the sub-DMs if necessary. A value
                of `None` means that the default path should be used
        :param: dmCheckTimeout The timeout used before giving up and declaring
                a sub-DM as not-yet-present in a given host
        """
        self._dmPort = dmPort
        self._partitionAttr = partitionAttr
        self._dmExec = dmExec
        self._subDmId = subDmId
        self._dmHosts = dmHosts
        self._graph = {}
        self._drop_rels = {}
        self._sessionIds = [] # TODO: it's still unclear how sessions are managed at the composite-manager level
        self._pkeyPath = pkeyPath
        self._dmCheckTimeout = dmCheckTimeout
        n_threads = max(1,min(len(dmHosts),20))
        self._tp = multiprocessing.pool.ThreadPool(n_threads)

        # The list of bottom-level nodes that are covered by this manager
        # This list is different from the dmHosts, which are the machines that
        # are directly managed by this manager (which in turn could manage more
        # machines)
        self._nodes = []

        self.startDMChecker()

    def startDMChecker(self):
        self._dmCheckerEvt = threading.Event()
        self._dmCheckerThread = threading.Thread(name='DMChecker Thread', target=self._checkDM)
        self._dmCheckerThread.start()

    def stopDMChecker(self):
        if not self._dmCheckerEvt.isSet():
            self._dmCheckerEvt.set()
            self._dmCheckerThread.join()

    # Explicit shutdown
    def shutdown(self):
        self.stopDMChecker()
        self._tp.close()
        self._tp.join()

    def _checkDM(self):
        while True:
            for host in self._dmHosts:
                if self._dmCheckerEvt.is_set():
                    break
                try:
                    self.ensureDM(host, timeout=self._dmCheckTimeout)
                except:
                    logger.warning("Couldn't ensure a DM for host %s, will try again later", host)
            if self._dmCheckerEvt.wait(60):
                break

    @property
    def dmHosts(self):
        return self._dmHosts[:]

    def addDmHost(self, host):
        self._dmHosts.append(host)

    @property
    def nodes(self):
        return self._nodes[:]

    def add_node(self, node):
        self._nodes.append(node)

    def remove_node(self, node):
        self._nodes.remove(node)

    @property
    def dmPort(self):
        return self._dmPort

    def subDMCommandLine(self, host):
        return '{0} -i {1} -P {2} -d --host {3}'.format(self._dmExec, self._subDmId, self._dmPort, host)

    def startDM(self, host):
        client = remote.createClient(host, pkeyPath=self._pkeyPath)
        out, err, status = remote.execRemoteWithClient(client, self.subDMCommandLine(host))
        if status != 0:
            logger.error("Failed to start the DM on %s:%d, stdout/stderr follow:\n==STDOUT==\n%s\n==STDERR==\n%s" % (host, self._dmPort, out, err))
            raise DaliugeException("Failed to start the DM on %s:%d" % (host, self._dmPort))
        logger.info("DM successfully started at %s:%d", host, self._dmPort)

    def ensureDM(self, host, timeout=10):

        logger.debug("Checking DM presence at %s:%d", host, self._dmPort)
        if portIsOpen(host, self._dmPort, timeout):
            logger.debug("DM already present at %s:%d", host, self._dmPort)
            return

        # We rely on having ssh keys for this, since we're using
        # the dfms.remote module, which authenticates using public keys
        logger.debug("DM not present at %s:%d, will start it now", host, self._dmPort)
        self.startDM(host)

        # Wait a bit until the DM starts; if it doesn't we fail
        if not portIsOpen(host, self._dmPort, timeout):
            raise DaliugeException("DM started at %s:%d, but couldn't connect to it" % (host, self._dmPort))

    def dmAt(self, host, port=None):
        port = port or self._dmPort
        return NodeManagerClient(host, port, 10)

    def getSessionIds(self):
        return self._sessionIds;

    #
    # Replication of commands to underlying drop managers
    # If "collect" is given, then individual results are also kept in the given
    # structure, which is either a dictionary or a list
    #
    def _do_in_host(self, sessionId, exceptions, f, collect, iterable):

        host = iterable
        if isinstance(iterable, (list, tuple)):
            host = iterable[0]

        try:
            self.ensureDM(host)
            with self.dmAt(host) as dm:
                res = f(dm, iterable, sessionId)

            if isinstance(collect, dict):
                collect.update(res)
            elif isinstance(collect, list):
                collect.append(res)

        except Exception as e:
            exceptions[host] = e
            raise # so it gets printed

    def replicate(self, sessionId, f, action, collect=None, iterable=None):
        """
        Replicates the given function call on each of the underlying drop managers
        """
        thrExs = {}
        iterable = iterable or self._dmHosts
        self._tp.map(functools.partial(self._do_in_host, sessionId, thrExs, f, collect), iterable)
        if thrExs:
            msg = "One or more errors occurred while %s on session %s" % (action, sessionId)
            raise SubManagerException(msg, thrExs)

    #
    # Commands and their per-underlying-drop-manager functions
    #
    def _createSession(self, dm, host, sessionId):
        dm.createSession(sessionId)
        logger.debug('Successfully created session %s on %s', sessionId, host)

    def createSession(self, sessionId):
        """
        Creates a session in all underlying DMs.
        """
        logger.info('Creating Session %s in all hosts', sessionId)
        self.replicate(sessionId, self._createSession, "creating sessions")
        logger.info('Successfully created session %s in all hosts', sessionId)
        self._sessionIds.append(sessionId)

    def _destroySession(self, dm, host, sessionId):
        dm.destroySession(sessionId)
        logger.debug('Successfully destroyed session %s on %s', sessionId, host)

    def destroySession(self, sessionId):
        """
        Destroy a session in all underlying DMs.
        """
        logger.info('Destroying Session %s in all hosts', sessionId)
        self.replicate(sessionId, self._destroySession, "creating sessions")
        self._sessionIds.remove(sessionId)

    def _add_node_subscriptions(self, dm, host_and_subscriptions, sessionId):
        host, subscriptions = host_and_subscriptions
        dm.add_node_subscriptions(sessionId, subscriptions)
        logger.debug("Successfully added relationship info to session %s on %s", sessionId, host)

    def _addGraphSpec(self, dm, host_and_graphspec, sessionId):
        host, graphSpec = host_and_graphspec
        dm.addGraphSpec(sessionId, graphSpec)
        logger.info("Successfully appended graph to session %s on %s", sessionId, host)

    def addGraphSpec(self, sessionId, graphSpec):

        # The first step is to break down the graph into smaller graphs that
        # belong to the same host, so we can submit that graph into the individual
        # DMs. For this we need to make sure that our graph has a the correct
        # attribute set
        logger.info('Separating graph with %d dropSpecs', len(graphSpec))
        perPartition = collections.defaultdict(list)
        for dropSpec in graphSpec:
            if self._partitionAttr not in dropSpec:
                msg = "DROP %s doesn't specify a %s attribute" % (dropSpec['oid'], self._partitionAttr)
                raise InvalidGraphException(msg)

            partition = dropSpec[self._partitionAttr]
            if partition not in self._dmHosts:
                msg = "DROP %s's %s %s does not belong to this DM" % (dropSpec['oid'], self._partitionAttr, partition)
                raise InvalidGraphException(msg)

            perPartition[partition].append(dropSpec)

            # Add the drop specs to our graph
            self._graph[uid_for_drop(dropSpec)] = dropSpec

        # At each partition the relationships between DROPs should be local at the
        # moment of submitting the graph; thus we record the inter-partition
        # relationships separately and remove them from the original graph spec
        inter_partition_rels = []
        for dropSpecs in perPartition.values():
            inter_partition_rels.extend(graph_loader.removeUnmetRelationships(dropSpecs))
        sanitize_relations(inter_partition_rels, self._graph)
        logger.info('Removed (and sanitized) %d inter-dm relationships', len(inter_partition_rels))

        # Store the inter-partition relationships; later on they have to be
        # communicated to the NMs so they can establish them as needed.
        drop_rels = collections.defaultdict(functools.partial(collections.defaultdict, list))
        for rel in inter_partition_rels:
            rhn = self._graph[rel.rhs]['node']
            lhn = self._graph[rel.lhs]['node']
            drop_rels[lhn][rhn].append(rel)
            drop_rels[rhn][lhn].append(rel)

        self._drop_rels[sessionId] = drop_rels
        logger.debug("Calculated NM-level drop relationships: %r", drop_rels)

        # Create the individual graphs on each DM now that they are correctly
        # separated.
        logger.info('Adding individual graphSpec of session %s to each DM', sessionId)
        self.replicate(sessionId, self._addGraphSpec, "appending graphSpec to individual DMs", iterable=perPartition.items())
        logger.info('Successfully added individual graphSpec of session %s to each DM', sessionId)

    def _deploySession(self, dm, host, sessionId):
        dm.deploySession(sessionId)
        logger.debug('Successfully deployed session %s on %s', sessionId, host)

    def _triggerDrops(self, exceptions, session_id, host_and_uids):

        host, uids = host_and_uids

        # Call "async_execute" for InputFiredAppDROPs, "setCompleted" otherwise
        logger.info("Will trigger initial drops of session %s in host %s", session_id, host)
        with self.dmAt(host, port=constants.NODE_DEFAULT_REST_PORT) as c:
            try:
                c.trigger_drops(session_id, uids)
            except Exception as e:
                exceptions[host] = e
                logger.exception("An exception occurred while moving DROPs to COMPLETED")

    def _add_node_subscriptions_wrapper(self, exceptions, sessionId, host_and_subscriptions):
        host = host_and_subscriptions[0]
        with self.dmAt(host, port=constants.NODE_DEFAULT_REST_PORT) as dm:
            try:
                self._add_node_subscriptions(dm, host_and_subscriptions, sessionId)
            except Exception as e:
                exceptions[host] = e
                logger.exception("An exception occurred while adding node subscription")

    def deploySession(self, sessionId, completedDrops=[]):

        # Indicate the node managers that they have to subscribe to events
        # published by some nodes
        if self._drop_rels[sessionId]:
            # This call throws exception if "I" am MM (but not DIM)
            #self.replicate(sessionId, self._add_node_subscriptions, "adding relationship information", iterable=self._drop_rels[sessionId].items())

            # It appears that the function ensureDM() inside the _do_in_host()
            # cannot make "cross hiearchy level" calls, this is
            # because the self_dmPort is hardcoded inside ensureDM() to be the
            # port directly managed by me (i.e. MM) but not by my children DIMs.
            # In addition, when calling dmAT, _do_in_host() does not explicitly
            # specify a NODE port so MM cannot directly contact NM.
            # Here we instead invoke add_node_subscription() directly for now.
            # It appears working fine.
            # It also appears that we are mixing this non-recursive call inside
            # a resursive function: deploySession())

            ####
            thrExs = {}
            self._tp.map(functools.partial(self._add_node_subscriptions_wrapper, thrExs, sessionId), self._drop_rels[sessionId].items())
            if thrExs:
                raise DaliugeException("One or more exceptions occurred while adding node subscription: %s" % (sessionId), thrExs)
            ###
            logger.info("Delivered node subscription list to node managers")

        logger.info('Deploying Session %s in all hosts', sessionId)
        self.replicate(sessionId, self._deploySession, "deploying session")
        logger.info('Successfully deployed session %s in all hosts', sessionId)

        # Now that everything is wired up we move the requested DROPs to COMPLETED
        # (instead of doing it at the DM-level deployment time, in which case
        # we would certainly miss most of the events)
        if completedDrops:
            not_found = set(completedDrops) - set(self._graph)
            if not_found:
                raise DaliugeException("UIDs for completed drops not found: %r", not_found)
            logger.info('Moving following DROPs to COMPLETED right away: %r', completedDrops)
            completed_by_host = group_by_node(completedDrops, self._graph)
            thrExs = {}
            self._tp.map(functools.partial(self._triggerDrops, thrExs, sessionId), completed_by_host.items())
            if thrExs:
                raise DaliugeException("One or more exceptions occurred while moving DROPs to COMPLETED: %s" % (sessionId), thrExs)
            logger.info('Successfully triggered drops')

    def _getGraphStatus(self, dm, host, sessionId):
        return dm.getGraphStatus(sessionId)

    def getGraphStatus(self, sessionId):
        allStatus = {}
        self.replicate(sessionId, self._getGraphStatus, "getting graph status", collect=allStatus)
        return allStatus

    def _getGraph(self, dm, host, sessionId):
        return dm.getGraph(sessionId)

    def getGraph(self, sessionId):

        allGraphs = {}
        self.replicate(sessionId, self._getGraph, "getting the graph", collect=allGraphs)

        # The graphs coming from the DMs are not interconnected, we need to
        # add the missing connections to the graph before returning upstream
        rels = set([z for x in self._drop_rels[sessionId].values() for y in x.values() for z in y])
        for rel in rels:
            graph_loader.addLink(rel.rel, allGraphs[rel.rhs], rel.lhs)

        return allGraphs

    def _getSessionStatus(self, dm, host, sessionId):
        return {host: dm.getSessionStatus(sessionId)}

    def getSessionStatus(self, sessionId):
        allStatus = {}
        self.replicate(sessionId, self._getSessionStatus, "getting the graph status", collect=allStatus)
        return allStatus

    def _getGraphSize(self, dm, host, sessionId):
        return dm.getGraphSize(sessionId)

    def getGraphSize(self, sessionId):
        allCounts = []
        self.replicate(sessionId, self._getGraphSize, "getting the graph size", collect=allCounts)
        return sum(allCounts)

class DataIslandManager(CompositeManager):
    """
    The DataIslandManager, which manages a number of NodeManagers.
    """

    def __init__(self, dmHosts=[], pkeyPath=None, dmCheckTimeout=10):
        super(DataIslandManager, self).__init__(NODE_DEFAULT_REST_PORT,
                                                'node',
                                                'dfmsNM',
                                                'nm',
                                                dmHosts=dmHosts,
                                                pkeyPath=pkeyPath,
                                                dmCheckTimeout=dmCheckTimeout)

        # In the case of the Data Island the dmHosts are the final nodes as well
        self._nodes = dmHosts
        logger.info('Created DataIslandManager for hosts: %r', self._dmHosts)

    def add_node(self, node):
        CompositeManager.add_node(self, node)
        self._dmHosts.append(node)

class MasterManager(CompositeManager):
    """
    The MasterManager, which manages a number of DataIslandManagers.
    """

    def __init__(self, dmHosts=[], pkeyPath=None, dmCheckTimeout=10):
        super(MasterManager, self).__init__(ISLAND_DEFAULT_REST_PORT,
                                            'island',
                                            'dfmsDIM',
                                            'dim',
                                            dmHosts=dmHosts,
                                            pkeyPath=pkeyPath,
                                            dmCheckTimeout=dmCheckTimeout)
        logger.info('Created MasterManager for hosts: %r', self._dmHosts)
