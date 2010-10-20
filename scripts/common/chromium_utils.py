# Copyright (c) 2010 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

""" Set of basic operations/utilities that are used by the build. """

import copy
import errno
import fnmatch
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

# Local errors.
class MissingArgument(Exception): pass
class PathNotFound(Exception): pass
class ExternalError(Exception): pass


def IsWindows():
  return sys.platform == 'cygwin' or sys.platform.startswith('win')

def IsLinux():
  return sys.platform.startswith('linux')

def IsMac():
  return sys.platform.startswith('darwin')

def IsWine():
  return (os.environ.get('WINE') and
          os.environ.get('WINEPREFIX') and
          os.environ.get('WINESERVER'))

# For chromeos we need to end up with a different platform name, but the
# scripts use the values like sys.platform for both the build target and
# and the running OS, so this gives us a back door that can be hit to
# force different naming then the default for some of the chromeos build
# steps.
override_platform_name = None


def OverridePlatformName(name):
  """Sets the override for PlatformName()"""
  global override_platform_name
  override_platform_name = name


def PlatformName():
  """Return a string to be used in paths for the platform."""
  if override_platform_name:
    return override_platform_name
  if IsWindows():
    return 'win32'
  if IsLinux():
    return 'linux'
  if IsMac():
    return 'mac'
  raise NotImplementedError('Unknown platform "%s".' % sys.platform)


# GetParentClass allows a class instance to find its parent class using Python's
# inspect module.  This allows a class instantiated from a module to access
# their parent class's methods even after the containing module has been
# re-imported and reloaded.
#
# Also see:
#   http://code.google.com/p/chromium/issues/detail?id=34089
#   http://atlee.ca/blog/2008/11/21/python-reload-danger-here-be-dragons/
#
def GetParentClass(obj, n=1):
  import inspect
  return inspect.getmro(obj.__class__)[n]


def MeanAndStandardDeviation(data):
  """Calculates mean and standard deviation for the values in the list.

    Args:
      data: list of numbers

    Returns:
      Mean and standard deviation for the numbers in the list.
  """
  n = len(data)
  if n == 0:
    return 0.0, 0.0
  mean = float(sum(data)) / n
  variance = sum([(element - mean)**2 for element in data]) / n
  return mean, math.sqrt(variance)


def FilteredMeanAndStandardDeviation(data):
  """Calculates mean and standard deviation for the values in the list
  ignoring first occurence of max value.

    Args:
      data: list of numbers

    Returns:
      Mean and standard deviation for the numbers in the list ignoring
      first occurence of max value.
  """
  def _FilterMax(array):
    new_array = copy.copy(array) # making sure we are not creating side-effects
    new_array.remove(max(new_array))
    return new_array
  return MeanAndStandardDeviation(_FilterMax(data))


class InitializePartiallyWithArguments:
  """Function currying implementation.

  Works for constructors too. Primary use is to be able to construct a class
  with some constructor arguments beings set ahead of actual initialization.
  Copy of an ASPN cookbook (#52549).
  """
  def __init__(self, clazz, *args, **kwargs):
    self.clazz = clazz
    self.pending = args[:]
    self.kwargs = kwargs.copy()

  def __call__(self, *args, **kwargs):
    if kwargs and self.kwargs:
      kw = self.kwargs.copy()
      kw.update(kwargs)
    else:
      kw = kwargs or self.kwargs

    return self.clazz(*(self.pending + args), **kw)


def Prepend(filepath, text):
  """ Prepends text to the file.

  Creates the file if it does not exist.
  """
  file_data = text
  if os.path.exists(filepath):
    file_data += open(filepath).read()
  f = open(filepath, 'w')
  f.write(file_data)
  f.close()


def MakeWorldReadable(path):
  """Change the permissions of the given path to make it world-readable.
  This is often needed for archived files, so they can be served by web servers
  or accessed by unprivileged network users."""
  perms = stat.S_IMODE(os.stat(path)[stat.ST_MODE])
  if os.path.isdir(path):
    # Directories need read and exec.
    os.chmod(path, perms | 0555)
  else:
    os.chmod(path, perms | 0444)


def MaybeMakeDirectory(*path):
  """Creates an entire path, if it doesn't already exist."""
  file_path = os.path.join(*path)
  try:
    os.makedirs(file_path)
  except OSError, e:
    if e.errno != errno.EEXIST:
      raise


def RemoveFile(*path):
  """Removes the file located at 'path', if it exists."""
  file_path = os.path.join(*path)
  try:
    os.remove(file_path)
  except OSError, e:
    if e.errno != errno.ENOENT:
      raise

def MoveFile(path, new_path):
  """Moves the file located at 'path' to 'new_path', if it exists."""
  try:
    RemoveFile(new_path)
    os.rename(path, new_path)
  except OSError, e:
    if e.errno != errno.ENOENT:
      raise

def LocateFiles(pattern, root=os.curdir):
  """Yeilds files matching pattern found in root and its subdirectories.

  An exception is thrown if root doesn't exist."""
  for path, _, files in os.walk(os.path.abspath(root)):
    for filename in fnmatch.filter(files, pattern):
      yield os.path.join(path, filename)


def RemoveFilesWildcards(file_wildcard, root=os.curdir):
  """Removes files matching 'file_wildcard' in root and its subdirectories, if
  any exists.

  An exception is thrown if root doesn't exist."""
  for item in LocateFiles(file_wildcard, root):
    try:
      os.remove(item)
    except OSError, e:
      if e.errno != errno.ENOENT:
        raise


def RemoveDirectory(*path):
  """Recursively removes a directory, even if it's marked read-only.

  Remove the directory located at *path, if it exists.

  shutil.rmtree() doesn't work on Windows if any of the files or directories
  are read-only, which svn repositories and some .svn files are.  We need to
  be able to force the files to be writable (i.e., deletable) as we traverse
  the tree.

  Even with all this, Windows still sometimes fails to delete a file, citing
  a permission error (maybe something to do with antivirus scans or disk
  indexing).  The best suggestion any of the user forums had was to wait a
  bit and try again, so we do that too.  It's hand-waving, but sometimes it
  works. :/
  """
  file_path = os.path.join(*path)
  if not os.path.exists(file_path):
    return

  def RemoveWithRetry_win(rmfunc, path):
    os.chmod(path, stat.S_IWRITE)
    if win32_api_avail:
      win32api.SetFileAttributes(path, win32con.FILE_ATTRIBUTE_NORMAL)
    try:
      return rmfunc(path)
    except EnvironmentError, e:
      if e.errno != errno.EACCES:
        raise
      print 'Failed to delete %s: trying again' % repr(path)
      time.sleep(0.1)
      return rmfunc(path)

  def RemoveWithRetry_non_win(rmfunc, path):
    if os.path.islink(path):
      return os.remove(path)
    else:
      return rmfunc(path)

  win32_api_avail = False
  remove_with_retry = None
  if sys.platform.startswith('win'):
    # Some people don't have the APIs installed. In that case we'll do without.
    try:
      win32api = __import__('win32api')
      win32con = __import__('win32con')
      win32_api_avail = True
    except ImportError:
      pass
    remove_with_retry = RemoveWithRetry_win
  else:
    remove_with_retry = RemoveWithRetry_non_win

  for root, dirs, files in os.walk(file_path, topdown=False):
    # For POSIX:  making the directory writable guarantees removability.
    # Windows will ignore the non-read-only bits in the chmod value.
    os.chmod(root, 0770)
    for name in files:
      remove_with_retry(os.remove, os.path.join(root, name))
    for name in dirs:
      remove_with_retry(shutil.rmtree, os.path.join(root, name))

  remove_with_retry(os.rmdir, file_path)


def CopyFileToDir(src_path, dest_dir):
  """Copies the file found at src_path to the same filename within the
  dest_dir directory.

  Raises PathNotFound if either the file or the directory is not found.
  """
  # Verify the file and directory separately so we can tell them apart and
  # raise PathNotFound rather than shutil.copyfile's IOError.
  if not os.path.isfile(src_path):
    raise PathNotFound('Unable to find file %s' % src_path)
  if not os.path.isdir(dest_dir):
    raise PathNotFound('Unable to find dir %s' % dest_dir)
  src_file = os.path.basename(src_path)
  shutil.copy(src_path, os.path.join(dest_dir, src_file))


def MakeZip(output_dir, archive_name, file_list, file_relative_dir,
            raise_error=True, remove_archive_directory=True):
  """Packs files into a new zip archive.

  Files are first copied into a directory within the output_dir named for
  the archive_name, which will be created if necessary and emptied if it
  already exists.  The files are then then packed using archive names
  relative to the output_dir.  That is, if the zipfile is unpacked in place,
  it will create a directory identical to the new archiev_name directory, in
  the output_dir.  The zip file will be named as the archive_name, plus
  '.zip'.

  Args:
    output_dir: Absolute path to the directory in which the archive is to
      be created.
    archive_dir: Subdirectory of output_dir holding files to be added to
      the new zipfile.
    file_list: List of paths to files or subdirectories, relative to the
      file_relative_dir.
    file_relative_dir: Absolute path to the directory containing the files
      and subdirectories in the file_list.
    raise_error: Whether to raise a PathNotFound error if one of the files in
      the list is not found.
    remove_archive_directory: Whether to remove the archive staging directory
      before copying files over to it.

  Returns:
    A tuple consisting of (archive_dir, zip_file_path), where archive_dir
    is the full path to the newly created archive_name subdirectory.

  Raises:
    PathNotFound if any of the files in the list is not found, unless
    raise_error is False, in which case the error will be ignored.
  """

  # Collect files into the archive directory.
  archive_dir = os.path.join(output_dir, archive_name)
  if remove_archive_directory:
    RemoveDirectory(archive_dir)
  MaybeMakeDirectory(archive_dir)
  for needed_file in file_list:
    needed_file = needed_file.rstrip()
    # These paths are relative to the file_relative_dir.  We need to copy
    # them over maintaining the relative directories, where applicable.
    src_path = os.path.join(file_relative_dir, needed_file)
    dirname, basename = os.path.split(needed_file)
    try:
      if os.path.isdir(src_path):
        shutil.copytree(src_path, os.path.join(archive_dir, needed_file),
                        symlinks=True)
      elif dirname != '' and basename != '':
        dest_dir = os.path.join(archive_dir, dirname)
        MaybeMakeDirectory(dest_dir)
        CopyFileToDir(src_path, dest_dir)
      else:
        CopyFileToDir(src_path, archive_dir)
    except PathNotFound:
      if raise_error:
        raise

  # Pack the zip file.
  output_file = '%s.zip' % archive_dir
  previous_file = '%s_old.zip' % archive_dir
  MoveFile(output_file, previous_file)
  # On Windows we use the python zip module; on Linux and Mac, we use the zip
  # command as it will handle links and file bits (executable).  Which is much
  # easier then trying to do that with ZipInfo options.
  if IsWindows():
    print 'Creating %s' % output_file
    def _Addfiles(to_zip_file, dirname, files_to_add):
      for this_file in files_to_add:
        archive_name = this_file
        this_path = os.path.join(dirname, this_file)
        if os.path.isfile(this_path):
          # Store files named relative to the outer output_dir.
          archive_name = this_path.replace(output_dir + os.sep, '')
          to_zip_file.write(this_path, archive_name)
          print 'Adding %s' % archive_name
    zip_file = zipfile.ZipFile(output_file, 'w', zipfile.ZIP_DEFLATED)
    try:
      os.path.walk(archive_dir, _Addfiles, zip_file)
    finally:
      zip_file.close()
  else:
    assert IsMac() or IsLinux()
    saved_dir = os.getcwd()
    os.chdir(os.path.dirname(archive_dir))
    command = ['zip', '-yr1', output_file, os.path.basename(archive_dir)]
    result = RunCommand(command)
    os.chdir(saved_dir)
    if result and raise_error:
      raise ExternalError('zip failed: %s => %s' %
                          (str(command), result))
  return (archive_dir, output_file)


def ExtractZip(filename, output_dir, verbose=True):
  """ Extract the zip archive in the output directory.
      Based on http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/252508.
  """
  MaybeMakeDirectory(output_dir)

  # On Windows we use the python zip module; on Linux and Mac, we use the zip
  # command as it will handle links and file bits (executable).  Which is much
  # easier then trying to do that with ZipInfo options.
  if IsWindows():
    zf = zipfile.ZipFile(filename)

    # Grabs all the directories in the zip structure. This is necessary
    # to create the structure before trying to extract the file to it.
    dirs = set([os.path.dirname(n) for n in zf.namelist()])

    # Create the directory structure.
    for item in dirs:
      MaybeMakeDirectory(os.path.join(output_dir, item))

    # Extract files to directory structure.
    for name in zf.namelist():
      if verbose:
        print 'Extracting %s' % name
      outfile = open(os.path.join(output_dir, name), 'wb')
      outfile.write(zf.read(name))
      outfile.close()
  else:
    assert IsMac() or IsLinux()
    # Make sure path is absolute before changing directories.
    filepath = os.path.abspath(filename)
    saved_dir = os.getcwd()
    os.chdir(output_dir)
    command = ['unzip', '-o', filepath]
    result = RunCommand(command)
    os.chdir(saved_dir)
    if result:
      raise ExternalError('unzip failed: %s => %s' %
                          (str(command), result))


def WindowsPath(path):
  """Returns a Windows mixed-style absolute path, given a Cygwin absolute path.

  The version of Python in the Chromium tree uses posixpath for os.path even
  on Windows, so we convert to a mixed Windows path (that is, a Windows path
  that uses forward slashes instead of backslashes) manually.
  """
  # TODO(pamg): make this work for other drives too.
  if path.startswith('/cygdrive/c/'):
    return path.replace('/cygdrive/c/', 'C:/')
  return path


def FindUpwardParent(start_dir, *desired_list):
  """Finds the desired object's parent, searching upward from the start_dir.

  Searches within start_dir and within all its parents looking for the desired
  directory or file, which may be given in one or more path components. Returns
  the first directory in which the top desired path component was found, or
  raises PathNotFound if it wasn't.
  """
  desired_path = os.path.join(*desired_list)
  last_dir = ''
  cur_dir = start_dir
  found_path = os.path.join(cur_dir, desired_path)
  while not os.path.exists(found_path):
    last_dir = cur_dir
    cur_dir = os.path.dirname(cur_dir)
    if last_dir == cur_dir:
      raise PathNotFound('Unable to find %s above %s' %
                         (desired_path, start_dir))
    found_path = os.path.join(cur_dir, desired_path)
  # Strip the entire original desired path from the end of the one found
  # and remove a trailing path separator, if present.
  found_path = found_path[:len(found_path) - len(desired_path)]
  if found_path.endswith(os.sep):
    found_path = found_path[:len(found_path) - 1]
  return found_path


def FindUpward(start_dir, *desired_list):
  """Returns a path to the desired directory or file, searching upward.

  Searches within start_dir and within all its parents looking for the desired
  directory or file, which may be given in one or more path components. Returns
  the full path to the desired object, or raises PathNotFound if it wasn't
  found.
  """
  parent = FindUpwardParent(start_dir, *desired_list)
  return os.path.join(parent, *desired_list)


def RunAndPrintDots(function):
  """Starts a background thread that prints dots while the function runs."""
  def Hook(*args, **kwargs):
    event = threading.Event()
    def PrintDots():
      counter = 0
      while not event.isSet():
        event.wait(5)
        sys.stdout.write('.')
        counter = (counter + 1) % 80
        if not counter:
          sys.stdout.write('\n')
        sys.stdout.flush()
    t = threading.Thread(target=PrintDots)
    t.start()
    try:
      return function(*args, **kwargs)
    finally:
      event.set()
      t.join()
  return Hook


def RunCommand(command, parser_func=None, **kwargs):
  """Runs the command list, printing its output and returning its exit status.

  Prints the given command (which should be a list of one or more strings),
  then runs it and writes its stdout and stderr to the appropriate file handles.

  If parser_func is not given, the subprocess's output is passed to stdout
  and stderr directly.  If the func is given, each line of the subprocess's
  stdout/stderr is passed to the func and then written to stdout.

  We do not currently support parsing stdout and stderr independent of
  each other.  In previous attempts, this led to output ordering issues.
  By merging them when either needs to be parsed, we avoid those ordering
  issues completely.
  """

  # TODO(all): nsylvain's CommandRunner in buildbot_slave is based on this
  # method.  Update it when changes are introduced here.
  def ProcessRead(readfh, writefh, parser_func=None):
    last_flushed_at = time.time()
    writefh.flush()

    in_byte = readfh.read(1)
    in_line = ''
    while in_byte:
      # Capture all characters except \r.
      if in_byte != '\r':
        in_line += in_byte

      # Write and flush on newline.
      if in_byte == '\n':
        if parser_func:
          parser_func(in_line.strip())
        # Python on Windows writes the buffer only when it reaches 4k.  This is
        # not fast enough.  Flush each 10 seconds instead.  Also, we only write
        # and flush no more often than 10 seconds to avoid flooding the master
        # with network traffic from unbuffered output.
        writefh.write(in_line)
        if (time.time() - last_flushed_at) > 10:
          last_flushed_at = time.time()
          writefh.flush()
        in_line = ''
      in_byte = readfh.read(1)

    # Write remaining data and flush on EOF.
    writefh.write(in_line)
    writefh.flush()

  # Print the given command (which should be a list of one or more strings).
  print '\n' + subprocess.list2cmdline(command) + '\n',
  sys.stdout.flush()
  sys.stderr.flush()
  if not parser_func:
    # Run the command.  The stdout and stderr file handles are passed to the
    # subprocess directly for writing.  No processing happens on the output of
    # the subprocess.
    proc = subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr,
                            bufsize=0, **kwargs)
  else:
    # Run the command.
    proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0, **kwargs)
    # Launch and start the reader thread.
    thread = threading.Thread(target=ProcessRead,
                              args=(proc.stdout, sys.stdout, parser_func))
    thread.start()
    # Wait for the reader thread to complete (implies EOF reached on stdout/
    # stderr pipes).
    thread.join()
  # Wait for the command to terminate.
  proc.wait()
  return proc.returncode


def GetCommandOutput(command):
  """Runs the command list, returning its output.

  Prints the given command (which should be a list of one or more strings),
  then runs it and returns its output (stdout and stderr) as a string.

  If the command exits with an error, raises ExternalError.
  """
  proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, bufsize=1)
  output = proc.communicate()[0]
  result = proc.returncode
  if result:
    raise ExternalError('%s: %s' % (subprocess.list2cmdline(command), output))
  return output


def GetGClientCommand(platform=None):
  """Returns the executable command name, depending on the platform.
  """
  if not platform:
    platform = sys.platform
  if platform.startswith('win'):
    # Windows doesn't want to depend on bash.
    return 'gclient.bat'
  else:
    return 'gclient'


# Linux scripts use ssh to to move files to the archive host.
def SshMakeDirectory(host, dest_path):
  """Creates the entire dest_path on the remote ssh host.
  """
  command = ['ssh', host, 'mkdir', '-p', dest_path]
  result = RunCommand(command)
  if result:
    raise ExternalError('Failed to ssh mkdir "%s" on "%s" (%s)' %
                        (dest_path, host, result))


def SshCopyFiles(srcs, host, dst):
  """Copies the srcs file(s) to dst on the remote ssh host.
  dst is expected to exist.
  """
  command = ['scp', srcs, host + ':' + dst]
  result = RunCommand(command)
  if result:
    raise ExternalError('Failed to scp "%s" to "%s" (%s)' %
                        (srcs, host + ':' + dst, result))


def SshExtractZip(host, zipname, dst):
  """extract the remote zip file to dst on the remote ssh host.
  """
  command = ['ssh', host, 'unzip', '-o', '-d', dst, zipname]
  result = RunCommand(command)
  if result:
    raise ExternalError('Failed to ssh unzip -o -d "%s" "%s" on "%s" (%s)' %
                        (dst, zipname, host, result))

  # unzip will create directories with access 700, which is not often what we
  # need. Fix the permissions for the whole archive.
  command = ['ssh', host, 'chmod', '-R', '755', dst]
  result = RunCommand(command)
  if result:
    raise ExternalError('Failed to ssh chmod -R 755 "%s" on "%s" (%s)' %
                        (dst, host, result))


def SshCopyTree(srctree, host, dst):
  """Recursively copies the srctree to dst on the remote ssh host.
  For consistency with shutil, dst is expected to not exist.
  """
  command = ['ssh', host, '[ -d "%s" ]' % dst]
  result = RunCommand(command)
  if result:
    raise ExternalError('SshCopyTree destination directory "%s" already exists.'
                       % host + ':' + dst)

  SshMakeDirectory(host, os.path.dirname(dst))
  command = ['scp', '-r', '-p', srctree, host + ':' + dst]
  result = RunCommand(command)
  if result:
    raise ExternalError('Failed to scp "%s" to "%s" (%s)' %
                       (srctree, host + ':' + dst, result))


def LogAndRemoveFiles(temp_dir, regex_pattern):
  """Remove files in |temp_dir| that match |regex_pattern|.
  This function prints out the name of each directory or filename before
  it deletes the file from disk."""
  regex = re.compile(regex_pattern)
  for dir_item in os.listdir(temp_dir):
    if regex.search(dir_item):
      full_path = os.path.join(temp_dir, dir_item)
      print "Removing leaked temp item: %s" % full_path
      try:
        if os.path.islink(full_path) or os.path.isfile(full_path):
          os.remove(full_path)
        elif os.path.isdir(full_path):
          RemoveDirectory(full_path)
        else:
          print "Temp item wasn't a file or directory?"
      except OSError, e:
        print >> sys.stderr, e
        # Don't fail.


def RemoveChromeTemporaryFiles():
  """A large hammer to nuke what could be leaked files from unittests or
  files left from a unittest that crashed, was killed, etc."""
  # NOTE: print out what is cleaned up so the bots don't timeout if
  # there is a lot to cleanup and also se we see the leaks in the
  # build logs.
  kLogRegex = '^(com\.google\.chrome|org\.chromium|\.org\.chromium)\.'
  if IsWindows():
    kLogRegex = '^(scoped_dir|nps|chrome_test|SafeBrowseringTest)'
    LogAndRemoveFiles(tempfile.gettempdir(), kLogRegex)
  elif IsLinux():
    LogAndRemoveFiles(tempfile.gettempdir(), kLogRegex)
    LogAndRemoveFiles('/dev/shm', kLogRegex)
  elif IsMac():
    nstempdir_path = '/usr/local/libexec/nstempdir'
    if os.path.exists(nstempdir_path):
      ns_temp_dir = GetCommandOutput([nstempdir_path]).strip()
      if ns_temp_dir:
        LogAndRemoveFiles(ns_temp_dir, kLogRegex)
  else:
    raise NotImplementedError(
        'Platform "%s" is not currently supported.' % sys.platform)
