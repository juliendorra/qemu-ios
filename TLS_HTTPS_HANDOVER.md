# Handover: transparent HTTPS for iPhone OS 1 on QEMU 11

This file is a self-contained prompt for a fresh session. Paste the whole
"PROMPT" block below into a new Claude Code session started in this repo.

Repo: `/Users/julien/Documents/GitHub/qemu-ipod_touch_1g`  ·  branch: `ipod_touch_1g`

---

## PROMPT

```
Continue the S5L8900 (iPod Touch 1G / iPhone 2G) QEMU 11 project at:
/Users/julien/Documents/GitHub/qemu-ipod_touch_1g   (branch: ipod_touch_1g)

Work autonomously through investigation, implementation, build, boot-testing,
and updating the packaged app. Do not stop after merely proposing a design.

==================================================================
GOAL
==================================================================
Make direct HTTPS work transparently in iPhone OS 1 Safari. Typing
https://example.com (and: an HTTP->HTTPS redirect, and an HTTPS subresource)
must LOAD in-guest over a TLS session the guest itself validates and accepts.

Today this fails ("cannot establish a secure connection"); the HTTP bridge's
CONNECT handler returns 501 by design. Plain HTTP by hostname already works.

==================================================================
HARD CONSTRAINTS (do not violate any of these)
==================================================================
- Do NOT globally disable or weaken TLS certificate verification anywhere
  (not host-side, not in the proxy's upstream leg).
- Do NOT require the user to change any guest Settings: no manual proxy config
  in the guest, no "open 10.0.2.2 first". Direct URL entry is the acceptance
  bar.
- Do NOT claim HTTPS is solved while CONNECT still fails or while any
  verification is disabled.
- Ship NO secret to the guest and commit NO secret. The guest receives only a
  CA *certificate*; the CA *private key* and the proxy stay host-side and are
  generated per-install, never committed.
- Do NOT touch the installed app's only firmware copy during diagnosis. Use a
  staged/copied NAND tree.
- Never run `git clean` or a destructive reset. Preserve the untracked
  user-owned leftovers: roms/* submodule dirs, build-release/, capstone/,
  dtc/, meson/, slirp/, tests/fp/berkeley-*/, ui/keycodemapdb/. Never stage
  them. Keep commits atomic.

==================================================================
WHAT ALREADY WORKS — do not redo or regress
==================================================================
- MV8686 Wi-Fi association, DHCP (guest gets 10.0.2.15, router/DNS 10.0.2.3),
  ARP, numeric-address HTTP, and hostname HTTP in Safari.
- DNS: the guest mDNSResponder job was missing from the shipped rootfs; it is
  restored by a reproducible BUNDLE-TIME NAND patch. Read DNS_RESOLVER_NOTES.md
  for the root cause, the fix, and the already-documented HTTPS boundary.
- HTTP forward-proxy / bridge (scripts/ipod-http-bridge.py): the guest speaks
  PLAIN HTTP to the host and the host performs the upstream TLS. This
  "TLS-termination bridge" already works — precisely because Safari validates
  no certificate on the guest<->bridge leg. Default bridge port is 18080; it
  also serves /proxy.pac and a mobile-optimised landing page.
- Sleep/wake: a sleeping device shows a BLACK screen (not a battery image) and
  Home/Power wakes it. Read SLEEP_BATTERY_SCREEN_FIX.md. Your changes must not
  regress this.

==================================================================
THE PROVEN TLS BOUNDARY (why the tempting shortcuts fail)
==================================================================
A TLS server must PROVE possession of the private key for the certificate it
presents (it signs the handshake / decrypts the premaster). Therefore:
- You cannot reuse a real site's 2007 certificate: its private key was never
  public and is unobtainable. A certificate without its key authenticates
  nothing.
- You cannot mint a certificate signed by a real 2007 CA (VeriSign, Thawte,
  etc.): you do not hold the CA private key. Controlling the fake access point
  does NOT defeat certificate validation.
- Negotiating old protocol versions (TLS 1.0) is fine on the wire; the
  CERTIFICATE TRUST CHAIN is the wall, not the protocol version.

==================================================================
THE ONE VIABLE APPROACH — implement this
==================================================================
1. Generate a bridge ROOT CA on the host (private key never leaves host; keep
   it out of git; generate per-install).

2. Inject that CA CERTIFICATE into the GUEST trust store via a reproducible,
   reversible BUNDLE-TIME NAND patch. Model it EXACTLY on the existing DNS fix
   tool: scripts/ipod-nand-restore-dns.py. That tool already demonstrates the
   whole mechanism you need:
     - reconstruct the HFSX volume from NAND pages
       (FILESYSTEM_START_VPN = 206851; page path = bank{vpn%8}/{vpn//8}.page;
       see include/hw/arm/ipod_touch_nand.h),
     - hdiutil-attach it, edit files, fsck it,
     - write only the changed logical pages back,
     - write a manifest + per-page backups for reversibility.
   After patching, rebuild the immutable base with scripts/pack-ipod-nand.py.

   INVESTIGATE FIRST, do not guess: find WHERE iPhone OS 1's TLS stack
   (Security.framework / CFNetwork) reads its X.509 trust anchors, and in what
   FORMAT. Leads:
     - reconstruct + mount the volume READ-ONLY and search
       /System/Library/Keychains/ and /System/Library/Frameworks/
       Security.framework for an anchors keychain or a certs plist,
     - the firmware already ships certs such as
       /System/Library/Lockdown/iPhoneDebug.pem — trace how those are loaded,
     - confirm DER vs PEM vs a keychain (SQLite/DB) blob before writing.
   Prove the store you patch is the one the TLS handshake actually consults
   (e.g. by adding a throwaway CA and observing acceptance), before wiring it
   into the bundle.

3. Stand up a MITM proxy (mitmproxy, squid ssl-bump, or nginx) that, per
   requested host, mints a leaf certificate signed by the bridge CA and
   performs the real MODERN-TLS fetch upstream (with normal verification).
   The GUEST-FACING listener MUST speak the down-level TLS the 2007 stack
   negotiates: SSL3/TLS1.0, legacy ciphers (RSA key exchange, RC4/3DES). Most
   host TLS libs disable these by default — you will likely need a custom
   OpenSSL context / cipher string, or a legacy-enabled build, for the
   guest-facing side only. The upstream side stays modern and verified.

4. CLOCK / VALIDITY: the leaf cert's notBefore/notAfter must bracket the guest
   clock. The guest currently boots believing it is ~2026-07-20 (visible on the
   lock screen), so "now" validity should work — but VERIFY the guest clock and
   backdate notBefore if the guest disagrees. Also ensure the injected ROOT CA
   validity spans the guest clock. This is a real failure mode; test it
   explicitly.

5. TRANSPARENT INTERCEPTION: route guest TCP :443 to the proxy inside the
   controlled QEMU/libslirp network path so no guest proxy setting is needed
   (the same transparent-intercept idea the project already floats for :80).
   Preserve SNI / Host and the original destination so the proxy fetches the
   right upstream. Do NOT silently intercept or log credentials.

==================================================================
INTEGRATION INTO THE APP BUNDLE
==================================================================
The CA-injection step must run automatically as part of staging the app's
firmware (parallel to the DNS patch), operate on COPIED firmware, be
documented, deterministic, and reversible (manifest + backups). Only the CA
certificate reaches the guest. The proxy launches alongside the existing
bridge (see scripts/ipod-app-launcher.sh) and must not collide on ports.

==================================================================
BUILD REALITY (read before trusting any binary)
==================================================================
- The main worktree IS QEMU 11.0.2 source (VERSION = 11.0.2).
- A working QEMU 11 build now exists at build-ipod11/qemu-system-arm
  (11.0.2, machine iPod-Touch). To rebuild after editing hw/arm/*.c:
    cd build-ipod11 && PYTHONPATH="$PWD/../.build-pydeps" ninja qemu-system-arm
  Full offline reconstitution recipe (if build-ipod11/ is ever lost) is in
  BUILDING.md. Key gotchas it documents: vendor tomli into .build-pydeps and
  pass it via PYTHONPATH (only python3.9 exists here — there is NO python3.11+);
  populate subprojects/dtc from top-level dtc/; extract keycodemapdb rev
  f5772a62 from .git/modules/ui/keycodemapdb (the submodule is on an old rev).
- Do NOT build under /private/tmp (it gets purged — that is how the previous
  build tree was lost). The stale QEMU-6.2 build/ has been removed;
  build-release/ is a preserve-listed 6.2 leftover — do not use it for testing.
- The installed app (/Applications/iPod Touch.app) currently runs a QEMU 11
  engine dated 2026-07-20 13:04 and boots to SpringBoard from nand.pack.

==================================================================
ACCEPTANCE TESTS (boot the real build; then repeat from the packaged app)
==================================================================
A. Regression — all must still pass:
   Wi-Fi assoc, DHCP lease, ARP, numeric-address HTTP, hostname HTTP, DNS
   request/response packets present, sleep -> BLACK screen, Home, Power, touch,
   and the existing Wi-Fi harness.
B. HTTPS:
   1. Direct https://example.com loads in Safari by hostname; TLS accepted.
   2. Test a second genuinely-HTTPS host.
   3. An HTTP->HTTPS redirect follows through to a rendered HTTPS page.
   4. An HTTPS subresource on an HTTP page loads.
   5. Packet capture proves: guest completed a TLS handshake to the proxy, AND
      the proxy completed a SEPARATE modern verified TLS session upstream.
C. Report honestly, per case (HTTP, HTTP->HTTPS redirect, explicit HTTPS,
   HTTPS subresource). If a case fails, say so with the evidence — do not round
   up.

==================================================================
PACKAGING (only after guest tests pass)
==================================================================
Rebuild nand.pack, update /Applications/iPod Touch.app via
scripts/install-ipod-app-engine.sh, code-sign, relaunch the INSTALLED app, and
re-run the acceptance tests from the bundle. Confirm no stale process holds the
bridge/proxy ports.

==================================================================
FINAL REPORT MUST STATE
==================================================================
- Which guest trust store you patched and its format.
- The CA-injection method (and how it stays reversible / secret-free).
- The proxy design and how legacy guest-facing TLS was enabled.
- Clock/validity handling.
- Packet-level proof of both TLS legs.
- Exact https:// URLs that loaded.
- Honest per-case HTTPS results.
- iPod regression results.
- Commits created and remaining risks.
```

---

## Quick reference for the next session (facts, not part of the prompt)

- DNS-patch template: `scripts/ipod-nand-restore-dns.py` (mount → edit → fsck →
  write-back → manifest + backups). Key functions: `reconstruct_volume`,
  `attach`, `fix_catalog_ownership`, `fsck_complaints`, `write_back`.
- NAND geometry: `include/hw/arm/ipod_touch_nand.h` —
  `FILESYSTEM_START_VPN 206851`, `FILESYSTEM_NUM_PAGES 132854`, 8 banks,
  2048-byte pages; logical page → `bank{vpn%8}/{vpn//8}.page`.
- Pack builder: `scripts/pack-ipod-nand.py` → `nand.pack` (immutable base; the
  emulator prefers it over loose pages — see `hw/arm/ipod_touch_nand.c`,
  `nand_read_packed_page`).
- Bridge/proxy: `scripts/ipod-http-bridge.py` (port 18080; `/proxy.pac`;
  CONNECT currently 501). Launcher: `scripts/ipod-app-launcher.sh`.
- Docs to read: `DNS_RESOLVER_NOTES.md` (DNS fix + HTTPS boundary),
  `SLEEP_BATTERY_SCREEN_FIX.md` (keep sleep black), `WIFI_SDIO_NOTES.md`,
  `WIFI_SDIO_DEADENDS.md`.
- Firmware cert already present: `/System/Library/Lockdown/iPhoneDebug.pem`
  (a lead for how the guest loads certs — not necessarily the TLS anchor
  store).
