from contextlib import closing
import json
import os
from http.client import HTTPConnection
from pathlib import Path
import shutil
import ssl
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from printbench.server import make_server
from printbench.store import Store


@unittest.skipUnless(shutil.which('openssl'), 'openssl required for an isolated TLS fixture')
class ClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'library')
        self.key = self.root / 'tls.key'
        self.cert = self.root / 'tls.crt'
        subprocess.run(['openssl', 'req', '-new', '-x509', '-nodes', '-newkey', 'rsa:2048',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost', '-days', '1',
                        '-keyout', str(self.key), '-out', str(self.cert)], check=True, capture_output=True)
        self.key.chmod(0o600)
        self.token = self.root / 'token'
        self.token.write_text('a' * 64)
        self.token.chmod(0o600)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(self.cert, self.key)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'https://localhost:{server.server_port}'

    def client(self, url, *args, auth=True, trust=True):
        return subprocess.run([sys.executable, '-m', 'printbench.client', '--url', url,
                               *(['--token-file', str(self.token)] if auth else []),
                               *(['--ca-file', str(self.cert)] if trust else []), *args],
                              capture_output=True, text=True, timeout=6)

    def test_token_is_validated_before_startup(self):
        base = [sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'serve', '--port', '0']
        env = os.environ | {'PRINTBENCH_MANAGEMENT_TOKEN_FILE': str(self.token)}
        for value, mode in ((None, 0o600), ('invalid', 0o600), ('a' * 64, 0o644)):
            with self.subTest(present=value is not None, mode=mode):
                if value is None:
                    self.token.unlink()
                else:
                    self.token.write_text(value)
                    self.token.chmod(mode)
                try:
                    result = subprocess.run(base, env=env, capture_output=True, text=True, timeout=2)
                except subprocess.TimeoutExpired as exc:
                    self.fail(f'App served with invalid credentials: {bool(exc.stdout)} startup output')
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                self.assertIn('credential', json.loads(result.stderr)['error'].lower())
        self.assertFalse(self.store.root.exists())

    def test_actual_entry_point_opt_in_and_https_mutation(self):
        base = [sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'serve', '--port', '0']
        seen = []
        backend_port = None

        class Edge(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def forward(self):
                connection = HTTPConnection('127.0.0.1', backend_port, timeout=5)
                headers = {key: self.headers[key] for key in ('Authorization', 'Content-Length',
                           'Content-Type', 'If-Match', 'If-None-Match') if key in self.headers}
                seen.append((self.path, headers.get('Authorization') == 'Bearer ' + 'a' * 64,
                             'Authorization' in headers))
                try:
                    length = int(headers.get('Content-Length', '0'))
                    connection.request(self.command, self.path, self.rfile.read(length), headers)
                    response = connection.getresponse()
                    content = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ('date', 'server'):
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(content)
                finally:
                    connection.close()

            do_GET = do_HEAD = do_POST = do_PUT = forward

        with subprocess.Popen(base, env=os.environ | {'PRINTBENCH_MANAGEMENT_TOKEN_FILE': str(self.token)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as server:
            try:
                import select
                from urllib.parse import urlsplit
                assert server.stdout is not None
                self.assertTrue(select.select([server.stdout], [], [], 5)[0])
                startup_line = server.stdout.readline()
                startup = json.loads(startup_line)
                self.assertEqual(set(startup), {'url', 'data'})
                backend_port = urlsplit(startup['url']).port
                edge = HTTPServer(('127.0.0.1', 0), Edge)
                edge.socket = self.tls.wrap_socket(edge.socket, server_side=True)
                url = self.start(edge)
                listed = self.client(url, 'list')
                self.assertEqual((listed.returncode, json.loads(listed.stdout)), (0, []))
                artifacts = self.root / 'artifacts'
                artifacts.mkdir()
                (artifacts / 'sample.txt').write_bytes(b'synthetic')
                create = self.client(url, 'create', '--spec', 'examples/sample.json', '--source', str(artifacts))
                self.assertEqual(create.returncode, 0, create.stderr)
                self.assertEqual(json.loads(create.stdout)['revision'], 'r1')
                result = self.client(url, 'state', 'sample')
                self.assertEqual(result.returncode, 0, result.stderr)
                state = json.loads(result.stdout)
                self.assertEqual(state['current'], 'r1')
                spec = json.loads(Path('examples/sample.json').read_bytes())
                spec_file = self.root / 'append.json'
                spec_file.write_text(json.dumps(dict(spec, revision='r2')))
                append = self.client(url, 'append', '--spec', str(spec_file), '--source', str(artifacts),
                                     '--match', state['etag'])
                self.assertEqual(append.returncode, 0, append.stderr)
                self.assertEqual(json.loads(append.stdout)['revision'], 'r2')
                latest = json.loads(self.client(url, 'state', 'sample').stdout)
                archive = self.client(url, 'archive', 'sample', '--match', latest['etag'])
                self.assertEqual(archive.returncode, 0, archive.stderr)
                archived = json.loads(archive.stdout)
                self.assertTrue(archived['archived'])
                self.assertEqual(self.store.list(), [])
                restore = self.client(url, 'restore', 'sample', '--match', archived['etag'])
                self.assertEqual(restore.returncode, 0, restore.stderr)
                self.assertFalse(json.loads(restore.stdout)['archived'])
                self.assertEqual(self.store.history('sample'), {'current': 'r2', 'revisions': ['r1', 'r2']})
                self.assertIn(('/api/projects', False, False), seen)
                self.assertIn(('/manage/v1/projects/sample', True, True), seen)
                self.assertNotIn('a' * 64, startup_line + create.stdout + create.stderr)
                with closing(HTTPConnection('127.0.0.1', backend_port, timeout=3)) as direct:
                    direct.request('GET', '/')
                    view = direct.getresponse()
                    self.assertEqual(view.status, 200)
                    self.assertNotIn(b'a' * 64, view.read())
            finally:
                if server.poll() is None:
                    server.send_signal(signal.SIGINT)
                out, err = server.communicate(timeout=5)
                self.assertEqual((server.returncode, out, err), (0, '', ''))

    def test_verify_tls_no_redirect_and_credential_file_protection(self):
        class Redirect(BaseHTTPRequestHandler):
            hits = 0
            def do_GET(self):
                type(self).hits += 1
                self.send_response(301)
                self.send_header('Location', 'https://localhost:1/')
                self.end_headers()
            def log_message(self, format, *args):
                pass
        redirect = HTTPServer(('127.0.0.1', 0), Redirect)
        redirect.socket = self.tls.wrap_socket(redirect.socket, server_side=True)
        url = self.start(redirect)
        bounced = self.client(url, 'state', 'tile')
        self.assertIn('301', json.loads(bounced.stderr)['error'])
        self.assertEqual(Redirect.hits, 1)
        for bad in (url.replace('https:', 'http:'), url.replace('localhost', '127.0.0.1'), url + '/path'):
            with self.subTest(url=bad):
                self.assertNotEqual(self.client(bad, 'state', 'tile').returncode, 0)
        self.assertNotEqual(self.client(url, 'state', 'tile', trust=False).returncode, 0)
        self.token.chmod(0o644)
        unsafe = self.client(url, 'state', 'tile')
        self.assertIn('protected', json.loads(unsafe.stderr)['error'].lower())
        self.assertFalse(self.store.root.exists())

    def test_read_client_download_checks_hash_and_never_overwrites(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'sample.txt').write_bytes(b'synthetic')
        self.store.publish(json.loads(Path('examples/sample.json').read_bytes()), artifacts)
        listener = make_server(self.store, 0)
        listener.socket = self.tls.wrap_socket(listener.socket, server_side=True)
        url = self.start(listener)
        self.token.unlink()  # Ordinary reads do not try to load even an unnecessarily supplied token file.
        out = self.root / 'verified.txt'
        args = ('download', 'sample', '--revision', 'r1', 'sample.txt', '--output', str(out))
        served = self.client(url, *args)
        self.assertEqual(served.returncode, 0, served.stderr)
        self.assertEqual(out.read_bytes(), b'synthetic')
        self.assertNotEqual(self.client(url, *args).returncode, 0)
        self.assertEqual(out.read_bytes(), b'synthetic')
