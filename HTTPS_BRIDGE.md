# Transparent HTTPS bridge

## Trust store and CA injection

iPhone OS 1 reads factory anchors from
`/System/Library/Frameworks/Security.framework/TrustStore.sqlite3`. Its
SQLite `tsettings` table stores SHA-1 of the DER certificate, DER Subject
contents, a fixed empty trust-settings plist, and the DER certificate. The
original image contains 110 anchors.

`scripts/guest_trust_store.py` owns the guest-side half: what an anchor row
looks like, and how to add one to a mounted root filesystem. It asserts the
exact `CREATE TABLE` text before writing, so a firmware with a different
Security.framework fails loudly instead of getting a row it will not read.
Both devices ship the same store — 1A543a (1.0) and 4A102 (1.1.4) each have
the identical schema and the same 110 factory rows as the iPod's N45AP image.

The two boards reach that store by different routes, because their NANDs come
from different places:

* **N45AP (iPod)** — `scripts/ipod-nand-trust-ca.py`. The iPod ships a real
  device dump that cannot be regenerated, so the tool reconstructs the HFSX
  volume from NAND pages, inserts the row, compares pre/post fsck findings,
  and writes only changed pages back. It uses filesystem start VPN 206851 and
  maps each VPN as bank `vpn % 8`, page `vpn // 8`. A manifest and per-page
  backups make it reversible. Run it only on a staged NAND;
  `install-ipod-app-engine.sh` enforces that ordering and packs the result
  before changing the bundle.
* **M68AP (iPhone)** — `scripts/build-m68ap-homescreen-nand.py`, step 3/5. The
  iPhone's NAND is *built* on every packaging run, so the row goes in while the
  root filesystem is still an ordinary mounted image, next to the activation
  patch and the `LK_ENABLE_MBX2D=0` edit. None of the iPod's page-level
  reconstruction applies (and would not work: an M68AP NAND has the real
  two-partition root+data layout). `--no-bridge-ca` opts out; `--ca-cert` and
  `--https-state` override where the certificate comes from.

**The CA is per host and per profile**, and the M68AP path bakes it in at NAND
*generation* time rather than at engine-install time. That is sound because for
the iPhone the two are the same moment: `package-iphone-app.sh` regenerates the
NAND every run, and it takes the CA from the same state directory the launcher
will use (`.../S5L8900 HTTPS Bridge/iphone-2g`), reusing an existing CA rather
than minting a new one. The consequence is that an M68AP bundle is only fully
functional on the machine that packaged it: copied elsewhere, HTTPS through the
bridge fails closed (Safari refuses the leaf) — re-run `package-iphone-app.sh`
there. `package-iphone-app.sh` verifies both halves of this, checking that the
shipped `nand.pack` contains a bridge CA at all and that its sha256 (recorded
in `nand-provenance.json` as `recipe.bridge_ca_sha256`) is this host's.

**Every future iPhone packaging run gets this automatically.** Injection is the
default in `build-m68ap-homescreen-nand.py` (`--no-bridge-ca` is an opt-out, and
`package-iphone-app.sh` never passes it), and it is enforced rather than merely
attempted: the two checks below fail packaging outright — `exit 1`,
"packaging INCOMPLETE" — so a bundle cannot silently ship without the trust, not
even when a NAND built elsewhere is supplied with `--nand`.

```
  ok   guest trusts a bridge CA          # the pack contains a bridge root
  ok   trusted CA is this host's         # ...and its sha256 is this host's
```

The one way to end up mismatched is to delete the state directory (a fresh CA
is then minted while the shipped NAND still trusts the old one). Packaging
catches it; the launcher does not, so the symptom would be certificate errors
in Safari. `scripts/package-iphone-app.sh --verify-only` diagnoses it in a
second, and re-packaging fixes it.

The per-install CA and reusable RSA leaf key live by default under
`~/Library/Application Support/S5L8900 HTTPS Bridge/<profile>/`. Private keys
are mode 0600 and never enter the repository, app bundle, or guest. Only the
public `bridge-ca.der` is injected. CA and leaf validity is fixed at
2020-01-01 through 2045-12-31, bracketing the guest's observed July 2026
clock.

## Network design

QEMU observes DNS responses, preserves the originally requested hostname
across CNAME chains, and records an address-to-hostname slot. A guest TCP SYN
to destination port 443 is NATed to that slot's local proxy port, with a
metadata-only UDP notification sent first. No guest proxy setting is used and
Safari need not send SNI.

`scripts/ipod-https-proxy.py` binds only 127.0.0.1. Its guest leg permits the
SSLv3/TLS 1.0 RSA suites offered by SecureTransport and presents a per-host
leaf signed by the injected CA. Its upstream leg uses
`ssl.create_default_context()`, `CERT_REQUIRED`, and hostname checking. Proof
logs contain TLS metadata only—never HTTP payloads, URLs, headers, cookies, or
credentials. The launcher prints an interception warning on every start.

## Scripted validation

Use `scripts/ipod-https-acceptance.py`; do not drive repeated taps through an
agent. It waits for the fully settled Go key, detects Safari's retained
alpha/symbol layout, handles first-use Wi-Fi sheets, and emits JSON plus
screenshots. Representative cases are:

```sh
scripts/ipod-https-acceptance.py \
  --qmp /private/tmp/ipod-https-qmp.sock \
  --serial /private/tmp/ipod-serial.log \
  --proxy-log /private/tmp/https-proof.jsonl \
  --http-log /private/tmp/http-proof.jsonl \
  --case numeric-http=http://10.0.2.2:18080/ \
  --case hostname-http=http://www.example.com/ \
  --case explicit-https=https://www.example.com/ \
  --case redirect=http://10.0.2.2:18080/redirect \
  --require-tls redirect \
  --case https-subresource=https://www.wikipedia.org/ \
  --min-tls https-subresource=2
```

The deterministic redirect targets `https://example.com/`, distinct from the
explicit `www.example.com` test. Wikipedia exercises a CNAME and loads its
HTTPS puzzle-globe image; two established connections are required for that
case. Packet capture should independently assert DHCP, ARP, DNS, and guest
TCP/TLS traffic.

## Limitations and safety

This is an intentional local MITM for a controlled emulator. Do not enter
credentials unless interception is intended. Address-to-hostname slots are
bounded and DNS-driven; an application connecting to a bare HTTPS IP without
a preceding hostname lookup cannot receive a hostname-valid leaf. The HTTP
bridge's CONNECT response remains 501 by design.
