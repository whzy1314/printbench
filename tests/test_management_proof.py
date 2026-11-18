from contextlib import contextmanager
from http.client import HTTPResponse
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from printbench.store import Store
from test_store import snapshot


PROJECT = '/manage/v1/projects/sample'
STATE = PROJECT + '/state'
PROOF_ENV = 'PRINTBENCH_MANAGEMENT_PROOF_FILE'
RECEIPT = 'X-PrintBench-Proof-Receipt'


class ProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.store = Store(self.base / 'library')
        self.command = [sys.executable, '-m', 'printbench', '--data', str(self.store.root),
                        'serve', '--port', '0']
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('PRINTBENCH_')}
        self.projection = self.base / 'projection'
        self.projection.mkdir()
        self.sentinel = self.projection / 'sentinel'
        self.sentinel.symlink_to('..data/sentinel')
        self.old = secrets.token_hex(32)  # Synthetic, never sent outside loopback.
        self.rotate(self.old, 1)

    def rotate(self, value, generation):
        version = self.projection / f'..2026_01_01_00_00_0{generation}'
        version.mkdir()
        fd = os.open(version / 'sentinel', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
        selector = self.projection / '..data.new'
        selector.symlink_to(version.name)
        selector.replace(self.projection / '..data')

    @contextmanager
    def serve(self, **config):
        with subprocess.Popen(self.command, env=self.env | config, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                assert process.stdout is not None
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'App did not start')
                port = urlsplit(json.loads(process.stdout.readline())['url']).port
                yield port
            finally:
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertFalse(stdout or stderr, 'Request handling produced unexpected logs')
                self.assertEqual(process.returncode, 0)

    def request(self, port, method, path, headers=(), *, host=None, body=b''):
        if host is None:
            host = f'localhost:{port}'
        lines = [f'{method} {path} HTTP/1.1', f'Host: {host}', *headers, '', '']
        with socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
            conn.sendall('\r\n'.join(lines).encode('ascii') + body)
            with HTTPResponse(conn, method=method) as response:
                response.begin()
                data = response.read()
                self.assertFalse(self.old.encode() in data or any(self.old in value for _, value in
                                 response.getheaders()), 'Sentinel leaked in response')
                return response.status, response.headers, data

    def test_only_exact_bodyless_old_authorization_gets_a_denial_receipt(self):
        old = 'Authorization: Bearer ' + self.old
        wrong = 'Authorization: Bearer ' + secrets.token_hex(32)
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            first, second = (self.request(port, 'GET', STATE, (old,)) for _ in range(2))
            for status, headers, body in (first, second):
                self.assertEqual(status, 401)
                self.assertEqual(headers.get('Cache-Control'), 'no-store')
                self.assertEqual(headers.get('WWW-Authenticate'), 'Bearer realm="PrintBench"')
                self.assertEqual(json.loads(body), {'error': 'Credential required'})
                self.assertNotIn('ETag', headers)
                self.assertRegex(headers.get(RECEIPT, ''), r'\A[0-9a-f]{32}\Z')
            self.assertNotEqual(first[1][RECEIPT], second[1][RECEIPT])

            for name, headers, status in (
                ('absent', (), 401), ('wrong', (wrong,), 401), ('duplicate', (old, old), 401),
                ('proxy metadata', ('X-Forwarded-Authorization: ' + old.split(': ', 1)[1],), 401),
                ('combined', (old + ', ' + wrong.split(': ', 1)[1],), 401),
                ('folded', ('X-Other: value', ' ' + wrong, old), 400),
                ('bad header', (old, 'Authorization : Bearer ignored'), 400),
                ('chunked', (old, 'Transfer-Encoding: chunked'), 400),
                ('encoded', (old, 'Content-Encoding: gzip'), 400),
                ('expect', (old, 'Expect: 100-continue'), 400),
                ('body withheld', (old, 'Content-Length: 10'), 400),
                ('duplicate length', (old, 'Content-Length: 0', 'Content-Length: 0'), 400),
                ('duplicate Host', (old, f'Host: localhost:{port}'), 403),
            ):
                with self.subTest(name=name):
                    actual, response, _ = self.request(port, 'GET', STATE, headers)
                    self.assertEqual(actual, status)
                    self.assertNotIn(RECEIPT, response)
            for path, host, expected in ((STATE, 'wrong.example', 403), (STATE + '?x=y', None, 404)):
                status, headers, _ = self.request(port, 'GET', path, (old, 'X-Forwarded-Host: localhost'),
                                                  host=host)
                self.assertEqual(status, expected)
                self.assertNotIn(RECEIPT, headers)
            status, headers, _ = self.request(port, 'GET', STATE, (old, 'Content-Length: 0'))
            self.assertEqual(status, 401)
            self.assertIn(RECEIPT, headers)

    def test_projection_rotation_stops_receipting_old_without_sending_replacement(self):
        old = 'Authorization: Bearer ' + self.old
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            self.assertIn(RECEIPT, self.request(port, 'GET', STATE, (old,))[1])
            self.rotate(secrets.token_hex(32), 2)  # Do not send the replacement.
            status, headers, _ = self.request(port, 'GET', STATE, (old,))
            self.assertEqual(status, 401)
            self.assertNotIn(RECEIPT, headers)
            self.sentinel.resolve().chmod(0o644)
            status, headers, _ = self.request(port, 'GET', STATE, (old,))
            self.assertEqual(status, 503)
            self.assertNotIn(RECEIPT, headers)

    def test_mutation_dispatch_rejects_before_body_and_leaves_reads_and_store_unchanged(self):
        source = self.base / 'source'
        source.mkdir()
        (source / 'sample.txt').write_bytes(b'Fixture only')
        self.store.publish(json.loads(Path('examples/sample.json').read_text()), source)
        before = snapshot(self.store.root)
        version, _ = self.store.state('sample')
        old = 'Authorization: Bearer ' + self.old
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            for method, path, headers in (
                ('PUT', PROJECT, ('Content-Length: 100', 'Content-Type: application/octet-stream')),
                ('POST', PROJECT + '/archive', ()), ('POST', PROJECT + '/restore', ()),
                ('DELETE', PROJECT, ()), ('PROPFIND', STATE, ()), ('HEAD', STATE, ()),
            ):
                for auth in ((), (old,), ('Authorization: Bearer ' + secrets.token_hex(32),)):
                    status, response, _ = self.request(port, method, path, auth + headers + (
                        f'If-Match: "{version}"', 'X-Forwarded-Proto: https'))
                    self.assertEqual(status, 403)
                    self.assertNotIn(RECEIPT, response)
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(port, method, '/api/projects/sample')[0], 200)
            self.assertEqual(self.request(port, 'PUT', '/api/projects/sample', (old,))[0], 405)
        self.assertEqual(snapshot(self.store.root), before)

    def test_default_has_no_receipt_and_bad_opt_in_refuses_startup(self):
        with self.serve() as port:
            status, headers, _ = self.request(port, 'GET', STATE, ('Authorization: Bearer ' + self.old,))
            self.assertEqual(status, 404)
            self.assertNotIn(RECEIPT, headers)
        for proof in ('', str(self.base / 'missing'), str(self.sentinel)):
            self.sentinel.resolve().chmod(0o644)
            result = subprocess.run(self.command, env=self.env | {PROOF_ENV: proof}, text=True,
                                    capture_output=True, timeout=3)
            self.assertEqual((result.returncode, result.stdout), (1, ''))
            self.assertIn('protected sentinel file', result.stderr)
        self.sentinel.resolve().chmod(0o600)
        result = subprocess.run(self.command + ['--management-token-file', str(self.sentinel)],
                                env=self.env | {PROOF_ENV: str(self.sentinel)}, text=True,
                                capture_output=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('mutually exclusive', result.stderr)
        self.assertFalse(self.old in result.stderr, 'Sentinel leaked in startup error')
        self.rotate('invalid', 2)
        result = subprocess.run(self.command, env=self.env | {PROOF_ENV: str(self.sentinel)}, text=True,
                                capture_output=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('protected sentinel file', result.stderr)
