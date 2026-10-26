#!/usr/bin/env python3
"""
Generate manifest.json for the enrollment dashboard.

Discovers snapshot files in the specified data directory and writes
<data_dir>/manifest.json listing each snapshot with its date, part-of-term,
source file, and any required content filter. Preserves custom top-level
manifest fields (goals, etc.) across regenerations.

v2 (2026-10-26): Added defensive abort when protected top-level fields would
be lost in regeneration. See PROTECTED_FIELDS below. If an existing manifest
has 'goals', 'census', 'pot_dates', 'term_label', 'term_start', or 'term_end'
and the new write would drop them (because they weren't present in the file
the script actually read from disk at run time), the script now aborts with
a loud error and writes nothing. This prevents silent data loss when a race
condition or stale read strips the fields. To intentionally remove a
protected field, pass --allow-field-removal.

Usage:
    python3 generate_manifest.py --data-dir data/26FA
    python3 generate_manifest.py --data-dir data/26FA --allow-field-removal
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
ENABLE_COMBINED_FILES = True


# ==============================================================================
# PROTECTED FIELDS (v2 safety net)
# ==============================================================================
# Top-level manifest fields that should never silently disappear across a
# regeneration. If the existing manifest had any of these and the preservation
# logic would strip them, the script aborts. See defensive abort in
# write_manifest(). This specifically guards against races and stale reads
# that previously corrupted manifests silently.
PROTECTED_FIELDS = {
    'goals',
    'census',
    'pot_dates',
    'term_label',
    'term_start',
    'term_end',
}


# ==============================================================================
# CONFIGURATION
# ==============================================================================
POT_SUBFOLDERS = ['15W', '11W', '7A', '7B']
SHEET_NAMES = ['Individual Sects All', 'Query result']


def build_config(term_code):
    """
    Build the term-specific patterns and mappings from a term code (e.g. '26FA').
    """
    escaped = re.escape(term_code)
    return {
        'term_code': term_code,
        'combined_pattern': re.compile(rf'^{escaped}R[_ ]', re.IGNORECASE),
        'individual_prefix_map': [
            ('11W', re.compile(rf'^{escaped}11[_ ]', re.IGNORECASE)),
            ('7A',  re.compile(rf'^{escaped}7A[_ ]', re.IGNORECASE)),
            ('7B',  re.compile(rf'^{escaped}7B[_ ]', re.IGNORECASE)),
            ('15W', re.compile(rf'^{escaped}[_ ]',   re.IGNORECASE)),
        ],
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
    m = re.search(r'[_ ](\d{2})(\d{2})(\d{2})\.xlsx?$', filename, re.IGNORECASE)
    if not m:
        return None
    mm, dd, yy = m.groups()
    try:
        return date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None


def detect_individual_pot_from_filename(filename, config):
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
    if not manifest_path.exists():
        return {}
    try:
        with open(manifest_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: could not read existing manifest ({e}) — regenerating from scratch",
              file=sys.stderr)
        return {}


def write_manifest(data_root, term_code, snapshots, allow_field_removal=False):
    """
    Assemble and write the manifest, preserving custom top-level fields.

    v2 (2026-10-26): defensive abort. If the existing manifest had any of the
    PROTECTED_FIELDS (goals, census, pot_dates, term dates/label) and the
    newly-computed preserved set has lost them, abort with a loud error and
    write nothing. This prevents silent data loss when a race condition or
    stale read makes the existing manifest look stripped at run time.

    Pass allow_field_removal=True to override (needed only if you deliberately
    want to remove a protected field).
    """
    manifest_path = data_root / 'manifest.json'
    existing = load_existing_manifest(manifest_path)

    auto_generated_keys = {'term', 'updated', 'snapshots'}
    preserved = {k: v for k, v in existing.items() if k not in auto_generated_keys}

    # Defensive abort — see docstring and PROTECTED_FIELDS comment above.
    existing_protected = {k for k in existing.keys() if k in PROTECTED_FIELDS}
    preserved_protected = {k for k in preserved.keys() if k in PROTECTED_FIELDS}
    missing = existing_protected - preserved_protected

    if missing and not allow_field_removal:
        print(
            f"\n*** ABORT: refusing to strip protected fields from {manifest_path} ***\n"
            f"The existing manifest had protected fields: {sorted(existing_protected)}\n"
            f"The regeneration would preserve: {sorted(preserved_protected)}\n"
            f"That would silently drop: {sorted(missing)}\n"
            f"\n"
            f"This usually means a race condition or stale read corrupted the existing\n"
            f"manifest, or the manifest was overwritten by an incomplete edit. The\n"
            f"script is refusing to write to prevent data loss. Nothing has changed.\n"
            f"\n"
            f"To proceed:\n"
            f"  1. Inspect {manifest_path} and verify it has all expected fields.\n"
            f"  2. If fields are legitimately missing, restore them and re-run.\n"
            f"  3. If you really do want to remove the fields, re-run with\n"
            f"     --allow-field-removal.\n",
            file=sys.stderr,
        )
        return None

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
    parser.add_argument(
        '--allow-field-removal', action='store_true',
        help='Override the defensive abort when protected fields would be stripped.',
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
    print(f"  Protected fields: {sorted(PROTECTED_FIELDS)}")
    if args.allow_field_removal:
        print(f"  Field-removal override: ENABLED")

    individual = discover_individual_snapshots(data_root, config)
    combined = discover_combined_snapshots(data_root, config)

    snapshots, dropped_by_collision = build_snapshot_entries(individual, combined)

    manifest_path = write_manifest(
        data_root, term_code, snapshots,
        allow_field_removal=args.allow_field_removal,
    )

    if manifest_path is None:
        return 1

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
