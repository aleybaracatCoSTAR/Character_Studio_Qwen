"""Start the listening experiment on loopback; never download at startup."""
import argparse
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18095)
    parser.add_argument('--store', type=Path, help='Local checkpoint store prepared by download_listening_models.py.')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Choose a local port between 1 and 65535.')
    if args.store is not None:
        os.environ['LISTENING_STORE'] = str(args.store)
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)


if __name__ == '__main__':
    main()
