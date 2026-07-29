#!/usr/bin/env python3
"""Run one browser boot to completion and print its numbers.

Measuring the JIT by watching a tab does not work, and the failure is not
obvious: a browser THROTTLES a hidden page, so a run that looks stalled at
"compiled=352" is often just a backgrounded tab. This launches Chrome with
background throttling disabled, points it at web/bench-b/, and waits for the
page to post its landmarks back (scripts/wasm/serve.py --results).

One run at a time, on an idle machine: an early A/B ran two browser tabs at
once, halved the CPU available to each, and invalidated itself.

    scripts/wasm/bench-run.py --instantiate 50   --label sweep-50
    scripts/wasm/bench-run.py --mode chunked --cold --label chunked-cold
    scripts/wasm/bench-run.py --mode chunked --label chunked-warm
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# Chrome throttles timers, workers and rendering in backgrounded or occluded
# windows. Every one of these matters for an unattended run.
CHROME_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "--autoplay-policy=no-user-gesture-required",
    "--window-size=1000,760",
]


def port_is_busy(port: int) -> bool:
    """Is something already listening?

    Worth checking rather than assuming: a killed run can leave its server
    behind, the new run's bind then loses the race silently, and the RESULTS
    GO TO THE OLD RUN'S FILE. One measurement was read out of the wrong file
    that way before the cause was obvious.
    """
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_for_server(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/__chunk-stats",
                                   timeout=1).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    raise SystemExit(f"server on :{port} never came up")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instantiate", type=int, default=100)
    parser.add_argument("--max", type=int, default=48000)
    parser.add_argument("--build", default="1A543a",
                        help="firmware build: 1A543a (1.0) or 4A102 (1.1.4). "
                             "The security epoch travels with it -- a wrong one "
                             "wedges iBoot with an EMPTY serial log")
    parser.add_argument("--mode", choices=("pack", "chunked"), default="pack")
    parser.add_argument("--cold", action="store_true",
                        help="chunked mode: drop the chunk cache first")
    parser.add_argument("--no-prefetch", action="store_true")
    parser.add_argument("--display", default=None,
                        choices=("none", "wasm"),
                        help="display backend (default: the page's, wasm)")
    parser.add_argument("--read-only", action="store_true",
                        help="do not mark the NAND writable")
    parser.add_argument("--no-sw", action="store_true",
                        help="chunked mode: bypass the service worker and let "
                             "the emulator's XHRs hit the network directly")
    parser.add_argument("--static-threshold", action="store_true",
                        help="pin the compile threshold instead of scaling it "
                             "with cap pressure (adaptive is the default)")
    parser.add_argument("--until", default="launchd",
                        help="stop once this landmark is reached "
                             "(iBoot banner/kernel/BSD root/launchd/SpringBoard)")
    parser.add_argument("--settle", type=float, default=0,
                        help="keep running this many seconds after --until is "
                             "reached; a home screen crossing the threshold is "
                             "not the same as a home screen finished drawing")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, default=Path("/tmp/wasm-bench"))
    parser.add_argument("--profile", default=None,
                        help="Chrome profile directory name; SHARE it between "
                             "a cold and a warm run, or the 'warm' run gets a "
                             "fresh Cache Storage and is cold again")
    parser.add_argument("--headed", action="store_true",
                        help="show the window (default is --headless=new)")
    args = parser.parse_args()

    if not CHROME.exists():
        raise SystemExit(f"Chrome not found at {CHROME}")
    if port_is_busy(args.port):
        raise SystemExit(
            f"something is already listening on :{args.port} -- probably a "
            "server left behind by an interrupted run. Kill it "
            f"(pkill -f 'serve.py --port {args.port}') or pass --port.")
    args.out.mkdir(parents=True, exist_ok=True)
    result_path = args.out / f"{args.label}.json"
    if result_path.exists():
        result_path.unlink()
    profile = args.out / f"profile-{args.profile or args.label}"

    server = subprocess.Popen(
        [sys.executable, str(REPO / "scripts/wasm/serve.py"),
         "--port", str(args.port), "--results", str(result_path),
         "--results-label", args.label],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server(args.port)

        query = (f"?instantiate={args.instantiate}&max={args.max}"
                 f"&mode={args.mode}&label={args.label}&build={args.build}")
        if args.cold:
            query += "&cold=1"
        if args.no_prefetch:
            query += "&prefetch=0"
        if args.static_threshold:
            query += "&adaptive=0"
        if args.no_sw:
            query += "&sw=0"
        if args.display:
            query += f"&display={args.display}"
        if args.read_only:
            query += "&writable=0"
        url = f"http://localhost:{args.port}/bench-b/{query}"

        flags = list(CHROME_FLAGS)
        if not args.headed:
            flags.append("--headless=new")
        chrome = subprocess.Popen(
            [str(CHROME), f"--user-data-dir={profile}", *flags, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

        print(f"[bench] {args.label}: {url}", flush=True)
        started = time.time()
        latest: dict = {}
        last_print = 0.0
        settling = None
        try:
            while time.time() - started < args.timeout:
                time.sleep(2)
                if result_path.exists():
                    try:
                        latest = json.loads(result_path.read_text())
                    except json.JSONDecodeError:
                        continue
                    if time.time() - last_print > 30:
                        last_print = time.time()
                        print(f"  {latest.get('elapsed')}s "
                              f"{latest.get('counters', {}).get('JIT', '')} "
                              f"{list(latest.get('landmarks', {}))}", flush=True)
                    if args.until in latest.get("landmarks", {}):
                        if not args.settle:
                            break
                        if settling is None:
                            settling = time.time()
                            print(f"  reached {args.until}; settling "
                                  f"{args.settle:.0f}s", flush=True)
                        elif time.time() - settling >= args.settle:
                            break
                    if latest.get("failure"):
                        print(f"  failure: {latest['failure']}", flush=True)
                        break
                if chrome.poll() is not None:
                    print("  chrome exited", flush=True)
                    break
        finally:
            try:
                os.killpg(os.getpgid(chrome.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

        try:
            stats = json.loads(urllib.request.urlopen(
                f"http://localhost:{args.port}/__chunk-stats", timeout=2).read())
        except OSError:
            stats = None
        latest["chunkServerStats"] = stats
        latest["label"] = args.label
        latest["wallSeconds"] = round(time.time() - started, 1)
        result_path.write_text(json.dumps(latest, indent=1) + "\n")
        print(json.dumps(latest, indent=1))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
