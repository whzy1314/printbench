import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

from .store import BenchError, Store, directory, read_json

DEFAULT_DATA = Path.home() / '3d-projects/printbench-data'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BenchError(message)


def serve_library(store, args):
    from .server import make_server

    with ExitStack() as stack:
        server = stack.enter_context(make_server(store, args.port, host=args.host))
        receipt: dict[str, str | int] = {'url': f'http://127.0.0.1:{server.server_port}', 'data': str(store.root)}
        options = (args.management_host, args.management_token_file, args.management_cert_file, args.management_key_file)
        if args.management_port is not None or args.management_proxy_ca_file is not None or any(
                value is not None for value in options):
            import ssl
            import threading
            from .management import make_management_server, protected_file

            if args.management_port is None or not all(options[1:]):
                raise BenchError('Management requires an explicit port, protected credential file, TLS certificate and key')
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                with protected_file(args.management_key_file.absolute(), projection=True) as key:
                    tls.load_cert_chain(args.management_cert_file, f'/proc/self/fd/{key.fileno()}')
                if args.management_proxy_ca_file is not None:
                    tls.load_verify_locations(cafile=args.management_proxy_ca_file)
                    tls.verify_mode = ssl.CERT_REQUIRED
            except OSError:
                raise BenchError('Management TLS certificate or protected key unavailable') from None
            listener = stack.enter_context(make_management_server(
                store, args.management_port, args.management_token_file, host=args.management_host, tls=tls))
            worker = threading.Thread(target=listener.serve_forever, daemon=True)
            worker.start()
            stack.callback(worker.join)
            stack.callback(listener.shutdown)
            if args.management_host is None:
                receipt['management_url'] = f'https://localhost:{listener.server_port}'
            else:
                receipt['management_port'] = listener.server_port
        print(json.dumps(receipt), flush=True)
        server.serve_forever()


def main():
    try:
        parser = Parser(description='PrintBench local JSON CLI (no printer actions)')
        parser.add_argument('--data', type=Path, default=DEFAULT_DATA)
        commands = parser.add_subparsers(dest='command', required=True)
        commands.add_parser('list')
        get = commands.add_parser('get')
        get.add_argument('id')
        get.add_argument('--revision')
        publish = commands.add_parser('publish')
        publish.add_argument('--spec', type=Path, required=True)
        publish.add_argument('--source', type=Path, required=True)
        serve = commands.add_parser('serve')
        serve.add_argument('--port', type=int, default=8765)
        serve.add_argument('--host', default=os.environ.get('PRINTBENCH_HOST'),
                           help='canonical DNS hostname to opt in to a network listener')
        serve.add_argument('--management-port', type=int, default=os.environ.get('PRINTBENCH_MANAGEMENT_PORT'))
        serve.add_argument('--management-host', default=os.environ.get('PRINTBENCH_MANAGEMENT_HOST'))
        for option in ('token', 'cert', 'key', 'proxy-ca'):
            serve.add_argument(f'--management-{option}-file', type=Path,
                               default=os.environ.get(f"PRINTBENCH_MANAGEMENT_{option.upper().replace('-', '_')}_FILE"))
        args = parser.parse_args()
        store = Store(args.data)
        if args.command == 'list':
            result = store.list()
        elif args.command == 'get':
            result = store.get(args.id, args.revision)
        elif args.command == 'publish':
            with directory(args.spec.absolute().parent) as fd:
                spec = read_json(fd, args.spec.name)
            result = store.publish(spec, args.source)
        else:
            serve_library(store, args)
            return 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (BenchError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
