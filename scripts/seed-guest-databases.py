#!/usr/bin/env python3
"""Create the SQLite databases iPhone OS expects to find in /var.

Why this exists
---------------
On a real device the first boot after a restore CREATES these databases. Our
generated NAND cannot: the guest's writes never reach storage (the FTL issues
no page-write commands against a constructed tree -- the iPod, whose NAND is a
device dump, does issue them: 0x500/0x400/0x100 via the ADM). A daemon that
must create state therefore fails forever. Measured worst case:
`com.apple.AddressBook` creates its database, reads it back, finds
"no such table: ABPerson", and retries ~250 times a SECOND -- pegging the
emulated CPU at 98% where the iPod idles at 11-15%.

Rather than delete the daemon (which would cost Contacts), we hand it the
database it would have made. The schema is taken from the FIRMWARE ITSELF, not
invented: `AddressBook.framework` and `AppSupport.framework` carry their
`CREATE TABLE/INDEX/TRIGGER` statements as plain strings.

One table is the exception. `CREATE TABLE ABPerson` appears NOWHERE in the
firmware -- it is built at runtime -- so its columns are reconstructed from the
daemon's own SELECT, which the guest prints verbatim when the table is missing:

    SELECT ROWID, First, Last, Middle, NULL, NULL, NULL, Organization, NULL,
           NULL, Kind, NULL, NULL, NULL, Prefix, Suffix, FirstSort, LastSort,
           CreationDate, ModificationDate, CompositeNameFallback FROM ABPerson

plus the columns named by other queries in the framework (JobTitle, Nickname,
Department, Note, Birthday). That reconstruction is the one guessed part here
and is marked as such; everything else is the vendor's own SQL.

This is a workaround for the read-only NAND, not a fix for it: settings still
will not persist. The real fix is task T6 (make the generated NAND writable).

Usage
-----
  scripts/seed-guest-databases.py --root-hfs <root.img> --out /tmp/seed
  # then inject, e.g. into the /var image:
  scripts/inject-guest-file.py --image data-var.img \\
      --src /tmp/seed/AddressBook.sqlitedb \\
      --dest /mobile/Library/AddressBook/AddressBook.sqlitedb
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import attached

AB_FRAMEWORK = "System/Library/Frameworks/AddressBook.framework/AddressBook"
APPSUPPORT = "System/Library/Frameworks/AppSupport.framework/AppSupport"

# Reconstructed, not extracted -- see the module docstring.
AB_PERSON = """CREATE TABLE ABPerson (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    First TEXT, Last TEXT, Middle TEXT, FirstPhonetic TEXT,
    MiddlePhonetic TEXT, LastPhonetic TEXT, Organization TEXT,
    Department TEXT, Note TEXT, Kind INTEGER, Birthday TEXT,
    JobTitle TEXT, Nickname TEXT, Prefix TEXT, Suffix TEXT,
    FirstSort TEXT, LastSort TEXT, CreationDate INTEGER,
    ModificationDate INTEGER, CompositeNameFallback TEXT)"""

# Image database (separate file on a real device).
AB_IMAGES = [
    "CREATE TABLE ABFullSizeImage (record_id INTEGER PRIMARY KEY, data BLOB)",
    "CREATE TABLE ABThumbnailImage (record_id INTEGER PRIMARY KEY, "
    "format INTEGER, data BLOB)",
]


def extract_sql(binary: Path) -> list[str]:
    """Every CREATE TABLE/INDEX/TRIGGER statement the binary carries."""
    out = subprocess.run(["strings", "-a", str(binary)],
                         capture_output=True, text=True, check=True).stdout
    statements: list[str] = []
    for line in out.splitlines():
        # several statements are concatenated into one string constant
        for part in re.split(r";(?=CREATE)", line):
            part = part.strip().rstrip(";").strip()
            if not re.match(r"^CREATE (TABLE|INDEX|TRIGGER|UNIQUE INDEX)\b",
                            part, re.I):
                continue
            if re.search(r"\bEND$|BEGIN\b", part, re.I) and \
                    not re.search(r"\bEND\b", part, re.I):
                continue          # truncated trigger body: skip, not guessable
            if part not in statements:
                statements.append(part)
    return statements


def build(path: Path, statements: list[str], label: str) -> int:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    # The guest runs a 2007 SQLite. A database written by a modern library
    # uses a newer schema format and the guest rejects it outright with
    # "unsupported file format" -- measured: the daemon then loops exactly as
    # if the file were missing. legacy_file_format=ON forces schema format 1,
    # which every SQLite 3.x can read, and it must be set BEFORE the first
    # table is created (it is baked into the header at creation time).
    db.execute("PRAGMA legacy_file_format=ON")
    db.execute("PRAGMA journal_mode=DELETE")   # no WAL: unknown to 2007 SQLite
    applied = 0
    for sql in statements:
        try:
            db.execute(sql)
            applied += 1
        except sqlite3.Error as e:
            print(f"    skipped ({e}): {sql[:70]}...")
    db.commit()
    db.close()
    downgrade_header(path)
    print(f"  {label}: {applied}/{len(statements)} statements -> {path.name} "
          f"({path.stat().st_size} bytes)")
    return applied


def downgrade_header(path: Path) -> None:
    """Stamp the file header back to schema format 1.

    `PRAGMA legacy_file_format` is a NO-OP on a modern SQLite (measured: 3.43
    still wrote schema format 4), and the guest's 2007 library rejects that
    outright -- "unsupported file format" -- exactly as if the database were
    missing. Nothing in this schema needs a format above 1 (no descending
    indexes, no boolean literals), so the four header bytes at offset 44 can
    simply be set to 1. Page size stays 4096, which SQLite has accepted since
    3.0. Header layout: sqlite.org/fileformat2.html#the_database_header.
    """
    data = bytearray(path.read_bytes())
    if data[:15] != b"SQLite format 3":
        raise SystemExit(f"not a SQLite database: {path}")
    data[44:48] = (1).to_bytes(4, "big")     # schema format number
    path.write_bytes(bytes(data))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root-hfs", type=Path, required=True,
                    help="M68AP root filesystem image (read-only)")
    ap.add_argument("--out", type=Path, required=True,
                    help="directory to write the databases into")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    with attached(args.root_hfs, readonly=True) as mnt:
        ab_sql = extract_sql(Path(mnt) / AB_FRAMEWORK)
        support_sql = extract_sql(Path(mnt) / APPSUPPORT)

    props = [s for s in support_sql if "_SqliteDatabaseProperties" in s]
    if not props:
        raise SystemExit("could not find the _SqliteDatabaseProperties schema "
                         "in AppSupport.framework")
    print(f"extracted {len(ab_sql)} AddressBook + {len(props)} AppSupport "
          f"statements from the firmware")

    images = [s for s in ab_sql if re.search(r"AB(FullSize|Thumbnail)Image", s)]
    main_sql = [s for s in ab_sql if s not in images]
    # ABPerson first: indexes and triggers reference it.
    build(args.out / "AddressBook.sqlitedb",
          props + [AB_PERSON] + main_sql, "AddressBook")
    build(args.out / "AddressBookImages.sqlitedb",
          props + (images or AB_IMAGES), "AddressBookImages")

    print("\nseeded. Inject under /mobile/Library/AddressBook/ in the /var "
          "image (owner uid 501 = mobile).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
