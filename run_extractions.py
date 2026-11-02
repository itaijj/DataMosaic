#!/usr/bin/env python3
"""Runnable document-to-database extraction example for DataMosaic.

Instead of prompting the LLM directly once per document, this script runs the
**DBMosaic agent engine** (module/DBMosaic, the `Extract` engine described in
the README). The engine orchestrates the full closed loop:

    extraction -> verification (constraint checks) -> repair -> final database

per table and per document, driven by the target schema provided on the
command line.

What it does:
  1. Ensures the DBMosaic submodule and its LLM credentials (llm/.env) exist.
  2. Converts the source documents (PDF) to plain text using **Docling**,
     keeping Docling's per-page structure (page numbers and raw page text come
     from Docling's document model and item provenance). The engine consumes
     text documents.
  3. Starts an asynchronous Doc2DB processing job through
     `modules.services.Doc2DBService` (the same service the Flask backend and
     test_dataset_evaluate.py use), streaming agent steps to the console.
  4. Reads the final per-table snapshots produced by the orchestrator and
     enriches every record with provenance metadata taken from the engine's
     internal data model (not part of the schema):
       - `_page_number`: page of the source PDF the record was grounded in
         (from Docling's item provenance, located via the row's evidence
         document and key values)
       - `_raw_text`:    the Docling item(s) on that page that contain the
         record, with table items narrowed to the matching data row(s);
         falls back to the full page text when no single item matches
       - `_confidence`:  0-1 confidence computed from engine-internal signals
         (cell fill rate vs. the schema, evidence documents attached to the
         row's CellData, and unresolved violations from violations.jsonl /
         fixes.jsonl for the row's tuple_id)
       - `_source_document`: source document the record was grounded in
       - `warnings`:     the specific engine violations recorded for this
         entry, annotated with whether the fixer resolved them
       - `excluded`:     English text segments from the record's evidence
         that are not covered by any of the entry's extracted values
  5. Writes the merged result to the output JSON, one key per table (in
     schema order), each mapping to a list of record objects.

Environment variables (all optional, CLI flags take precedence):
  API_BASE    base URL of the OpenAI-compatible API used by the engine
  API_KEY     API key
  MODEL       model id to use
  TIMEOUT     max seconds to wait for the agent run (default 3600)

  Credentials are only written to the engine's untracked llm/.env when that
  file does not exist yet; an existing file is left untouched.

Prerequisites:
  - git submodule update --init module/DBMosaic
  - python3 -m venv module/DBMosaic/.venv
  - module/DBMosaic/.venv/bin/pip install -r module/DBMosaic/requirements.txt docling

Usage:
  python3 run_extractions.py DOCS_DIR [OUTPUT_JSON] [--schema SCHEMA.json]
                               [--model MODEL] [--api-base URL] [--api-key KEY]
                               [--timeout SECONDS] [--work-dir DIR]

  DOCS_DIR is the directory of source documents; SCHEMA defaults to
  schema.json next to DOCS_DIR; OUTPUT_JSON defaults to answer.json next to
  DOCS_DIR; the work directory (Docling text, engine run artifacts) defaults
  to a 'dbmosaic_work' directory next to OUTPUT_JSON.
"""

import argparse
import json
import logging
import warnings
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "module" / "DBMosaic"

PAGE_MARKER = "\n\n===== [Page {n}] =====\n\n"


def ensure_engine(api_base: str, api_key: str) -> None:
    """Make sure the DBMosaic engine submodule and its credentials exist."""
    if not (ENGINE_DIR / "modules" / "services").is_dir():
        sys.exit(
            f"DBMosaic engine not found at {ENGINE_DIR}.\n"
            "Initialize the submodules first:\n"
            "  git submodule update --init module/DBMosaic"
        )

    # The engine reads its model credentials from the untracked llm/.env
    # (see the README installation section). Create it from the provided
    # settings if the user has not configured one yet; never overwrite it.
    env_file = ENGINE_DIR / "llm" / ".env"
    if not env_file.exists():
        if not (api_base and api_key):
            sys.exit(
                f"No engine credentials found at {env_file}.\n"
                "Provide --api-base/--api-key (or API_BASE/API_KEY env vars),\n"
                "or configure the engine's llm/.env yourself."
            )
        api_url = api_base.rstrip("/")
        if not api_url.endswith("/chat/completions"):
            api_url += "/chat/completions"
        env_file.write_text(
            f"API_KEY={api_key}\n"
            f"API_URL={api_url}\n"
            "LLM_REQUEST_TIMEOUT=300\n"
            "LLM_MAX_RETRIES=3\n"
            "LLM_RETRY_DELAY=3\n",
            encoding="utf-8",
        )
        print(f"Created engine credentials file: {env_file}")


def convert_schema(schema: dict) -> dict:
    """Convert schema format: 'fields' -> 'attributes' (engine input format)."""
    converted = json.loads(json.dumps(schema))
    for table in converted.get("tables", []):
        if "fields" in table:
            table["attributes"] = table.pop("fields")
    return converted


def schema_info(schema: dict) -> dict:
    """Map table name -> {'fields': [...], 'primary_key': str or None}."""
    info = {}
    for table in schema.get("tables", []):
        fields = [f["name"] for f in table.get("fields", [])]
        pk = next(
            (f["name"] for f in table.get("fields", [])
             if (f.get("constraints") or {}).get("primary_key")),
            None,
        )
        info[table["name"]] = {"fields": fields, "primary_key": pk}
    return info


def schema_table_order(schema: dict) -> list:
    """Table names in schema order (output key order)."""
    return [t["name"] for t in schema.get("tables", [])]


def _pdfs_to_text(docs_dir: Path, work_dir: Path, documents=None, diagnostics=None):
    """Convert the source PDFs to page-marked text files using Docling.

    The engine consumes text documents; the Docling document model provides
    the page structure: page numbers come from each text item's provenance
    (`item.prov[].page_no`) and the raw page text is the concatenation of the
    Docling text items on that page. Page markers let us recover the page
    number and raw page text for each extracted record afterwards.

    Returns (text_file_paths, page_index, item_index) where page_index maps
    the document stem to its list of Docling raw page texts and item_index
    maps the stem to {page_no: [item texts]} for subsection-level evidence.
    """
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    work_dir.mkdir(parents=True, exist_ok=True)
    text_files, page_index, item_index = [], {}, {}
    for pdf in (documents if documents is not None else sorted(docs_dir.glob("*.pdf"))):
        print(f"   (docling) {pdf.name}")
        result = converter.convert(str(pdf))
        if diagnostics is not None:
            for error in getattr(result, "errors", []) or []:
                diagnostics.append(f"Docling conversion diagnostic: {error}")
            for warning in getattr(result, "warnings", []) or []:
                diagnostics.append(f"Docling conversion warning: {warning}")
        doc = result.document

        # Group Docling items into pages via their provenance. Text items
        # contribute their raw text; tables are exported to Markdown so the
        # engine's table extractor keeps the row/column structure.
        items_by_page = {}
        for item, _ in doc.iterate_items():
            if type(item).__name__ == "TableItem":
                text = item.export_to_markdown()
            elif hasattr(item, "text"):
                text = item.text
            else:  # pictures etc. carry no text
                continue
            for prov in item.prov:
                items_by_page.setdefault(prov.page_no, []).append(text)

        if not items_by_page:
            print(f"   (no extractable text in {pdf.name}, skipped)")
            continue

        pages = ["\n".join(items_by_page.get(n, [])) for n in range(1, len(doc.pages) + 1)]
        out = work_dir / (pdf.stem + ".txt")
        out.write_text(
            f"Source document filename: {pdf.name}\n"
            + "".join(PAGE_MARKER.format(n=i + 1) + p for i, p in enumerate(pages)),
            encoding="utf-8",
        )
        text_files.append(str(out))
        page_index[pdf.stem] = pages
        item_index[pdf.stem] = {
            n: items_by_page.get(n, []) for n in range(1, len(doc.pages) + 1)
        }
    return text_files, page_index, item_index


class _ConversionWarningHandler(logging.Handler):
    """Capture conversion-time logs without suppressing normal console logging."""

    def __init__(self, messages):
        super().__init__(logging.WARNING)
        self.messages = messages

    def emit(self, record):
        self.messages.append(f"Docling [{record.levelname}] {record.name}: {record.getMessage()}")


def pdfs_to_text(docs_dir: Path, work_dir: Path, failures=None, document_warnings=None):
    """Convert independently and attribute conversion diagnostics to each PDF.

    Conversion is sequential, so warnings from Docling and its dependencies
    during each conversion belong to that PDF. Handlers are always removed.
    """
    failures = failures if failures is not None else {}
    document_warnings = document_warnings if document_warnings is not None else {}
    text_files, pages, items = [], {}, {}
    for pdf in sorted(docs_dir.glob("*.pdf")):
        messages = document_warnings.setdefault(pdf.name, [])
        handler = _ConversionWarningHandler(messages)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                try:
                    files, page_index, item_index = _pdfs_to_text(
                        docs_dir, work_dir, [pdf], diagnostics=messages)
                    if not files:
                        failures[pdf.name] = ["PDF conversion produced no extractable text"]
                        continue
                    text_files.extend(files)
                    pages.update(page_index)
                    items.update(item_index)
                finally:
                    messages.extend(
                        f"Docling Python warning [{w.category.__name__}]: {w.message} "
                        f"({w.filename}:{w.lineno})" for w in captured)
        except Exception as exc:
            failures[pdf.name] = [f"PDF conversion failed: {type(exc).__name__}: {exc}"]
        finally:
            root_logger.removeHandler(handler)
            handler.close()
    return text_files, pages, items


def write_document_status(path: Path, pdfs: list, failures: dict) -> dict:
    status = {
        "documents_processed": sum(pdf.name not in failures for pdf in pdfs),
        "documents_failed": failures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status


def run_engine(text_files: list, schema: dict, output_root: Path, model: str,
               run_timeout: int) -> Path:
    """Run the DBMosaic extraction -> verification -> repair agent loop."""
    sys.path.insert(0, str(ENGINE_DIR))

    # The engine's direct-LLM client falls back to 'gpt-4o' for unknown model
    # ids; wrap it so direct calls keep using the configured model.
    import llm.gpt as gpt

    configured_model = model
    if getattr(gpt, "_model_patched", False) is False:
        _orig_get_answer = gpt.get_answer

        def _model_get_answer(text, image=None, system_prompt=None, history=None, model=None):
            return _orig_get_answer(text, image=image, system_prompt=system_prompt, history=history, model=configured_model)

        gpt.get_answer = _model_get_answer
        gpt._model_patched = True
    from modules.services import Doc2DBService

    service = Doc2DBService(output_dir=str(output_root))

    # Multi-table schema: pass an empty table_name so all tables are processed.
    run_id = service.start_processing_async(
        documents=text_files,
        schema=schema,
        table_name="",
        nl_prompt="",
        model=model,
        processing_mode="auto",
        document_mode="single",
    )
    print(f"Run ID: {run_id}\n")

    queue = service.task_queues[run_id]
    final = None
    deadline = time.time() + run_timeout
    while time.time() < deadline:
        try:
            msg = queue.get(timeout=2)
        except Exception:  # queue.Empty
            if run_id not in service.running_tasks:
                break
            continue

        msg_type = msg.get("type")
        if msg_type == "step_completed":
            print(f"  [step] {msg.get('description') or msg.get('step')}")
        elif msg_type == "final_status":
            final = msg
            break
        elif msg_type == "error":
            sys.exit(f"Agent run failed: {msg.get('error')}")

    if final is None:
        sys.exit(f"Agent run did not finish within {run_timeout}s")

    status = final.get("status")
    if status != "completed":
        sys.exit(f"Agent run ended with status: {status}")
    return output_root / run_id


def load_final_tables(run_dir: Path) -> dict:
    """Load the best (final-stage) snapshot per table from snapshots.jsonl.

    Returns {table: [{'tuple_id', 'evidences', 'values': {field: value}}]}.
    """
    snapshots_file = run_dir / "snapshots.jsonl"
    if not snapshots_file.exists():
        sys.exit(f"No snapshots written by the engine: {snapshots_file}")

    by_table = {}
    with open(snapshots_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            snap = json.loads(line)
            table = snap.get("table")
            if table:
                by_table.setdefault(table, []).append(snap)

    tables = {}
    for table, snaps in by_table.items():
        # Prefer the final-stage snapshot (post verification + repair);
        # otherwise the most complete earlier-stage snapshot.
        stage_order = {"final": 0, "verification": 1, "fixing": 2, "extraction": 3}
        snaps.sort(key=lambda s: (stage_order.get(s.get("processing_stage"), 9), -s.get("rows_count", 0)))
        chosen = snaps[0]
        rows = []
        for row in chosen.get("rows", []):
            values, evidences = {}, set()
            for attr, cell in (row.get("cells") or {}).items():
                if isinstance(cell, dict):
                    values[attr] = cell.get("value", cell.get("best", {}).get("value", ""))
                    evidences.update(e for e in cell.get("evidences", []) if e)
                else:
                    values[attr] = cell
            if values:
                rows.append({"tuple_id": row.get("tuple_id", ""), "evidences": evidences, "values": values})
        if rows:
            tables[table] = rows
    return tables


def load_violation_stats(run_dir: Path):
    """Aggregate the engine's violations/fixes per (table, tuple_id).

    Returns (stats, details):
      stats:   {(table, tuple_id): {'violations': n, 'fixed': m}}
      details: {(table, tuple_id): [violation dicts]} with the specific
               violations recorded by the engine for that entry, annotated
               with 'resolved' when a successful fix exists for the same
               table/tuple/attribute.
    """
    stats, details = {}, {}

    def bump(table, tuple_id, key):
        stats.setdefault((table, tuple_id), {"violations": 0, "fixed": 0})[key] += 1

    fixed_attrs = set()
    fixes_file = run_dir / "fixes.jsonl"
    if fixes_file.exists():
        with open(fixes_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("fix_success", True) and rec.get("table") and rec.get("tuple_id"):
                    fixed_attrs.add((rec["table"], rec["tuple_id"], rec.get("attr")))
                    bump(rec["table"], rec["tuple_id"], "fixed")

    violations_file = run_dir / "violations.jsonl"
    if violations_file.exists():
        with open(violations_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                table, tuple_id = rec.get("table"), rec.get("tuple_id")
                if not (table and tuple_id):
                    continue
                bump(table, tuple_id, "violations")
                details.setdefault((table, tuple_id), []).append({
                    "attr": rec.get("attr"),
                    "constraint_type": rec.get("constraint_type"),
                    "severity": rec.get("severity"),
                    "description": rec.get("description"),
                    "suggested_fix": (rec.get("suggested_fix") or {}).get("value"),
                    "resolved": (table, tuple_id, rec.get("attr")) in fixed_attrs,
                })
    return stats, details


def evidence_doc(evidences) -> str:
    """Resolve the source document name from a row's CellData evidences."""
    for e in sorted(evidences or []):
        name = re.sub(r"^\[Warm Start Fix\]\s*", "", e).strip()
        if name and not name.startswith("["):
            return Path(name).stem
    return ""


def _norm(s: str) -> str:
    """Normalize text for evidence matching.

    The extractor may normalize spacing/punctuation that Docling keeps
    (e.g. 'I7- 9750H' vs 'I7-9750H', or en dash '-' vs hyphen '-'), so
    matching folds case, whitespace and punctuation entirely.
    """
    return re.sub(r"[^0-9a-z]", "", str(s).lower())


def _row_norm_values(row: dict, pk: str) -> list:
    """Normalized (folded) versions of a row's values, PK first."""
    values = []
    if pk and row["values"].get(pk):
        values.append(str(row["values"][pk]))
    values.extend(str(v) for v in row["values"].values() if v not in (None, ""))
    return [v for v in dict.fromkeys(_norm(x) for x in values) if len(v) >= 3]


def locate_page(row: dict, pk: str, page_index: dict):
    """Find (doc_stem, page_number, page_text, norm_values) for a record.

    Uses the row's evidence document, then scores each page by how many of
    the row's values appear on it (primary key counted first, so pages that
    repeat header fields do not outweigh the page actually carrying the
    record's content, e.g. the description of a line item). Matching folds
    case, whitespace and punctuation, and matches are weighted by their
    length so a full description outweighs header fields that repeat on
    every page.
    """
    doc = evidence_doc(row["evidences"])
    candidates = [doc] if doc in page_index else list(page_index)
    norm_values = _row_norm_values(row, pk)

    best = None
    for stem in candidates:
        for i, page in enumerate(page_index[stem], start=1):
            norm_page = _norm(page)
            score = sum(len(v) for v in norm_values if v in norm_page)
            if score and (best is None or score > best[0]):
                best = (score, stem, i, page)
    if best:
        return best[1], best[2], best[3], norm_values
    return doc or None, None, None, norm_values


def _narrow_table_to_rows(item_text: str, norm_values: list) -> str:
    """Keep only the matching data row(s) of a Markdown table item.

    Docling models a whole table as a single item; split its Markdown export
    into the header (first two pipe lines) plus only the data rows whose
    normalized text contains one of the record's values. Returns the item
    unchanged when it is not a table or no row matches.
    """
    lines = item_text.splitlines()
    pipe_idx = [i for i, l in enumerate(lines) if l.strip().startswith("|")]
    if len(pipe_idx) < 3:  # header + separator + at least one data row
        return item_text
    header = pipe_idx[:2]
    body = [i for i in pipe_idx[2:] if any(v in _norm(lines[i]) for v in norm_values)]
    if not body:
        return item_text
    return "\n".join(lines[i] for i in sorted(header + body))


def locate_subsection(stem, page: int, norm_values: list, item_index: dict):
    """Narrow the evidence to the Docling item(s) containing the record.

    item_index maps doc stem -> {page_no: [item texts]}, where each item is
    a Docling text item or a Markdown-exported table. Table items are
    further narrowed to the specific data row(s) matching the record.
    Returns the matched evidence joined (in document order, deduplicated),
    or None when no single item matches (e.g. the extractor merged fragments
    across items).
    """
    if not stem or not page:
        return None
    items = item_index.get(stem, {}).get(page, [])
    matched = [
        _narrow_table_to_rows(it, norm_values) if any(v in _norm(it) for v in norm_values) else None
        for it in items
    ]
    return "\n".join(dict.fromkeys(m for m in matched if m)) or None


def excluded_text(raw: str, values: list) -> list:
    """Text in the record's evidence that no extracted value covers.

    Marks the character spans of every extracted value inside the evidence
    text (normalized, case/whitespace/punctuation-insensitive) and returns
    the remaining segments that contain English words, as a list. Numeric-only
    or symbolic leftovers are dropped.
    """
    if not raw:
        return []

    # Index map from normalized characters back to positions in `raw`.
    norm_chars = [(ch.lower(), i) for i, ch in enumerate(raw) if ch.isalnum()]
    ns = "".join(c for c, _ in norm_chars)
    covered = [False] * len(raw)
    for value in values:
        nv = _norm(value)
        if len(nv) < 3:
            continue
        start = 0
        while True:
            j = ns.find(nv, start)
            if j == -1:
                break
            for k in range(j, j + len(nv)):
                covered[norm_chars[k][1]] = True
            start = j + len(nv)

    segments, current = [], []
    for i, ch in enumerate(raw):
        if covered[i]:
            if current:
                segments.append("".join(current))
                current = []
        elif ch.isalnum() or (current and (ch.isspace() or ch in "'-&./")):
            current.append(ch)
        elif current:
            segments.append("".join(current))
            current = []
    if current:
        segments.append("".join(current))

    excluded = []
    for seg in segments:
        seg = seg.strip(" -|.\n\t")
        if re.search(r"[A-Za-z]{2,}", seg):
            excluded.append(seg)
    return excluded


def compute_confidence(row: dict, fields: list, violations: int, fixed: int) -> float:
    """Compute a 0-1 confidence from engine-internal signals.

    - fill rate:   non-empty cells / schema fields for the table
    - evidence:    the engine attached source documents to the row's cells
    - integrity:   constraint violations on the row's tuple_id that were not
                   repaired by the fixer (each unresolved violation lowers it)
    """
    fill = sum(1 for f in fields if row["values"].get(f) not in (None, "")) / max(1, len(fields))
    evidence = 1.0 if row["evidences"] else 0.0
    unresolved = max(0, violations - fixed)
    integrity = max(0.0, 1.0 - 0.2 * unresolved)
    return round(0.5 * fill + 0.2 * evidence + 0.3 * integrity, 3)


def enrich_tables(tables: dict, schema_meta: dict, page_index: dict, violation_stats: dict,
                  item_index: dict = None, violation_details: dict = None) -> dict:
    """Attach provenance metadata to every record (not part of the schema).

    - `_page_number` / `_raw_text` / `_confidence` / `_source_document` as before
    - `warnings`: the specific engine violations recorded for this entry
      (from violations.jsonl, annotated with whether the fixer resolved them)
    - `excluded`: English text segments from the record's evidence that are
      not covered by any of the entry's extracted values

    `_raw_text` is narrowed to the Docling item(s) on the located page that
    contain the record, with table items narrowed to the matching data
    row(s); it falls back to the full page text when no single item matches.
    """
    item_index = item_index or {}
    violation_details = violation_details or {}
    enriched = {}
    for table, rows in tables.items():
        meta = schema_meta.get(table, {})
        fields, pk = meta.get("fields", list(rows[0]["values"]) if rows else []), meta.get("primary_key")
        out_rows = []
        for row in rows:
            record = dict(row["values"])
            doc, page, page_text, norm_values = locate_page(row, pk, page_index)
            raw = locate_subsection(doc, page, norm_values, item_index) or page_text
            stats = violation_stats.get((table, row["tuple_id"]), {"violations": 0, "fixed": 0})
            record["_page_number"] = page
            record["_raw_text"] = raw
            record["_confidence"] = compute_confidence(row, fields or list(record), stats["violations"], stats["fixed"])
            if doc:
                record["_source_document"] = doc + ".pdf"
            record["warnings"] = violation_details.get((table, row["tuple_id"]), [])
            record["excluded"] = excluded_text(raw, [v for v in row["values"].values() if v not in (None, "")])
            out_rows.append(record)
        enriched[table] = out_rows
    return enriched


def write_document_answers(answer: dict, pdfs: list, output_dir: Path,
                           failures: dict = None, document_warnings: dict = None) -> None:
    """Project the enriched plan-review tables into one JSON per input PDF.

    This is a lossless evidence projection, not another LLM extraction: raw
    evidence can span multiple comments and excluded fragments are not proof
    that an item was cleared. Unknown provenance is surfaced in warnings.
    """
    failures = failures or {}
    document_warnings = document_warnings or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {pdf.name: {
        "document": pdf.name, "jurisdiction": None, "rejections": [],
        "excluded": [], "warnings": (list(failures.get(pdf.name, []))
                                      + list(document_warnings.get(pdf.name, []))),
    } for pdf in pdfs}
    stems = {pdf.stem: pdf.name for pdf in pdfs}

    def resolve(value):
        if not isinstance(value, str):
            return None
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        return name if name in documents else stems.get(Path(name).stem)

    def source(row):
        return resolve(row.get("_source_document")) or resolve(row.get("document"))

    def warn(doc, message):
        if message not in documents[doc]["warnings"]:
            documents[doc]["warnings"].append(message)

    def metadata(doc, row):
        for warning in row.get("warnings") or []:
            warn(doc, warning)
        for fragment in row.get("excluded") or []:
            item = ({"text": fragment, "reason": "unmapped_evidence"}
                    if isinstance(fragment, str) else fragment)
            if item not in documents[doc]["excluded"]:
                documents[doc]["excluded"].append(item)

    report_docs = {}
    for row in answer.get("plan_review_report", []):
        doc = source(row) or resolve(row.get("id"))
        if not doc:
            continue
        if row.get("id"):
            report_docs[row["id"]] = doc
        jurisdiction = row.get("jurisdiction")
        if jurisdiction:
            previous = documents[doc]["jurisdiction"]
            if previous and previous != jurisdiction:
                warn(doc, f"Conflicting jurisdictions: {previous!r} and {jurisdiction!r}")
            else:
                documents[doc]["jurisdiction"] = jurisdiction
        metadata(doc, row)

    rejection_docs = {}
    parents = {}
    for row in answer.get("rejection", []):
        doc = (source(row) or report_docs.get(row.get("report_id"))
               or resolve(row.get("report_id")))
        if not doc:
            for name in documents:
                warn(name, f"Could not attribute rejection {row.get('rejection_id')!r} to a document")
            continue
        item = {
            "rejection_id": row.get("rejection_id"),
            "source_item_id": row.get("source_item_id"),
            "discipline": row.get("discipline"),
            "raw_text": row.get("_raw_text") or "",
            "canonical_category": row.get("canonical_category"),
            "code_references": [],
            "page": row.get("_page_number"),
            "source": "text",
            "confidence": row.get("_confidence"),
        }
        documents[doc]["rejections"].append(item)
        key = row.get("rejection_id")
        if key:
            rejection_docs.setdefault(key, set()).add(doc)
            parents.setdefault((doc, key), []).append(item)
        metadata(doc, row)
        if not item["raw_text"]:
            warn(doc, f"No evidence text located for rejection {key!r}")
        if item["page"] is None:
            warn(doc, f"No page located for rejection {key!r}")

    for row in answer.get("code_reference", []):
        key = row.get("rejection_id")
        doc = source(row)
        candidates = rejection_docs.get(key, set())
        if not doc and len(candidates) == 1:
            doc = next(iter(candidates))
        matches = parents.get((doc, key), [])
        if len(matches) != 1:
            for name in ([doc] if doc else documents):
                warn(name, f"Code reference {row.get('id')!r} has missing or ambiguous parent {key!r}")
            continue
        citation = row.get("code_reference")
        if isinstance(citation, str) and citation.strip():
            if citation not in matches[0]["code_references"]:
                matches[0]["code_references"].append(citation)
        else:
            warn(doc, f"Code reference {row.get('id')!r} is not a nonempty string")
        metadata(doc, row)

    for pdf in pdfs:
        document = documents[pdf.name]
        if document["excluded"]:
            warn(pdf.name, "Excluded fragments are unmapped evidence, not confirmed cleared comments")
        path = output_dir / (pdf.stem + ".json")
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Document-to-database extraction via the DBMosaic agent engine."
    )
    parser.add_argument("docs_dir", type=Path, help="directory containing the source documents (PDF)")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="output JSON path (default: <docs_dir>/../answer.json)")
    parser.add_argument("--schema", type=Path, default=None,
                        help="target database schema JSON (default: <docs_dir>/../schema.json)")
    parser.add_argument("--status-output", type=Path, default=None,
                        help="document status JSON (default: <output stem>_status.json)")
    parser.add_argument("--model", default=os.environ.get("MODEL", "gpt-4o"),
                        help="model id (env: MODEL, default: gpt-4o)")
    parser.add_argument("--api-base", default=os.environ.get("API_BASE"),
                        help="OpenAI-compatible API base URL (env: API_BASE)")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"),
                        help="API key (env: API_KEY)")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("TIMEOUT", "3600")),
                        help="max seconds to wait for the agent run (env: TIMEOUT, default: 3600)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="work directory for Docling text and engine run artifacts "
                             "(default: <output dir>/dbmosaic_work)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    docs_dir = args.docs_dir
    if not docs_dir.is_dir():
        sys.exit(f"Documents directory not found: {docs_dir}")
    output = args.output or docs_dir.parent / "answer.json"
    schema_file = args.schema or docs_dir.parent / "schema.json"
    work_dir = args.work_dir or output.parent / "dbmosaic_work"

    if not schema_file.exists():
        sys.exit(f"Schema file not found: {schema_file} (use --schema to point at it)")

    ensure_engine(args.api_base, args.api_key)

    raw_schema = json.loads(schema_file.read_text(encoding="utf-8"))
    schema = convert_schema(raw_schema)
    schema_meta = schema_info(raw_schema)
    table_order = schema_table_order(raw_schema)
    pdfs = sorted(docs_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {docs_dir}")

    print(f"Engine: DBMosaic ({ENGINE_DIR})")
    print(f"Model : {args.model}")
    if args.api_base:
        print(f"API   : {args.api_base}")
    print(f"Schema: {schema_file} ({len(table_order)} tables)")
    print(f"Docs  : {len(pdfs)} PDF(s) in {docs_dir}\n")

    status_file = args.status_output or output.with_name(output.stem + "_status.json")
    if status_file.resolve() == output.resolve():
        sys.exit("Status output must not overwrite extraction output")
    failures = {}
    document_warnings = {}
    text_files, page_index, item_index = pdfs_to_text(
        docs_dir, work_dir / "docs_text", failures, document_warnings)
    if not text_files:
        write_document_status(status_file, pdfs, failures)
        sys.exit("No extractable text in any PDF")

    try:
        run_dir = run_engine(text_files, schema, work_dir / "runs", args.model, args.timeout)
        tables = load_final_tables(run_dir)
    except (Exception, SystemExit) as exc:
        for pdf in pdfs:
            if pdf.name not in failures:
                failures[pdf.name] = [f"Shared extraction run failed: {exc}"]
        write_document_status(status_file, pdfs, failures)
        raise
    violation_stats, violation_details = load_violation_stats(run_dir)
    enriched = enrich_tables(tables, schema_meta, page_index, violation_stats, item_index, violation_details)

    merged = {t: enriched.get(t, []) for t in table_order}
    for extra, rows in enriched.items():
        if extra not in merged:
            merged[extra] = rows

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Also write the enriched records into the run's result.json so the run
    # artifact carries the full enriched output (the engine's own result.json
    # only contains the plain table records, written before enrichment).
    result_file = run_dir / "result.json"
    result_file.write_text(
        json.dumps({"run_id": run_dir.name, "tables": merged}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    docs_answer = output.parent / "docs_answer"
    write_document_answers(merged, pdfs, docs_answer, failures, document_warnings)
    status = write_document_status(status_file, pdfs, failures)
    print(f"Per-document answers: {docs_answer}")
    print(f"Document status: {status_file}")
    print(f"\nWrote {output} (enriched result also in {result_file}; run artifacts in {run_dir})")
    for t, rows in merged.items():
        located = sum(1 for r in rows if r.get("_page_number"))
        avg_conf = round(sum(r.get("_confidence") or 0 for r in rows) / len(rows), 3) if rows else 0
        print(f"  {t:20s} {len(rows)} record(s), {located} page-located, avg confidence {avg_conf}")
    return status


if __name__ == "__main__":
    main()
