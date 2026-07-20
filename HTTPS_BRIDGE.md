# Transparent HTTPS bridge

## Trust store and CA injection

iPhone OS 1 reads factory anchors from
`/System/Library/Frameworks/Security.framework/TrustStore.sqlite3`. Its
SQLite `tsettings` table stores SHA-1 of the DER certificate, DER Subject
contents, a fixed empty trust-settings plist, and the DER certificate. The
original image contains 110 anchors.

`scripts/ipod-nand-trust-ca.py` reconstructs the HFSX volume from NAND pages,
inserts one exact-format anchor row, compares pre/post fsck findings, and
writes only changed pages back. It uses filesystem start VPN 206851 and maps
each VPN as bank `vpn % 8`, page `vpn // 8`. A manifest and per-page backups
make it reversible. Run it only on a staged NAND; the app installer enforces
that ordering and packs the result before changing the bundle.

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
