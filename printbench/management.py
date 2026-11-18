"""Bounded, authenticated management protocol. Never mount on the read handler."""
from contextlib import contextmanager
import hmac
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import ssl
import stat
import threading
import time
import unicodedata

from .server import Handler, address
from .store import (BenchError, MAX_FILE, MAX_JSON, VERSION, StateChanged,
                    child_directory, directory, validate_spec)

PREFIX = '/manage/v1/projects/'
ROUTE = re.compile(PREFIX + r'([a-z0-9][a-z0-9-]{0,63})(?:/(state|archive|restore))?')
TOKEN = re.compile(rb'[a-f0-9]{64}\n?')
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_BODY = 4 + MAX_JSON + MAX_ARTIFACT_BYTES
SOCKET_TIMEOUT = 10


@contextmanager
def protected_file(path, *, projection=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    @contextmanager
    def checked(fd, name):
        with os.fdopen(os.open(name, flags, dir_fd=fd), 'rb') as stream:
            info = os.fstat(stream.fileno())
            mode = stat.S_IMODE(info.st_mode)
            own = info.st_uid == os.geteuid() and mode in (0o400, 0o600)
            kube = projection and info.st_uid == 0 and info.st_gid == os.getegid() and mode == 0o440
            if not stat.S_ISREG(info.st_mode) or not (own or kube):
                raise OSError('Expected a protected regular file')
            yield stream

    with directory(path.parent) as parent:
        name = path.name
        # Native Secret volumes use <key> -> ..data/<key>, not a subPath mount.
        if projection and stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
            if os.readlink(name, dir_fd=parent) != f'..data/{name}':
                raise OSError('Invalid credential projection')
            version = os.readlink('..data', dir_fd=parent)
            if not re.fullmatch(r'\.\.[0-9][0-9_.-]{1,100}', version):
                raise OSError('Invalid credential projection')
            with child_directory(parent, version) as projected:
                with checked(projected, name) as stream:
                    yield stream
        else:
            with checked(parent, name) as stream:
                yield stream


def read_token(path, *, projection=False):
    with protected_file(path, projection=projection) as stream:
        token = stream.read(66)
    if not TOKEN.fullmatch(token):
        raise OSError('Invalid credential format')
    return token.rstrip(b'\n').decode('ascii')


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate JSON keys')
    return result


class LimitedServer(ThreadingHTTPServer):
    def __init__(self, bind, handler):
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(bind, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Connection failures and tracebacks must not echo request data or credentials.
        pass


def make_management_server(store, port, credential, *, host=None, tls=None):
    bind = address(port, host)
    if host is not None and (tls is None or tls.verify_mode != ssl.CERT_REQUIRED):
        raise BenchError('Network management requires TLS with proxy client authentication')
    credential = Path(credential).absolute()

    class ManagementHandler(Handler):
        timeout = SOCKET_TIMEOUT

        def setup(self):
            if tls is not None:
                self.request.settimeout(self.timeout)
                self.request = tls.wrap_socket(self.request, server_side=True)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                if tls is not None:
                    self.request.close()

        def send_error(self, code, message=None, explain=None):
            self.result(code, {'error': 'Unsupported or malformed HTTP request'})

        def result(self, code, message, **headers):
            self.reply(code, json.dumps(message, ensure_ascii=True).encode(), extra=headers)

        def header(self, name):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                raise RequestError(400, 'Duplicate header')
            return values[0] if values else None

        def authenticated(self):
            try:
                token = read_token(credential, projection=True)
            except OSError:
                raise RequestError(503, 'Management credential unavailable') from None
            auth = self.headers.get_all('Authorization')
            return bool(auth and len(auth) == 1 and re.fullmatch(r'Bearer [a-f0-9]{64}', auth[0])
                        and hmac.compare_digest(auth[0][7:], token))

        def take(self, length, deadline):
            chunks = bytearray()
            while len(chunks) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestError(408, 'Upload deadline exceeded')
                self.connection.settimeout(min(SOCKET_TIMEOUT, remaining))
                chunk = self.rfile.read1(min(65536, length - len(chunks)))
                if not chunk:
                    raise RequestError(400, 'Incomplete upload')
                chunks.extend(chunk)
            self.connection.settimeout(self.timeout)
            return chunks

        def upload(self, length, project):
            if (length is None or length < 5 or self.header('Content-Type') != 'application/octet-stream'):
                raise RequestError(400, 'Expected a length-delimited PrintBench upload')
            deadline = time.monotonic() + 60
            meta_length = int.from_bytes(self.take(4, deadline), 'big')
            if not 1 <= meta_length <= min(MAX_JSON, length - 4):
                raise RequestError(400, 'Invalid metadata length')
            try:
                meta = json.loads(self.take(meta_length, deadline).decode('utf-8'), object_pairs_hook=unique_object)
                if not isinstance(meta, dict) or set(meta) != {'spec', 'sizes'}:
                    raise ValueError('Invalid metadata fields')
                spec, sizes = meta['spec'], meta['sizes']
                validate_spec(spec)
                if spec['id'] != project:
                    raise ValueError('Project identity mismatch')
                text = [spec['name'], spec['notes'], *spec['requirements']]
                if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') and c not in '\t\n\r'
                       for field in text for c in field):
                    raise ValueError('Invalid control character in metadata')
                names = spec['artifacts']
                if (not isinstance(sizes, list) or len(sizes) != len(names)
                        or any(type(size) is not int or not 0 <= size <= MAX_FILE for size in sizes)
                        or sum(sizes) != length - 4 - meta_length):
                    raise ValueError('Invalid file lengths')
            except (ValueError, RecursionError):
                raise RequestError(400, 'Invalid upload metadata or file lengths') from None
            if sum(sizes) > MAX_ARTIFACT_BYTES:
                raise RequestError(413, 'Total artifacts exceed 64 MiB')
            return spec, {name:self.take(size, deadline) for name, size in zip(names, sizes)}

        def manage(self):
            self.close_connection = True
            try:
                hosts = self.headers.get_all('Host')
                port = listener.server_port
                allowed = ([host],) if host is not None else ([f'127.0.0.1:{port}'], [f'localhost:{port}'])
                if hosts not in allowed:
                    raise RequestError(403, 'Unexpected Host')
                route = ROUTE.fullmatch(self.path)
                if route is None or self.requestline.split()[1] != self.path:
                    raise RequestError(404, 'Not found')
                if not self.authenticated():
                    return self.result(401, {'error': 'Credential required'},
                                       **{'WWW-Authenticate': 'Bearer realm="PrintBench"'})
                if self.headers.defects or any(self.headers.get_all(name) is not None for name in
                                               ('Transfer-Encoding', 'Content-Encoding', 'Expect')):
                    raise RequestError(400, 'Unsupported request framing')
                raw_length = self.header('Content-Length')
                if raw_length is not None and not re.fullmatch(r'0|[1-9][0-9]{0,9}', raw_length):
                    raise RequestError(400, 'Invalid Content-Length')
                length = int(raw_length) if raw_length is not None else None
                if length is not None and length > MAX_BODY:
                    raise RequestError(413, 'Upload too large')
                project, action = route.groups()
                allowed_method = {None: 'PUT', 'state': 'GET', 'archive': 'POST', 'restore': 'POST'}[action]
                if self.command != allowed_method:
                    return self.result(405, {'error': 'Method not allowed'}, Allow=allowed_method)
                if action is not None and length not in (None, 0):
                    raise RequestError(400, 'Unexpected request body')
                if action == 'state':
                    version, state = store.state(project)
                    return self.result(200, state, ETag=f'"{version}"')
                match = self.header('If-Match')
                absent = self.header('If-None-Match')
                if action is None and absent == '*' and match is None:
                    expected = '*'
                elif absent is None and match is not None and VERSION.fullmatch(match[1:-1]) and match[0] == match[-1] == '"':
                    expected = match[1:-1]
                elif match is absent is None:
                    raise RequestError(428, 'Project precondition required')
                else:
                    raise RequestError(400, 'Invalid project precondition')
                if action is None:
                    spec, content = self.upload(length, project)
                    if not self.authenticated():
                        raise RequestError(401, 'Credential changed during upload')
                    manifest = store._publish(spec, content, 'PrintBench management API', expected=expected)
                    return self.result(201, manifest)
                version, state = store.set_archived(project, action == 'archive', expected)
                return self.result(200, state, ETag=f'"{version}"')
            except RequestError as exc:
                return self.result(exc.status, {'error': str(exc)})
            except StateChanged:
                return self.result(412, {'error': 'Project state changed; read back before deciding what to do'})
            except FileNotFoundError:
                return self.result(404, {'error': 'Not found'})
            except TimeoutError:
                return self.result(408, {'error': 'Request timed out; read back before retrying'})
            except BenchError:
                return self.result(409, {'error': 'Publication or state conflict; read back before retrying'})
            except OSError:
                return self.result(500, {'error': 'Storage or connection error; read back before retrying'})

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = do_PATCH = do_OPTIONS = manage

    listener = LimitedServer(bind, ManagementHandler)
    return listener
