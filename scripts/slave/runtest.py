#!/usr/bin/env python
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A tool used to run a Chrome test executable and process the output.

This script is used by the buildbot slaves. It must be run from the outer
build directory, e.g. chrome-release/build/.

For a list of command-line options, call this script with '--help'.
"""

import ast
import copy
import datetime
import exceptions
import gzip
import hashlib
import json
import logging
import optparse
import os
import re
import stat
import subprocess
import sys
import tempfile

# The following note was added in 2010 by nsylvain:
#
# sys.path needs to be modified here because python2.6 automatically adds the
# system "google" module (/usr/lib/pymodules/python2.6/google) to sys.modules
# when we import "chromium_config" (I don't know why it does this). This causes
# the import of our local "google.*" modules to fail because python seems to
# only look for a system "google.*", even if our path is in sys.path before
# importing "google.*". If we modify sys.path here, before importing
# "chromium_config", python2.6 properly uses our path to find our "google.*"
# (even though it still automatically adds the system "google" module to
# sys.modules, and probably should still be using that to resolve "google.*",
# which I really don't understand).
sys.path.insert(0, os.path.abspath('src/tools/python'))

from common import chromium_utils
from common import gtest_utils

# TODO(crbug.com/403564). We almost certainly shouldn't be importing this.
import config

from slave import annotation_utils
from slave import build_directory
from slave import crash_utils
from slave import gtest_slave_utils
from slave import performance_log_processor
from slave import results_dashboard
from slave import slave_utils
from slave import xvfb

USAGE = '%s [options] test.exe [test args]' % os.path.basename(sys.argv[0])

CHROME_SANDBOX_PATH = '/opt/chromium/chrome_sandbox'

# Directory to write JSON for test results into.
DEST_DIR = 'gtest_results'

# Names of httpd configuration file under different platforms.
HTTPD_CONF = {
    'linux': 'httpd2_linux.conf',
    'mac': 'httpd2_mac.conf',
    'win': 'httpd.conf'
}
# Regex matching git comment lines containing svn revision info.
GIT_SVN_ID_RE = re.compile('^git-svn-id: .*@([0-9]+) .*$')
# Regex for the master branch commit position.
GIT_CR_POS_RE = re.compile('^Cr-Commit-Position: refs/heads/master@{#(\d+)}$')

# The directory that this script is in.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_PROCESSOR_CLASSES = {
    'gtest': gtest_utils.GTestLogParser,
    'graphing': performance_log_processor.GraphingLogProcessor,
    'pagecycler': performance_log_processor.GraphingPageCyclerLogProcessor,
}


def _ShouldEnableSandbox(sandbox_path):
  """Checks whether the current slave should use the sandbox.

  This is based on should_enable_sandbox in src/testing/test_env.py.

  Args:
    sandbox_path: Path to sandbox file.

  Returns:
    True iff the slave is a Linux host with the sandbox file both present and
    configured correctly.
  """
  if not (sys.platform.startswith('linux') and
          os.path.exists(sandbox_path)):
    return False
  sandbox_stat = os.stat(sandbox_path)
  if ((sandbox_stat.st_mode & stat.S_ISUID) and
      (sandbox_stat.st_mode & stat.S_IRUSR) and
      (sandbox_stat.st_mode & stat.S_IXUSR) and
      (sandbox_stat.st_uid == 0)):
    return True
  return False


def _GetTempCount():
  """Returns the number of files and directories inside the temporary dir."""
  return len(os.listdir(tempfile.gettempdir()))


def _LaunchDBus():
  """Launches DBus to work around a bug in GLib.

  Works around a bug in GLib where it performs operations which aren't
  async-signal-safe (in particular, memory allocations) between fork and exec
  when it spawns subprocesses. This causes threads inside Chrome's browser and
  utility processes to get stuck, and this harness to hang waiting for those
  processes, which will never terminate. This doesn't happen on users'
  machines, because they have an active desktop session and the
  DBUS_SESSION_BUS_ADDRESS environment variable set, but it does happen on the
  bots. See crbug.com/309093 for more details.

  Returns:
    True if it actually spawned DBus.
  """
  import platform
  if (platform.uname()[0].lower() == 'linux' and
      'DBUS_SESSION_BUS_ADDRESS' not in os.environ):
    try:
      print 'DBUS_SESSION_BUS_ADDRESS env var not found, starting dbus-launch'
      dbus_output = subprocess.check_output(['dbus-launch']).split('\n')
      for line in dbus_output:
        m = re.match(r'([^=]+)\=(.+)', line)
        if m:
          os.environ[m.group(1)] = m.group(2)
          print ' setting %s to %s' % (m.group(1), m.group(2))
      return True
    except (subprocess.CalledProcessError, OSError) as e:
      print 'Exception while running dbus_launch: %s' % e
  return False


def _ShutdownDBus():
  """Manually kills the previously-launched DBus daemon.

  It appears that passing --exit-with-session to dbus-launch in
  _LaunchDBus(), above, doesn't cause the launched dbus-daemon to shut
  down properly. Manually kill the sub-process using the PID it gave
  us at launch time.

  This function is called when the flag --spawn-dbus is given, and if
  _LaunchDBus(), above, actually spawned the dbus-daemon.
  """
  import signal
  if 'DBUS_SESSION_BUS_PID' in os.environ:
    dbus_pid = os.environ['DBUS_SESSION_BUS_PID']
    try:
      os.kill(int(dbus_pid), signal.SIGTERM)
      print ' killed dbus-daemon with PID %s' % dbus_pid
    except OSError as e:
      print ' error killing dbus-daemon with PID %s: %s' % (dbus_pid, e)
  # Try to clean up any stray DBUS_SESSION_BUS_ADDRESS environment
  # variable too. Some of the bots seem to re-invoke runtest.py in a
  # way that this variable sticks around from run to run.
  if 'DBUS_SESSION_BUS_ADDRESS' in os.environ:
    del os.environ['DBUS_SESSION_BUS_ADDRESS']
    print ' cleared DBUS_SESSION_BUS_ADDRESS environment variable'


def _RunGTestCommand(command, extra_env, log_processor=None, pipes=None):
  """Runs a test, printing and possibly processing the output.

  Args:
    command: A list of strings in a command (the command and its arguments).
    extra_env: A dictionary of extra environment variables to set.
    log_processor: A log processor instance which has the ProcessLine method.
    pipes: A list of command string lists which the output will be piped to.

  Returns:
    The process return code.
  """
  env = os.environ.copy()
  if extra_env:
    print 'Additional test environment:'
    for k, v in sorted(extra_env.items()):
      print '  %s=%s' % (k, v)
  env.update(extra_env or {})

  # Trigger bot mode (test retries, redirection of stdio, possibly faster,
  # etc.) - using an environment variable instead of command-line flags because
  # some internal waterfalls run this (_RunGTestCommand) for totally non-gtest
  # code.
  # TODO(phajdan.jr): Clean this up when internal waterfalls are fixed.
  env.update({'CHROMIUM_TEST_LAUNCHER_BOT_MODE': '1'})

  if log_processor:
    return chromium_utils.RunCommand(
        command, pipes=pipes, parser_func=log_processor.ProcessLine, env=env)
  else:
    return chromium_utils.RunCommand(command, pipes=pipes, env=env)


def _GetMaster():
  """Returns a master name as listed in the slaves.cfg file."""
  return slave_utils.GetActiveMaster()


def _GetMasterString(master):
  """Returns a message describing what the master is."""
  return '[Running for master: "%s"]' % master


def _GetGitCommitPositionFromLog(log):
  """Returns either the commit position or svn rev from a git log."""
  # Parse from the bottom up, in case the commit message embeds the message
  # from a different commit (e.g., for a revert).
  for r in [GIT_CR_POS_RE, GIT_SVN_ID_RE]:
    for line in reversed(log.splitlines()):
      m = r.match(line.strip())
      if m:
        return m.group(1)
  return None


def _GetGitCommitPosition(dir_path):
  """Extracts the commit position or svn revision number of the HEAD commit."""
  git_exe = 'git.bat' if sys.platform.startswith('win') else 'git'
  p = subprocess.Popen(
      [git_exe, 'log', '-n', '1', '--pretty=format:%B', 'HEAD'],
      cwd=dir_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  (log, _) = p.communicate()
  if p.returncode != 0:
    return None
  return _GetGitCommitPositionFromLog(log)


def _IsGitDirectory(dir_path):
  """Checks whether the given directory is in a git repository.

  Args:
    dir_path: The directory path to be tested.

  Returns:
    True if given directory is in a git repository, False otherwise.
  """
  git_exe = 'git.bat' if sys.platform.startswith('win') else 'git'
  with open(os.devnull, 'w') as devnull:
    p = subprocess.Popen([git_exe, 'rev-parse', '--git-dir'],
                         cwd=dir_path, stdout=devnull, stderr=devnull)
    return p.wait() == 0


def _GetRevision(in_directory):
  """Returns the SVN revision, git commit position, or git hash.

  Args:
    in_directory: A directory in the repository to be checked.

  Returns:
    An SVN revision as a string if the given directory is in a SVN repository,
    or a git commit position number, or if that's not available, a git hash.
    If all of that fails, an empty string is returned.
  """
  import xml.dom.minidom
  if not os.path.exists(os.path.join(in_directory, '.svn')):
    if _IsGitDirectory(in_directory):
      svn_rev = _GetGitCommitPosition(in_directory)
      if svn_rev:
        return svn_rev
      return _GetGitRevision(in_directory)
    else:
      return ''

  # Note: Not thread safe: http://bugs.python.org/issue2320
  output = subprocess.Popen(['svn', 'info', '--xml'],
                            cwd=in_directory,
                            shell=(sys.platform == 'win32'),
                            stdout=subprocess.PIPE).communicate()[0]
  try:
    dom = xml.dom.minidom.parseString(output)
    return dom.getElementsByTagName('entry')[0].getAttribute('revision')
  except xml.parsers.expat.ExpatError:
    return ''
  return ''


def _GetGitRevision(in_directory):
  """Returns the git hash tag for the given directory.

  Args:
    in_directory: The directory where git is to be run.

  Returns:
    The git SHA1 hash string.
  """
  git_exe = 'git.bat' if sys.platform.startswith('win') else 'git'
  p = subprocess.Popen(
      [git_exe, 'rev-parse', 'HEAD'],
      cwd=in_directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  (stdout, _) = p.communicate()
  return stdout.strip()


def _GenerateJSONForTestResults(options, log_processor):
  """Generates or updates a JSON file from the gtest results XML and upload the
  file to the archive server.

  The archived JSON file will be placed at:
    www-dir/DEST_DIR/buildname/testname/results.json
  on the archive server. NOTE: This will be deprecated.

  Args:
    options: command-line options that are supposed to have build_dir,
        results_directory, builder_name, build_name and test_output_xml values.
    log_processor: An instance of PerformanceLogProcessor or similar class.

  Returns:
    True upon success, False upon failure.
  """
  results_map = None
  try:
    if (os.path.exists(options.test_output_xml) and
        not _UsingGtestJson(options)):
      results_map = gtest_slave_utils.GetResultsMapFromXML(
          options.test_output_xml)
    else:
      if _UsingGtestJson(options):
        sys.stderr.write('using JSON summary output instead of gtest XML\n')
      else:
        sys.stderr.write(
            ('"%s" \ "%s" doesn\'t exist: Unable to generate JSON from XML, '
             'using log output.\n') % (os.getcwd(), options.test_output_xml))
      # The file did not get generated. See if we can generate a results map
      # from the log output.
      results_map = gtest_slave_utils.GetResultsMap(log_processor)
  except Exception as e:
    # This error will be caught by the following 'not results_map' statement.
    print 'Error: ', e

  if not results_map:
    print 'No data was available to update the JSON results'
    # Consider this non-fatal.
    return True

  build_dir = os.path.abspath(options.build_dir)
  slave_name = slave_utils.SlaveBuildName(build_dir)

  generate_json_options = copy.copy(options)
  generate_json_options.build_name = slave_name
  generate_json_options.input_results_xml = options.test_output_xml
  generate_json_options.builder_base_url = '%s/%s/%s/%s' % (
      config.Master.archive_url, DEST_DIR, slave_name, options.test_type)
  generate_json_options.master_name = options.master_class_name or _GetMaster()
  generate_json_options.test_results_server = config.Master.test_results_server

  print _GetMasterString(generate_json_options.master_name)

  generator = None

  try:
    if options.revision:
      generate_json_options.chrome_revision = options.revision
    else:
      chrome_dir = chromium_utils.FindUpwardParent(build_dir, 'third_party')
      generate_json_options.chrome_revision = _GetRevision(chrome_dir)

    if options.webkit_revision:
      generate_json_options.webkit_revision = options.webkit_revision
    else:
      webkit_dir = chromium_utils.FindUpward(
        build_dir, 'third_party', 'WebKit', 'Source')
      generate_json_options.webkit_revision = _GetRevision(webkit_dir)

    # Generate results JSON file and upload it to the appspot server.
    generator = gtest_slave_utils.GenerateJSONResults(
        results_map, generate_json_options)

  except Exception as e:
    print 'Unexpected error while generating JSON: %s' % e
    sys.excepthook(*sys.exc_info())
    return False

  # The code can throw all sorts of exceptions, including
  # slave.gtest.networktransaction.NetworkTimeout so just trap everything.
  # Earlier versions of this code ignored network errors, so until a
  # retry mechanism is added, continue to do so rather than reporting
  # an error.
  try:
    # Upload results JSON file to the appspot server.
    gtest_slave_utils.UploadJSONResults(generator)
  except Exception as e:
    # Consider this non-fatal for the moment.
    print 'Unexpected error while uploading JSON: %s' % e
    sys.excepthook(*sys.exc_info())

  return True


def _BuildParallelCommand(build_dir, test_exe_path, options):
  """Builds a command to run a command in a parallel manner.

  Args:
    build_dir: Path to the tools/build directory.
    test_exe_path: Path to test command binary.
    options: Options passed for this invocation of runtest.py.

  Returns:
    A command, represented as a list of command parts.
  """
  command = [
      test_exe_path,
      '--test-launcher-bot-mode',
  ]

  if options.total_shards and options.shard_index:
    command.extend([
        '--test-launcher-total-shards=%d' % options.total_shards,
        '--test-launcher-shard-index=%d' % (options.shard_index - 1)])

  if options.sharding_args:
    command.extend(options.sharding_args.split())

  # Extra options for run_test_cases.py must be passed after the exe path.
  # TODO(earthdok): pass '--test-launcher-batch-limit' in extra_sharding_args.
  # No need for a separate factory property.
  cluster_size = options.factory_properties.get('cluster_size')
  if cluster_size is not None:
    command.append('--test-launcher-batch-limit=%s' % str(cluster_size))
  command.extend(options.extra_sharding_args.split())

  return command


def _StartHttpServer(platform, build_dir, test_exe_path, document_root):
  """Starts an Apache HTTP server instance.

  Args:
    platform: A string describing the platform: linux, mac or win.
    build_dir: Path of build directory.
    test_exe_path: Path to the test executable file.
    document_root: Document root to use for the HTTP server.

  Returns:
    The google.http_utils.ApacheHttpd instance that was created.
  """
  # The following warnings are related to the fact that another module named
  # "google" is in the module search path. See the note at the top of this file.
  # E0611: No name in module. F0401: Unable to import.
  # pylint: disable=E0611,F0401
  import google.httpd_utils
  import google.platform_utils
  # pylint: enable=E0611,F0401
  platform_util = google.platform_utils.PlatformUtility(build_dir)

  # Name the output directory for the exe, without its path or suffix.
  # e.g., chrome-release/httpd_logs/unit_tests/
  test_exe_name = os.path.splitext(os.path.basename(test_exe_path))[0]
  output_dir = os.path.join(slave_utils.SlaveBaseDir(build_dir),
                            'httpd_logs',
                            test_exe_name)

  # Sanity checks for httpd2_linux.conf.
  if platform == 'linux':
    for ssl_file in ['ssl.conf', 'ssl.load']:
      ssl_path = os.path.join('/etc/apache/mods-enabled', ssl_file)
      if not os.path.exists(ssl_path):
        sys.stderr.write('WARNING: %s missing, http server may not start\n' %
                         ssl_path)
    if not os.access('/var/run/apache2', os.W_OK):
      sys.stderr.write('WARNING: cannot write to /var/run/apache2, '
                       'http server may not start\n')

  apache_config_dir = google.httpd_utils.ApacheConfigDir(build_dir)
  httpd_conf_path = os.path.join(apache_config_dir, HTTPD_CONF[platform])
  mime_types_path = os.path.join(apache_config_dir, 'mime.types')
  document_root = os.path.abspath(document_root)

  start_cmd = platform_util.GetStartHttpdCommand(output_dir,
                                                 httpd_conf_path,
                                                 mime_types_path,
                                                 document_root)
  stop_cmd = platform_util.GetStopHttpdCommand()
  http_server = google.httpd_utils.ApacheHttpd(start_cmd, stop_cmd, [8000])
  try:
    http_server.StartServer()
  except google.httpd_utils.HttpdNotStarted as e:
    raise google.httpd_utils.HttpdNotStarted('%s. See log file in %s' %
                                             (e, output_dir))
  return http_server


def _UsingGtestJson(options):
  """Returns True if we're using GTest JSON summary."""
  return (options.annotate == 'gtest' and
          not options.run_python_script and
          not options.run_shell_script)


def _ListLogProcessors(selection):
  """Prints a list of available log processor classes iff the input is 'list'.

  Args:
    selection: A log processor name, or the string "list".

  Returns:
    True if a list was printed, False otherwise.
  """
  shouldlist = selection and selection == 'list'
  if shouldlist:
    print
    print 'Available log processors:'
    for p in LOG_PROCESSOR_CLASSES:
      print ' ', p, LOG_PROCESSOR_CLASSES[p].__name__

  return shouldlist


def _SelectLogProcessor(options):
  """Returns a log processor class based on the command line options.

  Args:
    options: Command-line options (from OptionParser).

  Returns:
    A log processor class, or None.
  """
  if _UsingGtestJson(options):
    return gtest_utils.GTestJSONParser

  if options.annotate:
    if options.annotate in LOG_PROCESSOR_CLASSES:
      if options.generate_json_file and options.annotate != 'gtest':
        raise NotImplementedError('"%s" doesn\'t make sense with '
                                  'options.generate_json_file.')
      else:
        return LOG_PROCESSOR_CLASSES[options.annotate]
    else:
      raise KeyError('"%s" is not a valid GTest parser!' % options.annotate)
  elif options.generate_json_file:
    return LOG_PROCESSOR_CLASSES['gtest']

  return None


def _GetCommitPos(build_properties):
  """Extracts the commit position from the build properties, if its there."""
  if 'got_revision_cp' not in build_properties:
    return None
  commit_pos = build_properties['got_revision_cp']
  return int(re.search(r'{#(\d+)}', commit_pos).group(1))


def _CreateLogProcessor(log_processor_class, options):
  """Creates a log processor instance.

  Args:
    log_processor_class: A subclass of PerformanceLogProcessor or similar class.
    options: Command-line options (from OptionParser).

  Returns:
    An instance of a log processor class, or None.
  """
  if not log_processor_class:
    return None

  if log_processor_class.__name__ in ('GTestLogParser',):
    tracker_obj = log_processor_class()
  elif log_processor_class.__name__ in ('GTestJSONParser',):
    tracker_obj = log_processor_class(
        options.build_properties.get('mastername'))
  else:
    build_dir = os.path.abspath(options.build_dir)

    if options.webkit_revision:
      webkit_revision = options.webkit_revision
    else:
      try:
        webkit_dir = chromium_utils.FindUpward(
            build_dir, 'third_party', 'WebKit', 'Source')
        webkit_revision = _GetRevision(webkit_dir)
      except Exception:
        webkit_revision = 'undefined'

    commit_pos_num = _GetCommitPos(options.build_properties)
    if commit_pos_num is not None:
      revision = commit_pos_num
    elif options.revision:
      revision = options.revision
    else:
      revision = _GetRevision(os.path.dirname(build_dir))

    tracker_obj = log_processor_class(
        revision=revision,
        build_properties=options.build_properties,
        factory_properties=options.factory_properties,
        webkit_revision=webkit_revision)

  if options.annotate and options.generate_json_file:
    tracker_obj.ProcessLine(_GetMasterString(_GetMaster()))

  return tracker_obj


def _GetSupplementalColumns(build_dir, supplemental_colummns_file_name):
  """Reads supplemental columns data from a file.

  Args:
    build_dir: Build dir name.
    supplemental_columns_file_name: Name of a file which contains the
        supplemental columns data (in JSON format).

  Returns:
    A dict of supplemental data to send to the dashboard.
  """
  supplemental_columns = {}
  supplemental_columns_file = os.path.join(build_dir,
                                           results_dashboard.CACHE_DIR,
                                           supplemental_colummns_file_name)
  if os.path.exists(supplemental_columns_file):
    with file(supplemental_columns_file, 'r') as f:
      supplemental_columns = json.loads(f.read())
  return supplemental_columns


def _SendResultsToDashboard(log_processor, system, test, url, build_dir,
                            mastername, buildername, buildnumber,
                            supplemental_columns_file, extra_columns=None):
  """Sends results from a log processor instance to the dashboard.

  Args:
    log_processor: An instance of a log processor class, which has been used to
        process the test output, so it contains the test results.
    system: A string such as 'linux-release', which comes from perf_id.
    test: Test "suite" name string.
    url: Dashboard URL.
    build_dir: Build dir name (used for cache file by results_dashboard).
    mastername: Buildbot master name, e.g. 'chromium.perf'.
        WARNING! This is incorrectly called "masterid" in some parts of the
        dashboard code.
    buildername: Builder name, e.g. 'Linux QA Perf (1)'
    buildnumber: Build number (as a string).
    supplemental_columns_file: Filename for JSON supplemental columns file.
    extra_columns: A dict of extra values to add to the supplemental columns
        dict.
  """
  if system is None:
    # perf_id not specified in factory properties.
    print 'Error: No system name (perf_id) specified when sending to dashboard.'
    return
  supplemental_columns = _GetSupplementalColumns(
      build_dir, supplemental_columns_file)
  if extra_columns:
    supplemental_columns.update(extra_columns)

  charts = _GetDataFromLogProcessor(log_processor)
  points = results_dashboard.MakeListOfPoints(
      charts, system, test, mastername, buildername, buildnumber,
      supplemental_columns)
  results_dashboard.SendResults(points, url, build_dir)


def _GetDataFromLogProcessor(log_processor):
  """Returns a mapping of chart names to chart data.

  Args:
    log_processor: A log processor (aka results tracker) object.

  Returns:
    A dictionary mapping chart name to lists of chart data.
    put together in log_processor. Each chart data dictionary contains:
      "traces": A dictionary mapping trace names to value, stddev pairs.
      "units": Units for the chart.
      "rev": A revision number or git hash.
      Plus other revision keys, e.g. webkit_rev, ver, v8_rev.
  """
  charts = {}
  for log_file_name, line_list in log_processor.PerformanceLogs().iteritems():
    if not log_file_name.endswith('-summary.dat'):
      # The log processor data also contains "graphs list" file contents,
      # which we can ignore.
      continue
    chart_name = log_file_name.replace('-summary.dat', '')

    # It's assumed that the log lines list has length one, because for each
    # graph name only one line is added in log_processor in the method
    # GraphingLogProcessor._CreateSummaryOutput.
    if len(line_list) != 1:
      print 'Error: Unexpected log processor line list: %s' % str(line_list)
      continue
    line = line_list[0].rstrip()
    try:
      charts[chart_name] = json.loads(line)
    except ValueError:
      print 'Error: Could not parse JSON: %s' % line
  return charts


def _BuildCoverageGtestExclusions(options, args):
  """Appends a list of GTest exclusion filters to the args list."""
  gtest_exclusions = {
    'win32': {
      'browser_tests': (
        'ChromeNotifierDelegateBrowserTest.ClickTest',
        'ChromeNotifierDelegateBrowserTest.ButtonClickTest',
        'SyncFileSystemApiTest.GetFileStatuses',
        'SyncFileSystemApiTest.WriteFileThenGetUsage',
        'NaClExtensionTest.HostedApp',
        'MediaGalleriesPlatformAppBrowserTest.MediaGalleriesCopyToNoAccess',
        'PlatformAppBrowserTest.ComponentAppBackgroundPage',
        'BookmarksTest.CommandAgainGoesBackToBookmarksTab',
        'NotificationBitmapFetcherBrowserTest.OnURLFetchFailureTest',
        'PreservedWindowPlacementIsMigrated.Test',
        'ShowAppListBrowserTest.ShowAppListFlag',
        '*AvatarMenuButtonTest.*',
        'NotificationBitmapFetcherBrowserTest.HandleImageFailedTest',
        'NotificationBitmapFetcherBrowserTest.OnImageDecodedTest',
        'NotificationBitmapFetcherBrowserTest.StartTest',
      )
    },
    'darwin2': {},
    'linux2': {},
  }
  gtest_exclusion_filters = []
  if sys.platform in gtest_exclusions:
    excldict = gtest_exclusions.get(sys.platform)
    if options.test_type in excldict:
      gtest_exclusion_filters = excldict[options.test_type]
  args.append('--gtest_filter=-' + ':'.join(gtest_exclusion_filters))


def _UploadProfilingData(options, args):
  """Archives profiling data to Google Storage."""
  # args[1] has --gtest-filter argument.
  if len(args) < 2:
    return 0

  builder_name = options.build_properties.get('buildername')
  if ((builder_name != 'XP Perf (dbg) (2)' and
       builder_name != 'Linux Perf (lowmem)') or
      options.build_properties.get('mastername') != 'chromium.perf' or
      not options.build_properties.get('got_revision')):
    return 0

  gtest_filter = args[1]
  if gtest_filter is None:
    return 0
  gtest_name = ''
  if gtest_filter.find('StartupTest.*') > -1:
    gtest_name = 'StartupTest'
  else:
    return 0

  build_dir = os.path.normpath(os.path.abspath(options.build_dir))

  # archive_profiling_data.py is in /b/build/scripts/slave and
  # build_dir is /b/build/slave/SLAVE_NAME/build/src/build.
  profiling_archive_tool = os.path.join(build_dir, '..', '..', '..', '..', '..',
                                        'scripts', 'slave',
                                        'archive_profiling_data.py')

  if sys.platform == 'win32':
    python = 'python_slave'
  else:
    python = 'python'

  revision = options.build_properties.get('got_revision')
  cmd = [python, profiling_archive_tool, '--revision', revision,
         '--builder-name', builder_name, '--test-name', gtest_name]

  return chromium_utils.RunCommand(cmd)


def _UploadGtestJsonSummary(json_path, build_properties, test_exe):
  """Archives GTest results to Google Storage."""
  if not os.path.exists(json_path):
    return

  orig_json_data = 'invalid'
  try:
    with open(json_path) as orig_json:
      orig_json_data = json.load(orig_json)
  except ValueError:
    pass

  target_json = {
    # Increment the version number when making incompatible changes
    # to the layout of this dict. This way clients can recognize different
    # formats instead of guessing.
    'version': 1,
    'timestamp': str(datetime.datetime.now()),
    'test_exe': test_exe,
    'build_properties': build_properties,
    'gtest_results': orig_json_data,
  }
  target_json_serialized = json.dumps(target_json, indent=2)

  now = datetime.datetime.utcnow()
  today = now.date()
  weekly_timestamp = today - datetime.timedelta(days=today.weekday())

  # Pick a non-colliding file name by hashing the JSON contents
  # (build metadata should be different from build to build).
  target_name = hashlib.sha1(target_json_serialized).hexdigest()

  # Use a directory structure that makes it easy to filter by year,
  # month, week and day based just on the file path.
  raw_json_gs_path = 'gs://chrome-gtest-results/raw/%d/%d/%d/%d/%s.json.gz' % (
      weekly_timestamp.year,
      weekly_timestamp.month,
      weekly_timestamp.day,
      today.day,
      target_name)

  fd, target_json_path = tempfile.mkstemp()
  try:
    with os.fdopen(fd, 'w') as f:
      with gzip.GzipFile(fileobj=f, compresslevel=9) as gzipf:
        gzipf.write(target_json_serialized)

    slave_utils.GSUtilCopy(target_json_path, raw_json_gs_path)
  finally:
    os.remove(target_json_path)

  if target_json['gtest_results'] == 'invalid':
    return

  # Use a directory structure that makes it easy to filter by year,
  # month, week and day based just on the file path.
  bigquery_json_gs_path = (
      'gs://chrome-gtest-results/bigquery/%d/%d/%d/%d/%s.json.gz' % (
          weekly_timestamp.year,
          weekly_timestamp.month,
          weekly_timestamp.day,
          today.day,
          target_name))

  fd, bigquery_json_path = tempfile.mkstemp()
  try:
    with os.fdopen(fd, 'w') as f:
      with gzip.GzipFile(fileobj=f, compresslevel=9) as gzipf:
        for iteration_data in (
            target_json['gtest_results']['per_iteration_data']):
          for test_name, test_runs in iteration_data.iteritems():
            # Compute the number of flaky failures. A failure is only considered
            # flaky, when the test succeeds at least once on the same code.
            # However, we do not consider a test flaky if it only changes
            # between various failure states, e.g. FAIL and TIMEOUT.
            num_successes = len([r['status'] for r in test_runs
                                             if r['status'] == 'SUCCESS'])
            num_failures = len(test_runs) - num_successes
            if num_failures > 0 and num_successes > 0:
              flaky_failures = num_failures
            else:
              flaky_failures = 0

            for run_index, run_data in enumerate(test_runs):
              row = {
                'test_name': test_name,
                'run_index': run_index,
                'elapsed_time_ms': run_data['elapsed_time_ms'],
                'status': run_data['status'],
                'test_exe': target_json['test_exe'],
                'global_tags': target_json['gtest_results']['global_tags'],
                'slavename':
                    target_json['build_properties'].get('slavename', ''),
                'buildername':
                    target_json['build_properties'].get('buildername', ''),
                'mastername':
                    target_json['build_properties'].get('mastername', ''),
                'raw_json_gs_path': raw_json_gs_path,
                'timestamp': now.strftime('%Y-%m-%d %H:%M:%S.%f'),
                'flaky_failures': flaky_failures,
                'num_successes': num_successes,
                'num_failures': num_failures
              }
              gzipf.write(json.dumps(row) + '\n')

    slave_utils.GSUtilCopy(bigquery_json_path, bigquery_json_gs_path)
  finally:
    os.remove(bigquery_json_path)


def _GenerateRunIsolatedCommand(build_dir, test_exe_path, options, command):
  """Converts the command to run through the run isolate script.

  All commands are sent through the run isolated script, in case
  they need to be run in isolate mode.
  """
  run_isolated_test = os.path.join(BASE_DIR, 'runisolatedtest.py')
  isolate_command = [
      sys.executable, run_isolated_test,
      '--test_name', options.test_type,
      '--builder_name', options.build_properties.get('buildername', ''),
      '--checkout_dir', os.path.dirname(os.path.dirname(build_dir)),
  ]
  if options.factory_properties.get('force_isolated'):
    isolate_command += ['--force-isolated']
  isolate_command += [test_exe_path, '--'] + command

  return isolate_command


def _GetPerfID(options):
  if options.perf_id:
    perf_id = options.perf_id
  else:
    perf_id = options.factory_properties.get('perf_id')
    if options.factory_properties.get('add_perf_id_suffix'):
      perf_id += options.build_properties.get('perf_id_suffix')
  return perf_id


def _MainParse(options, _args):
  """Run input through annotated test parser.

  This doesn't execute a test, but reads test input from a file and runs it
  through the specified annotation parser (aka log processor).
  """
  if not options.annotate:
    raise chromium_utils.MissingArgument('--parse-input doesn\'t make sense '
                                         'without --annotate.')

  # If --annotate=list was passed, list the log processor classes and exit.
  if _ListLogProcessors(options.annotate):
    return 0

  log_processor_class = _SelectLogProcessor(options)
  log_processor = _CreateLogProcessor(log_processor_class, options)

  if options.generate_json_file:
    if os.path.exists(options.test_output_xml):
      # remove the old XML output file.
      os.remove(options.test_output_xml)

  if options.parse_input == '-':
    f = sys.stdin
  else:
    try:
      f = open(options.parse_input, 'rb')
    except IOError as e:
      print 'Error %d opening \'%s\': %s' % (e.errno, options.parse_input,
                                             e.strerror)
      return 1

  with f:
    for line in f:
      log_processor.ProcessLine(line)

  if options.generate_json_file:
    if not _GenerateJSONForTestResults(options, log_processor):
      return 1

  if options.annotate:
    annotation_utils.annotate(
        options.test_type, options.parse_result, log_processor,
        options.factory_properties.get('full_test_name'),
        perf_dashboard_id=options.perf_dashboard_id)

  return options.parse_result


def _MainMac(options, args, extra_env):
  """Runs the test on mac."""
  if len(args) < 1:
    raise chromium_utils.MissingArgument('Usage: %s' % USAGE)

  test_exe = args[0]
  if options.run_python_script:
    build_dir = os.path.normpath(os.path.abspath(options.build_dir))
    test_exe_path = test_exe
  else:
    build_dir = os.path.normpath(os.path.abspath(options.build_dir))
    test_exe_path = os.path.join(build_dir, options.target, test_exe)

  # Nuke anything that appears to be stale chrome items in the temporary
  # directory from previous test runs (i.e.- from crashes or unittest leaks).
  slave_utils.RemoveChromeTemporaryFiles()

  if options.parallel:
    command = _BuildParallelCommand(build_dir, test_exe_path, options)
  elif options.run_shell_script:
    command = ['bash', test_exe_path]
  elif options.run_python_script:
    command = [sys.executable, test_exe]
  else:
    command = [test_exe_path]
    if options.annotate == 'gtest':
      command.extend(['--brave-new-test-launcher', '--test-launcher-bot-mode'])
  command.extend(args[1:])

  # If --annotate=list was passed, list the log processor classes and exit.
  if _ListLogProcessors(options.annotate):
    return 0
  log_processor_class = _SelectLogProcessor(options)
  log_processor = _CreateLogProcessor(log_processor_class, options)

  if options.generate_json_file:
    if os.path.exists(options.test_output_xml):
      # remove the old XML output file.
      os.remove(options.test_output_xml)

  try:
    http_server = None
    if options.document_root:
      http_server = _StartHttpServer('mac', build_dir=build_dir,
                                      test_exe_path=test_exe_path,
                                      document_root=options.document_root)

    if _UsingGtestJson(options):
      json_file_name = log_processor.PrepareJSONFile(
          options.test_launcher_summary_output)
      command.append('--test-launcher-summary-output=%s' % json_file_name)

    pipes = []
    if options.enable_asan:
      symbolize = os.path.abspath(os.path.join('src', 'tools', 'valgrind',
                                               'asan', 'asan_symbolize.py'))
      pipes = [[sys.executable, symbolize], ['c++filt']]

    command = _GenerateRunIsolatedCommand(build_dir, test_exe_path, options,
                                          command)
    result = _RunGTestCommand(command, extra_env, pipes=pipes,
                              log_processor=log_processor)
  finally:
    if http_server:
      http_server.StopServer()
    if _UsingGtestJson(options):
      _UploadGtestJsonSummary(json_file_name,
                              options.build_properties,
                              test_exe)
      log_processor.ProcessJSONFile(options.build_dir)

  if options.generate_json_file:
    if not _GenerateJSONForTestResults(options, log_processor):
      return 1

  if options.annotate:
    annotation_utils.annotate(
        options.test_type, result, log_processor,
        options.factory_properties.get('full_test_name'),
        perf_dashboard_id=options.perf_dashboard_id)

  if options.results_url:
    _SendResultsToDashboard(
        log_processor, _GetPerfID(options),
        options.test_type, options.results_url, options.build_dir,
        options.build_properties.get('mastername'),
        options.build_properties.get('buildername'),
        options.build_properties.get('buildnumber'),
        options.supplemental_columns_file,
        options.perf_config)

  return result


def _MainIOS(options, args, extra_env):
  """Runs the test on iOS."""
  if len(args) < 1:
    raise chromium_utils.MissingArgument('Usage: %s' % USAGE)

  def kill_simulator():
    chromium_utils.RunCommand(['/usr/bin/killall', 'iPhone Simulator'])

  # For iOS tests, the args come in in the following order:
  #   [0] test display name formatted as 'test_name (device[ ios_version])'
  #   [1:] gtest args (e.g. --gtest_print_time)

  # Set defaults in case the device family and iOS version can't be parsed out
  # of |args|
  device = 'iPhone Retina (4-inch)'
  ios_version = '7.1'

  # Parse the test_name and device from the test display name.
  # The expected format is: <test_name> (<device>)
  result = re.match(r'(.*) \((.*)\)$', args[0])
  if result is not None:
    test_name, device = result.groups()
    # Check if the device has an iOS version. The expected format is:
    # <device_name><space><ios_version>, where ios_version may have 2 or 3
    # numerals (e.g. '4.3.11' or '5.0').
    result = re.match(r'(.*) (\d+\.\d+(\.\d+)?)$', device)
    if result is not None:
      device = result.groups()[0]
      ios_version = result.groups()[1]
  else:
    # If first argument is not in the correct format, log a warning but
    # fall back to assuming the first arg is the test_name and just run
    # on the iphone simulator.
    test_name = args[0]
    print ('Can\'t parse test name, device, and iOS version. '
           'Running %s on %s %s' % (test_name, device, ios_version))

  # Build the args for invoking iossim, which will install the app on the
  # simulator and launch it, then dump the test results to stdout.

  build_dir = os.path.normpath(os.path.abspath(options.build_dir))
  app_exe_path = os.path.join(
      build_dir, options.target + '-iphonesimulator', test_name + '.app')
  test_exe_path = os.path.join(
      build_dir, 'ninja-iossim', options.target, 'iossim')
  tmpdir = tempfile.mkdtemp()
  command = [test_exe_path,
      '-d', device,
      '-s', ios_version,
      '-t', '120',
      '-u', tmpdir,
      app_exe_path, '--'
  ]
  command.extend(args[1:])

  # If --annotate=list was passed, list the log processor classes and exit.
  if _ListLogProcessors(options.annotate):
    return 0
  log_processor = _CreateLogProcessor(LOG_PROCESSOR_CLASSES['gtest'], options)

  # Make sure the simulator isn't running.
  kill_simulator()

  # Nuke anything that appears to be stale chrome items in the temporary
  # directory from previous test runs (i.e.- from crashes or unittest leaks).
  slave_utils.RemoveChromeTemporaryFiles()

  dirs_to_cleanup = [tmpdir]
  crash_files_before = set([])
  crash_files_after = set([])
  crash_files_before = set(crash_utils.list_crash_logs())

  result = _RunGTestCommand(command, extra_env, log_processor)

  # Because test apps kill themselves, iossim sometimes returns non-zero
  # status even though all tests have passed.  Check the log_processor to
  # see if the test run was successful.
  if log_processor.CompletedWithoutFailure():
    result = 0
  else:
    result = 1

  if result != 0:
    crash_utils.wait_for_crash_logs()
  crash_files_after = set(crash_utils.list_crash_logs())

  kill_simulator()

  new_crash_files = crash_files_after.difference(crash_files_before)
  crash_utils.print_new_crash_files(new_crash_files)

  for a_dir in dirs_to_cleanup:
    try:
      chromium_utils.RemoveDirectory(a_dir)
    except OSError as e:
      print >> sys.stderr, e
      # Don't fail.

  return result


def _MainLinux(options, args, extra_env):
  """Runs the test on Linux."""
  if len(args) < 1:
    raise chromium_utils.MissingArgument('Usage: %s' % USAGE)

  build_dir = os.path.normpath(os.path.abspath(options.build_dir))
  if options.slave_name:
    slave_name = options.slave_name
  else:
    slave_name = slave_utils.SlaveBuildName(build_dir)
  bin_dir = os.path.join(build_dir, options.target)

  # Figure out what we want for a special frame buffer directory.
  special_xvfb_dir = None
  if options.special_xvfb == 'auto':
    fp_special_xvfb = options.factory_properties.get('special_xvfb', None)
    fp_chromeos = options.factory_properties.get('chromeos', None)
    if fp_special_xvfb or (fp_special_xvfb is None and (fp_chromeos or
        slave_utils.GypFlagIsOn(options, 'use_aura') or
        slave_utils.GypFlagIsOn(options, 'chromeos'))):
      special_xvfb_dir = options.special_xvfb_dir
  elif options.special_xvfb:
    special_xvfb_dir = options.special_xvfb_dir

  test_exe = args[0]
  if options.run_python_script:
    test_exe_path = test_exe
  else:
    test_exe_path = os.path.join(bin_dir, test_exe)
  if not os.path.exists(test_exe_path):
    if options.factory_properties.get('succeed_on_missing_exe', False):
      print '%s missing but succeed_on_missing_exe used, exiting' % (
          test_exe_path)
      return 0
    msg = 'Unable to find %s' % test_exe_path
    raise chromium_utils.PathNotFound(msg)

  # Unset http_proxy and HTTPS_PROXY environment variables.  When set, this
  # causes some tests to hang.  See http://crbug.com/139638 for more info.
  if 'http_proxy' in os.environ:
    del(os.environ['http_proxy'])
    print 'Deleted http_proxy environment variable.'
  if 'HTTPS_PROXY' in os.environ:
    del(os.environ['HTTPS_PROXY'])
    print 'Deleted HTTPS_PROXY environment variable.'

  # Decide whether to enable the suid sandbox for Chrome.
  if (_ShouldEnableSandbox(CHROME_SANDBOX_PATH) and
      not options.enable_tsan and
      not options.enable_lsan):
    print 'Enabling sandbox.  Setting environment variable:'
    print '  CHROME_DEVEL_SANDBOX="%s"' % CHROME_SANDBOX_PATH
    extra_env['CHROME_DEVEL_SANDBOX'] = CHROME_SANDBOX_PATH
  else:
    print 'Disabling sandbox.  Setting environment variable:'
    print '  CHROME_DEVEL_SANDBOX=""'
    extra_env['CHROME_DEVEL_SANDBOX'] = ''

  # Nuke anything that appears to be stale chrome items in the temporary
  # directory from previous test runs (i.e.- from crashes or unittest leaks).
  slave_utils.RemoveChromeTemporaryFiles()

  extra_env['LD_LIBRARY_PATH'] = ''

  if options.enable_lsan:
    # Use the debug version of libstdc++ under LSan. If we don't, there will be
    # a lot of incomplete stack traces in the reports.
    extra_env['LD_LIBRARY_PATH'] += '/usr/lib/x86_64-linux-gnu/debug:'

  extra_env['LD_LIBRARY_PATH'] += '%s:%s/lib:%s/lib.target' % (bin_dir, bin_dir,
                                                               bin_dir)
  # Figure out what we want for a special llvmpipe directory.
  if options.llvmpipe_dir and os.path.exists(options.llvmpipe_dir):
    extra_env['LD_LIBRARY_PATH'] += ':' + options.llvmpipe_dir

  if options.parallel:
    command = _BuildParallelCommand(build_dir, test_exe_path, options)
  elif options.run_shell_script:
    command = ['bash', test_exe_path]
  elif options.run_python_script:
    command = [sys.executable, test_exe]
  else:
    command = [test_exe_path]
    if options.annotate == 'gtest':
      command.extend(['--brave-new-test-launcher', '--test-launcher-bot-mode'])
  command.extend(args[1:])

  # If --annotate=list was passed, list the log processor classes and exit.
  if _ListLogProcessors(options.annotate):
    return 0
  log_processor_class = _SelectLogProcessor(options)
  log_processor = _CreateLogProcessor(log_processor_class, options)

  if options.generate_json_file:
    if os.path.exists(options.test_output_xml):
      # remove the old XML output file.
      os.remove(options.test_output_xml)

  try:
    start_xvfb = False
    http_server = None
    json_file_name = None
    if options.document_root:
      http_server = _StartHttpServer('linux', build_dir=build_dir,
                                      test_exe_path=test_exe_path,
                                      document_root=options.document_root)

    # TODO(dpranke): checking on test_exe is a temporary hack until we
    # can change the buildbot master to pass --xvfb instead of --no-xvfb
    # for these two steps. See
    # https://code.google.com/p/chromium/issues/detail?id=179814
    start_xvfb = (options.xvfb or
                  'layout_test_wrapper' in test_exe or
                  'devtools_perf_test_wrapper' in test_exe)
    if start_xvfb:
      xvfb.StartVirtualX(
          slave_name, bin_dir,
          with_wm=(options.factory_properties.get('window_manager', 'True') ==
                   'True'),
          server_dir=special_xvfb_dir)

    if _UsingGtestJson(options):
      json_file_name = log_processor.PrepareJSONFile(
          options.test_launcher_summary_output)
      command.append('--test-launcher-summary-output=%s' % json_file_name)

    pipes = []
    # See the comment in main() regarding offline symbolization.
    if (options.enable_asan or options.enable_msan) and not options.enable_lsan:
      symbolize = os.path.abspath(os.path.join('src', 'tools', 'valgrind',
                                               'asan', 'asan_symbolize.py'))
      asan_symbolize = [sys.executable, symbolize]
      if options.strip_path_prefix:
        asan_symbolize.append(options.strip_path_prefix)
      pipes = [asan_symbolize]

    command = _GenerateRunIsolatedCommand(build_dir, test_exe_path, options,
                                          command)
    result = _RunGTestCommand(command, extra_env, pipes=pipes,
                              log_processor=log_processor)
  finally:
    if http_server:
      http_server.StopServer()
    if start_xvfb:
      xvfb.StopVirtualX(slave_name)
    if _UsingGtestJson(options):
      if json_file_name:
        _UploadGtestJsonSummary(json_file_name,
                                options.build_properties,
                                test_exe)
      log_processor.ProcessJSONFile(options.build_dir)

  if options.generate_json_file:
    if not _GenerateJSONForTestResults(options, log_processor):
      return 1

  if options.annotate:
    annotation_utils.annotate(
        options.test_type, result, log_processor,
        options.factory_properties.get('full_test_name'),
        perf_dashboard_id=options.perf_dashboard_id)

  if options.results_url:
    _SendResultsToDashboard(
        log_processor, _GetPerfID(options),
        options.test_type, options.results_url, options.build_dir,
        options.build_properties.get('mastername'),
        options.build_properties.get('buildername'),
        options.build_properties.get('buildnumber'),
        options.supplemental_columns_file,
        options.perf_config)

  return result


def _MainWin(options, args, extra_env):
  """Runs tests on windows.

  Using the target build configuration, run the executable given in the
  first non-option argument, passing any following arguments to that
  executable.

  Args:
    options: Command-line options for this invocation of runtest.py.
    args: Command and arguments for the test.
    extra_env: A dictionary of extra environment variables to set.

  Returns:
    Exit status code.
  """
  if len(args) < 1:
    raise chromium_utils.MissingArgument('Usage: %s' % USAGE)

  test_exe = args[0]
  build_dir = os.path.abspath(options.build_dir)
  if options.run_python_script:
    test_exe_path = test_exe
  else:
    test_exe_path = os.path.join(build_dir, options.target, test_exe)

  if not os.path.exists(test_exe_path):
    if options.factory_properties.get('succeed_on_missing_exe', False):
      print '%s missing but succeed_on_missing_exe used, exiting' % (
          test_exe_path)
      return 0
    raise chromium_utils.PathNotFound('Unable to find %s' % test_exe_path)

  if options.enable_pageheap:
    slave_utils.SetPageHeap(build_dir, 'chrome.exe', True)

  if options.parallel:
    command = _BuildParallelCommand(build_dir, test_exe_path, options)
  elif options.run_python_script:
    command = [sys.executable, test_exe]
  else:
    command = [test_exe_path]
    if options.annotate == 'gtest':
      command.extend(['--brave-new-test-launcher', '--test-launcher-bot-mode'])

  # The ASan tests needs to run under agent_logger in order to get the stack
  # traces. The win SyzyASan builder is responsible to put it in the
  # build_dir/target/ directory.
  if options.factory_properties.get('syzyasan') or options.use_syzyasan_logger:
    logfile = test_exe_path + '.asan_log'
    command = ['%s' % os.path.join(build_dir,
                                   options.target,
                                   'agent_logger.exe'),
               'start',
               '--output-file=%s' % logfile,
               '--'] + command
  command.extend(args[1:])

  # Nuke anything that appears to be stale chrome items in the temporary
  # directory from previous test runs (i.e.- from crashes or unittest leaks).
  slave_utils.RemoveChromeTemporaryFiles()

  # If --annotate=list was passed, list the log processor classes and exit.
  if _ListLogProcessors(options.annotate):
    return 0
  log_processor_class = _SelectLogProcessor(options)
  log_processor = _CreateLogProcessor(log_processor_class, options)

  if options.generate_json_file:
    if os.path.exists(options.test_output_xml):
      # remove the old XML output file.
      os.remove(options.test_output_xml)

  try:
    http_server = None
    if options.document_root:
      http_server = _StartHttpServer('win', build_dir=build_dir,
                                      test_exe_path=test_exe_path,
                                      document_root=options.document_root)

    if _UsingGtestJson(options):
      json_file_name = log_processor.PrepareJSONFile(
          options.test_launcher_summary_output)
      command.append('--test-launcher-summary-output=%s' % json_file_name)

    command = _GenerateRunIsolatedCommand(build_dir, test_exe_path, options,
                                          command)
    result = _RunGTestCommand(command, extra_env, log_processor)
  finally:
    if http_server:
      http_server.StopServer()
    if _UsingGtestJson(options):
      _UploadGtestJsonSummary(json_file_name,
                              options.build_properties,
                              test_exe)
      log_processor.ProcessJSONFile(options.build_dir)

  if options.enable_pageheap:
    slave_utils.SetPageHeap(build_dir, 'chrome.exe', False)

  if options.generate_json_file:
    if not _GenerateJSONForTestResults(options, log_processor):
      return 1

  if options.annotate:
    annotation_utils.annotate(
        options.test_type, result, log_processor,
        options.factory_properties.get('full_test_name'),
        perf_dashboard_id=options.perf_dashboard_id)

  if options.results_url:
    _SendResultsToDashboard(
        log_processor, _GetPerfID(options),
        options.test_type, options.results_url, options.build_dir,
        options.build_properties.get('mastername'),
        options.build_properties.get('buildername'),
        options.build_properties.get('buildnumber'),
        options.supplemental_columns_file,
        options.perf_config)

  return result


def _MainAndroid(options, args, extra_env):
  """Runs tests on android.

  Running GTest-based tests on android is different than on Linux as it requires
  src/build/android/test_runner.py to deploy and communicate with the device.
  Python scripts are the same as with Linux.

  Args:
    options: Command-line options for this invocation of runtest.py.
    args: Command and arguments for the test.
    extra_env: A dictionary of extra environment variables to set.

  Returns:
    Exit status code.
  """
  if options.run_python_script:
    return _MainLinux(options, args, extra_env)

  if len(args) < 1:
    raise chromium_utils.MissingArgument('Usage: %s' % USAGE)

  if _ListLogProcessors(options.annotate):
    return 0
  log_processor_class = _SelectLogProcessor(options)
  log_processor = _CreateLogProcessor(log_processor_class, options)

  if options.generate_json_file:
    if os.path.exists(options.test_output_xml):
      # remove the old XML output file.
      os.remove(options.test_output_xml)

  # Assume it's a gtest apk, so use the android harness.
  test_suite = args[0]
  run_test_target_option = '--release'
  if options.target == 'Debug':
    run_test_target_option = '--debug'
  command = ['src/build/android/test_runner.py', 'gtest',
             run_test_target_option, '-s', test_suite]
  result = _RunGTestCommand(command, extra_env, log_processor=log_processor)

  if options.generate_json_file:
    if not _GenerateJSONForTestResults(options, log_processor):
      return 1

  if options.annotate:
    annotation_utils.annotate(
        options.test_type, result, log_processor,
        options.factory_properties.get('full_test_name'),
        perf_dashboard_id=options.perf_dashboard_id)

  if options.results_url:
    _SendResultsToDashboard(
        log_processor, _GetPerfID(options),
        options.test_type, options.results_url, options.build_dir,
        options.build_properties.get('mastername'),
        options.build_properties.get('buildername'),
        options.build_properties.get('buildnumber'),
        options.supplemental_columns_file,
        options.perf_config)

  return result


def main():
  """Entry point for runtest.py.

  This function:
    (1) Sets up the command-line options.
    (2) Sets environment variables based on those options.
    (3) Delegates to the platform-specific main functions.

  Returns:
    Exit code for this script.
  """
  import platform

  xvfb_path = os.path.join(os.path.dirname(sys.argv[0]), '..', '..',
                           'third_party', 'xvfb', platform.architecture()[0])

  # Initialize logging.
  log_level = logging.INFO
  logging.basicConfig(level=log_level,
                      format='%(asctime)s %(filename)s:%(lineno)-3d'
                             ' %(levelname)s %(message)s',
                      datefmt='%y%m%d %H:%M:%S')

  option_parser = optparse.OptionParser(usage=USAGE)

  # Since the trailing program to run may have has command-line args of its
  # own, we need to stop parsing when we reach the first positional argument.
  option_parser.disable_interspersed_args()

  option_parser.add_option('--target', default='Release',
                           help='build target (Debug or Release)')
  option_parser.add_option('--pass-target', action='store_true', default=False,
                           help='pass --target to the spawned test script')
  option_parser.add_option('--build-dir', help='ignored')
  option_parser.add_option('--pass-build-dir', action='store_true',
                           default=False,
                           help='pass --build-dir to the spawned test script')
  option_parser.add_option('--test-platform',
                           help='Platform to test on, e.g. ios-simulator')
  option_parser.add_option('--enable-pageheap', action='store_true',
                           default=False,
                           help='enable pageheap checking for chrome.exe')
  # --with-httpd assumes a chromium checkout with src/tools/python.
  option_parser.add_option('--with-httpd', dest='document_root',
                           default=None, metavar='DOC_ROOT',
                           help='Start a local httpd server using the given '
                                'document root, relative to the current dir')
  option_parser.add_option('--total-shards', dest='total_shards',
                           default=None, type='int',
                           help='Number of shards to split this test into.')
  option_parser.add_option('--shard-index', dest='shard_index',
                           default=None, type='int',
                           help='Shard to run. Must be between 1 and '
                                'total-shards.')
  option_parser.add_option('--run-shell-script', action='store_true',
                           default=False,
                           help='treat first argument as the shell script'
                                'to run.')
  option_parser.add_option('--run-python-script', action='store_true',
                           default=False,
                           help='treat first argument as a python script'
                                'to run.')
  option_parser.add_option('--generate-json-file', action='store_true',
                           default=False,
                           help='output JSON results file if specified.')
  option_parser.add_option('--parallel', action='store_true',
                           help='Shard and run tests in parallel for speed '
                                'with sharding_supervisor.')
  option_parser.add_option('--llvmpipe', action='store_const',
                           const=xvfb_path, dest='llvmpipe_dir',
                           help='Use software gpu pipe directory.')
  option_parser.add_option('--no-llvmpipe', action='store_const',
                           const=None, dest='llvmpipe_dir',
                           help='Do not use software gpu pipe directory.')
  option_parser.add_option('--llvmpipe-dir',
                           default=None, dest='llvmpipe_dir',
                           help='Path to software gpu library directory.')
  option_parser.add_option('--special-xvfb-dir', default=xvfb_path,
                           help='Path to virtual X server directory on Linux.')
  option_parser.add_option('--special-xvfb', action='store_true',
                           default='auto',
                           help='use non-default virtual X server on Linux.')
  option_parser.add_option('--no-special-xvfb', action='store_false',
                           dest='special_xvfb',
                           help='Use default virtual X server on Linux.')
  option_parser.add_option('--auto-special-xvfb', action='store_const',
                           const='auto', dest='special_xvfb',
                           help='Guess as to virtual X server on Linux.')
  option_parser.add_option('--xvfb', action='store_true', dest='xvfb',
                           default=True,
                           help='Start virtual X server on Linux.')
  option_parser.add_option('--no-xvfb', action='store_false', dest='xvfb',
                           help='Do not start virtual X server on Linux.')
  option_parser.add_option('--sharding-args', dest='sharding_args',
                           default='',
                           help='Options to pass to sharding_supervisor.')
  option_parser.add_option('-o', '--results-directory', default='',
                           help='output results directory for JSON file.')
  option_parser.add_option('--builder-name', default=None,
                           help='The name of the builder running this script.')
  option_parser.add_option('--slave-name', default=None,
                           help='The name of the slave running this script.')
  option_parser.add_option('--master-class-name', default=None,
                           help='The class name of the buildbot master running '
                                'this script: examples include "Chromium", '
                                '"ChromiumWebkit", and "ChromiumGPU". The '
                                'flakiness dashboard uses this value to '
                                'categorize results. See buildershandler.py '
                                'in the flakiness dashboard code '
                                '(use codesearch) for the known values. '
                                'Defaults to fetching it from slaves.cfg.')
  option_parser.add_option('--build-number', default=None,
                           help=('The build number of the builder running'
                                 'this script.'))
  option_parser.add_option('--test-type', default='',
                           help='The test name that identifies the test, '
                                'e.g. \'unit-tests\'')
  option_parser.add_option('--test-results-server', default='',
                           help='The test results server to upload the '
                                'results.')
  option_parser.add_option('--annotate', default='',
                           help='Annotate output when run as a buildstep. '
                                'Specify which type of test to parse, available'
                                ' types listed with --annotate=list.')
  option_parser.add_option('--parse-input', default='',
                           help='When combined with --annotate, reads test '
                                'from a file instead of executing a test '
                                'binary. Use - for stdin.')
  option_parser.add_option('--parse-result', default=0,
                           help='Sets the return value of the simulated '
                                'executable under test. Only has meaning when '
                                '--parse-input is used.')
  option_parser.add_option('--results-url', default='',
                           help='The URI of the perf dashboard to upload '
                                'results to.')
  option_parser.add_option('--perf-dashboard-id', default='',
                           help='The ID on the perf dashboard to add results '
                                'to.')
  option_parser.add_option('--perf-id', default='',
                           help='The perf builder id')
  option_parser.add_option('--perf-config', default='',
                           help='Perf configuration dictionary (as a string). '
                                'This allows to specify custom revisions to be '
                                'the main revision at the Perf dashboard. '
                                'Example: --perf-config="{\'a_default_rev\': '
                                '\'r_webrtc_rev\'}"')
  option_parser.add_option('--supplemental-columns-file',
                           default='supplemental_columns',
                           help='A file containing a JSON blob with a dict '
                                'that will be uploaded to the results '
                                'dashboard as supplemental columns.')
  option_parser.add_option('--revision',
                           help='The revision number which will be is used as '
                                'primary key by the dashboard. If omitted it '
                                'is automatically extracted from the checkout.')
  option_parser.add_option('--webkit-revision',
                           help='See --revision.')
  option_parser.add_option('--enable-asan', action='store_true', default=False,
                           help='Enable fast memory error detection '
                                '(AddressSanitizer). Can also enabled with the '
                                'factory property "asan" (deprecated).')
  option_parser.add_option('--enable-lsan', action='store_true', default=False,
                           help='Enable memory leak detection (LeakSanitizer). '
                                'Can also be enabled with the factory '
                                'property "lsan" (deprecated).')
  option_parser.add_option('--lsan-suppressions-file',
                           default='src/tools/lsan/suppressions.txt',
                           help='Suppression file for LeakSanitizer. '
                                'Default: %default.')
  option_parser.add_option('--enable-msan', action='store_true', default=False,
                           help='Enable uninitialized memory reads detection '
                                '(MemorySanitizer). Can also enabled with the '
                                'factory property "msan" (deprecated).')
  option_parser.add_option('--enable-tsan', action='store_true', default=False,
                           help='Enable data race detection '
                                '(ThreadSanitizer). Can also enabled with the '
                                'factory property "tsan" (deprecated).')
  option_parser.add_option('--strip-path-prefix',
                           default='build/src/out/Release/../../',
                           help='Source paths in stack traces will be stripped '
                           'of prefixes ending with this substring. This '
                           'option is used by sanitizer tools.')
  option_parser.add_option('--extra-sharding-args', default='',
                           help='Extra options for run_test_cases.py.')
  option_parser.add_option('--no-spawn-dbus', action='store_true',
                           default=False,
                           help='Disable GLib DBus bug workaround: '
                                'manually spawning dbus-launch')
  option_parser.add_option('--use-syzyasan-logger', action='store_true',
                           default=False,
                           help='Run the tests under the SyzyASan logger.')
  option_parser.add_option('--test-launcher-summary-output',
                           help='Path to test results file with all the info '
                                'from the test launcher')

  chromium_utils.AddPropertiesOptions(option_parser)
  options, args = option_parser.parse_args()
  if not options.perf_dashboard_id:
    options.perf_dashboard_id = options.factory_properties.get('test_name')

  options.test_type = options.test_type or options.factory_properties.get(
      'step_name', '')

  if options.run_shell_script and options.run_python_script:
    sys.stderr.write('Use either --run-shell-script OR --run-python-script, '
                     'not both.')
    return 1

  print '[Running on builder: "%s"]' % options.builder_name

  did_launch_dbus = False
  if not options.no_spawn_dbus:
    did_launch_dbus = _LaunchDBus()

  try:
    options.build_dir = build_directory.GetBuildOutputDirectory()

    if options.pass_target and options.target:
      args.extend(['--target', options.target])
    if options.pass_build_dir:
      args.extend(['--build-dir', options.build_dir])

    options.enable_asan = (options.enable_asan or
                           options.factory_properties.get('asan', False))
    options.enable_msan = (options.enable_msan or
                           options.factory_properties.get('msan', False))
    options.enable_tsan = (options.enable_tsan or
                           options.factory_properties.get('tsan', False))
    options.enable_lsan = (options.enable_lsan or
                           options.factory_properties.get('lsan', False))

    extra_sharding_args_list = options.extra_sharding_args.split()

    # run_test_cases.py and the brave new test launcher need different flags to
    # enable verbose output. We support both.
    always_print_test_output = ['--verbose',
                                '--test-launcher-print-test-stdio=always']
    if options.enable_asan:
      extra_sharding_args_list.extend(always_print_test_output)
    if options.enable_tsan:
      # Print ThreadSanitizer reports; don't cluster the tests so that TSan exit
      # code denotes a test failure.
      extra_sharding_args_list.extend(always_print_test_output)
      extra_sharding_args_list.append('--clusters=1')
      # Data races may be flaky, but we don't want to restart the test if
      # there's been a race report.
      options.sharding_args += ' --retries 0'

    options.extra_sharding_args = ' '.join(extra_sharding_args_list)

    # We will use this to accumulate overrides for the command under test,
    # That we may not need or want for other support commands.
    extra_env = {}

    if (options.enable_asan or options.enable_tsan or
        options.enable_msan or options.enable_lsan):
      # Instruct GTK to use malloc while running ASan, TSan, MSan or LSan tests.
      extra_env['G_SLICE'] = 'always-malloc'
      extra_env['NSS_DISABLE_ARENA_FREE_LIST'] = '1'
      extra_env['NSS_DISABLE_UNLOAD'] = '1'

    # TODO(glider): remove the symbolizer path once
    # https://code.google.com/p/address-sanitizer/issues/detail?id=134 is fixed.
    symbolizer_path = os.path.abspath(os.path.join('src', 'third_party',
        'llvm-build', 'Release+Asserts', 'bin', 'llvm-symbolizer'))
    strip_path_prefix = options.strip_path_prefix
    disable_sandbox_flag = '--no-sandbox'
    if args and 'layout_test_wrapper' in args[0]:
      disable_sandbox_flag = '--additional-drt-flag=%s' % disable_sandbox_flag

    # Symbolization of sanitizer reports.
    if options.enable_tsan or options.enable_lsan:
      # TSan and LSan are not sandbox-compatible, so we can use online
      # symbolization. In fact, they need symbolization to be able to apply
      # suppressions.
      symbolization_options = ['symbolize=1',
                               'external_symbolizer_path=%s' % symbolizer_path,
                               'strip_path_prefix=%s' %  strip_path_prefix]
    else:
      # ASan and MSan use a script for offline symbolization.
      # Important note: when running ASan or MSan with leak detection enabled,
      # we must use the LSan symbolization options above.
      symbolization_options = ['symbolize=0']
      # Set the path to llvm-symbolizer to be used by asan_symbolize.py
      extra_env['LLVM_SYMBOLIZER_PATH'] = symbolizer_path

    # ThreadSanitizer
    if options.enable_tsan:
      tsan_options = symbolization_options
      extra_env['TSAN_OPTIONS'] = ' '.join(tsan_options)
      # Disable sandboxing under TSan for now. http://crbug.com/223602.
      args.append(disable_sandbox_flag)

    # LeakSanitizer
    if options.enable_lsan:
      # Symbolization options set here take effect only for standalone LSan.
      lsan_options = symbolization_options + \
                     ['suppressions=%s' % options.lsan_suppressions_file,
                      'print_suppressions=1']
      extra_env['LSAN_OPTIONS'] = ' '.join(lsan_options)
      # Disable sandboxing under LSan.
      args.append(disable_sandbox_flag)

    # AddressSanitizer
    if options.enable_asan:
      # Avoid aggressive memcmp checks until http://crbug.com/178677 is
      # fixed.  Also do not replace memcpy/memmove/memset to suppress a
      # report in OpenCL, see http://crbug.com/162461.
      asan_options = symbolization_options + \
                     ['strict_memcmp=0',
                      'replace_intrin=0',
                      'strip_path_prefix=%s ' % strip_path_prefix]
      if options.enable_lsan:
        asan_options += ['detect_leaks=1']
      extra_env['ASAN_OPTIONS'] = ' '.join(asan_options)

      # ASan is not yet sandbox-friendly on Windows (http://crbug.com/382867).
      if sys.platform == 'win32':
        args.append(disable_sandbox_flag)

    # MemorySanitizer
    if options.enable_msan:
      msan_options = symbolization_options
      if options.enable_lsan:
        msan_options += ['detect_leaks=1']
      extra_env['MSAN_OPTIONS'] = ' '.join(msan_options)

    # Set the number of shards environement variables.
    if options.total_shards and options.shard_index:
      extra_env['GTEST_TOTAL_SHARDS'] = str(options.total_shards)
      extra_env['GTEST_SHARD_INDEX'] = str(options.shard_index - 1)

    # If perf config is passed via command line, parse the string into a dict.
    if options.perf_config:
      try:
        options.perf_config = ast.literal_eval(options.perf_config)
        assert type(options.perf_config) is dict, (
            'Value of --perf-config couldn\'t be evaluated into a dict.')
      except (exceptions.SyntaxError, ValueError):
        option_parser.error('Failed to parse --perf-config value into a dict: '
                            '%s' % options.perf_config)
        return 1

    # Allow factory property 'perf_config' as well during a transition period.
    options.perf_config = (options.perf_config or
                           options.factory_properties.get('perf_config'))

    if options.results_directory:
      options.test_output_xml = os.path.normpath(os.path.abspath(os.path.join(
          options.results_directory, '%s.xml' % options.test_type)))
      args.append('--gtest_output=xml:' + options.test_output_xml)
    elif options.generate_json_file:
      option_parser.error(
          '--results-directory is required with --generate-json-file=True')
      return 1

    if options.factory_properties.get('coverage_gtest_exclusions', False):
      _BuildCoverageGtestExclusions(options, args)

    temp_files = _GetTempCount()
    if options.parse_input:
      result = _MainParse(options, args)
    elif sys.platform.startswith('darwin'):
      test_platform = options.factory_properties.get(
          'test_platform', options.test_platform)
      if test_platform in ('ios-simulator',):
        result = _MainIOS(options, args, extra_env)
      else:
        result = _MainMac(options, args, extra_env)
    elif sys.platform == 'win32':
      result = _MainWin(options, args, extra_env)
    elif sys.platform == 'linux2':
      if options.factory_properties.get('test_platform',
            options.test_platform) == 'android':
        result = _MainAndroid(options, args, extra_env)
      else:
        result = _MainLinux(options, args, extra_env)
    else:
      sys.stderr.write('Unknown sys.platform value %s\n' % repr(sys.platform))
      return 1

    _UploadProfilingData(options, args)

    new_temp_files = _GetTempCount()
    if temp_files > new_temp_files:
      print >> sys.stderr, (
          'Confused: %d files were deleted from %s during the test run') % (
              (temp_files - new_temp_files), tempfile.gettempdir())
    elif temp_files < new_temp_files:
      print >> sys.stderr, (
          '%d new files were left in %s: Fix the tests to clean up themselves.'
          ) % ((new_temp_files - temp_files), tempfile.gettempdir())
      # TODO(maruel): Make it an error soon. Not yet since I want to iron
      # out all the remaining cases before.
      #result = 1
    return result
  finally:
    if did_launch_dbus:
      # It looks like the command line argument --exit-with-session
      # isn't working to clean up the spawned dbus-daemon. Kill it
      # manually.
      _ShutdownDBus()


if '__main__' == __name__:
  sys.exit(main())
