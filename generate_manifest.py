#!/usr/bin/env python3
"""
Generate manifest.json for the enrollment dashboard.

Discovers snapshot files in the specified data directory and writes
<data_dir>/manifest.json listing each snapshot with its date, part-of-term,
source file, and any required content filter. Preserves custom top-level
manifest fields (goals, etc.) across regenerations.

The term code is derived from the data directory name — data/26FA/ → 26FA,
data/27SP/ → 27SP, etc. All POT patterns are built dynamically from the term
code, so this script works for future semesters without modification.

Two file paths supported per term:

  1. Per-POT files (legacy).
     Filename patterns (using 26FA as the term-code example):
       26FA_*_MMDDYY.xlsx    → 15W
       26FA11_*_MMDDYY.xlsx  → 11W
       26FA7A_*_MMDDYY.xlsx  → 7A
       26FA7B_*_MMDDYY.xlsx  → 7B
     Location: subfolder (<data_dir>/15W/, etc.) preferred, or loose in
     <data_dir>/. Subfolder location takes precedence over filename prefix
     when both are present.
     Manifest: one entry per file.

  2. Combined files (new).
     Filename pattern: <term>R_*_MMDDYY.xlsx  (e.g. 26FAR_...)
     Location: loose in <data_dir>/
     Content: one xlsx containing rows from every POT. POT is identified
     by the "Term" column: <term> = 15W, <term>11 = 11W, <term>7A = 7A,
     <term>7B = 7B.
     Manifest: one entry per POT actually present in the file, all pointing
     at the same source file with a "pot_term_filter" field indicating which
     Term value to keep at ingest.

Reversibility: ENABLE_COMBINED_FILES flag below. Set to False to disable
combined-file support entirely — combined files are silently ignored and
only per-POT files land in the manifest. Existing per-POT flow is untouched.

Collision policy: if a date has both a combined file AND per-POT files for
POTs covered by the combined file, the combined file wins silently. The
per-POT files for those POTs get dropped from the manifest for that date.
This supports a gradual transition where a stale per-POT file may briefly
coexist with a combined file for the same date.

Usage:
    python3 generate_manifest.py --data-dir data/26FA
"""

import argparse
import json
import pathlib
import re
import sys
from datetime import date, datetime, timezone

import pandas as pd


# ==============================================================================
# FEATURE FLAG
# ==============================================================================
# Set to False to disable combined-file support entirely.
# Existing per-POT files continue to work either way. Flipping this flag and
# rerunning the workflow is the intended rollback path if the combined-file
# path causes problems.
ENABLE_COMBINED_FILES = True


# ==============================================================================
# CONFIGURATION
# ==============================================================================
POT_SUBFOLDERS = ['15W', '11W', '7A', '7B']
SHEET_NAMES = ['Individual Sects All', 'Query result']


def build_config(term_code):
    """
    Build the term-specific patterns and mappings from a term code (e.g. '26FA').
    All prefix regexes and Term-value → POT mappings derive from this so the
    same script works for any current or future term.
    """
    escaped = re.escape(term_code)
    return {
        'term_code': term_code,
        # Combined-file prefix — e.g. 26FAR_ (Colleague's naming for the
        # report-style query that returns all POTs in one file).
        'combined_pattern': re.compile(rf'^{escaped}R[_ ]', re.IGNORECASE),
        # Filename prefix → POT for loose per-POT files. Order matters:
        # more-specific patterns are checked first so '26FA11_' doesn't
        # match '26FA_' by accident.
        'individual_prefix_map': [
            ('11W', re.compile(rf'^{escaped}11[_ ]', re.IGNORECASE)),
            ('7A',  re.compile(rf'^{escaped}7A[_ ]', re.IGNORECASE)),
            ('7B',  re.compile(rf'^{escaped}7B[_ ]', re.IGNORECASE)),
            ('15W', re.compile(rf'^{escaped}[_ ]',   re.IGNORECASE)),
        ],
        # Term column values → POT identifier. Used to split combined rows.
        'term_to_pot': {
            term_code:            '15W',
            f'{term_code}11':     '11W',
            f'{term_code}7A':     '7A',
            f'{term_code}7B':     '7B',
        },
    }


# ==============================================================================
# FILENAME PARSING
# ==============================================================================
def parse_date_from_filename(filename):
    """
    Extract MMDDYY date from filenames ending with _MMDDYY.xlsx or space
    MMDDYY.xlsx. Returns a date object or None if no match.
    """
    m = re.search(r'[_ ](\d{2})(\d{2})(\d{2})\.xlsx?$', filename, re.IGNORECASE)
    if not m:
        return None
    mm, dd, yy = m.groups()
    try:
        return date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None


def detect_individual_pot_from_filename(filename, config):
    """Detect POT from a per-POT filename prefix. Returns POT string or None."""
    if config['combined_pattern'].match(filename):
        return None
    for pot, pattern in config['individual_prefix_map']:
        if pattern.match(filename):
            return pot
    return None


def is_combined_file(filename, config):
    return bool(config['combined_pattern'].match(filename))


# ==============================================================================
# FILE READING
# ==============================================================================
def read_sheet(path):
    """
    Read the appropriate sheet from an xlsx file. Tries known sheet names in
    order. Raises RuntimeError if none present.
    """
    xls = pd.ExcelFile(path)
    for name in SHEET_NAMES:
        if name in xls.sheet_names:
            return pd.read_excel(path, sheet_name=name), name
    raise RuntimeError(
        f"{path.name}: no recognized sheet found. "
        f"Sheets present: {xls.sheet_names}. "
        f"Expected one of: {SHEET_NAMES}."
    )


# ==============================================================================
# DISCOVERY
# ==============================================================================
def discover_individual_snapshots(data_root, config):
    """
    Find per-POT files in subfolders or loose in data_root. Returns a list of
    dicts: {'file': relpath_from_data_root, 'date': date, 'pot': pot_code}.
    Subfolder-based discovery is preferred over prefix-based for loose files.
    """
    results = []
    seen_paths = set()

    for pot in POT_SUBFOLDERS:
        pot_dir = data_root / pot
        if not pot_dir.exists():
            continue
        for xlsx_path in sorted(pot_dir.glob('*.xlsx')):
            date_val = parse_date_from_filename(xlsx_path.name)
            if not date_val:
                print(f"  WARNING: could not parse date from {pot}/{xlsx_path.name} — skipping",
                      file=sys.stderr)
                continue
            rel = f"{pot}/{xlsx_path.name}"
            results.append({'file': rel, 'date': date_val, 'pot': pot})
            seen_paths.add(xlsx_path.resolve())

    for xlsx_path in sorted(data_root.glob('*.xlsx')):
        if xlsx_path.resolve() in seen_paths:
            continue
        if is_combined_file(xlsx_path.name, config):
            continue
        pot = detect_individual_pot_from_filename(xlsx_path.name, config)
        if not pot:
            print(f"  WARNING: could not detect POT from loose file {xlsx_path.name} — skipping",
                  file=sys.stderr)
            continue
        date_val = parse_date_from_filename(xlsx_path.name)
        if not date_val:
            print(f"  WARNING: could not parse date from {xlsx_path.name} — skipping",
                  file=sys.stderr)
            continue
        results.append({'file': xlsx_path.name, 'date': date_val, 'pot': pot})

    return results


def discover_combined_snapshots(data_root, config):
    """
    Find combined files in data_root. Reads each to determine which POTs are
    present via the Term column. Returns a list of dicts:
      {'file': filename, 'date': date, 'pot_map': {pot: term_value, ...}}
    """
    if not ENABLE_COMBINED_FILES:
        return []

    results = []
    for xlsx_path in sorted(data_root.glob('*.xlsx')):
        if not is_combined_file(xlsx_path.name, config):
            continue

        date_val = parse_date_from_filename(xlsx_path.name)
        if not date_val:
            print(f"  WARNING: could not parse date from combined file {xlsx_path.name} — skipping",
                  file=sys.stderr)
            continue

        try:
            df, _sheet_used = read_sheet(xlsx_path)
        except RuntimeError as e:
            print(f"  WARNING: {e} — skipping", file=sys.stderr)
            continue

        if 'Term' not in df.columns:
            print(f"  WARNING: combined file {xlsx_path.name} has no 'Term' column — skipping",
                  file=sys.stderr)
            continue

        term_values = set(df['Term'].astype(str).dropna().unique())
        pot_map = {}
        unrecognized = []
        for term_val in term_values:
            pot = config['term_to_pot'].get(term_val)
            if pot:
                pot_map.setdefault(pot, term_val)
            else:
                unrecognized.append(term_val)

        if unrecognized:
            print(f"  NOTE: combined file {xlsx_path.name} has unrecognized Term values: "
                  f"{sorted(unrecognized)} — those rows will be dropped",
                  file=sys.stderr)

        if not pot_map:
            print(f"  WARNING: combined file {xlsx_path.name} has no recognized POTs — skipping",
                  file=sys.stderr)
            continue

        results.append({'file': xlsx_path.name, 'date': date_val, 'pot_map': pot_map})

    return results


# ==============================================================================
# MANIFEST BUILD
# ==============================================================================
def build_snapshot_entries(individual, combined):
    """
    Merge individual and combined snapshot descriptors into a single sorted
    snapshot list. Collision policy: for any (date, pot) also covered by a
    combined file, the combined entry wins and the individual entry is
    silently dropped.
    """
    snapshots = []

    combined_coverage = set()
    for c in combined:
        for pot in c['pot_map']:
            combined_coverage.add((c['date'].isoformat(), pot))

    for c in combined:
        for pot, term_val in sorted(c['pot_map'].items()):
            snapshots.append({
                'id': f"{c['date'].isoformat()}-{pot}",
                'date': c['date'].isoformat(),
                'part_of_term': pot,
                'file': c['file'],
                'pot_term_filter': term_val,
            })

    dropped_by_collision = 0
    for i in individual:
        key = (i['date'].isoformat(), i['pot'])
        if key in combined_coverage:
            dropped_by_collision += 1
            continue
        snapshots.append({
            'id': f"{i['date'].isoformat()}-{i['pot']}",
            'date': i['date'].isoformat(),
            'part_of_term': i['pot'],
            'file': i['file'],
        })

    snapshots.sort(key=lambda s: (s['date'], s['part_of_term']))
    return snapshots, dropped_by_collision


def load_existing_manifest(manifest_path):
    """Return the previous manifest's contents as a dict, or {} if none."""
    if not manifest_path.exists():
        return {}
    try:
        with open(manifest_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: could not read existing manifest ({e}) — regenerating from scratch",
              file=sys.stderr)
        return {}


def write_manifest(data_root, term_code, snapshots):
    """Assemble and write the manifest, preserving custom top-level fields."""
    manifest_path = data_root / 'manifest.json'
    existing = load_existing_manifest(manifest_path)

    auto_generated_keys = {'term', 'updated', 'snapshots'}
    preserved = {k: v for k, v in existing.items() if k not in auto_generated_keys}

    manifest = {
        'term': term_code,
        'updated': datetime.now(timezone.utc).isoformat(),
        **preserved,
        'snapshots': snapshots,
    }

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')

    return manifest_path


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Generate manifest.json for an enrollment dashboard term folder."
    )
    parser.add_argument(
        '--data-dir', type=pathlib.Path, required=True,
        help='Path to the term data folder (e.g. data/26FA)',
    )
    args = parser.parse_args()

    data_root = args.data_dir
    if not data_root.exists():
        print(f"ERROR: {data_root} does not exist", file=sys.stderr)
        return 1
    if not data_root.is_dir():
        print(f"ERROR: {data_root} is not a directory", file=sys.stderr)
        return 1

    term_code = data_root.name
    config = build_config(term_code)

    print(f"Scanning {data_root} (term: {term_code})...")
    print(f"  Combined-file support: {'ENABLED' if ENABLE_COMBINED_FILES else 'DISABLED'}")

    individual = discover_individual_snapshots(data_root, config)
    combined = discover_combined_snapshots(data_root, config)

    snapshots, dropped_by_collision = build_snapshot_entries(individual, combined)

    manifest_path = write_manifest(data_root, term_code, snapshots)

    combined_entries = sum(1 for s in snapshots if 'pot_term_filter' in s)
    individual_entries = len(snapshots) - combined_entries

    print(f"Wrote {manifest_path}")
    print(f"  {len(snapshots)} snapshot entries total")
    print(f"    from combined files:   {combined_entries}")
    print(f"    from individual files: {individual_entries}")
    if dropped_by_collision:
        print(f"  {dropped_by_collision} individual entries dropped due to collision with combined files")
    if combined:
        print(f"  {len(combined)} combined source file(s) scanned")

    return 0


if __name__ == '__main__':
    sys.exit(main())
