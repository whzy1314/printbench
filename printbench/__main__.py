import argparse
import json
import os
from pathlib import Path
import sys

from .store import BenchError, Store, directory, read_json

DEFAULT_DATA = Path.home() / '3d-projects/printbench-data'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BenchError(message)


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
            from .server import make_server
            server = make_server(store, args.port, host=args.host)
            print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'data':str(store.root)}), flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
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
