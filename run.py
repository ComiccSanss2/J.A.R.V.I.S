"""Start J.A.R.V.I.S. OS:  python run.py  [--ssl] [--port 8765] [--host 0.0.0.0]"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from backend.certs import ensure_cert, local_ips
from backend.config import settings


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows console: avoid errors with non-ASCII characters
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. OS server")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--ssl", action="store_true", default=settings.ssl,
                        help="HTTPS with a self-signed certificate (required for the microphone on other devices)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload (development)")
    args = parser.parse_args()

    ssl_kwargs = {}
    scheme = "http"
    if args.ssl:
        cert, key = ensure_cert(settings.data_dir / "certs")
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        scheme = "https"

    token = f"?token={settings.access_token}" if settings.access_token else ""
    bar = "=" * 62
    print(f"\n{bar}\n  J.A.R.V.I.S. OS  -  central server\n{bar}")
    print(f"  This device       : {scheme}://localhost:{args.port}/{token}")
    for ip in local_ips():
        if ip != "127.0.0.1":
            print(f"  Other devices     : {scheme}://{ip}:{args.port}/{token}")
    if not args.ssl:
        print("  Note: other devices need HTTPS for the microphone -> start with --ssl")
    print(f"{bar}\n")

    uvicorn.run("backend.main:app", host=args.host, port=args.port, reload=args.reload,
                log_level="info", **ssl_kwargs)


if __name__ == "__main__":
    sys.exit(main())
