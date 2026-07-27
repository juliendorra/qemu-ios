#!/usr/bin/env python3
"""Parallel, self-judging M68AP baseband experiments.

Boots one iPhone-2G instance per ruleset, wires uart1 to an external Python
S-Gold2 modem (scripts/sgold2d.py) driven by that ruleset, and watches each
serial log until a DECISIVE verdict fires, then kills the instance. Runs all
rulesets concurrently, so a whole hypothesis matrix costs one boot's wall
time (minutes), and every run leaves a full uart1 trace + an "unmatched
commands" list that says exactly what to answer next.

Verdicts (checked from the serial log every poll):
  springboard_reached   "Configuring SpringBoard" or the framebuffer attach
                        marker appeared; instance runs --post-success more
                        seconds, then framebuffers are dumped from RAM.
  baseband_retry_loop   serial stalled AND >= --retry-threshold AppleBaseband
                        lines: the known baseband-on failure signature.
  stalled               serial stopped growing for --stall-secs.
  timeout               --max-wall reached without any of the above.

On every terminal verdict the lab samples the guest PC (deadlock vs crawl),
dumps the three framebuffer bases (did anything render), keeps the last
serial lines, and folds in the modem's summary.json (commands seen /
UNMATCHED). Results land in <logs>/matrix.json + a printed table.

Special ruleset names (instead of a JSON path):
  none      IT_M68AP_NO_BASEBAND=1, nothing on uart1 -- the known-good
            SpringBoard baseline.
  builtin   the in-QEMU C stub, with IT_BASEBAND_TRACE capturing its
            conversation.

Example -- capture what AppleBaseband actually says, three ways, in parallel:
    python3 scripts/baseband-lab.py --logs /tmp/bblab \
        --rules scripts/baseband-rules/silent.json \
                scripts/baseband-rules/stub.json none

NOTE the env-var asymmetry: external-modem instances still set
IT_M68AP_NO_BASEBAND=1 -- that only disables the *built-in* C stub so uart1
falls through to the second -serial argument (the modem socket).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import Workspace, prune_runs, require_free_bytes, NAND_TREE_BYTES
import m68ap_paths

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
DEFAULT_BOOTROM = m68ap_paths.BOOTROM
# The iBoot/NOR/NAND defaults come from --build (see resolve_build); the lab
# used to hardcode m68ap-artifacts/stage/, which WAS iPhone OS 1.1.4.
DEFAULT_IBOOT = DEFAULT_NOR = DEFAULT_NAND = None
SGOLD2D = REPO / "scripts" / "sgold2d.py"

FB_BASES = {"iboot_0x0fe00000": 0x0FE00000,
            "kernel_0x0f400000": 0x0F400000,
            "kernel_0x0f496000": 0x0F496000}
FB_SIZE = 320 * 480 * 4

MARKERS = {
    "panic": rb"panic\(cpu",
    "springboard_config": rb"Configuring SpringBoard",
    "fb_attach": rb"IOMobileFramebufferUserClient",
    "applebaseband": rb"AppleBaseband",
    "sim_status": rb"lookup_baseband_info",
    "activation": rb"determine_activation_state",
    "launchd": rb"launchd",
    "springboard_proc": rb"SpringBoard\[",
}
R15_RE = re.compile(rb"R15=([0-9a-fA-F]{8})")


class QMP:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self._read_obj()
        self.execute("qmp_capabilities")

    def _read_obj(self, timeout: float = 10.0) -> dict:
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def execute(self, cmd: str, **args):
        req = {"execute": cmd}
        if args:
            req["arguments"] = args
        self.sock.sendall((json.dumps(req) + "\n").encode())
        while True:
            obj = self._read_obj()
            if "return" in obj or "error" in obj:
                return obj

    def hmp(self, line: str) -> str:
        r = self.execute("human-monitor-command", **{"command-line": line})
        return r.get("return", "") if isinstance(r, dict) else ""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def nonzero_pct(path: Path) -> float:
    try:
        data = path.read_bytes()
    except OSError:
        return -1.0
    if not data:
        return -1.0
    return round(sum(1 for b in data if b) / len(data) * 100.0, 3)


class Instance:
    def __init__(self, name: str, ruleset: str, args):
        self.name = name
        self.ruleset = ruleset          # path, "none", or "builtin"
        self.args = args
        self.logs = args.logs / name
        self.logs.mkdir(parents=True, exist_ok=True)
        self.serial = self.logs / "serial.log"
        self.qmp_path = f"/tmp/bblab-{os.getpid()}-{name}.qmp"
        self.bb_path = f"/tmp/bblab-{os.getpid()}-{name}.bb"
        self.qemu: subprocess.Popen | None = None
        self.modem: subprocess.Popen | None = None
        self.result: dict = {"name": name, "ruleset": ruleset}

    # -- setup -----------------------------------------------------------
    def stage(self) -> tuple[Path, Path]:
        stage = self.logs / "stage"
        nand = stage / "nand"
        if nand.exists():
            shutil.rmtree(nand)
        stage.mkdir(exist_ok=True)
        subprocess.run(["cp", "-Rc", str(self.args.nand), str(nand)],
                       check=True)
        for b in range(8):
            (nand / f"bank{b}").mkdir(exist_ok=True)
        nor = stage / "nor.bin"
        shutil.copy2(self.args.nor, nor)
        return nand, nor

    def launch(self) -> None:
        nand, nor = self.stage()
        for p in (self.qmp_path, self.bb_path):
            if os.path.exists(p):
                os.unlink(p)
        self.serial.write_bytes(b"")
        machine = (f"iPhone-2G,bootrom={self.args.bootrom},"
                   f"iboot={self.args.iboot},nand={nand}"
                   f",epoch={self.args.epoch}")
        cmd = [str(self.args.qemu), "-M", machine, "-m", "1G",
               "-pflash", str(nor),
               "-L", str(APP / "Resources" / "pc-bios"),
               "-display", "none",
               "-serial", f"file:{self.serial}",
               "-qmp", f"unix:{self.qmp_path},server,nowait"]
        env = dict(os.environ)
        if self.ruleset == "builtin" or self.ruleset.endswith(".rules"):
            # In-QEMU stub: instant in-MMIO replies. A .rules path loads a
            # file-driven response table (IT_BASEBAND_RULES) so hypotheses
            # iterate without a rebuild but with builtin timing.
            env.pop("IT_M68AP_NO_BASEBAND", None)
            env["IT_BASEBAND_TRACE"] = str(self.logs / "bb-c-trace.log")
            if self.ruleset.endswith(".rules"):
                env["IT_BASEBAND_RULES"] = self.ruleset
        else:
            # Disable only the BUILT-IN stub; for external-modem instances
            # uart1 then falls through to the second -serial (the socket).
            env["IT_M68AP_NO_BASEBAND"] = "1"
            if self.ruleset != "none":
                idx = cmd.index("-qmp")
                cmd[idx:idx] = ["-serial",
                                f"unix:{self.bb_path},server=on,wait=off"]
        (self.logs / "command.txt").write_text(
            " ".join(cmd) + f"\n# IT_M68AP_NO_BASEBAND={env.get('IT_M68AP_NO_BASEBAND', '')}\n")
        self.qemu = subprocess.Popen(
            cmd, env=env,
            stdout=(self.logs / "qemu-stdout.log").open("wb"),
            stderr=(self.logs / "qemu-stderr.log").open("wb"))
        if self.ruleset not in ("none", "builtin") and \
                not self.ruleset.endswith(".rules"):
            mcmd = [sys.executable, str(SGOLD2D),
                    "--socket", self.bb_path,
                    "--rules", self.ruleset,
                    "--logs", str(self.logs / "modem")]
            self.modem = subprocess.Popen(
                mcmd,
                stdout=(self.logs / "modem-stdout.log").open("wb"),
                stderr=subprocess.STDOUT)

    # -- monitoring ------------------------------------------------------
    def read_serial(self) -> bytes:
        try:
            return self.serial.read_bytes()
        except OSError:
            return b""

    def monitor(self) -> dict:
        t0 = time.monotonic()
        last_size = -1
        last_growth = t0
        success_at = None
        timeline = (self.logs / "timeline.jsonl").open("a")
        verdict = "timeout"
        counts: Counter = Counter()
        while True:
            time.sleep(self.args.poll_secs)
            wall = time.monotonic() - t0
            data = self.read_serial()
            counts = Counter({k: len(re.findall(rx, data))
                              for k, rx in MARKERS.items()})
            if len(data) != last_size:
                last_size = len(data)
                last_growth = time.monotonic()
            stalled_for = time.monotonic() - last_growth
            tick = {"t": round(wall, 1), "serial_bytes": len(data),
                    "stalled_for": round(stalled_for, 1), **counts}
            timeline.write(json.dumps(tick) + "\n")
            timeline.flush()

            reached_sb = (counts["springboard_config"] > 0 or
                          counts["fb_attach"] > 0)
            if reached_sb and success_at is None:
                success_at = time.monotonic()
                print(f"[{self.name}] SpringBoard markers at t={wall:.0f}s; "
                      f"letting it settle {self.args.post_success}s",
                      flush=True)
            if success_at is not None:
                if time.monotonic() - success_at >= self.args.post_success:
                    verdict = "springboard_reached"
                    break
            elif counts["panic"] > 0:
                verdict = "panicked"
                break
            elif stalled_for >= self.args.stall_secs:
                if counts["applebaseband"] >= self.args.retry_threshold:
                    verdict = "baseband_retry_loop"
                else:
                    verdict = "stalled"
                break
            elif wall >= self.args.max_wall:
                verdict = "timeout"
                break
            if self.qemu and self.qemu.poll() is not None:
                verdict = "qemu_died"
                break
        timeline.close()
        return {"verdict": verdict,
                "wall_s": round(time.monotonic() - t0, 1),
                "serial_bytes": last_size,
                "serial_lines": self.read_serial().count(b"\n"),
                "markers": dict(counts)}

    # -- diagnostics -----------------------------------------------------
    def diagnose(self) -> dict:
        diag: dict = {}
        try:
            qmp = QMP(self.qmp_path)
        except OSError as e:
            return {"qmp_error": str(e)}
        try:
            pcs = []
            for _ in range(self.args.pc_samples):
                m = R15_RE.search(qmp.hmp("info registers").encode())
                if m:
                    pcs.append(m.group(1).decode())
                time.sleep(0.5)
            diag["pc_samples"] = pcs
            diag["distinct_pcs"] = len(set(pcs))
            qmp.execute("stop")
            diag["framebuffers"] = {}
            for name, base in FB_BASES.items():
                raw = self.logs / f"fb_{name}.raw"
                qmp.execute("pmemsave", val=base, size=FB_SIZE,
                            filename=str(raw))
                diag["framebuffers"][name] = nonzero_pct(raw)
                raw.unlink(missing_ok=True)
        except (OSError, EOFError) as e:
            diag["qmp_error"] = str(e)
        finally:
            qmp.close()
        return diag

    def collect(self, mon: dict) -> dict:
        res = {**self.result, **mon}
        res.update(self.diagnose())
        tail = self.read_serial().splitlines()[-self.args.tail_lines:]
        (self.logs / "serial-tail.txt").write_bytes(
            b"\n".join(tail) + b"\n")
        summary = self.logs / "modem" / "summary.json"
        if summary.exists():
            try:
                res["modem"] = json.loads(summary.read_text())
            except json.JSONDecodeError:
                pass
        return res

    def kill(self) -> None:
        for proc in (self.modem, self.qemu):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
                proc.wait()
        for p in (self.qmp_path, self.bb_path):
            if os.path.exists(p):
                os.unlink(p)

    def run(self) -> dict:
        try:
            self.launch()
            print(f"[{self.name}] booting (ruleset={self.ruleset})", flush=True)
            mon = self.monitor()
            print(f"[{self.name}] verdict: {mon['verdict']} "
                  f"after {mon['wall_s']}s "
                  f"({mon['serial_lines']} serial lines)", flush=True)
            return self.collect(mon)
        except Exception as e:  # keep the matrix alive if one instance dies
            return {**self.result, "verdict": "lab_error", "error": repr(e)}
        finally:
            self.kill()
            # Staged NAND/NOR are reproducible; evidence (serial, traces,
            # modem logs, timeline) is not and is never registered here.
            # See scripts/lab_workspace.py -- a session of un-pruned runs
            # once filled the disk outright.
            ws = Workspace(self.logs, keep=getattr(self.args, "keep_artifacts",
                                                   False), label=self.name)
            ws.disposable(self.logs / "stage")
            ws.cleanup(verbose=False)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", nargs="+", required=True,
                    help="ruleset JSON paths and/or the special names "
                    "'none' and 'builtin'; one parallel instance each")
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path, default=DEFAULT_BOOTROM)
    ap.add_argument("--iboot", type=Path, default=DEFAULT_IBOOT)
    ap.add_argument("--nor", type=Path, default=DEFAULT_NOR)
    ap.add_argument("--nand", type=Path, default=DEFAULT_NAND)
    ap.add_argument("--logs", type=Path, required=True)
    m68ap_paths.add_build_argument(ap)
    ap.add_argument("--max-wall", type=float, default=600,
                    help="hard per-instance wall-clock cap (s)")
    ap.add_argument("--stall-secs", type=float, default=90,
                    help="no serial growth for this long => terminal verdict")
    ap.add_argument("--post-success", type=float, default=60,
                    help="extra run time after SpringBoard markers, before "
                    "the framebuffer dump")
    ap.add_argument("--retry-threshold", type=int, default=20,
                    help="AppleBaseband lines that qualify a stall as the "
                    "baseband retry-loop signature")
    ap.add_argument("--stagger-secs", type=float, default=30,
                    help="delay between instance launches. Host contention "
                    "during the guest's USB-start window (~15-25s into a "
                    "boot) stretches driver starts and flips the "
                    "IOPMrootDomain under-retain race into the "
                    "IOIpodUSBDevice::start panic; staggering keeps at most "
                    "one instance in that window at a time. 0 = simultaneous "
                    "(only safe for 1-2 instances).")
    ap.add_argument("--keep-artifacts", action="store_true",
                    help="keep staged NAND/NOR per instance (debugging); "
                         "default deletes them, evidence is always kept")
    ap.add_argument("--keep-runs", type=int, default=3,
                    help="previous run dirs to keep alongside --logs "
                         "(0 = keep all)")
    ap.add_argument("--poll-secs", type=float, default=2.0)
    ap.add_argument("--pc-samples", type=int, default=8)
    ap.add_argument("--tail-lines", type=int, default=40)
    args = ap.parse_args()

    # Fill whatever was not given explicitly from this build's directory, and
    # carry its security epoch: booting a firmware under another's wedges in
    # iBoot with an empty serial log.
    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    args.iboot = args.iboot or paths.iboot_sb
    args.nor = args.nor or paths.nor
    args.nand = args.nand or paths.nand
    args.epoch = paths.epoch
    print(f"[baseband-lab] {m68ap_paths.describe(args.build)}")

    for p in (args.qemu, args.bootrom, args.iboot, args.nor, args.nand):
        if not Path(p).exists():
            ap.error(f"missing artifact: {p}")

    instances = []
    seen: dict[str, int] = {}
    for spec in args.rules:
        if spec in ("none", "builtin"):
            name, ruleset = spec, spec
        else:
            path = Path(spec)
            if not path.is_file():
                ap.error(f"ruleset not found: {spec}")
            # .json = external socket modem; .rules = in-QEMU C table.
            name = path.stem + ("-ct" if path.suffix == ".rules" else "")
            ruleset = str(path.resolve())
        # The same ruleset may be listed multiple times to measure flaky
        # boots (e.g. the nondeterministic IOIpodUSBDevice::start panic);
        # suffix repeats so instances stay distinct.
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}-{seen[name]}"
        instances.append(Instance(name, ruleset, args))

    args.logs.mkdir(parents=True, exist_ok=True)

    # Each instance stages its own NAND clone; refuse up front rather than
    # fill the disk half-way through a matrix (see scripts/lab_workspace.py).
    require_free_bytes(args.logs, len(instances) * NAND_TREE_BYTES,
                       f"{len(instances)} staged NAND tree(s)")
    if getattr(args, "keep_runs", 3):
        prune_runs(args.logs.parent, args.keep_runs, f"{args.logs.name}*")
    results: list[dict] = [None] * len(instances)  # type: ignore

    def worker(i: int) -> None:
        time.sleep(i * args.stagger_secs)
        results[i] = instances[i].run()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(len(instances))]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("interrupted; killing instances", flush=True)
        for inst in instances:
            inst.kill()
        return 1

    matrix = {"wall_s": round(time.monotonic() - t0, 1),
              "artifacts": {"iboot": str(args.iboot), "nor": str(args.nor),
                            "nand": str(args.nand)},
              "results": results}
    (args.logs / "matrix.json").write_text(json.dumps(matrix, indent=2) + "\n")

    print("\n=== MATRIX ===")
    hdr = (f"{'instance':<12} {'verdict':<22} {'wall':>6} {'lines':>6} "
           f"{'bb':>4} {'unmatched':>9} {'fb0f400000':>10}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        modem = r.get("modem") or {}
        unmatched = len(modem.get("unmatched_commands", {}))
        fbs = r.get("framebuffers") or {}
        print(f"{r['name']:<12} {r.get('verdict', '?'):<22} "
              f"{r.get('wall_s', 0):>6} {r.get('serial_lines', 0):>6} "
              f"{r.get('markers', {}).get('applebaseband', 0):>4} "
              f"{unmatched:>9} "
              f"{fbs.get('kernel_0x0f400000', -1):>10}")
    print(f"\nreport: {args.logs / 'matrix.json'}")
    for r in results:
        modem = r.get("modem") or {}
        um = modem.get("unmatched_commands") or {}
        if um:
            print(f"\n[{r['name']}] unmatched commands (answer these next):")
            for cmd, n in list(um.items())[:20]:
                print(f"  {n:>4}x {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
