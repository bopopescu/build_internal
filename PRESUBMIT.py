# Copyright (c) 2010 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Top-level presubmit script for buildbot.

See http://dev.chromium.org/developers/how-tos/depottools/presubmit-scripts for
details on the presubmit API built into gcl.
"""

def CheckChangeOnUpload(input_api, output_api):
  output = []
  output.extend(RunPylint(input_api, output_api))
  return output


def CheckChangeOnCommit(input_api, output_api):
  output = []
  output.extend(RunPylint(input_api, output_api))
  return output


def RunPylint(input_api, output_api):
  """Run pylint -E on most python files in buildbot."""
  # In short, it is
  # PYTHONPATH=third_party:third_party/buildbot_7_12:third_party/twisted_8_1:scripts:site_config pylint -E *.py
  import os
  try:
    env = input_api.environ.copy()
    path = [env.get('PYTHONPATH', None)]
    path.append('third_party')
    path.append(input_api.os_path.join('third_party', 'buildbot_7_12'))
    path.append(input_api.os_path.join('third_party', 'twisted_8_1'))
    path.append(input_api.os_path.join('scripts'))
    path.append(input_api.os_path.join('site_config'))
    env['PYTHONPATH'] = input_api.os_path.pathsep.join(p for p in path if p)
    files = []
    for dirpath, dirnames, filenames in os.walk('.'):
      for item in dirnames[:]:
        if (item.startswith('.') or
            item in ('build', 'depot_tools', 'third_party', 'unittests')):
          dirnames.remove(item)
      files.extend(input_api.os_path.join(dirpath, f)
                   for f in filenames
                   if f.endswith('.py'))
    proc = input_api.subprocess.Popen(['pylint'] + sorted(files), env=env)
    proc.communicate()
    if proc.returncode:
      return [output_api.PresubmitPromptWarning('Fix pylint errors first.')]
    return []
  except OSError:
    if input_api.platform == 'win32':
      # It's windows, give up.
      return []
    return [output_api.PresubmitError(
        'Please install pylint with "sudo apt-get install python-setuptools; '
        'sudo easy_install pylint"')]
