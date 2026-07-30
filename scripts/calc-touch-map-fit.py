#!/usr/bin/env python3
"""Answer the only question the touch map exists to answer.

Feed it one or more `map.json` files from `scripts/calc-touch-map.py`.  For each
axis it fits

    shift(C) = slope * C + intercept

over the probed button centres, and says which of the two explanations the
numbers support:

* a CONSTANT shift (slope ~ 0) is consistent with iPhone OS's own tap-target
  compensation -- UIKit/SpringBoard make targets bigger than the drawn control
  and bias them upward, which a mouse (exact, no finger) reports as a uniform
  error.  That lives in the GUEST and must not be "fixed" here.
* a shift that GROWS with the coordinate (slope != 0) is a SCALE error, and the
  scaling is ours: the model's sensor surface is the only place a scale lives.

The recovered scale is `a = 1 / (1 + slope)`: the factor the guest's coordinate
is multiplied by relative to the click.  `a < 1` means the guest receives a
point closer to the origin than the cursor.

    scripts/calc-touch-map-fit.py /tmp/map10/map.json /tmp/map114/map.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def fit(points):
    """Least squares on (centre, shift).  Returns slope, intercept, resid."""
    pts = [(c, s) for c, s in points if s is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(c for c, _ in pts) / n
    my = sum(s for _, s in pts) / n
    sxx = sum((c - mx) ** 2 for c, _ in pts)
    if sxx == 0:
        return None
    slope = sum((c - mx) * (s - my) for c, s in pts) / sxx
    inter = my - slope * mx
    resid = max(abs(s - (slope * c + inter)) for c, s in pts)
    return slope, inter, resid, n


def report(path: Path) -> None:
    d = json.loads(path.read_text())
    rows = d.get("summary") or []
    print(f"\n=== {path}  (build {d.get('build')}) ===")
    if not rows:
        print("  no summary in this map")
        return
    for axis, ckey, skey in (("x", "cx", "shift_x"), ("y", "cy", "shift_y")):
        pts = [(r[ckey], r[skey]) for r in rows]
        got = [(c, s) for c, s in pts if s is not None]
        print(f"  {axis}: " + ", ".join(f"{c:.0f}->{s:+.1f}" for c, s in got))
        f = fit(pts)
        if not f:
            print("     too few points to separate a constant from a scale")
            continue
        slope, inter, resid, n = f
        # A 1 px binary-search resolution on each of two edges puts ~+-0.5 px of
        # noise on every shift, so a slope is only meaningful if it moves the
        # shift by more than that across the probed span.
        span = max(c for c, _ in got) - min(c for c, _ in got)
        swing = slope * span
        verdict = ("SCALE (grows with position)" if abs(swing) > 2.0
                   else "CONSTANT (no position dependence)")
        print(f"     slope {slope:+.4f}  intercept {inter:+.2f}  "
              f"max resid {resid:.1f}  n={n}")
        print(f"     swing across the probed span ({span:.0f} px): "
              f"{swing:+.1f} px  ->  {verdict}")
        if abs(swing) > 2.0:
            print(f"     implied guest/click scale a = {1/(1+slope):.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("maps", nargs="+", type=Path)
    args = ap.parse_args()
    for p in args.maps:
        report(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
