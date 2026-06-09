"""
bibvik.audit — Stratified random sampling and manual audit support.

Draws a stratified random sample from the citation graph for human review.
The sample is written as a Markdown file designed for direct annotation.

See docs/methods/audit-sampling.md for full methodological documentation,
including citations for the stratified sampling approach, the manual
validation precedent, and a record of approaches considered and not adopted.

Strata
------
Each stratum targets a different class of potential pipeline error:

  crossref    — CrossRef-resolved entries: check that the match is correct,
                not merely plausible.
  unresolved  — Entries with no external validation: most likely to contain
                extraction or parsing errors.
  minimal     — Entries with completeness = 'minimal': only bare minimum
                fields present; highest risk of being wrong or duplicated.
  duplicates  — Suspected duplicate pairs: entries with high title/author
                similarity that may have escaped deduplication. All pairs
                above the similarity threshold are included (not sampled).
  ocr         — Entries from OCR-processed papers: OCR may have introduced
                character errors corrupting names, titles, or years.
  language    — Entries from non-English source papers, sampled per language.
                Requires language to be stored in processed_papers; omitted
                if language data is unavailable (see item C in todo).

Reproducibility
---------------
A fixed random seed ensures the same sample is drawn on every run against
the same graph state. The seed is documented in the output file.

Usage
-----
    python run.py --audit
    python run.py --audit --audit-n 15 --audit-seed 99 --audit-threshold 0.85
"""

import difflib
import logging
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default parameters — all overridable via CLI flags.
DEFAULT_N = 10           # Entries per stratum
DEFAULT_SEED = 42        # Random seed for reproducibility
DEFAULT_THRESHOLD = 0.85 # Title similarity threshold for duplicate detection


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
    Draw a stratified random sample from the bibliography and write it as a
    Markdown file for human review.

    Args:
        bibliography:     Full bibliography dict from graph state.
        processed_papers: Processed paper data from graph state.
        output_dir:       Directory to write audit_sample.md.
        n:                Sample size per stratum.
        seed:             Random seed for reproducibility.
        threshold:        Title similarity threshold for duplicate detection.

    Returns:
        Path to the written audit_sample.md.
    """
    rng = random.Random(seed)
    ocr_originals_dir = output_dir / "ocr" / "originals"

    logger.info("Drawing audit sample (n=%d per stratum, seed=%d)...", n, seed)

    # ── Build strata ──────────────────────────────────────────────────────────
    crossref       = _stratum_crossref(bibliography)
    unresolved     = _stratum_unresolved(bibliography)
    minimal        = _stratum_minimal(bibliography)
    duplicates     = _stratum_duplicates(bibliography, threshold, rng=rng)
    ocr            = _stratum_ocr(bibliography, ocr_originals_dir)
    by_lang        = _stratum_by_language(bibliography, processed_papers)
    catalogue      = _stratum_catalogue(bibliography)
    # New flag strata
    citekey_collisions  = _stratum_citekey_collisions(bibliography)
    oversized_titles    = _stratum_oversized_titles(bibliography)
    missing_given       = _stratum_missing_given_names(bibliography)
    near_dup_flagged    = _stratum_near_duplicate_flagged(bibliography)

    # ── Sample ────────────────────────────────────────────────────────────────
    s_crossref         = _sample(crossref,          n, rng)
    s_unresolved       = _sample(unresolved,        n, rng)
    s_minimal          = _sample(minimal,           n, rng)
    s_ocr              = _sample(ocr,               n, rng)
    s_catalogue        = _sample(catalogue,         n, rng)
    s_citekey          = _sample(citekey_collisions, n, rng)
    s_oversized        = _sample(oversized_titles,  n, rng)
    s_missing_given    = _sample(missing_given,     n, rng)
    s_near_dup         = _sample(near_dup_flagged,  n, rng)
    s_by_lang          = {lang: _sample(entries, n, rng) for lang, entries in by_lang.items()}

    # Log stratum sizes
    logger.info("  CrossRef-resolved:    %d entries (sampling %d)", len(crossref),          len(s_crossref))
    logger.info("  Unresolved:           %d entries (sampling %d)", len(unresolved),        len(s_unresolved))
    logger.info("  Minimal:              %d entries (sampling %d)", len(minimal),           len(s_minimal))
    logger.info("  Duplicate pairs:      %d pairs (all included)",  len(duplicates))
    logger.info("  OCR source:           %d entries (sampling %d)", len(ocr),               len(s_ocr))
    logger.info("  Catalogue candidates: %d entries (sampling %d)", len(catalogue),         len(s_catalogue))
    logger.info("  Citekey collisions:   %d entries (sampling %d)", len(citekey_collisions), len(s_citekey))
    logger.info("  Oversized titles:     %d entries (sampling %d)", len(oversized_titles),  len(s_oversized))
    logger.info("  Missing given names:  %d entries (sampling %d)", len(missing_given),     len(s_missing_given))
    logger.info("  Near-dup flagged:     %d entries (sampling %d)", len(near_dup_flagged),  len(s_near_dup))
    for lang, entries in by_lang.items():
        logger.info("  Language %-10s %d entries (sampling %d)", lang + ":", len(entries), len(s_by_lang[lang]))
    if not by_lang:
        logger.info("  Language strata:    none (lingua not installed or no non-English papers detected)")

    # ── Render ────────────────────────────────────────────────────────────────
    output_path = output_dir / "audit_sample.md"
    _render(
        path         = output_path,
        crossref     = s_crossref,
        unresolved   = s_unresolved,
        minimal      = s_minimal,
        duplicates   = duplicates,
        ocr          = s_ocr,
        catalogue    = s_catalogue,
        by_lang      = s_by_lang,
        bibliography = bibliography,
        pool_sizes   = {
            "crossref":          len(crossref),
            "unresolved":        len(unresolved),
            "minimal":           len(minimal),
            "ocr":               len(ocr),
            "catalogue":         len(catalogue),
            "citekey_collisions": len(citekey_collisions),
            "oversized_titles":  len(oversized_titles),
            "missing_given":     len(missing_given),
            "near_dup_flagged":  len(near_dup_flagged),
            "by_lang":           {lang: len(entries) for lang, entries in by_lang.items()},
        },
        n         = n,
        seed      = seed,
        threshold = threshold,
        citekey_collisions = s_citekey,
        oversized          = s_oversized,
        missing_given      = s_missing_given,
        near_dup           = s_near_dup,
    )

    logger.info("Audit sample written: %s", output_path)
    return output_path


# =============================================================================
# Strata builders
# =============================================================================

def _stratum_crossref(bibliography: dict[str, dict]) -> list[str]:
    """Entries resolved via CrossRef."""
    return [
        ck for ck, e in bibliography.items()
        if e.get("_resolution_method") == "crossref"
    ]


def _stratum_unresolved(bibliography: dict[str, dict]) -> list[str]:
    """Entries with no resolution method (GROBID extraction only)."""
    return [
        ck for ck, e in bibliography.items()
        if not e.get("_resolution_method")
        and e.get("generation") != "P"  # exclude seed paper itself
    ]


def _stratum_minimal(bibliography: dict[str, dict]) -> list[str]:
    """Entries with completeness label 'minimal'."""
    return [
        ck for ck, e in bibliography.items()
        if e.get("completeness", {}).get("label") == "minimal"
    ]


def _stratum_duplicates(
    bibliography: dict[str, dict],
    threshold: float,
    max_sample: int = 500,
    rng: "random.Random | None" = None,
) -> list[tuple[str, str, float]]:
    """
    All pairs of entries with title similarity above threshold.

    To avoid O(n²) comparison on large bibliographies, a random sample
    of up to max_sample entries is drawn before comparison. At max_sample=500
    this is ~125k pair comparisons rather than millions. The sample is noted
    in the rendered output.

    Returns a list of (citekey_a, citekey_b, similarity_score) tuples,
    sorted by descending similarity.
    """
    import random as _random
    all_entries = [
        (ck, _normalise_title(e.get("title", "")))
        for ck, e in bibliography.items()
        if e.get("title")
    ]

    # Sample if bibliography is large
    if len(all_entries) > max_sample:
        r = rng or _random.Random(42)
        entries = r.sample(all_entries, max_sample)
    else:
        entries = all_entries

    pairs = []
    for i in range(len(entries)):
        ck_a, title_a = entries[i]
        for j in range(i + 1, len(entries)):
            ck_b, title_b = entries[j]
            score = difflib.SequenceMatcher(None, title_a, title_b).ratio()
            if score >= threshold:
                pairs.append((ck_a, ck_b, score))

    pairs.sort(key=lambda x: -x[2])
    return pairs


def _stratum_catalogue(bibliography: dict[str, dict]) -> list[str]:
    """Entries flagged as possible artefact catalogue references."""
    return [
        ck for ck, e in bibliography.items()
        if e.get("_catalogue_candidate")
    ]


def _stratum_citekey_collisions(bibliography: dict[str, dict]) -> list[str]:
    """
    Entries where the base citekey (without a/b/c suffix) is shared with
    another entry — may indicate different works with same author+year,
    or a genuine duplicate that escaped deduplication.
    """
    import re as _re
    base_to_citekeys: dict[str, list] = defaultdict(list)
    for ck in bibliography:
        base = _re.sub(r"[a-z]$", "", ck)
        base_to_citekeys[base].append(ck)
    return [
        ck
        for citekeys in base_to_citekeys.values()
        if len(citekeys) >= 2
        for ck in citekeys
    ]


def _stratum_oversized_titles(bibliography: dict[str, dict]) -> list[str]:
    """Entries flagged as having oversized titles (likely compound citation blowout)."""
    return [ck for ck, e in bibliography.items() if e.get("_title_too_long")]


def _stratum_missing_given_names(bibliography: dict[str, dict]) -> list[str]:
    """Entries where any author has an empty given name."""
    result = []
    for ck, e in bibliography.items():
        authors = e.get("author", [])
        if any(not a.get("given", "").strip() for a in authors if a.get("family", "").strip()):
            result.append(ck)
    return result


def _stratum_near_duplicate_flagged(bibliography: dict[str, dict]) -> list[str]:
    """Entries flagged as near-duplicate candidates by postprocess."""
    return [ck for ck, e in bibliography.items() if e.get("_near_duplicate_candidate")]


def _stratum_ocr(
    bibliography: dict[str, dict],
    ocr_originals_dir: Path,
) -> list[str]:
    """
    Entries extracted from OCR-processed papers.

    A paper was OCR-processed if its filename appears in output/ocr/originals/,
    which is where _run_ocr() moves the original before replacing it in place.
    """
    if not ocr_originals_dir.is_dir():
        return []

    ocr_processed = {p.name for p in ocr_originals_dir.iterdir() if p.suffix == ".pdf"}
    if not ocr_processed:
        return []

    return [
        ck for ck, e in bibliography.items()
        if e.get("_source_pdf") in ocr_processed
    ]


def _stratum_by_language(
    bibliography: dict[str, dict],
    processed_papers: dict[str, dict],
) -> dict[str, list[str]]:
    """
    Entries from non-English source papers, grouped by language.

    Requires language to be stored under processed_papers[pdf_name]['language'].
    Returns an empty dict if lingua is not installed or no non-English papers
    were detected. Install lingua with: pip install lingua-language-detector
    """
    lang_map: dict[str, str] = {}
    for pdf_name, paper_data in processed_papers.items():
        lang = paper_data.get("language", "")
        if lang and lang.lower() not in ("en", "english", "eng"):
            lang_map[pdf_name] = lang

    if not lang_map:
        return {}

    by_lang: dict[str, list[str]] = defaultdict(list)
    for ck, e in bibliography.items():
        source_pdf = e.get("_source_pdf", "")
        if source_pdf in lang_map:
            by_lang[lang_map[source_pdf]].append(ck)

    return dict(by_lang)


# =============================================================================
# Sampling
# =============================================================================

def _sample(population: list, n: int, rng: random.Random) -> list:
    """
    Draw n items from population without replacement.

    If population has fewer than n items, all are returned. The shortfall
    is noted in the rendered output.
    """
    if len(population) <= n:
        return list(population)
    return rng.sample(population, n)


# =============================================================================
# Rendering
# =============================================================================

def _render(
    path: Path,
    crossref: list[str],
    unresolved: list[str],
    minimal: list[str],
    duplicates: list[tuple[str, str, float]],
    ocr: list[str],
    catalogue: list[str],
    by_lang: dict[str, list[str]],
    bibliography: dict[str, dict],
    pool_sizes: dict,
    n: int,
    seed: int,
    threshold: float,
    **kwargs,
) -> None:
    """Write the full audit sample Markdown file."""
    lines = []

    lines += [
        "# BibVik Citation Graph — Audit Sample",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"Sample size per stratum: {n}  ",
        f"Random seed: {seed}  ",
        f"Duplicate similarity threshold: {threshold}  ",
        "",
        "This file is designed for direct annotation. Add your notes in the "
        "**Notes** field after each entry or pair. The file becomes part of "
        "the research record once reviewed.",
        "",
        "See `docs/methods/audit-sampling.md` for full methodological documentation.",
        "",
        "---",
        "",
    ]

    # ── CrossRef-resolved ─────────────────────────────────────────────────────
    lines += _render_stratum_header(
        title     = "CrossRef-resolved entries",
        sample    = crossref,
        pool_size = pool_sizes["crossref"],
        n         = n,
        guidance  = (
            "These entries were matched to CrossRef metadata. "
            "Check that the CrossRef match is actually correct — "
            "a plausible but wrong match will produce a structurally "
            "complete but factually incorrect record."
        ),
    )
    for i, ck in enumerate(crossref, 1):
        lines += _render_entry(ck, bibliography[ck], i, len(crossref))

    # ── Unresolved ────────────────────────────────────────────────────────────
    lines += _render_stratum_header(
        title     = "Unresolved entries",
        sample    = unresolved,
        pool_size = pool_sizes["unresolved"],
        n         = n,
        guidance  = (
            "These entries could not be matched via CrossRef or LLM. "
            "They contain only what GROBID extracted from the PDF. "
            "Check whether the raw citation string was correctly parsed "
            "into structured fields."
        ),
    )
    for i, ck in enumerate(unresolved, 1):
        lines += _render_entry(ck, bibliography[ck], i, len(unresolved))

    # ── Minimal completeness ──────────────────────────────────────────────────
    lines += _render_stratum_header(
        title     = "Minimal-completeness entries",
        sample    = minimal,
        pool_size = pool_sizes["minimal"],
        n         = n,
        guidance  = (
            "These entries have only the bare minimum fields (typically "
            "author and year). They may be genuine sparse citations or "
            "extraction failures where most data was lost. Check whether "
            "the raw citation string contains more information than was "
            "captured in the structured fields."
        ),
    )
    for i, ck in enumerate(minimal, 1):
        lines += _render_entry(ck, bibliography[ck], i, len(minimal))

    # ── Suspected duplicates ──────────────────────────────────────────────────
    lines += [
        "---",
        "",
        f"## Suspected duplicate pairs ({len(duplicates)} pairs — all included)",
        "",
        (
            f"Pairs of entries with title similarity ≥ {threshold} "
            f"(computed using `difflib.SequenceMatcher` on a sample of up to 500 entries). "
            "These may represent the same work that escaped deduplication, "
            "or genuinely distinct works with similar titles. "
            "Check whether each pair should be merged."
        ),
        "",
    ]
    if not duplicates:
        lines += [f"*No pairs found above the {threshold} similarity threshold.*", ""]
    else:
        for i, (ck_a, ck_b, score) in enumerate(duplicates, 1):
            lines += _render_duplicate_pair(
                ck_a, bibliography[ck_a],
                ck_b, bibliography[ck_b],
                score, i, len(duplicates),
            )

    # ── OCR source ────────────────────────────────────────────────────────────
    lines += _render_stratum_header(
        title     = "Entries from OCR-processed papers",
        sample    = ocr,
        pool_size = pool_sizes["ocr"],
        n         = n,
        guidance  = (
            "These entries were extracted from papers that had no text "
            "layer and were processed via ocrmypdf before GROBID extraction. "
            "Check for character errors in author names, titles, or years "
            "that may have been introduced by OCR."
        ),
    )
    if not ocr and pool_sizes["ocr"] == 0:
        lines += ["*No OCR-processed papers in the current graph state.*", ""]
    else:
        for i, ck in enumerate(ocr, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(ocr))

    # ── Catalogue candidates ──────────────────────────────────────────────────
    lines += _render_stratum_header(
        title     = "Possible artefact catalogue entries",
        sample    = catalogue,
        pool_size = pool_sizes["catalogue"],
        n         = n,
        guidance  = (
            "These entries were flagged as possible artefact catalogue references "
            "rather than scholarly citations. Patterns detected in the raw citation "
            "string: `Kat.-Nr.` (German catalogue number), `Taf.` + number (plate "
            "reference), or museum accession number format (e.g. SHM 3217, C5821). "
            "Check whether each entry is a genuine bibliographic reference or a "
            "catalogue/findspot record that should be excluded from citation analysis."
        ),
    )
    if not catalogue and pool_sizes["catalogue"] == 0:
        lines += ["*No catalogue candidate entries in the current graph state.*", ""]
    else:
        for i, ck in enumerate(catalogue, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(catalogue))

    # ── Non-English source papers ─────────────────────────────────────────────
    if not by_lang:
        lines += [
            "---",
            "",
            "## Non-English source papers",
            "",
            (
                "*No non-English papers detected in the current graph state. "
                "Install lingua-language-detector for language detection: "
                "`pip install lingua-language-detector`*"
            ),
            "",
        ]
    else:
        for lang, sample in sorted(by_lang.items()):
            pool_size = pool_sizes["by_lang"].get(lang, len(sample))
            lines += _render_stratum_header(
                title     = f"Non-English source papers: {lang}",
                sample    = sample,
                pool_size = pool_size,
                n         = n,
                guidance  = (
                    f"These entries were extracted from source papers in {lang}. "
                    "Check for encoding errors in non-Latin characters, incorrect "
                    "transliteration in citekeys, and broken author name parsing."
                ),
            )
            for i, ck in enumerate(sample, 1):
                lines += _render_entry(ck, bibliography[ck], i, len(sample))

    # ── Citekey suffix collisions ─────────────────────────────────────────────
    citekey_collisions = kwargs.get("citekey_collisions", [])
    citekey_pool = pool_sizes.get("citekey_collisions", 0)
    if citekey_pool > 0:
        lines += _render_stratum_header(
            title     = "Citekey suffix collisions",
            sample    = citekey_collisions,
            pool_size = citekey_pool,
            n         = n,
            guidance  = (
                "These entries share a base citekey with at least one other entry "
                "(e.g. price2002 and price2002a). Check whether they are genuinely "
                "different works or duplicates that escaped deduplication."
            ),
        )
        for i, ck in enumerate(citekey_collisions, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(citekey_collisions))

    # ── Oversized titles ──────────────────────────────────────────────────────
    oversized = kwargs.get("oversized", [])
    oversized_pool = pool_sizes.get("oversized_titles", 0)
    if oversized_pool > 0:
        lines += _render_stratum_header(
            title     = "Oversized titles",
            sample    = oversized,
            pool_size = oversized_pool,
            n         = n,
            guidance  = (
                "These entries have titles over 300 characters, likely from compound "
                "citation blowout (GROBID treating a full reference string as a title). "
                "Review the title and correct manually if possible."
            ),
        )
        for i, ck in enumerate(oversized, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(oversized))

    # ── Missing given names ───────────────────────────────────────────────────
    missing_given = kwargs.get("missing_given", [])
    missing_pool = pool_sizes.get("missing_given", 0)
    if missing_pool > 0:
        lines += _render_stratum_header(
            title     = "Missing given names",
            sample    = missing_given,
            pool_size = missing_pool,
            n         = n,
            guidance  = (
                "These entries have one or more authors with no given name. "
                "CrossRef enrichment may fill these in. If not, check the source PDF."
            ),
        )
        for i, ck in enumerate(missing_given, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(missing_given))

    # ── Near-duplicate flagged ────────────────────────────────────────────────
    near_dup = kwargs.get("near_dup", [])
    near_dup_pool = pool_sizes.get("near_dup_flagged", 0)
    if near_dup_pool > 0:
        lines += _render_stratum_header(
            title     = "Near-duplicate candidates",
            sample    = near_dup,
            pool_size = near_dup_pool,
            n         = n,
            guidance  = (
                "These entries were flagged as near-duplicates by postprocess "
                "(same author+year, ≥70% title token overlap) but could not be "
                "resolved automatically. Check _near_duplicate_candidate field for "
                "the paired citekey."
            ),
        )
        for i, ck in enumerate(near_dup, 1):
            lines += _render_entry(ck, bibliography[ck], i, len(near_dup))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _render_stratum_header(
    title: str,
    sample: list,
    pool_size: int,
    n: int,
    guidance: str,
) -> list[str]:
    """Render the header block for a stratum."""
    shortfall = pool_size < n
    count_note = (
        f"{len(sample)} of {pool_size} — "
        + ("all included (fewer than " + str(n) + " available)" if shortfall
           else f"sampled from {pool_size}")
    )
    return [
        "---",
        "",
        f"## {title} ({count_note})",
        "",
        guidance,
        "",
    ]


def _render_entry(
    citekey: str,
    entry: dict,
    index: int,
    total: int,
) -> list[str]:
    """Render a single bibliography entry for review."""
    lines = [f"### {citekey} [{index} of {total}]", ""]

    source = entry.get("_source_pdf", "")
    if source:
        lines += [f"**Extracted from:** {source}  "]

    method = entry.get("_resolution_method")
    confidence = entry.get("_resolution_confidence")
    if method:
        try:
            conf_str = f" (confidence: {float(confidence):.2f})" if confidence else ""
        except (TypeError, ValueError):
            conf_str = f" (confidence: {confidence})" if confidence else ""
        lines += [f"**Resolution:** {method}{conf_str}  "]

    raw = entry.get("_raw_citation", "")
    if raw:
        lines += ["", "**Raw citation string:**", f"> {raw.strip()}", ""]

    lines += ["**Structured fields:**", ""]
    lines += [f"- **Authors:** {_format_authors(entry.get('author', []))}"]
    lines += [f"- **Title:** {entry.get('title', '*(missing)*')}"]
    lines += [f"- **Year:** {entry.get('date', entry.get('year', '*(missing)*'))}"]
    lines += [f"- **Type:** {entry.get('entry_type', '*(missing)*')}"]

    if entry.get("journaltitle"):
        lines += [f"- **Journal:** {entry['journaltitle']}"]
    if entry.get("booktitle"):
        lines += [f"- **In:** {entry['booktitle']}"]
    if entry.get("publisher"):
        lines += [f"- **Publisher:** {entry['publisher']}"]
    if entry.get("location"):
        lines += [f"- **Location:** {entry['location']}"]
    if entry.get("pages"):
        lines += [f"- **Pages:** {entry['pages']}"]
    if entry.get("doi"):
        lines += [f"- **DOI:** {entry['doi']}"]

    completeness = entry.get("completeness", {})
    lines += [f"- **Completeness:** {completeness.get('label', '?')} (score: {completeness.get('score', '?')})"]

    cited_by = entry.get("cited_by", [])
    lines += [f"- **Cited by:** {', '.join(cited_by) if cited_by else '*(none)*'}"]

    lines += ["", "**Notes:**", "", ""]
    return lines


def _render_duplicate_pair(
    ck_a: str, entry_a: dict,
    ck_b: str, entry_b: dict,
    score: float,
    index: int,
    total: int,
) -> list[str]:
    """Render a suspected duplicate pair for side-by-side comparison."""
    lines = [
        f"### Pair {index} of {total} — similarity {score:.3f}",
        "",
    ]

    for label, ck, entry in [("Entry A", ck_a, entry_a), ("Entry B", ck_b, entry_b)]:
        raw = entry.get("_raw_citation", "")
        lines += [
            f"**{label} — {ck}**  ",
            f"Source: {entry.get('_source_pdf', '*(unknown)*')}  ",
        ]
        if raw:
            lines += [f"Raw citation: {raw.strip()[:200]}  "]
        lines += [
            f"Authors: {_format_authors(entry.get('author', []))}  ",
            f"Title: {entry.get('title', '*(missing)*')}  ",
            f"Year: {entry.get('date', entry.get('year', '*(missing)*'))}  ",
            f"Type: {entry.get('entry_type', '*(missing)*')}  ",
            "",
        ]

    lines += ["**Should these be merged?**", "", "**Notes:**", "", ""]
    return lines


# =============================================================================
# Helpers
# =============================================================================

def _format_authors(authors: list[dict]) -> str:
    """Format an author list as a readable string."""
    if not authors:
        return "*(missing)*"
    parts = []
    for a in authors:
        family = a.get("family", "")
        given = a.get("given", "")
        parts.append(f"{family}, {given}".strip(", "))
    return "; ".join(parts)


def _normalise_title(title: str) -> str:
    """Normalise a title string for similarity comparison."""
    return title.lower().strip()
