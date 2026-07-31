#!/usr/bin/env node
/*
 * CPU-profile the emulator WORKER during a jit-boot run, over the Chrome
 * DevTools Protocol, with no npm dependencies (raw WebSocket via node:http).
 *
 * Why a script and not DevTools by hand: the emulator runs in a nested
 * dedicated worker under -sPROXY_TO_PTHREAD, headless, and the interesting
 * stretch (an app launch, a boot phase) is minutes in — a hand-driven
 * profile is unrepeatable. This attaches to EVERY worker target, starts the
 * sampling profiler on each, waits, stops, and writes one .cpuprofile per
 * worker plus a self-contained summary (top functions by self time, bucketed
 * into: generated wasm TBs, wasm64.c runtime, libffi, display, JS glue).
 *
 * Usage:
 *   node scripts/wasm/profile-run.mjs \
 *     --url 'http://localhost:8031/public/jit-boot/?resume=1' \
 *     --warmup 25 --duration 60 --out /tmp/prof
 *
 * The page is loaded in headless Chrome with the three throttle flags off
 * (same as bench-run.py — a throttled tab profiles as a hang). Chrome is
 * launched with --remote-debugging-port=0 and the port is read back from
 * DevToolsActivePort in the profile dir.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import http from 'node:http';
import crypto from 'node:crypto';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, arr) => {
  if (v.startsWith('--')) a.push([v.slice(2), arr[i + 1]]);
  return a;
}, []));
const URL_ = args.url ?? 'http://localhost:8031/public/jit-boot/?resume=1';
const WARMUP = Number(args.warmup ?? 25);       // let staging/boot settle
const DURATION = Number(args.duration ?? 60);   // profiled stretch
const OUT = args.out ?? '/tmp/prof';
const CHROME = args.chrome ??
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const PROFDIR = `${OUT}/chrome-profile`;
rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

/* ------------------------------------------------- minimal CDP transport --- */

function wsConnect(url) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const key = crypto.randomBytes(16).toString('base64');
    const req = http.request({
      host: u.hostname, port: u.port, path: u.pathname + u.search,
      headers: {
        Connection: 'Upgrade', Upgrade: 'websocket',
        'Sec-WebSocket-Version': 13, 'Sec-WebSocket-Key': key,
      },
    });
    req.on('upgrade', (res, socket) => {
      socket.setNoDelay(true);
      resolve(makeWs(socket));
    });
    req.on('error', reject);
    req.end();
  });
}

/* Client-to-server frames must be masked; server-to-client never are. */
function makeWs(socket) {
  const handlers = new Set();
  let buf = Buffer.alloc(0);
  /* A large response (a multi-MB .cpuprofile) arrives FRAGMENTED: one op=1
   * frame with fin=0, then op=0 continuations. Dropping those was a hang
   * that looked like Profiler.stop never answering. */
  let frags = [];
  socket.on('data', (d) => {
    buf = Buffer.concat([buf, d]);
    for (;;) {
      if (buf.length < 2) return;
      const fin = buf[0] & 0x80, op = buf[0] & 0x0f;
      let len = buf[1] & 0x7f, off = 2;
      if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
      if (buf.length < off + len) return;
      const payload = buf.subarray(off, off + len);
      buf = buf.subarray(off + len);
      if (op === 1 || op === 0) {
        frags.push(Buffer.from(payload));
        if (fin) {
          const text = Buffer.concat(frags).toString('utf8');
          frags = [];
          handlers.forEach((h) => h(text));
        }
      } else if (op === 9) {           /* ping -> pong */
        send(0x0a, payload);
      }
    }
  });
  function send(op, payload) {
    const mask = crypto.randomBytes(4);
    const masked = Buffer.from(payload);
    for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i & 3];
    let header;
    if (payload.length < 126) header = Buffer.from([0x80 | op, 0x80 | payload.length]);
    else if (payload.length < 65536) {
      header = Buffer.alloc(4); header[0] = 0x80 | op; header[1] = 0x80 | 126;
      header.writeUInt16BE(payload.length, 2);
    } else {
      header = Buffer.alloc(10); header[0] = 0x80 | op; header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(payload.length), 2);
    }
    socket.write(Buffer.concat([header, mask, masked]));
  }
  return {
    sendText: (t) => send(1, Buffer.from(t, 'utf8')),
    onMessage: (h) => handlers.add(h),
    close: () => socket.destroy(),
  };
}

function makeCdp(ws) {
  let id = 0;
  const pending = new Map();
  const eventHandlers = new Map();
  ws.onMessage((text) => {
    const m = JSON.parse(text);
    if (m.id !== undefined && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(m.error.message)) : resolve(m.result);
    } else if (m.method && eventHandlers.has(m.method)) {
      eventHandlers.get(m.method)(m.params);
    }
  });
  return {
    call: (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const msg = { id: ++id, method, params };
      if (sessionId) msg.sessionId = sessionId;
      pending.set(msg.id, { resolve, reject });
      ws.sendText(JSON.stringify(msg));
      /* A dead worker never answers; a hang here once cost a whole night. */
      setTimeout(() => {
        if (pending.has(msg.id)) {
          pending.delete(msg.id);
          reject(new Error(`${method} timed out after 30s`));
        }
      }, 30000);
    }),
    on: (method, h) => eventHandlers.set(method, h),
    close: ws.close,
  };
}

const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));

/* ------------------------------------------------------------------ main --- */

const chrome = spawn(CHROME, [
  `--user-data-dir=${PROFDIR}`, '--headless=new', '--remote-debugging-port=0',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--disable-features=CalculateNativeWinOcclusion',
  URL_,
], { stdio: ['ignore', 'ignore', 'pipe'] });
let chromeErr = '';
chrome.stderr.on('data', (d) => { chromeErr += d; });

let port = null;
for (let i = 0; i < 50 && port === null; i++) {
  await sleep(0.2);
  try {
    port = readFileSync(`${PROFDIR}/DevToolsActivePort`, 'utf8').split('\n')[0];
  } catch { /* not written yet */ }
}
if (port === null) {
  console.error('Chrome never wrote DevToolsActivePort; stderr tail:');
  console.error(chromeErr.slice(-2000));
  process.exit(1);
}

const list = await new Promise((resolve, reject) => {
  http.get({ host: '127.0.0.1', port, path: '/json/version' }, (res) => {
    let d = ''; res.on('data', (c) => d += c); res.on('end', () => resolve(JSON.parse(d)));
  }).on('error', reject);
});
const cdp = makeCdp(await wsConnect(list.webSocketDebuggerUrl));

/*
 * Workers are NESTED (page -> pthread-pool worker -> ...), so waitForDebugger
 * on auto-attach and immediate resume is the reliable way to catch them all.
 */
const sessions = new Map();            /* sessionId -> {url, profile?} */
cdp.on('Target.attachedToTarget', async ({ sessionId, targetInfo }) => {
  sessions.set(sessionId, { url: targetInfo.url, type: targetInfo.type });
  try {
    await cdp.call('Runtime.runIfWaitingForDebugger', {}, sessionId);
    await cdp.call('Target.setAutoAttach',
      { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sessionId);
  } catch { /* target may be gone, or not support Target.* -- fine */ }
});
try {
  await cdp.call('Target.setAutoAttach',
    { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
} catch (e) {
  console.log(`[profile] browser-level setAutoAttach: ${e.message} `
    + '(continuing; page-level auto-attach still applies)');
}
/* Also attach to the already-created page target. */
const { targetInfos } = await cdp.call('Target.getTargets');
for (const t of targetInfos.filter((t) => t.type === 'page')) {
  try {
    const { sessionId } = await cdp.call('Target.attachToTarget',
      { targetId: t.targetId, flatten: true });
    sessions.set(sessionId, { url: t.url, type: t.type });
    await cdp.call('Target.setAutoAttach',
      { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sessionId);
  } catch { /* fine */ }
}

console.log(`[profile] warmup ${WARMUP}s (${URL_})`);
await sleep(WARMUP);

const workers = [...sessions.entries()]
  .filter(([, s]) => s.type === 'worker' || s.type === 'shared_worker');
console.log(`[profile] ${workers.length} worker targets:`);
workers.forEach(([, s]) => console.log(`  - ${s.url}`));

for (const [sid] of workers) {
  try {
    await cdp.call('Profiler.enable', {}, sid);
    await cdp.call('Profiler.setSamplingInterval', { interval: 200 }, sid);
    await cdp.call('Profiler.start', {}, sid);
  } catch (e) { console.log(`  start failed: ${e.message}`); }
}
console.log(`[profile] sampling for ${DURATION}s...`);
await sleep(DURATION);

const summaries = [];
let n = 0;
for (const [sid, s] of workers) {
  try {
    const { profile } = await cdp.call('Profiler.stop', {}, sid);
    const file = `${OUT}/worker-${n}.cpuprofile`;
    writeFileSync(file, JSON.stringify(profile));
    summaries.push(summarize(profile, s.url, file));
    n++;
  } catch (e) { console.log(`  stop failed (${s.url}): ${e.message}`); }
}

function summarize(profile, url, file) {
  /* self time per node = samples * interval; walk samples[] */
  const nodeById = new Map(profile.nodes.map((nd) => [nd.id, nd]));
  const selfUs = new Map();
  const dt = profile.timeDeltas ?? [];
  (profile.samples ?? []).forEach((id, i) => {
    selfUs.set(id, (selfUs.get(id) ?? 0) + (dt[i] ?? 0));
  });
  const buckets = {};
  const top = [];
  let total = 0;
  for (const [id, us] of selfUs) {
    const nd = nodeById.get(id);
    if (!nd) continue;
    const fn = nd.callFrame.functionName || '(anonymous)';
    const src = nd.callFrame.url || '';
    total += us;
    let bucket;
    if (/^wasm-function|^js-to-wasm|^wasm-to-js/.test(fn) || /^wasm:/.test(src)) {
      /* Generated TB modules are anonymous instances; the big engine module
       * has names. Distinguish by module URL hash count later if needed. */
      bucket = /qemu/.test(src) ? 'engine wasm (qemu module)' : 'generated TB wasm';
    } else if (fn === '(garbage collector)') bucket = 'GC';
    else if (fn === '(program)') bucket = '(program - V8 internal)';
    else if (fn === '(idle)') bucket = '(idle)';
    else bucket = 'JS';
    buckets[bucket] = (buckets[bucket] ?? 0) + us;
    top.push({ fn: fn.slice(0, 90), src: src.slice(-60), ms: us / 1000 });
  }
  top.sort((a, b) => b.ms - a.ms);
  return { url, file, totalMs: total / 1000, buckets, top: top.slice(0, 25) };
}

for (const s of summaries) {
  console.log(`\n=== ${s.url}  (${s.totalMs.toFixed(0)} ms sampled) -> ${s.file}`);
  for (const [b, us] of Object.entries(s.buckets).sort((a, b) => b[1] - a[1])) {
    console.log(`  ${(us / 1000).toFixed(0).padStart(8)} ms  ${(100 * us / (s.totalMs * 1000)).toFixed(1).padStart(5)}%  ${b}`);
  }
  console.log('  top self-time frames:');
  for (const t of s.top.slice(0, 15)) {
    console.log(`    ${t.ms.toFixed(0).padStart(7)} ms  ${t.fn}  ${t.src}`);
  }
}

writeFileSync(`${OUT}/summary.json`, JSON.stringify(summaries, null, 2));
console.log(`\n[profile] wrote ${OUT}/summary.json`);
chrome.kill('SIGTERM');
cdp.close();
process.exit(0);
