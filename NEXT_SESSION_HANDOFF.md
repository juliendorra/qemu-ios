# Next session — start here

**Date:** 2026-07-26 · **Branch:** `ipod_touch_1g`

> **2026-07-27 — the active thread is the browser/WebAssembly port.**
> Start at [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md): iPhone OS 1.0
> and 1.1.4 are packaged and measured, the Emscripten toolchain is installed,
> and the next step is finishing the wasm build of QEMU. The M68AP build system
> was reorganised the same day — every firmware now lives in
> `m68ap-artifacts/builds/<BUILD>/` and every tool takes an explicit `--build`
> ([`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md)), so commands quoted below
> need their paths translated.

> **2026-07-28 — MBX has its own handoff now.** The PowerVR MBX stub is what
> the TVOut swap-device window and the `LK_ENABLE_MBX2D=0` guest plist edit
> both stand on (tasks T1/T2). What is known, what is already ruled out, how it
> interacts with the browser port, and where to start are collected in
> [`MBX_HANDOFF.md`](MBX_HANDOFF.md). Touch across all three app bundles is
> fixed and written up in
> [`TOUCH_INVESTIGATION.md`](TOUCH_INVESTIGATION.md).

> **2026-07-28 — OPEN: on iPhone OS 1.0, Home and Power do nothing while an
> app is frontmost.** They work on SpringBoard (P sleeps, H wakes to the lock
> screen, slide-to-unlock works). Open an app and both go dead. Only surfaced
> now because touch on 1.0 was broken until today, so nobody had ever opened an
> app there; whether 1.1.4 shares it is UNVERIFIED.
>
> Reproduced headlessly (no display client), so it is not a UI artifact:
> * in-app touch still works — tapping Settings changes 97% of the frame, a row
>   inside it 35%, frames consumed each time;
> * both key events reach `ipod_touch_key_event` (`IT_KEY_TRACE=1`, two events
>   per press — down and up);
> * the POWER path completes all the way INTO the guest: `[PMU] ONKEY pressed`,
>   INT2 set, `nIRQ assert`, the guest READS INT1/INT2 (so its PMU ISR runs),
>   `nIRQ de-assert` — and the panel still never sleeps, no `[LCD] Merlot panel
>   entered sleep`;
> * it is not a slow handshake: 60 s after HOME the screen is still the app
>   (0.39% vs the in-app frame, 97% vs the home screen).
>
> So delivery and servicing are fine and the guest simply takes no action. Not
> yet done, in order: `IT_GPIO_TRACE` to confirm the Home GPIO IRQ is raised
> and acked while an app is frontmost; the same probe on 1.1.4 as a control
> (attempted twice, both runs invalid — see the disk note); and PC-sampling
> with `scripts/m68ap-freeze-probe.py` to see whether SpringBoard's button
> handling runs at all on the press. Probe used:
> `scratchpad/btnprobe.py` / `btnwait.py` pattern — drive the bundle through
> its LAUNCHER, never `nand=<bundle>/...`.
>
> **Disk hazard, active.** This machine sits at ~100% full with 1-2 GiB free.
> The iphone-2g launcher clones a 302 MB NAND per launch into `$TMPDIR` and
> only removes it on a clean exit, so every killed run leaks one. Two 1.1.4
> measurements this session were silently wrong because of it (the boot never
> reached the touch gate and every diff read 0.0%). Check
> `df -h /System/Volumes/Data` before a run and
> `rm -rf $TMPDIR/s5l8900-nand.*` after.

The sections below remain the record of the 2026-07-26 session.

Both problems the previous handoff carried are addressed in this session:

* **T6 — the iPhone app pegged a host core: SOLVED.** The generated `/var`
  was missing the OS's own directory skeleton. Idle CPU 98% → **6–10%**, and
  the guest log now contains **zero** SQLite errors (it had 15 592 AddressBook
  lines per boot).
* **T7b — the iPhone would not wake (P sleeps, H does nothing): FIXED in the
  model.** The key handler used the iPod's Home pin/IRQ on both boards.

```bash
open -a "/Applications/iPhone 2G (iOS 1.1.4).app"     # the iPhone
open -a "/Applications/iPod Touch.app"    # the reference
```

---

## T6 — what it actually was

**Not** storage, not SQLite locking, not a missing database. `/var` is a
separate HFS partition we generate ourselves, and it shipped a **hand-written
8-directory list** (`MINIMAL_DIRS` in `build-m68ap-var.py`). A real device's
`/var` is laid down by the restore ramdisk from a template that the **root
filesystem itself carries at `/private/var`** — 73 entries with their real
modes:

```
tmp (1777)  run  preferences  logs  log  db/{dyld,timezone}  Keychains
vm  msgs  empty  mobile/{Library,Media}  root/Library ...
```

Without them every daemon that writes to /var failed;
`com.apple.AddressBook` (`ABDatabaseDoctor`) failed `CREATE TABLE` with
SQLITE_BUSY and retried ~250×/s forever, which is what burned the core.

**The fix** — `scripts/build-m68ap-var.py --template-from <root image>`,
wired into the product recipe (`build-m68ap-homescreen-nand.py`). Measured on
the app's own artifacts: home screen renders (69.6% non-black on all three FB
bases), **0** `database is locked`, **0** `no such table`, idle CPU 6–10%
(the iPod idles at 11–15%). The guest even gets a sane clock now, because
`/var/db/timezone` exists.

**Ruled out along the way — do not re-chase** (each measured this session):

| Hypothesis | Verdict |
|---|---|
| The guest cannot READ the seeded database | **False.** Corrupting the file's SQLite magic *through a NAND page override* made SpringBoard report `SQLITE_CORRUPT encountered while accessing /var/mobile/Library/AddressBook/AddressBook.sqlitedb`. The bytes reach SQLite. |
| Two `ABDatabaseDoctor` instances locking each other out | **False.** Only ever pid 16 in the log. |
| The database is under the wrong `$HOME` (the real iPod keeps its under `/var/root`, because on 1.1 the daemon runs as root; on 1.1.4 it runs as `mobile`) | **False.** Adding a second copy at `/var/root/Library/AddressBook/` changed nothing. |
| The volume's case-sensitivity | **False.** Ours and the real device are both HFSX. |
| SQLite page size (guest ships 3.1.3) | **False.** The real device's own database is page-size 4096, same as ours. |

## T7b — what it was

`hw/arm/ipod_touch.c` used `GPIO_BUTTON_HOME` (0x1606) and
`GPIO_BUTTON_HOME_IRQ` (0x2E) for **both** boards. M68AP's device tree puts
`button_menu` on **0x1600** and never lists 0x2E. Power/`hold` is 0x1605 on
both boards, which is exactly why P worked and H did not.

The IRQ pairing is a **derivation, not a measurement**: N45AP fixes the rule
`IRQ = 0x28 + (pin & 0xf)` (0x1605→0x2D, 0x1606→0x2E), and applying it to
M68AP's five pins reproduces its device tree's interrupt SET exactly
(`0x2d 0x28 0x29 0x2a 0x2b`, with 0x2C absent because pin 0x1604 is unused).
So Home/menu = **0x1600 / IRQ 0x28**.

Corroborated by a second field: `interrupts` is five `(irq, trigger)` pairs
(`0x2d 7  0x28 7  0x29 5  0x2a 5  0x2b 7`), and under this pairing trigger 7
falls on every pin whose GPIO flags are 0x100 and trigger 5 on every pin whose
flags are 0x000 — a perfect split, whereas pairing by the DT's stored property
order would need `7,7,5,7,5`. Still inference, not an observed acknowledge:
`IT_M68AP_HOME_IRQ=<n>` overrides it without a rebuild, and
`IT_GPIO_TRACE=stderr` during a Home press is how to settle it outright.

---

## Tools added this session

* **`scripts/overlay-hfs-into-nand.py`** — drop an HFS partition image into a
  NAND tree as `bank<N>/<page>.page` overrides (spare copied from the pack).
  Changing `/var` and booting is now ~1 second + a boot instead of a full NAND
  rebuild. This is what made T6 tractable; use it for any "what if /var looked
  like this?" question.

  ```bash
  scripts/extract-hfs-from-nand.py "<bundle>/…/nand" /tmp/var.img \
      --active-banks 4 --partition data          # get the pristine partition
  # …edit /tmp/var.img…
  scripts/overlay-hfs-into-nand.py --nand /tmp/nand-test --image /tmp/var.img \
      --pack "<bundle>/…/nand/nand.pack" --partition data --active-banks 4
  S5L8900_STAGE_NAND=0 S5L8900_NAND=/tmp/nand-test IT_NAND_WRITABLE=1 \
      S5L8900_DEBUG=1 "/Applications/iPhone 2G (iOS 1.1.4).app/Contents/MacOS/iPod Touch"
  ```

* **`scripts/extract-hfs-from-nand.py` — two real bugs fixed.** It addressed
  pack entries as if `active_banks` were always 8, so on a generated 4-bank
  M68AP NAND it read the wrong pages and died with "invalid HFS volume
  signature"; and it refused any pack with holes, which every generated
  (sparse) NAND has. Holes now read as zeros, exactly as the QEMU model reads
  them. Both partitions of the shipped iPhone NAND now extract.

* `S5L8900_DEBUG=1` on either bundle gives serial on stdout — the cheapest
  window into the guest, and how all of the above was measured.

* **`scripts/nor-image-store.py` + `scripts/verify-boot-logo.py`** (2026-07-27,
  from the boot-logo fix). The first walks a NOR's IMG2 store the way iBoot
  does and reports **reachable** separately from **present** — a store can hold
  all seven images and expose only one. The second boots and asserts the logo
  is lit early, defaulting to the installed bundle. Both are gates
  (`--check`, non-zero exit), and `verify-boot-logo.py` was confirmed to FAIL
  on the pre-fix NOR, so neither is a check that cannot fail.

  ```bash
  python3 scripts/nor-image-store.py <nor.bin> --check --expect 7
  python3 scripts/verify-boot-logo.py --app "/Applications/iPhone 2G (iOS 1.1.4).app"
  python3 scripts/test-nor-image-store.py     # fixture tests, no Apple payloads
  ```

  Run both after regenerating any NOR or touching `ipod_touch_lcd.c`.

* **`install-iphone-firmware.py --keep-existing`** — partial firmware update:
  reuses the installed iBoot/NAND for anything not passed, so a NOR-only swap
  is one command. It also now carries the `epoch` file across, which it
  previously dropped (silently wedging a 1.0 or 1.1.1 bundle in iBoot).

## Still open

* **T1/T2** — model the MBX (swap completion + 2D) and drop
  `LK_ENABLE_MBX2D=0`.
* ~~**The M68AP kernel framebuffers stay black for a long time.**~~
  **NOT AN ISSUE — I measured the wrong artifact (corrected 2026-07-27).**
  The observation was real (110 s in, `0x0f400000` and `0x0f496000` both 0.0%
  with SpringBoard running) but it was taken against
  `m68ap-artifacts/builds/<BUILD>/nand`, the **plain** NAND, whose own
  provenance reads `"root and data HFS+ partitions placed; GPT/MBR
  synthesised"`. The three things M68AP needs to render at all — the lockdownd
  activation patch, `LK_ENABLE_MBX2D=0`, and the reference-shaped data ark —
  are applied by `build-m68ap-homescreen-nand.py`, which only
  `package-iphone-app.sh` runs. The plain NAND was never going to render.

  On the bundle, sampled every 8 s: logo at t=10 and t=18, and by **t=26 s**
  both kernel buffers are at ~74% with the screen at 69.75%. Clean handoff.

  **The general trap:** `builds/<BUILD>/nand` and the bundle's NAND are not the
  same artifact, and only the latter is expected to render. Judge anything
  about rendering on the bundle — which is ground rule 1 above, and this is
  what ignoring it looks like.
* **Bundles predate the boot-logo fix.** Both the NOR builder change and the
  LCD window change are needed; the installed `/Applications/iPhone 2G*.app`
  bundles carry neither, and their `nor.bin` must be regenerated (not just the
  engine replaced). The N45AP bundle also benefits — the iPod now shows the
  logo from ~4 s instead of ~13 s.
* **T5** — drive the M68AP button pins from reset instead of at the kernel
  banner.
* ~~Repackage the iPhone bundle~~ **done** — `/Applications/iPhone 2G (iOS 1.1.4).app`
  carries both fixes (engine + regenerated NAND) and passes
  `lock-unlock-probe.py --app` 2/2 with **47 touch frames consumed per
  cycle**. The iPod bundle was left alone: the engine change is
  board-conditional and nothing in it touches N45AP.

## Ground rules (carried forward, all still true)

1. **Test the packaged bundle, not the repo build.**
2. **Never test headless** — `-display none` means no `gfx_update`.
3. **Assert at the model boundary, not on pixels.**
4. **If the harness cannot observe the thing under test without changing it,
   the harness is wrong.**
5. **Disk fills fast and fatally.** Clean `/tmp/nand-*`, `/tmp/sblab-*`,
   `/tmp/s5l8900-nand.*` (the launcher's per-launch clones leak when a run is
   killed) and detach `hdiutil` mounts. In zsh a non-matching glob aborts the
   whole `rm` line — loop over paths instead.

## Reference documents

* `M68AP_RENDER_HANDOFF.md` — §0 task table (T1–T8), current status.
* `M68AP_HOMESCREEN_CASE_STUDY.md` — how the home screen and the touch bug
  were solved, including every dead end.
* `IOS_1_0_BRINGUP_CASE_STUDY.md` — the 1.0/1.0.2 bring-up as *process*: the
  six fixes, the claims asserted without measuring, the hypotheses killed (the
  NAND signature among them), and the rabbit holes with the escape from each.
  Read it before touching the NAND/FTL path or hand-disassembling iBoot.
* `IPHONE_OS_1X_VERSIONS.md` — the per-build measurement matrix (format,
  epoch, iBoot, FIL signature, root FS) and the open-issue list.
* `IPHONE_2G_BRINGUP_HANDOFF.md` — long-form log.
* `BUILD.md` §2b — one-command packaging for both bundles.
