#!/usr/bin/env python3
"""Merge a single-entry extraction JSON into a cumulative results.json.

Input entry format (produced by run_extraction.py):

    {
      "document": "sample_01.pdf",
      "run_id": "...",
      "tables": { "<table>": [record, ...], ... }
    }

The cumulative results.json has the same table -> records shape as the
multi-document answer.json:

    { "<table>": [record, ...], ... }

Merging rules:
  - Records are appended per table.
  - A record already present for the same table (same tuple_id, or the same
    values if tuple_id is empty) is replaced by the incoming one, so
    re-running an extraction for a document updates rather than duplicates.
  - Tables new to the results file are added.

Usage:
  python3 add_to_results.py ENTRY.json [--results RESULTS.json] [--replace]

  RESULTS defaults to results.json in the current directory. Use --replace
  to drop records from the entry's source document (matched via
  _source_document) before adding the new ones.
"""

import argparse
import json
from pathlib import Path


def record_key(record: dict) -> tuple:
    """Identity of a record: tuple_id if set, else its (frozen) values."""
    values = record.get("values") or {
        k: v for k, v in record.items() if not k.startswith("_")
    }
    tid = record.get("tuple_id") or record.get("_tuple_id")
    if tid:
        return ("tid", tid)
    frozen = tuple(sorted((str(k), str(v)) for k, v in (values or {}).items()))
    return ("values", frozen)


def same_document(record: dict, document: str) -> bool:
    return (record.get("_source_document") or "") == document


def merge_entry(entry: dict, results: dict, replace: bool) -> dict:
    document = entry.get("document", "")
    tables = entry.get("tables") or {}

    # Optionally drop the document's previous records from every table.
    if replace and document:
        for table in list(results):
            results[table] = [r for r in results[table]
                              if not same_document(r, document)]
            if not results[table]:
                del results[table]

    merged = json.loads(json.dumps(results))  # deep copy
    for table, records in tables.items():
        existing = {record_key(r): i for i, r in enumerate(merged.get(table, []))}
        for record in records or []:
            key = record_key(record)
            if key in existing:
                merged[table][existing[key]] = record  # update in place
            else:
                existing[key] = len(merged.setdefault(table, []))
                merged[table].append(record)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge a single-entry extraction JSON into a big results.json.")
    parser.add_argument("entry", type=Path, help="single-entry JSON from run_extraction.py")
    parser.add_argument("--results", type=Path, default=Path("results.json"),
                        help="cumulative results JSON (default: results.json)")
    parser.add_argument("--replace", action="store_true",
                        help="first remove the document's old records "
                             "(matched via _source_document) from the results")
    args = parser.parse_args()

    if not args.entry.is_file():
        raise SystemExit(f"Entry file not found: {args.entry}")

    entry = json.loads(args.entry.read_text(encoding="utf-8"))
    if "tables" not in entry or "document" not in entry:
        raise SystemExit(
            f"{args.entry} is not a single-entry extraction JSON "
            "(expected 'document' and 'tables' keys, as written by run_extraction.py)")

    results = {}
    if args.results.exists():
        results = json.loads(args.results.read_text(encoding="utf-8"))
    if not isinstance(results, dict):
        raise SystemExit(f"{args.results} does not contain a JSON object")

    merged = merge_entry(entry, results, args.replace)
    added = {t: len(r or []) for t, r in (entry.get("tables") or {}).items()}
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    print(f"Merged {args.entry} (document: {entry['document']}) into {args.results}")
    for table, count in added.items():
        print(f"  {table:20s} {count} record(s) -> {len(merged.get(table, []))} total")


if __name__ == "__main__":
    main()
