/**
 * UI shell: capability check, asset loading, worker lifecycle, input.
 *
 * Everything expensive (download, hashing, QEMU) happens off this thread; the
 * main thread only paints and forwards input.
 */

import { loadAssetSet, LoadError, checkCapabilities } from '../emulator/loader.js';

const DEFAULT_SET = 'm68ap-114-v1';

const elements = {
  state: document.getElementById('state'),
  bar: document.getElementById('bar'),
  screen: document.getElementById('screen'),
  home: document.getElementById('home'),
  power: document.getElementById('power'),
  diagnostics: document.getElementById('diagnostics-body'),
};

const context = elements.screen.getContext('2d', { alpha: false });
let worker = null;
const serial = [];

function setState(text, kind = 'info') {
  elements.state.textContent = text;
  elements.state.dataset.kind = kind;
}

function setProgress(fraction) {
  elements.bar.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;
}

function diagnostics(entries) {
  elements.diagnostics.replaceChildren();
  for (const [key, value] of Object.entries(entries)) {
    const term = document.createElement('dt');
    term.textContent = key;
    const definition = document.createElement('dd');
    definition.textContent = value;
    elements.diagnostics.append(term, definition);
  }
}

function formatBytes(count) {
  if (!count) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(count) / Math.log(1024)));
  return `${(count / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function drawFrame({ width, height, buffer }) {
  if (elements.screen.width !== width || elements.screen.height !== height) {
    elements.screen.width = width;
    elements.screen.height = height;
  }
  const image = new ImageData(new Uint8ClampedArray(buffer), width, height);
  context.putImageData(image, 0, 0);
}

/** Browser coordinates -> the panel's own 320x480 pixel space. */
function screenPoint(event) {
  const rect = elements.screen.getBoundingClientRect();
  return {
    x: Math.round(((event.clientX - rect.left) / rect.width) * elements.screen.width),
    y: Math.round(((event.clientY - rect.top) / rect.height) * elements.screen.height),
  };
}

function send(message) {
  worker?.postMessage(message);
}

function installInput() {
  // Pointer capture so a drag that leaves the canvas still delivers its
  // release: otherwise a touch can stay stuck down inside the guest.
  elements.screen.addEventListener('pointerdown', (event) => {
    elements.screen.setPointerCapture(event.pointerId);
    send({ type: 'input', kind: 'touch', phase: 'down', ...screenPoint(event) });
  });
  elements.screen.addEventListener('pointermove', (event) => {
    if (event.buttons === 0) return;
    send({ type: 'input', kind: 'touch', phase: 'move', ...screenPoint(event) });
  });
  const release = (event) => {
    send({ type: 'input', kind: 'touch', phase: 'up', ...screenPoint(event) });
  };
  elements.screen.addEventListener('pointerup', release);
  elements.screen.addEventListener('pointercancel', release);

  const button = (element, kind) => {
    element.addEventListener('pointerdown', () => send({ type: 'input', kind, phase: 'down' }));
    element.addEventListener('pointerup', () => send({ type: 'input', kind, phase: 'up' }));
    element.addEventListener('pointerleave', () => send({ type: 'input', kind, phase: 'up' }));
  };
  button(elements.home, 'home');
  button(elements.power, 'power');

  // The native emulator's key bindings, kept identical here.
  const keys = { h: 'home', p: 'power' };
  addEventListener('keydown', (event) => {
    const kind = keys[event.key.toLowerCase()];
    if (kind && !event.repeat) send({ type: 'input', kind, phase: 'down' });
  });
  addEventListener('keyup', (event) => {
    const kind = keys[event.key.toLowerCase()];
    if (kind) send({ type: 'input', kind, phase: 'up' });
  });

  // Losing focus must not leave a button held down in the guest.
  addEventListener('blur', () => {
    for (const kind of ['home', 'power']) send({ type: 'input', kind, phase: 'up' });
  });
}

async function main() {
  const params = new URLSearchParams(location.search);
  const setId = params.get('set') ?? DEFAULT_SET;
  const manifestUrl = new URL(
    `./public/assets/${setId}/asset-manifest.json`, location.href,
  ).href;

  const base = {
    'asset set': setId,
    'cross-origin isolated': String(globalThis.crossOriginIsolated === true),
    'hardware threads': String(navigator.hardwareConcurrency ?? 'unknown'),
  };
  diagnostics(base);

  const problems = checkCapabilities();
  if (problems.length) {
    setState(problems[0].message, 'error');
    return;
  }

  let loaded;
  try {
    loaded = await loadAssetSet(manifestUrl, (event) => {
      if (event.state === 'downloading' && event.total) {
        setState(`downloading ${event.asset} — ${formatBytes(event.received)} / ${formatBytes(event.total)}`);
        setProgress(event.received / event.total);
      } else {
        setState(`${event.state}${event.asset ? ` ${event.asset}` : ''}`);
      }
    });
  } catch (error) {
    const code = error instanceof LoadError ? error.code : 'UNKNOWN';
    setState(`${code}: ${error.message}`, 'error');
    return;
  }

  setProgress(1);
  const { manifest, files } = loaded;
  diagnostics({
    ...base,
    board: manifest.board,
    machine: manifest.machine,
    firmware: manifest.firmware,
    ...Object.fromEntries(Object.entries(manifest.assets).map(
      ([name, asset]) => [name, `${formatBytes(asset.size)} · ${asset.sha256.slice(0, 12)}…`],
    )),
  });

  worker = new Worker(new URL('../workers/emulator-worker.js', import.meta.url), {
    type: 'module',
  });
  worker.onmessage = (event) => {
    const message = event.data;
    switch (message.type) {
      case 'state':
        setState(message.detail ? `${message.state} — ${message.detail}` : message.state);
        break;
      case 'serial':
        serial.push(message.line);
        if (serial.length > 200) serial.shift();
        break;
      case 'frame':
        drawFrame(message);
        break;
      case 'error':
        setState(`${message.code}: ${message.message}`, 'error');
        break;
      default:
        break;
    }
  };

  installInput();
  // Transferring the byte arrays hands ownership to the worker rather than
  // duplicating ~300 MB of NAND across two heaps.
  worker.postMessage(
    { type: 'start', manifest, files },
    Object.values(files).map((bytes) => bytes.buffer),
  );
}

main();
