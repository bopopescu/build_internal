#!/usr/bin/env python
# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import subprocess
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



class SwarmBotSetupTest(unittest.TestCase):
  def capture(self, args):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, 'swarm_bot_setup.py')] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    out = proc.communicate()[0]
    self.assertEqual(0, proc.returncode, out)
    return out

  def testWin(self):
    out = self.capture(['-p', '-w', '-b', 'joe'])
    expected = [
      'sftp chrome-bot@joe',
      '  rmdir /b/swarm_slave',
      '  mkdir /b',
      '  mkdir /b/swarm_slave',
      '  put swarm_bootstrap/* /b/swarm_slave',
      '  exit',
      'ssh -o ConnectTimeout=5 -t chrome-bot@joe cmd.exe /c '
        '"e: && '
        'xcopy /i /e /h /y c:\\b\\swarm_slave e:\\b\\swarm_slave\\ &&'
        ' cd e:\\b\\swarm_slave\\ && '
        'python get_swarm_code.py https://chromium-swarm.appspot.com && '
        'call swarm_bot_setup.bat '
        'https://chromium-swarm.appspot.com e:\\b\\swarm_slave\\"',
    ]
    self.assertEqual(expected, out.splitlines())

  def testMac(self):
    out = self.capture(['-p', '-m', '-b', 'joe'])
    expected = [
      'sftp chrome-bot@joe',
      '  rmdir /b/swarm_slave',
      '  mkdir /b',
      '  mkdir /b/swarm_slave',
      '  put swarm_bootstrap/* /b/swarm_slave',
      '  exit',
      'ssh -o ConnectTimeout=5 -t chrome-bot@joe cd /b/swarm_slave && '
        'python get_swarm_code.py https://chromium-swarm.appspot.com && '
        './swarm_bot_setup.sh https://chromium-swarm.appspot.com '
        '/b/swarm_slave',
    ]
    self.assertEqual(expected, out.splitlines())


if __name__ == '__main__':
  unittest.TestCase.maxDiff = None
  unittest.main()
