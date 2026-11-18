import json
from http.client import HTTPSConnection, RemoteDisconnected
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

from printbench.management import make_management_server
from printbench.server import make_server
from printbench.store import BenchError, Store


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

    def test_actual_entry_point_opt_in_and_https_mutation(self):
        base = [sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'serve', '--port', '0']
        incomplete = subprocess.run(base + ['--management-port', '0'], capture_output=True, text=True, timeout=5)
        self.assertEqual(incomplete.returncode, 1)
        self.assertIn('credential', json.loads(incomplete.stderr)['error'].lower())
        with subprocess.Popen(base + ['--management-port', '0', '--management-token-file', str(self.token),
                                      '--management-cert-file', str(self.cert), '--management-key-file', str(self.key)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as server:
            try:
                import select
                assert server.stdout is not None
                self.assertTrue(select.select([server.stdout], [], [], 5)[0])
                startup = json.loads(server.stdout.readline())
                url = startup['management_url']
                self.assertTrue(url.startswith('https://localhost:'))
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
            finally:
                server.terminate()
                server.communicate(timeout=5)

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
        live = self.start(make_management_server(self.store, 0, self.token, tls=self.tls))
        for bad in (live.replace('https:', 'http:'), live.replace('localhost', '127.0.0.1'), live + '/path'):
            with self.subTest(url=bad):
                self.assertNotEqual(self.client(bad, 'state', 'tile').returncode, 0)
        self.assertNotEqual(self.client(live, 'state', 'tile', trust=False).returncode, 0)
        self.token.chmod(0o644)
        unsafe = self.client(live, 'state', 'tile')
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

    def test_network_backend_requires_client_authenticated_tls_before_binding(self):
        with self.assertRaisesRegex(BenchError, 'proxy client'):
            with make_management_server(self.store, 0, self.token, host='bench.example', tls=self.tls):
                self.fail('Network management must not bind without proxy authentication')

    def test_proxy_tls_identity_and_application_bearer_are_both_required(self):
        self.tls.load_verify_locations(cafile=self.cert)
        self.tls.verify_mode = ssl.CERT_REQUIRED
        with patch('printbench.management.address', return_value=('127.0.0.1', 0)):
            listener = make_management_server(self.store, 0, self.token, host='bench.example', tls=self.tls)
        self.start(listener)
        tls = ssl.create_default_context(cafile=self.cert)

        def probe(context, headers):
            conn = HTTPSConnection('localhost', listener.server_port, timeout=3, context=context)
            try:
                conn.request('GET', '/manage/v1/projects/tile/state', headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        good = {'Host': 'bench.example', 'Authorization': 'Bearer ' + 'a' * 64}
        with self.assertRaises((ssl.SSLError, RemoteDisconnected, ConnectionResetError)):
            probe(tls, good | {'X-Forwarded-Proto': 'https'})
        tls.load_cert_chain(self.cert, self.key)  # Synthetic cert only, never a production proxy key.
        self.assertEqual(probe(tls, {'Host': 'bench.example'})[0], 401)
        self.assertEqual(probe(tls, good | {'Host': 'wrong.example', 'X-Forwarded-Host': 'bench.example'})[0], 403)
        self.assertEqual(probe(tls, good), (404, b'{"error": "Not found"}'))
        self.assertFalse(self.store.root.exists())
