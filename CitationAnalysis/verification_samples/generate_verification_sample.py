#!/usr/bin/env python3
"""
generate_verification_sample.py — Draw a random sample of bibliography
entries for manual expert verification, formatted for a non-technical
subject-matter expert to scan in Google Sheets.

Pulls a random sample (default n=100, seeded for reproducibility) from
bibliography.json and writes both an .xlsx and a .csv with columns
ordered for human scanning: the identifying/citation info a reviewer
reads first, then the extracted structured fields to check against it,
then extraction/correction provenance (detection method, confidence,
completeness, enrichment source, correction history) so a reviewer can
judge how much to trust an entry, then a raw source excerpt for
ground-truth comparison, then blank reviewer-input columns last.

Excluded: only the handful of fields that are pure internal bookkeeping
with no verification value even to a careful reviewer (_grobid_id,
internal split/merge/rename linkage pointers like _split_from,
_split_into, _merged_into, _renamed_from — these reference other
citekeys' internal history, not this entry's own correctness).
Everything else in the record, including data-quality flags
(_titleless_duplicate_candidate, _cross_script_duplicate_candidate,
_title_too_long, _catalogue_candidate, _year_possibly_absorbed,
_author_recovery_failed, _placeholder_title) and correction notes, is
included since these directly bear on whether an entry is trustworthy.

The .xlsx carries formatting (header styling, column widths, frozen
header row, wrapped text, highlighted reviewer columns) for direct use;
the .csv is the same data with none of that. Both import identically
into Google Sheets (File > Import); only the .xlsx keeps formatting.

Usage:
    python3 generate_verification_sample.py bibliography.json [--n 100] [--seed 42] [--out-dir verification_samples]

    Writes verification_sample_n<N>_seed<SEED>_<DATE>_<TIME>.xlsx and .csv
    into --out-dir (default: verification_samples/, created alongside
    this script if it doesn't exist). Pass --basename to name the files
    something else.
"""

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter


def format_authors(people):
    if not people:
        return ""
    parts = []
    for a in people:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            parts.append(f"{family}, {given}")
        elif family:
            parts.append(family)
        elif given:
            parts.append(given)
    return "; ".join(parts)


def format_container(entry):
    journal = (entry.get("journaltitle") or "").strip()
    book = (entry.get("booktitle") or "").strip()
    return journal or book


def format_list(value, max_shown=8):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if len(value) <= max_shown:
        return ", ".join(str(v) for v in value)
    shown = ", ".join(str(v) for v in value[:max_shown])
    return f"{shown}, +{len(value) - max_shown} more"


def format_completeness(entry):
    c = entry.get("completeness")
    if not c:
        return "", ""
    score = c.get("score")
    label = c.get("label", "")
    score_str = f"{score:.2f}" if isinstance(score, (int, float)) else ""
    missing = c.get("required_missing", []) + c.get("recommended_missing", [])
    detail = f"{label}" + (f" (missing: {', '.join(missing)})" if missing else "")
    return score_str, detail


def format_flags(entry):
    """Data-quality flag fields, shown together as one human-readable summary."""
    flags = []
    flag_fields = [
        "_catalogue_candidate", "_titleless_duplicate_candidate",
        "_cross_script_duplicate_candidate", "_title_too_long",
        "_year_possibly_absorbed", "_author_recovery_failed",
        "_placeholder_title", "_deleted",
    ]
    for f in flag_fields:
        v = entry.get(f)
        if v:
            label = f.lstrip("_")
            flags.append(label if v is True else f"{label}={v}")
    return "; ".join(flags)


def build_row(citekey, entry):
    completeness_score, completeness_detail = format_completeness(entry)
    return {
        "Citekey": citekey,
        "Title": entry.get("title") or "",
        "Author(s)": format_authors(entry.get("author")),
        "Editor(s)": format_authors(entry.get("editor")),
        "Translator(s)": format_authors(entry.get("translator")),
        "Year": entry.get("year") or "",
        "Date": entry.get("date") or "",
        "Published In": format_container(entry),
        "Entry Type": entry.get("entry_type") or "",
        "Entry Type (original)": entry.get("_entry_type_original") or "",
        "Publisher": entry.get("publisher") or "",
        "Location": entry.get("location") or "",
        "Series": entry.get("series") or "",
        "Volume": entry.get("volume") or "",
        "Number": entry.get("number") or "",
        "Pages": entry.get("pages") or "",
        "Event/Title": entry.get("eventtitle") or "",
        "DOI": entry.get("doi") or "",
        "URL": entry.get("url") or "",
        "Note": entry.get("note") or "",
        "Generation": entry.get("generation") or "",
        "Detection Method": entry.get("_resolution_method") or "",
        "Detection Confidence": entry.get("_resolution_confidence") or "",
        "Enriched Via": entry.get("_enriched_via") or "",
        "Completeness Score": completeness_score,
        "Completeness Detail": completeness_detail,
        "Data Quality Flags": format_flags(entry),
        "Correction Note": entry.get("_correction_note") or "",
        "Corrections Applied": format_list(entry.get("_corrections_applied")),
        "Source PDF": entry.get("_source_pdf") or "",
        "Source Footnote": entry.get("_source_footnote") or "",
        "Raw Citation (as extracted)": entry.get("_raw_citation") or "",
        "Cited By (citekeys)": format_list(entry.get("cited_by")),
        "Reviewer: Correct? (Y/N)": "",
        "Reviewer: Notes": "",
    }


COLUMN_WIDTHS = {
    "Citekey": 22,
    "Title": 44,
    "Author(s)": 26,
    "Editor(s)": 22,
    "Translator(s)": 18,
    "Year": 8,
    "Date": 12,
    "Published In": 26,
    "Entry Type": 12,
    "Entry Type (original)": 14,
    "Publisher": 20,
    "Location": 14,
    "Series": 18,
    "Volume": 8,
    "Number": 8,
    "Pages": 10,
    "Event/Title": 16,
    "DOI": 18,
    "URL": 22,
    "Note": 20,
    "Generation": 10,
    "Detection Method": 18,
    "Detection Confidence": 14,
    "Enriched Via": 14,
    "Completeness Score": 12,
    "Completeness Detail": 26,
    "Data Quality Flags": 26,
    "Correction Note": 34,
    "Corrections Applied": 20,
    "Source PDF": 28,
    "Source Footnote": 34,
    "Raw Citation (as extracted)": 44,
    "Cited By (citekeys)": 22,
    "Reviewer: Correct? (Y/N)": 20,
    "Reviewer: Notes": 34,
}

WRAP_COLUMNS = {
    "Title", "Author(s)", "Editor(s)", "Published In", "Note",
    "Completeness Detail", "Data Quality Flags", "Correction Note",
    "Source Footnote", "Raw Citation (as extracted)", "Reviewer: Notes",
}


def write_xlsx(rows, columns, output_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Verification Sample"

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    body_font = Font(name="Arial", size=10)
    reviewer_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

    for col_idx, col_name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = COLUMN_WIDTHS[col_name]

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(columns, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=row[col_name])
            cell.font = body_font
            wrap = col_name in WRAP_COLUMNS
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=wrap)
            if col_name.startswith("Reviewer:"):
                cell.fill = reviewer_fill

    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30
    for row_idx in range(2, len(rows) + 2):
        ws.row_dimensions[row_idx].height = 45

    wb.save(output_path)


def write_csv(rows, columns, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bib_path", type=Path)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--out-dir", type=Path, default=Path(__file__).parent / "verification_samples",
        help="Directory to write outputs into (default: verification_samples/ next to this script). Created if it doesn't exist.",
    )
    parser.add_argument(
        "--basename", type=str, default=None,
        help="Base filename (no extension). Default: verification_sample_n<N>_seed<SEED>.",
    )
    parser.add_argument(
        "--exclude-deleted", action="store_true", default=True,
        help="Exclude entries marked _deleted (default: on — deleted entries aren't live records to verify).",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    basename = args.basename or f"verification_sample_n{args.n}_seed{args.seed}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    output_basename = args.out_dir / basename

    with open(args.bib_path) as f:
        bib = json.load(f)

    candidates = [
        (ck, e) for ck, e in bib.items()
        if not (args.exclude_deleted and e.get("_deleted"))
    ]

    if len(candidates) < args.n:
        print(f"WARNING: only {len(candidates)} eligible entries, less than requested n={args.n}", file=sys.stderr)

    rng = random.Random(args.seed)
    sample = rng.sample(candidates, min(args.n, len(candidates)))

    rows = [build_row(ck, e) for ck, e in sample]
    columns = list(COLUMN_WIDTHS.keys())

    xlsx_path = output_basename.with_suffix(".xlsx")
    csv_path = output_basename.with_suffix(".csv")

    write_xlsx(rows, columns, xlsx_path)
    write_csv(rows, columns, csv_path)

    print(f"Wrote {len(rows)} entries to {xlsx_path}")
    print(f"Wrote {len(rows)} entries to {csv_path}")


if __name__ == "__main__":
    main()
