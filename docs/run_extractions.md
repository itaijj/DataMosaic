# PDF extraction and plan-review reports

`run_extractions.py` runs the **DBMosaic agent engine** in `module/DBMosaic`:

```text
PDFs → Docling text → extraction → verification → repair → enriched JSON
                                                        → per-document JSON
                                                        → document status JSON
```

Run commands below from the repository root. The `.venv/bin/python` paths are
for Linux/WSL, matching the configuration used here.

## 1. Install dependencies

```bash
git submodule update --init module/DBMosaic
python3 -m venv module/DBMosaic/.venv
module/DBMosaic/.venv/bin/pip install -r module/DBMosaic/requirements.txt docling
```

## 2. Configure OpenAI

Create or edit **`module/DBMosaic/llm/.env`** (Windows path:
`module\DBMosaic\llm\.env`). Configure the engine with these exact names:

```dotenv
API_URL=https://api.openai.com/v1/chat/completions
API_KEY=YOUR_OPENAI_API_KEY
LLM_REQUEST_TIMEOUT=300
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY=3
```

Use your own OpenAI API key with access to `gpt-4o`. Keep this file untracked
and do not share the key or put it in example JSON files.

**Important:** `API_URL` must be the full chat-completions endpoint, **not**
`https://api.openai.com/v1`. The engine posts directly to this URL; the base
URL alone returns 404 and can cause `Expecting value: line 1 column 1` JSON
parsing errors. The credential names read by this client are **`API_URL` and
`API_KEY`**, not `OPENAI_BASE_URL` or `OPENAI_API_KEY`.

The engine loads these values from `module/DBMosaic/llm/.env`; setting only
shell variables is not a substitute for configuring that file. Select the
model with `--model gpt-4o` when running the script.

### Optional credential bootstrap

If `.env` does not exist, `run_extractions.py` can create it using
`--api-base https://api.openai.com/v1 --api-key ...` (or shell variables
`API_BASE` and `API_KEY`). This is only a bootstrap convenience: it appends
`/chat/completions` and writes `API_URL`/`API_KEY` to `.env`.

An existing `.env` is **never overwritten**, so these flags do not override
its credentials. Editing the file directly as above is the recommended setup
and avoids putting the key in shell history.

## 3. Put PDFs in the plan-review input directory

```text
examples/extract/plan_review/
├── schema.json
└── docs/
    ├── sample_01.pdf
    ├── sample_02.pdf
    ├── sample_03.pdf
    └── sample_04.pdf
```

Place your input PDFs directly in **`examples/extract/plan_review/docs/`**.
The script scans `*.pdf` in that directory, not nested directories; use the
lowercase `.pdf` extension. Each PDF should have a distinct filename stem.

The supplied `schema.json` has three tables: `plan_review_report`, `rejection`,
and `code_reference`. Table/field descriptions guide extraction, including:

- Report IDs use the source filename stem, e.g. `sample_03`.
- Rejection IDs use document-wide reading order, e.g. `sample_03-001`.
- Printed reviewer labels such as `BPEL2` stay in `source_item_id`.
- Citations require a code name and section/article number, e.g.
  `2023 NEC 408.36`; edition years must not be invented.
- Citations belong to the enclosing reviewer comment, not the first comment
  on a page or in a multi-comment Docling block. For example, `2023 NEC 250`
  in sample_01's BPEL2 belongs to `sample_01-002`, with citation ID
  `sample_01-002-code-001`.

These matching/ID instructions are LLM-guided, not a deterministic ID
allocator or a guarantee of correct associations. The citation field also
has a regex business rule; the engine reports its violations as warnings
for verification/repair rather than blocking output.

## 4. Run extraction

```bash
module/DBMosaic/.venv/bin/python run_extractions.py \
  examples/extract/plan_review/docs \
  examples/extract/plan_review/answer.json \
  --model gpt-4o
```

The default schema is `schema.json` next to the `docs` directory. Output files
are written alongside the specified `answer.json`:

```text
examples/extract/plan_review/
├── answer.json                    # merged enriched tables
├── answer_status.json             # processed count and filename → error lists
├── docs_answer/
│   ├── sample_01.json             # output for sample_01.pdf
│   ├── sample_02.json
│   ├── sample_03.json
│   └── sample_04.json
└── dbmosaic_work/
    ├── docs_text/                 # filename context + page-marked Docling text
    └── runs/<run_id>/             # engine logs, snapshots, violations and fixes
```

### Per-document output

After `enrich_tables`, `write_document_answers` joins report rows, rejections,
and their code references into **`docs_answer/<PDF stem>.json`**:

```json
{
  "document": "sample_03.pdf",
  "jurisdiction": "City of Austin, TX",
  "rejections": [
    {
      "rejection_id": "sample_03-001",
      "source_item_id": "BPEL1",
      "discipline": "Electrical",
      "raw_text": "Evidence text from the converted document",
      "canonical_category": "Panelboard overcurrent protection",
      "code_references": ["2023 NEC 408.36"],
      "page": 3,
      "source": "text",
      "confidence": 0.9
    }
  ],
  "excluded": [],
  "warnings": []
}
```

- `jurisdiction` is null when unidentified; no new jurisdiction is inferred
  during this projection.
- `source` is currently `text`, reflecting the text-based extraction pipeline;
  it does not distinguish image or annotation origins.
- `raw_text` preserves enriched Docling evidence. It may span multiple comments
  or fall back to a full page; it is not guaranteed to be an isolated item.
- `excluded` contains `{ "text": "...", "reason": "unmapped_evidence" }`
  fragments not covered by extracted values. They are **not** confirmed
  cleared comments.
- `warnings` gathers per-file Docling/dependency warning and error logs,
  Python warnings, structured conversion diagnostics, extraction violations,
  and projection issues such as missing pages or ambiguous citation parents.
  Conversion warnings are captured during the current run, not recoverable
  from an old `answer.json` alone. Warning entries may be strings or structured
  engine violation objects.
- Per-document files are written only after successful enrichment, not from
  the conversion or shared-engine failure paths.

### Document status

`answer_status.json` is separate from the unchanged table-based `answer.json`:

```json
{
  "documents_processed": 3,
  "documents_failed": {
    "broken.pdf": ["PDF conversion failed: ValueError: invalid PDF"]
  }
}
```

Conversion failures are recorded per PDF and other PDFs continue. A PDF with
no extractable text receives an error. If the shared engine run fails, every
submitted document receives that shared error because the engine does not
identify a single responsible PDF. A successful shared run counts converted
PDFs as processed; that count is not a guarantee that every expected row was
extracted. Status is also written on these conversion/shared-run failures.

## 5. Generate the category report later, without another extraction

After extraction has produced `answer.json`, run:

```bash
module/DBMosaic/.venv/bin/python run_plan_review.py \
  examples/extract/plan_review/docs \
  examples/extract/plan_review/answer.json \
  --report-only
```

**`--report-only` is important:** it reads existing results and makes no LLM
calls. Keep the matching `answer_status.json` beside `answer.json` so document
counts and errors come from the extraction run. Older answers without a status
file use row coverage as a fallback, with an explicit explanation for any
PDF considered failed.

The runner creates both **`examples/extract/plan_review/answer_report.json`**
and **`examples/extract/plan_review/answer_report.md`**. The Markdown version
includes totals, top five categories, a category statistics table, evidence
examples and per-document failure errors. Both outputs use the same summary;
Markdown displays the fractional percentages as human-readable percentages.

JSON report format:

```json
{
  "documents_processed": 4,
  "documents_failed": {},
  "total_rejections": 20,
  "categories": [
    {
      "canonical_category": "GFCI protection",
      "occurrences": 3,
      "documents": 2,
      "pct_of_documents": 0.5,
      "pct_of_rejections": 0.15,
      "by_discipline": {"Electrical": 3},
      "examples": ["Evidence example one", "Evidence example two"]
    }
  ],
  "top_5": ["GFCI protection"]
}
```

The example is illustrative, not an expected result for the supplied PDFs.
Categories are sorted by occurrence count descending (alphabetically on ties).
Each final rejection row counts once; document counts are distinct source
PDFs. Percentages are **fractions**, with processed documents and total
rejections as their respective denominators. Up to two distinct `_raw_text`
examples are included per category. Missing categories/disciplines are grouped
as `Uncategorized`/`Unspecified`. Category labels are not semantically merged.

To run extraction **and** reporting together instead:

```bash
module/DBMosaic/.venv/bin/python run_plan_review.py \
  examples/extract/plan_review/docs \
  examples/extract/plan_review/answer.json \
  --model gpt-4o
```

Without `--report-only`, this wrapper launches `run_extractions.py` first.
If extraction fails with a status file, it writes a failure report without
summarizing a stale answer, then exits nonzero.

## CLI reference

```text
run_extractions.py DOCS_DIR [OUTPUT_JSON]
  [--schema SCHEMA.json] [--model MODEL]
  [--api-base URL] [--api-key KEY]
  [--timeout SECONDS] [--work-dir DIR] [--status-output PATH]
```

| Argument | Default | Meaning |
|---|---|---|
| `DOCS_DIR` | required | Directory containing PDFs |
| `OUTPUT_JSON` | `<docs_dir>/../answer.json` | Merged enriched table output |
| `--schema` | `<docs_dir>/../schema.json` | Schema with `tables`, `fields`, constraints and descriptions |
| `--model` | shell `MODEL`, otherwise `gpt-4o` | Model identifier |
| `--api-base` | shell `API_BASE` | Bootstrap only when `.env` is missing; not the engine's `API_URL` setting |
| `--api-key` | shell `API_KEY` | Bootstrap key; existing `.env` is untouched |
| `--timeout` | shell `TIMEOUT`, otherwise `3600` | Shared agent run timeout in seconds |
| `--work-dir` | `<output dir>/dbmosaic_work` | Converted text and engine artifacts |
| `--status-output` | `<output stem>_status.json` beside output | Document status JSON |

`run_plan_review.py` accepts the same positional paths and forwards extraction
options when launching extraction. Its additional options are:

| Option | Meaning |
|---|---|
| `--report-only` | Read existing output; do not extract or call an LLM |
| `--report PATH` | Override `<output stem>_report.json` |
| `--markdown PATH` | Override the Markdown path (default: JSON report path with `.md` suffix) |
| `--status-output PATH` | Read/write a custom extraction status file |

Extraction options such as `--model` cannot be combined with `--report-only`.

## Enrichment and engine artifacts

The script converts schema `fields` to engine `attributes`, invokes
`modules.services.Doc2DBService`, streams agent steps, and loads the best
snapshot per table (preferring final-stage snapshots). Docling text includes
the original PDF filename and `===== [Page N] =====` markers; table items are
exported as Markdown.

Merged rows have automatic provenance fields outside the target schema:

| Field | Meaning |
|---|---|
| `_page_number` | Best matching source page using Docling provenance/evidence |
| `_raw_text` | Matching Docling items/table rows, or fallback page text |
| `_source_document` | Source PDF identified from engine evidence |
| `_confidence` | `0.5 × fill + 0.2 × evidence + 0.3 × integrity` |
| `warnings` | Engine violations annotated with repair status |
| `excluded` | Evidence fragments not covered by that row's extracted values |

`fill` is populated schema cells divided by field count; `evidence` is 1 when
engine evidence exists; `integrity` is `max(0, 1 − 0.2 × unresolved violations)`.
This is a heuristic, not a calibrated probability. Coverage/excluded analysis
is per row, so another table may capture text excluded from the current row.

Each `<work-dir>/runs/<run_id>/` contains `snapshots.jsonl`, `violations.jsonl`,
`fixes.jsonl`, and engine logs. The script replaces the engine's plain
`result.json` with `{ "run_id": "...", "tables": ... }` containing the same
enriched tables as `answer.json`.

For platform details, see the [main README](../README.md) and the
[DBMosaic engine](../module/DBMosaic).
