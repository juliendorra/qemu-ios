#!/usr/bin/env python3
"""Compare ordered iPhone OS 1.x startup events from N45AP and M68AP serial logs."""

from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path


START_RE = re.compile(r"([A-Za-z0-9_]+)::start(?:\(([^)]*)\))?")
CONFIG_RE = re.compile(r"config\([^)]*\): (starting|stalling) on ([^,\r\n]+)")
REGISTER_RE = re.compile(r"Registering:\s*(.+)")
FOCUS_RE = re.compile(
    r"GPIO|USB|SDIO|Baseband|AppleS5L8900XIO|IOPMrootDomain|"
    r"AppleNANDFTL|IOFlashBlock|disk0s1|launchd|SpringBoard",
    re.IGNORECASE,
)


def parse_serial(path: Path) -> dict:
    text = path.read_bytes().decode("utf-8", errors="replace").replace("\r", "\n")
    events = []
    starts = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = START_RE.search(line)
        if match:
            service, provider = match.groups()
            event = {
                "line": line_no,
                "kind": "service_start",
                "service": service,
                "provider": provider,
                "failed": line.endswith("failed"),
                "text": line,
            }
            starts.append(service)
            events.append(event)
            continue
        match = CONFIG_RE.search(line)
        if match:
            action, provider = match.groups()
            events.append({
                "line": line_no,
                "kind": f"config_{action}",
                "provider": provider,
                "text": line,
            })
            continue
        match = REGISTER_RE.search(line)
        if match:
            events.append({
                "line": line_no,
                "kind": "register",
                "path": match.group(1),
                "text": line,
            })

    milestones = {
        "root": "BSD root: disk0s1" in text,
        "launchd": "launchd[1]: BOOT_TIME" in text,
        "springboard": "Configuring SpringBoard for" in text,
        "panic": "panic(cpu" in text or "Debugger message: Fatal Exception" in text,
    }
    focus = [event for event in events if FOCUS_RE.search(event["text"])]
    return {
        "path": str(path),
        "milestones": milestones,
        "event_count": len(events),
        "service_start_count": len(starts),
        "service_starts": starts,
        "focus_events": focus,
    }


def compare(n45: dict, m68: dict) -> dict:
    left = n45["service_starts"]
    right = m68["service_starts"]
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    matching = []
    differences = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matching.append({
                "n45_range": [i1, i2],
                "m68_range": [j1, j2],
                "services": left[i1:i2],
            })
        else:
            differences.append({
                "kind": tag,
                "n45_range": [i1, i2],
                "m68_range": [j1, j2],
                "n45_services": left[i1:i2],
                "m68_services": right[j1:j2],
            })
    return {
        "shared_start_count": sum(len(item["services"]) for item in matching),
        "matching_runs": matching,
        "differences": differences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n45-serial", type=Path, required=True)
    parser.add_argument("--m68-serial", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    for path in (args.n45_serial, args.m68_serial):
        if not path.is_file():
            raise SystemExit(f"serial log not found: {path}")

    n45 = parse_serial(args.n45_serial)
    m68 = parse_serial(args.m68_serial)
    result = {"n45ap": n45, "m68ap": m68, "comparison": compare(n45, m68)}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
