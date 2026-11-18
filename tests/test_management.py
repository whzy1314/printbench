import concurrent.futures
from contextlib import closing
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from printbench import server, store
from printbench.store import Store
import test_store

PATH = '/manage/v1/projects/tile'
TOKEN = 'a' * 64  # Synthetic only.
AUTH = {'Authorization': 'Bearer ' + TOKEN}
UPLOAD = 'application/octet-stream'


def frame(spec, payload=b'model', sizes=None):
    header = json.dumps({'spec': spec, 'sizes': sizes if sizes is not None else [len(payload)]}).encode()
    return len(header).to_bytes(4, 'big') + header + payload


class ManagementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'tile.stl').write_bytes(b'CLI fixture')
        self.store = Store(self.base / 'data')
        self.spec = {'id': 'tile', 'revision': 'r1', 'name': 'Tile', 'requirements': ['Flat underside'],
                     'dimensions_mm': [30, 20, 3], 'notes': 'First revision', 'artifacts': ['tile.stl']}
        self.token = self.base / 'credential'
        self.token.write_text(TOKEN + '\n')
        self.token.chmod(0o600)
        self.read = self.manage = self.start(server.make_server(self.store, 0, management_token_file=self.token))

    def start(self, listener):
        t = threading.Thread(target=listener.serve_forever, daemon=True)
        t.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener

    def request(self, listener, method, path, headers=None, body=None):
        conn = HTTPConnection('127.0.0.1', listener.server_port, timeout=5)
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def manage_request(self, method, path=PATH, headers=None, body=None):
        return self.request(self.manage, method, path, AUTH | (headers or {}), body)

    def publish(self, *, spec=None, match=None, body=None):
        precondition = {'If-Match': match} if match else {'If-None-Match': '*'}
        return self.manage_request('PUT', headers=precondition | {'Content-Type': UPLOAD},
                                   body=frame(spec or self.spec) if body is None else body)

    def state(self):
        status, headers, body = self.manage_request('GET', PATH + '/state')
        self.assertEqual(status, 200)
        return headers['ETag'], json.loads(body)

    def transition(self, action, match):
        return self.manage_request('POST', PATH + '/' + action, {'If-Match': match}, b'')

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'printbench', '--data', str(self.store.root), *args],
                              capture_output=True, text=True, timeout=5)

    def test_read_listener_and_credential_gates(self):
        disabled = self.start(server.make_server(self.store, 0))
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            self.assertEqual(self.request(disabled, method, PATH,
                AUTH | {'Host': 'manage.example', 'X-Forwarded-Host': 'manage.example',
                        'X-Original-URL': PATH}, frame(self.spec))[0], 405)
        self.assertEqual(self.request(disabled, 'GET', PATH + '/state', AUTH)[0], 404)
        self.assertEqual(self.request(self.manage, 'PUT', PATH, {'Content-Length': '100',
                         'Expect': '100-continue', 'X-Forwarded-Proto': 'https', 'X-Real-IP': '127.0.0.1'})[0], 401)
        self.assertEqual(self.request(self.manage, 'PROPFIND', PATH)[0], 401)
        self.assertEqual(self.manage_request('GET', PATH + '/state', {'Authorization': 'Bearer ' + 'b' * 64})[0], 401)
        self.assertEqual(self.manage_request('GET', PATH + '/state', {'Host': 'other.example',
                         'X-Forwarded-Host': 'localhost'})[0], 403)
        self.assertEqual(self.request(self.read, 'GET', '/api/projects')[0], 200)
        self.assertEqual(self.manage_request('PUT', '/api/projects', body=frame(self.spec))[0], 405)
        self.assertEqual(self.request(self.manage, 'GET', PATH + '/state?x=y', AUTH)[0], 404)
        with closing(HTTPConnection('127.0.0.1', self.manage.server_port, timeout=2)) as conn:
            conn.putrequest('PUT', PATH)
            conn.putheader('Authorization', AUTH['Authorization'])
            conn.putheader('Authorization', AUTH['Authorization'])
            conn.putheader('Content-Length', '100')
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 401)  # No body sent.
        self.token.chmod(0o644)
        self.assertEqual(self.publish()[0], 503)
        self.token.unlink()
        self.assertEqual(self.publish()[0], 503)
        self.assertFalse(self.store.root.exists())

    def test_malformed_framing_filename_and_boundaries_never_publish(self):
        for target, headers, data, expected in [
            (PATH, {}, b'', 428),
            (PATH, {'If-None-Match': '*', 'Transfer-Encoding': 'chunked'}, b'', 400),
            (PATH, {'If-None-Match': '*', 'Expect': '100-continue'}, b'', 400),
            (PATH, {'If-None-Match': '*', 'Content-Length': str(66 * 1024 * 1024)}, b'', 413),
            (PATH, {'If-None-Match': '*'}, b'broken', 400),
            (PATH, {'If-None-Match': '*'}, frame(self.spec, sizes=[6]), 400),
            (PATH, {'If-None-Match': '*'}, frame(self.spec, sizes=[4]), 400),
            (PATH, {'If-None-Match': '*'}, frame(dict(self.spec, artifacts=['../tile.stl'])), 400),
            (PATH, {'If-None-Match': '*'}, frame(dict(self.spec, artifacts=['manifest.json'])), 400),
            (PATH, {'If-None-Match': '*'}, frame(dict(self.spec, notes='test\x00control')), 400),
            (PATH, {'If-None-Match': '*'}, frame(dict(self.spec, dimensions_mm=[10**400, 2, 3])), 400),
            ('/manage/v1/projects/tile%2fextra', {'If-None-Match': '*'}, frame(self.spec), 404),
            (PATH, {'If-None-Match': '*'}, frame(dict(self.spec, id='other')), 400),
        ]:
            with self.subTest(target=target, headers=headers, expected=expected):
                status, _, body = self.manage_request('PUT', target, {'Content-Type': UPLOAD} | headers, data)
                self.assertEqual(status, expected, body)
                self.assertFalse(self.store.root.exists())
        conn = HTTPConnection('127.0.0.1', self.manage.server_port, timeout=5)
        with closing(conn):
            conn.putrequest('PUT', PATH)
            for key, value in (AUTH | {'If-None-Match': '*', 'Content-Type': UPLOAD,
                                        'Content-Length': '0'}).items():
                conn.putheader(key, value)
            conn.putheader('Content-Length', '0')
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400)
        self.assertFalse(self.store.root.exists())

    def test_direct_backend_host_and_bearer_are_independent(self):
        with patch('printbench.server.address', return_value=('127.0.0.1', 0)):
            listener = self.start(server.make_server(self.store, 0, host='bench.example',
                                                       management_token_file=self.token))
        host = {'Host': 'bench.example'}
        spoof = {'X-Forwarded-Host': 'bench.example', 'X-Forwarded-Proto': 'https'}
        self.assertEqual(self.request(listener, 'GET', '/api/projects', host)[0], 200)
        self.assertEqual(self.request(listener, 'GET', PATH + '/state', host | spoof)[0], 401)
        self.assertEqual(self.request(listener, 'GET', PATH + '/state', AUTH | spoof)[0], 403)
        self.assertFalse(self.store.root.exists())
        put = host | AUTH | {'Content-Type': UPLOAD, 'If-None-Match': '*'}
        self.assertEqual(self.request(listener, 'PUT', PATH, put, frame(self.spec))[0], 201)
        self.assertEqual(self.request(listener, 'GET', '/api/projects/tile', host)[0], 200)

    def test_create_append_archive_restore_preserves_hashes_and_rejects_replays(self):
        self.assertEqual(self.publish()[0], 201)
        before = test_store.snapshot(self.store.root)
        self.assertEqual(self.publish()[0], 412)
        self.assertEqual(test_store.snapshot(self.store.root), before)
        first, state = self.state()
        self.assertEqual(state, {'current': 'r1', 'archived': False})
        r2 = dict(self.spec, revision='r2', notes='Changed', status={'slicer': 'not-tested'})
        self.assertEqual(self.publish(body=frame(r2, b'different model bytes'), match=first)[0], 201)
        second, _ = self.state()
        self.assertNotEqual(first, second)
        self.assertEqual(self.publish(spec=r2, match=first)[0], 412)
        self.assertEqual(self.publish(spec=r2, match=second)[0], 409)
        self.assertEqual(self.transition('archive', second)[0], 200)
        archived, state = self.state()
        self.assertEqual(state, {'current': 'r2', 'archived': True})
        self.assertEqual(json.loads(self.cli('list').stdout), [])
        hidden = self.cli('get', 'tile', '--revision', 'r1')
        self.assertEqual((hidden.returncode, hidden.stdout), (1, ''))
        self.assertIn('error', json.loads(hidden.stderr))
        self.assertEqual(json.loads(self.request(self.read, 'GET', '/api/projects')[2]), [])
        for tail in ('', '/history', '/revisions/r1', '/revisions/r2', '/revisions/r1/files/tile.stl',
                     '/revisions/r2/files/tile.stl'):
            for method in ('GET', 'HEAD'):
                status, headers, body = self.request(self.read, method, '/api/projects/tile' + tail,
                                                     {'If-None-Match': '*'})
                self.assertEqual(status, 404, tail)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('ETag', headers)
                if method == 'HEAD':
                    self.assertEqual(body, b'')
        self.assertEqual(self.transition('restore', archived)[0], 200)
        restored, _ = self.state()
        self.assertNotIn(restored, (first, second, archived))
        self.assertEqual(self.transition('archive', second)[0], 412)
        self.assertEqual(self.transition('restore', archived)[0], 412)
        history = self.request(self.read, 'GET', '/api/projects/tile/history')[2]
        self.assertEqual(json.loads(history), {'current': 'r2', 'revisions': ['r1', 'r2']})
        download = self.request(self.read, 'GET', '/api/projects/tile/revisions/r1/files/tile.stl')
        self.assertEqual((download[0], download[2]), (200, b'model'))
        self.assertEqual(download[1]['X-Content-SHA256'], hashlib.sha256(b'model').hexdigest())
        for path, record in before.items():
            if path.startswith('tile/revisions/r1/'):
                self.assertEqual(test_store.snapshot(self.store.root)[path], record)
        self.assertEqual(json.loads(self.cli('get', 'tile').stdout)['status'],
                         {'mesh': 'unknown', 'slicer': 'not-tested', 'physical': 'unknown'})

    def test_cli_and_concurrent_api_writers_share_cas(self):
        self.store.publish(self.spec, self.source)
        old, _ = self.state()
        spec_file = self.base / 'spec.json'
        spec_file.write_text(json.dumps(dict(self.spec, revision='cli-r2')))
        self.assertEqual(self.cli('publish', '--spec', str(spec_file), '--source', str(self.source)).returncode, 0)
        self.assertEqual(self.transition('archive', old)[0], 412)
        fresh, _ = self.state()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda rev: self.publish(spec=dict(self.spec, revision=rev), match=fresh)[0],
                                    ('next-a', 'next-b')))
        self.assertEqual(sorted(results), [201, 412])
        self.assertEqual(len(self.store.history('tile')['revisions']), 3)

    def test_symlink_store_target_denied_and_rotated_token_rejected(self):
        self.store.root.mkdir()
        (self.store.root / 'tile').symlink_to(self.source, target_is_directory=True)
        self.assertNotEqual(self.publish()[0], 201)
        self.assertFalse((self.source / 'project.json').exists())
        self.token.write_text('c' * 64)
        self.assertEqual(self.publish()[0], 401)

    def test_near_limit_legacy_index_remains_archivable(self):
        self.store.publish(self.spec, self.source)
        index = self.store.root / 'tile/project.json'
        legacy = {'schema': 1, 'id': 'tile', 'current': 'r1',
                  'revisions': ['r1', *[f'r{i:03d}-' + 'x' * 30 for i in range(25)]]}
        formatted = json.dumps(legacy, indent=2).encode() + b'\n'
        index.write_bytes(formatted)
        with patch('printbench.store.MAX_JSON', len(formatted)):
            old, state = self.state()
            self.assertEqual(state, {'current': 'r1', 'archived': False})
            self.assertEqual(self.transition('archive', old)[0], 200)
            new, _ = self.state()
            self.assertEqual(self.transition('restore', new)[0], 200)
            self.assertEqual(self.store.history('tile')['revisions'], legacy['revisions'])

    def test_lost_archive_acknowledgement_is_observable_and_not_replayed(self):
        self.assertEqual(self.publish()[0], 201)
        version, _ = self.state()
        from os import replace

        def replace_then_fail(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('injected lost acknowledgement')

        with patch('printbench.store.os.replace', side_effect=replace_then_fail):
            self.assertEqual(self.transition('archive', version)[0], 500)
        changed, state = self.state()
        self.assertTrue(state['archived'])
        self.assertEqual(self.transition('archive', version)[0], 412)
        self.assertEqual(self.transition('restore', changed)[0], 200)
        self.assertEqual(self.transition('archive', version)[0], 412)

    def test_committed_reads_complete_during_artifact_write_and_flush(self):
        self.assertEqual(self.publish()[0], 201)
        self.store.publish(dict(self.spec, id='other'), self.source)
        root = '/api/projects'
        for revision, flush in [('r2', False), ('r3', True)]:
            with self.subTest(flush=flush):
                tag, state = self.state()
                entered, release = threading.Event(), threading.Event()
                write, fsync = store.write_bytes, os.fsync

                def block():
                    entered.set()
                    if not release.wait(8):
                        raise TimeoutError('Fixture did not release writer')

                def stall_write(fd, name, data):
                    if name == 'tile.stl' and not flush:
                        block()
                    return write(fd, name, data)

                def stall_flush(fd):
                    if flush and os.readlink(f'/proc/self/fd/{fd}').endswith('/tile/revisions'):
                        block()  # Revision renamed, but not yet committed in the index.
                    return fsync(fd)

                with patch('printbench.store.write_bytes', side_effect=stall_write), \
                        patch('printbench.store.os.fsync', side_effect=stall_flush), \
                        concurrent.futures.ThreadPoolExecutor() as workers:
                    writer = workers.submit(self.publish, spec=dict(self.spec, revision=revision), match=tag)
                    try:
                        self.assertTrue(entered.wait(3))
                        download = root + '/tile/revisions/r1/files/tile.stl'
                        pending_rev = root + '/tile/revisions/' + revision
                        checks = [('GET', root, 200), ('HEAD', root, 200),  # Chart readiness.
                                  ('GET', root + '/tile', 200), ('HEAD', root + '/tile', 200),
                                  ('GET', root + '/tile/history', 200),
                                  ('GET', root + '/tile/revisions/r1', 200),
                                  ('GET', download, 200), ('HEAD', download, 200),
                                  ('GET', root + '/other', 200), ('GET', pending_rev, 409),
                                  ('HEAD', pending_rev + '/files/tile.stl', 409)]
                        reads = [workers.submit(self.request, self.read, method, path) for method, path, _ in checks]
                        status = workers.submit(self.state)
                        self.assertFalse(concurrent.futures.wait(reads, timeout=3).not_done,
                                         'Committed HTTP reads blocked behind the writer')
                        self.assertFalse(writer.done())
                        for (method, path, code), result in zip(checks, reads):
                            self.assertEqual(result.result()[0], code, (method, path))
                        self.assertEqual(json.loads(reads[2].result()[2])['revision'], state['current'])
                        self.assertEqual(reads[6].result()[2], b'model')
                        self.assertEqual(status.result(timeout=1), (tag, state))
                    finally:
                        release.set()
                    self.assertEqual(writer.result()[0], 201)
                self.assertEqual(json.loads(self.request(self.read, 'GET', root + '/tile')[2])['revision'], revision)

    def test_preconditions_bind_project_and_complete_legacy_and_versioned_indexes(self):
        for project in ('tile', 'other'):
            self.store.publish(dict(self.spec, id=project), self.source)
            index = self.store.root / project / 'project.json'
            index.write_text(json.dumps({'schema': 1, 'id': project, 'current': 'r1', 'revisions': ['r1']}))
        tile_index = self.store.root / 'tile/project.json'
        before = test_store.snapshot(self.store.root)
        tile, _ = self.state()
        other_status, other_headers, _ = self.manage_request('GET', '/manage/v1/projects/other/state')
        self.assertEqual(other_status, 200)
        self.assertEqual(test_store.snapshot(self.store.root), before)
        self.assertEqual(self.transition('archive', other_headers['ETag'])[0], 412)
        self.assertEqual(self.publish(spec=dict(self.spec, revision='r2'), match=other_headers['ETag'])[0], 412)
        self.assertEqual(test_store.snapshot(self.store.root), before)

        for versioned in (False, True):
            with self.subTest(versioned=versioned):
                old = tile if not versioned else self.state()[0]
                updated = json.loads(tile_index.read_bytes())
                updated['revisions'].insert(0, 'historical' if versioned else 'previous')
                tile_index.write_bytes(store.encoded(updated, compact=True))
                before = test_store.snapshot(self.store.root)
                self.assertEqual(self.transition('archive', old)[0], 412)
                self.assertEqual(self.publish(spec=dict(self.spec, revision='r2'), match=old)[0], 412)
                self.assertEqual(test_store.snapshot(self.store.root), before)
                current, _ = self.state()
                self.assertNotEqual(old, current)
                self.assertEqual(self.transition('archive', current)[0], 200)
                archived, _ = self.state()
                self.assertEqual(self.transition('restore', archived)[0], 200)

    def test_download_already_on_a_snapshot_can_finish_after_archive(self):
        self.assertEqual(self.publish()[0], 201)
        tag, _ = self.state()
        enter, release = threading.Event(), threading.Event()
        manifest = self.store._manifest

        def stall(*args):
            enter.set()
            if not release.wait(6):
                raise TimeoutError('Fixture did not release reader')
            return manifest(*args)

        path = '/api/projects/tile/revisions/r1/files/tile.stl'
        with patch.object(self.store, '_manifest', side_effect=stall), concurrent.futures.ThreadPoolExecutor() as workers:
            read = workers.submit(self.request, self.read, 'GET', path)
            try:
                self.assertTrue(enter.wait(2))
                self.assertEqual(self.transition('archive', tag)[0], 200)
                self.assertEqual(self.request(self.read, 'HEAD', path)[0], 404)
            finally:
                release.set()
            self.assertEqual(read.result()[2], b'model')
        archived, _ = self.state()
        self.assertEqual(self.transition('restore', archived)[0], 200)
        self.assertEqual(self.request(self.read, 'GET', path)[2], b'model')

    def test_artifact_aggregate_limit_is_checked_before_payload(self):
        cap = 64 * 1024 * 1024
        spec = dict(self.spec, artifacts=['tile.stl', 'other.stl'])
        prefix = frame(spec, b'', [cap, 1])
        length = len(prefix) + cap + 1  # Inside the separate maximum wire envelope.
        with closing(HTTPConnection('127.0.0.1', self.manage.server_port, timeout=2)) as conn:
            conn.request('PUT', PATH, prefix, AUTH | {'If-None-Match': '*', 'Content-Type': UPLOAD,
                                                     'Content-Length': str(length)})
            response = conn.getresponse()  # Withhold payload while the connection stays open.
            self.assertEqual(response.status, 413)
            response.read()
        self.assertFalse(self.store.root.exists())
        payload = b'x' * (cap - 1) + b'y'
        status, _, data = self.publish(body=frame(spec, payload, [cap - 1, 1]))
        self.assertEqual(status, 201, data)
        self.assertEqual([a['bytes'] for a in json.loads(data)['artifacts']], [cap - 1, 1])
        self.assertEqual(self.store.artifact('tile', 'r1', 'other.stl')[0], b'y')
