# Next session — start here: T6 (speed) then T7b (iPhone wake)

**Date:** 2026-07-26 · **Branch:** `ipod_touch_1g` · **Head:** `066df863d5`

Two problems, in the user's priority order. Both are **iPhone-only**: the
packaged **iPod Touch app works fine** and is the reference to copy from — it
idles at 11–15% CPU, its sleep/wake + slide-to-unlock work, and its guest logs
zero storage errors.

```bash
# reproduce either problem in one command each (both bundles are installed)
open -a "/Applications/iPhone 2G.app"     # slow, and P then H does not wake
open -a "/Applications/iPod Touch.app"    # the reference: fast, wakes, unlocks
```

---

## Priority 1 — T6: the iPhone app is pegged at ~98% CPU

**Symptom.** `iPhone 2G.app` burns ~a full host core at idle; `iPod Touch.app`
idles at 11–15%. Everything else works (home screen renders, touch works).

**Cause, as far as it is established.** `com.apple.AddressBook`
(`ABDatabaseDoctor`) fails and retries **~250×/s** forever. Its guest log:

```
error compiling query "SELECT value FROM _SqliteDatabaseProperties ...": no such table
error compiling query "SELECT ... FROM ABPerson;": no such table: ABPerson
error 5 creating properties table: database is locked          <-- SQLITE_BUSY
```

**What is NOT the cause** (all measured this session — do not re-chase):

| Hypothesis | Verdict |
|---|---|
| "the guest never writes; the FTL issues no page writes on a generated NAND" | **WRONG — my earlier claim.** It writes: ADM `0x500`, ~100–144 pages/boot, **including 5 SQLite JOURNAL pages**. The measurement that said otherwise counted files in a tree where every page already existed, so overwrites were invisible. |
| Writes are discarded because the model writes `<page>_new.page` and reads `<page>.page` | **Real, and fixed** — `IT_NAND_WRITABLE=1` makes writes land where reads look and shadow the pack. Not sufficient on its own. |
| The written pages carry the wrong FTL metadata | **Real, and fixed** — the ADM write path stored the spare left over from the previous READ; it now takes the spare from the guest at `data3_sec_addr`. Not sufficient either. |
| A missing `/var` skeleton | No — `mobile/Library/AddressBook` present, unchanged behaviour |
| Seeding the database (schema extracted from the firmware) | No — tried in modern SQLite format ("unsupported file format"), in legacy format (schema format 1 stamped into the header), injected after build AND written during volume construction. `--no-seed-databases` also tried. Same failure. |

**So the live question is not storage but SQLite:** why does `CREATE TABLE`
return `SQLITE_BUSY` when the writes underneath it do persist?

**Next diagnostics, cheapest first:**

1. **Is more than one `ABDatabaseDoctor` running?** Its job is `OnDemand` with a
   `MachService`; two instances would lock each other out. Count them in the
   guest log by pid.
2. **Does `fcntl` locking work on our `/var` volume?** It is a macOS-created
   **case-sensitive HFS+ (HFSX), non-journaled** image; the iPod's `/var` is a
   directory on its device-dump root volume. A locking difference would produce
   exactly `SQLITE_BUSY`.
3. **A SQLite-independent persistence test.** Does *any* file the guest creates
   survive a reboot? That separates "storage still broken" from "SQLite
   specifically unhappy", and nothing so far tests it directly.
4. **Copy the iPod's shape.** Its NAND declares **one** partition and mounts
   `/` **read-write** (`/dev/disk0s1 on / (hfs, local, noatime)`); ours mounts
   `/` read-only plus a separate `/private/var`. Building the M68AP NAND
   single-partition, iPod-style, is a big but well-lit lever — it is the
   configuration known to work.

**Stopgap the user has REJECTED (do not ship it):** dropping the AddressBook
daemon (`build-m68ap-homescreen-nand.py --drop-addressbook`) takes CPU from
98% → ~9% but costs Contacts. Keep it as a measurement, not a product.

**Tools:** `IT_NAND_WRITABLE=1` (writes readable in-session), `IT_NAND_TRACE=1`
(ADM commands: `0x200`/`0x300` read, `0x500` write), `IT_NAND_RB=1` (did the
guest read back a page it wrote), `IT_NAND_WATCH=<bank>/<page>,…` (exact-match
page read watch).

---

## Priority 2 — T7b: the iPhone does not wake (P then H does nothing)

**Symptom (user, 2026-07-26).** On `iPhone 2G.app`, press P (sleep) then H —
nothing happens. The iPod app wakes correctly from the same keys.

**Prime suspect, already evidenced — the Home button is on the wrong GPIO pin
for M68AP.** From the board's own device tree
(`m68ap-artifacts/extracted/DeviceTree.m68ap.bin`, node `buttons`,
compatible `buttons,m68`):

```
function-button_menu      GPIO 0x1600   flags 0x100     <-- Home on the iPhone
function-button_volup     GPIO 0x1601   flags 0x000
function-button_voldown   GPIO 0x1602   flags 0x000
function-button_ringerab  GPIO 0x1603   flags 0x100
function-button_hold      GPIO 0x1605   flags 0x100     <-- Power (same as iPod)
interrupts  0x2d 7  0x28 7  0x29 5  0x2a 5  0x2b 7
```

The key handler in `hw/arm/ipod_touch.c` uses `GPIO_BUTTON_HOME = 0x1606` and
`GPIO_BUTTON_HOME_IRQ = 0x2E` for **both** boards. `0x1606`/`0x2E` are the
**iPod's** home button; M68AP's menu is `0x1600` and `0x2E` is not even in its
interrupt list. So P (hold, `0x1605`) works on both, and H is delivered to a
pin the iPhone does not watch — which matches the symptom exactly.

**To fix:** make the button pin *and* its IRQ board-aware. The pin is certain;
the **IRQ mapping is not** — the DT lists five (`0x2d, 0x28, 0x29, 0x2a,
0x2b`) without an explicit pairing, and `0x2d` is the iPod's power IRQ, so
`0x2d`↔hold is the likely anchor. Determine the rest empirically (press H,
watch which IRQ the guest unmasks/acknowledges) rather than guessing; a wrong
IRQ will look like "still not waking".

**Related, already done (do not redo):** the M68AP volume pins are active-low
and idle HIGH; leaving them at 0 made the ringer/volume HUD stick permanently.
They are set at the kernel banner because iBoot samples the same port at
t=0.087 s to pick a boot mode, and presenting them released at reset panics
the kernel 3/3 (task T5).

---

## Ground rules this session earned the hard way

1. **Test the packaged bundle, not the repo build.** `lock-unlock-probe.py
   --app "/Applications/iPod Touch.app"` runs the bundle's own launcher,
   engine, firmware and bridges. A fix passed the repo build and regressed the
   app earlier today.
2. **Never test headless.** `-display none` means QEMU never calls
   `gfx_update`, so the display/touch readiness paths do not execute. The probe
   attaches a VNC refresh client by default; `--no-display-client` exists only
   for A/B against old runs.
3. **Assert at the model boundary, not on pixels.** The probe counts touch
   frames the guest actually consumed (`IT_MT_TRACE=1` → "frame consumed").
   A pixel-only verdict once scored a cycle `unlocked` with **0 frames
   consumed**.
4. **If the harness cannot observe the thing under test without changing it,
   the harness is wrong** — do not change the model to make the test work.
5. **Disk fills fast and fatally.** A NAND tree is ~300 MB packed, ~900 MB
   sparse. Clean up `/tmp/nand-*`, `/tmp/sblab-*`, `/tmp/fbsnap-*` and detach
   `hdiutil` mounts; a leaked mount pins its storage. In zsh a non-matching
   glob aborts the whole `rm` line — loop over paths instead.

## Reference documents

* `M68AP_RENDER_HANDOFF.md` — §0 task table (T1–T8), current status.
* `M68AP_HOMESCREEN_CASE_STUDY.md` — how the home screen and the touch bug were
  actually solved, including every dead end and the test-fidelity ladder.
* `IPHONE_2G_BRINGUP_HANDOFF.md` — long-form log.
* `BUILD.md` §2b — one-command packaging for both bundles.
