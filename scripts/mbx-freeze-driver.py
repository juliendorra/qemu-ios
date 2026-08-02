#!/usr/bin/env python3
"""Drive a packaged app bundle through app-open + HOME and introspect the MBX.

Built 2026-08-02 while hunting the unpatched-1.0 snapshot dismissal freeze.
The app-button-probe tells you WHETHER the dismissal works; this tool tells
you WHERE it stops: it reproduces the sequence over QMP, then PC-samples the
guest and dumps the AppleMBX op-state structures, and can record a per-TB
exec trace of the userland MBX2D/MBXConnect libraries and the AppleMBX kext
(the instrument that found the TA doorbell).  Leaves QEMU running for
further QMP poking; kill it yourself.

Typical runs:
  scripts/mbx-freeze-driver.py --logs /tmp/mbx-freeze
  scripts/mbx-freeze-driver.py --logs /tmp/mbx-freeze --exec-trace
Symbolize the exec trace with scripts/mbx-trace-symbolize.py.

MBX env (IT_MBX_*) is inherited; the defaults below only fill gaps, so a
regression run can pin any engine mode it wants.
"""
import argparse, collections, importlib.util, json, os, pathlib, socket, \
    subprocess, time

REPO = pathlib.Path(__file__).resolve().parent.parent
FB_W, FB_H = 320, 480

# Prebound library/kext text ranges on iPhone OS 1.0 (1A543a); the kext
# range is LIVE addresses (restore kernelcache file VA = live - 0x2000).
DFILTER_1A543A = '0x30b37000+0x9000,0x31baf000+0x2000,0xc032b000+0x11000'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QMP:
    def __init__(self, path, deadline=420):
        t0 = time.time()
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX)
                self.s.connect(path)
                break
            except OSError:
                if time.time() - t0 > deadline:
                    raise
                time.sleep(2)
        self.f = self.s.makefile('rw')
        json.loads(self.f.readline())
        self.cmd('qmp_capabilities')

    def cmd(self, c, args=None):
        self.f.write(json.dumps({'execute': c, 'arguments': args or {}}) + '\n')
        self.f.flush()
        while True:
            r = json.loads(self.f.readline())
            if 'event' not in r:
                return r

    def hmp(self, line):
        return self.cmd('human-monitor-command',
                        {'command-line': line}).get('return', '')


def abs_move(q, px, py):
    q.cmd('input-send-event', {'events': [
        {'type': 'abs', 'data': {'axis': 'x', 'value': int(px / FB_W * 32768)}},
        {'type': 'abs', 'data': {'axis': 'y', 'value': int(py / FB_H * 32768)}}]})


def tap(q, px, py, hold=0.25):
    abs_move(q, px, py)
    q.cmd('input-send-event', {'events': [
        {'type': 'btn', 'data': {'down': True, 'button': 'left'}}]})
    time.sleep(hold)
    q.cmd('input-send-event', {'events': [
        {'type': 'btn', 'data': {'down': False, 'button': 'left'}}]})


def key(q, name, hold=0.15):
    for down in (True, False):
        q.cmd('input-send-event', {'events': [
            {'type': 'key', 'data': {'down': down,
                                     'key': {'type': 'qcode', 'data': name}}}]})
        if down:
            time.sleep(hold)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--app', default='/Applications/iPhone 2G (iOS 1.0).app')
    ap.add_argument('--logs', type=pathlib.Path, required=True)
    ap.add_argument('--icon', default='277,258',
                    help='app icon tap coordinates x,y')
    ap.add_argument('--exec-trace', action='store_true',
                    help='per-TB exec trace of MBX2D/MBXConnect/AppleMBX '
                         '(1A543a ranges) into <logs>/mbx2d-exec.log')
    ap.add_argument('--dfilter', default=DFILTER_1A543A)
    ap.add_argument('--vnc-port', type=int, default=5997)
    ap.add_argument('--open-wait', type=float, default=50)
    ap.add_argument('--dismiss-wait', type=float, default=60)
    args = ap.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    icon = tuple(int(v) for v in args.icon.split(','))
    qmp_path = f'/tmp/mbx-freeze-{os.getpid()}.sock'

    lock = _load('lockprobe', REPO / 'scripts' / 'lock-unlock-probe.py')
    env = dict(os.environ, IT_LCD_TRACE='1', S5L8900_HTTP_BRIDGE='0',
               S5L8900_HTTPS_BRIDGE='0')
    # Engine mode: inherit, fill gaps with the current investigation config.
    for k, v in {'IT_IOS10_SOFTWARE_MBX2D': '0', 'IT_MBX_2D_RASTER': '1',
                 'IT_MBX_2D_RING': '1', 'IT_MBX_2D_EVENT': '0x45c',
                 'IT_MBX_TRACE': 'all', 'IT_MBX_2D_TRACE': '1',
                 'IT_MBX_OP_TRACE': '1'}.items():
        env.setdefault(k, v)

    cmd = [f'{args.app}/Contents/MacOS/iPod Touch',
           '-qmp', f'unix:{qmp_path},server,nowait',
           '-vnc', f'127.0.0.1:{args.vnc_port - 5900}']
    if args.exec_trace:
        cmd += ['-dfilter', args.dfilter, '-d', 'exec,nochain',
                '-D', str(args.logs / 'mbx2d-exec.log')]
    log = open(args.logs / 'qemu.log', 'wb')
    proc = subprocess.Popen(cmd, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    print('launched, pid', proc.pid, flush=True)
    client = lock.DisplayClient(args.vnc_port)
    client.start()
    time.sleep(3)
    q = QMP(qmp_path)
    print('qmp up', flush=True)

    t0 = time.time()
    while time.time() - t0 < 420:
        if b'Touch input ready' in (args.logs / 'qemu.log').read_bytes():
            break
        time.sleep(5)
    print('touch gate open', flush=True)
    time.sleep(20)

    tap(q, *icon)
    print('icon tapped', flush=True)
    time.sleep(args.open_wait)
    q.cmd('screendump', {'filename': str(args.logs / 'in_app.ppm')})
    key(q, 'h')
    print('HOME pressed', flush=True)
    time.sleep(args.dismiss_wait)
    q.cmd('screendump', {'filename': str(args.logs / 'after_home.ppm')})

    pcs = collections.Counter()
    for _ in range(300):
        for line in q.hmp('info registers').splitlines():
            if 'R15=' in line:
                pcs[line.split('R15=')[1][:8]] += 1
        time.sleep(0.03)
    print('PC samples:', pcs.most_common(12), flush=True)
    print('state1:', q.hmp('x/32wx 0xc0a01200'), flush=True)
    print('state2:', q.hmp('x/48wx 0xf2767000'), flush=True)
    print('DONE - qemu left running, qmp at', qmp_path, flush=True)


if __name__ == '__main__':
    main()
