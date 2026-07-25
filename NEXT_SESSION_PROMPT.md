# Prompt for the next session

Paste everything below the line into a fresh session.

---

Continue the iPhone 2G (M68AP) bring-up in this repo. **Read
`M68AP_RENDER_HANDOFF.md` first** — it is the focused handoff: current status,
what is already ruled out *with the measurement that did it*, the tools that
exist with runnable commands, the reproduction recipe, ranked next steps, and
the traps. `IPHONE_2G_BRINGUP_HANDOFF.md` is the long-form log if you need the
detail behind any line of it.

## The goal

M68AP boots iPhone OS 1.1.4 to SpringBoard, reports `[Activated]`, and has the
WiFi driver up — but the screen stays black: SpringBoard logs two lockdown
lines and then never programs a kernel framebuffer base, while the guest PC
parks in the kernel wait-for-interrupt idle loop. That is a **blocked wait**,
not a crash or a spin. Find what it waits on and make M68AP render its home
screen. The end goal after that is the WiFi→Safari proof
(`scripts/ipod-https-acceptance.py --select-wifi`), which is blocked only by
the black screen.

Already eliminated by measurement — **do not re-chase without new evidence**:
activation, the baseband, an empty `/var`, board-specific display code. A
*full* `/var` skeleton is actively harmful (launchd never starts).

## How I want you to work

**1. Parallelize with scripts. Do not drive experiments by hand.**

`scripts/springboard-lab.py` already boots N configurations concurrently
(staggered), self-judges each (`rendered` / `wedged` / `crawling` / `panicked` /
`timeout`), and collects PC histograms, LCD bases, framebuffer non-black %,
boot phase, SpringBoard lines and a normalised full-log `--diff A=B`.
Hypotheses live in its `VARIANTS` table as **data**, not code.

- Add each new hypothesis as a **variant or a flag**, then run a matrix — never
  a one-off manual boot followed by eyeballing a log.
- If a question needs evidence the lab cannot yet produce, **extend the lab or
  write a new script for it** (e.g. an `IT_FB_TRACE` in the LCD/CoreSurface
  path, a launchd job-set differ, a SpringBoard disassembly helper). The script
  is the deliverable; the answer is its output.
- Always include the **N45AP control** in the matrix — it renders, M68AP does
  not, and the difference between them is the signal.
- Reuse `scripts/lab_workspace.py` for anything that creates artifacts:
  pre-flight `require_free_bytes`, `Workspace` disposal, `prune_runs`,
  leak-proof `attached()` mounts. A previous session filled the disk fatally;
  cleanup is on by default and must stay that way.
- Respect the traps in the handoff: stagger boots (host contention flips the
  `IOIpodUSBDevice::start` race), build artifacts serially, repeat any
  `panicked` seat once before believing it.

**2. Advance by building the least hacky solution that actually works.**

- Prefer the **authentic mechanism** over a bypass: how does the real device —
  or the working N45AP path — do this? Check the reference before reaching for
  a patch (`ipod-nand-restore-dns.py` and N45AP's own NAND are the templates).
- Prefer **data over code patches**: a file the system already reads
  (preferences, a plist, a device-tree value) beats editing a binary.
- If a binary patch is genuinely unavoidable, patch **one authority**, not each
  consumer — and record *why* it was unavoidable, with the measurement that
  proves the cleaner route fails. Activation is the worked example: data ark
  first, one `lockdownd` patch only after measuring that no data-only ark
  survives re-validation.
- Prefer **emulator-side correctness** over guest-side hacks: if a device model
  is wrong (as the UART interrupt semantics were), fixing the model is the real
  solution and usually fixes several symptoms at once.
- When you take a shortcut, mark it clearly as a shortcut, keep it in one
  place, and write down what the non-hacky version would be.

**3. Document and commit as you go.**

Update `M68AP_RENDER_HANDOFF.md` (and the long-form log when relevant) with
findings, dead ends, corrected hypotheses and successes **as they happen**, and
commit docs alongside code — earlier sessions circled back onto already-ruled-out
ground when this was skipped. Record negative results as prominently as positive
ones: "X is not the blocker, here is the measurement" is valuable output.

**4. Report honestly.**

If something does not work, say so plainly with the evidence. Do not describe a
partial result as a success, and do not assert a fix without a run that shows
it. Verified > plausible.

## Suggested starting point

The handoff ranks the avenues. The cheapest high-information one is
**emulator-side tracing of the display conversation**: add an `IT_FB_TRACE` to
the LCD / `IOMobileFramebuffer` / CoreSurface path that logs every register and
mapping touch after SpringBoard starts, then run the lab with `n45ap-control`
and `m68ap-full` and diff the two traces. The emulator sees both sides of that
conversation, and the boards must diverge somewhere in it.

Second cheapest: diff the **launchd job sets** of the two root filesystems, and
test the **Zephyr1 multitouch** difference (`mt->zephyr1` is set only for
M68AP in `hw/arm/ipod_touch.c`) — if SpringBoard waits on a touchscreen-ready
event the Z1 path never delivers, it would look exactly like this wait.
