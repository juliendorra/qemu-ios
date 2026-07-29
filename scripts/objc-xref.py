#!/usr/bin/env python3
"""Who sends this selector? Cross-references an old-ABI ObjC binary.

`-[SpringBoard menuButtonUp:]` turned out to gate its call to
`[_uiController clickedMenuButton]` on `_menuButtonTimer`, and on iPhone OS
1.1.4 -- which WORKS -- that gate returns early. So the interesting question
becomes "then who else sends clickedMenuButton", which is a static question.

The old ABI makes it answerable: a message send loads a word from
`__OBJC __message_refs`, and that word points at the selector cstring. So find
the cstring, find the message-ref entry pointing at it, then find every literal
in __TEXT equal to that entry's address -- each one is a call site. Method
boundaries come from the same metadata (every class's method lists), which turns
a raw address into `-[Class selector]+0x..`.

Usage:
  scripts/objc-xref.py <binary> clickedMenuButton
  scripts/objc-xref.py <binary> --list-methods | grep -i menu
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def methods(img):
    """[(imp, '-[Class sel]')] from every class's method lists."""
    d = img.d
    out = []
    secs = {s["name"]: s for s in img.secs}
    cls = secs.get("__class") or secs.get("__cls_refs")
    if not cls:
        return out
    off, size = cls["offset"], cls["size"]
    for o in range(off, off + size, 48):          # struct objc_class is 48 B
        if o + 40 > len(d):
            break
        _isa, _sup, name, _ver, info, isize, _ivars, meths = \
            struct.unpack_from("<8I", d, o)
        cname = img.cstr(name)
        if not cname or not meths:
            continue
        kind = "+" if (info & 1) == 0 else "-"
        mo = img.off(meths)
        if mo is None:
            continue
        # struct objc_method_list { void *obsolete; int count; method[] }
        _obs, cnt = struct.unpack_from("<Ii", d, mo)
        if not (0 < cnt < 4000):
            continue
        for i in range(cnt):
            e = mo + 8 + i * 12
            if e + 12 > len(d):
                break
            sel, _types, imp = struct.unpack_from("<III", d, e)
            s = img.cstr(sel)
            if s and imp:
                out.append((imp & ~1, f"{kind}[{cname} {s}]"))
    return sorted(set(out))


def owner(addr, meths):
    best = None
    for imp, name in meths:
        if imp <= addr:
            best = (imp, name)
        else:
            break
    return f"{best[1]}+{addr - best[0]:#x}" if best else f"{addr:#x}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", type=Path)
    ap.add_argument("selector", nargs="?")
    ap.add_argument("--list-methods", action="store_true")
    args = ap.parse_args()

    dis = _load("objcdis", REPO / "scripts" / "objc-method-disasm.py")
    img = dis.Image(args.binary)
    ms = methods(img)

    if args.list_methods:
        for imp, name in ms:
            print(f"{imp:#010x}  {name}")
        return 0
    if not args.selector:
        ap.error("give a selector or --list-methods")

    d = img.d
    sel = args.selector.encode()
    # every vmaddr at which the selector cstring appears
    cstr_vas = []
    for m in re.finditer(re.escape(sel) + rb"\x00", d):
        for s in img.secs:
            o = s.get("offset", 0)
            if o <= m.start() < o + s.get("size", 0):
                cstr_vas.append(m.start() - o + s["addr"])
    # message-ref entries pointing at it
    refs = []
    for va in cstr_vas:
        for m in re.finditer(re.escape(struct.pack("<I", va)), d):
            for s in img.secs:
                o = s.get("offset", 0)
                if o <= m.start() < o + s.get("size", 0):
                    refs.append(m.start() - o + s["addr"])
    refs = sorted(set(refs))
    print(f"selector cstring at {[hex(v) for v in cstr_vas]}")
    print(f"message/selector refs at {[hex(v) for v in refs]}")

    sites = []
    for r in refs:
        for m in re.finditer(re.escape(struct.pack("<I", r)), d):
            for s in img.secs:
                o = s.get("offset", 0)
                if o <= m.start() < o + s.get("size", 0) and s["name"] == "__text":
                    sites.append(m.start() - o + s["addr"])
    print(f"\n{len(sites)} literal(s) in __text referencing it:")
    for a in sorted(set(sites)):
        print(f"  literal @ {a:#010x}   in {owner(a, ms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
