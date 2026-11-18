"""Folder/JSON storage. Only project.json makes an installed revision visible."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z')
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_JSON = 1024 * 1024
VERSION = re.compile(r'[a-f0-9]{32}')


class BenchError(ValueError):
    pass


class StateChanged(BenchError):
    pass


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise BenchError('IDs must be 1–64 lowercase letters, digits or hyphens')
    return value


def filename(value):
    if not isinstance(value, str) or not FILENAME.fullmatch(value):
        raise BenchError('Artifact names must be safe basenames (1–120 characters)')
    return value


@contextmanager
def directory(path, create=False):
    """Walk with no-follow dirfds, including every ancestor of absolute paths."""
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def child_directory(fd, name, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        yield child
    finally:
        os.close(child)


def read_bytes(fd, name, limit=MAX_FILE):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise BenchError('Expected a regular file within size limit')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise BenchError('File exceeds size limit')
        return data


def decode_json(data):
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BenchError('Invalid JSON manifest') from exc


def read_json(fd, name):
    return decode_json(read_bytes(fd, name, MAX_JSON))


def state_tag(data):
    # The committed bytes include project identity, full state and (on writes) a fresh nonce.
    return hashlib.sha256(data).hexdigest()[:32]


def write_bytes(fd, name, data):
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    with os.fdopen(handle, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def encoded(value, *, compact=False):
    data = (json.dumps(value, indent=None if compact else 2, separators=(',', ':') if compact else None,
                       ensure_ascii=False, allow_nan=False) + '\n').encode()
    if len(data) > MAX_JSON:
        raise BenchError('Serialized JSON exceeds reader size limit')
    return data


def replace_index(pfd, data):
    pointer = '.pointer-' + uuid.uuid4().hex
    try:
        write_bytes(pfd, pointer, data)
        os.replace(pointer, 'project.json', src_dir_fd=pfd, dst_dir_fd=pfd)
    finally:
        try:
            os.unlink(pointer, dir_fd=pfd)
        except FileNotFoundError:
            pass
    os.fsync(pfd)


def validate_spec(spec):
    if not isinstance(spec, dict):
        raise BenchError('Specification must be an object')
    allowed = {'id','revision','name','requirements','dimensions_mm','notes','status',
               'status_evidence','artifacts','thumbnail'}
    if set(spec) - allowed:
        raise BenchError('Unknown specification fields')
    identifier(spec.get('id'))
    identifier(spec.get('revision'))
    for key in ['name','notes']:
        if not isinstance(spec.get(key), str) or not spec[key].strip() or len(spec[key]) > 10000:
            raise BenchError(f'{key} must be nonempty text (max 10000 characters)')
    req = spec.get('requirements')
    if not isinstance(req, list) or not req or len(req) > 100 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 10000 for v in req):
        raise BenchError('requirements must be a nonempty list of text')
    dims = spec.get('dimensions_mm')
    if not isinstance(dims, list) or len(dims) != 3 or any(
            type(v) not in (int,float) or not 0 < v <= sys.float_info.max for v in dims):
        raise BenchError('dimensions_mm must contain three positive finite float-range numbers')
    names = spec.get('artifacts')
    if not isinstance(names, list) or not names or len(names) > 100:
        raise BenchError('artifacts must contain 1–100 basenames')
    for name in names:
        filename(name)
        if name == 'manifest.json':
            raise BenchError('manifest.json is reserved')
    if len(set(names)) != len(names):
        raise BenchError('Duplicate artifact name')
    if spec.get('thumbnail') is not None and (
            spec['thumbnail'] not in names or Path(spec['thumbnail']).suffix.lower() not in {'.png','.jpg','.jpeg'}):
        raise BenchError('thumbnail must name a supplied PNG or JPEG')
    statuses = spec.get('status', {})
    if not isinstance(statuses, dict) or set(statuses) - {'mesh','slicer','physical'} or any(
            v not in ('unknown','passed','failed','not-tested') for v in statuses.values()):
        raise BenchError('Invalid independent mesh/slicer/physical status')
    evidence = spec.get('status_evidence', {})
    if not isinstance(evidence, dict) or set(evidence) - {'mesh','slicer','physical'} or any(
            v not in names for v in evidence.values()):
        raise BenchError('Status evidence must reference supplied artifacts')
    for key, value in statuses.items():
        if value in ('passed','failed') and key not in evidence:
            raise BenchError(f'{key} status requires a supplied evidence artifact')


class Store:
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))

    @contextmanager
    def _writer(self, *, create=False):
        with directory(self.root, create=create) as root:
            fcntl.flock(root, fcntl.LOCK_EX)
            yield root

    def _index(self, pfd, project):
        # Atomic replacement leaves an already-open fd on one complete index snapshot.
        data = read_bytes(pfd, 'project.json', MAX_JSON)
        index = decode_json(data)
        if not isinstance(index, dict) or index.get('schema') != 1 or index.get('id') != project:
            raise BenchError('Invalid project index')
        revisions = index.get('revisions')
        if not isinstance(revisions, list) or not revisions:
            raise BenchError('Invalid revision list')
        for rev in revisions:
            identifier(rev)
        if len(set(revisions)) != len(revisions) or index.get('current') not in revisions:
            raise BenchError('Invalid current revision pointer')
        if 'version' not in index and 'archived' not in index:
            index['archived'] = False
        elif (type(index.get('archived')) is not bool or not isinstance(index.get('version'), str)
                or not VERSION.fullmatch(index['version'])):
            raise BenchError('Invalid project visibility state')
        return index, data

    def _visible_index(self, pfd, project):
        index, _ = self._index(pfd, project)
        if index['archived']:
            raise FileNotFoundError('Project not found')
        return index

    def state(self, project):
        identifier(project)
        with directory(self.root) as root, child_directory(root, project) as pfd:
            index, data = self._index(pfd, project)
            return state_tag(data), {key:index[key] for key in ('current','archived')}

    def set_archived(self, project, archived, expected):
        identifier(project)
        with self._writer() as root, child_directory(root, project) as pfd:
            index, data = self._index(pfd, project)
            if state_tag(data) != expected:
                raise StateChanged('Project state changed')
            if index['archived'] == archived:
                raise BenchError('Project is already in that state')
            index.update(version=uuid.uuid4().hex, archived=archived)
            data = encoded(index, compact=True)
            replace_index(pfd, data)
            return state_tag(data), {key:index[key] for key in ('current','archived')}

    def history(self, project):
        identifier(project)
        with directory(self.root) as root, child_directory(root, project) as pfd:
            index = self._visible_index(pfd, project)
            return {key:index[key] for key in ('current','revisions')}

    def get(self, project, revision=None):
        identifier(project)
        if revision is not None:
            identifier(revision)
        with directory(self.root) as root, child_directory(root, project) as pfd:
            return self._manifest(pfd, project, self._visible_index(pfd, project), revision)

    def _manifest(self, pfd, project, index, revision=None):
        revision = revision or index['current']
        if revision not in index['revisions']:
            raise BenchError('Revision is not published')
        with child_directory(pfd, 'revisions') as revs, child_directory(revs, revision) as rfd:
            result = read_json(rfd, 'manifest.json')
        if not isinstance(result, dict) or result.get('id') != project or result.get('revision') != revision:
            raise BenchError('Invalid revision identity')
        artifacts = result.get('artifacts')
        if not isinstance(artifacts, list) or not artifacts:
            raise BenchError('Invalid artifact manifest')
        for a in artifacts:
            if not isinstance(a, dict):
                raise BenchError('Invalid artifact entry')
            filename(a.get('name'))
            if not isinstance(a.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',a['sha256']):
                raise BenchError('Invalid artifact hash')
            if type(a.get('bytes')) is not int or not 0 <= a['bytes'] <= MAX_FILE:
                raise BenchError('Invalid artifact size')
        return result

    def list(self):
        if not self.root.exists() and not self.root.is_symlink():
            return []
        projects = []
        with directory(self.root) as root:
            for project in sorted(os.listdir(root)):
                with child_directory(root, identifier(project)) as pfd:
                    try:
                        index = self._visible_index(pfd, project)
                    except FileNotFoundError:
                        continue  # A first publish may still be staging, or have failed.
                    projects.append(self._manifest(pfd, project, index))
        return projects

    def artifact(self, project, revision, name):
        identifier(project)
        identifier(revision)
        filename(name)
        with directory(self.root) as root, child_directory(root, project) as pfd:
            manifest = self._manifest(pfd, project, self._visible_index(pfd, project), revision)
            entry = next((a for a in manifest['artifacts'] if a['name'] == name), None)
            if entry is None:
                raise BenchError('Artifact is not published')
            with child_directory(pfd, 'revisions') as revs, child_directory(revs, revision) as rfd:
                data = read_bytes(rfd, name)
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise BenchError('Artifact integrity check failed')
        return data, entry

    def publish(self, spec, source):
        validate_spec(spec)
        # Open/read sources before creating anything in the store.
        content = {}
        with directory(source) as sfd:
            for name in spec['artifacts']:
                content[name] = read_bytes(sfd, name)
                if sum(map(len, content.values())) > MAX_TOTAL:
                    raise BenchError('Total artifacts exceed 256 MiB')
        return self._publish(spec, content, os.path.abspath(source))

    def _publish(self, spec, content, source, *, expected=None):
        project, revision = spec['id'], spec['revision']
        manifest = {key:value for key,value in spec.items() if key != 'artifacts'}
        manifest['schema'] = 1
        manifest['status'] = dict.fromkeys(('mesh','slicer','physical'), 'unknown') | spec.get('status', {})
        manifest['status_evidence'] = spec.get('status_evidence', {})
        manifest['artifacts'] = [
            {'name':name, 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest(),
             'provenance':{'source_directory':source, 'source_name':name}}
            for name,data in content.items()]
        manifest_bytes = encoded(manifest)
        create = expected in (None, '*')
        with self._writer(create=create) as root:
            with child_directory(root, project, create=create) as pfd:
                try:
                    index, data = self._index(pfd, project)
                except FileNotFoundError:
                    if not create:
                        raise StateChanged('Project state changed') from None
                    index = {'schema':1,'id':project,'current':None,'revisions':[], 'archived':False}
                else:
                    if expected == '*' or (expected is not None and state_tag(data) != expected):
                        raise StateChanged('Project state changed')
                if index['archived']:
                    raise BenchError('Archived project must be restored before publication')
                if revision in index['revisions']:
                    raise BenchError('Duplicate revision; publish a new revision ID')
                index['revisions'].append(revision)
                index['current'] = revision
                index['version'] = uuid.uuid4().hex
                index_bytes = encoded(index, compact=True)
                with child_directory(pfd, 'revisions', create=True) as revs:
                    try:
                        os.stat(revision, dir_fd=revs, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise BenchError('Revision path already exists (possibly an unpublished orphan)')
                    stage = '.stage-' + uuid.uuid4().hex
                    installed = committed = False
                    os.mkdir(stage, mode=0o700, dir_fd=revs)
                    try:
                        with child_directory(revs, stage) as staging:
                            for name,data in content.items():
                                write_bytes(staging, name, data)
                            write_bytes(staging, 'manifest.json', manifest_bytes)
                            os.fsync(staging)
                        os.rename(stage, revision, src_dir_fd=revs, dst_dir_fd=revs)
                        installed = True
                        os.fsync(revs)
                        replace_index(pfd, index_bytes)
                        committed = True
                        os.fsync(root)
                    finally:
                        if installed and not committed:
                            try:
                                index, _ = self._index(pfd, project)
                                committed = revision in index['revisions']
                            except FileNotFoundError:
                                pass
                            # If index reconciliation itself fails, retain the revision.
                        if not committed and installed:
                            shutil.rmtree(revision, dir_fd=revs)
                        elif not installed:
                            shutil.rmtree(stage, dir_fd=revs)

        return manifest
