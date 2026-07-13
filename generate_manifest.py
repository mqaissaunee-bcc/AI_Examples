#!/usr/bin/env python3
"""
generate_manifest.py
====================

Scans a data folder for Brookdale enrollment report spreadsheets and
generates a manifest.json that the dashboard reads at load time.

Expected filename pattern:
    <anything>_<MMDDYY>.xlsx

Examples that parse correctly:
    26FA_Course_Enrollment_Report_042826.xlsx  -> 2026-04-28
    26FA_Course_Enrollment_Report_050126.xlsx  -> 2026-05-01

Folder structure — two conventions supported side by side:

    Loose files at the term level (backward compat, treated as 15-week):
        data/26FA/26FA_..._050126.xlsx

    Files organized into sub-term subfolders (new; use folder name as
    the part-of-term identifier):
        data/26FA/15W/26FA_..._050126.xlsx
        data/26FA/11W/26FA_..._050126.xlsx
        data/26FA/7A/26FA_..._050126.xlsx
        data/26FA/7B/26FA_..._050126.xlsx

Both conventions can coexist during migration. After all files are
moved into subfolders, the loose-file path is no longer used but the
script still tolerates loose files (defaulting them to "15W") so that
accidental drops don't break the pipeline.

Subfolder names starting with '.' or '_' are ignored (avoids picking
up .git, __pycache__, _backup, etc.). Only one level of nesting is
walked — files inside data/{term}/{pot}/deeper/ are not discovered.

Usage
-----
    # Default: scans data/26FA/, writes manifest there
    python3 generate_manifest.py

    # Custom data folder
    python3 generate_manifest.py --data-dir data/27SP

    # Dry run: print what the manifest would be without writing
    python3 generate_manifest.py --dry-run

Exit codes
----------
    0   manifest written successfully
    1   one or more spreadsheets had unparseable filenames (fail loudly)
    2   data folder not found or empty
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple


# Match the trailing _MMDDYY or " MMDDYY" before .xlsx
# (last 6 digits before extension, preceded by either underscore or space)
DATE_PATTERN = re.compile(r"[_\s](\d{2})(\d{2})(\d{2})\.xlsx$", re.IGNORECASE)

# Part-of-term assigned to files found loose at the term-folder level
# (i.e., not inside a POT subfolder). Preserves the meaning of files
# uploaded before the sub-term structure existed — all such files were
# 15-week snapshots.
LOOSE_DEFAULT_POT = "15W"


def parse_date_from_filename(filename: str) -> Optional[str]:
    """
    Extract YYYY-MM-DD from a filename ending in _MMDDYY.xlsx or MMDDYY.xlsx
    (with either an underscore or a space before the date).
    Returns None if the pattern does not match or the date is invalid.
    """
    match = DATE_PATTERN.search(filename)
    if not match:
        return None

    mm, dd, yy = match.groups()
    try:
        # Two-digit years interpreted as 20YY
        date = datetime(2000 + int(yy), int(mm), int(dd))
        return date.strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_filename(filename: str) -> str:
    """
    Convert spaces to underscores in a filename. This keeps the data folder
    consistent regardless of how files are uploaded — GitHub's web interface
    silently converts spaces to underscores during drag-and-drop uploads,
    but other upload methods (git CLI, GitHub Desktop, the API) preserve
    spaces. Normalizing here means the repo always ends up looking the same.
    """
    return filename.replace(" ", "_")


def derive_term(data_dir: Path) -> str:
    """Use the data folder's leaf name as the term label (e.g., '26FA')."""
    return data_dir.name


def is_valid_xlsx(f: Path) -> bool:
    """
    True if f is a real .xlsx file worth including in the manifest.
    Excludes Office temp/lock files (~$foo.xlsx) and hidden files (.foo).
    """
    return (
        f.is_file()
        and f.suffix.lower() == ".xlsx"
        and not f.name.startswith("~$")
        and not f.name.startswith(".")
    )


def discover_snapshots(data_dir: Path) -> List[Tuple[Path, str]]:
    """
    Return a list of (file_path, part_of_term) tuples for every valid
    xlsx file in and beneath data_dir.

    Two search patterns, combined:
      - Loose .xlsx files directly in data_dir → part_of_term = "15W"
        (backward compat: files uploaded before the sub-term structure)
      - .xlsx files inside a subfolder → part_of_term = subfolder name

    Subfolders starting with '.' or '_' are skipped (avoids .git,
    __pycache__, _backup, etc.). Deeper nesting is not walked.
    """
    results: List[Tuple[Path, str]] = []

    # Loose files (backward compat: treated as 15W)
    for f in data_dir.iterdir():
        if is_valid_xlsx(f):
            results.append((f, LOOSE_DEFAULT_POT))

    # Subfolders (new sub-term structure)
    for sub in data_dir.iterdir():
        if not sub.is_dir():
            continue
        if sub.name.startswith(".") or sub.name.startswith("_"):
            continue
        pot = sub.name  # e.g., "15W", "11W", "7A", "7B", "SU1"
        for f in sub.iterdir():
            if is_valid_xlsx(f):
                results.append((f, pot))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate manifest.json for the enrollment dashboard."
    )
    parser.add_argument(
        "--data-dir",
        default="data/26FA",
        help="Path to the folder containing the .xlsx reports (default: data/26FA)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the manifest without writing to disk",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    # Sanity check: folder exists
    if not data_dir.exists() or not data_dir.is_dir():
        print(f"ERROR: data folder not found: {data_dir}", file=sys.stderr)
        return 2

    # Discover all xlsx files (loose files + subfolder-organized files)
    discovered = discover_snapshots(data_dir)

    if not discovered:
        print(f"ERROR: no .xlsx files found in {data_dir}", file=sys.stderr)
        return 2

    # Sort deterministically: by POT, then by filename. Final manifest
    # sorting (by date) happens later; this initial sort just makes the
    # normalization and rename output easier to scan in logs.
    discovered.sort(key=lambda t: (t[1], t[0].name))

    # Normalize filenames: spaces -> underscores. Applied to files at any
    # location (loose or in a subfolder). Once renamed, the parent path is
    # unchanged; the same POT applies.
    renamed = []
    for i, (f, pot) in enumerate(discovered):
        normalized = normalize_filename(f.name)
        if normalized != f.name:
            new_path = f.parent / normalized
            if new_path.exists():
                # Conflict — refuse rather than overwrite
                print(
                    f"ERROR: cannot rename '{f.name}' to '{normalized}' "
                    f"because that filename already exists. Resolve manually.",
                    file=sys.stderr,
                )
                return 1
            f.rename(new_path)
            renamed.append((f.name, normalized))
            discovered[i] = (new_path, pot)

    if renamed:
        print("Normalized filenames (spaces -> underscores):")
        for old, new in renamed:
            print(f"  {old}  ->  {new}")

    # Parse dates, collecting any failures
    snapshots = []
    unparseable = []

    for f, pot in discovered:
        date = parse_date_from_filename(f.name)
        # The file path stored in the manifest is relative to data_dir.
        # For loose files this is just the filename; for subfolder files
        # it's "POT/filename". The client fetches ${data_dir}/${file}
        # so this works transparently in both cases.
        rel_path = str(f.relative_to(data_dir))
        if date is None:
            unparseable.append(rel_path)
        else:
            snapshots.append({
                "date": date,
                "part_of_term": pot,
                "file": rel_path,
            })

    # Fail loudly on bad filenames — do not write a partial manifest
    if unparseable:
        print(
            "ERROR: the following files in {} do not match the expected naming "
            "pattern (_MMDDYY.xlsx) and could not be added to the manifest:".format(data_dir),
            file=sys.stderr,
        )
        for name in unparseable:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nFix by renaming each file to end with the report date in MMDDYY format,\n"
            "for example: 26FA_Course_Enrollment_Report_051526.xlsx",
            file=sys.stderr,
        )
        return 1

    # Sort by (date, part_of_term) so entries are ordered oldest-first, and
    # so that multiple POTs on the same date have a deterministic order.
    snapshots.sort(key=lambda s: (s["date"], s["part_of_term"]))

    # Detect duplicates — same (date, part_of_term) pair must appear at most
    # once. Note that a 15W and a 7A snapshot on the same date are NOT
    # duplicates; they're different POTs happening to share a report date.
    dates_seen = {}
    for s in snapshots:
        key = (s["date"], s["part_of_term"])
        dates_seen.setdefault(key, []).append(s["file"])
    duplicates = {k: files for k, files in dates_seen.items() if len(files) > 1}
    if duplicates:
        print(
            "ERROR: the following (date, part-of-term) pairs have multiple files.\n"
            "The dashboard expects exactly one report per date per part of term:",
            file=sys.stderr,
        )
        for (date, pot), files in duplicates.items():
            print(f"  {date} · {pot}: {', '.join(files)}", file=sys.stderr)
        return 1

    # Preserve any non-auto-generated fields (like goals) from an existing
    # manifest, so the Action doesn't wipe configuration on every regeneration.
    manifest_path = data_dir / "manifest.json"
    preserved = {}
    if manifest_path.exists():
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
            # Keep any keys that aren't auto-generated by this script
            for key, value in existing.items():
                if key not in ("term", "updated", "snapshots"):
                    preserved[key] = value
        except (json.JSONDecodeError, OSError):
            # If the existing manifest is corrupt, just regenerate from scratch
            pass

    manifest = {
        "term": derive_term(data_dir),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **preserved,
        "snapshots": snapshots,
    }

    output = json.dumps(manifest, indent=2) + "\n"

    if args.dry_run:
        print(output)
        return 0

    manifest_path.write_text(output, encoding="utf-8")
    print(f"Wrote {manifest_path} with {len(snapshots)} snapshot(s):")
    for s in snapshots:
        print(f"  {s['part_of_term']:4s}  {s['date']}  {s['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
