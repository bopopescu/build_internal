# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility class to generate a Native-Client-specific BuildFactory.

Based on gclient_factory.py."""

from main.factory import gclient_factory
from main.factory import nacl_commands

import config

class NativeClientFactory(gclient_factory.GClientFactory):
  """Encapsulates data and methods common to the nacl main.cfg files."""

  CUSTOM_VARS_GOOGLECODE_URL = ('googlecode_url', config.Main.googlecode_url)
  CUSTOM_VARS_SOURCEFORGE_URL = ('sourceforge_url',
                                 config.Main.sourceforge_url)
  CUSTOM_VARS_WEBKIT_MIRROR = ('webkit_trunk', config.Main.webkit_trunk_url)

  def __init__(self, build_dir, target_platform,
               alternate_url=None, custom_deps_list=None, target_os=None):
    solutions = []
    self.target_platform = target_platform
    nacl_url = config.Main.nacl_url
    if alternate_url:
      nacl_url = alternate_url
    main = gclient_factory.GClientSolution(
        nacl_url, custom_deps_list=custom_deps_list,
        custom_vars_list=[self.CUSTOM_VARS_WEBKIT_MIRROR,
                          self.CUSTOM_VARS_GOOGLECODE_URL,
                          self.CUSTOM_VARS_SOURCEFORGE_URL])
    solutions.append(main)

    gclient_factory.GClientFactory.__init__(self, build_dir, solutions,
                                            target_platform=target_platform,
                                            target_os=target_os)

  @staticmethod
  def _AddTriggerTests(factory_cmd_obj, tests):
    """Add the tests listed in 'tests' to the factory_cmd_obj."""
    # This function is too crowded, try to simplify it a little.
    def R(test):
      return gclient_factory.ShouldRunTest(tests, test)
    f = factory_cmd_obj

    for mode in ['dbg', 'opt', 'spec', 'opt_panda', 'perf_panda']:
      if R('nacl_trigger_arm_hw_%s' % mode):
        f.AddTrigger('arm_%s_hw_tests' % mode)
      if R('nacl_trigger_win7atom64_hw_%s' % mode):
        f.AddTrigger('win7atom64_%s_hw_tests' % mode)
    if R('nacl_trigger_llvm'):
      f.AddTrigger('llvm_trigger')

  def NativeClientFactory(self, tests=None, subordinate_type='BuilderTester',
                          official_release=False,
                          options=None, factory_properties=None):
    factory_properties = factory_properties or {}
    options = options or {}
    tests = tests or []
    # Create the spec for the solutions
    gclient_spec = self.BuildGClientSpec(tests)
    # Initialize the factory with the basic steps.
    factory = self.BaseFactory(gclient_spec,
                               official_release=official_release,
                               factory_properties=factory_properties,
                               subordinate_type=subordinate_type)
    # Get the factory command object to create new steps to the factory.
    nacl_cmd_obj = nacl_commands.NativeClientCommands(factory,
                                                      self._build_dir,
                                                      self._target_platform)

    # Upload expectations before running the tests to check against
    # the latest expectations.
    if factory_properties.get('expectations'):
      nacl_cmd_obj.AddUploadPerfExpectations(factory_properties)

    # Whole build in one step.
    nacl_cmd_obj.AddAnnotatedStep(
        ['buildbot/buildbot_selector.py'], timeout=1500, usePython=True,
        env={'BUILDBOT_SLAVE_TYPE': subordinate_type},
        factory_properties=factory_properties)

    # Trigger tests on other builders.
    self._AddTriggerTests(nacl_cmd_obj, tests)

    return factory
