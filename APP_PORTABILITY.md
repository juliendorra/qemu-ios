# App portability — can you just copy the .app to another Mac?

Goal: copy `iPod Touch.app` or an `iPhone 2G (…).app` to another Apple-silicon
Mac and have it work, with no repo, no build toolchain, no Homebrew.

**Status (2026-07-28): the emulator is portable. The HTTPS bridge is not.**
Copy the app to a clean Mac and the device boots and browses HTTP; HTTPS sites
that need the local TLS bridge fail. Two host tool dependencies are the reason,
and both are listed below with what a fix would take.

Everything here was measured on this machine, not assumed. Re-check with the
commands quoted before acting on any of it.

---

## Fixed

### The bridge CA used to be host-bound — now it ships in the bundle

The guest's trust store carries the CA of whichever machine packaged the app
(see [HTTPS_BRIDGE.md](HTTPS_BRIDGE.md)). While the CA was generated per host
and never left `~/Library/Application Support/…`, a copied bundle's bridge
signed with a CA the guest had never heard of, and every HTTPS site failed.
"Re-run the packaging script there" was not a real answer: it needs the repo,
the IPSW-derived artifacts and a build toolchain.

Fixed by shipping the CA — **private key included** — at
`Contents/Resources/https-bridge-ca/`. `install-ipod-app-engine.sh` writes it
for both profiles; `ipod-app-launcher.sh` seeds its state directory from the
bundle on every start and wipes the cached leaves when the CA changes, so the
bundle's CA always wins over anything the host generated. Verified on all three
bundles:

```
iPod Touch             CA in bundle (key 600) sha=c566338443da
iPhone 2G (iOS 1.1.4)  CA in bundle (key 600) sha=10f5b49bc533
iPhone 2G (iOS 1.0)    CA in bundle (key 600) sha=10f5b49bc533
```

**Why shipping a CA private key is acceptable here, and what must keep holding
for that to stay true.** This root is trusted only by the emulated guest's
`TrustStore.sqlite3`; macOS does not trust it. `ipod-https-proxy.py` binds
`127.0.0.1` only. So whoever extracts the key can intercept the emulator's
traffic on a machine they already control, and nothing else. Treat the key as
public. **Never add `bridge-ca.pem` to a macOS keychain** — that is the one
action that would turn a shared, extractable key into a real MITM risk, and it
is why the bundle carries a `README.txt` saying so.

### Already clean (checked, no work needed)

* **No host paths in the engine or dylibs.** `otool -L` on
  `Contents/MacOS/qemu-system-arm` and on every `Contents/Frameworks/*.dylib`
  shows only `@executable_path/../Frameworks`, `/usr/lib` and
  `/System/Library`. `install-ipod-app-engine.sh` bundles Homebrew dependencies
  recursively and *fails packaging* if any `/opt/homebrew` path survives.
* **`pc-bios` ships in the bundle** and the launcher passes
  `-L "$RESOURCES/pc-bios"`.
* **No absolute host paths in the launcher.** Everything derives from `$0`.
* **arm64** — fine on any M-series Mac.

---

## Blocker 1 — OpenSSL 3 (hard; breaks HTTPS on a clean Mac)

`ipod_tls_common.find_openssl()` requires `x509 -not_before` / `-not_after`.
Those flags are not cosmetic: leaf certificates must carry a 2020-01-01 →
2045-12-31 validity window to bracket the guest's 2007-era clock, and `-days`
(which LibreSSL does have) starts the window *now*.

macOS ships **LibreSSL**, which has neither flag:

```
$ /usr/bin/openssl version
LibreSSL 3.3.6
$ /usr/bin/openssl x509 -help 2>&1 | grep -c -- -not_before
0
```

The only candidate `find_openssl()` accepts is Homebrew's `openssl@3` — a
developer install. On a Mac without it the bridge raises at start, the launcher
catches it and boots the device *without* HTTPS.

**Bundling the CA did not fix this.** The CA is no longer generated on the
target machine, but every per-host *leaf* still is, on demand, per hostname.

**Fix, in order of preference:**

1. **Bundle `openssl` plus its dylibs**, exactly the way the engine's Homebrew
   dependencies are already bundled — `bundle_dependencies()` in
   `install-ipod-app-engine.sh` is a recursive `otool -L` + `install_name_tool`
   walk that would work unchanged on the `openssl` binary. Then point
   `find_openssl()` at `Contents/Frameworks/openssl` (via the existing
   `S5L8900_OPENSSL` environment variable, which the launcher can export) ahead
   of the Homebrew path. Smallest change, no new failure modes, ~5 MB.
2. Mint leaves in pure Python (DER + RSA signing by hand). No dependency at
   all, but real work and real risk in code that signs certificates.
3. Keep `-days` and shift the guest clock instead. Rejected: the guest clock is
   load-bearing elsewhere (firmware epochs, activation), and this trades a
   contained dependency for a global behaviour change.

## Blocker 2 — python3 (hard; breaks BOTH bridges on a clean Mac)

`ipod-http-bridge.py` and `ipod-https-proxy.py` are Python. macOS has not
shipped a Python interpreter since 12.3 — `/usr/bin/python3` is a Command Line
Tools *shim* that, on a Mac without Xcode CLT, pops the developer-tools
installer instead of running.

The launcher's guard does not catch this, because the shim exists:

```sh
command -v python3 >/dev/null 2>&1   # true even when no interpreter is installed
```

So on a clean Mac the launcher proceeds, both bridges die, and the user may get
an unexpected system dialog. The emulator itself still runs.

**Fix:** bundling a Python runtime is heavy (~40 MB and its own signing
problems). The cheaper direction is to make the guard honest — probe
`python3 -c 'pass'` rather than `command -v` — so the app degrades quietly and
says why, and then decide whether the bridges are worth a compiled helper.

## Blocker 3 — Gatekeeper (friction, not breakage)

The bundles are ad-hoc signed (`codesign -s -`). A copy that travels over the
network or AirDrop carries `com.apple.quarantine` and is refused with
"the developer cannot be verified". The recipient must right-click → Open once,
or run:

```bash
xattr -dr com.apple.quarantine "/Applications/iPhone 2G (iOS 1.1.4).app"
```

A real fix is a Developer ID signature plus notarisation, which needs a paid
Apple developer account. Worth doing only if these are distributed properly.

---

## Checking a bundle

`--verify-only` audits an installed app without touching it, and now covers the
CA end of this:

```bash
scripts/package-iphone-app.sh --app "/Applications/iPhone 2G (iOS 1.0).app" --verify-only
```

```
  ok   no Homebrew load paths (found 0)
  ok   guest trusts a bridge CA          # the pack contains a bridge root
  ok   bundle carries its CA (portable)  # ...and the key travels with the app
  ok   bundled CA matches the guest's    # ...and it is the one the NAND trusts
  ok   trusted CA is this host's
```

These fail packaging outright (`exit 1`, "packaging INCOMPLETE"), verified by
pointing `S5L8900_HTTPS_STATE_DIR` at an empty directory.

Note that `trusted CA is this host's` is a *packaging-machine* check and is
expected to be meaningless on a machine that only received a copy; the two
checks above it are the portable ones.
