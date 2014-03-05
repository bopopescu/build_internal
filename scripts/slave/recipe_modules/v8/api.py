# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from slave import recipe_api

class V8Api(recipe_api.RecipeApi):
  def checkout(self):
    return self.m.gclient.checkout()

  def runhooks(self, **kwargs):
    return self.m.chromium.runhooks(**kwargs)

  def compile(self):
    return self.m.chromium.compile()

  def _runtest(self, name, tests, flaky_tests=None):
    full_args = [
      '--target', self.m.chromium.c.build_config_fs,
      '--arch', self.m.chromium.c.gyp_env.GYP_DEFINES['target_arch'],
      '--testname', tests,
    ]
    if flaky_tests:
      full_args += ['--flaky-tests', flaky_tests]

    return self.m.python(
      name,
      self.m.path.build('scripts', 'slave', 'v8', 'v8testing.py'),
      full_args,
      cwd=self.m.path.checkout(),
    )

  def runtest(self, test):
    if test['flaky_step']:
      return [
        self._runtest(test['name'],
                      test['tests'],
                      flaky_tests='skip'),
        self._runtest(test['name'] + ' - flaky',
                      test['tests'],
                      flaky_tests='run'),
      ]
    else:
      return self._runtest(test['name'], test['tests'])
