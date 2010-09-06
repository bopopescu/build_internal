#!/usr/bin/python
# Copyright (c) 2006-2009 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility classes to generate and manage a BuildFactory to be passed to a
builder dictionary as the 'factory' member, for each builder in c['builders'].

Specifically creates a base BuildFactory that will execute a gclient checkout
first."""

import os
import re

from build_factory import BuildFactory
import commands


def ShouldRunTest(tests, name):
  """Returns True if |name| is an entry in |tests|."""
  if not tests:
    return False

  if name in tests:
    return True
  return False


def ShouldRunMatchingTest(tests, pattern):
  """Returns True if regex |pattern| matches an entry in |tests|."""
  if not tests:
    return False

  for test in tests:
    if re.match(pattern, test):
      return True
  return False


class GClientSolution(object):
  """Defines a GClient solution."""

  def __init__(self, svn_url, name=None, custom_deps_list=None,
               needed_components=None, custom_vars_list=None,
               custom_deps_file=None, safesync_url=None):
    """ Initialize the GClient Solution.
    Params:
      svn_url: SVN path for this solution.
      name: Name for this solution. if None, it uses the last item in the path.
      custom_deps_list: Modifications to make on the DEPS file.
      needed_components: A map used to skip dependencies when a test is not run.
          The map key is the test name. The map value is an array containing the
          dependencies that are not needed when this test is not run.
      custom_vars_list: Modifications to make on the vars in the DEPS file.
      custom_deps_file: Change the default DEPS filename.
      safesync_url: Select to build based on a lkgr url.
    """
    self.svn_url = svn_url
    self.name = name
    self.custom_deps_list = custom_deps_list or []
    self.custom_vars_list = custom_vars_list or []
    self.custom_deps_file = custom_deps_file
    self.needed_components = needed_components
    self.safesync_url = safesync_url

    if not self.name:
      self.name = svn_url.split('/')[-1]

  def GetSpec(self, tests=None):
    """Returns the specs for this solution.
    Params:
      tests: List of tests to run. This is required only when needed_components
             is not None.
    """

    final_custom_deps_list = self.custom_deps_list[:]
    # Extend the custom deps with everything that is not going to be used
    # in this factory
    if self.needed_components:
      for test, dependencies in self.needed_components.iteritems():
        if ShouldRunMatchingTest(tests, test):
          continue
        final_custom_deps_list.extend(dependencies)

    # Create the custom_deps string
    custom_deps = ''
    for dep in final_custom_deps_list:
      if dep[1] is None:
        dep_url = None
      else:
        dep_url = '"%s"' % dep[1]
      custom_deps += '"%s" : %s, ' % (dep[0], dep_url)

    custom_vars = ''
    for var in self.custom_vars_list:
      if var[1] is None:
        var_value = None
      else:
        var_value = '"%s"' % var[1]
      custom_vars += '"%s" : %s, ' % (var[0], var_value)

    extras = ''
    if self.custom_deps_file:
      extras += '"deps_file": "%s",' % self.custom_deps_file
    if self.safesync_url:
      extras += '"safesync_url": "%s"' % self.safesync_url

    # This must not contain any line breaks or other characters that would
    # require escaping on the command line, since it will be passed to gclient.
    spec = (
      '{ "name": "%s", '
        '"url": "%s", '
        '"custom_deps": {'
                          '%s'
                       '},'
        '"custom_vars": {'
                          '%s'
                       '},'
        '%s'
      '},' % (self.name, self.svn_url, custom_deps, custom_vars, extras)
    )
    return spec


class GClientFactory(object):
  """Encapsulates data and methods common to both (all) master.cfg files."""

  def __init__(self, build_dir, solutions, project=None, target_platform=None):
    self._build_dir = build_dir
    self._solutions = solutions
    self._target_platform = target_platform or 'win32'

    if self._target_platform == 'win32':
      # Look for a solution named for its enclosing directory.
      self._project = project or os.path.basename(self._build_dir) + '.sln'
    else:
      self._project = project

  def BuildGClientSpec(self, tests=None):
    spec = 'solutions = ['
    for solution in self._solutions:
      spec += solution.GetSpec(tests)
    spec += ']'

    return spec

  def BaseFactory(self, gclient_spec=None, official_release=False,
                  factory_properties=None, build_properties=None,
                  delay_compile_step=False, sudo_for_remove=False):
    if gclient_spec is None:
      gclient_spec = self.BuildGClientSpec()
    factory_properties = factory_properties or {}
    factory = BuildFactory(build_properties)
    factory_cmd_obj = commands.FactoryCommands(factory,
        target_platform=self._target_platform)
    # First kill any svn.exe tasks so we can update in peace, and
    # afterwards use the checked-out script to kill everything else.
    if self._target_platform == 'win32':
      factory_cmd_obj.AddSvnKillStep()
    factory_cmd_obj.AddUpdateScriptStep()
    # Once the script is updated, the zombie processes left by the previous
    # run can be killed.
    if self._target_platform == 'win32':
      factory_cmd_obj.AddTaskkillStep()
    env = factory_properties.get('gclient_env', {})
    # svn timeout is 2 min; we allow 5
    timeout = factory_properties.get('gclient_timeout')
    if official_release:
      factory_cmd_obj.AddClobberTreeStep(gclient_spec, env, timeout)
    # We safely update the tree only after killing the residual tasks.
    if not delay_compile_step:
      self.AddUpdateStep(gclient_spec, factory_properties, factory,
                         sudo_for_remove)
    return factory

  def BuildFactory(self, identifier, target='Release', clobber=False,
                   tests=[], mode=None, slave_type='BuilderTester',
                   options=None, compile_timeout=1200, build_url=None,
                    project=None, factory_properties=None):
    # Create the spec for the solutions
    gclient_spec = self.BuildGClientSpec(tests)

    # Initialize the factory with the basic steps.
    factory = self.BaseFactory(gclient_spec,
                               factory_properties=factory_properties)

    # Get the factory command object to create new steps to the factory.
    factory_cmd_obj = commands.FactoryCommands(factory, identifier, target,
                                               self._build_dir,
                                               self._target_platform)

    # Add the compile step if needed.
    if (slave_type == 'BuilderTester' or slave_type == 'Builder' or
        slave_type == 'Trybot'):
      factory_cmd_obj.AddCompileStep(project or self._project, clobber,
                                     mode=mode, options=options,
                                     timeout=compile_timeout)

    # Archive the full output directory if the machine is a builder.
    if slave_type == 'Builder':
      factory_cmd_obj.AddZipBuild()

    # Download the full output directory if the machine is a tester.
    if slave_type == 'Tester':
      factory_cmd_obj.AddExtractBuild(build_url)

    return factory

  def AddUpdateStep(self, gclient_spec, factory_properties, factory,
                    sudo_for_remove=False):
    if gclient_spec is None:
      gclient_spec = self.BuildGClientSpec()
    factory_properties = factory_properties or {}

    # Get the factory command object to add update step to the factory.
    factory_cmd_obj = commands.FactoryCommands(factory,
        target_platform=self._target_platform)

    # Get variables needed for the update.
    env = factory_properties.get('gclient_env', {})
    timeout = factory_properties.get('gclient_timeout')

    # Add the update step.
    factory_cmd_obj.AddUpdateStep(gclient_spec, env, timeout, sudo_for_remove)
