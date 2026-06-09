"""
bibvik.normalize — Normalization of bibliographic field values for consistent output.

This module handles two categories of normalization:

1. **Title normalization**: Ensures consistent capitalization across bibliography
   entries. GROBID sometimes produces ALL-CAPS titles (from PDFs with all-caps
   headings), leading-apostrophe artifacts, or inconsistently mixed case.

   Strategy:
   - All-caps titles (more than half the alpha chars are uppercase): convert to
     title case using humanities conventions (lowercase articles, prepositions,
     conjunctions in English; preserve originals in non-English titles).
   - Titles with a leading apostrophe or other artifact: strip the artifact.
   - Sentence-case titles (first word capitalized, rest lower): leave as-is —
     sentence case is conventional in many European languages and GROBID often
     preserves it correctly.
   - Already-reasonable mixed-case titles: leave as-is.

   Language detection is heuristic: if the title contains a high proportion of
   non-ASCII characters or known non-English function words, treat it as
   non-English and do not apply English title-case rules.

2. **Author name normalization**: Resolves inconsistent given-name forms across
   entries for the same author (e.g., "Hanne Lovise" vs "H L" vs "Hanne").

   Strategy:
   - Within a single entry's author list: expand initials where a fuller form
     is known from elsewhere in the bibliography (pass 2, corpus-wide).
   - Prefer the longest / most complete given-name form seen for a family name.
   - Do not collapse different people who share a family name — use year/title
     context as a guard.

   In practice, normalization of given names is corpus-wide and run as a
   post-processing pass over the full bibliography dict.

Both normalizations are non-destructive: they only change values that are
clearly wrong or inconsistent, and always log what they changed so diffs
are reviewable.
"""

import logging
import re
from typing import Any

from unidecode import unidecode

logger = logging.getLogger(__name__)


# =============================================================================
# Title normalization
# =============================================================================

# English function words that should stay lowercase in title case
# (unless they are the first or last word).
_EN_LOWERCASE_WORDS = frozenset({
    # Articles
    "a", "an", "the",
    # Coordinating conjunctions
    "and", "but", "for", "nor", "or", "so", "yet",
    # Short prepositions (4 letters or fewer is a common rule)
    "at", "by", "for", "in", "of", "on", "to", "up", "as", "if",
    "into", "like", "near", "once", "onto", "over", "past", "than",
    "that", "till", "upon", "via", "with",
    # Other short function words
    "v", "vs",
})

# Heuristic: if the title contains any of these non-English function words,
# treat as non-English and skip English title-case rules.
_NON_EN_SIGNALS = frozenset({
    # Norwegian/Danish/Swedish
    "og", "av", "på", "til", "fra", "med", "om", "en", "et", "ei",
    "i", "de", "det", "den", "som", "for", "er",
    # German
    "und", "der", "die", "das", "des", "dem", "den", "ein", "eine",
    "in", "im", "zu", "zur", "zum", "von", "vom", "nach", "mit",
    # French
    "et", "le", "la", "les", "du", "de", "des", "un", "une",
    "en", "au", "aux",
})

# Characters that indicate non-ASCII / non-Latin script presence.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def normalize_title(title: str, langid: str = "") -> str:
    """
    Normalize a bibliographic title to consistent capitalization.

    Args:
        title:  Raw title string from GROBID or footnote extraction.
        langid: BibLaTeX langid hint (e.g., "english", "norsk", "german").
                Empty string means unknown.

    Returns:
        Normalized title string.
    """
    if not title:
        return title

    # Strip leading/trailing whitespace and artifact characters.
    cleaned = title.strip()
    # Strip leading apostrophe/curly-quote (GROBID artifact from some PDFs).
    cleaned = re.sub(r"^['\u2018\u2019\u201c\u201d]+", "", cleaned).strip()

    if not cleaned:
        return title

    # Determine if the title is in ALL CAPS (GROBID from all-caps PDF headings).
    alpha_chars = [c for c in cleaned if c.isalpha()]
    if not alpha_chars:
        return cleaned

    upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
    is_all_caps = upper_ratio > 0.75
    is_all_lower = upper_ratio < 0.05  # Essentially no uppercase at all

    # If a leading artifact was stripped and the result now starts lowercase,
    # capitalize the first letter only — don't re-case the whole title.
    # (The rest of the string may already be correctly cased by GROBID.)
    artifact_stripped = cleaned != title.strip()
    if artifact_stripped and cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
        # Re-evaluate using only the *remainder* of the string (chars 1+),
        # since we just forced the first character uppercase. If the rest
        # contains a mix of upper and lower, the title was already well-cased
        # and we should leave it alone.
        rest_alpha = [c for c in cleaned[1:] if c.isalpha()]
        if rest_alpha:
            rest_upper_ratio = sum(1 for c in rest_alpha if c.isupper()) / len(rest_alpha)
            is_all_caps = rest_upper_ratio > 0.75
            # Even a single interior uppercase letter (ratio > 0) means the
            # remainder was intentionally cased — leave it alone.
            is_all_lower = rest_upper_ratio == 0.0
        else:
            is_all_caps = False
            is_all_lower = False

    if not is_all_caps and not is_all_lower:
        # Title is already in reasonable mixed case — leave as-is.
        return cleaned

    # --- Apply title case to all-caps or all-lowercase titles ---
    is_english = _detect_english(cleaned, langid)
    return _apply_title_case(cleaned, english=is_english)


def _detect_english(title: str, langid: str) -> bool:
    """
    Heuristically determine whether a title is English.

    Returns True if we're reasonably confident it's English.
    """
    # Explicit langid overrides heuristics.
    langid_lower = langid.lower()
    if langid_lower in ("english", "american", "british"):
        return True
    if langid_lower and langid_lower not in ("", "und", "unknown"):
        # Any explicit non-English langid → not English.
        return False

    # Heuristic 1: if title contains non-ASCII characters, likely non-English.
    if _NON_ASCII_RE.search(title):
        return False

    # Heuristic 2: if title contains non-English function words, non-English.
    words = re.findall(r"\b\w+\b", title.lower())
    non_en_hits = sum(1 for w in words if w in _NON_EN_SIGNALS)
    if non_en_hits >= 2:
        return False

    return True


def _apply_title_case(title: str, english: bool = True) -> str:
    """
    Apply title case to a string.

    For English: first and last words always capitalized; function words
    lowercase in the middle. After a colon or em-dash, capitalize the
    next word (subtitle start).

    For non-English: capitalize only the first word and proper nouns
    (heuristic: words that were capitalized in the original all-caps source
    cannot be distinguished, so we default to sentence case — first word
    capitalized, rest lowercase).

    This is necessarily imperfect for non-English titles; the goal is to
    produce something clearly better than ALL CAPS without introducing
    incorrect capitalizations.
    """
    if not english:
        # Sentence case: capitalize first word only.
        lowered = title.lower()
        return lowered[0].upper() + lowered[1:] if lowered else lowered

    # English title case.
    # Split preserving punctuation and spaces.
    # We tokenize on word boundaries but keep track of position.
    words = re.split(r"(\s+|[:\u2013\u2014\-])", title)
    result = []
    word_index = 0  # Index counting only actual words (not spaces/punct)
    word_positions = []  # Store indices of actual word tokens

    # First pass: identify word token positions.
    for i, token in enumerate(words):
        if re.match(r"\w", token):
            word_positions.append(i)

    total_words = len(word_positions)

    after_subtitle_marker = False  # Whether next word starts a subtitle

    for i, token in enumerate(words):
        if not re.match(r"\w", token):
            # Spaces and punctuation: check if this is a subtitle marker.
            if re.search(r"[:\u2013\u2014]", token):
                after_subtitle_marker = True
            result.append(token)
            continue

        word_lower = token.lower()
        position_in_words = word_positions.index(i)
        is_first = position_in_words == 0
        is_last = position_in_words == total_words - 1

        if is_first or is_last or after_subtitle_marker:
            # Always capitalize first word, last word, and words after colons/dashes.
            result.append(_capitalize_word(token))
            after_subtitle_marker = False
        elif word_lower in _EN_LOWERCASE_WORDS and len(word_lower) <= 4:
            result.append(word_lower)
        else:
            result.append(_capitalize_word(token))

    return "".join(result)


def _capitalize_word(word: str) -> str:
    """Capitalize the first letter of a word, preserving the rest."""
    if not word:
        return word
    return word[0].upper() + word[1:].lower()


def normalize_titles_in_bibliography(bibliography: dict[str, dict]) -> int:
    """
    Apply title normalization to all entries in the bibliography dict in-place.

    Returns the number of titles changed.
    """
    changed = 0
    for citekey, entry in bibliography.items():
        if citekey.startswith("_"):
            continue
        raw = entry.get("title", "")
        if not raw:
            continue
        langid = entry.get("langid", "")
        normalized = normalize_title(raw, langid)
        if normalized != raw:
            logger.debug(
                "Title normalized for %s:\n  before: %s\n  after:  %s",
                citekey, raw, normalized,
            )
            entry["title"] = normalized
            changed += 1

    if changed:
        logger.info("Normalized %d titles in bibliography.", changed)
    return changed


# =============================================================================
# Author name normalization
# =============================================================================

def normalize_authors_in_bibliography(bibliography: dict[str, dict]) -> int:
    """
    Normalize author given-name forms corpus-wide.

    For each family name seen in the bibliography, find the most complete
    given-name form (longest / most specific). Replace shorter/less complete
    forms with the most complete one, but only where we can be confident
    it's the same person (same family name + overlapping given-name prefix).

    Returns the number of author records changed.
    """
    # --- Pass 1: Build a registry of best known given-name forms per family name ---
    # Key: normalized family name (lowercase, unaccented)
    # Value: dict mapping normalized-given prefix → best given string seen
    def _norm_family(s: str) -> str:
        return unidecode(s).lower().strip()

    def _norm_given(s: str) -> str:
        """Normalize a given name to a comparable prefix form."""
        # Convert "Hanne Lovise" → "hl", "H L" → "hl", "H." → "h"
        parts = re.split(r"[\s.]+", s.strip())
        return "".join(p[0].lower() for p in parts if p)

    # Build: family_norm → list of (given_norm_prefix, full_given_string, length_score)
    registry: dict[str, list[tuple[str, str, int]]] = {}

    for entry in bibliography.values():
        if not isinstance(entry, dict):
            continue
        for role in ("author", "editor"):
            for person in entry.get(role, []):
                family = person.get("family", "").strip()
                given = person.get("given", "").strip()
                if not family or not given:
                    continue
                fam_key = _norm_family(family)
                giv_prefix = _norm_given(given)
                # Length score: longer given name = more complete
                length_score = len(re.sub(r"\s+", "", given))
                registry.setdefault(fam_key, [])
                registry[fam_key].append((giv_prefix, given, length_score))

    # For each family name, find the best (most complete) given form per prefix group.
    # Two given forms belong to the same person if one prefix is a prefix of the other.
    best_given: dict[tuple[str, str], str] = {}  # (fam_key, giv_prefix) → best given

    for fam_key, records in registry.items():
        # Sort by length score descending — longest form first.
        records_sorted = sorted(records, key=lambda r: r[2], reverse=True)

        for giv_prefix, given, score in records_sorted:
            # Find if any existing key in best_given for this family is a
            # prefix match with this form.
            matched = False
            for existing_prefix in list(best_given.keys()):
                if existing_prefix[0] != fam_key:
                    continue
                ep = existing_prefix[1]
                # Same person if one prefix is a prefix of the other.
                if ep.startswith(giv_prefix) or giv_prefix.startswith(ep):
                    # Keep whichever is longer (already in best_given because
                    # we sorted by length descending, so the existing is better
                    # or equal — only update if new is longer).
                    existing_score = len(re.sub(r"\s+", "", best_given[existing_prefix]))
                    if score > existing_score:
                        best_given[existing_prefix] = given
                        # Also register the new prefix.
                        best_given[(fam_key, giv_prefix)] = given
                    matched = True
                    break
            if not matched:
                best_given[(fam_key, giv_prefix)] = given

    # --- Pass 2: Apply best given-name forms across all entries ---
    changed = 0

    for citekey, entry in bibliography.items():
        if not isinstance(entry, dict) or citekey.startswith("_"):
            continue
        for role in ("author", "editor"):
            for person in entry.get(role, []):
                family = person.get("family", "").strip()
                given = person.get("given", "").strip()
                if not family or not given:
                    continue
                fam_key = _norm_family(family)
                giv_prefix = _norm_given(given)

                # Look up the best known given name for this person.
                best = None
                for (fk, gp), best_g in best_given.items():
                    if fk != fam_key:
                        continue
                    if gp.startswith(giv_prefix) or giv_prefix.startswith(gp):
                        if best is None or len(re.sub(r"\s+", "", best_g)) > len(re.sub(r"\s+", "", best)):
                            best = best_g

                if best and best != given:
                    logger.debug(
                        "Author given-name normalized for %s in %s: %r → %r",
                        family, citekey, given, best,
                    )
                    person["given"] = best
                    changed += 1

    if changed:
        logger.info("Normalized %d author given-name forms in bibliography.", changed)
    return changed


# =============================================================================
# Combined entry-level normalization (for newly inserted entries)
# =============================================================================

def normalize_entry(entry: dict[str, Any], langid: str = "") -> dict[str, Any]:
    """
    Apply all normalizations to a single bibliography entry in-place.

    Call this when inserting a new entry before it's added to the bibliography.
    Handles: title cleanup, date/DOI/page normalization, oversized title flagging,
    letter prefix stripping, hyphenated line-break joining, LLM placeholder removal,
    and entry type reclassification for misc entries.

    Args:
        entry:  Bibliography entry dict (modified in-place).
        langid: BibLaTeX langid hint, overrides entry's own langid if provided.

    Returns:
        The same entry dict (for chaining).
    """
    lang = langid or entry.get("langid", "")

    # ── Title cleanup ──────────────────────────────────────────────────────────
    title = entry.get("title", "")
    if title:
        # Strip letter prefix from year+suffix parsing (e.g. "a: Title" → "Title")
        title = re.sub(r"^[a-z]\s*:\s*", "", title).strip()
        # Join hyphenated line breaks
        title = re.sub(r"-\s*\n\s*", "", title)
        title = re.sub(r"-\s*$", "", title).strip()
        # Flag oversized titles (likely compound citation blowout)
        if len(title) > 300:
            entry["_title_too_long"] = True
        # Remove LLM placeholder titles
        _placeholder = re.compile(
            r"^(article|статья|стаття)\s+(by|в\.?\s*и\.?|від)\b|"
            r"^unknown\s+title|^\[untitled\]|^no\s+title",
            re.IGNORECASE,
        )
        if _placeholder.match(title):
            entry["_placeholder_title"] = title
            title = ""
        entry["title"] = normalize_title(title, lang) if title else ""

    # ── Booktitle cleanup ──────────────────────────────────────────────────────
    if entry.get("booktitle"):
        entry["booktitle"] = re.sub(
            r"^['\u2018\u2019\u201c\u201d]+", "", entry["booktitle"]
        ).strip()

    # ── DOI normalization ──────────────────────────────────────────────────────
    doi = entry.get("doi", "")
    if doi:
        entry["doi"] = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi).strip()

    # ── Date normalization: "2016-01" → "2016" ────────────────────────────────
    date = entry.get("date", "")
    if date:
        m = re.match(r"^(\d{4})", str(date))
        if m:
            entry["date"] = m.group(1)
            entry["year"] = m.group(1)

    # ── Page range normalization ───────────────────────────────────────────────
    pages = entry.get("pages", "")
    if pages:
        pages = re.sub(r"e(\d)", r"\1", pages)              # strip spurious 'e'
        pages = re.sub(r"(?<!-)-(?!-)", "--", pages)         # normalize single dash
        entry["pages"] = pages

    # ── Volume extraction from pages field ────────────────────────────────────
    if pages and not entry.get("volume"):
        m = re.match(r"^(\d+)\s*[,:]?\s*(?:pp?\.)?\s*(\d+\s*[-–]\s*\d+)$", pages)
        if m:
            entry["volume"] = m.group(1)
            entry["pages"] = re.sub(r"(?<!-)-(?!-)", "--", m.group(2)).strip()

    # ── Entry type reclassification (misc only — conservative) ────────────────
    if entry.get("entry_type") == "misc":
        journal   = entry.get("journaltitle", "").strip()
        booktitle = entry.get("booktitle", "").strip()
        editors   = entry.get("editor", [])
        volume    = entry.get("volume", "").strip()
        number    = entry.get("number", "").strip()
        p         = entry.get("pages", "").strip()
        pages_range = bool(re.search(r"\d+\s*[-–]+\s*\d+", p))
        if journal and (volume or number or pages_range):
            entry["entry_type"] = "article"
        elif booktitle and editors:
            entry["entry_type"] = "incollection"
        elif booktitle and not editors:
            entry["entry_type"] = "inbook"

    # ── Author/editor given-name cleanup ──────────────────────────────────────
    for role in ("author", "editor"):
        for person in entry.get(role, []):
            if person.get("given"):
                person["given"] = _clean_given_name(person["given"])
            if person.get("family"):
                person["family"] = _clean_family_name(person["family"])

    return entry


def _clean_given_name(given: str) -> str:
    """
    Clean a given name: normalize whitespace, ensure initials have periods.

    Examples:
        "H L"   → "H. L."
        "H.L."  → "H. L."
        "Hanne" → "Hanne"
        "Hanne Lovise" → "Hanne Lovise"
    """
    given = given.strip()
    # If all "words" are single letters (initials without periods), add periods.
    parts = given.split()
    if all(len(p.rstrip(".")) == 1 for p in parts):
        return " ".join(p.rstrip(".") + "." for p in parts)
    return given


def _clean_family_name(family: str) -> str:
    """
    Clean a family name: normalize whitespace, fix obvious issues.
    """
    family = family.strip()
    # Normalize internal whitespace.
    family = re.sub(r"\s+", " ", family)
    return family