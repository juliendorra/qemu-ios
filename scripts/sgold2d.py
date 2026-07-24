#!/usr/bin/env python3
"""External S-Gold2 (PMB8876) baseband modem for the iPhone-2G machine.

Speaks to the guest's UART1 over a QEMU chardev socket, so the modem's
behavior lives HERE (hot-reloadable JSON rules) instead of in compiled C.
Changing a response hypothesis = edit the rules file; the daemon reloads it
on the fly (mtime watch), no QEMU rebuild, not even a reboot.

Wiring (the machine must NOT attach its built-in C stub):

    IT_M68AP_NO_BASEBAND=1 qemu-system-arm -M iPhone-2G,... \
        -serial file:/tmp/serial.log \
        -serial unix:/tmp/bb.sock,server=on,wait=off ...
    python3 scripts/sgold2d.py --socket /tmp/bb.sock \
        --rules scripts/baseband-rules/stub.json --logs /tmp/bb-logs

Every byte in both directions is recorded three ways under --logs:
  raw.log       timestamped hexdump (ground truth, catches binary handshakes)
  lines.log     human-readable line-level conversation
  events.jsonl  machine-readable stream for diffing runs
  summary.json  per-command counts + every UNMATCHED command (written on exit
                and refreshed after every command) -- this file is what makes
                respond-until-satisfied fast: it lists exactly what the
                driver asked that the ruleset had no specific answer for.

Rules file format (JSON):
    {
      "default": "\r\nOK\r\n",     // reply for unmatched AT lines; null = stay silent
      "non_at": null,               // reply for non-AT lines; null = stay silent
      "rules": [
        {"match": "(?i)^at\\+xdrv=9,1,(\\d+)$",
         "response": "\r\n+XDRV: 9,1,0,{1},NULL\r\n\r\nOK\r\n",
         "delay_ms": 0,                  // optional response delay
         "set": {"nvram_read": "{1}"},   // optional state assignment
         "if": {"var": "x", "equals": "1"},  // optional state guard
         "note": "baseband NVRAM read"},
        ...
      ]
    }
"response" may be a string or a list of strings (sent in order). "{N}" in a
response/set template substitutes regex group N; "{var:name}" substitutes a
state variable. Rules are tried top to bottom; first match wins. A rule with
"response": null matches silently (useful to suppress the default).
--record-only ignores the rules file and never sends a byte.

A top-level "unsolicited" list sends bytes WITHOUT being asked -- essential
because the kernel-era AppleBaseband stalls while writing nothing to uart1,
so only baseband-initiated traffic can poke it:

    "unsolicited": [
        {"at_ms": 2000, "send": "\r\n+XDRV: 4,0\r\n"},           // once
        {"at_ms": 5000, "period_ms": 3000, "send": "\r\nRING\r\n"} // repeat
    ]
"at_ms" counts from daemon connect (~= power-on); timers reset on rules
reload, so a hot edit can re-fire them mid-boot.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from collections import Counter
from pathlib import Path

VAR_RE = re.compile(r"\{var:([A-Za-z_][A-Za-z0-9_]*)\}")
GROUP_RE = re.compile(r"\{(\d+)\}")


class Rules:
    def __init__(self, path: Path | None):
        self.path = path
        self.mtime = 0.0
        self.default: str | None = "\r\nOK\r\n"
        self.non_at: str | None = None
        self.rules: list[dict] = []
        self.unsolicited: list[dict] = []
        if path:
            self.load()

    def load(self) -> None:
        data = json.loads(self.path.read_text())
        rules = []
        for r in data.get("rules", []):
            r = dict(r)
            r["_re"] = re.compile(r["match"])
            rules.append(r)
        # Swap in atomically only after everything parsed.
        self.default = data.get("default", "\r\nOK\r\n")
        self.non_at = data.get("non_at")
        self.rules = rules
        self.unsolicited = list(data.get("unsolicited", []))
        self.mtime = self.path.stat().st_mtime

    def maybe_reload(self) -> bool:
        if not self.path:
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        try:
            self.load()
            return True
        except (json.JSONDecodeError, re.error, KeyError) as e:
            print(f"sgold2d: rules reload FAILED, keeping old rules: {e}",
                  file=sys.stderr, flush=True)
            self.mtime = mtime  # don't retry a broken file every loop
            return False


class Trace:
    def __init__(self, logs: Path):
        logs.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.raw = (logs / "raw.log").open("a")
        self.lines = (logs / "lines.log").open("a")
        self.events = (logs / "events.jsonl").open("a")
        self.summary_path = logs / "summary.json"

    def ts(self) -> float:
        return round(time.monotonic() - self.t0, 6)

    def raw_bytes(self, direction: str, data: bytes) -> None:
        ts = self.ts()
        hexs = " ".join(f"{b:02x}" for b in data)
        asc = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in data)
        self.raw.write(f"[{ts:12.6f}] {direction} {hexs}  |{asc}|\n")
        self.raw.flush()
        self.events.write(json.dumps(
            {"t": ts, "dir": direction, "hex": hexs, "ascii": asc}) + "\n")
        self.events.flush()

    def line(self, direction: str, text: str, note: str = "") -> None:
        suffix = f"   # {note}" if note else ""
        self.lines.write(f"[{self.ts():12.6f}] {direction} {text!r}{suffix}\n")
        self.lines.flush()

    def write_summary(self, summary: dict) -> None:
        self.summary_path.write_text(json.dumps(summary, indent=2) + "\n")


def render(template: str, m: re.Match | None, state: dict) -> str:
    out = template
    if m:
        out = GROUP_RE.sub(lambda g: m.group(int(g.group(1))) or "", out)
    out = VAR_RE.sub(lambda g: state.get(g.group(1), ""), out)
    return out


class Modem:
    def __init__(self, rules: Rules, trace: Trace, record_only: bool):
        self.rules = rules
        self.trace = trace
        self.record_only = record_only
        self.state: dict[str, str] = {}
        self.linebuf = bytearray()
        self.cmd_counts: Counter[str] = Counter()
        self.unmatched: Counter[str] = Counter()
        self.non_at_lines: Counter[str] = Counter()
        self.pending_out: list[tuple[float, bytes]] = []  # (send_at, data)
        self.unsched: list[dict] = []
        self.arm_unsolicited()

    def arm_unsolicited(self) -> None:
        now = time.monotonic()
        self.unsched = [{"due": now + u.get("at_ms", 0) / 1000.0,
                         "period_ms": u.get("period_ms"),
                         "send": u["send"]}
                        for u in self.rules.unsolicited]

    def tick_unsolicited(self) -> None:
        if self.record_only:
            return
        now = time.monotonic()
        for u in self.unsched[:]:
            if u["due"] <= now:
                self.trace.line("<-", u["send"], "unsolicited")
                self.queue(u["send"])
                if u["period_ms"]:
                    u["due"] = now + u["period_ms"] / 1000.0
                else:
                    self.unsched.remove(u)

    # -- summary ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "record_only": self.record_only,
            "rules_file": str(self.rules.path) if self.rules.path else None,
            "commands_seen": dict(self.cmd_counts.most_common()),
            "unmatched_commands": dict(self.unmatched.most_common()),
            "non_at_lines": dict(self.non_at_lines.most_common()),
        }

    def flush_summary(self) -> None:
        self.trace.write_summary(self.summary())

    # -- output ----------------------------------------------------------
    def queue(self, text: str, delay_ms: float = 0.0) -> None:
        if self.record_only or text is None:
            return
        send_at = time.monotonic() + delay_ms / 1000.0
        self.pending_out.append((send_at, text.encode("latin-1")))

    def take_ready_output(self) -> bytes:
        now = time.monotonic()
        out = b""
        rest = []
        for send_at, data in self.pending_out:
            if send_at <= now:
                out += data
            else:
                rest.append((send_at, data))
        self.pending_out = rest
        return out

    def next_deadline(self) -> float | None:
        if not self.pending_out:
            return None
        return min(t for t, _ in self.pending_out)

    # -- input -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self.trace.raw_bytes("->", data)
        for b in data:
            if b in (0x0D, 0x0A):
                if self.linebuf:
                    self.handle_line(self.linebuf.decode("latin-1"))
                    self.linebuf.clear()
            else:
                self.linebuf.append(b)
                if len(self.linebuf) > 4096:  # runaway binary stream guard
                    self.handle_line(self.linebuf.decode("latin-1"))
                    self.linebuf.clear()

    def handle_line(self, line: str) -> None:
        is_at = line[:2].lower() == "at"
        if not is_at:
            self.non_at_lines[line] += 1
            self.trace.line("->", line, "non-AT")
            if self.rules.non_at:
                self.respond(self.rules.non_at, None, "non_at handler")
            self.flush_summary()
            return

        self.cmd_counts[line] += 1
        for rule in self.rules.rules:
            m = rule["_re"].search(line)
            if not m:
                continue
            guard = rule.get("if")
            if guard and self.state.get(guard["var"]) != guard["equals"]:
                continue
            for var, tmpl in (rule.get("set") or {}).items():
                self.state[var] = render(tmpl, m, self.state)
            note = rule.get("note", rule["match"])
            self.trace.line("->", line, f"matched: {note}")
            resp = rule.get("response")
            if resp is not None:
                responses = resp if isinstance(resp, list) else [resp]
                for r in responses:
                    self.respond(r, m, note, rule.get("delay_ms", 0))
            self.flush_summary()
            return

        # No rule matched.
        self.unmatched[line] += 1
        self.trace.line("->", line, "UNMATCHED")
        if self.rules.default is not None:
            self.respond(self.rules.default, None, "default")
        self.flush_summary()

    def respond(self, template: str, m: re.Match | None, note: str,
                delay_ms: float = 0.0) -> None:
        if self.record_only:
            return
        text = render(template, m, self.state)
        self.trace.line("<-", text, note)
        self.queue(text, delay_ms)


def connect(path: str, timeout_s: float) -> socket.socket:
    deadline = time.monotonic() + timeout_s
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", required=True,
                    help="QEMU chardev unix socket for uart1 (QEMU is server)")
    ap.add_argument("--rules", type=Path,
                    help="JSON ruleset; hot-reloaded when its mtime changes")
    ap.add_argument("--record-only", action="store_true",
                    help="never send a byte; pure capture")
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--connect-timeout", type=float, default=60.0)
    args = ap.parse_args()

    if not args.record_only and not args.rules:
        ap.error("--rules is required unless --record-only")

    trace = Trace(args.logs)
    rules = Rules(args.rules if args.rules else None)
    modem = Modem(rules, trace, args.record_only)
    modem.flush_summary()

    sock = connect(args.socket, args.connect_timeout)
    print(f"sgold2d: connected to {args.socket}"
          f" ({'record-only' if args.record_only else args.rules})",
          flush=True)

    try:
        while True:
            deadline = modem.next_deadline()
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
            timeout = 0.5 if timeout is None else min(timeout, 0.5)
            sock.settimeout(timeout)
            try:
                data = sock.recv(4096)
                if not data:
                    print("sgold2d: QEMU closed the socket", flush=True)
                    break
                modem.feed(data)
            except socket.timeout:
                pass
            modem.tick_unsolicited()
            out = modem.take_ready_output()
            if out:
                trace.raw_bytes("<-", out)
                sock.sendall(out)
            if rules.maybe_reload():
                print("sgold2d: rules reloaded", flush=True)
                trace.line("--", f"rules reloaded from {rules.path}")
                modem.arm_unsolicited()
    except KeyboardInterrupt:
        pass
    finally:
        # Flush any trailing partial line into the trace so nothing is lost.
        if modem.linebuf:
            modem.handle_line(modem.linebuf.decode("latin-1"))
            modem.linebuf.clear()
        modem.flush_summary()
        sock.close()
    print(json.dumps(modem.summary(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
