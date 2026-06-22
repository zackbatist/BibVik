"""
bibvik.audit — Citation graph diagnostic report.

Produces a self-contained HTML report (audit_report.html) assessing
the quality of the bibliography across four dimensions:

    Completeness         — missing-field rates by detection method
    Accuracy             — sample review: non-English papers, CrossRef matches
    Representational consistency — suspected duplicate pairs
    Coverage             — papers with low citation counts
    Provenance           — detection method distribution

The report is diagnostic, not corrective. It enables a researcher to
judge whether the bibliography is trustworthy enough to analyse.
Corrective actions are recorded in corrections.yaml.

See docs/methods/audit-sampling.md for methodological documentation.

Usage:
    python run.py --audit
    python run.py --audit --audit-n 15 --audit-seed 99
"""

import difflib
import logging
import random
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_N         = 10
DEFAULT_SEED      = 42
DEFAULT_THRESHOLD = 0.70  # Lower than before — token overlap, not sequence match


# =============================================================================
# Public entry point
# =============================================================================

def run_audit(
    bibliography: dict[str, dict],
    processed_papers: dict[str, dict],
    output_dir: Path,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    threshold: float = DEFAULT_THRESHOLD,
) -> Path:
    """
    Produce a diagnostic audit report from the bibliography.

    Args:
        bibliography:     Full bibliography dict from graph state.
        processed_papers: Processed paper data from graph state.
        output_dir:       Directory to write audit_report.html.
        n:                Sample size for each section.
        seed:             Random seed for reproducibility.
        threshold:        Title token overlap threshold for duplicate detection.

    Returns:
        Path to the written audit_report.html.
    """
    rng = random.Random(seed)

    logger.info("Building audit report (n=%d, seed=%d, threshold=%.2f)...", n, seed, threshold)

    # Active entries only
    active = {ck: e for ck, e in bibliography.items() if not e.get("_deleted")}

    # ── Completeness ─────────────────────────────────────────────────────────
    completeness_data = _compute_completeness(active)
    no_title_sample   = _sample(
        [ck for ck, e in active.items() if not e.get("title") and e.get("_raw_citation")],
        n, rng
    )

    # ── Accuracy ─────────────────────────────────────────────────────────────
    non_english = _get_non_english(active, processed_papers)
    accuracy_lang_sample    = _sample(non_english, n, rng)
    crossref_sample         = _sample(
        [ck for ck, e in active.items() if e.get("_resolution_method") == "crossref"],
        n, rng
    )

    # ── Representational consistency ──────────────────────────────────────────
    dup_pairs = _find_duplicate_pairs(active, threshold, rng)
    dup_sample = dup_pairs[:n]

    # ── Coverage ──────────────────────────────────────────────────────────────
    coverage_data = _compute_coverage(active, processed_papers)

    # ── Provenance ────────────────────────────────────────────────────────────
    provenance_data = _compute_provenance(active)

    # ── Render ────────────────────────────────────────────────────────────────
    output_path = Path(output_dir) / "audit_report.html"
    _render_html(
        path               = output_path,
        bibliography       = active,
        completeness_data  = completeness_data,
        no_title_sample    = no_title_sample,
        accuracy_lang_sample = accuracy_lang_sample,
        crossref_sample    = crossref_sample,
        dup_sample         = dup_sample,
        dup_total          = len(dup_pairs),
        coverage_data      = coverage_data,
        provenance_data    = provenance_data,
        n                  = n,
        seed               = seed,
        threshold          = threshold,
        generated          = datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    logger.info("Audit report written: %s", output_path)
    return output_path


# =============================================================================
# Data collection
# =============================================================================

def _compute_completeness(bib: dict) -> dict:
    """
    For each detection method, count entries missing each required field.
    Returns a dict suitable for table rendering.
    """
    methods = ["grobid", "llm_bib_reparse", "llm_from_footnote", "crossref", "other"]
    method_labels = {
        "grobid":           "GROBID",
        "llm_bib_reparse":  "Method 6",
        "llm_from_footnote":"Footnote extraction",
        "crossref":         "CrossRef",
        "other":            "Other / unknown",
    }

    def _method(entry):
        m = entry.get("_resolution_method") or ""
        if not m or m in ("grobid", ""):
            return "grobid"
        if m in method_labels:
            return m
        return "other"

    rows = {m: {"total": 0, "no_author": 0, "no_title": 0, "no_year": 0} for m in methods}

    for entry in bib.values():
        m = _method(entry)
        rows[m]["total"] += 1
        if not entry.get("author"):
            rows[m]["no_author"] += 1
        if not entry.get("title"):
            rows[m]["no_title"] += 1
        if not (entry.get("date") or entry.get("year")):
            rows[m]["no_year"] += 1

    return {"rows": rows, "labels": method_labels, "order": methods}


def _get_non_english(
    bib: dict,
    processed_papers: dict,
) -> list[str]:
    """Citekeys of entries from non-English source papers."""
    non_eng_pdfs = {
        name for name, data in processed_papers.items()
        if data.get("language", "").lower() not in ("en", "english", "eng", "")
    }
    return [ck for ck, e in bib.items() if e.get("_source_pdf") in non_eng_pdfs]


def _find_duplicate_pairs(
    bib: dict,
    threshold: float,
    rng: random.Random,
    max_pool: int = 600,
) -> list[tuple[str, str, float]]:
    """
    Find pairs with same first-author surname + year and high title token overlap.
    Returns list of (ck_a, ck_b, score) sorted by descending score.
    """
    from collections import defaultdict

    # Index by (norm_author, year)
    index: dict[tuple, list] = defaultdict(list)
    for ck, e in bib.items():
        authors = e.get("author", [])
        if not authors:
            continue
        family = re.sub(r"[^a-z]", "", authors[0].get("family", "").lower())
        year = str(e.get("date", e.get("year", "")))[:4]
        if family and year:
            index[(family, year)].append(ck)

    candidates = [(ck_a, ck_b) for cks in index.values() if len(cks) >= 2
                  for i, ck_a in enumerate(cks) for ck_b in cks[i+1:]]

    if len(candidates) > max_pool:
        candidates = rng.sample(candidates, max_pool)

    pairs = []
    for ck_a, ck_b in candidates:
        ta = _token_set(bib[ck_a].get("title", ""))
        tb = _token_set(bib[ck_b].get("title", ""))
        if not ta or not tb:
            continue
        overlap = len(ta & tb) / min(len(ta), len(tb))
        if overlap >= threshold:
            pairs.append((ck_a, ck_b, overlap))

    pairs.sort(key=lambda x: -x[2])
    return pairs


def _token_set(title: str) -> set:
    return set(w for w in re.findall(r"\b\w{4,}\b", title.lower()))


def _compute_coverage(bib: dict, processed_papers: dict) -> list[dict]:
    """
    Return papers sorted by citation count (ascending), for bottom-N review.
    Each dict has: name, citations_extracted, methods_used, failed.
    """
    paper_counts: dict[str, dict] = {}

    for ck, e in bib.items():
        pdf = e.get("_source_pdf", "")
        if not pdf:
            continue
        if pdf not in paper_counts:
            paper_counts[pdf] = {"name": pdf, "count": 0, "methods": set()}
        paper_counts[pdf]["count"] += 1
        m = e.get("_resolution_method") or "grobid"
        paper_counts[pdf]["methods"].add(m)

    # Add papers that processed but produced zero entries
    for pdf_name, data in processed_papers.items():
        if pdf_name not in paper_counts:
            paper_counts[pdf_name] = {"name": pdf_name, "count": 0, "methods": set()}

    result = [
        {
            "name": v["name"].replace(".pdf", ""),
            "count": v["count"],
            "methods": ", ".join(sorted(v["methods"])) or "—",
        }
        for v in paper_counts.values()
    ]
    result.sort(key=lambda x: x["count"])
    return result


def _compute_provenance(bib: dict) -> dict:
    """
    Detection method × generation breakdown.
    Returns {method: {generation: count}}.
    """
    method_labels = {
        None:               "GROBID (unresolved)",
        "":                 "GROBID (unresolved)",
        "crossref":         "CrossRef",
        "llm_bib_reparse":  "Method 6",
        "llm_from_footnote":"Footnote extraction",
        "llm_from_context": "Citation context",
    }
    generations = ["P", "F1", "F2"]
    methods_order = [
        None, "crossref", "llm_bib_reparse", "llm_from_footnote", "llm_from_context"
    ]

    data: dict = {m: {g: 0 for g in generations} for m in methods_order}
    totals = {g: 0 for g in generations}

    for e in bib.values():
        m = e.get("_resolution_method") or None
        if m not in data:
            m = None  # group unknowns with GROBID unresolved
        g = e.get("generation", "F2")
        if g not in generations:
            g = "F2"
        data[m][g] += 1
        totals[g] += 1

    return {"data": data, "labels": method_labels, "order": methods_order,
            "generations": generations, "totals": totals}


# =============================================================================
# HTML rendering
# =============================================================================

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: "Georgia", "Times New Roman", serif;
  font-size: 15px;
  line-height: 1.6;
  color: #1a1a1a;
  background: #f8f7f4;
  max-width: 900px;
  margin: 0 auto;
  padding: 2rem 1.5rem 4rem;
}

h1 {
  font-size: 1.6rem;
  font-weight: normal;
  letter-spacing: -0.02em;
  border-bottom: 2px solid #1a1a1a;
  padding-bottom: 0.5rem;
  margin-bottom: 0.4rem;
}

.meta {
  font-size: 0.78rem;
  color: #666;
  margin-bottom: 2.5rem;
  font-family: monospace;
}

h2 {
  font-size: 1.1rem;
  font-weight: normal;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #444;
  margin: 2.5rem 0 1rem;
  padding-top: 1.5rem;
  border-top: 1px solid #ccc;
}

h3 {
  font-size: 0.9rem;
  font-weight: bold;
  margin: 1.2rem 0 0.4rem;
  color: #333;
}

p { margin-bottom: 0.8rem; }

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  margin: 1rem 0 1.5rem;
}

th {
  text-align: left;
  border-bottom: 2px solid #1a1a1a;
  padding: 0.4rem 0.6rem;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #444;
}

td {
  padding: 0.35rem 0.6rem;
  border-bottom: 1px solid #ddd;
  vertical-align: top;
}

tr:hover td { background: #f0ede8; }

td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.pct { text-align: right; color: #888; font-size: 0.8rem; }

.missing-high { color: #c0392b; font-weight: bold; }
.missing-mid  { color: #e67e22; }
.missing-low  { color: #888; }

details {
  margin: 1rem 0;
  border: 1px solid #ddd;
  border-radius: 2px;
}

summary {
  padding: 0.6rem 0.8rem;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: bold;
  color: #444;
  background: #f0ede8;
  user-select: none;
}

summary:hover { background: #e8e4de; }

.sample-inner { padding: 0.8rem 1rem; }

.entry {
  margin-bottom: 1.2rem;
  padding-bottom: 1.2rem;
  border-bottom: 1px solid #e8e4de;
}

.entry:last-child { border-bottom: none; margin-bottom: 0; }

.entry-header {
  font-family: monospace;
  font-size: 0.8rem;
  color: #888;
  margin-bottom: 0.3rem;
}

.entry-title {
  font-size: 0.95rem;
  font-style: italic;
  margin-bottom: 0.2rem;
}

.entry-authors {
  font-size: 0.85rem;
  color: #333;
}

.entry-year {
  font-size: 0.85rem;
  color: #666;
}

.entry-raw {
  font-size: 0.78rem;
  color: #666;
  font-family: monospace;
  background: #f0ede8;
  padding: 0.4rem 0.6rem;
  margin-top: 0.4rem;
  border-left: 3px solid #ccc;
  white-space: pre-wrap;
  word-break: break-word;
}

.entry-method {
  display: inline-block;
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
  border-radius: 2px;
  background: #ddd;
  color: #444;
  font-family: monospace;
  margin-top: 0.3rem;
}

.pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 1.2rem;
  padding-bottom: 1.2rem;
  border-bottom: 1px solid #e8e4de;
}

.pair:last-child { border-bottom: none; }

.pair-label {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888;
  margin-bottom: 0.3rem;
}

.pair-score {
  font-size: 0.8rem;
  color: #888;
  font-family: monospace;
  margin-bottom: 0.6rem;
}

.stat-block {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 0.8rem;
  margin: 1rem 0 1.5rem;
}

.stat {
  background: #f0ede8;
  padding: 0.8rem;
  border-radius: 2px;
}

.stat-value {
  font-size: 1.6rem;
  font-weight: bold;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: 0.72rem;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-top: 0.2rem;
}

@media print {
  body { background: white; padding: 0; }
  details { border: none; }
  details[open] summary { display: none; }
  .stat-block { break-inside: avoid; }
}
"""

def _h(s: str) -> str:
    """Escape HTML."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_authors(authors: list) -> str:
    if not authors:
        return "—"
    parts = []
    for a in authors[:3]:
        family = a.get("family", "")
        given = a.get("given", "")
        parts.append(f"{_h(family)}, {_h(given)}".strip(", "))
    s = "; ".join(parts)
    if len(authors) > 3:
        s += f" + {len(authors) - 3} more"
    return s


def _method_label(entry: dict) -> str:
    m = entry.get("_resolution_method") or ""
    labels = {
        "crossref":          "CrossRef",
        "llm_bib_reparse":   "Method 6",
        "llm_from_footnote": "Footnote",
        "llm_from_context":  "Context",
    }
    return labels.get(m, "GROBID")


def _render_entry_card(ck: str, entry: dict) -> str:
    title   = entry.get("title") or "<em>no title</em>"
    authors = _fmt_authors(entry.get("author", []))
    year    = entry.get("date") or entry.get("year") or "—"
    raw     = entry.get("_raw_citation", "")
    source  = entry.get("_source_pdf", "")
    method  = _method_label(entry)

    raw_html = f'<div class="entry-raw">{_h(raw[:300])}{"…" if len(raw) > 300 else ""}</div>' if raw else ""
    source_html = f'<div class="entry-year">From: {_h(source)}</div>' if source else ""

    return f"""<div class="entry">
  <div class="entry-header">{_h(ck)}</div>
  <div class="entry-title">{_h(title) if isinstance(title, str) else title}</div>
  <div class="entry-authors">{authors}</div>
  <div class="entry-year">{_h(str(year))}</div>
  {source_html}
  <span class="entry-method">{_h(method)}</span>
  {raw_html}
</div>"""


def _pct_class(rate: float) -> str:
    if rate >= 0.15:
        return "missing-high"
    if rate >= 0.05:
        return "missing-mid"
    return "missing-low"


def _render_html(
    path: Path,
    bibliography: dict,
    completeness_data: dict,
    no_title_sample: list,
    accuracy_lang_sample: list,
    crossref_sample: list,
    dup_sample: list,
    dup_total: int,
    coverage_data: list,
    provenance_data: dict,
    n: int,
    seed: int,
    threshold: float,
    generated: str,
) -> None:

    total = len(bibliography)
    gen_counts = {"P": 0, "F1": 0, "F2": 0}
    for e in bibliography.values():
        g = e.get("generation", "F2")
        if g in gen_counts:
            gen_counts[g] += 1

    # ── Overview stats ─────────────────────────────────────────────────────────
    overview_stats = f"""
<div class="stat-block">
  <div class="stat"><div class="stat-value">{total:,}</div><div class="stat-label">Total entries</div></div>
  <div class="stat"><div class="stat-value">{gen_counts['F1']:,}</div><div class="stat-label">F1 papers</div></div>
  <div class="stat"><div class="stat-value">{gen_counts['F2']:,}</div><div class="stat-label">F2 citations</div></div>
</div>"""

    # ── Completeness table ─────────────────────────────────────────────────────
    cd = completeness_data
    comp_rows = ""
    for m in cd["order"]:
        row = cd["rows"][m]
        if row["total"] == 0:
            continue
        t = row["total"]
        def _cell(k):
            v = row[k]
            r = v / t if t else 0
            cls = _pct_class(r)
            return f'<td class="num {cls}">{v}</td><td class="pct">({r:.0%})</td>'
        comp_rows += f"""<tr>
  <td>{_h(cd["labels"][m])}</td>
  <td class="num">{t:,}</td>
  {_cell("no_author")}{_cell("no_title")}{_cell("no_year")}
</tr>"""

    completeness_table = f"""<table>
<thead><tr>
  <th>Method</th><th class="num">Entries</th>
  <th class="num" colspan="2">No author</th>
  <th class="num" colspan="2">No title</th>
  <th class="num" colspan="2">No year</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>"""

    # Sample of no-title entries
    no_title_cards = "".join(_render_entry_card(ck, bibliography[ck]) for ck in no_title_sample)
    no_title_details = f"""<details>
<summary>Sample of entries with no title ({len(no_title_sample)} shown)</summary>
<div class="sample-inner">{no_title_cards}</div>
</details>""" if no_title_sample else ""

    # ── Accuracy: non-English sample ───────────────────────────────────────────
    lang_cards = "".join(_render_entry_card(ck, bibliography[ck]) for ck in accuracy_lang_sample)
    lang_details = f"""<details>
<summary>Sample from non-English source papers ({len(accuracy_lang_sample)} shown)</summary>
<div class="sample-inner">{lang_cards or "<p>No non-English source papers detected.</p>"}</div>
</details>"""

    # CrossRef sample
    cr_cards = "".join(_render_entry_card(ck, bibliography[ck]) for ck in crossref_sample)
    cr_details = f"""<details>
<summary>Sample of CrossRef-resolved entries ({len(crossref_sample)} shown)</summary>
<div class="sample-inner">{cr_cards or "<p>No CrossRef-resolved entries.</p>"}</div>
</details>"""

    # ── Representational consistency: duplicate pairs ──────────────────────────
    pair_html = ""
    for ck_a, ck_b, score in dup_sample:
        ea = bibliography.get(ck_a, {})
        eb = bibliography.get(ck_b, {})
        def _side(ck, e):
            return f"""<div>
  <div class="pair-label">{_h(ck)}</div>
  <div class="entry-title">{_h(e.get("title") or "—")}</div>
  <div class="entry-authors">{_fmt_authors(e.get("author", []))}</div>
  <div class="entry-year">{_h(str(e.get("date") or e.get("year") or "—"))}</div>
  <div class="entry-raw">{_h((e.get("_raw_citation") or "")[:200])}</div>
</div>"""
        pair_html += f"""<div class="pair">
  <div><div class="pair-score">overlap {score:.0%}</div>{_side(ck_a, ea)}</div>
  <div><div style="height:1.4rem"></div>{_side(ck_b, eb)}</div>
</div>"""

    dup_details = f"""<details>
<summary>Suspected duplicate pairs ({len(dup_sample)} of {dup_total} shown)</summary>
<div class="sample-inner">{pair_html or "<p>No suspected duplicate pairs found.</p>"}</div>
</details>"""

    # ── Coverage table ─────────────────────────────────────────────────────────
    cov_rows = ""
    for row in coverage_data[:20]:
        cov_rows += f"""<tr>
  <td>{_h(row["name"])}</td>
  <td class="num">{row["count"]}</td>
  <td>{_h(row["methods"])}</td>
</tr>"""
    coverage_table = f"""<table>
<thead><tr><th>Paper</th><th class="num">Citations extracted</th><th>Methods used</th></tr></thead>
<tbody>{cov_rows}</tbody>
</table>"""

    # ── Provenance table ───────────────────────────────────────────────────────
    pd_ = provenance_data
    prov_header = "".join(f'<th class="num">{g}</th>' for g in pd_["generations"])
    prov_rows = ""
    for m in pd_["order"]:
        label = pd_["labels"].get(m, str(m))
        cells = "".join(
            f'<td class="num">{pd_["data"][m][g]:,}</td>'
            for g in pd_["generations"]
        )
        row_total = sum(pd_["data"][m][g] for g in pd_["generations"])
        prov_rows += f'<tr><td>{_h(label)}</td>{cells}<td class="num">{row_total:,}</td></tr>'
    gen_totals = "".join(f'<td class="num"><strong>{pd_["totals"][g]:,}</strong></td>' for g in pd_["generations"])
    all_total = sum(pd_["totals"].values())
    prov_rows += f'<tr><td><strong>Total</strong></td>{gen_totals}<td class="num"><strong>{all_total:,}</strong></td></tr>'

    provenance_table = f"""<table>
<thead><tr><th>Detection method</th>{prov_header}<th class="num">Total</th></tr></thead>
<tbody>{prov_rows}</tbody>
</table>"""

    # ── Assemble ───────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BibVik — Audit Report</title>
<style>{_CSS}</style>
</head>
<body>

<h1>BibVik — Audit Report</h1>
<div class="meta">Generated {_h(generated)} &nbsp;·&nbsp; seed {seed} &nbsp;·&nbsp; sample n={n} &nbsp;·&nbsp; duplicate threshold {threshold:.0%}</div>

{overview_stats}

<p>This report assesses the quality of the bibliography across five dimensions.
It is diagnostic — it supports a judgment about whether the bibliography is
trustworthy enough to analyse. To record a correction, add an entry to
<code>corrections.yaml</code>.</p>

<h2>1. Completeness</h2>
<p>Entries missing required fields, broken down by detection method.
High missing-title rates in GROBID entries are expected — GROBID commonly
fails to extract titles from complex layouts. Method 6 should recover
many of these from the raw reference text.</p>

{completeness_table}
{no_title_details}

<h2>2. Accuracy</h2>
<p>Are extracted values correct? Check author names and titles for encoding
errors, diacritic loss, or transposed given/family names — particularly in
entries from non-English source papers.</p>

{lang_details}

<p>For CrossRef-resolved entries, check that the CrossRef match is actually
correct — a plausible but wrong match produces a structurally complete but
factually incorrect record.</p>

{cr_details}

<h2>3. Representational consistency</h2>
<p>Suspected duplicate pairs: same first-author surname and year, with
{threshold:.0%} or more title word overlap, that were not automatically merged.
{dup_total} pairs found in total.</p>

{dup_details}

<h2>4. Coverage</h2>
<p>Papers with the fewest citations extracted. Low counts may indicate a
bibliography the pipeline failed to process — check whether the paper has
a substantive reference list that should have been extracted.</p>

{coverage_table}

<h2>5. Provenance</h2>
<p>How entries were detected, broken down by generation. An unexpected
distribution — for example, very few Method 6 recoveries — signals a
systematic failure in that detection path.</p>

{provenance_table}

</body>
</html>"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")