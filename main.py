#!/usr/bin/env python3
"""Billing app launcher.  Run:  python main.py   (then open http://127.0.0.1:8000)"""
import argparse
import os
import threading
import webbrowser
from pathlib import Path

from billing import __version__
from billing.db import Database
from billing.services import seed_demo
from billing.web import make_server


def main():
    parser = argparse.ArgumentParser(description="Simple billing & invoicing app")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1, this computer only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "billing.db"), help="SQLite database file")
    parser.add_argument("--password", default=os.environ.get("BILLING_PASSWORD"),
                        help="require this password (username: admin); also read from $BILLING_PASSWORD")
    parser.add_argument("--demo", action="store_true", help="load sample data into an empty database")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    db = Database(args.db)
    if args.demo:
        with db.session() as conn:
            seed_demo(conn)

    server = make_server(db, args.host, args.port, args.password, args.verbose)
    url = "http://%s:%d" % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, args.port)
    print("Billing app %s running at %s\nDatabase: %s\nPress Ctrl+C to stop." % (__version__, url, args.db))
    if args.host not in ("127.0.0.1", "localhost") and not args.password:
        print("WARNING: listening on the network without a password. Use --password.")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
