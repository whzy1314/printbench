"""Reader/writer round trips at the review's serialization and numeric boundaries."""
from http.client import HTTPConnection
import hashlib
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from printbench.server import make_server
from printbench.store import BenchError, Store, directory, encoded, read_json
import test_store

JSON_LIMIT = 1024 * 1024


def disk_state(root):
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        if path.exists():
            info = path.stat()
            result[str(path.relative_to(root))] = (
                info.st_mode, info.st_mtime_ns, info.st_ctime_ns, info.st_ino,
                path.read_bytes() if path.is_file() else None)
    return result


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        test_store.StoreTests.setUp(self)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'printbench', '--data',
                               str(self.store.root), *map(str, args)],
                              capture_output=True, text=True, timeout=30)

    def cli_publish(self, spec):
        path = self.base / 'spec.json'
        path.write_text(json.dumps(spec))
        return self.cli('publish', '--spec', path, '--source', self.source)

    def assert_json_rejection(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIsInstance(json.loads(result.stderr)['error'], str)

    def combined_spec(self, revision):
        names = [f'{i:03d}-' + 'a' * 112 + '.stl' for i in range(100)]
        for name in names:
            self.assertEqual(len(name), 120)
            (self.source / name).write_bytes(b'owned')
        spec = dict(self.spec, revision=revision, name='n' * 10000, notes='n' * 10000,
                    requirements=['r' * 10000 for _ in range(100)], artifacts=names)
        self.assertEqual(len(json.dumps(spec).encode()), 1032920)
        self.assertLess(len(json.dumps(spec).encode()), JSON_LIMIT)
        return spec

    def test_cli_generated_manifest_limit_rejects_before_first_write(self):
        result = self.cli_publish(self.combined_spec('r1'))
        self.assert_json_rejection(result)
        self.assertIn('JSON', json.loads(result.stderr)['error'])
        self.assertFalse(self.store.root.exists())
        listed = self.cli('list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), [])

    def test_cli_rejected_r2_preserves_current_history_library_and_downloads(self):
        published = self.cli_publish(self.spec)
        self.assertEqual(published.returncode, 0, published.stderr)
        old = json.loads(published.stdout)
        rejected_spec = self.combined_spec('r2')
        before = disk_state(self.store.root)
        self.assert_json_rejection(self.cli_publish(rejected_spec))
        self.assertEqual(disk_state(self.store.root), before)
        self.assertEqual(self.store.history('tile'), {'current': 'r1', 'revisions': ['r1']})
        for args, expected in [(('get', 'tile'), old),
                               (('get', 'tile', '--revision', 'r1'), old),
                               (('list',), [old])]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
        self.assert_json_rejection(self.cli('get', 'tile', '--revision', 'r2'))
        server = make_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def get(path):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        try:
            for path, expected in [('/api/projects', [old]), ('/api/projects/tile', old),
                                   ('/api/projects/tile/revisions/r1', old),
                                   ('/api/projects/tile/history', {'current':'r1','revisions':['r1']})]:
                status, data = get(path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data), expected)
            status, data = get('/api/projects/tile/revisions/r1/files/tile.stl')
            self.assertEqual(status, 200)
            self.assertEqual(hashlib.sha256(data).hexdigest(), old['artifacts'][0]['sha256'])
            self.assertNotEqual(get('/api/projects/tile/revisions/r2')[0], 200)
            self.assertFalse((self.store.root / 'tile/revisions/r2').exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_json_serialization_and_reader_share_exact_byte_limit(self):
        overhead = len(encoded({'pad': ''}))
        exact = {'pad': 'x' * (JSON_LIMIT - overhead)}
        data = encoded(exact)
        self.assertEqual(len(data), JSON_LIMIT)
        path = self.base / 'limit.json'
        path.write_bytes(data)
        with directory(self.base) as fd:
            self.assertEqual(read_json(fd, path.name), exact)
        with self.assertRaises(BenchError):
            encoded({'pad': exact['pad'] + 'x'})
        path.write_bytes(data + b' ')
        with directory(self.base) as fd, self.assertRaises(BenchError):
            read_json(fd, path.name)

    def test_cli_huge_integer_dimensions_are_json_errors_without_writes(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    accepted = self.cli_publish(self.spec)
                    self.assertEqual(accepted.returncode, 0, accepted.stderr)
                before = disk_state(self.store.root)
                result = self.cli_publish(dict(self.spec, revision='r2', dimensions_mm=[10**400, 20, 3]))
                self.assert_json_rejection(result)
                self.assertEqual(disk_state(self.store.root), before)
                if existing:
                    self.assertEqual(self.store.get('tile')['revision'], 'r1')
                else:
                    self.assertFalse(self.store.root.exists())

    def test_shared_dimension_validation_refuses_overflow_but_accepts_float_range(self):
        for value in (10**400, -10**400, int(sys.float_info.max) + 1):
            with self.subTest(value=value), self.assertRaises(BenchError):
                self.store.publish(dict(self.spec, dimensions_mm=[value, 20, 3]), self.source)
        self.assertFalse(self.store.root.exists())
        for revision, value in [('float-max', sys.float_info.max), ('int-max', int(sys.float_info.max))]:
            self.store.publish(dict(self.spec, revision=revision, dimensions_mm=[value, 20, 3]), self.source)
            self.assertEqual(self.store.get('tile')['dimensions_mm'], [value, 20, 3])

    def test_index_growth_rejects_before_staging_without_changing_history(self):
        # A small shared cap reaches the genuine index-growth boundary with real revisions.
        with patch('printbench.store.MAX_JSON', 1024, create=True):
            self.store.publish(self.spec, self.source)
            history = {'schema':1, 'id':'tile', 'current':'r1', 'revisions':['r1'],
                       'version':'0' * 32, 'archived':False}
            for i in range(30):
                revision = f'r{i:03d}-' + 'x' * 59
                next_index = dict(history, current=revision, revisions=[*history['revisions'], revision])
                size = len((json.dumps(next_index, separators=(',', ':')) + '\n').encode())
                before = disk_state(self.store.root)
                if size > 1024:
                    with self.assertRaises(BenchError):
                        self.store.publish(dict(self.spec, revision=revision), self.source)
                    self.assertEqual(disk_state(self.store.root), before)
                    self.assertEqual(self.store.history('tile'), {k:history[k] for k in ('current','revisions')})
                    self.assertEqual(self.store.get('tile')['revision'], history['current'])
                    self.assertEqual(len(self.store.list()), 1)
                    self.assertEqual(self.store.artifact('tile', 'r1', 'tile.stl')[0], (self.source/'tile.stl').read_bytes())
                    self.assertFalse((self.store.root/'tile/revisions'/revision).exists())
                    break
                self.store.publish(dict(self.spec, revision=revision), self.source)
                history = next_index
            else:
                self.fail('The fixture did not reach the index serialization boundary')


if __name__ == '__main__':
    unittest.main()
