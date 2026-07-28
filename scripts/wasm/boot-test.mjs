// Boot an M68AP firmware under the WebAssembly build, in Node.
//
// Staging into MEMFS (rather than NODEFS) on purpose: it is what the browser
// will have to do too, so the memory cost is measured under representative
// conditions.
//
// Usage (from anywhere in the repo):
//     node --max-old-space-size=4096 scripts/wasm/boot-test.mjs \
//         [BUILD] [seconds] [logfile]
//
// IT_ICOUNT=<shift> runs with -icount, which decouples QEMU_CLOCK_VIRTUAL from
// wall clock. That matters here far more than usual: TCI is ~13x slower than
// native, so without icount the guest sees driver start() calls taking tens of
// real seconds and takes different, rarely-exercised timeout paths. See
// AGENTS.md ("Determinism -- use -icount").
import { readFileSync, appendFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { default: Module } =
  await import(pathToFileURL(`${REPO}/build-wasm/qemu-system-arm.js`).href);
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
  // IT_NAND_PACK overrides the pack, for testing a NAND that is not (yet)
  // the build directory's own -- e.g. a packaged bundle's home-screen NAND.
  ['/fw/nand/nand.pack',
   process.env.IT_NAND_PACK || `${ART}/builds/${build}/nand/nand.pack`],
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
    ...(process.env.IT_ICOUNT
        ? ['-icount', `shift=${process.env.IT_ICOUNT}`] : []),
  ],
  preRun: [(mod) => {
    mod.FS.mkdir('/fw');
    mod.FS.mkdir('/fw/nand');
    // The bank directories must exist even with a packed, read-only NAND:
    // nand_flush_buffered_page() opens <nand>/bank<N>/<page>_new.page for
    // WRITING on every guest page write and hw_error()s -- aborting the whole
    // emulator -- if the directory is missing. fb-snapshot.py and the app
    // launcher both create them; this harness did not, which aborted a boot
    // moments after it mounted the root filesystem.
    for (let bank = 0; bank < 8; bank++) mod.FS.mkdir(`/fw/nand/bank${bank}`);
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
