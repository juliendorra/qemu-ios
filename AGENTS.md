# Repository working method

## Script tests first

For guest UI, Wi-Fi, power, browser, and HTTPS work, use a reusable scripted
test whenever possible. Do not spend one agent/LLM round trip per tap, key,
wait, screenshot, or log check.

- Put repeatable guest interaction behind QMP and run the whole case in one
  local process.
- Make tests emit machine-readable results plus screenshots and relevant log
  offsets, so failures can be diagnosed after the run.
- Manual tap-by-tap work is only for discovering a missing coordinate/state or
  investigating a failed script. Fold every useful discovery back into the
  harness before continuing repeated tests.
- Prefer state/evidence-based waits over fixed sleeps. A fixed delay is
  acceptable only when the guest exposes no observable readiness signal.
- Cover Wi-Fi association, DHCP/DNS/network traffic, HTTP, HTTPS, redirects,
  subresources, input, and power as separate cases. Do not let sleep/wake
  testing stand in for broader acceptance coverage.
- Reuse `scripts/ipod-https-acceptance.py` for Safari/HTTPS cases and
  `scripts/wifi-dev/sdio-trace-boot.py` for SDIO bring-up. Extend those scripts
  instead of recreating manual sequences.

All firmware diagnosis and scripted tests must use staged NAND/NOR copies.
Never mutate the installed app's only firmware copy while diagnosing.

## Firmware and derived-artifact policy

- Prefer reproducible construction from a user-supplied IPSW over requiring a
  physical-device dump. A dump may be used as an optional comparison oracle,
  but must not become the only supported input when a documented constructor
  is practical.
- Do not commit IPSWs, decrypted Apple images, generated NOR/NAND trees,
  device-unique data, or physical dumps. Keep only extraction/construction
  code, format documentation, hashes/manifests, and tests in the repository.
- Record provenance for every local artifact used in a result: device/build,
  source kind (IPSW or device), cryptographic hash, constructor version, and
  any guest-file modifications. Do not describe an artifact as a device dump
  without evidence.
- For S5L8900 NAND work, follow the historically proven qemu-ios approach:
  construct the sparse physical page tree and its spare/VFL/FTL/WMR metadata
  from an IPSW-derived filesystem. Treat emulated DFU/restore as a separate
  higher-fidelity track, not a prerequisite for first boot.
- Do not bypass guest storage validation or patch guest FTL reads in the
  production path. Such patches are diagnostic-only and must be called out in
  test results.
- Public project history is technical provenance, not a legal conclusion.
  Avoid asserting that an artifact or distribution is lawful; keep repository
  policy focused on user-supplied inputs and non-distribution of Apple data.
