# iPhone OS 1 DNS resolution: root cause, fix, and the HTTPS boundary

Status: **fixed and verified** on QEMU 11 (`ipod_touch_1g`), July 20 2026.
Companion documents: `WIFI_SDIO_NOTES.md` (transport bring-up) and
`scripts/ipod-nand-restore-dns.py` (the fix tool).

## Symptom

With Wi-Fi association, DHCP (10.0.2.15), ARP, and numeric-address HTTP all
working, entering any hostname in Safari failed *instantly* with "can't find
server", and packet captures showed **no DNS request ever left the guest**.
Experiments with libslirp DNS addresses, DHCP option 15, and WPAD/option 252
were dead ends: the failure was local to the guest.

## Root cause

The distributed n45ap NAND image was pruned by its original author and no
longer contains the launchd job for the system resolver daemon.

Evidence trail:

- A RAM dump of a normally booted guest contains launchd's `SubmitJob`
  buffers (two copies, at 0xefb048 and 0x171c048). Exactly six jobs were
  submitted, in ASCII order: `com.apple.AddressBook`, `com.apple.CommCenter`,
  `com.apple.SpringBoard`, `com.apple.configd`, `com.apple.mobile.lockdown`,
  `com.apple.notifyd`. No resolver job.
- The only `com.apple.mDNSResponder` strings in RAM come from the
  DNSServiceDiscovery *client library* (`DNSServiceDiscovery.c`); no
  daemon-unique string (`mDNSResponder-118`, `daemon.c`) is present.
- Reconstructing the guest HFSX volume from the NAND (linear mapping,
  `vpn = page*8 + bank`, filesystem base vpn 206851 — matching
  `FILESYSTEM_START_VPN` in `ipod_touch_nand.h`) and mounting it shows the
  live `/System/Library/LaunchDaemons/` holds only those six plists.
- HFSX free space still contains the original sixteen-plist directory,
  the full text of the deleted plists, and **Finder Trash "put back"
  records** (`ptbL`/`ptbN` DS_Store entries) for all sixteen — the image
  author moved them to the Trash in macOS Finder and restored only six.
  The author's surviving edits are dated March–April 2022.
- `/usr/sbin/mDNSResponder` (the daemon binary) is still present and intact.

Why this kills all hostname lookups: on iPhone OS 1 (Leopard-era libinfo),
`getaddrinfo()`/`gethostbyname()` route unicast DNS through the
mDNSResponder daemon via its `com.apple.mDNSResponder` Mach service and
`/var/run/mDNSResponder` socket. With no daemon, resolution fails
immediately inside the guest — hence zero packets on the wire. (The
`/etc/resolv.conf` in the image even carries a 2022-era `1.1.1.1/8.8.8.8`
hack by the image author; it is not consulted by this resolution path.)

## Fix

`scripts/ipod-nand-restore-dns.py --nand <nand-tree>` restores
`com.apple.mDNSResponder.plist` (780 bytes, byte-identical to the Apple
original recovered from the image's own free space; sha256
`73b9fc5f0eece763b988ac885d3c71a2063589ff1667030a63b7921856e88f9f`):

1. rebuilds the volume from the NAND pages into a temp image,
2. mounts a staged copy read-write with `hdiutil` (Spotlight disabled),
3. writes the plist into `/System/Library/LaunchDaemons/`,
4. rewrites the new catalog record's BSD ownership to root:wheel 0644 in
   the raw image (launchctl skips "dubious" non-root plists, and hdiutil
   mounts without honoring owners),
5. verifies `fsck_hfs` complaints are identical to the pristine baseline
   (the 2007 mkfs predates folder counts, so a warning-free run is not
   attainable; the gate is "no *new* complaints"),
6. writes back only the changed 2048-byte pages (18 pages), with a
   manifest and page backups; `--revert` restores the original state.

Guest writes never modify the base pages (the emulator writes `*_new.page`
files it does not read back), so the patch is stable across guest boots.
The dev NAND at `build/ipod_files/nand` and the app bundle's `nand.pack`
must both be patched (the pack is rebuilt with `scripts/pack-ipod-nand.py`).

A related emulator fix landed alongside: restoring mDNSResponder adds
background network traffic, which makes the Wi-Fi driver's idle-lock
deep-sleep path race into its `invokeTheHandOfGod` recovery. The MV8686
model now survives that power-cycle (reset on inquiry CMD5, EEPROM stage
skipped on warm reloads), so recovery completes instead of wedging the
driver workloop. See commit "Recover the MV8686 from AppleMRVL868x
power-cycle resets".

## Verification (QEMU 11, patched NAND, 2026-07-20)

- Serial: `mDNSResponder-118 (Sep 6 2007 23:52:18)[12]: starting`.
- Safari loads `http://example.com/` **by hostname** — first DNS packets
  ever observed from the guest:
  - TX: `10.0.2.15:5353 → 10.0.2.3:53`, standard UDP DNS query
    (source port 5353 is mDNSResponder's unicast query socket),
  - RX: matching response (ID `0x2c16`, flags `0x8180`), page data follows.
- `neverssl.com` resolves, loads, and follows its HTTP redirect to a
  random subdomain (a second, uncached lookup). `www.google.com` also
  renders over plain HTTP.
- Numeric URLs still work (`http://10.0.2.2:18080/`), the responsive
  bridge page renders in guest Safari, and the `/fetch` bridge mode
  delivers HTTPS-origin content over HTTP.
- Sleep/wake (Power), Home, touch, and unlock all behave; after a
  deep-sleep watchdog recovery the firmware warm-reloads and hostname
  browsing continues to work.
- Auto-Lock was set to Never during interactive testing; with the default
  1-minute Auto-Lock the deep-sleep watchdog can still fire (see above) —
  it now recovers cleanly.

## HTTPS boundary and transparent bridge

- **Plain HTTP by hostname and numeric address works** (the DNS fix above).
- **Direct HTTPS, redirects, and HTTPS subresources work through the
  transparent compatibility bridge.** The bundle installer generates a
  private per-install CA outside the app, injects only its public certificate
  into a staged iPhone OS trust store, and then packs/installs that NAND.
- QEMU observes DNS A/CNAME results and transparently redirects guest TCP
  destination port 443 to a local-only legacy TLS listener. The guest still
  performs normal certificate validation against its patched trust store.
  The proxy creates a separate upstream connection with Python's default
  `CERT_REQUIRED` and hostname checking; verification is never disabled.
- The old HTTP bridge remains available on port 18080. Its `CONNECT` method
  deliberately remains 501 because direct HTTPS uses the transparent QEMU
  path, not guest proxy settings.
- See `HTTPS_BRIDGE.md` for trust-store format, validity dates, safety model,
  integration, tests, and current limitations.
- iPhone OS 1 requested DHCP option 252 (WPAD) and received it in
  Offer/Ack during earlier experiments, but Safari never fetched the PAC;
  those DHCP experiments were reverted and should not be retried without
  new evidence.

## Future direction: transparent HTTP interception

Now that DNS works, ordinary `http://` browsing goes direct through slirp.
A possible next step is transparently steering guest TCP port 80 through
the host bridge inside the emulator's network path (preserving Host
headers and destination semantics) so modern-web conveniences (retries,
redirects to HTTP-capable mirrors) apply without any guest settings. Port 80
interception remains separate from the implemented port 443 bridge.
