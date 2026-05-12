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
from typing import Optional


# Match the trailing _MMDDYY or " MMDDYY" before .xlsx
# (last 6 digits before extension, preceded by either underscore or space)
DATE_PATTERN = re.compile(r"[_\s](\d{2})(\d{2})(\d{2})\.xlsx$", re.IGNORECASE)


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

    # Find all .xlsx files (case-insensitive), excluding temp/lock files
    xlsx_files = sorted(
        f for f in data_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() == ".xlsx"
        and not f.name.startswith("~$")
        and not f.name.startswith(".")
    )

    if not xlsx_files:
        print(f"ERROR: no .xlsx files found in {data_dir}", file=sys.stderr)
        return 2

    # Normalize filenames: spaces -> underscores. This handles the case where
    # IR sends a file with spaces and the upload preserves them. Once renamed,
    # the rest of the script and the dashboard's manifest see consistent names.
    renamed = []
    for i, f in enumerate(xlsx_files):
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
            xlsx_files[i] = new_path

    if renamed:
        print("Normalized filenames (spaces -> underscores):")
        for old, new in renamed:
            print(f"  {old}  ->  {new}")

    # Parse dates, collecting any failures
    snapshots = []
    unparseable = []

    for f in xlsx_files:
        date = parse_date_from_filename(f.name)
        if date is None:
            unparseable.append(f.name)
        else:
            snapshots.append({
                "date": date,
                "file": f.name,
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

    # Sort by date ascending so the dashboard displays oldest-first naturally
    snapshots.sort(key=lambda s: s["date"])

    # Detect duplicate dates — these produce ambiguous results in the dashboard
    dates_seen = {}
    for s in snapshots:
        dates_seen.setdefault(s["date"], []).append(s["file"])
    duplicates = {d: files for d, files in dates_seen.items() if len(files) > 1}
    if duplicates:
        print(
            "ERROR: the following report dates have multiple files. The dashboard\n"
            "expects exactly one report per date:",
            file=sys.stderr,
        )
        for date, files in duplicates.items():
            print(f"  {date}: {', '.join(files)}", file=sys.stderr)
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
        print(f"  {s['date']}  {s['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
