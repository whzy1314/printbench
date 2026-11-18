import fcntl
import hashlib
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, HTTPServer
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
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from printbench import management, server
from printbench.store import Store
from test_management import frame
from test_store import snapshot

PROJECT = '/manage/v1/projects/sample'
STATE = PROJECT + '/state'
ARM = 'X-PrintBench-Shutdown-Arm: 1'
RECEIPT = 'X-PrintBench-Shutdown-Receipt'
OPT_IN = 'PRINTBENCH_MANAGEMENT_SHUTDOWN_PROBE'
READ = '/api/projects/sample'


class WriterShutdownTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.store = Store(self.base / 'library')
        self.source = self.base / 'source'
        self.source.mkdir()
        self.artifact = b'Synthetic initial artifact'
        (self.source / 'sample.txt').write_bytes(self.artifact)
        self.spec = json.loads(Path('examples/sample.json').read_bytes())
        self.store.publish(self.spec, self.source)
        self.spec_path = self.base / 'spec.json'
        self.spec_path.write_text(json.dumps(dict(self.spec, revision='cli-r2')))
        self.projection = self.base / 'projection'
        self.projection.mkdir()
        self.token = self.projection / 'token'
        self.token.symlink_to('..data/token')
        self.old, self.new = (secrets.token_hex(32) for _ in range(2))
        self.auth = 'Authorization: Bearer ' + self.old
        self.generation = 0
        self.rotate(self.old)

    def rotate(self, token):
        self.generation += 1
        version = self.projection / f'..2026_01_01_00_00_{self.generation:02}'
        version.mkdir()
        fd = os.open(version / 'token', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(token)
        selector = self.projection / '..data.new'
        selector.symlink_to(version.name)
        selector.replace(self.projection / '..data')

    def start(self, listener):
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener.server_port

    def writer(self, **options):
        return self.start(server.make_server(self.store, 0, management_token_file=self.token, **options))

    def send(self, conn, port, method, path, headers, body=b'', host=None):
        host = f'127.0.0.1:{port}' if host is None else host
        lines = [f'{method} {path} HTTP/1.1', f'Host: {host}', *headers, '', '']
        conn.sendall('\r\n'.join(lines).encode('ascii') + body)

    def response(self, conn, method='GET'):
        with HTTPResponse(conn, method=method) as response:
            response.begin()
            data = response.read()
            self.assertFalse(any(token.encode() in data or any(token in v for _, v in response.getheaders())
                                 for token in (self.old, self.new)), 'Synthetic bearer leaked in response')
            self.assertEqual(conn.recv(1), b'')  # The handler has released its permit.
            return response.status, response.headers, data

    def request(self, port, headers=(), *, path=STATE, method='GET', host=None):
        with socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
            self.send(conn, port, method, path, headers, host=host)
            return self.response(conn, method)

    def assertReceipt(self, response):
        status, headers, body = response
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {'error': 'Credential required'})
        self.assertEqual(headers.get_all('WWW-Authenticate'), ['Bearer realm="PrintBench"'])
        self.assertEqual(headers.get_all('Cache-Control'), ['no-store'])
        self.assertNotIn('ETag', headers)
        values = headers.get_all(RECEIPT, [])
        self.assertEqual(len(values), 1, 'Expected one app shutdown receipt')
        self.assertRegex(values[0], r'\A[0-9a-f]{32}\Z')
        return values[0]

    def assertNoReceipt(self, response, status):
        self.assertEqual(response[0], status)
        self.assertNotIn(RECEIPT, response[1])

    def test_exact_denial_and_stripped_at_loopback_edge(self):
        port = self.writer(shutdown_probe=True)
        case = self
        forwarded = []

        class StrippingEdge(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                forwarded.append(self.headers.get_all('Authorization') == [case.auth.split(': ', 1)[1]])
                status, headers, body = case.request(port, path=self.path, host=self.headers['Host'])
                self.send_response_only(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

        edge_port = self.start(HTTPServer(('127.0.0.1', 0), StrippingEdge))
        before = snapshot(self.store.root)
        self.assertNoReceipt(self.request(port, (self.auth, ARM, 'Content-Length: 0')), 200)
        self.assertNoReceipt(self.request(port, (self.auth,)), 200)  # A still authorizes.
        self.rotate(self.new)  # Never send B.
        direct = self.request(port, (self.auth,))
        first = self.assertReceipt(direct)
        self.assertNotEqual(first, self.assertReceipt(self.request(port, (self.auth,))))
        stripped = self.request(edge_port, (self.auth,), host=f'127.0.0.1:{port}')
        self.assertEqual(forwarded, [True])
        self.assertNoReceipt(stripped, 401)
        self.assertEqual(stripped[2], direct[2])
        wrong = 'Authorization: Bearer ' + secrets.token_hex(32)
        for label, headers in (
            ('absent', ()), ('wrong', (wrong,)), ('duplicate', (self.auth, self.auth)),
            ('forwarded only', ('X-Forwarded-' + self.auth,)), ('combined', (self.auth + ', ' + wrong,)),
            ('chunked', (self.auth, 'Transfer-Encoding: chunked')),
            ('encoded', (self.auth, 'Content-Encoding: gzip')), ('expect', (self.auth, 'Expect: 100-continue')),
            ('body', (self.auth, 'Content-Length: 1')), ('duplicate length', (self.auth, 'Content-Length: 0', 'Content-Length: 0')),
            ('folding', (self.auth, 'X-Folded: a', ' b')), ('bad header', (self.auth, 'Authorization : ignored')),
        ):
            with self.subTest(label=label):
                self.assertNoReceipt(self.request(port, headers), 401)
        for host, path, method, status in (
            (f'localhost:{port}', STATE, 'GET', 401), ('wrong.example', STATE, 'GET', 403),
            (None, STATE + '?x=1', 'GET', 401), (None, '/' + STATE, 'GET', 401),
            (None, '/manage/v1/projects/other/state', 'GET', 401), (None, STATE, 'HEAD', 401),
            (None, PROJECT + '/archive', 'POST', 401),
        ):
            self.assertNoReceipt(self.request(port, (self.auth,), path=path, method=method, host=host), status)
        self.assertNoReceipt(self.request(port, (self.auth, f'Host: 127.0.0.1:{port}')), 403)
        self.assertEqual(snapshot(self.store.root), before)

    def test_no_arm_from_default_failed_or_ambiguous_state_reads(self):
        default = self.writer()
        enabled = self.writer(shutdown_probe=True)
        self.assertNoReceipt(self.request(default, (self.auth, ARM)), 200)
        self.assertEqual(self.request(enabled, (self.auth,))[0], 200)
        self.assertEqual(self.request(enabled, (self.auth, ARM), path='/manage/v1/projects/missing/state')[0], 404)
        for headers, code in (((self.auth, ARM, 'Content-Length: 1'), 400),
                              ((self.auth, ARM, ARM), 200), ((self.auth, ARM, 'X-Folded: a', ' b'), 200),
                              ((self.auth, self.auth, ARM), 401), ((ARM,), 401)):
            self.assertNoReceipt(self.request(enabled, headers), code)
        self.rotate(self.new)
        self.assertNoReceipt(self.request(default, (self.auth,)), 401)
        self.assertNoReceipt(self.request(enabled, (self.auth, ARM)), 401)

    def test_bad_projection_generation_and_expiry_suppress_receipt(self):
        clock = SimpleNamespace(monotonic=lambda: 1)
        port = self.writer(shutdown_probe=True)
        with patch.object(management, 'time', clock):
            self.assertNoReceipt(self.request(port, (self.auth, ARM)), 200)
            self.rotate('malformed')
            self.assertNoReceipt(self.request(port, (self.auth,)), 503)
            self.rotate(self.new)
            self.token.resolve().chmod(0o644)
            self.assertNoReceipt(self.request(port, (self.auth,)), 503)
            self.token.resolve().chmod(0o600)
            self.assertReceipt(self.request(port, (self.auth,)))

            read_token = management.read_token

            def swap_after_first_read(*args, **kwargs):
                token = read_token(*args, **kwargs)
                if token == self.new:
                    self.rotate(self.old)
                return token

            with patch.object(management, 'read_token', side_effect=swap_after_first_read):
                self.assertNoReceipt(self.request(port, (self.auth,)), 401)
            self.assertNoReceipt(self.request(port, (self.auth,)), 200)
            self.rotate(self.new)
            clock.monotonic = lambda: 601
            self.assertNoReceipt(self.request(port, (self.auth,)), 401)
            self.rotate(self.old)
            self.assertNoReceipt(self.request(port, (self.auth, ARM)), 200)
            self.rotate(self.new)
            self.assertNoReceipt(self.request(port, (self.auth,)), 401)

    def test_opt_in_entry_point_cannot_receipt_across_process_restart(self):
        cmd = [sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'serve', '--port', '0']
        env = {k: v for k, v in os.environ.items() if not k.startswith('PRINTBENCH_')} | {OPT_IN: '1'}
        for config in ({}, {'PRINTBENCH_MANAGEMENT_PROOF_FILE': str(self.token)}, {OPT_IN: 'yes'}):
            result = subprocess.run(cmd, env=env | config, capture_output=True, timeout=3)
            self.assertEqual((result.returncode, result.stdout), (1, b''))
            self.assertIn(b'shutdown probe', result.stderr)
        env['PRINTBENCH_MANAGEMENT_TOKEN_FILE'] = str(self.token)
        for first in (True, False):
            with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as writer:
                try:
                    assert writer.stdout is not None
                    self.assertTrue(select.select([writer.stdout], [], [], 3)[0])
                    port = urlsplit(json.loads(writer.stdout.readline())['url']).port
                    if first:
                        self.assertNoReceipt(self.request(port, (self.auth, ARM)), 200)
                        self.rotate(self.new)
                        self.assertReceipt(self.request(port, (self.auth,)))
                    else:
                        self.assertNoReceipt(self.request(port, (self.auth,)), 401)
                finally:
                    writer.send_signal(signal.SIGINT)
                    out, err = writer.communicate(timeout=3)
                    self.assertEqual((writer.returncode, out, err), (0, b'', b''))

    def test_withheld_upload_must_settle_without_replay(self):
        port = self.writer(shutdown_probe=True)
        arm = self.request(port, (self.auth, ARM))
        self.assertEqual(arm[0], 200)
        before = snapshot(self.store.root)
        payload = b'Withheld synthetic payload'
        body = frame(dict(self.spec, revision='not-admitted'), payload)
        entered = threading.Event()
        validate = management.validate_spec

        def observed_metadata(spec):
            validate(spec)
            entered.set()

        with patch.object(management, 'validate_spec', side_effect=observed_metadata), \
                socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
            self.send(conn, port, 'PUT', PROJECT, (self.auth, f'If-Match: {arm[1]["ETag"]}',
                      'Content-Type: application/octet-stream', f'Content-Length: {len(body)}'), body[:-len(payload)])
            self.assertTrue(entered.wait(3), 'Upload was not admitted through real authentication')
            self.rotate(self.new)
            self.assertNoReceipt(self.request(port, (self.auth,)), 401)
            self.assertEqual(snapshot(self.store.root), before)
            conn.sendall(payload)
            rejected = self.response(conn, 'PUT')
            self.assertNoReceipt(rejected, 401)
            self.assertEqual(json.loads(rejected[2]), {'error': 'Credential changed during upload'})
        self.assertReceipt(self.request(port, (self.auth,)))
        self.assertEqual(snapshot(self.store.root), before)

    def test_admitted_write_at_exact_flock_must_commit_before_receipt(self):
        cli = subprocess.run([sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'publish',
                              '--spec', str(self.spec_path), '--source', str(self.source)], capture_output=True, timeout=3)
        self.assertEqual((cli.returncode, cli.stderr, json.loads(cli.stdout)['revision']), (0, b'', 'cli-r2'))
        port = self.writer(shutdown_probe=True)
        arm = self.request(port, (self.auth, ARM))
        self.assertEqual(arm[0], 200)
        before = snapshot(self.store.root)
        payload = b'Admitted next revision'
        body = frame(dict(self.spec, revision='http-r3'), payload)
        contention = threading.Event()
        flock = fcntl.flock
        with socket.create_connection(('127.0.0.1', port), timeout=3) as conn:
            with self.store._writer() as lock_fd:
                inode = os.fstat(lock_fd)

                def observe_flock(fd, operation):
                    opened = os.fstat(fd)
                    self.assertEqual((opened.st_dev, opened.st_ino, operation),
                                     (inode.st_dev, inode.st_ino, fcntl.LOCK_EX))
                    with self.assertRaises(BlockingIOError):
                        flock(fd, operation | fcntl.LOCK_NB)
                    contention.set()
                    return flock(fd, operation)

                with patch.object(fcntl, 'flock', side_effect=observe_flock):
                    self.send(conn, port, 'PUT', PROJECT, (self.auth, f'If-Match: {arm[1]["ETag"]}',
                              'Content-Type: application/octet-stream', f'Content-Length: {len(body)}'), body)
                    self.assertTrue(contention.wait(3), 'Authenticated upload never reached the real writer lock')
                    self.rotate(self.new)
                    self.assertNoReceipt(self.request(port, (self.auth,)), 401)
                    self.assertEqual(snapshot(self.store.root), before)
            committed = self.response(conn, 'PUT')
        self.assertNoReceipt(committed, 201)
        self.assertEqual(json.loads(committed[2])['revision'], 'http-r3')
        self.assertReceipt(self.request(port, (self.auth,)))
        self.assertEqual(self.store.state('sample')[1], {'current': 'http-r3', 'archived': False})
        self.assertEqual(self.store.history('sample')['revisions'], ['r1', 'cli-r2', 'http-r3'])
        after = snapshot(self.store.root)
        for path, entry in before.items():
            if path.startswith('sample/revisions/'):
                self.assertEqual(after[path], entry)
        for revision, data in (('r1', self.artifact), ('cli-r2', self.artifact), ('http-r3', payload)):
            status, headers, actual = self.request(port, path=READ + f'/revisions/{revision}/files/sample.txt')
            self.assertEqual((status, headers['X-Content-SHA256'], actual), (200, hashlib.sha256(data).hexdigest(), data))
        self.assertEqual(snapshot(self.store.root), after)
