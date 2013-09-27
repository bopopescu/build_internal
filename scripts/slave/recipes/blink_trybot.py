# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

DEPS = [
  'chromium',
  'gclient',
  'json',
  'path',
  'platform',
  'properties',
  'python',
  'rietveld',
  'step',
  'step_history',
]


def html_results(*name_vals):
  ret = ''
  for name, val in name_vals:
    if val:
      ret += '<br/>%s:<br/>' % name
    for test in val:
      ret += test + '<br/>'
  return ret


def followup_fn(step_result):
  r = step_result.json.test_results
  p = step_result.presentation

  p.step_text += html_results(
    ('unexpected_flakes', r.unexpected_flakes),
    ('unexpected_failures', r.unexpected_failures),
  )

  p.step_text += '<br/>Total executed: %s' % r.num_passes

  if r.unexpected_flakes or r.unexpected_failures:
    p.status = 'WARNING'
  else:
    p.status = 'SUCCESS'


def summarize_failures(ignored, new):
  def summarize_failures_inner(step_result):
    p = step_result.presentation
    p.step_text += html_results(
      ('new', new),
      ('ignored', ignored),
    )
    if new:
      p.status = 'FAILURE'
    elif ignored:
      p.status = 'WARNING'
  return summarize_failures_inner


def GenSteps(api):
  api.chromium.set_config('blink')
  api.chromium.apply_config('trybot_flavor')
  api.chromium.apply_config('disable_aura')
  api.gclient.set_config('blink_internal',
                         GIT_MODE=api.properties.get('GIT_MODE', False))
  api.step.auto_resolve_conflicts = True

  webkit_lint = api.path.build('scripts', 'slave', 'chromium',
                               'lint_test_files_wrapper.py')
  archive_layout_test_results = api.path.build(
    'scripts', 'slave', 'chromium', 'archive_layout_test_results.py')
  webkit_python_tests = api.path.build('scripts', 'slave', 'chromium',
                                       'test_webkitpy_wrapper.py')
  results_dir = api.path.slave_build('layout-test-results')


  def BlinkTestsStep(with_patch):
    name = 'webkit_tests (with%s patch)' % ('' if with_patch else 'out')
    test = api.path.build('scripts', 'slave', 'chromium',
                          'layout_test_wrapper.py')
    args = ['--target', api.chromium.c.BUILD_CONFIG,
            '-o', results_dir,
            '--build-dir', api.chromium.c.build_dir,
            '--json-test-results', api.json.test_results()]
    return api.chromium.runtests(test, args, name=name, can_fail_build=False,
                                 followup_fn=followup_fn)

  yield (
    api.gclient.checkout(),
    api.rietveld.apply_issue('third_party', 'WebKit'),
    api.chromium.runhooks(),
    api.chromium.compile(),
    api.python('webkit_lint', webkit_lint, [
      '--build-dir', api.path.checkout('out'),
      '--target', api.properties['build_config']]),
    api.python('webkit_python_tests', webkit_python_tests, [
      '--build-dir', api.path.checkout('out'),
      '--target', api.properties['build_config']
    ]),
    api.chromium.runtests('webkit_unit_tests'),
    api.chromium.runtests('weborigin_unittests'),
    api.chromium.runtests('wtf_unittests'),
  )

  yield BlinkTestsStep(with_patch=True)
  with_patch = api.step_history.last_step().json.test_results

  buildername = api.properties['buildername']
  buildnumber = api.properties['buildnumber']
  def archive_webkit_tests_results_followup(step_result):
    base = (
        "https://storage.googleapis.com/chromium-layout-test-archives/%s/%s" %
        (buildername, buildnumber))

    step_result.presentation.links['layout_test_results'] = (
        base + '/layout-test-results/results.html')
    step_result.presentation.links['(zip)'] = (
        base + '/layout-test-results.zip')

  yield api.python(
    'archive_webkit_tests_results',
    archive_layout_test_results,
    [
      '--results-dir', results_dir,
      '--build-dir', api.chromium.c.build_dir,
      '--build-number', buildnumber,
      '--builder-name', buildername,
      '--gs-bucket', 'gs://chromium-layout-test-archives',
    ] + api.json.property_args(),
    followup_fn=archive_webkit_tests_results_followup
  )

  if not with_patch.unexpected_failures:
    yield api.python.inline('webkit_tests', 'print "ALL IS WELL"')
    return

  yield (
    api.gclient.revert(),
    api.chromium.runhooks(),
    api.chromium.compile(),
    BlinkTestsStep(with_patch=False),
  )
  clean = api.step_history.last_step().json.test_results

  ignored_failures = set(clean.unexpected_failures)
  new_failures = (set(with_patch.unexpected_failures) -
                  ignored_failures)

  ignored_failures = sorted(ignored_failures)
  new_failures = sorted(new_failures)

  yield api.python.inline(
    'webkit_tests',
    r"""
    import sys, json
    failures = json.load(open(sys.argv[1], 'rb'))

    if failures['new']:
      print 'New failures:'
      print '\n'.join(failures['new'])
    if failures['ignored']:
      print 'Ignored failures:'
      print '\n'.join(failures['ignored'])

    sys.exit(bool(failures['new']))
    """,
    args=[
      api.json.input({
        'new': new_failures,
        'ignored': ignored_failures
      })
    ],
    followup_fn=summarize_failures(ignored_failures, new_failures)
  )


def GenTests(api):
  canned_test = api.json.canned_test_output
  with_patch = 'webkit_tests (with patch)'
  without_patch = 'webkit_tests (without patch)'

  for result, good in [('success', True), ('fail', False)]:
    for build_config in ['Release', 'Debug']:
      for plat in ('win', 'mac', 'linux'):
        for git_mode in True, False:
          suffix = '_git' if git_mode else ''
          name = '%s_%s_%s%s' % (plat, result, build_config.lower(), suffix)
          test = (
            api.test(name) +
            api.properties.tryserver(
              build_config=build_config,
              config_name='blink',
              root='src/third_party/WebKit',
              GIT_MODE=git_mode,
            ) +
            api.platform.name(plat) +
            api.step_data(with_patch, canned_test(good))
          )
          if not good:
            test += api.step_data(without_patch, canned_test(False))
          yield test

  yield (
    api.test('warn_on_flakey') +
    api.properties.tryserver(
      build_config='Release',
      config_name='blink',
      root='src/third_party/WebKit',
      GIT_MODE=False,
    ) +
    api.step_data(with_patch, canned_test(False)) +
    api.step_data(without_patch, canned_test(True))
  )
