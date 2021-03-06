# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import ast
import re

from slave import recipe_api


class SyzygyApi(recipe_api.RecipeApi):
  # Used for constructing URLs to the Syzygy archives.
  _SYZYGY_ARCHIVE_URL = (
      'https://syzygy-archive.commondatastorage.googleapis.com')
  _SYZYGY_GS = 'gs://syzygy-archive'

  # Fake unittests.gypi contents.
  _FAKE_UNITTESTS_GYPI_DATA = repr({
    'variables': {
      'unittests': [
        '<(src)/syzygy/agent/asan/asan.gyp:foo_unittests',
        '<(src)/syzygy/agent/common/common.gyp:bar_unittests',
        '<(src)/syzygy/agent/coverage/coverage.gyp:baz_unittests',
      ]
    }
  })

  # Fake version file data.
  _FAKE_VERSION_DATA = """# Copyright 2012 Google Inc. All Rights Reserved.
#
# Boilerplate!
#
#      http://url/to/nowhere
#
# And some more boilerplate, followed by a blank line!

MAJOR=0
MINOR=0
BUILD=0
PATCH=1
"""

  def __init__(self, *args, **kwargs):
    super(SyzygyApi, self).__init__(*args, **kwargs)
    # This is populated by the first call to 'version'.
    self._version = None

  @property
  def build_dir(self):
    """Returns the build directory for the project."""
    return self.m.path['checkout'].join('build')

  @property
  def output_dir(self):
    """Returns the configuration-specific output directory for the project."""
    return self.build_dir.join(self.m.chromium.c.BUILD_CONFIG)

  @property
  def public_scripts_dir(self):
    """Returns the public Syzygy build scripts directory."""
    return self.m.path['checkout'].join('syzygy', 'build')

  @property
  def internal_scripts_dir(self):
    """Returns the internal Syzygy build scripts directory."""
    return self.m.path['checkout'].join('syzygy', 'internal', 'build')

  @property
  def version(self):
    """Returns the version tuple associated with the checkout."""
    # Only read the value if it hasn't yet been read.
    if not self._version:
      version = self.m.path['checkout'].join('syzygy', 'VERSION')
      version = self.m.file.read('read_version', version,
                                 test_data=self._FAKE_VERSION_DATA)
      d = {}
      for l in version.splitlines():
        # Look for a 'NAME=VALUE' pair.
        m = re.match('^\s*([A-Z]+)\s*=\s*(\d+)\s*$', l)
        if not m:
          continue
        key = m.group(1)
        value = m.group(2)
        d[key] = int(value)
      self._version = (d['MAJOR'], d['MINOR'], d['BUILD'], d['PATCH'])

    # Return the cached value.
    return self._version

  def _get_build_id(self):
    """Returns a string build identifier that uniquely identifies this build."""
    revision = self.m.properties['revision']

    # For official builds we use the actual version number and the revision.
    if self.c.official_build:
      return ('%03d.%03d.%03d.%03d' % self.version) + ('_%s' % revision)

    # Otherwise use the buildnumber and the revision.
    buildnumber = self.m.properties['buildnumber']
    return '%06d_%s' % (int(buildnumber), revision)

  def _gen_step_gs_util_cp_dir(self, step_name, src_dir, dst_rel_path):
    """Returns a gsutil_cp_dir step. Internal use only.

    Args:
      step_name: The step name as a string.
      src_dir: The source directory on the local file system. This should be a
          Path object.
      dst_rel_path: The destination path relative to the syzygy_archive root.
          This should be a string.

    Returns:
      The generated python step.
    """
    gsutil_cp_dir_py = self.m.path['build'].join(
        'scripts', 'slave', 'syzygy', 'gsutil_cp_dir.py')
    dst_dir = '%s/%s' % (self._SYZYGY_GS, dst_rel_path)
    args = [src_dir, dst_dir]
    return self.m.python(step_name, gsutil_cp_dir_py, args)

  def taskkill(self):
    """Run chromium.taskkill.

    This invokes a dummy step on the test slave as killing all instances of
    Chrome seriously impairs development.
    """
    if self.m.properties['slavename'] == 'fake_slave':
      return self.m.python.inline('taskkill', 'print "dummy taskkill"')
    return self.m.chromium.taskkill()

  def checkout(self):
    """Checks out the Syzygy code using the current gclient configuration."""
    self.m.gclient.checkout()

  def runhooks(self):
    return self.m.chromium.runhooks()

  def compile(self):
    """Generates a step to compile the project."""
    # Compile the project. This is done by manually invoking compile.py for now,
    # as chromium.compile doesn't support MSVS.
    # TODO(chrisha): Fix this once we build with Ninja!
    #compile_py = self.m.path['build'].join('scripts', 'slave', 'compile.py')
    #args = ['--solution', self.c.solution,
    #        '--project', self.c.project,
    #        '--target',self.c.build_config,
    #        '--build-tool=vs']
    #return self.m.python('compile', compile_py, args)
    return self.m.chromium.compile()

  def read_unittests_gypi(self):
    """Reads and parses unittests.gypi from the checkout, returning a list."""
    gypi = self.m.path['checkout'].join('syzygy', 'unittests.gypi')
    gypi = self.m.file.read('read_unittests_gypi', gypi,
                            test_data=self._FAKE_UNITTESTS_GYPI_DATA)
    gypi = ast.literal_eval(gypi)
    unittests = [t.split(':')[1] for t in gypi['variables']['unittests']]
    return unittests

  def run_unittests(self, unittests):
    # Generate a test step for each unittest.
    app_verifier_py = self.public_scripts_dir.join('app_verifier.py')
    for unittest in unittests:
      unittest_path = self.output_dir.join(unittest + '.exe')
      args = ['--on-waterfall',
              unittest_path,
              '--',
              # Arguments to the actual gtest unittest.
              '--gtest_print_time']
      self.m.chromium.runtest(app_verifier_py, args, name=unittest,
                              test_type=unittest)

  def randomly_reorder_chrome(self):
    """Returns a test step that randomly reorders Chrome and ensures it runs."""
    randomize_chrome_py = self.internal_scripts_dir.join(
        'randomize_chrome.py')
    args = ['--build-dir', self.build_dir,
            '--target', self.m.chromium.c.BUILD_CONFIG,
            '--verbose']
    return self.m.python('randomly_reorder_chrome', randomize_chrome_py, args)

  def benchmark_chrome(self):
    """Returns a test step that benchmarks an optimized Chrome."""
    benchmark_chrome_py = self.internal_scripts_dir.join(
        'benchmark_chrome.py')
    args = ['--build-dir', self.build_dir,
            '--target', self.m.chromium.c.BUILD_CONFIG,
            '--verbose']
    return self.m.python('benchmark_chrome',  benchmark_chrome_py, args)

  def capture_unittest_coverage(self):
    """Returns a step that runs the coverage script.

    Only meant to be called from the 'Coverage' configuration.
    """
    assert self.m.chromium.c.BUILD_CONFIG == 'Coverage'
    generate_coverage_py = self.public_scripts_dir.join(
        'generate_coverage.py')
    args = ['--verbose',
            '--syzygy',
            '--build-dir', self.output_dir]
    return self.m.python(
        'capture_unittest_coverage', generate_coverage_py, args)

  def archive_coverage(self):
    """Returns a step that archives the coverage report.

    Only meant to be called from the 'Coverage' configuration.
    """
    assert self.m.chromium.c.BUILD_CONFIG == 'Coverage'
    cov_dir = self.output_dir.join('cov')
    build_id = self._get_build_id()
    archive_path = 'builds/coverage/%s' % build_id
    if self.m.properties['slavename'] == 'fake_slave':
      archive_path = 'test/' + archive_path
    report_url = '%s/%s/index.html' % (self._SYZYGY_ARCHIVE_URL, archive_path)
    step = self._gen_step_gs_util_cp_dir(
        'archive_coverage', cov_dir, archive_path)
    step.presentation.links['coverage_report'] = report_url
    return step

  def archive_binaries(self):
    """Returns a step that archives the official binaries.

    Only meant to be called from an official build.
    """
    assert self.m.chromium.c.BUILD_CONFIG == 'Release' and self.c.official_build
    bin_dir = self.output_dir.join('archive')
    archive_name = self._get_build_id()
    archive_path = 'builds/official/%s' % archive_name
    if self.m.properties['slavename'] == 'fake_slave':
      archive_path = 'test/' + archive_path
    bin_url = '%s/index.html?path=%s/' % (
        self._SYZYGY_ARCHIVE_URL, archive_path)
    step = self._gen_step_gs_util_cp_dir(
        'archive_binaries', bin_dir, archive_path)
    step.presentation.links['archive'] = bin_url
    return step

  def upload_symbols(self):
    """Returns a step that source indexes and uploads symbols.

    Only meant to be called from an official build.
    """
    assert self.m.chromium.c.BUILD_CONFIG == 'Release' and self.c.official_build
    archive_symbols_py = self.m.path['checkout'].join(
        'syzygy', 'internal', 'scripts', 'archive_symbols.py')
    asan_rtl_dll = self.output_dir.join('*asan_rtl.dll')
    client_dlls = self.output_dir.join('*client.dll')
    args = ['-s', '-b', asan_rtl_dll, client_dlls]
    return self.m.python('upload_symbols', archive_symbols_py, args)

  def download_binaries(self):
    """Returns a step that downloads the current official binaries."""
    revision = self.m.properties['revision']
    get_syzygy_binaries_py = self.public_scripts_dir.join(
        'get_syzygy_binaries.py')
    output_dir = self.m.path['checkout'].join('syzygy', 'binaries')
    args = ['--output_dir', output_dir,
            '--revision', revision,
            '--overwrite',
            '--verbose']
    return self.m.python('download_binaries', get_syzygy_binaries_py, args)

  def smoke_test(self):
    """Returns a step that launches the smoke test script."""
    smoke_test_py = self.internal_scripts_dir.join('smoke_test.py')
    build_dir = self.m.path['checkout'].join('build')
    args = ['--verbose', '--build-dir', build_dir]
    return self.m.python('smoke_test', smoke_test_py, args)
