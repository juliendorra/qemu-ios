/**
 * The emulator worker: owns QEMU, keeps it off the UI thread.
 *
 * Protocol with the main thread
 *   in : {type:'start', manifest, files, canvas?}
 *        {type:'input', kind:'touch'|'home'|'power', ...}
 *        {type:'stop'}
 *   out: {type:'state', state, detail?}
 *        {type:'serial', line}
 *        {type:'frame', width, height, buffer}   (only without OffscreenCanvas)
 *        {type:'error', code, message}
 *
 * STATUS: the asset plumbing below is final; the QEMU instantiation marked
 * PROVISIONAL is written against Emscripten's documented module shape and must
 * be re-checked against the first real build's qemu-system-arm.js. See
 * BROWSER_WASM_STATUS.md.
 */

let module = null;
let canvas = null;

function post(message, transfer) {
  self.postMessage(message, transfer ?? []);
}

function state(name, detail) {
  post({ type: 'state', state: name, detail });
}

function fail(code, message) {
  post({ type: 'error', code, message });
}

/**
 * Build QEMU's argv from the manifest, so board knowledge stays in the asset
 * set rather than in this file. Mirrors the native command line used in
 * IPHONE_2G_BRINGUP_HANDOFF.md:
 *
 *   -M iPhone-2G,bootrom=...,iboot=...,nand=... -m 1G -pflash nor.bin
 */
export function buildArgv(manifest, paths) {
  const options = [
    `bootrom=${paths.bootrom}`,
    `iboot=${paths.iboot}`,
    `nand=${paths.nandDir}`,
    ...(manifest.machineOptions ?? []),
  ];
  return [
    'qemu-system-arm',
    '-M', `${manifest.machine},${options.join(',')}`,
    '-m', '1G',
    '-pflash', paths.nor,
    '-serial', 'stdio',
    '-display', 'none',
  ];
}

/**
 * Place the verified bytes in the emulator's filesystem.
 *
 * The NAND pack keeps its canonical name inside a directory, because the
 * machine takes nand=<dir> and hw/arm/ipod_touch_nand.c looks for
 * <dir>/nand.pack (falling back to per-page files, which the browser never
 * ships).
 */
function mountAssets(FS, files) {
  FS.mkdir('/fw');
  FS.mkdir('/fw/nand');
  FS.writeFile('/fw/bootrom', files.bootrom);
  FS.writeFile('/fw/iboot.bin', files.iboot);
  FS.writeFile('/fw/nor.bin', files.nor);
  FS.writeFile('/fw/nand/nand.pack', files.nand);
  return {
    bootrom: '/fw/bootrom',
    iboot: '/fw/iboot.bin',
    nor: '/fw/nor.bin',
    nandDir: '/fw/nand',
  };
}

async function start({ manifest, files, canvas: offscreen }) {
  canvas = offscreen ?? null;

  state('loading-qemu');
  let factory;
  try {
    // PROVISIONAL: emitted by scripts/wasm/build-qemu.sh (-sEXPORT_ES6=1).
    ({ default: factory } = await import('../../emulator/qemu-system-arm.js'));
  } catch (error) {
    fail('WASM_INSTANTIATION_FAILED',
      'qemu-system-arm.js is missing. Run scripts/wasm/build-qemu.sh and ' +
      'copy the artifacts into web/emulator/. ' + error.message);
    return;
  }

  let paths;
  try {
    module = await factory({
      // QEMU's own main() consumes these; the module is built with
      // -sPROXY_TO_PTHREAD so main runs off this worker's event loop.
      arguments: [],
      preRun: [(runtime) => { paths = mountAssets(runtime.FS, files); }],
      print: (line) => post({ type: 'serial', line }),
      printErr: (line) => post({ type: 'serial', line }),
      onAbort: (reason) => fail('QEMU_BOOT_TIMEOUT', String(reason)),
    });
  } catch (error) {
    fail('WASM_INSTANTIATION_FAILED', error.message);
    return;
  }

  const argv = buildArgv(manifest, paths);
  state('booting', argv.join(' '));

  try {
    module.callMain(argv.slice(1));
  } catch (error) {
    // Emscripten throws ExitStatus on a clean exit.
    if (error && error.name === 'ExitStatus') {
      state('exited', `status ${error.status}`);
      return;
    }
    fail('QEMU_BOOT_TIMEOUT', String(error && error.message ? error.message : error));
    return;
  }
  state('running');
}

self.onmessage = (event) => {
  const message = event.data;
  switch (message.type) {
    case 'start':
      start(message).catch((error) => fail('QEMU_BOOT_TIMEOUT', String(error)));
      break;
    case 'input':
      // PROVISIONAL: routed once the display/input bridge exists. Until then
      // events are dropped rather than silently queued forever.
      break;
    case 'stop':
      state('idle');
      break;
    default:
      break;
  }
};

self.postMessage({ type: 'state', state: 'idle' });
