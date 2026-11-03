#!/usr/bin/env python3
"""Run the DBMosaic extraction on a **single** document.

This is a thin wrapper around run_extractions.py (unchanged): it reuses its
Docling conversion, engine invocation, and record-enrichment helpers, but
processes exactly one PDF and writes a *single-entry* JSON:

    {
      "document": "<pdf filename>",
      "run_id": "<engine run id>",
      "tables": { "<table>": [record, ...], ... }
    }

Each record carries the same provenance metadata as the multi-document
output (_page_number, _raw_text, _confidence, _source_document, warnings,
excluded).

Usage:
  python3 run_extraction.py DOC.pdf [--schema SCHEMA.json] [--output OUT.json]
                                   [--model MODEL] [--api-base URL] [--api-key KEY]
                                   [--timeout SECONDS] [--work-dir DIR] [--langsmith]
                                   [--langsmith-project NAME]

  SCHEMA defaults to schema.json next to DOC.pdf's parent directory (i.e.
  <docs_dir>/../schema.json, matching run_extractions.py's convention);
  OUTPUT defaults to <docs_dir>/../single/<doc stem>.json -- i.e. it is derived
  from the input filename (override with --output PATH/NAME.json); the
  work directory defaults to <output dir>/dbmosaic_work.

  The result JSON can be merged into a cumulative results.json with
  add_to_results.py.

Run with the engine venv interpreter (docling lives there):
  module/DBMosaic/.venv/bin/python run_extraction.py DOC.pdf ...
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import run_extractions as rx  # noqa: E402  (reuse its helpers, unchanged)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Document-to-database extraction for a single document.")
    parser.add_argument("doc", type=Path, help="source document (PDF)")
    parser.add_argument("--schema", type=Path, default=None,
                        help="target database schema JSON "
                             "(default: <doc parent dir>/../schema.json)")
    parser.add_argument("--output", type=Path, default=None,
                        help="single-entry output JSON; derived from the input "
                             "filename by default: <doc parent>/../single/<stem>.json")
    parser.add_argument("--model", default=rx.os.environ.get("MODEL", "gpt-4o"),
                        help="model id (env: MODEL, default: gpt-4o)")
    parser.add_argument("--api-base", default=rx.os.environ.get("API_BASE"),
                        help="OpenAI-compatible API base URL (env: API_BASE)")
    parser.add_argument("--api-key", default=rx.os.environ.get("API_KEY"),
                        help="API key (env: API_KEY)")
    parser.add_argument("--timeout", type=int,
                        default=int(rx.os.environ.get("TIMEOUT", "3600")),
                        help="max seconds to wait for the agent run (env: TIMEOUT)")
    parser.add_argument("--langsmith", action="store_true",
                        default=rx.os.environ.get("LANGSMITH_TRACING", "").lower() in ("1", "true"),
                        help="send LangSmith tracing for the agent run: LLM calls with "
                             "token usage and latency, agent steps as tool runs, and a "
                             "root run per extraction (env: LANGSMITH_TRACING; requires "
                             "LANGSMITH_API_KEY)")
    parser.add_argument("--langsmith-project", default=rx.os.environ.get("LANGSMITH_PROJECT"),
                        help="LangSmith project name (env: LANGSMITH_PROJECT, "
                             "default: datamosaic-extraction)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="work directory for Docling text and engine artifacts "
                             "(default: <output dir>/dbmosaic_work)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    doc = args.doc
    if not doc.is_file():
        sys.exit(f"Document not found: {doc}")
    docs_dir = doc.parent

    output = args.output or docs_dir.parent / "single" / f"{doc.stem}.json"
    schema_file = args.schema or docs_dir.parent / "schema.json"
    work_dir = args.work_dir or output.parent / "dbmosaic_work"

    if not schema_file.exists():
        sys.exit(f"Schema file not found: {schema_file} (use --schema to point at it)")

    rx.ensure_engine(args.api_base, args.api_key)

    raw_schema = json.loads(schema_file.read_text(encoding="utf-8"))
    schema = rx.convert_schema(raw_schema)
    schema_meta = rx.schema_info(raw_schema)
    table_order = rx.schema_table_order(raw_schema)

    print(f"Engine: DBMosaic ({rx.ENGINE_DIR})")
    print(f"Model : {args.model}")
    print(f"Schema: {schema_file} ({len(table_order)} tables)")
    print(f"Doc   : {doc}")
    print(f"Output: {output}\n")

    diagnostics = []
    text_files, page_index, item_index = rx._pdfs_to_text(
        docs_dir, work_dir / "docs_text", documents=[doc], diagnostics=diagnostics)
    if not text_files:
        sys.exit(f"No extractable text in {doc}")
    page_index = {doc.stem: page_index[doc.stem]}
    item_index = {doc.stem: item_index[doc.stem]}

    tracer = rx.make_tracer(
        args.langsmith, args.langsmith_project,
        f"extraction: {doc.name}",
        {"model": args.model,
         "schema": [t.get("name") for t in raw_schema.get("tables", [])],
         "documents": [doc.name]},
    )
    if tracer is not None:
        print(f"LangSmith tracing enabled (project: {rx.tracer_project(tracer)})\n")

    try:
        run_dir = rx.run_engine(text_files, schema, work_dir / "runs",
                                args.model, args.timeout, tracer=tracer)
        tables = rx.load_final_tables(run_dir)
    except SystemExit:
        raise
    except Exception as exc:
        rx.tracer_finish(tracer, outputs=None, error=f"extraction run failed: {exc}")
        sys.exit(f"Extraction run failed: {exc}")
    violation_stats, violation_details = rx.load_violation_stats(run_dir)
    enriched = rx.enrich_tables(tables, schema_meta, page_index,
                                violation_stats, item_index, violation_details)

    merged = {t: enriched.get(t, []) for t in table_order}
    for extra, rows in enriched.items():
        if extra not in merged:
            merged[extra] = rows

    entry = {
        "document": doc.name,
        "run_id": run_dir.name,
        "tables": merged,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    result_file = run_dir / "result.json"
    result_file.write_text(
        json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\nWrote {output} (enriched result also in {result_file}; "
          f"run artifacts in {run_dir})")
    for t, rows in merged.items():
        located = sum(1 for r in rows if r.get("_page_number"))
        avg_conf = round(sum(r.get("_confidence") or 0 for r in rows) / len(rows), 3) if rows else 0
        print(f"  {t:20s} {len(rows)} record(s), {located} page-located, avg confidence {avg_conf}")


if __name__ == "__main__":
    main()
