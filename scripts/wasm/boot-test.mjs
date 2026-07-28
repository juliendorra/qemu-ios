// Boot an M68AP firmware under the WebAssembly build, in Node.
//
// Staging into MEMFS (rather than NODEFS) on purpose: it is what the browser
// will have to do too, so the memory cost is measured under representative
// conditions. Usage:
//     node --max-old-space-size=4096 boot-test.mjs [BUILD] [seconds]
// Run from build-wasm/:  node --max-old-space-size=4096 \
//     ../scripts/wasm/boot-test.mjs 4A102 300 boot.log
import Module from './qemu-system-arm.js';
import { readFileSync, appendFileSync, writeFileSync } from 'node:fs';

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const ART = `${REPO}/m68ap-artifacts`;
const build = process.argv[2] || '4A102';
const epoch = { '1A543a': 0, '1C28': 0, '3A109a': 2, '4A102': 3 }[build];
const budget = Number(process.argv[3] || 300) * 1000;
const log = process.argv[4] || 'boot-serial.log';
writeFileSync(log, '');

const files = [
  ['/fw/bootrom', `${ART}/shared/bootrom_s5l8900`],
  ['/fw/iboot.bin', `${ART}/builds/${build}/iboot-sb.bin`],
  ['/fw/nor.bin', `${ART}/builds/${build}/nor.bin`],
  ['/fw/nand/nand.pack', `${ART}/builds/${build}/nand/nand.pack`],
];

const t0 = Date.now();
const at = () => ((Date.now() - t0) / 1000).toFixed(1).padStart(6);
let lines = 0;
let staged = 0;

setTimeout(() => {
  console.log(`\n--- ${budget / 1000}s budget reached; ${lines} serial lines`);
  process.exit(0);
}, budget);

await Module({
  arguments: [
    '-M', `iPhone-2G,bootrom=/fw/bootrom,iboot=/fw/iboot.bin,` +
          `nand=/fw/nand,epoch=${epoch}`,
    '-m', '1G',
    '-pflash', '/fw/nor.bin',
    '-display', 'none',
    '-serial', 'stdio',
  ],
  preRun: [(mod) => {
    mod.FS.mkdir('/fw');
    mod.FS.mkdir('/fw/nand');
    for (const [dst, src] of files) {
      const data = readFileSync(src);
      mod.FS.writeFile(dst, data);
      staged += data.length;
    }
    console.log(`[${at()}s] staged ${(staged / 1048576).toFixed(1)} MiB ` +
                `into MEMFS for ${build}`);
  }],
  print: (t) => { lines++; appendFileSync(log, `[${at()}s] ${t}\n`); },
  printErr: (t) => {
    if (!/unsupported syscall/.test(t)) appendFileSync(log, `[${at()}s] ! ${t}\n`);
  },
});
console.log(`--- module returned at ${at()}s after ${lines} serial lines`);
