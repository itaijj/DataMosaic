# run_extractions.py — Document-to-Database Extraction via the DBMosaic Agent Engine

`run_extractions.py` (repo root) turns a directory of source documents plus a
target database schema into structured, relational records by driving the full **DBMosaic agent engine**
(`module/DBMosaic`, the `Extract` engine described in the main
[README](README.md)) through its orchestrated closed loop:

```
extraction → verification (constraint checks) → repair → final database
```

The engine mediates between LLM extraction and strict database logic: schema
rules, foreign keys, and business constraints guide targeted corrections, so
the result is a structurally sound database rather than a list of isolated
fields.

---

## Quick start

```bash
# 1. One-time setup
git submodule update --init module/DBMosaic
python3 -m venv module/DBMosaic/.venv
module/DBMosaic/.venv/bin/pip install -r module/DBMosaic/requirements.txt docling

# 2. Run (invoice example from this repo)
module/DBMosaic/.venv/bin/python run_extractions.py \
    examples/extract/invoice/docs \
    examples/extract/invoice/answer.json \
    --model model_id

# 3. Inspect the result
#    examples/extract/invoice/answer.json                        (final output)
#    examples/extract/invoice/dbmosaic_work/runs/<run_id>/       (run artifacts)
```

## CLI usage

```
python3 run_extractions.py DOCS_DIR [OUTPUT_JSON] [--schema SCHEMA.json]
                            [--model MODEL] [--api-base URL] [--api-key KEY]
                            [--timeout SECONDS] [--work-dir DIR]
```

| Argument | Default | Description |
|---|---|---|
| `DOCS_DIR` | (required) | Directory containing the source documents (PDF) |
| `OUTPUT_JSON` | `<docs_dir>/../answer.json` | Where the merged result is written |
| `--schema` | `<docs_dir>/../schema.json` | Target database schema (JSON, see below) |
| `--model` | `gpt-4o` (env `MODEL`) | Model id used by the engine |
| `--api-base` | env `API_BASE` | OpenAI-compatible API base URL |
| `--api-key` | env `API_KEY` | API key |
| `--timeout` | `3600` (env `TIMEOUT`) | Max seconds to wait for the agent run |
| `--work-dir` | `<output dir>/dbmosaic_work` | Docling text + engine run artifacts |

Environment variables: `API_BASE`, `API_KEY`, `MODEL`, `TIMEOUT` (CLI flags
take precedence). Credentials are only written to the engine's untracked
`module/DBMosaic/llm/.env` when that file does not exist yet — an existing
file is never overwritten, matching the engine's own credential handling (see
the main README installation section).

### Schema format

The schema is the same JSON used by the other `examples/extract/case*`
examples and the engine's evaluation harness: a `tables` list where each table
has `name`, `type`, and `fields` with `type`, `constraints`
(`primary_key`, `foreign_key`, `nullable`), and a human `description` per
field. The script converts `fields` → `attributes` internally (the engine's
input format) and derives the output table order from the schema.

---

## How it works inside

The script runs five phases:

### 1. Engine & credential setup (`ensure_engine`)

Verifies the `module/DBMosaic` submodule is checked out, and bootstraps
`module/DBMosaic/llm/.env` from `--api-base/--api-key` if missing. The engine
reads its model credentials from that file (via `llm/utils.load_env_config`).

### 2. Document conversion with Docling (`pdfs_to_text`)

The engine consumes plain text documents, so each PDF is converted with
[Docling](https://github.com/docling-project/docling). Docling's document
model is preserved:

- Page numbers come from each item's **provenance** (`item.prov[].page_no`).
- Text items contribute their raw text; **tables are exported to Markdown** so
  the engine's table extraction keeps the row/column structure.
- Items are grouped per page, and pages are written to
  `<work-dir>/docs_text/<doc>.txt` with `===== [Page N] =====` markers.

The per-page text and per-item structure are kept in memory (`page_index`,
`item_index`) for the enrichment phase.

### 3. The agent run (`run_engine`)

Starts an asynchronous job through `modules.services.Doc2DBService` — the same
service layer the engine's Flask backend and `test_dataset_evaluate.py` use —
with the converted text files, the converted schema, and the configured model.
Because the schema is multi-table, an empty `table_name` is passed so **all
tables** are processed. With multiple tables the engine auto-selects its
**parallel orchestrator**, which runs per table and per document:

- **Extractor** — entity/relation extraction per table × document
  (LangChain structured output with Pydantic parsing and automatic output
  repair; falls back to a Markdown-table prompt).
- **Verifier (MCP plugins)** — constraint checks: structure, type, value,
  logic, format, temporal, reference, aggregation, and data quality, plus
  cross-table (foreign-key) checks.
- **Fixer** — targeted re-extraction/repair of violating cells
  (warm-start, cell-level fixes).
- **Orchestrator** — signal-driven loop that repeats
  verify→repair until constraints hold, persisting per-stage snapshots to
  `snapshots.jsonl`, `violations.jsonl`, and `fixes.jsonl` in
  `<work-dir>/runs/<run_id>/`.

The script streams each agent step to the console and waits for the
`final_status` message.

### 4. Result loading (`load_final_tables`, `load_violation_stats`)

- Reads `snapshots.jsonl` and selects, per table, the **final-stage
  snapshot** (post verification + repair); if absent, the most complete
  earlier-stage snapshot.
- Each row keeps its engine-internal identity: `tuple_id` and the
  `evidences` (source documents) attached to each cell's `CellData`.
- Aggregates `violations.jsonl` / `fixes.jsonl` per `(table, tuple_id)`.

### 5. Enrichment (`enrich_tables`)

Every record is enriched with provenance and quality metadata taken from the
engine's internal data model (all automatic, **not part of the schema**):

| Field | Meaning | Source |
|---|---|---|
| `_page_number` | Page of the source PDF the record is grounded in | Docling item provenance + evidence document, located by scoring pages against the record's values |
| `_raw_text` | The Docling item(s) containing the record, with tables narrowed to the matching data row(s) | Docling item text / Markdown table export |
| `_confidence` | 0–1 confidence | `0.5·fill + 0.2·evidence + 0.3·integrity` (see below) |
| `_source_document` | Source document name | Engine `CellData.evidences` |
| `warnings` | Specific constraint violations recorded for this entry, with `resolved` flag | `violations.jsonl` + `fixes.jsonl` by `tuple_id` |
| `excluded` | English text segments from the record's evidence not covered by any extracted value | Character-coverage analysis of `_raw_text` vs. the record's values |

**Page/evidence matching** folds case, whitespace, and punctuation (the
extractor may normalize `I7- 9750H` to `I7-9750H`, or en dashes to hyphens),
and weights matches by length so a full line-item description outweighs
header fields that repeat on every page of an invoice.

**Confidence formula:**

- *fill* — non-empty cells ÷ schema fields for the table
- *evidence* — 1.0 when the engine attached source documents to the row
- *integrity* — `1 − 0.2 × unresolved_violations` (violations on the row's
  `tuple_id` that the fixer did not repair)

**`excluded` semantics:** computed per entry against *that entry's* values
only — shared evidence (e.g. a seller address block on an invoice page) shows
up as excluded for the `invoice` record even though it is mapped in the
`seller` table. This surfaces both text no table captured and
extraction/evidence mismatches.

### Outputs

```
<OUTPUT_JSON>                              merged result: {table: [records]}
<work-dir>/docs_text/                      Docling page-marked text per document
<work-dir>/runs/<run_id>/snapshots.jsonl   per-stage table snapshots (engine)
<work-dir>/runs/<run_id>/violations.jsonl  constraint violations (engine)
<work-dir>/runs/<run_id>/fixes.jsonl       applied repairs (engine)
<work-dir>/runs/<run_id>/result.json       same enriched records as OUTPUT_JSON,
                                           wrapped as {run_id, tables}
<work-dir>/runs/<run_id>/run.log           full engine log
```

Note: the engine writes its own `result.json` (plain records) when the run
completes; the script rewrites it with the enriched records afterwards, so the
run artifact carries the full output.

---

## Relationship to the rest of DataMosaic

- The engine invoked here (`module/DBMosaic`) is the same one behind the
  **Extract** application (`/dbmosaic/`) of the DataMosaic portal — this
  script is its headless, importable-library entry point (no Flask backend or
  frontend needed).
- The output format matches the other extraction examples
  (`examples/extract/case*/answer.json`), extended with the enrichment
  fields above.
- See the [main README](README.md) for the full platform (DocMosaic/Analyze,
  DBMosaic/Extract, OntoMosaic) and [module/DBMosaic](module/DBMosaic) for
  the engine's own documentation.
