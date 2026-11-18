"""Browser reads and optional authenticated management, loopback by default."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .store import BenchError, directory, read_bytes

WEB = Path(__file__).resolve().parent.parent / 'web'
HOSTNAME = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?')
STATIC = {'/':('index.html','text/html; charset=utf-8'),
          '/app.js':('app.js','text/javascript; charset=utf-8'),
          '/viewer.mjs':('viewer.mjs','text/javascript; charset=utf-8'),
          '/style.css':('style.css','text/css; charset=utf-8')}


def address(port, host):
    if type(port) is not int or not 0 <= port <= 65535:
        raise BenchError('Invalid HTTP port')
    if host is not None and (not isinstance(host, str) or len(host) > 253 or not HOSTNAME.fullmatch(host)):
        raise BenchError('Expected a canonical lowercase DNS hostname without a port or trailing dot')
    return '0.0.0.0' if host is not None else '127.0.0.1', port


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data, content_type='application/json; charset=utf-8', extra=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key,value in (extra or {}).items():
            self.send_header(key,value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # No artifact contents or untrusted paths in server logs.


def make_server(store, port=8765, *, host=None, management_token_file=None, management_proof_file=None):
    bind = address(port, host)
    proof = management_proof_file is not None
    if proof and management_token_file is not None:
        raise BenchError('Proof mode and management writes are mutually exclusive')

    class ReadHandler(Handler):

        def do_GET(self):
            hosts = self.headers.get_all('Host')
            port = self.server.server_port
            canonical = host is not None and hosts == [host]
            local = host is None or self.client_address[0] == '127.0.0.1'
            if not (canonical or (local and hosts in ([f'127.0.0.1:{port}'], [f'localhost:{port}']))):
                return self.reply(403,b'{"error":"Unexpected Host"}')
            try:
                path = unquote(urlsplit(self.path).path, errors='strict')
                if path in STATIC:
                    name,content_type = STATIC[path]
                    with directory(WEB) as fd:
                        data = read_bytes(fd,name)
                    return self.reply(200,data,content_type)
                parts = path.strip('/').split('/')
                if parts == ['api','projects']:
                    result = store.list()
                elif len(parts) == 3 and parts[:2] == ['api','projects']:
                    result = store.get(parts[2])
                elif len(parts) == 4 and parts[:2] == ['api','projects'] and parts[3] == 'history':
                    result = store.history(parts[2])
                elif len(parts) == 5 and parts[:2] == ['api','projects'] and parts[3] == 'revisions':
                    result = store.get(parts[2],parts[4])
                elif len(parts) == 7 and parts[:2] == ['api','projects'] and parts[3] == 'revisions' and parts[5] == 'files':
                    name = parts[6]
                    data,entry = store.artifact(parts[2],parts[4],name)
                    content_type = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}.get(Path(name).suffix.lower(),'application/octet-stream')
                    return self.reply(200,data,content_type,{
                        'Content-Disposition':f'attachment; filename="{name}"',
                        'ETag':f'"{entry["sha256"]}"', 'X-Content-SHA256':entry['sha256']})
                else:
                    return self.reply(404,b'{"error":"Not found"}')
                return self.reply(200,json.dumps(result,ensure_ascii=False,allow_nan=False).encode())
            except FileNotFoundError:
                return self.reply(404,b'{"error":"Not found"}')
            except (BenchError, OSError, UnicodeError, ValueError):
                return self.reply(409,b'{"error":"Invalid path, manifest or artifact integrity"}')

        do_HEAD = do_GET

        def do_POST(self):
            self.reply(405,b'{"error":"Read-only HTTP; use the local publish CLI"}',extra={'Allow':'GET, HEAD'})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    if proof or management_token_file is not None:
        from .management import management_handler
        credential = management_proof_file if proof else management_token_file
        return ThreadingHTTPServer(bind, management_handler(ReadHandler, store, credential, host=host, proof=proof))
    return ThreadingHTTPServer(bind, ReadHandler)
