# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from subordinate import recipe_test_api

from . import builders


class ChromiumTestApi(recipe_test_api.RecipeTestApi):
  @property
  def builders(self):
    return builders.BUILDERS
