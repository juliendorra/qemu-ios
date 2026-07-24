# Fast scripted guest testing

The default development loop is one scripted QMP run, followed by inspection
of its JSON and screenshots. Driving one touch at a time through an agent is
slow, races iPhone OS Auto-Lock, and tends to over-test whichever screen is
currently visible.

For bringing up an emulated *device* against an unknown guest-driver
protocol (baseband, bluetooth, sensors), see `DEVICE_BRINGUP_PLAYBOOK.md`:
trace at the boundary, externalize the device brain, self-judging parallel
matrices with controls.

## HTTPS/Safari harness

`scripts/ipod-https-acceptance.py` connects to an already-running QEMU QMP
Unix socket and performs the complete wake/unlock/Safari/URL sequence locally.
It can also select the emulated Wi-Fi network on a fresh guest. Example:

```sh
scripts/ipod-https-acceptance.py \
  --qmp /private/tmp/ipod-https-qmp.sock \
  --serial /private/tmp/ipod-https-serial.log \
  --proxy-log /private/tmp/ipod-https-state/https-proof.jsonl \
  --output-dir /private/tmp/ipod-https-acceptance \
  --select-wifi \
  --case explicit=https://example.com/
```

Omit `--select-wifi` after the staged guest has associated. Add
`--sleep-wake` only when power regression coverage is wanted; it is not a
prerequisite for every networking iteration.

Each run writes `report.json` and PPM screenshots. An HTTPS case passes only
when the proxy log gains a `tls-bridge-established` event. A changed or
non-black screenshot alone is not TLS proof.

The harness also waits for the URL keyboard animation to finish and detects
whether Safari retained its alpha or symbol layout. Do not replace these
state checks with fixed per-key sleeps. Use `--keyboard-diagnostics` only when
the guest keyboard geometry itself changes.

## What the normal suite must cover

Keep these as distinct results rather than one broad “Safari works” result:

1. Wi-Fi association and link stability.
2. DHCP lease, ARP, and DNS packets.
3. Numeric HTTP and hostname HTTP.
4. HTTP to HTTPS redirect.
5. Explicit direct HTTPS.
6. HTTPS subresources.
7. Guest legacy TLS and separate verified upstream TLS packet/log evidence.
8. Home, Power, touch, and an optional sleep-to-black/wake regression.

For SDIO/card-level diagnosis, use `scripts/wifi-dev/sdio-trace-boot.py`.
When a manual investigation reveals a new guest state, coordinate, readiness
marker, or failure signature, add it to the relevant harness before rerunning
the same case.

## Firmware safety

Always boot staged NAND and NOR copies during diagnosis. CA/DNS patching,
packing, and guest acceptance happen on those copies. Updating and signing
`/Applications/iPod Touch.app` is a final step only after the development
engine suite passes.

### Why the historical self-launching harness was retired

The legacy QEMU 6 line included `scripts/ipod-acceptance-test.py`, a monolithic
sleep/wake harness that cloned NAND but passed the installed application's NOR
directly to QEMU as writable `-pflash`. That made the installed firmware copy
part of a diagnostic run and no longer satisfies this repository's staged
NAND/NOR policy.

The QEMU 11 workflow deliberately separates responsibilities instead:

1. Launch QEMU with explicit staged NAND and NOR copies.
2. Connect the reusable QMP harness to that already-running guest.
3. Run power (`--sleep-wake`), network, HTTP, and HTTPS checks as distinct
   evidence-producing cases.

This change exists to make firmware provenance and mutation boundaries
explicit, prevent accidental writes to the installed application's only NOR,
and avoid coupling every UI regression to one self-launching script. The old
scenario matrix remains documented in `SLEEP_WAKE_INVESTIGATION.md` as
historical coverage guidance, but the unsafe executable harness is intentionally
not carried onto the active branch. Extend the current QMP harness when fuller
power coverage is required.
