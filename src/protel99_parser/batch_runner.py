#!/usr/bin/env python3
"""
batch_runner.py - Process a directory of Protel 99 SE PCB files and report per-file stats.

Usage:
    python3 batch_runner.py <directory> [--json] [--verbose]

Exit code: 0 if all parseable (non-rejected) files succeeded, 1 if any error.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from protel99_parser import Protel99Parser


def find_pcb_files(directory: Path) -> list[Path]:
    """Find all *.PCB files recursively, case-insensitive."""
    files = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix.upper() == ".PCB":
            files.append(p)
    return files


def parse_one(filepath: Path) -> dict:
    """Parse a single PCB file and return a stats dict."""
    stat = {
        "filename": filepath.name,
        "filepath": str(filepath),
        "file_size_bytes": filepath.stat().st_size,
        "version_string": None,
        "record_count": 0,
        "component_count": 0,
        "unique_record_types": {},
        "ground_truth_found": False,
        "status": "ok",
        "error_message": None,
    }

    try:
        parser = Protel99Parser(filepath)
        pcb = parser.parse()

        stat["version_string"] = pcb.version
        stat["record_count"] = pcb.total_records
        stat["component_count"] = len(pcb.components)
        stat["unique_record_types"] = dict(pcb.record_counts)

        # ground_truth_found: check if any component has a real position source
        matched, total, pct = pcb.position_coverage
        stat["ground_truth_found"] = matched > 0

    except ValueError as e:
        stat["status"] = "rejected"
        stat["error_message"] = str(e)
    except Exception as e:
        stat["status"] = "error"
        stat["error_message"] = f"{type(e).__name__}: {e}"

    return stat


def print_table(results: list[dict], verbose: bool = False):
    """Print human-readable table to stdout."""
    # Header
    print(f"{'File':<30s} {'Size':>8s} {'Records':>8s} {'Comps':>6s} {'Status':<10s}")
    print("-" * 70)

    for r in results:
        name = r["filename"]
        if len(name) > 29:
            name = name[:26] + "..."
        size_kb = f"{r['file_size_bytes'] / 1024:.0f}KB"
        records = str(r["record_count"]) if r["status"] == "ok" else "-"
        comps = str(r["component_count"]) if r["status"] == "ok" else "-"
        status = r["status"]
        if r["error_message"]:
            status += f" ({r['error_message'][:40]})"

        print(f"{name:<30s} {size_kb:>8s} {records:>8s} {comps:>6s} {status}")

        if verbose and r["status"] == "ok" and r["unique_record_types"]:
            types_str = ", ".join(
                f"REC_{k:02X}: {v}" for k, v in sorted(r["unique_record_types"].items())
            )
            print(f"  └─ {types_str}")

    print("-" * 70)


def print_summary(results: list[dict]):
    """Print summary line."""
    ok = sum(1 for r in results if r["status"] == "ok")
    rejected = sum(1 for r in results if r["status"] == "rejected")
    errors = sum(1 for r in results if r["status"] == "error")
    total = len(results)
    print(f"{ok}/{total} files parsed successfully, {rejected} rejected (wrong format), {errors} errors")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Batch-parse Protel 99 SE PCB files")
    ap.add_argument("directory", type=Path, help="Directory to scan for *.PCB files")
    ap.add_argument("--json", action="store_true", dest="json_output", help="Output JSON array to stdout")
    ap.add_argument("--verbose", "-v", action="store_true", help="Show per-file record type distribution")
    args = ap.parse_args(argv)

    if not args.directory.is_dir():
        print(f"Error: '{args.directory}' is not a directory", file=sys.stderr)
        return 1

    files = find_pcb_files(args.directory)
    if not files:
        print(f"No *.PCB files found in '{args.directory}'", file=sys.stderr)
        return 1

    # Process files, printing progress to stderr
    results = []
    for f in files:
        print(f"[batch] {f.name}: ", end="", file=sys.stderr, flush=True)
        stat = parse_one(f)
        results.append(stat)
        if stat["status"] == "ok":
            print(f"{stat['record_count']} records, {stat['component_count']} components, OK", file=sys.stderr)
        else:
            print(f"{stat['status'].upper()} - {stat['error_message']}", file=sys.stderr)

    # Output
    if args.json_output:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, verbose=args.verbose)
        print_summary(results)

    # Exit code: 1 if any error (not rejected, just error)
    has_errors = any(r["status"] == "error" for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
