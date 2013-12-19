# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from slave import recipe_api

class WebRTCApi(recipe_api.RecipeApi):
  def __init__(self, **kwargs):
    super(WebRTCApi, self).__init__(**kwargs)
    self._env = {}

  NORMAL_TESTS = [
      'audio_decoder_unittests',
      'common_audio_unittests',
      'common_video_unittests',
      'libjingle_media_unittest',
      'libjingle_p2p_unittest',
      'libjingle_peerconnection_unittest',
      'libjingle_sound_unittest',
      'libjingle_unittest',
      'metrics_unittests',
      'modules_tests',
      'modules_unittests',
      'neteq_unittests',
      'system_wrappers_unittests',
      'test_support_unittests',
      'tools_unittests',
      'video_engine_core_unittests',
      'video_engine_tests',
      'voice_engine_unittests',
  ]

  def apply_svn_patch(self):
    script = self.m.path.build('scripts', 'slave', 'apply_svn_patch.py')
    args = ['-p', self.m.properties['patch_url'],
            '-r', self.c.patch_root_dir]

    # Allow manipulating patches for try jobs.
    if self.c.patch_filter_script and self.c.patch_path_filter:
      args += ['--filter-script', self.c.patch_filter_script,
               '--strip-level', self.c.patch_strip_level,
               '--', '--path-filter', self.c.patch_path_filter]
    return self.m.python('apply_patch', script, args)