import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from printbench.store import Store, BenchError


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in [root, *sorted(root.rglob('*'))] if p.exists() or p.is_symlink()}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'tile.stl').write_bytes(b'real bytes for storage testing')
        self.store = Store(self.base / 'data')
        self.spec = {'id': 'tile', 'revision': 'r1', 'name': 'Tile',
                     'requirements': ['Flat underside'], 'dimensions_mm': [30, 20, 3],
                     'notes': 'First revision', 'artifacts': ['tile.stl']}

    def publish(self, **changes):
        return self.store.publish(dict(self.spec, **changes), self.source)

    def test_publish_get_list_and_independent_unknown_status(self):
        result = self.publish()
        self.assertEqual(self.store.get('tile'), result)
        self.assertEqual(self.store.list(), [result])
        self.assertEqual(result['status'], dict.fromkeys(['mesh', 'slicer', 'physical'], 'unknown'))
        self.assertEqual(result['artifacts'][0]['sha256'], hashlib.sha256((self.source/'tile.stl').read_bytes()).hexdigest())
        self.assertEqual(result['artifacts'][0]['provenance']['source_directory'], str(self.source))
        self.assertEqual(self.store.artifact('tile', 'r1', 'tile.stl')[0], (self.source/'tile.stl').read_bytes())

    def test_revisions_immutable_and_explicit_current(self):
        old = self.publish(status={'mesh': 'passed'}, status_evidence={'mesh':'tile.stl'})
        (self.source/'tile.stl').write_bytes(b'new bytes')
        self.publish(revision='r2', notes='Changed', status={'slicer': 'passed'}, status_evidence={'slicer':'tile.stl'})
        self.assertEqual(self.store.get('tile', 'r1'), old)
        self.assertEqual(self.store.get('tile')['revision'], 'r2')
        self.assertEqual(self.store.get('tile')['status']['mesh'], 'unknown')
        self.assertEqual(self.store.get('tile')['status']['physical'], 'unknown')
        self.assertEqual(self.store.history('tile'), {'current': 'r2', 'revisions': ['r1', 'r2']})

    def test_duplicate_rejects_with_no_writes_even_metadata(self):
        self.publish()
        before = snapshot(self.store.root)
        with self.assertRaises(BenchError):
            self.publish(notes='Overwrite')
        self.assertEqual(snapshot(self.store.root), before)

    def test_failure_before_index_commit_rolls_back(self):
        self.publish()
        before = self.store.get('tile')
        original = os.replace
        def fail_pointer(src, dst, **kwargs):
            if dst == 'project.json':
                raise OSError('injected pointer failure')
            return original(src, dst, **kwargs)
        with patch('printbench.store.os.replace', side_effect=fail_pointer):
            with self.assertRaises(OSError):
                self.publish(revision='r2')
        self.assertEqual(self.store.get('tile'), before)
        self.assertEqual(self.store.history('tile')['revisions'], ['r1'])
        self.assertFalse((self.store.root/'tile/revisions/r2').exists())
        self.assertFalse(list(self.store.root.rglob('.stage-*')))
        self.publish(revision='r2')

    def test_failure_during_artifact_copy_does_not_publish(self):
        self.publish()
        with patch('printbench.store.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.publish(revision='r2')
        self.assertEqual(self.store.history('tile'), {'current': 'r1', 'revisions': ['r1']})
        self.assertFalse((self.store.root/'tile/revisions/r2').exists())
        self.assertFalse(list(self.store.root.rglob('.stage-*')))

    def test_first_publish_failure_is_not_a_library_entry(self):
        with patch('printbench.store.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.store.list(), [])
        self.publish()
        self.assertEqual(len(self.store.list()), 1)

    def test_failure_after_pointer_replacement_keeps_complete_revision(self):
        self.publish()
        original = os.replace
        def replace_then_fail(src, dst, **kwargs):
            result = original(src, dst, **kwargs)
            if dst == 'project.json':
                raise OSError('lost acknowledgement after atomic replace')
            return result
        with patch('printbench.store.os.replace', side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                self.publish(revision='r2')
        self.assertEqual(self.store.get('tile')['revision'], 'r2')
        self.assertEqual(self.store.artifact('tile','r2','tile.stl')[0], (self.source/'tile.stl').read_bytes())

    def test_process_crash_before_pointer_commit_never_exposes_partial_revision(self):
        self.publish()
        code = '''
import json,os,sys
from unittest.mock import patch
from printbench.store import Store
spec=json.loads(sys.argv[2])
def crash_before_commit(*args,**kwargs):
    os._exit(73)
with patch('printbench.store.os.replace',side_effect=crash_before_commit):
    Store(sys.argv[1]).publish(spec,sys.argv[3])
'''
        result = subprocess.run([sys.executable,'-c',code,str(self.store.root),
                                 json.dumps(dict(self.spec,revision='r2')),str(self.source)])
        self.assertEqual(result.returncode,73)
        self.assertEqual(self.store.get('tile')['revision'],'r1')
        self.assertEqual(len(self.store.list()),1)
        with self.assertRaises(BenchError):
            self.store.get('tile','r2')
        self.assertEqual(self.store.artifact('tile','r1','tile.stl')[0],(self.source/'tile.stl').read_bytes())
        before = snapshot(self.store.root)
        with self.assertRaises(BenchError):
            self.publish(revision='r2')
        self.assertEqual(snapshot(self.store.root),before)
        self.publish(revision='r3')
        self.assertEqual(self.store.history('tile'),{'current':'r3','revisions':['r1','r3']})

    def test_fsync_failure_after_commit_does_not_remove_visible_revision(self):
        self.publish()
        replace,fsync = os.replace,os.fsync
        replaced = False
        def do_replace(*args,**kwargs):
            nonlocal replaced
            result = replace(*args,**kwargs)
            replaced = True
            return result
        def fail_after_commit(fd):
            if replaced:
                raise OSError('injected postcommit directory fsync failure')
            return fsync(fd)
        with patch('printbench.store.os.replace',side_effect=do_replace), patch('printbench.store.os.fsync',side_effect=fail_after_commit):
            with self.assertRaises(OSError):
                self.publish(revision='r2')
        self.assertEqual(self.store.get('tile')['revision'],'r2')
        self.assertEqual(self.store.artifact('tile','r2','tile.stl')[0],(self.source/'tile.stl').read_bytes())

    def test_status_pass_requires_explicit_evidence(self):
        with self.assertRaises(BenchError):
            self.publish(status={'physical':'passed'})
        self.assertFalse(self.store.root.exists())

    def test_concurrent_publish_preserves_both_revisions(self):
        self.publish()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            list(pool.map(lambda r: self.publish(revision=r), ['r2', 'r3']))
        h = self.store.history('tile')
        self.assertEqual(set(h['revisions']), {'r1','r2','r3'})
        self.assertIn(h['current'], ['r2','r3'])

    def test_invalid_ids_paths_and_specs_rejected_before_writes(self):
        for key, values in {'id': ['../bad','/tmp/x','..','a/b','a%2fb','A',''],
                            'revision': ['../r1','a/b','..'],
                            'artifacts': [['../tile.stl'], ['/etc/passwd'], ['tile.stl','tile.stl']],
                            'dimensions_mm': [[0,1,1], [1,2], [True,2,3], [float('nan'),2,3]],
                            'status': [{'physical':'printed'}, {'mesh':'bogus'}, {'extra':'passed'}]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(BenchError):
                    self.publish(**{key:value})
        self.assertFalse(self.store.root.exists())

    def test_symlink_source_leaf_and_parent_refused(self):
        (self.source/'escape.stl').symlink_to(self.source/'tile.stl')
        with self.assertRaises((BenchError,OSError)):
            self.publish(artifacts=['escape.stl'])
        link = self.base/'source-link'
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises((BenchError,OSError)):
            self.store.publish(self.spec, link)
        self.assertFalse((self.store.root/'tile/revisions/r1').exists())

    def test_symlink_data_root_parent_project_index_revision_artifact_refused(self):
        self.publish()
        link = self.base/'link'
        link.symlink_to(self.store.root, target_is_directory=True)
        for root in [link, link/'nested']:
            with self.assertRaises((BenchError,OSError)):
                Store(root).publish(dict(self.spec,revision='r2'), self.source)
        for rel in ['tile', 'tile/project.json', 'tile/revisions', 'tile/revisions/r1',
                    'tile/revisions/r1/manifest.json', 'tile/revisions/r1/tile.stl']:
            p = self.store.root/rel
            backup = p.with_name(p.name+'.backup')
            p.rename(backup)
            p.symlink_to(backup, target_is_directory=backup.is_dir())
            try:
                with self.subTest(rel=rel), self.assertRaises((BenchError,OSError)):
                    self.store.artifact('tile','r1','tile.stl')
            finally:
                p.unlink()
                backup.rename(p)

    def test_tampered_download_fails_closed(self):
        self.publish()
        (self.store.root/'tile/revisions/r1/tile.stl').write_bytes(b'tampered')
        with self.assertRaises(BenchError):
            self.store.artifact('tile','r1','tile.stl')

    def test_unindexed_revision_is_not_visible(self):
        self.publish()
        orphan = self.store.root/'tile/revisions/orphan'
        orphan.mkdir()
        with self.assertRaises(BenchError):
            self.store.get('tile','orphan')
        self.assertEqual(len(self.store.list()),1)

    def test_cli_publish_get_list_errors_are_structured_json(self):
        spec = self.base/'spec.json'
        spec.write_text(json.dumps(self.spec))
        prefix = [sys.executable,'-m','printbench','--data',str(self.store.root)]
        def run(*args):
            return subprocess.run([*prefix,*args],capture_output=True,text=True)
        p = run('publish','--spec',str(spec),'--source',str(self.source))
        self.assertEqual(p.returncode,0,p.stderr)
        published = json.loads(p.stdout)
        self.assertEqual(json.loads(run('get','tile').stdout),published)
        self.assertEqual(json.loads(run('list').stdout),[published])
        duplicate = run('publish','--spec',str(spec),'--source',str(self.source))
        self.assertNotEqual(duplicate.returncode,0)
        self.assertIn('error',json.loads(duplicate.stderr))
        self.assertEqual(duplicate.stdout,'')


if __name__ == '__main__':
    unittest.main()
