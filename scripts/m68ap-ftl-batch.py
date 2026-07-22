#!/usr/bin/env python3
"""Run bounded M68AP FTL traces until one reaches an FTL_Open verdict."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TRACE = REPO / "scripts" / "m68ap-ftl-trace.py"
TERMINAL = {"FTL_OPEN_SUCCESS", "FTL_RESTORE_FALLBACK"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nand", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    args, forwarded = parser.parse_known_args()
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    if args.logs.exists() and any(args.logs.iterdir()):
        raise SystemExit(f"refusing non-empty batch directory: {args.logs}")
    args.logs.mkdir(parents=True, exist_ok=True)

    attempts = []
    selected = None
    for number in range(1, args.attempts + 1):
        attempt_dir = args.logs / f"attempt-{number:02d}"
        command = [
            sys.executable, "-B", str(TRACE), "--nand", str(args.nand),
            "--logs", str(attempt_dir), "--timeout", str(args.timeout),
            *forwarded,
        ]
        completed = subprocess.run(command, cwd=REPO, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        result_path = attempt_dir / "result.json"
        if not result_path.is_file():
            attempts.append({"attempt": number, "status": "NO_REPORT",
                             "exit_code": completed.returncode})
            continue
        result = json.loads(result_path.read_text())
        item = {"attempt": number, "status": result["status"],
                "exit_code": completed.returncode,
                "elapsed_seconds": result["elapsed_seconds"],
                "report": str(result_path)}
        attempts.append(item)
        print(json.dumps(item), flush=True)
        if result["status"] in TERMINAL:
            selected = result
            break

    summary = {
        "status": selected["status"] if selected else "NO_FTL_VERDICT",
        "attempts": attempts,
        "selected_report": (selected["logs"]["directory"] + "/result.json"
                            if selected else None),
        "ftl_context_edges": (selected["trace"]["ftl_context_edges"]
                              if selected else []),
        "nand_read_tail": (selected["nand_reads"]["tail"][-32:]
                           if selected else []),
    }
    output = args.logs / "batch-result.json"
    output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"report: {output}")
    return 0 if selected else 2


if __name__ == "__main__":
    sys.exit(main())
