#!/usr/bin/env python

from setuptools import setup
from setuptools import find_packages

setup(
      name='dfms',
      version='0.1',
      description='Data Flow Management System',
      author='',
      author_email='',
      url='',
      packages=find_packages(),
      package_data = {
        'dfms.manager' : ['web/*.html', 'web/static/css/*.css', 'web/static/fonts/*', 'web/static/js/*.js', 'web/static/js/d3/*']
      },
      install_requires=["Pyro4", "luigi", "psutil", "paramiko", "bottle", "tornado", "drive-casa", "docker-py"],
      dependency_links=["https://github.com/davepallot/drive-casa/archive/0.6.7.zip#egg=drive-casa-0.6.7"],
      test_suite="test",
      entry_points= {
          'console_scripts':[
              'dfmsDOM=dfms.manager.cmdline:dfmsDOM',
              'dfmsDIM=dfms.manager.cmdline:dfmsDIM'
          ],
      }
)
