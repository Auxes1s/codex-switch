#!/usr/bin/env python3
"""Exercise native launch output, auth diagnostics, and failure isolation."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ['DIAGNOSTIC_TEST_MODE']
if 'app-server' not in sys.argv:
    print('native stdout')
    print('native stderr', file=sys.stderr)
    sys.exit(0 if mode == 'success' else 7 if mode == 'other-exit' else 1)
if mode == 'success' or mode == 'other-exit':
    raise RuntimeError('diagnostics must not run')
if mode == 'timeout':
    time.sleep(60)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    request_id = request['id']
    if request_id == 1:
        result = {'id': 1, 'result': {}}
    elif request_id == 2:
        if mode == 'unrelated':
            result = {'id': 2, 'error': {'message': 'network unavailable'}}
        elif mode == 'healthy':
            result = {'id': 2, 'result': {'account': {'type': 'chatgpt', 'email': 'PRIVATE'}}}
        else:
            result = {'id': 2, 'error': {'code': -32603, 'message': 'workspace routing discovery unauthorized (401)'}}
    else:
        if mode == 'recovered':
            result = {'id': 3, 'result': {'account': {'type': 'chatgpt', 'access_token': 'SECRET'}}}
        else:
            print('unrelated log SECRET', file=sys.stderr, flush=True)
            if mode == 'revoked':
                print('ERROR Failed to refresh token status=401 Unauthorized detail=TokenErrorDetail { error_code: Some("refresh_token_invalidated"), error_message: Some("Your session has ended. Please log in again."), .. }', file=sys.stderr, flush=True)
            result = {'id': 3, 'result': {'account': None, 'requiresOpenaiAuth': True}}
    print(json.dumps(result), flush=True)
'''


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / '.codex').mkdir()
        (self.home / '.codex-gig').mkdir()
        (self.home / '.codex' / '.codex-switch-active').write_text('gig\n')
        self.stub = self.home / 'codex-stub'
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_SWITCH_CODEX_BIN=str(self.stub))

    def run_launcher(self, mode):
        self.env['DIAGNOSTIC_TEST_MODE'] = mode
        return subprocess.run([str(ROOT / 'bin/codex-switch'), 'run', '--', '--yolo'],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_revoked_error_keeps_native_output_and_exit(self):
        result = self.run_launcher('revoked')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, 'native stdout\n')
        self.assertTrue(result.stderr.startswith('native stderr\n'))
        self.assertIn("profile 'gig'", result.stderr)
        self.assertIn('Your session has ended. Please log in again. (refresh_token_invalidated)', result.stderr)
        self.assertIn('codex-switch login gig', result.stderr)
        self.assertNotIn('SECRET', result.stderr)

    def test_401_without_native_detail_does_not_claim_revocation(self):
        result = self.run_launcher('denied')
        self.assertIn('codex-switch login gig', result.stderr)
        self.assertNotIn('session has ended', result.stderr)
        self.assertNotIn('SECRET', result.stderr)

    def test_healthy_or_recovered_auth_does_not_suggest_login(self):
        for mode in ['healthy', 'recovered', 'unrelated']:
            with self.subTest(mode=mode):
                result = self.run_launcher(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, 'native stderr\n')

    def test_success_and_other_exit_codes_skip_diagnostics(self):
        for mode, code in [('success', 0), ('other-exit', 7)]:
            with self.subTest(mode=mode):
                result = self.run_launcher(mode)
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, 'native stdout\n')
                self.assertEqual(result.stderr, 'native stderr\n')

    def test_diagnostic_timeout_is_bounded_and_silent(self):
        spec = importlib.util.spec_from_file_location('diagnostic', ROOT / 'bin/codex-switch-diagnose.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get('DIAGNOSTIC_TEST_MODE')
        os.environ['DIAGNOSTIC_TEST_MODE'] = 'timeout'
        try:
            stderr = io.StringIO()
            start = time.monotonic()
            with contextlib.redirect_stderr(stderr):
                module.diagnose(str(self.stub), 'gig', timeout=0.1)
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(stderr.getvalue(), '')
        finally:
            if previous is None:
                os.environ.pop('DIAGNOSTIC_TEST_MODE', None)
            else:
                os.environ['DIAGNOSTIC_TEST_MODE'] = previous


if __name__ == '__main__':
    unittest.main()
