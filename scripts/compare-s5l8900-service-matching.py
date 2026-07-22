#!/usr/bin/env python3
"""Compare the first IOIpodUSBDevice service enumeration on N45AP and M68AP."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PROFILE_RE = re.compile(r"kernel_profile=(\w+)")
START_RE = re.compile(r"usb_device_start profile=(\w+) pc=(0x[0-9a-f]+)")
EVENT_RE = re.compile(
    r"service_match event=(\w+) pc=(0x[0-9a-f]+) "
    r"object=(0x[0-9a-f]+) vtable=(0x[0-9a-f]+) "
    r"references=(0x[0-9a-f]+) state=(0x[0-9a-f]+) "
    r"iterator=(0x[0-9a-f]+) iterator_references=(0x[0-9a-f]+)")
PANIC_RE = re.compile(r"pre_ftl_panic=(0x[0-9a-f]+)")


def parse_first_sequence(path: Path) -> dict:
    profile = None
    start = None
    events = []
    panic = None
    active = False
    for line in path.read_text(errors="replace").splitlines():
        if match := PROFILE_RE.search(line):
            profile = match.group(1)
        if match := START_RE.search(line):
            if start is None:
                start = match.group(2)
                active = True
            continue
        if not active:
            continue
        if match := EVENT_RE.search(line):
            event = {
                "event": match.group(1),
                "pc": match.group(2),
                "object": match.group(3),
                "vtable": match.group(4),
                "references": match.group(5),
                "state": match.group(6),
                "iterator": match.group(7),
                "iterator_references": match.group(8),
            }
            events.append(event)
            if event["event"] == "enumeration_return":
                break
        elif match := PANIC_RE.search(line):
            panic = match.group(1)
            break
    if profile is None or start is None or not events:
        raise ValueError(f"{path}: no complete USB service observation found")
    candidates = [item for item in events if item["event"] == "candidate"]
    matched = next((item for item in events if item["event"] == "matched"), None)
    return {
        "path": str(path),
        "profile": profile,
        "usb_device_start": start,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "matched": matched,
        "enumeration_returned": any(
            item["event"] == "enumeration_return" for item in events),
        "panic": panic,
    }


def compare(n45: dict, m68: dict) -> dict:
    paired = min(n45["candidate_count"], m68["candidate_count"])
    divergences = []
    for index in range(paired):
        left = n45["candidates"][index]
        right = m68["candidates"][index]
        if (left["references"], left["state"]) != (
                right["references"], right["state"]):
            divergences.append({
                "ordinal": index + 1,
                "n45ap": {
                    "references": left["references"],
                    "state": left["state"],
                    "vtable": left["vtable"],
                },
                "m68ap": {
                    "references": right["references"],
                    "state": right["state"],
                    "vtable": right["vtable"],
                },
            })
    matched_reference_parity = bool(
        n45["matched"] and m68["matched"] and
        n45["matched"]["references"] == m68["matched"]["references"] and
        n45["matched"]["state"] == m68["matched"]["state"])
    return {
        "n45ap": n45,
        "m68ap": m68,
        "comparison": {
            "paired_candidates": paired,
            "candidate_count_equal": n45["candidate_count"] == m68["candidate_count"],
            "matched_reference_parity": matched_reference_parity,
            "reference_or_state_divergences": divergences,
            "first_divergence": divergences[0] if divergences else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n45-log", type=Path, required=True)
    parser.add_argument("--m68-log", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        result = compare(
            parse_first_sequence(args.n45_log),
            parse_first_sequence(args.m68_log))
        output = json.dumps(result, indent=2) + "\n"
        if args.out:
            args.out.write_text(output)
        print(output, end="")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
