#!/usr/bin/env python3
"""Serve the browser build with the headers threaded WebAssembly requires.

A `file://` launch cannot work and a plain static server is not enough: the
emulator uses pthreads, so the page must be cross-origin isolated
(SharedArrayBuffer), which means every response needs COOP/COEP.  Getting this
wrong shows up as `crossOriginIsolated === false` and a module that refuses to
instantiate, so the server also has a self-check.

Usage:
    scripts/wasm/serve.py                 # serve web/ on http://localhost:8010
    scripts/wasm/serve.py --port 9000
    scripts/wasm/serve.py --check         # verify headers, then exit

Range requests are supported because the NAND pack is ~300 MB and the loader
fetches it in chunks.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import socketserver
import sys
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ISOLATION_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
}

EXTRA_TYPES = {
    ".wasm": "application/wasm",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".pack": "application/octet-stream",
}

RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static handler with cross-origin isolation and byte-range support."""

    def end_headers(self) -> None:
        for name, value in ISOLATION_HEADERS.items():
            self.send_header(name, value)
        # Development server: never let a stale emulator.wasm survive a rebuild.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def guess_type(self, path):  # noqa: N802 - stdlib API
        suffix = Path(path).suffix
        if suffix in EXTRA_TYPES:
            return EXTRA_TYPES[suffix]
        return super().guess_type(path)

    def send_head(self):
        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        match = RANGE.match(header.strip())
        path = self.translate_path(self.path)
        if not match or not os.path.isfile(path):
            return super().send_head()

        size = os.path.getsize(path)
        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            # Suffix range: the last N bytes.
            start = max(0, size - int(end_text or 0))
            end = size - 1
        if start >= size or start > end:
            self.send_error(416, "Requested Range Not Satisfiable")
            self.send_header("Content-Range", f"bytes */{size}")
            return None
        end = min(end, size - 1)

        handle = open(path, "rb")
        handle.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        # SimpleHTTPRequestHandler.copyfile() would send the whole file, so the
        # bounded copy happens here and send_head returns None to stop it.
        remaining = end - start + 1
        while remaining > 0:
            block = handle.read(min(1 << 20, remaining))
            if not block:
                break
            try:
                self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(block)
        handle.close()
        return None

    def log_message(self, fmt, *fmt_args):  # quieter default log
        sys.stderr.write("  %s\n" % (fmt % fmt_args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def check(port: int) -> int:
    """Fetch one response and confirm the isolation headers really arrive."""
    url = f"http://localhost:{port}/"
    with urllib.request.urlopen(url, timeout=5) as response:
        headers = {k.lower(): v for k, v in response.getheaders()}
    ok = True
    for name, expected in ISOLATION_HEADERS.items():
        actual = headers.get(name.lower())
        status = "ok " if actual == expected else "FAIL"
        if actual != expected:
            ok = False
        print(f"  [{status}] {name}: {actual!r} (want {expected!r})")
    print("cross-origin isolation headers:", "present" if ok else "MISSING")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--root", type=Path, default=REPO / "web")
    parser.add_argument("--check", action="store_true",
                        help="start, verify the headers, print the result, exit")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"web root not found: {root}")

    handler = functools.partial(Handler, directory=str(root))
    with Server(("127.0.0.1", args.port), handler) as server:
        if args.check:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            code = check(args.port)
            server.shutdown()
            raise SystemExit(code)
        print(f"serving {root} on http://localhost:{args.port}/")
        print("cross-origin isolation: COOP=same-origin COEP=require-corp")
        print("stop with Ctrl-C")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
