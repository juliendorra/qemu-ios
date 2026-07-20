#!/usr/bin/env python3
"""Fast QMP-driven Safari/Wi-Fi/HTTPS acceptance harness.

This script connects to an already-running iPod Touch QEMU process.  It keeps
all UI actions in one local process so Auto-Lock and per-tap operator latency
do not dominate a test run.  It saves screenshots and a JSON report suitable
for comparing repeated runs.

Example:
  scripts/ipod-https-acceptance.py \
      --qmp /private/tmp/ipod-https-qmp.sock \
      --serial /private/tmp/ipod-https-serial.log \
      --proxy-log /private/tmp/ipod-https-state/proxy.jsonl \
      --case explicit=https://example.com/
"""

import argparse
import hashlib
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


SCREEN_WIDTH = 320
SCREEN_HEIGHT = 480

ALPHA_KEYS = {
    **dict(zip("qwertyuiop", ((16 + 32 * i, 314) for i in range(10)))),
    **dict(zip("asdfghjkl", ((32 + 32 * i, 356) for i in range(9)))),
    **dict(zip("zxcvbnm", ((64 + 32 * i, 402) for i in range(7)))),
}
SYMBOL_KEYS = {
    **dict(zip("1234567890", ((16 + 32 * i, 295) for i in range(10)))),
    "@": (40, 349),
    "&": (88, 349),
    "%": (136, 349),
    "?": (184, 349),
    ",": (232, 349),
    "=": (280, 349),
    "-": (76, 403),
    ":": (132, 403),
    "+": (244, 403),
    "/": (160, 458),
}
# The URL keyboard keeps the period at the same coordinate on both its alpha
# and symbol layouts.  It does not change layouts, so numeric addresses can
# stay on the symbol keyboard across every separator.
COMMON_KEYS = {
    ".": (108, 458),
}


class QMP:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.buffer = b""
        self.next_id = 0
        self._message()  # greeting
        self.command("qmp_capabilities")

    def _message(self):
        while b"\n" not in self.buffer:
            data = self.socket.recv(65536)
            if not data:
                raise RuntimeError("QMP connection closed")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def command(self, execute, arguments=None):
        self.next_id += 1
        request = {"execute": execute, "id": self.next_id}
        if arguments is not None:
            request["arguments"] = arguments
        self.socket.sendall((json.dumps(request) + "\n").encode())
        while True:
            response = self._message()
            if response.get("id") != self.next_id:
                continue
            if "error" in response:
                raise RuntimeError(f"QMP {execute}: {response['error']}")
            return response.get("return")

    def close(self):
        self.socket.close()


def send_events(qmp, events):
    qmp.command("input-send-event", {"events": events})


def screen_xy(x, y):
    return (
        round(x * 0x7fff / (SCREEN_WIDTH - 1)),
        round(y * 0x7fff / (SCREEN_HEIGHT - 1)),
    )


def tap(qmp, x, y, delay=0.035):
    abs_x, abs_y = screen_xy(x, y)
    send_events(qmp, [
        {"type": "abs", "data": {"axis": "x", "value": abs_x}},
        {"type": "abs", "data": {"axis": "y", "value": abs_y}},
        {"type": "btn", "data": {"button": "left", "down": True}},
    ])
    time.sleep(delay)
    send_events(qmp, [
        {"type": "btn", "data": {"button": "left", "down": False}},
    ])
    time.sleep(delay)


def drag(qmp, start, end, duration=0.35, steps=14):
    def move(point, button=None):
        abs_x, abs_y = screen_xy(*point)
        events = [
            {"type": "abs", "data": {"axis": "x", "value": abs_x}},
            {"type": "abs", "data": {"axis": "y", "value": abs_y}},
        ]
        if button is not None:
            events.append({
                "type": "btn",
                "data": {"button": "left", "down": button},
            })
        send_events(qmp, events)

    move(start, True)
    for index in range(1, steps + 1):
        fraction = index / steps
        move((start[0] + (end[0] - start[0]) * fraction,
              start[1] + (end[1] - start[1]) * fraction))
        time.sleep(duration / steps)
    send_events(qmp, [
        {"type": "btn", "data": {"button": "left", "down": False}},
    ])


def press_button(qmp, qcode):
    for down in (True, False):
        send_events(qmp, [{
            "type": "key",
            "data": {
                "key": {"type": "qcode", "data": qcode},
                "down": down,
            },
        }])
        time.sleep(0.08)


def screenshot(qmp, output_dir, name):
    path = (output_dir / f"{name}.ppm").resolve()
    qmp.command("human-monitor-command", {
        "command-line": f"screendump {path}",
    })
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists():
        raise RuntimeError(f"QEMU did not create {path}")
    data = path.read_bytes()
    keyboard_visible = ppm_keyboard_visible(data)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "black": ppm_is_black(data),
        "keyboard_visible": keyboard_visible,
        "keyboard_layout": (ppm_keyboard_layout(data)
                            if keyboard_visible else None),
        "lock_screen_visible": ppm_lock_screen_visible(data),
        "safari_alert_visible": ppm_safari_alert_visible(data),
        "wifi_chooser_visible": ppm_wifi_chooser_visible(data),
    }


def ppm_is_black(data):
    """Return true when at least 99.5% of a P6 screenshot is near-black."""
    if not data.startswith(b"P6"):
        raise RuntimeError("unexpected screendump format (wanted P6 PPM)")
    position = 2
    tokens = []
    while len(tokens) < 3:
        while position < len(data) and chr(data[position]).isspace():
            position += 1
        if position < len(data) and data[position] == ord("#"):
            position = data.index(b"\n", position) + 1
            continue
        end = position
        while end < len(data) and not chr(data[end]).isspace():
            end += 1
        tokens.append(int(data[position:end]))
        position = end
    width, height, maximum = tokens
    if position >= len(data) or not chr(data[position]).isspace():
        raise RuntimeError("malformed PPM header")
    position += 1
    pixels = data[position:]
    if (width, height, maximum) != (SCREEN_WIDTH, SCREEN_HEIGHT, 255):
        raise RuntimeError(f"unexpected PPM geometry {width}x{height}/{maximum}")
    bright = sum(value > 12 for value in pixels)
    return bright / len(pixels) < 0.005


def ppm_pixel(data, x, y):
    marker = data.find(b"255\n")
    if marker < 0:
        raise RuntimeError("malformed PPM header")
    offset = marker + 4 + (y * SCREEN_WIDTH + x) * 3
    return tuple(data[offset:offset + 3])


def ppm_keyboard_visible(data):
    # Wait for the keyboard's slide-up animation to finish.  Looking only at
    # its first row misclassified intermediate frames and sent taps one row
    # off.  The blue Go key reaches this final coordinate only when settled.
    go_key = ppm_pixel(data, 250, 458)
    return (not ppm_is_black(data) and
            not ppm_lock_screen_visible(data) and
            not ppm_wifi_chooser_visible(data) and
            not ppm_safari_alert_visible(data) and
            go_key[2] > go_key[0] + 80 and go_key[2] > 150)


def ppm_keyboard_layout(data):
    """Distinguish QWERTY's Q from the numeric layout's 1 key."""
    # In the captured iPhone OS 1 URL keyboard, this pixel is the white hole
    # inside Q but the black vertical stroke of 1.  Confirm with a second
    # inverse pixel so a transient or unrelated screen fails loudly.
    q_hole_or_one = sum(ppm_pixel(data, 17, 290))
    q_stroke_or_one_space = sum(ppm_pixel(data, 14, 287))
    if q_hole_or_one > 600 and q_stroke_or_one_space < 90:
        return "alpha"
    if q_hole_or_one < 90 and q_stroke_or_one_space > 600:
        return "symbols"
    raise RuntimeError("could not identify URL keyboard layout")


def ppm_lock_screen_visible(data):
    # The lock screen retains the old black status bar. Safari's status bar is
    # light gray. A fully black sleeping panel is handled separately.
    status = ppm_pixel(data, 100, 10)
    return not ppm_is_black(data) and sum(status) < 90


def ppm_wifi_chooser_visible(data):
    # The chooser can appear either over a page or over the URL keyboard.  Its
    # blue title bar and blue Cancel button are stable; the dimmed background
    # is not, so do not use it for classification.
    title_bar = ppm_pixel(data, 40, 145)
    cancel_edge = ppm_pixel(data, 40, 335)
    cancel_text = ppm_pixel(data, 150, 335)
    return (title_bar[2] > title_bar[0] + 10 and
            40 < min(title_bar) and max(title_bar) < 180 and
            cancel_edge[2] > cancel_edge[0] + 25 and
            sum(cancel_text) > 650)


def ppm_safari_alert_visible(data):
    # Safari alerts start near y=175.  Some iPhone OS 1 alerts dim the page
    # behind them and some (notably "address is invalid") leave it white, so
    # recognize the blue title and the blue-gray action button themselves.
    above = ppm_pixel(data, 40, 145)
    title = ppm_pixel(data, 40, 200)
    button = ppm_pixel(data, 160, 290)
    return (max(above) - min(above) < 8 and
            title[2] > title[0] + 20 and
            button[2] > button[0] + 20 and
            60 < min(button) and max(button) < 210)


def serial_facts(path):
    text = path.read_text(errors="replace") if path and path.exists() else ""
    markers = {
        "mdns_responder_started": "mDNSResponder-118" in text,
        "springboard_started": "Configuring SpringBoard for N45AP" in text,
        "touch_ready": "Touch input ready" in text,
    }
    return {"path": str(path) if path else None, "markers": markers}


def read_jsonl_since(path, offset):
    if not path or not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        stream.seek(offset)
        events = []
        for line in stream:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"event": "invalid-json", "raw": line.rstrip()})
        return events


def file_size(path):
    return path.stat().st_size if path and path.exists() else 0


def wait_for_tls_event(path, offset, timeout, keep_awake=None):
    deadline = time.monotonic() + timeout
    next_keep_awake = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = read_jsonl_since(path, offset)
        established = [event for event in events
                       if event.get("event") == "tls-bridge-established"]
        errors = [event for event in events
                  if event.get("event") in
                  ("tls-bridge-failed", "tls-bridge-error", "error")]
        if established or errors:
            return events
        if keep_awake and time.monotonic() >= next_keep_awake:
            keep_awake()
            next_keep_awake = time.monotonic() + 10
        time.sleep(0.1)
    return read_jsonl_since(path, offset)


def wait_for_named_event(path, offset, event_name, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = read_jsonl_since(path, offset)
        if any(event.get("event") == event_name for event in events):
            return events
        time.sleep(0.1)
    return read_jsonl_since(path, offset)


def set_keyboard(qmp, current, wanted):
    if current != wanted:
        tap(qmp, 40, 458)
        # iPhone OS 1 animates the layout replacement.  A key sent during the
        # transition can land on the old layout, so wait locally once here.
        time.sleep(0.18)
    return wanted


def type_url(qmp, url, initial_layout):
    layout = initial_layout
    for character in url.lower():
        if character in COMMON_KEYS:
            tap(qmp, *COMMON_KEYS[character])
        elif character in ALPHA_KEYS:
            layout = set_keyboard(qmp, layout, "alpha")
            tap(qmp, *ALPHA_KEYS[character])
        elif character in SYMBOL_KEYS:
            layout = set_keyboard(qmp, layout, "symbols")
            tap(qmp, *SYMBOL_KEYS[character])
        else:
            raise ValueError(f"URL character not supported by keyboard map: {character!r}")
    return layout


def prepare_safari(qmp, output_dir, select_wifi, wake_delay):
    """Normalize a locked/running guest into Safari's address editor."""
    before = screenshot(qmp, output_dir, "00-start")

    # Home wakes a sleeping guest.  If already awake it normalizes an open app
    # back to SpringBoard; the unlock drag is harmless on SpringBoard.
    press_button(qmp, "h")
    # A deep-sleep wake reloads the retained LCD/touch path.  Waiting here is
    # faster and much more reliable than retrying individual taps by hand.
    time.sleep(wake_delay)
    drag(qmp, (55, 432), (280, 432))
    time.sleep(1.5)
    tap(qmp, 45, 62)  # Safari icon
    time.sleep(1.2)

    # Fresh Safari opens Bookmarks.  Tapping Done at this position is harmless
    # when the modal is absent.
    tap(qmp, 289, 44)
    time.sleep(0.4)

    ready = None
    for attempt in range(1, 16):
        ready = screenshot(qmp, output_dir, f"prepare-{attempt:02d}")
        if ready["keyboard_visible"]:
            break
        if ready["black"]:
            # Either physical button should wake an iPod, but very old
            # SpringBoard occasionally stops accepting Home after repeated
            # Auto-Lock cycles.  The Power key remains a reliable short wake.
            press_button(qmp, "p")
            time.sleep(wake_delay)
            drag(qmp, (55, 432), (280, 432))
            time.sleep(1.0)
            continue
        if ready["lock_screen_visible"]:
            drag(qmp, (55, 432), (280, 432))
            time.sleep(1.2)
            tap(qmp, 45, 62)  # Safari icon after unlock
            time.sleep(1.2)
            continue
        if ready["wifi_chooser_visible"]:
            if not select_wifi:
                raise RuntimeError(
                    "Wi-Fi chooser visible; rerun with --select-wifi"
                )
            tap(qmp, 150, 210)
            time.sleep(6.0)
            # First-use association can return to Safari's Bookmarks sheet
            # after the earlier optimistic Done tap was covered by chooser.
            tap(qmp, 289, 44)
            time.sleep(0.5)
            continue
        if ready["safari_alert_visible"]:
            tap(qmp, 160, 290)  # OK
            time.sleep(1.0)
            continue
        tap(qmp, 125, 44)  # address field
        time.sleep(0.8)
    if not ready["keyboard_visible"]:
        raise RuntimeError("Safari URL keyboard did not appear")
    return [before, ready]


def clear_address_field(qmp, count=48):
    # Safari normally selects the whole address on focus, making the first tap
    # sufficient. Repetition also handles a retained cursor deterministically.
    for _ in range(count):
        tap(qmp, 300, 402, delay=0.012)


def associate_after_network_failure(qmp, output_dir, initial_screen):
    """Handle the delayed first-use Wi-Fi chooser after a failed load."""
    state = initial_screen
    for attempt in range(1, 9):
        if state["wifi_chooser_visible"]:
            tap(qmp, 150, 210)
            time.sleep(8.0)
            return True
        if state["safari_alert_visible"]:
            tap(qmp, 160, 290)  # OK
        time.sleep(4.0)
        state = screenshot(qmp, output_dir,
                           f"wifi-association-wait-{attempt:02d}")
    return False


def parse_case(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("case must be NAME=URL")
    name, url = value.split("=", 1)
    if not name or not url:
        raise argparse.ArgumentTypeError("case must be NAME=URL")
    return name, url


def parse_case_count(value):
    name, raw_count = parse_case(value)
    try:
        count = int(raw_count)
    except ValueError as error:
        raise argparse.ArgumentTypeError("count must be an integer") from error
    if count < 1:
        raise argparse.ArgumentTypeError("count must be at least 1")
    return name, count


def run_case(qmp, output_dir, proxy_log, http_log, name, url, timeout,
             select_wifi, require_tls, min_tls):
    started = time.monotonic()
    parsed_url = urlparse(url)
    expects_tls = url.lower().startswith("https://") or name in require_tls
    expected_tls_hostname = (parsed_url.hostname
                             if url.lower().startswith("https://") else None)
    expects_bridge_http = (parsed_url.hostname == "10.0.2.2" and
                           (parsed_url.port or 80) == 18080)
    case_log_offset = file_size(proxy_log)
    http_log_offset = file_size(http_log)
    all_events = []
    rendered = None
    for attempt in range(1, 3):
        tap(qmp, 125, 44)
        time.sleep(0.8)
        keyboard = screenshot(
            qmp, output_dir, f"case-{name}-keyboard-{attempt}")
        if not keyboard["keyboard_visible"]:
            raise RuntimeError(f"URL keyboard not visible for case {name}")
        clear_address_field(qmp)
        type_url(qmp, url, keyboard["keyboard_layout"])
        screenshot(qmp, output_dir, f"case-{name}-entered-{attempt}")
        tap(qmp, 278, 458)  # Go

        if expects_tls:
            events = wait_for_tls_event(
                proxy_log, case_log_offset, timeout,
                keep_awake=lambda: tap(qmp, 310, 20),
            )
        elif expects_bridge_http:
            wait_for_named_event(http_log, http_log_offset,
                                 "http-request", timeout)
            events = read_jsonl_since(proxy_log, case_log_offset)
        else:
            time.sleep(min(timeout, 8.0))
            events = read_jsonl_since(proxy_log, case_log_offset)
        http_events = read_jsonl_since(http_log, http_log_offset)
        all_events = events
        # Give old WebKit a short deterministic render window after TLS.
        time.sleep(2.0 if any(e.get("event") == "tls-bridge-established"
                              for e in events) else 0.5)
        # Subresources and parallel connections commonly establish during the
        # render window, after the main-document handshake unblocked the wait.
        events = read_jsonl_since(proxy_log, case_log_offset)
        http_events = read_jsonl_since(http_log, http_log_offset)
        all_events = events
        rendered = screenshot(
            qmp, output_dir, f"case-{name}-rendered-{attempt}")
        if (select_wifi and attempt == 1 and
                (rendered["wifi_chooser_visible"] or
                 rendered["safari_alert_visible"])):
            if associate_after_network_failure(qmp, output_dir, rendered):
                continue
        break
    tls_established = [event for event in all_events
                       if (event.get("event") == "tls-bridge-established" and
                           (expected_tls_hostname is None or
                            event.get("hostname") == expected_tls_hostname))]
    visually_rendered = not any((
        rendered["black"],
        rendered["keyboard_visible"],
        rendered["lock_screen_visible"],
        rendered["safari_alert_visible"],
        rendered["wifi_chooser_visible"],
    ))
    bridge_requests = [event for event in http_events
                       if (event.get("event") == "http-request" and
                           event.get("path") == (parsed_url.path or "/"))]
    if expects_tls:
        passed = len(tls_established) >= min_tls.get(name, 1)
    elif expects_bridge_http:
        passed = visually_rendered and bool(bridge_requests)
    else:
        passed = visually_rendered
    return {
        "name": name,
        "url": url,
        "expects_tls": expects_tls,
        "minimum_tls_establishments": min_tls.get(name, 1),
        "passed": passed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "proxy_events": all_events,
        "http_events": http_events,
        "rendered_screen": rendered,
    }


def sleep_wake_check(qmp, output_dir):
    press_button(qmp, "p")
    time.sleep(3.0)
    asleep = screenshot(qmp, output_dir, "regression-sleep")
    press_button(qmp, "h")
    time.sleep(1.0)
    awake = screenshot(qmp, output_dir, "regression-wake")
    return {
        "passed": asleep["black"] and not awake["black"],
        "sleep_screen": asleep,
        "wake_screen": awake,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmp", type=Path,
                        default=Path("/private/tmp/ipod-https-qmp.sock"))
    parser.add_argument("--serial", type=Path,
                        default=Path("/private/tmp/ipod-https-serial.log"))
    parser.add_argument("--proxy-log", type=Path, required=True)
    parser.add_argument("--http-log", type=Path,
                        help="metadata-only JSONL from ipod-http-bridge.py")
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/private/tmp/ipod-https-acceptance"))
    parser.add_argument("--case", action="append", type=parse_case,
                        default=[] , metavar="NAME=URL")
    parser.add_argument("--require-tls", action="append", default=[],
                        metavar="CASE",
                        help="require a TLS bridge event for an HTTP case")
    parser.add_argument("--min-tls", action="append", type=parse_case_count,
                        default=[], metavar="CASE=COUNT",
                        help="minimum established TLS sessions for a case")
    parser.add_argument("--select-wifi", action="store_true",
                        help="select the first-use Wi-Fi chooser row")
    parser.add_argument("--skip-prepare", action="store_true",
                        help="assume Safari's URL keyboard is already visible")
    parser.add_argument("--sleep-wake", action="store_true",
                        help="run the optional black-screen sleep/wake check")
    parser.add_argument("--keyboard-diagnostics", action="store_true",
                        help="capture alpha and symbol URL keyboard layouts")
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="seconds to wait for each proxy result")
    parser.add_argument("--wake-delay", type=float, default=5.0,
                        help="seconds for retained touch input after wake")
    args = parser.parse_args()

    if not args.case and not args.keyboard_diagnostics:
        args.case = [("explicit-https", "https://example.com/")]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "started_unix": time.time(),
        "qmp": str(args.qmp),
        "proxy_log": str(args.proxy_log),
        "http_log": str(args.http_log) if args.http_log else None,
        "serial": serial_facts(args.serial),
        "screens": [],
        "cases": [],
    }

    qmp = QMP(args.qmp)
    try:
        if not args.skip_prepare:
            report["screens"] = prepare_safari(
                qmp, args.output_dir, args.select_wifi, args.wake_delay)
        if args.keyboard_diagnostics:
            report["keyboard_diagnostics"] = [
                screenshot(qmp, args.output_dir, "keyboard-alpha"),
            ]
            tap(qmp, 40, 458)  # @123
            time.sleep(0.5)
            report["keyboard_diagnostics"].append(
                screenshot(qmp, args.output_dir, "keyboard-symbols"))
            tap(qmp, *SYMBOL_KEYS[":"])
            time.sleep(0.5)
            report["keyboard_diagnostics"].append(
                screenshot(qmp, args.output_dir, "keyboard-after-colon"))
        for name, url in args.case:
            min_tls = dict(args.min_tls)
            result = run_case(qmp, args.output_dir, args.proxy_log,
                              args.http_log, name, url, args.timeout,
                              args.select_wifi, set(args.require_tls), min_tls)
            report["cases"].append(result)
            print(f"{name}: {'PASS' if result['passed'] else 'FAIL'} "
                  f"({result['elapsed_seconds']}s)", flush=True)
        if args.sleep_wake:
            report["sleep_wake"] = sleep_wake_check(qmp, args.output_dir)
    finally:
        qmp.close()

    report["finished_unix"] = time.time()
    report["passed"] = (
        all(case["passed"] for case in report["cases"])
        and (not args.sleep_wake or report["sleep_wake"]["passed"])
    )
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"report: {report_path}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
