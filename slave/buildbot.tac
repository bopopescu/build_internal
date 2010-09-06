# -*- python -*-
# ex: set syntax=python:

# Copyright (c) 2009 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Chrome Buildbot slave configuration

import os
import sys

from twisted.application import service
from buildbot.slave.bot import BuildSlave

# Register the commands.
import chromium_commands
# Load default settings.
import chromium_config as config


# Determines the slave type:
ActiveMaster = config.Master.Chromium


# Slave properties:
slavename = None
password = config.Master.GetBotPassword()
host = None
port = None
basedir = None
keepalive = 600
usepty = 1
umask = None


if slavename is None:
    # Automatically determine the host name.
    import socket
    slavename = socket.getfqdn().split('.')[0]

if password is None:
    msg = '*** No password configured in %s.\n' % repr(__file__)
    sys.stderr.write(msg)
    sys.exit(1)

if host is None:
    host = ActiveMaster.master_host

if port is None:
    port = ActiveMaster.slave_port

if basedir is None:
    dir, _file = os.path.split(__file__)
    basedir = os.path.abspath(dir)


application = service.Application('buildslave')
s = BuildSlave(host, port, slavename, password, basedir, keepalive, usepty,
               umask=umask)
s.setServiceParent(application)
