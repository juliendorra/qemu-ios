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
import json
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

# A content-addressed NAND chunk: <dir>/chunks/<sha256>.
CHUNK_PATH = re.compile(r"/chunks/[0-9a-f]{64}$")
CHUNK_COUNTER = {"requests": 0, "bytes": 0}
RESULT_PATH: Path | None = None
# Diagnostic: serve the stored (still-compressed) chunk bodies WITHOUT the
# Content-Encoding header. The emulator then rejects them for length, but the
# transport either works or does not -- which is what separates "this browser
# will not do a synchronous XHR here" from "it will not do one for a
# content-encoded response".
NO_BROTLI_HEADER = False
COUNTER_LOCK = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static handler with cross-origin isolation and byte-range support."""

    def end_headers(self) -> None:
        for name, value in ISOLATION_HEADERS.items():
            self.send_header(name, value)
        # Development server: never let a stale emulator.wasm survive a
        # rebuild. Chunks are exempt -- they are content-addressed, and send
        # their own immutable Cache-Control.
        if not CHUNK_PATH.search(self.path.split("?", 1)[0]):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def guess_type(self, path):  # noqa: N802 - stdlib API
        suffix = Path(path).suffix
        if suffix in EXTRA_TYPES:
            return EXTRA_TYPES[suffix]
        return super().guess_type(path)

    def do_GET(self):  # noqa: N802 - stdlib API
        """/__chunk-stats reports what this server has actually shipped.

        The independent measurement: the page under test cannot flatter it.
        """
        if self.path.split("?", 1)[0] == "/__chunk-stats":
            with COUNTER_LOCK:
                body = json.dumps(dict(CHUNK_COUNTER)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] == "/__chunk-stats/reset":
            with COUNTER_LOCK:
                CHUNK_COUNTER.update(requests=0, bytes=0)
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802 - stdlib API
        """/__bench-result collects a run's numbers from the page.

        The bench page (web/bench-b/) posts a snapshot at every landmark, so a
        run that is killed on a timeout still leaves usable data behind -- and
        so a sweep can be driven headlessly instead of by watching a tab.
        """
        if self.path.split("?", 1)[0] != "/__bench-result":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if RESULT_PATH:
            RESULT_PATH.write_bytes(body)
        else:
            sys.stderr.write(body.decode("utf-8", "replace") + "\n")
        self.send_response(204)
        self.end_headers()

    def send_head(self):
        chunk = self.send_chunk_head()
        if chunk is not None:
            return chunk

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

    def send_chunk_head(self):
        """Serve a NAND chunk: stored Brotli, declared as Content-Encoding.

        scripts/wasm/chunk-pack.py writes each chunk already compressed, so the
        BROWSER decompresses it on the way in and the emulator carries no
        Brotli decoder. A CDN does exactly this with a pre-compressed object;
        here it is one header.

        Also the byte counter: GET /__chunk-stats reports how much a run
        actually pulled, which is the independent check on "a cold boot downloads
        18.6 MiB, a warm boot downloads nothing" -- independent because it is
        measured by the server rather than by the page under test.
        """
        if not CHUNK_PATH.search(self.path.split("?", 1)[0]):
            return None
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return None

        size = os.path.getsize(path)
        with COUNTER_LOCK:
            CHUNK_COUNTER["requests"] += 1
            CHUNK_COUNTER["bytes"] += size

        handle = open(path, "rb")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if not NO_BROTLI_HEADER:
            self.send_header("Content-Encoding", "br")
        self.send_header("Content-Length", str(size))
        # The page reads this to attribute wire bytes per chunk; Content-Length
        # is not visible to it once the body has been decoded.
        self.send_header("X-Encoded-Length", str(size))
        # Content-addressed: the bytes can never change under this name.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        return handle

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
    parser.add_argument("--no-brotli-header", action="store_true",
                        help="diagnostic: omit Content-Encoding on chunks")
    parser.add_argument("--results", type=Path,
                        help="write POSTs to /__bench-result here "
                             "(scripts/wasm/bench-run.py uses this)")
    parser.add_argument("--check", action="store_true",
                        help="start, verify the headers, print the result, exit")
    args = parser.parse_args()

    global RESULT_PATH, NO_BROTLI_HEADER
    RESULT_PATH = args.results
    NO_BROTLI_HEADER = args.no_brotli_header

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
