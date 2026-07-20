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
