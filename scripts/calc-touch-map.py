#!/usr/bin/env python3
"""Measure WHERE a tap actually lands, using Calculator as the readout.

Every other touch test in this tree answers a yes/no question -- did an app
launch, did the screen change -- with a settle window long enough to be unsure
about.  Calculator answers a *precise* one: press a digit and that digit appears
in the display.  So the display is a per-tap oracle for "which button did the
guest think I pressed", and the hit-box boundary can be located by binary search
instead of guessed.

That matters because the reported touch shift is NOT uniform across the screen
on 1.1.4, so a single offset cannot describe it and a map is needed.

What it does
------------
1. restores a snapshot (fast, identical machine state every run) or boots, then
   launches Calculator by tapping its home-screen icon;
2. profiles the button grid from the real framebuffer, so the VISUAL geometry is
   measured rather than assumed;
3. calibrates a display fingerprint for each probe digit by tapping its centre;
4. binary-searches the left/right/top/bottom edge of that button's ACTUAL hit
   box, clearing with `c` between taps;
5. prints measured-minus-drawn for each edge, per button.

Reading the output
------------------
A positive `dL`/`dR`/`dT`/`dB` means that edge of the HIT box sits to the
right/below the drawn edge, i.e. a tap must be further right/down than it looks
-- equivalently the guest receives a point LEFT of / ABOVE the cursor.

The two numbers that matter are derived per button:

    shift = (dL + dR) / 2      how far the hit box CENTRE moved
    slop  = (dR - dL) / 2      how much LARGER than drawn the hit box is

`slop` is the guest's own tap-target expansion (UIKit/SpringBoard hit-testing
deliberately makes targets bigger than the drawn control, and biases them
upward, to compensate for the finger).  It is not ours and must not be
"fixed".  `shift` is the part that can be a coordinate error -- and a CONSTANT
shift across the screen is still plausibly design compensation, whereas a shift
that GROWS with distance from the origin is a scale error and is ours.

    scripts/calc-touch-map.py --build 1A543a \\
        --snapshot web/public/jit-boot/snapshots/1A543a/state --logs /tmp/map10

`0` is deliberately not probe-able: a cleared display already reads `0`, so it
is indistinguishable from "the tap hit nothing".
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402

FB_W, FB_H = 320, 480

# The Calculator display: a light strip under the status bar.  The keypad is
# everything below it.  Both were measured off a real framebuffer, not assumed.
DISPLAY_ROWS = (24, 100)
KEYPAD_ROWS = (110, 478)

# How much of the display may differ and still count as "cleared", in percent
# of bytes.  A digit changes thousands of pixels (measured: 1-3% of the region);
# stray single-pixel churn at the bevel changes ~0.001%.
CLEAR_TOL = 0.05

# Calculator's keypad is 4 columns x 5 rows.  Labels are only used to name the
# probe buttons and to know which ones change the display.
KEY_LABELS = [
    ["m+", "m-", "mrc", "div"],
    ["7", "8", "9", "mul"],
    ["4", "5", "6", "add"],
    ["1", "2", "3", "sub"],
    ["0", ".", "c", "eq"],
]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load("fb_snapshot", REPO / "scripts" / "fb-snapshot.py")
QMP = fb.QMP


def ppm_pixels(path: Path) -> bytes:
    d = path.read_bytes()
    return d[d.index(b"255\n") + 4:]


class Machine:
    def __init__(self, qemu, machine, nor, logs, sock, incoming=None,
                 extra_env=None):
        cmd = [str(qemu), "-M", machine, "-m", "1G", "-pflash", str(nor),
               "-L", "/Applications/iPod Touch.app/Contents/Resources/pc-bios",
               "-display", "none", "-serial", f"file:{logs / 'serial.log'}",
               # -icount is not optional: without it the guest takes timeout
               # paths and nothing measured here is trustworthy.
               "-icount", "shift=1", "-net", "none",
               "-qmp", f"unix:{sock},server,nowait"]
        if incoming:
            cmd += ["-incoming", f"file:{incoming}"]
        env = dict(os.environ)
        env.update(extra_env or {})
        self.logs = logs
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=(logs / "stderr.log").open("wb"),
                                     stderr=subprocess.STDOUT)
        self.q = QMP(sock, wait=60)
        if incoming:
            self.q.execute("cont")
        self.taps = 0

    def status(self) -> str:
        return self.q.execute("query-status")["return"]["status"]

    def shot(self, tag="s") -> bytes:
        p = self.logs / f"{tag}.ppm"
        self.q.execute("screendump", filename=str(p))
        return ppm_pixels(p)

    def tap(self, px, py, hold=0.25, settle=0.0):
        # Never tap a machine whose vCPUs are stopped: the guest auto-locks and
        # the PMU parks it, and a tap then reaches nothing while looking exactly
        # like a device fault.
        st = self.status()
        if st != "running":
            raise SystemExit(f"machine not running before a tap ({st}) -- "
                             "every result after this point would be a lie")
        self.q.execute("input-send-event", events=[
            {"type": "abs", "data": {"axis": "x",
                                     "value": int(px / FB_W * 32768)}},
            {"type": "abs", "data": {"axis": "y",
                                     "value": int(py / FB_H * 32768)}},
            {"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(hold)
        self.q.execute("input-send-event", events=[
            {"type": "btn", "data": {"down": False, "button": "left"}}])
        self.taps += 1
        if settle:
            time.sleep(settle)

    def nonblack(self, px: bytes) -> float:
        n = sum(1 for i in range(0, len(px), 3)
                if px[i] or px[i + 1] or px[i + 2])
        return 100.0 * n / (FB_W * FB_H)

    def wait_live(self, timeout: float, pct: float = 40.0,
                  poll: float = 8.0) -> float:
        """Proceed the MOMENT the panel is live, rather than after a fixed wait.

        A fixed boot-wait is how every earlier probe here ended up measuring a
        sleeping device: 1.1.4 reaches its home screen at ~250 s and auto-locks
        shortly after, and once the Merlot panel has slept, Home alone does not
        bring it back (it comes up on the lock screen, which then wants a
        slide).  Polling removes the race instead of tuning around it -- and the
        map itself taps continuously, so nothing sleeps once it starts.

        Requires two consecutive live samples: a boot flashes bright frames on
        the way past.
        """
        deadline = time.time() + timeout
        good = 0
        live = 0.0
        while time.time() < deadline:
            live = self.nonblack(self.shot("wake"))
            good = good + 1 if live >= pct else 0
            if good >= 2:
                return live
            time.sleep(poll)
        return live

    def ensure_awake(self, tries=4) -> float:
        """Wake the panel if the guest auto-locked, before anything is tapped.

        A long boot-wait routinely lands on a device that has already
        auto-locked: the PMU powers the panel off and can park the vCPUs, and a
        tap then reaches nothing while looking exactly like a device fault.
        Pressing Home is what a user does.  `h` is the key binding; the qcode
        `home` is silently discarded.
        """
        for _ in range(tries):
            live = self.nonblack(self.shot("wake"))
            if self.status() == "running" and live > 10.0:
                return live
            self.q.execute("send-key", keys=[{"type": "qcode", "data": "h"}])
            time.sleep(6.0)
        return self.nonblack(self.shot("wake"))

    def close(self):
        try:
            self.q.close()
        except Exception:                                   # noqa: BLE001
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------- geometry ---

def _lum(px: bytes, x: int, y: int) -> float:
    o = (y * FB_W + x) * 3
    return (px[o] + px[o + 1] + px[o + 2]) / 3.0


def _profile(px, along, lo, hi, frm, to):
    """Mean luminance of each row (`along='row'`) or column in a band."""
    out = []
    for a in range(frm, to):
        t = 0.0
        for b in range(lo, hi):
            x, y = (a, b) if along == "col" else (b, a)
            t += _lum(px, x, y)
        out.append(t / (hi - lo))
    return out


def _runs(vals, frm, floor_frac=0.10, min_len=20):
    """Contiguous runs standing above the LOCAL background.

    Two passes, and the second one is not optional.  The keypad background is a
    dark leather texture that brightens down the panel, and the button rows are
    not equally bright -- row 1 (m+/m-/mrc) and row 5 (the orange `=`) are much
    lighter than the digit rows.  A single global threshold therefore finds the
    bright rows at full height and clips the dark ones by 10-15 px, which would
    put a systematic error straight into every "measured minus drawn" number
    this tool exists to produce.

    So: pass 1 locates the runs with a generous global threshold; pass 2 grows
    each one outward against a floor and a maximum taken from ITS OWN
    neighbourhood.
    """
    s = sorted(vals)
    floor = s[int(len(s) * 0.15)]
    span = max(vals) - floor or 1.0
    coarse, start = [], None
    for i, v in enumerate(vals):
        on = v > floor + span * floor_frac
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= min_len:
                coarse.append((start, i - 1))
            start = None
    if start is not None and len(vals) - start >= min_len:
        coarse.append((start, len(vals) - 1))

    out = []
    for a, b in coarse:
        near = [vals[i] for i in range(max(0, a - 30), min(len(vals), b + 31))
                if not a <= i <= b]
        local_floor = sorted(near)[len(near) // 2] if near else floor
        local_max = max(vals[a:b + 1])
        thresh = local_floor + (local_max - local_floor) * floor_frac
        while a > 0 and vals[a - 1] > thresh:
            a -= 1
        while b < len(vals) - 1 and vals[b + 1] > thresh:
            b += 1
        out.append((frm + a, frm + b))
    return out


def profile_keypad(px):
    """Drawn row bands and drawn column bands, each measured where the buttons
    are UNIFORMLY light.

    The digit buttons are dark circles carrying a bright glyph, so a column
    profile taken across a digit row is a profile of the GLYPH (~30 px), not of
    the button (~51 px).  Row 1 (`m+ m- mrc div`) is light across all four
    columns and column 4 (`div mul add sub eq`) is light across all five rows,
    so each axis is measured against a band that is bright end to end.
    """
    rows = _runs(_profile(px, "row", 0, FB_W, *KEYPAD_ROWS), KEYPAD_ROWS[0])
    if len(rows) != 5:
        return rows, None
    band = rows[0]
    cols = _runs(_profile(px, "col", band[0], band[1] + 1, 0, FB_W), 0)
    if len(cols) != 4:
        return rows, cols
    rows = _runs(_profile(px, "row", cols[3][0], cols[3][1] + 1, *KEYPAD_ROWS),
                 KEYPAD_ROWS[0])
    return rows, cols


def bright_runs(px: bytes, along: str, lo: int, hi: int, frm: int, to: int,
                thresh=0.25, min_len=15):
    """Kept for callers that profiled the home screen with an absolute
    threshold (icons on black); the keypad needs `_runs` instead."""
    vals = _profile(px, along, lo, hi, frm, to)
    mx = max(vals) or 1
    runs, start = [], None
    for i, v in enumerate(vals):
        on = v > mx * thresh
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= min_len:
                runs.append((frm + start, frm + i - 1))
            start = None
    if start is not None and (to - frm) - start >= min_len:
        runs.append((frm + start, to - 1))
    return runs


# ------------------------------------------------------------------ oracle ---

def display_of(px: bytes) -> bytes:
    y0, y1 = DISPLAY_ROWS
    return bytes(px[(y0 * FB_W) * 3:(y1 * FB_W) * 3])


def diff(a: bytes, b: bytes) -> float:
    """Percent of bytes that differ.

    NOT a mean absolute difference: the Calculator display is a big pale-blue
    gradient carrying one dark glyph, so `7` and `9` are separated by a few
    hundred pixels out of 27520 and their MEAN difference is tiny -- small
    enough that a mean-based check declared two perfectly distinguishable
    fingerprints identical.  Counting changed bytes keeps the glyph's weight.
    """
    n = min(len(a), len(b))
    return 100.0 * sum(1 for i in range(n) if a[i] != b[i]) / n


class Oracle:
    """Which digit does the display show?  Exact-match first, nearest after.

    Guest rendering is deterministic, so the same digit reproduces the same
    bytes; the nearest-match arm exists only so an unexpected frame is reported
    as `?` instead of being silently mis-labelled.
    """

    def __init__(self, machine, cleared):
        self.m = machine
        self.cleared = cleared
        self.prints: dict[str, bytes] = {}

    def learn(self, label: str, px: bytes):
        self.prints[label] = display_of(px)

    def is_cleared(self, d: bytes) -> bool:
        # NOT byte equality.  A single pixel at the display's bottom bevel
        # flipped mid-run and every later `c` press was then read as "the
        # display would not clear", which aborted a 12-minute map with two
        # buttons measured.  A digit changes thousands of pixels; the gate only
        # has to be far below that.
        return diff(d, self.cleared) < CLEAR_TOL

    def read(self, px: bytes, tol=0.5):
        d = display_of(px)
        if self.is_cleared(d):
            return None
        best, bestd = None, 1e9
        for label, ref in self.prints.items():
            v = diff(d, ref)
            if v < bestd:
                best, bestd = label, v
        if bestd <= tol:
            return best
        return "?"


# ------------------------------------------------------------------- probe ---

class ClearFailed(Exception):
    """The display would not go back to `0`, so the next probe is meaningless.

    Raised rather than exiting, so ONE bad patch costs one edge instead of the
    whole map.  The caller counts them and gives up if they keep coming.
    """


class Prober:
    def __init__(self, m: Machine, oracle: Oracle, clear_at, hold, window,
                 poll=0.3, verbose=True):
        self.m, self.o = m, oracle
        self.clear_at = clear_at
        self.hold, self.window, self.poll = hold, window, poll
        self.verbose = verbose
        self.log: list[dict] = []
        self.clear_failures = 0

    def _wait_change(self):
        """Poll the display until it leaves the cleared state, or time out."""
        deadline = time.time() + self.window
        px = None
        while time.time() < deadline:
            time.sleep(self.poll)
            px = self.m.shot("probe")
            if not self.o.is_cleared(display_of(px)):
                return self.o.read(px)
        return self.o.read(px) if px is not None else None

    def clear(self):
        for attempt in range(4):
            self.m.tap(*self.clear_at, hold=self.hold)
            time.sleep(0.6)
            if self.o.is_cleared(display_of(self.m.shot("probe"))):
                return True
        return False

    def probe(self, x, y, retries=1):
        """Tap (x, y) from a cleared display; return the digit it produced."""
        for attempt in range(retries + 1):
            if not self.clear():
                self.clear_failures += 1
                raise ClearFailed(f"display would not clear before "
                                  f"({x},{y})")
            self.m.tap(x, y, hold=self.hold)
            got = self._wait_change()
            if got is not None:
                break
        self.log.append({"x": x, "y": y, "got": got})
        if self.verbose:
            print(f"    tap ({x:3d},{y:3d}) -> {got or '-'}", flush=True)
        return got


def find_edge(prober, want, cx, cy, axis, direction, drawn_edge, span=48):
    """Binary-search the outermost coordinate along `axis` that still hits.

    `direction` is -1 (look left/up) or +1 (right/down).  Returns the LAST
    coordinate that still registered as `want`, or None if the search could not
    be bracketed.
    """
    def hit(v):
        x, y = (v, cy) if axis == "x" else (cx, v)
        return prober.probe(x, y) == want

    inside = cx if axis == "x" else cy
    outside = inside + direction * span
    outside = max(1, min(FB_W - 2 if axis == "x" else FB_H - 2, outside))
    if hit(outside):
        # The hit box reaches further than the search window; widen once.
        wider = inside + direction * span * 2
        wider = max(1, min(FB_W - 2 if axis == "x" else FB_H - 2, wider))
        if wider == outside or hit(wider):
            return None, f"unbracketed beyond {wider}"
        outside = wider
    lo, hi = inside, outside                 # lo hits, hi does not
    while abs(hi - lo) > 1:
        mid = (lo + hi) // 2
        if hit(mid):
            lo = mid
        else:
            hi = mid
    return lo, None


# -------------------------------------------------------------------- main ---

def centre(box):
    return (box[0] + box[1]) / 2.0


def summarise(edges, boxes):
    """The table the whole tool exists to print.

    `shift` is the hit box's CENTRE displacement -- the candidate coordinate
    error.  `slop` is half its excess width -- the guest's own tap-target
    expansion, which is design and not ours.
    """
    print("\n=== hit box minus drawn box, per button ===")
    print(f"{'digit':>5} {'centre(drawn)':>14} {'dL':>5} {'dR':>5} "
          f"{'dT':>5} {'dB':>5} {'shift x':>8} {'shift y':>8} "
          f"{'slop x':>7} {'slop y':>7}")
    rows = []
    for d, e in edges.items():
        b = boxes[d]

        def g(k):
            return e.get(k, {}).get("delta")

        def pair(a, c, sign):
            if g(a) is None or g(c) is None:
                return None
            return (g(c) + sign * g(a)) / 2

        sx, sy = pair("L", "R", +1), pair("T", "B", +1)
        wx, wy = pair("L", "R", -1), pair("T", "B", -1)

        def f(v):
            return "  -  " if v is None else f"{v:5.1f}"

        print(f"{d:>5} {centre(b[:2]):6.1f},{centre(b[2:]):6.1f} "
              f"{f(g('L'))} {f(g('R'))} {f(g('T'))} {f(g('B'))} "
              f"{f(sx):>8} {f(sy):>8} {f(wx):>7} {f(wy):>7}")
        rows.append({"digit": d, "cx": centre(b[:2]), "cy": centre(b[2:]),
                     "shift_x": sx, "shift_y": sy,
                     "slop_x": wx, "slop_y": wy})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path,
                    help="restore this migration file instead of cold-booting")
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--qemu", type=Path,
                    default=REPO / "build-ipod11" / "qemu-system-arm")
    ap.add_argument("--nand", type=Path,
                    help="override the NAND tree (use a CLONE, never a "
                         "bundle's own -- guest writes land in it)")
    ap.add_argument("--nor", type=Path, help="override the NOR image")
    ap.add_argument("--iboot", type=Path, help="override the iBoot image")
    ap.add_argument("--calc-icon", default="122,247",
                    help="home-screen position of the Calculator icon")
    # 1.1.4 comes up with SpringBoard's REORDER_INFO alert over the home screen
    # on EVERY launch -- expected, because the bundles clone a pristine NAND, so
    # every launch IS a first run (TOUCH_INVESTIGATION.md).  Its Dismiss button
    # is at (180, 325).  Nothing else can be tapped until it is gone.
    ap.add_argument("--pre-tap", action="append", default=[],
                    metavar="X,Y",
                    help="tap this before looking for the Calculator icon; "
                         "repeatable (1.1.4 needs --pre-tap 180,325 to dismiss "
                         "the Edit Home Screen alert)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="extra environment for QEMU, repeatable (e.g. "
                         "--env IT_MT_SENSOR_SCALE=aspect for the A/B)")
    ap.add_argument("--home-only", action="store_true",
                    help="stop after the home screen shot, for reconnaissance")
    ap.add_argument("--boot-wait", type=float, default=60.0,
                    help="how long to wait for a LIVE panel; the run proceeds "
                         "as soon as one appears, so a cold boot just needs a "
                         "generous value (~400)")
    ap.add_argument("--launch-wait", type=float, default=20.0)
    ap.add_argument("--digits", default="7,9,1,3,5",
                    help="which digit buttons to probe (never 0)")
    ap.add_argument("--hold", type=float, default=0.25)
    ap.add_argument("--window", type=float, default=2.4,
                    help="how long to wait for the display to react to a tap")
    ap.add_argument("--span", type=int, default=48,
                    help="how far from the drawn centre an edge may be")
    ap.add_argument("--geometry-only", action="store_true")
    m68ap_paths.add_build_argument(ap, required=True)
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    nand = args.nand or paths.nand
    nor = args.nor or paths.nor
    iboot = args.iboot or paths.iboot_sb
    for name, p in (("nand", nand), ("nor", nor), ("iboot", iboot)):
        if not Path(p).exists():
            raise SystemExit(f"missing {name}: {p}")
    args.logs.mkdir(parents=True, exist_ok=True)

    machine = (f"iPhone-2G,bootrom={paths.bootrom},iboot={iboot},"
               f"nand={nand},epoch={paths.epoch}")
    sock = f"/var/tmp/calcmap-{os.getpid()}.sock"
    if os.path.exists(sock):
        os.unlink(sock)

    extra_env = dict(kv.split("=", 1) for kv in args.env)
    m = Machine(args.qemu, machine, nor, args.logs, sock, args.snapshot,
                extra_env)
    result = {"build": args.build, "nand": str(nand), "env": extra_env,
              "snapshot": str(args.snapshot) if args.snapshot else None}
    try:
        print(f"waiting {args.boot_wait:.0f}s for the home screen ...",
              flush=True)
        live = m.wait_live(args.boot_wait)
        if live < 40.0:
            live = m.ensure_awake()
        print(f"panel {live:.1f}% non-black, status {m.status()}", flush=True)
        if live < 10.0:
            print("panel never came up -- refusing to tap a dark device",
                  file=sys.stderr)
            return 1
        m.shot("home")
        shutil.copy(args.logs / "home.ppm", args.logs / "home-keep.ppm")
        for spec in args.pre_tap:
            tx, ty = (int(v) for v in spec.split(","))
            print(f"pre-tap ({tx},{ty}) ...", flush=True)
            m.tap(tx, ty, hold=0.35, settle=6.0)
        if args.pre_tap:
            m.shot("home")
            shutil.copy(args.logs / "home.ppm", args.logs / "home-keep.ppm")
        if args.home_only:
            print(f"home screen written to {args.logs / 'home-keep.ppm'}")
            return 0

        cx, cy = (int(v) for v in args.calc_icon.split(","))
        print(f"launching Calculator at ({cx},{cy}) ...", flush=True)
        m.tap(cx, cy, hold=0.4, settle=args.launch_wait)

        px = m.shot("calc")
        shutil.copy(args.logs / "calc.ppm", args.logs / "calc-keep.ppm")
        # The Calculator display is a light strip across the top; the home
        # screen is much darker there.  Measured: home 102, Calculator 149.
        strip = sum(px[(y * FB_W + x) * 3] for y in range(60, 100)
                    for x in range(40, 280)) / (40 * 240)
        if strip < 125:
            print(f"not in Calculator (top strip brightness {strip:.0f}); "
                  f"see {args.logs / 'calc-keep.ppm'}", file=sys.stderr)
            return 1
        print(f"in Calculator (top strip {strip:.0f})", flush=True)

        rows, cols = profile_keypad(px)
        print("drawn rows:   ", rows)
        print("drawn columns:", cols)
        result["drawn_rows"], result["drawn_columns"] = rows, cols
        if cols is None or len(cols) != 4:
            print("keypad profile did not find 5x4 buttons", file=sys.stderr)
            (args.logs / "map.json").write_text(json.dumps(result, indent=2))
            return 1

        boxes = {}
        for r, row in enumerate(KEY_LABELS):
            for c, label in enumerate(row):
                boxes[label] = (cols[c][0], cols[c][1], rows[r][0], rows[r][1])
        result["drawn_boxes"] = boxes
        (args.logs / "map.json").write_text(json.dumps(result, indent=2) + "\n")
        if args.geometry_only:
            return 0

        # `c` is the reset between every probe.  The drawn centre may not be
        # inside its HIT box if the shift is large, so calibrate a point that
        # actually works before relying on it.
        cbox = boxes["c"]
        oracle = Oracle(m, display_of(px))
        probes = [d.strip() for d in args.digits.split(",") if d.strip()]
        if "0" in probes:
            raise SystemExit("0 cannot be a probe: a cleared display reads 0")

        clear_at = None
        seed = probes[0]
        sbox = boxes[seed]
        for dy in (0, 8, -8, 14):
            for dx in (0, 6, -6):
                m.tap(centre(sbox[:2]), centre(sbox[2:]), hold=args.hold,
                      settle=1.0)
                if oracle.is_cleared(display_of(m.shot("probe"))):
                    continue        # the seed tap itself missed; try again
                m.tap(centre(cbox[:2]) + dx, centre(cbox[2:]) + dy,
                      hold=args.hold, settle=1.0)
                if oracle.is_cleared(display_of(m.shot("probe"))):
                    clear_at = (centre(cbox[:2]) + dx, centre(cbox[2:]) + dy)
                    break
            if clear_at:
                break
        if clear_at is None:
            print("could not find a working `c` position", file=sys.stderr)
            return 1
        print(f"clear key works at {clear_at}", flush=True)
        result["clear_at"] = clear_at

        prober = Prober(m, oracle, clear_at, args.hold, args.window)

        print("\ncalibrating display fingerprints ...", flush=True)
        for d in probes:
            b = boxes[d]
            prober.clear()
            m.tap(centre(b[:2]), centre(b[2:]), hold=args.hold, settle=1.2)
            shot = m.shot("probe")
            if oracle.is_cleared(display_of(shot)):
                print(f"  digit {d}: centre tap produced NOTHING -- the shift "
                      f"is larger than half the button", file=sys.stderr)
                return 1
            oracle.learn(d, shot)
            print(f"  digit {d}: fingerprint taken", flush=True)
        # Every fingerprint must be distinguishable from every other, or the
        # whole oracle is worthless.
        for a in probes:
            for b_ in probes:
                if a < b_:
                    v = diff(oracle.prints[a], oracle.prints[b_])
                    print(f"  {a} vs {b_}: {v:.2f}% of the display differs",
                          flush=True)
                    if v < 0.5:
                        print(f"  fingerprints {a} and {b_} are too close",
                              file=sys.stderr)
                        return 1

        print("\nsearching hit-box edges ...", flush=True)
        edges = {}
        for d in probes:
            b = boxes[d]
            bcx, bcy = centre(b[:2]), centre(b[2:])
            print(f"  digit {d} drawn x {b[0]}..{b[1]} y {b[2]}..{b[3]}",
                  flush=True)
            e = {}
            for name, axis, direction, drawn in (
                    ("L", "x", -1, b[0]), ("R", "x", +1, b[1]),
                    ("T", "y", -1, b[2]), ("B", "y", +1, b[3])):
                print(f"   edge {name}:", flush=True)
                try:
                    v, why = find_edge(prober, d, int(bcx), int(bcy), axis,
                                       direction, drawn, args.span)
                except ClearFailed as exc:
                    v, why = None, str(exc)
                    if prober.clear_failures >= 3:
                        print("  three clear failures -- stopping and keeping "
                              "what is measured", file=sys.stderr)
                        e[name] = {"measured": None, "drawn": drawn,
                                   "delta": None, "note": why}
                        edges[d] = e
                        result["edges"] = edges
                        result["aborted"] = why
                        result["summary"] = summarise(edges, boxes)
                        (args.logs / "map.json").write_text(
                            json.dumps(result, indent=2) + "\n")
                        return 1
                e[name] = {"measured": v, "drawn": drawn,
                           "delta": None if v is None else v - drawn,
                           "note": why}
                print(f"   edge {name}: measured {v} drawn {drawn} "
                      f"delta {'-' if v is None else v - drawn}", flush=True)
                edges[d] = e
                result["edges"] = edges
                result["probe_log"] = prober.log
                (args.logs / "map.json").write_text(
                    json.dumps(result, indent=2) + "\n")
            edges[d] = e
            result["edges"] = edges
            result["probe_log"] = prober.log
            result["taps"] = m.taps
            (args.logs / "map.json").write_text(
                json.dumps(result, indent=2) + "\n")

        result["summary"] = summarise(edges, boxes)
        (args.logs / "map.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"\n{m.taps} taps; full log in {args.logs / 'map.json'}")
        return 0
    finally:
        m.close()
        if os.path.exists(sock):
            os.unlink(sock)


if __name__ == "__main__":
    raise SystemExit(main())
