"""CLI for the hosted PrintBench read and management APIs (HTTPS only)."""
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urlsplit

from .__main__ import Parser
from .management import MAX_ARTIFACT_BYTES, PREFIX, read_token
from .store import (BenchError, MAX_FILE, MAX_JSON, VERSION, directory, filename,
                    identifier, read_bytes, read_json, validate_spec, write_bytes)

READ_PREFIX = '/api/projects'


def request(args, path, *, method='GET', headers=None, body=None, limit=MAX_JSON * 4):
    url = urlsplit(args.url)
    if (url.scheme != 'https' or url.hostname is None or url.username is not None or url.password is not None
            or url.path not in ('', '/') or url.query or url.fragment
            or re.search(r'[\x00-\x20\x7f\\?#]', args.url)):
        raise BenchError('Expected an approved HTTPS origin, without credentials, path, query or fragment')
    headers = {'Cache-Control': 'no-store', **(headers or {})}
    if path.startswith(PREFIX):
        if args.token_file is None:
            raise BenchError('Management requires a protected credential file')
        try:
            token = read_token(args.token_file.absolute())
        except OSError:
            raise BenchError('Protected credential file unavailable') from None
        headers['Authorization'] = 'Bearer ' + token
    tls = ssl.create_default_context(cafile=args.ca_file)
    with_connection = HTTPSConnection(url.hostname, url.port, timeout=65, context=tls)
    try:
        with_connection.request(method, path, body=body, headers=headers)
        response = with_connection.getresponse()
        if response.status != (201 if method == 'PUT' else 200):
            raise BenchError(f'PrintBench HTTP {response.status}; no retry was sent. Read back before another write.')
        data = response.read(limit + 1)
        if len(data) > limit:
            raise BenchError('Response exceeds the client limit')
        return data, response.getheader('ETag')
    finally:
        with_connection.close()


def upload(args):
    with directory(args.spec.absolute().parent) as source:
        spec = read_json(source, args.spec.name)
    validate_spec(spec)
    files = []
    total = 0
    with directory(args.source) as source:
        for name in spec['artifacts']:
            data = read_bytes(source, name, min(MAX_FILE, MAX_ARTIFACT_BYTES - total))
            total += len(data)
            files.append(data)
    meta = json.dumps({'spec': spec, 'sizes': list(map(len, files))}, ensure_ascii=False, allow_nan=False).encode()
    if len(meta) > MAX_JSON:
        raise BenchError('Upload metadata exceeds 1 MiB')
    return spec['id'], b''.join([len(meta).to_bytes(4, 'big'), meta, *files])


def main():
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--url', required=True, help='approved HTTPS origin')
        parser.add_argument('--token-file', type=Path, help='owner-only credential file (management only)')
        parser.add_argument('--ca-file', help='optional approved CA bundle; TLS verification is mandatory')
        commands = parser.add_subparsers(dest='command', required=True)
        commands.add_parser('list')
        for name in ('get', 'history', 'download', 'state', 'archive', 'restore'):
            command = commands.add_parser(name)
            command.add_argument('project')
            if name in ('get', 'download'):
                command.add_argument('--revision', required=name == 'download')
            if name == 'download':
                command.add_argument('name')
                command.add_argument('--output', type=Path, required=True)
            if name in ('archive', 'restore'):
                command.add_argument('--match', required=True, help='exact quoted ETag from state')
        for name in ('create', 'append'):
            command = commands.add_parser(name)
            command.add_argument('--spec', type=Path, required=True)
            command.add_argument('--source', type=Path, required=True)
            if name == 'append':
                command.add_argument('--match', required=True, help='exact quoted ETag from state')
        args = parser.parse_args()
        operation = args.command
        match = getattr(args, 'match', None)
        if match is not None and (len(match) != 34 or match[0] != '"' or match[-1] != '"'
                                  or not VERSION.fullmatch(match[1:-1])):
            raise BenchError('Expected the exact quoted ETag from management state')
        precondition = {'If-Match': match} if match else {'If-None-Match': '*'}
        if operation in ('create', 'append'):
            project, body = upload(args)
            data, _ = request(args, PREFIX + project, method='PUT', body=body,
                              headers={'Content-Type': 'application/octet-stream', **precondition})
            result = json.loads(data)
        elif operation in ('state', 'archive', 'restore'):
            path = PREFIX + identifier(args.project) + '/' + operation
            if operation == 'state':
                data, tag = request(args, path)
            else:
                data, tag = request(args, path, method='POST', body=b'', headers=precondition)
            if tag is None or not re.fullmatch(r'"[a-f0-9]{32}"', tag):
                raise BenchError('Response has no valid project ETag; read back before writing')
            result = json.loads(data) | {'etag': tag}
        else:
            path = READ_PREFIX
            if operation != 'list':
                path += '/' + identifier(args.project)
            if operation == 'history':
                path += '/history'
            if getattr(args, 'revision', None) is not None:
                path += '/revisions/' + identifier(args.revision)
            data, _ = request(args, path, limit=16 * MAX_JSON if operation == 'list' else MAX_JSON * 2)
            result = json.loads(data)
            if operation == 'download':
                name = filename(args.name)
                entry = next((item for item in result['artifacts'] if item['name'] == name), None)
                if entry is None:
                    raise BenchError('Artifact is not in the revision manifest')
                data, _ = request(args, f'{path}/files/{name}', limit=MAX_FILE)
                sha = hashlib.sha256(data).hexdigest()
                if len(data) != entry['bytes'] or sha != entry['sha256']:
                    raise BenchError('Download does not match the revision manifest')
                with directory(args.output.absolute().parent) as parent:
                    write_bytes(parent, args.output.name, data)
                result = {'bytes': len(data), 'sha256': sha}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except BenchError as exc:
        error = str(exc)
    except (OSError, HTTPException, ValueError, TypeError, KeyError, RecursionError):
        error = 'PrintBench request or local I/O failed; no retry was sent. Verify TLS, paths and current state.'
    print(json.dumps({'error': error}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
