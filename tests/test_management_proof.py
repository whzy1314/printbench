from base64 import b64encode
from contextlib import contextmanager, redirect_stderr
from http.client import HTTPResponse
from http.server import HTTPServer
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from printbench import management
from printbench.server import Handler, make_server
from printbench.store import Store
from test_store import snapshot


PROJECT = '/manage/v1/projects/sample'
STATE = PROJECT + '/state'
PROOF_ENV = 'PRINTBENCH_MANAGEMENT_PROOF_FILE'
RECEIPT = 'X-PrintBench-Proof-Receipt'
RETIRED = 'X-PrintBench-Retired-Proof-Receipt'


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
        with subprocess.Popen(self.command, env=self.env | config,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            port = None
            try:
                assert process.stdout is not None
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'App did not start')
                try:
                    url = json.loads(process.stdout.readline().decode('utf-8'))['url']
                    if isinstance(url, str):
                        port = urlsplit(url).port
                except (ValueError, TypeError, KeyError, RecursionError):
                    pass
                if port is None:
                    self.fail('Invalid app startup output')
                yield port
            finally:
                try:
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
                    # Report cleanup failure only if no earlier failure is propagating.
                    stdout = stderr = None
            self.assertTrue(stdout is not None, 'App did not shut down cleanly')
            self.assertTrue(not stdout and not stderr, 'Request handling produced unexpected logs')
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

    def test_malformed_startup_output_has_a_fixed_diagnostic(self):
        encoded = b64encode(self.old.encode()).decode('ascii')
        for case, output in (
            ('invalid JSON', encoded),
            ('invalid raw port', json.dumps({'url': 'http://localhost:' + self.old})),
            ('invalid encoded port', json.dumps({'url': 'http://localhost:' + encoded})),
            ('non-string URL', json.dumps({'url': [self.old]})),
            ('missing port', json.dumps({'url': 'http://localhost/'})),
        ):
            with self.subTest(case=case), patch.object(subprocess, 'Popen') as popen, \
                    patch.object(select, 'select', return_value=([True], [], [])):
                process = popen.return_value.__enter__.return_value
                process.stdout = BytesIO((output + '\n').encode('utf-8'))
                process.communicate.return_value = (b'', b'')
                process.returncode = 0
                try:
                    with self.serve():
                        pass
                except Exception as exc:
                    diagnostic = ''.join(traceback.format_exception(exc))
                    fixed = type(exc) is AssertionError and str(exc) == 'Invalid app startup output'
                else:
                    self.fail('Malformed startup output was accepted')
                self.assertFalse(self.old in diagnostic or encoded in diagnostic,
                                 'Synthetic startup operand leaked in the traceback')
                self.assertTrue(fixed, 'Expected a fixed startup diagnostic')

    def test_invalid_startup_and_non_utf8_pipe_keep_the_fixed_diagnostic(self):
        encoded = b64encode(self.old.encode()).decode('ascii')
        self.command = [sys.executable, '-c',
                        'import os, signal, sys\n'
                        'raw, encoded = sys.stdin.buffer.read().splitlines()\n'
                        'signal.signal(signal.SIGINT, lambda *_: sys.exit(23))\n'
                        'os.write(2, b"\\xff" + raw + b" " + encoded + b"\\n")\n'
                        'os.write(1, b\'{"url":"http://localhost:invalid-\' + raw + b\'"}\\n\')\n'
                        'signal.pause()']
        read_fd, write_fd = os.pipe()
        popen = subprocess.Popen
        processes = []

        def start(*args, **kwargs):
            process = popen(*args, stdin=payload, **kwargs)
            processes.append(process)
            return process

        with os.fdopen(read_fd, 'rb') as payload:
            with os.fdopen(write_fd, 'wb') as writer:
                writer.write(f'{self.old}\n{encoded}\n'.encode('ascii'))
            with patch.object(subprocess, 'Popen', side_effect=start):
                try:
                    with self.serve():
                        pass
                except Exception as exc:
                    diagnostic = ''.join(traceback.format_exception(exc))
                    fixed = type(exc) is AssertionError and str(exc) == 'Invalid app startup output'
                else:
                    self.fail('Malformed startup output was accepted')
        process, = processes
        self.assertEqual(process.returncode, 23, 'Synthetic child was not reaped after SIGINT')
        self.assertTrue(process.stdout.closed and process.stderr.closed, 'Synthetic pipes were not closed')
        self.assertFalse(self.old in diagnostic or encoded in diagnostic,
                         'Synthetic startup operand leaked in the traceback')
        self.assertTrue(fixed, 'Expected a fixed startup diagnostic despite invalid pipe bytes')

    def test_proof_rejects_invalid_header_blocks_before_credential_lookup(self):
        with patch.object(management, 'read_token', return_value=self.old) as read_token:
            handler = management.management_handler(Handler, self.store, self.sentinel, proof=True)
            for name, lines in (
                ('invalid name', b'X(Test): example\r\n\r\n'),
                ('empty name', b': example\r\n\r\n'),
                ('embedded carriage return', b'X-Test: one\rX-Test: two\r\n\r\n'),
                ('EOF before blank line', b'X-Test: example\r\n'),
                ('partial blank line', b'X-Test: example\r\n\r'),
                ('bare LF', b'X-Test: example\n\n'),
            ):
                with self.subTest(name=name), redirect_stderr(StringIO()) as logs:
                    request = handler.__new__(handler)
                    request.raw_requestline = f'GET {STATE} HTTP/1.1\r\n'.encode('ascii')
                    request.server = Mock(spec=HTTPServer, server_port=1234)
                    request.connection = SimpleNamespace(settimeout=lambda _: None)
                    request.rfile = BytesIO(b'Host: localhost:1234\r\n' + lines)
                    request.wfile = BytesIO()
                    read_token.reset_mock()
                    self.assertFalse(request.parse_request())
                    response = request.wfile.getvalue()
                    self.assertTrue(response.startswith(b'HTTP/1.0 400 '), 'Invalid headers were not rejected')
                    self.assertFalse(read_token.called, 'Invalid headers reached credential lookup')
                    self.assertTrue(RECEIPT.encode() not in response and RETIRED.encode() not in response
                                    and not logs.getvalue(), 'Invalid headers produced proof evidence')

    def test_only_exact_bodyless_old_authorization_gets_a_denial_receipt(self):
        self.rotate(self.old, 1)
        old = 'Authorization: Bearer ' + self.old
        wrong = 'Authorization: Bearer ' + secrets.token_hex(32)
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            first, second = (self.request(port, 'GET', STATE, (old,)) for _ in range(2))
            for status, headers, body in (first, second):
                self.assertEqual(status, 401)
                self.assertTrue(headers.get('Cache-Control') == 'no-store', 'Expected no-store')
                self.assertTrue(headers.get('WWW-Authenticate') == 'Bearer realm="PrintBench"', 'Challenge changed')
                self.assertTrue(body == b'{"error": "Credential required"}', 'Denial body changed')
                self.assertTrue('ETag' not in headers, 'Denial included an ETag')
                self.assertTrue(bool(re.fullmatch('[0-9a-f]{32}', headers.get(RECEIPT, ''))), 'Invalid proof receipt')
            self.assertTrue(first[1][RECEIPT] != second[1][RECEIPT], 'Proof receipt was not fresh')

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
                    self.assertTrue(RECEIPT not in response, 'Unexpected proof receipt')
            for path, host, expected in ((STATE, 'wrong.example', 403), (STATE + '?x=y', None, 404)):
                status, headers, _ = self.request(port, 'GET', path, (old, 'X-Forwarded-Host: localhost'),
                                                  host=host)
                self.assertEqual(status, expected)
                self.assertTrue(RECEIPT not in headers, 'Unexpected proof receipt')
            status, headers, _ = self.request(port, 'GET', STATE, (old, 'Content-Length: 0', 'X-Test!: value'))
            self.assertEqual(status, 401)
            self.assertTrue(RECEIPT in headers, 'Missing proof receipt')

    def test_retired_receipt_requires_exact_final_header_and_fresh_process_event(self):
        old = 'Authorization: Bearer ' + self.old
        replacement = secrets.token_hex(32)
        substitute = secrets.token_hex(32)
        wrong = 'Authorization: Bearer ' + substitute
        private = (self.old, replacement, substitute)
        private += tuple(b64encode(value.encode()).decode('ascii') for value in private)
        payloads = {}
        logs = StringIO()
        clock = SimpleNamespace(monotonic=lambda: 0)

        def rotate(value, generation):
            self.rotate('', generation)
            payloads[self.sentinel.stat().st_ino] = value.encode()

        @contextmanager
        def protected(path, *, projection):
            with opener(path, projection=projection) as selector:
                yield BytesIO(payloads[os.fstat(selector.fileno()).st_ino])

        def probe(final=None, *, method='GET', path=STATE, host=None):
            start = logs.tell()
            # Apply any change only on this final hop, after forming exact A.
            response = self.request(server.server_port, method, path, (old,) if final is None else final, host=host)
            fresh = logs.getvalue()[start:]
            self.assertFalse(any(value in fresh or value.encode() in response[2]
                                 or any(value in field for _, field in response[1].items()) for value in private),
                             'Synthetic value leaked in proof output')
            return response, fresh

        def reject_malformed_probes(stage):
            for case, final, options in (
                ('invalid field value', (old, 'X-Test: invalid\x00value'), {}),
                ('invalid request line', None, {'method': 'GET\t'}),
            ):
                with self.subTest(stage=stage, case=case):
                    (status, headers, _), fresh = probe(final, **options)
                    self.assertTrue(RECEIPT not in headers and RETIRED not in headers and not fresh,
                                    'Malformed probe produced proof evidence')
                    self.assertEqual(status, 400)

        def accepted(response, fresh, seen=()):
            status, headers, body = response
            receipts = headers.get_all(RETIRED, [])
            return (status == 401 and headers.get_all('Cache-Control') == ['no-store']
                    and headers.get_all('WWW-Authenticate') == ['Bearer realm="PrintBench"']
                    and body == b'{"error": "Credential required"}' and 'ETag' not in headers
                    and RECEIPT not in headers and len(receipts) == 1
                    and bool(re.fullmatch('[0-9a-f]{32}', receipts[0])) and receipts[0] not in seen
                    and len(fresh.splitlines()) == 1
                    and json.loads(fresh) == {'receipt': receipts[0], 'verdict': 'retired'})

        rotate(self.old, 1)
        opener = management.protected_file
        with patch.object(management, 'protected_file', protected), patch.object(management, 'time', clock), \
                redirect_stderr(logs), make_server(self.store, 0, management_proof_file=self.sentinel) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                current, fresh = probe()
                self.assertTrue(current[0] == 401 and RECEIPT in current[1] and RETIRED not in current[1] and not fresh,
                                'Pre-rotation match changed')
                reject_malformed_probes('current')
                rotate(replacement, 2)  # B is read back through the opener but never sent.
                self.assertTrue(management.read_token(self.sentinel, projection=True) == replacement)
                before = snapshot(self.base)
                clock.monotonic = lambda: 299
                first, first_event = probe()
                self.assertTrue(accepted(first, first_event), 'Exact old A lacked a correlated retired denial')
                seen = (first[1][RETIRED],)
                second, second_event = probe()
                self.assertTrue(accepted(second, second_event, seen), 'Retired receipt was not fresh')
                self.assertFalse(accepted(second, ''), 'Accepted a receipt without a fresh process event')
                self.assertFalse(accepted(second, first_event), 'Accepted a mismatched process event')
                self.assertFalse(accepted(first, first_event, seen), 'Accepted a replay of both observations')
                reject_malformed_probes('retired')

                for name, final, options, status in (
                    ('strip', (), {}, 401), ('substitute', (wrong,), {}, 401),
                    ('duplicate', (old, old), {}, 401), ('wrong Host', None, {'host': 'wrong.example'}, 403),
                    ('wrong path', None, {'path': STATE + '?probe=1'}, 404),
                    ('framed body', (old, 'Content-Length: 1'), {}, 400),
                    ('not a state probe', None, {'method': 'PUT', 'path': PROJECT}, 403),
                ):
                    with self.subTest(name=name):
                        response, fresh = probe(final, **options)
                        self.assertEqual(response[0], status)
                        self.assertTrue(RECEIPT not in response[1] and RETIRED not in response[1] and not fresh)
                        self.assertFalse(accepted(first, fresh), 'Accepted a cached response after a rejected probe')
                self.assertTrue(snapshot(self.base) == before, 'Proof changed the library or selector files')

                self.sentinel.resolve().chmod(0o644)
                response, fresh = probe()
                self.assertTrue(response[0] == 503 and RETIRED not in response[1] and not fresh)
                self.sentinel.resolve().chmod(0o600)
                inode = self.sentinel.stat().st_ino
                payloads[inode] = b'invalid'
                response, fresh = probe()
                self.assertTrue(response[0] == 503 and RETIRED not in response[1] and not fresh)
                payloads[inode] = replacement.encode()
                clock.monotonic = lambda: 300
                response, fresh = probe()
                self.assertTrue(response[0] == 401 and RETIRED not in response[1] and not fresh)
                self.assertFalse(accepted(first, fresh), 'Accepted a stale receipt after expiry')
            finally:
                server.shutdown()
                thread.join()

    def test_fresh_process_started_after_rotation_does_not_receipt_old(self):
        self.rotate(self.old, 1)
        old = 'Authorization: Bearer ' + self.old
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            self.assertTrue(RECEIPT in self.request(port, 'GET', STATE, (old,))[1], 'Missing proof receipt')
        self.rotate(secrets.token_hex(32), 2)  # Do not send the replacement.
        with self.serve(**{PROOF_ENV: str(self.sentinel)}) as port:
            status, headers, _ = self.request(port, 'GET', STATE, (old,))
            self.assertEqual(status, 401)
            self.assertTrue(RECEIPT not in headers and RETIRED not in headers, 'Unexpected proof receipt')

    def test_writer_does_not_receipt_its_rotated_token(self):
        self.rotate(self.old, 1)
        old = ('Authorization: Bearer ' + self.old,)
        with self.serve(PRINTBENCH_MANAGEMENT_TOKEN_FILE=str(self.sentinel)) as port:
            status, headers, _ = self.request(port, 'GET', STATE, old)
            self.assertTrue(status == 404 and RECEIPT not in headers and RETIRED not in headers)
            self.rotate(secrets.token_hex(32), 2)
            status, headers, _ = self.request(port, 'GET', STATE, old)
            self.assertTrue(status == 401 and RECEIPT not in headers and RETIRED not in headers)

    def test_mutation_dispatch_rejects_before_body_and_leaves_reads_and_store_unchanged(self):
        self.rotate(self.old, 1)
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
                    self.assertTrue(RECEIPT not in response, 'Unexpected proof receipt')
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(port, method, '/api/projects/sample')[0], 200)
            self.assertEqual(self.request(port, 'PUT', '/api/projects/sample', (old,))[0], 405)
        self.assertTrue(snapshot(self.store.root) == before, 'Proof changed the library files')

    def test_default_has_no_receipt_and_bad_opt_in_refuses_startup(self):
        self.rotate(self.old, 1)
        with self.serve() as port:
            status, headers, _ = self.request(port, 'GET', STATE, ('Authorization: Bearer ' + self.old,))
            self.assertEqual(status, 404)
            self.assertTrue(RECEIPT not in headers and RETIRED not in headers, 'Unexpected proof receipt')
        for proof in ('', str(self.base / 'missing'), str(self.sentinel)):
            self.sentinel.resolve().chmod(0o644)
            result = subprocess.run(self.command, env=self.env | {PROOF_ENV: proof}, text=True,
                                    capture_output=True, timeout=3)
            self.assertTrue(result.returncode == 1 and not result.stdout
                            and 'protected sentinel file' in result.stderr and self.old not in result.stderr,
                            'Unsafe proof startup did not fail cleanly')
        self.sentinel.resolve().chmod(0o600)
        result = subprocess.run(self.command + ['--management-token-file', str(self.sentinel)],
                                env=self.env | {PROOF_ENV: str(self.sentinel)}, text=True,
                                capture_output=True, timeout=3)
        self.assertTrue(result.returncode == 1 and not result.stdout and 'mutually exclusive' in result.stderr
                        and self.old not in result.stderr, 'Mixed proof/writer startup did not fail cleanly')
        self.rotate('invalid', 2)
        result = subprocess.run(self.command, env=self.env | {PROOF_ENV: str(self.sentinel)}, text=True,
                                capture_output=True, timeout=3)
        self.assertTrue(result.returncode == 1 and not result.stdout and 'protected sentinel file' in result.stderr
                        and self.old not in result.stderr, 'Malformed proof startup did not fail cleanly')
