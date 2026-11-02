#!/usr/bin/env python3
"""Run the standard extractor and summarize its plan-review result.

Usage:
  python run_plan_review.py DOCS_DIR [OUTPUT_JSON] --model gpt-4o
  python run_plan_review.py DOCS_DIR OUTPUT_JSON --report-only

Both JSON and Markdown reports are written (use --markdown to override its path).
All unrecognized options are forwarded to run_extractions.py (e.g. --schema,
--model, --timeout, --work-dir). The default report is <output stem>_report.json.
Document counts and filename-to-error-list failures come from the extractor's
status sidecar. For older results without a sidecar, row coverage is used as
an explicitly labeled fallback. Percentages are fractions, not 0–100.
"""

import argparse
from collections import Counter, defaultdict
import json
import html
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def build_report(result: dict, documents: list[str], examples_limit: int = 2,
                 status: dict | None = None) -> dict:
    """Aggregate final rejection rows without modifying the extraction result.

    Each final row is one occurrence. Missing categories/disciplines are grouped
    as 'Uncategorized'/'Unspecified'. Document identity is resolved from
    _source_document, report.document, or the schema's filename-stem report_id.
    Unknown document identities do not contribute to distinct-document counts.
    """
    names = sorted(set(documents))
    known = set(names)
    stems = {Path(name).stem: name for name in names}

    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            return None
        name = value.replace('\\', '/').rsplit('/', 1)[-1]
        if name in known:
            return name
        return stems.get(Path(name).stem)

    reports = result.get('plan_review_report', [])
    report_docs = {}
    processed = set()
    for row in reports:
        doc = (resolve(row.get('_source_document')) or resolve(row.get('document'))
               or resolve(row.get('id')))
        if doc:
            processed.add(doc)
            if row.get('id'):
                report_docs[row['id']] = doc

    groups = defaultdict(lambda: {'count': 0, 'docs': set(),
                                 'disciplines': Counter(), 'examples': []})
    rejections = result.get('rejection', [])
    for row in rejections:
        doc = (resolve(row.get('_source_document'))
               or report_docs.get(row.get('report_id'))
               or resolve(row.get('report_id')))
        if doc:
            processed.add(doc)
        category = str(row.get('canonical_category') or '').strip() or 'Uncategorized'
        discipline = str(row.get('discipline') or '').strip() or 'Unspecified'
        group = groups[category]
        group['count'] += 1
        group['disciplines'][discipline] += 1
        if doc:
            group['docs'].add(doc)
        raw = row.get('_raw_text')
        if isinstance(raw, str) and raw.strip() and raw not in group['examples']:
            if len(group['examples']) < examples_limit:
                group['examples'].append(raw)

    # Prefer extractor-reported outcomes, including documents with zero rows.
    if status is not None:
        failed = status['documents_failed']
        processed_count = status['documents_processed']
    else:
        failed = {name: ['No attributable report or rejection row; extraction status unavailable']
                  for name in sorted(known - processed)}
        processed_count = len(processed)
    total = len(rejections)
    categories = []
    for category, group in sorted(groups.items(), key=lambda item: (-item[1]['count'], item[0])):
        categories.append({
            'canonical_category': category,
            'occurrences': group['count'],
            'documents': len(group['docs']),
            'pct_of_documents': len(group['docs']) / processed_count if processed_count else 0.0,
            'pct_of_rejections': group['count'] / total if total else 0.0,
            'by_discipline': dict(sorted(group['disciplines'].items())),
            'examples': group['examples'],
        })
    return {
        'documents_processed': processed_count,
        'documents_failed': failed,
        'total_rejections': total,
        'categories': categories,
        'top_5': [row['canonical_category'] for row in categories[:5]],
    }


def render_markdown(report: dict) -> str:
    """Render the same summary as readable Markdown, without extra LLM calls."""
    def text(value):
        value = html.escape(str(value), quote=False)
        special = '\\`*_{}[]()#+.!|>~-'
        return ''.join(chr(92) + char if char in special else char for char in value)

    def cell(value):
        return text(value).replace('\r', '').replace('\n', '<br>')

    failed = report['documents_failed']
    lines = [
        '# Plan Review Summary', '',
        f"- Documents processed: **{report['documents_processed']}**",
        f'- Documents failed: **{len(failed)}**',
        f"- Total rejections: **{report['total_rejections']}**", '',
        '## Top 5 categories', '',
    ]
    lines.extend(f'{i}. {cell(category)}' for i, category in enumerate(report['top_5'], 1))
    if not report['top_5']:
        lines.append('No categories.')
    lines.extend(['', '## Categories', '',
                  '| Category | Occurrences | Documents | % of documents | % of rejections | By discipline |',
                  '| --- | ---: | ---: | ---: | ---: | --- |'])
    for category in report['categories']:
        disciplines = '; '.join(f'{name}: {count}' for name, count in category['by_discipline'].items())
        lines.append(
            f"| {cell(category['canonical_category'])} | {category['occurrences']} "
            f"| {category['documents']} | {category['pct_of_documents']:.2%} "
            f"| {category['pct_of_rejections']:.2%} | {cell(disciplines)} |")
    if not report['categories']:
        lines.extend(['', 'No rejections.'])
    lines.extend(['', 'Percentages use processed documents and total rejections as denominators.',
                  '', '## Evidence examples', ''])
    for category in report['categories']:
        lines.extend([f"### {cell(category['canonical_category'])}", ''])
        for example in category['examples']:
            lines.extend('> ' + text(line) for line in example.splitlines())
            lines.append('')
        if not category['examples']:
            lines.extend(['No evidence text available.', ''])
    if not report['categories']:
        lines.extend(['No examples.', ''])
    lines.extend(['## Failed documents', ''])
    if failed:
        for document, errors in sorted(failed.items()):
            lines.append(f'- **{cell(document)}**')
            lines.extend(f'  - {cell(error)}' for error in errors)
    else:
        lines.append('None.')
    return '\n'.join(lines).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('docs_dir', type=Path)
    parser.add_argument('output', type=Path, nargs='?')
    parser.add_argument('--report', type=Path, help='summary JSON output path')
    parser.add_argument('--markdown', type=Path, help='Markdown output path (default: JSON report path with .md suffix)')
    parser.add_argument('--status-output', type=Path, help='extraction status JSON (default: <output stem>_status.json)')
    parser.add_argument('--report-only', action='store_true', help='summarize an existing extraction JSON without LLM calls')
    args, extraction_options = parser.parse_known_args()
    output = args.output or args.docs_dir.parent / 'answer.json'
    report_path = args.report or output.with_name(output.stem + '_report.json')
    markdown_path = args.markdown or report_path.with_suffix('.md')
    if len({report_path.resolve(), markdown_path.resolve(), output.resolve()}) != 3:
        parser.error('Extraction, JSON report and Markdown paths must be distinct')
    if not args.docs_dir.is_dir():
        parser.error(f'Documents directory not found: {args.docs_dir}')
    documents = sorted(p.name for p in args.docs_dir.glob('*.pdf'))
    if not documents:
        parser.error(f'No PDFs found in {args.docs_dir}')
    if args.report_only and extraction_options:
        parser.error('Extraction options cannot be used with --report-only')
    status_path = args.status_output or output.with_name(output.stem + '_status.json')
    if status_path.resolve() in {output.resolve(), report_path.resolve(), markdown_path.resolve()}:
        parser.error('Extraction, status, JSON report and Markdown paths must be distinct')
    returncode = 0
    if not args.report_only:
        # Remove stale status, but never overwrite an existing answer on failure.
        status_path.unlink(missing_ok=True)
        completed = subprocess.run([sys.executable, str(ROOT / 'run_extractions.py'),
                                   str(args.docs_dir), str(output),
                                   '--status-output', str(status_path), *extraction_options])
        returncode = completed.returncode
        if returncode and not status_path.exists():
            raise SystemExit(returncode)
    status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else None
    # On a failed run, do not accidentally summarize a previous answer.json.
    result = {} if returncode else json.loads(output.read_text(encoding='utf-8'))
    report = build_report(result, documents, status=status)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding='utf-8')
    print(f'Wrote summary: {report_path}')
    print(f'Wrote Markdown: {markdown_path}')
    if returncode:
        raise SystemExit(returncode)


if __name__ == '__main__':
    main()
