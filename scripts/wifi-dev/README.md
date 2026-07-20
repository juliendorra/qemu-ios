# Wi-Fi / SDIO development harnesses

Throwaway-but-useful tooling from the SDIO Wi-Fi bring-up. See
`../../WIFI_SDIO_NOTES.md` (findings) and `../../WIFI_SDIO_DEADENDS.md`
(attempts and dead ends).

The repository-wide rule is scripted testing first; see
`../../TESTING_WORKFLOW.md`. Interactive actions below are for discovering a
state or coordinate and should be folded into a repeatable harness before the
same sequence is run again.

All scripts assume the installed app engine at
`/Applications/iPod Touch.app`, or set `IPOD_QEMU` to a dev build.

## `sdio-trace-boot.py`

Clones a disposable NAND, boots with `IPOD_SDIO_TRACE=1`, waits for
SpringBoard plus a settle window, then prints the `[sdio]`/`[mv8686]`
trace lines from stderr. This is the primary loop for observing what the
Apple driver does at the SDIO register level.

Env: `IPOD_QEMU`, `IPOD_QMP_PORT` (use a unique port per concurrent run —
collisions leave stray QEMUs), `LOGS`, `SETTLE`, `IPOD_MV_WIFI`,
`IPOD_MV_EEPROM_FILL`, `TAPS`, and `TAP_DELAY`. For mixed timing and physical
button input, `ACTIONS_JSON` accepts tap, key, drag, and wait objects, for
example: `[{"key":"h","after":2},{"drag":[55,432,270,432]}]`.
With `INTERACTIVE=1`, the harness then accepts one action JSON object per line
on stdin until `quit`, which is useful for inspecting each generated screen.

## `dump-ram.py`

Boots, waits until the driver logs `Reading EEPROM data`, `stop`s the vCPU
and `pmemsave`s 128 MB of guest RAM (phys base 0x08000000) for offline
analysis of the kernelcache.

## `find_readeeprom.py`

Attempts to locate `AppleMRVL868x::readEEPROM` in a RAM dump by string +
literal-pool search. **Known-incomplete**: the kext uses PC-relative
`ADR` string refs, so absolute-pointer search finds the strings but not
the code. Kept as the starting point for a proper Ghidra/IDA workflow.
Mapping: kernel virtual 0xC0000000 == dump file offset 0.
