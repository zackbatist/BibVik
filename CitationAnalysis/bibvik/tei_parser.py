"""
bibvik.tei_parser — Parse GROBID's TEI-XML output into structured data.

GROBID returns documents in TEI (Text Encoding Initiative) XML format. This
module extracts two things from that XML:

1. **Bibliography entries** (<biblStruct> elements inside <listBibl>):
   Each entry contains structured author names, titles, journal/book info,
   dates, identifiers, etc. We parse these into Python dicts that conform
   to biblatex field conventions.

2. **Body text with citation markers** (<body> element):
   GROBID annotates inline citations as <ref type="bibr" target="#b42">
   elements within the parsed body text. We preserve these markers so that
   the context_extractor module can locate where each reference is cited
   and extract the surrounding text.

TEI namespace handling:
    GROBID uses the TEI namespace "http://www.tei-c.org/ns/1.0". All XPath
    queries must use this namespace, which we alias as 'tei'.

Character encoding:
    All text extraction preserves original Unicode characters — Greek, Cyrillic,
    CJK, diacritics, etc. No transliteration is performed here; that's only
    done for citekey generation in utils.py.
"""

import logging
import re
from typing import Any

from lxml import etree

logger = logging.getLogger(__name__)

# TEI namespace used by GROBID output.
TEI_NS = "http://www.tei-c.org/ns/1.0"
NS = {"tei": TEI_NS}


# =============================================================================
# Main entry points
# =============================================================================

def parse_tei_references(tei_xml: str) -> list[dict]:
    """
    Parse all bibliographic references from GROBID's TEI-XML output.

    Each reference is returned as a dict with biblatex-style field names.
    See _parse_biblstruct() for the full field list.

    Args:
        tei_xml: Raw TEI-XML string from GROBID.

    Returns:
        List of reference dicts. Each dict has at minimum:
        - 'title': str
        - 'author': list[dict] with 'family' and 'given' keys
        - '_grobid_id': str (the xml:id used for citation linking)
    """
    root = _parse_xml(tei_xml)
    if root is None:
        return []

    # Find all <biblStruct> elements in the back matter's <listBibl>.
    bibl_structs = root.findall(".//tei:listBibl/tei:biblStruct", NS)

    if not bibl_structs:
        # Fallback: search anywhere in document (some GROBID versions nest
        # differently).
        bibl_structs = root.findall(".//tei:biblStruct", NS)

    logger.info("Found %d bibliography entries in TEI-XML.", len(bibl_structs))

    references = []
    for bs in bibl_structs:
        ref = _parse_biblstruct(bs)
        if ref:
            references.append(ref)

    # --- Post-processing: split compound references ---
    # GROBID often collapses multiple references by the same author into a
    # single biblStruct when the reference list uses dashes (—, –, -) to
    # denote repeated authors. We detect these by looking for year patterns
    # preceded by dashes in the raw citation string, and split them into
    # separate reference records.
    expanded = []
    for ref in references:
        split_refs = _split_compound_reference(ref)
        expanded.extend(split_refs)

    if len(expanded) > len(references):
        logger.info(
            "Split %d compound references into %d individual entries.",
            len(expanded) - len(references) + sum(1 for r in references if r.get("_raw_citation") and _is_compound(r["_raw_citation"])),
            len(expanded) - len(references),
        )

    return expanded


def detect_language(paragraphs: list[dict]) -> str:
    """
    Detect the primary language of a paper from its body paragraphs.

    Uses the lingua library with a restricted set of languages known to
    appear in the BibVik corpus. Restricting to a subset improves both
    accuracy and speed compared to running against all supported languages.

    The first ~2000 characters of body text are used. This is sufficient
    for reliable detection on full paragraphs; shorter samples increase
    the risk of misclassification between closely related Scandinavian
    languages.

    Returns an ISO 639-1 language code (e.g. "en", "no", "da", "sv",
    "de", "fr"), or "unknown" if detection fails or lingua is not
    installed.

    Note: GROBID's own xml:lang attribute on the <text> element is not
    used because inspection of the TEI output found it to be unreliable
    — non-English papers were frequently tagged "en" when abstracts or
    keywords were in English.
    """
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError:
        logger.warning(
            "lingua not installed — language detection unavailable. "
            "Install with: pip install lingua-language-detector"
        )
        return "unknown"

    # Restrict to languages expected in the corpus. This improves accuracy
    # for closely related languages (Norwegian/Danish/Swedish) and reduces
    # load time compared to building a detector for all 75 supported languages.
    detector = LanguageDetectorBuilder.from_languages(
        Language.ENGLISH,
        Language.BOKMAL,
        Language.DANISH,
        Language.SWEDISH,
        Language.GERMAN,
        Language.FRENCH,
    ).build()

    # Concatenate paragraph text until we have ~2000 characters.
    sample_text = ""
    for para in paragraphs:
        sample_text += para.get("text", "") + " "
        if len(sample_text) >= 2000:
            break
    sample_text = sample_text.strip()

    if not sample_text:
        return "unknown"

    detected = detector.detect_language_of(sample_text)
    if detected is None:
        return "unknown"

    # Map lingua Language enum to ISO 639-1 codes.
    iso_map = {
        Language.ENGLISH: "en",
        Language.BOKMAL: "no",
        Language.DANISH: "da",
        Language.SWEDISH: "sv",
        Language.GERMAN: "de",
        Language.FRENCH: "fr",
    }
    return iso_map.get(detected, detected.iso_code_639_1.name.lower())


def parse_tei_body(tei_xml: str) -> list[dict]:
    """
    Parse the body text from GROBID's TEI-XML, preserving citation markers.

    Returns a list of "paragraph" dicts, each containing:
    - 'text': The full paragraph text with citation markers replaced by
              placeholder tokens like {{CITE:b42}}.
    - 'paragraph_index': 1-based sequential index within the paper body.
    - 'section_heading': The heading of the section this paragraph belongs to,
              as a breadcrumb string (e.g. "Results > Typological Analysis").
              Empty string if no heading is found.
    - 'citations': List of dicts, each with:
        - 'grobid_id': The target ID (e.g., "b42")
        - 'marker_text': The original citation marker text (e.g., "(Smith 2020)")
        - 'char_offset': Character offset of the placeholder in 'text'

    The placeholder format {{CITE:id}} is used so that downstream modules can
    locate citations precisely within the text string.

    Args:
        tei_xml: Raw TEI-XML string from GROBID.

    Returns:
        List of paragraph dicts.
    """
    root = _parse_xml(tei_xml)
    if root is None:
        return []

    body = root.find(".//tei:body", NS)
    if body is None:
        logger.warning("No <body> element found in TEI-XML.")
        return []

    # Build a map from each element to its ancestor heading breadcrumb.
    # We walk the body's <div> tree once rather than re-walking for each paragraph.
    heading_map = _build_heading_map(body)

    paragraphs = []
    para_index = 0

    for p_elem in body.iter(f"{{{TEI_NS}}}p"):
        para = _parse_paragraph(p_elem)
        if para and para["text"].strip():
            para_index += 1
            para["paragraph_index"] = para_index
            para["section_heading"] = heading_map.get(id(p_elem), "")
            paragraphs.append(para)

    logger.info("Parsed %d paragraphs from body text.", len(paragraphs))
    return paragraphs


def _build_heading_map(body_elem) -> dict[int, str]:
    """
    Build a map from paragraph element id() to section heading breadcrumb.

    Walks the <div> tree once, tracking the current heading path as we
    descend. Each <p> element is mapped to the heading breadcrumb of its
    nearest ancestor <div> that has a <head> child.

    Returns:
        Dict mapping id(p_element) → heading breadcrumb string.
    """
    heading_map: dict[int, str] = {}

    def walk(elem, ancestor_heads: list[str]) -> None:
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

        if tag == "div":
            head = elem.find(f"{{{TEI_NS}}}head")
            if head is not None:
                head_text = (head.text or "").strip()
                current_heads = ancestor_heads + ([head_text] if head_text else [])
            else:
                current_heads = ancestor_heads

            for child in elem:
                walk(child, current_heads)

        elif tag == "p":
            heading_map[id(elem)] = " > ".join(ancestor_heads)

        else:
            for child in elem:
                walk(child, ancestor_heads)

    walk(body_elem, [])
    return heading_map


def parse_tei_footnotes(tei_xml: str) -> list[dict]:
    """
    Extract footnote text from GROBID's TEI-XML output.

    GROBID represents footnotes as <note place="foot"> elements in the body.
    Some papers (particularly older humanities works) embed full bibliographic
    references in footnotes rather than a separate bibliography section.
    GROBID's standard bibliography extractor misses these entirely.

    Each returned dict contains:
    - 'note_id': The xml:id of the <note> element (or a generated id).
    - 'text': Full text content of the footnote.

    Args:
        tei_xml: Raw TEI-XML string from GROBID.

    Returns:
        List of footnote dicts, in document order.
    """
    root = _parse_xml(tei_xml)
    if root is None:
        return []

    footnotes = []
    seen_ids = set()

    # GROBID places footnotes as <note place="foot"> in the body.
    for i, note_elem in enumerate(root.findall(".//tei:note[@place='foot']", NS)):
        text = _get_text(note_elem).strip()
        if not text or len(text) < 20:
            continue
        note_id = note_elem.get("{http://www.w3.org/XML/1998/namespace}id", f"fn{i}")
        if note_id in seen_ids:
            continue
        seen_ids.add(note_id)
        footnotes.append({"note_id": note_id, "text": text})

    # Also capture unattributed <note> elements that look like bibliographic
    # references (contain a year pattern). Some GROBID versions omit place attribute.
    for i, note_elem in enumerate(root.findall(".//tei:note", NS)):
        if note_elem.get("place") == "foot":
            continue  # Already handled above.
        if note_elem.get("type") == "raw_reference":
            continue  # Raw citation strings, not footnotes.
        text = _get_text(note_elem).strip()
        if not text or len(text) < 30:
            continue
        # Heuristic: only treat as a potential bibliographic footnote if it
        # contains a 4-digit year (most citations do).
        if not re.search(r'\b(?:19|20)\d{2}\b', text):
            continue
        note_id = note_elem.get("{http://www.w3.org/XML/1998/namespace}id", f"fn_misc_{i}")
        if note_id in seen_ids:
            continue
        seen_ids.add(note_id)
        footnotes.append({"note_id": note_id, "text": text})

    logger.info("Found %d footnotes in TEI-XML.", len(footnotes))
    return footnotes


def parse_tei_header(tei_xml: str) -> dict:
    """
    Parse document header metadata (title, authors, abstract) from TEI-XML.

    This is used to build the bibliographic record for the document itself
    (as opposed to its references).

    Args:
        tei_xml: Raw TEI-XML string from GROBID.

    Returns:
        Dict with 'title', 'author', 'abstract', 'date', 'doi' fields.
    """
    root = _parse_xml(tei_xml)
    if root is None:
        return {}

    header = root.find(".//tei:teiHeader", NS)
    if header is None:
        return {}

    result = {}

    # --- Title ---
    title_elem = header.find(
        ".//tei:titleStmt/tei:title[@type='main']", NS
    )
    if title_elem is None:
        title_elem = header.find(".//tei:titleStmt/tei:title", NS)
    result["title"] = _get_text(title_elem) if title_elem is not None else ""

    # --- Authors and affiliations ---
    result["author"] = []
    for author_elem in header.findall(
        ".//tei:fileDesc/tei:sourceDesc//tei:author", NS
    ):
        name = _parse_persname(author_elem.find("tei:persName", NS))
        if name:
            affiliation = _parse_affiliation(author_elem.find("tei:affiliation", NS))
            if affiliation:
                name["affiliation"] = affiliation
            result["author"].append(name)

    # --- Date ---
    date_elem = header.find(
        ".//tei:publicationStmt/tei:date[@type='published']", NS
    )
    if date_elem is None:
        date_elem = header.find(".//tei:publicationStmt/tei:date", NS)
    if date_elem is not None:
        result["date"] = date_elem.get("when", _get_text(date_elem))

    # --- DOI ---
    for idno in header.findall(".//tei:idno", NS):
        if idno.get("type", "").lower() == "doi":
            result["doi"] = _get_text(idno)
            break

    # --- Abstract ---
    abstract_elem = header.find(".//tei:profileDesc/tei:abstract", NS)
    if abstract_elem is not None:
        # The abstract may contain <p> elements; concatenate their text.
        parts = []
        for p in abstract_elem.findall("tei:p", NS):
            parts.append(_get_text(p))
        if not parts:
            parts.append(_get_text(abstract_elem))
        result["abstract"] = " ".join(parts).strip()

    return result


# =============================================================================
# Internal helpers
# =============================================================================

def _parse_affiliation(aff_elem) -> dict | None:
    """
    Parse a TEI <affiliation> element into a structured dict.

    GROBID extracts affiliation data from the paper header when present.
    Quality is inconsistent — some papers have well-structured institutional
    affiliations; others have the author name in the institution field, or
    no affiliation at all. The raw data is stored as-is; reconciliation
    against a controlled vocabulary (ROR, GRID) is deferred.

    Returns a dict with any of: institution, department, address, country.
    Returns None if the element is absent or contains no usable data.
    """
    if aff_elem is None:
        return None

    result: dict[str, str] = {}

    for org in aff_elem.findall("tei:orgName", NS):
        org_type = org.get("type", "")
        text = (org.text or "").strip()
        if text and org_type:
            result[org_type] = text

    addr = aff_elem.find("tei:address", NS)
    if addr is not None:
        for field_tag in ("settlement", "region", "country", "postCode"):
            el = addr.find(f"tei:{field_tag}", NS)
            if el is not None and el.text:
                result[field_tag] = el.text.strip()

    return result if result else None

def _parse_xml(tei_xml: str) -> etree._Element | None:
    """
    Parse a TEI-XML string into an lxml Element tree.

    Handles encoding declarations and common GROBID output quirks.
    """
    try:
        # lxml requires bytes for XML with encoding declarations.
        # We encode to UTF-8 and let lxml handle the rest.
        return etree.fromstring(tei_xml.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        logger.error("Failed to parse TEI-XML: %s", e)
        return None


def _parse_biblstruct(bs: etree._Element) -> dict | None:
    """
    Parse a single <biblStruct> element into a biblatex-style dict.

    GROBID organizes each <biblStruct> into:
    - <analytic>: metadata for the article/chapter itself (title, authors)
    - <monogr>: metadata for the containing publication (journal, book, etc.)
      - <imprint>: publication details (publisher, date, volume, pages)

    We map these to biblatex fields following the biblatex specification:
    - 'title' → article/chapter title (from <analytic>) or book title (from <monogr>)
    - 'journaltitle' → journal name
    - 'booktitle' → book name for chapters in edited volumes
    - 'author' → list of {'family': ..., 'given': ...} dicts
    - 'editor' → list of editor name dicts (for edited volumes)
    - 'date' → publication year/date
    - 'volume', 'number', 'pages' → standard biblatex fields
    - 'publisher', 'location' → imprint info
    - 'doi', 'url', 'isbn', 'issn' → identifiers
    - 'note' → raw citation string (if available) as fallback reference

    Returns None if the entry is too incomplete to be useful.
    """
    result: dict[str, Any] = {}

    # --- GROBID's internal ID (used for citation linking) ---
    grobid_id = bs.get("{http://www.w3.org/XML/1998/namespace}id", "")
    result["_grobid_id"] = grobid_id

    analytic = bs.find("tei:analytic", NS)
    monogr = bs.find("tei:monogr", NS)

    # --- Entry type inference ---
    # If there's an <analytic> element, it's an article or book chapter.
    # If there's only <monogr>, it's a book or standalone publication.
    has_analytic = analytic is not None
    has_journal = False
    has_booktitle = False

    # --- Authors (from <analytic> if present, else <monogr>) ---
    author_source = analytic if has_analytic else monogr
    result["author"] = []
    if author_source is not None:
        for author_elem in author_source.findall("tei:author", NS):
            name = _parse_persname(author_elem.find("tei:persName", NS))
            if name:
                result["author"].append(name)

    # --- Title ---
    if has_analytic:
        title_elem = analytic.find("tei:title[@level='a']", NS)
        if title_elem is None:
            title_elem = analytic.find("tei:title", NS)
        result["title"] = _get_text(title_elem) if title_elem is not None else ""
    elif monogr is not None:
        title_elem = monogr.find("tei:title[@level='m']", NS)
        if title_elem is None:
            title_elem = monogr.find("tei:title", NS)
        result["title"] = _get_text(title_elem) if title_elem is not None else ""

    # --- Monograph info (journal or book) ---
    if monogr is not None:
        # Journal title
        journal_elem = monogr.find("tei:title[@level='j']", NS)
        if journal_elem is not None:
            result["journaltitle"] = _get_text(journal_elem)
            has_journal = True

        # Book title (for chapters in edited volumes)
        book_elem = monogr.find("tei:title[@level='m']", NS)
        if book_elem is not None and has_analytic:
            # This is a chapter in an edited volume: the monograph title
            # is the book title.
            result["booktitle"] = _get_text(book_elem)
            has_booktitle = True
        elif book_elem is not None and not has_analytic:
            # Standalone book — title is already captured above.
            pass

        # Series title
        series_elem = monogr.find("tei:title[@level='s']", NS)
        if series_elem is not None:
            result["series"] = _get_text(series_elem)

        # --- Editors (for edited volumes) ---
        result["editor"] = []
        for editor_elem in monogr.findall("tei:editor", NS):
            # Editors can be nested directly or contain <persName>
            persname = editor_elem.find("tei:persName", NS)
            if persname is not None:
                name = _parse_persname(persname)
            else:
                name = _parse_persname(editor_elem)
            if name:
                result["editor"].append(name)

        # Remove empty editor list to keep output clean
        if not result["editor"]:
            del result["editor"]

        # --- Imprint (publisher, date, volume, etc.) ---
        imprint = monogr.find("tei:imprint", NS)
        if imprint is not None:
            _parse_imprint(imprint, result)

        # --- Meeting / conference info ---
        meeting = monogr.find("tei:meeting", NS)
        if meeting is not None:
            result["eventtitle"] = _get_text(meeting)

    # --- Identifiers (DOI, URL, ISBN, etc.) ---
    for idno in bs.findall(".//tei:idno", NS):
        id_type = idno.get("type", "").lower()
        id_value = _get_text(idno).strip()
        if id_type == "doi" and id_value:
            result["doi"] = id_value
        elif id_type == "arxiv" and id_value:
            result["eprint"] = id_value
            result["eprinttype"] = "arxiv"
        elif id_type in ("isbn", "issn") and id_value:
            result[id_type] = id_value

    # --- URL ---
    for ptr in bs.findall(".//tei:ptr", NS):
        target = ptr.get("target", "")
        if target:
            result["url"] = target
            break

    # --- Raw citation string (fallback) ---
    raw_elem = bs.find("tei:note[@type='raw_reference']", NS)
    if raw_elem is not None:
        result["_raw_citation"] = _get_text(raw_elem)

    # --- Determine entry type ---
    result["entry_type"] = _infer_entry_type(
        has_analytic, has_journal, has_booktitle, result
    )

    # --- Validation: skip entries that are too incomplete ---
    if not result.get("title") and not result.get("_raw_citation"):
        logger.debug("Skipping empty biblStruct: %s", grobid_id)
        return None

    return result


def _parse_persname(elem: etree._Element | None) -> dict | None:
    """
    Parse a <persName> element into a dict with 'family' and 'given' keys.

    GROBID represents author names with child elements:
    - <surname>: Family name
    - <forename type="first">: Given name
    - <forename type="middle">: Middle name (appended to given)

    All Unicode characters are preserved. If the name has no structured
    sub-elements (just raw text), we attempt to split on the last space.
    """
    if elem is None:
        return None

    surname = elem.find("tei:surname", NS)
    forenames = elem.findall("tei:forename", NS)

    family = _get_text(surname).strip() if surname is not None else ""
    given_parts = [_get_text(fn).strip() for fn in forenames if _get_text(fn).strip()]
    given = " ".join(given_parts)

    if not family and not given:
        # Fallback: try raw text content of the element
        raw = _get_text(elem).strip()
        if raw:
            # Heuristic: split on last space → given / family
            parts = raw.rsplit(" ", 1)
            if len(parts) == 2:
                given, family = parts
            else:
                family = raw
        else:
            return None

    return {"family": family, "given": given}


def _parse_imprint(imprint: etree._Element, result: dict) -> None:
    """
    Extract publication details from an <imprint> element.

    Populates the result dict with:
    - 'date': Publication date/year
    - 'volume': Volume number
    - 'number': Issue number
    - 'pages': Page range (normalized to biblatex en-dash format)
    - 'publisher': Publisher name
    - 'location': Place of publication
    """
    # Date
    date_elem = imprint.find("tei:date[@type='published']", NS)
    if date_elem is None:
        date_elem = imprint.find("tei:date", NS)
    if date_elem is not None:
        when = date_elem.get("when", "")
        result["date"] = when if when else _get_text(date_elem)

    # Volume
    vol_elem = imprint.find("tei:biblScope[@unit='volume']", NS)
    if vol_elem is not None:
        result["volume"] = _get_text(vol_elem)

    # Issue / Number
    issue_elem = imprint.find("tei:biblScope[@unit='issue']", NS)
    if issue_elem is not None:
        result["number"] = _get_text(issue_elem)

    # Pages
    page_elem = imprint.find("tei:biblScope[@unit='page']", NS)
    if page_elem is not None:
        page_from = page_elem.get("from", "")
        page_to = page_elem.get("to", "")
        if page_from and page_to:
            result["pages"] = f"{page_from}--{page_to}"
        elif page_from:
            result["pages"] = page_from
        else:
            result["pages"] = _get_text(page_elem)

    # Publisher
    pub_elem = imprint.find("tei:publisher", NS)
    if pub_elem is not None:
        result["publisher"] = _get_text(pub_elem)

    # Location / Place
    loc_elem = imprint.find("tei:pubPlace", NS)
    if loc_elem is not None:
        result["location"] = _get_text(loc_elem)


def _parse_paragraph(p_elem: etree._Element) -> dict:
    """
    Parse a <p> element from the body, extracting text and citation markers.

    GROBID inserts <ref type="bibr" target="#b42"> elements inline in the
    body text to mark citations. We replace these with {{CITE:b42}} placeholders
    and record each citation's position and marker text.

    This approach lets us reconstruct exactly where citations appear in the
    running text, which is essential for context extraction.
    """
    citations = []
    parts = []

    # We walk the element tree manually to interleave text and citation markers.
    # p_elem.text is the text before the first child element.
    if p_elem.text:
        parts.append(p_elem.text)

    for child in p_elem:
        tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""

        if tag == "ref" and child.get("type") == "bibr":
            # This is a citation marker.
            target = child.get("target", "")
            # Strip the leading '#' from the target to get the GROBID ID.
            grobid_id = target.lstrip("#") if target else ""
            marker_text = _get_text(child)

            # Record the citation with its character offset in the text so far.
            char_offset = sum(len(p) for p in parts)

            if grobid_id:
                # Normal case: create a placeholder that the context extractor
                # will resolve to a readable citation marker or citekey.
                placeholder = f"{{{{CITE:{grobid_id}}}}}"
                citations.append({
                    "grobid_id": grobid_id,
                    "marker_text": marker_text,
                    "char_offset": char_offset,
                })
                parts.append(placeholder)
            else:
                # GROBID couldn't link this citation to a bibliography entry.
                # Use the original marker text directly (e.g., "(Smith 2020)")
                # rather than creating an unresolvable placeholder.
                parts.append(marker_text if marker_text else "")

        elif tag == "ref":
            # Non-bibliographic ref (e.g., figure, table reference).
            # Include its text content normally.
            parts.append(_get_text(child))
        else:
            # Other inline elements (e.g., <hi>, <formula>).
            # Include their text content.
            parts.append(_get_text(child))

        # Tail text (text after the closing tag of the child element).
        if child.tail:
            parts.append(child.tail)

    text = "".join(parts)

    return {
        "text": text,
        "citations": citations,
    }


def _infer_entry_type(
    has_analytic: bool,
    has_journal: bool,
    has_booktitle: bool,
    result: dict,
) -> str:
    """
    Infer the biblatex entry type from the structural cues in the TEI data.

    Mapping logic:
    - analytic + journal → @article
    - analytic + booktitle → @incollection (chapter in edited volume)
    - analytic + eventtitle → @inproceedings
    - no analytic + title → @book
    - fallback → @misc

    This is necessarily heuristic — GROBID doesn't explicitly state the entry
    type, so we infer it from the presence/absence of structural elements.
    """
    if has_analytic and has_journal:
        return "article"
    elif has_analytic and has_booktitle:
        return "incollection"
    elif has_analytic and result.get("eventtitle"):
        return "inproceedings"
    elif has_analytic:
        # Has analytic but no container info — treat as article (common for
        # preprints, working papers)
        return "article"
    elif not has_analytic and result.get("title"):
        return "book"
    else:
        return "misc"


def _get_text(elem: etree._Element | None) -> str:
    """
    Extract all text content from an element and its descendants.

    Uses lxml's itertext() to capture text across nested inline elements
    (e.g., <hi rend="italic">), preserving the original character encoding.
    """
    if elem is None:
        return ""
    return "".join(elem.itertext())


# =============================================================================
# Compound reference splitting
# =============================================================================

# Pattern that detects dash-year boundaries in raw citation strings.
# Matches: "—1987.", "–1989.", "-1987.", ". -1987." etc.
# These indicate a new reference by the same author in reference lists that
# use dashes to abbreviate repeated author names.
_COMPOUND_SPLIT_RE = re.compile(
    r'(?<=[.;])\s*'           # After a period or semicolon
    r'[-–—]'                  # A dash (any type)
    r'\s*(\d{4})'             # Followed by a 4-digit year
    r'[.,]?\s+'               # Optional punctuation and space
)

# Looser pattern that also catches "Author, X. YYYY." boundaries where
# a different author starts within the same raw string. This handles cases
# where GROBID merged two completely different authors into one biblStruct.
_AUTHOR_BOUNDARY_RE = re.compile(
    r'(?<=\s)'                        # After whitespace
    r'([A-ZÄÖÜÅÆØ][a-zäöüåæø]+,'     # Capitalized surname followed by comma
    r'\s+'                            # Space
    r'[A-ZÄÖÜÅÆØ]\.)'                # Initial with period
    r'\s*'                            # Optional space
    r'(?=\d{4})'                      # Followed by a year (lookahead)
)


def _is_compound(raw_citation: str) -> bool:
    """Check whether a raw citation string contains multiple references."""
    if not raw_citation:
        return False
    # Check for dash-year pattern
    if _COMPOUND_SPLIT_RE.search(raw_citation):
        return True
    # Check for multiple author-year patterns (different authors merged)
    import re
    author_years = re.findall(r'[A-Z][a-zà-ö]+,\s+[A-Z]\..*?\b((?:19|20)\d{2})\b', raw_citation)
    if len(author_years) >= 2:
        # Check if they look like distinct references (different years or names)
        return True
    return False


def _split_compound_reference(ref: dict) -> list[dict]:
    """
    Split a compound reference (one biblStruct containing multiple works)
    into individual reference records.

    Detection:
        We examine the _raw_citation field for patterns indicating multiple
        references were merged. The most common pattern in humanities reference
        lists is the dash convention:

            Jansson, I. 1986. Title One. Publisher.
            —1987. Title Two. Publisher.
            —1989. Title Three. Publisher.

        GROBID sees this as one block and creates one biblStruct. We split on
        the dash-year boundaries.

        We also detect cases where completely different authors were merged
        (GROBID failed to find a boundary between consecutive references).

    For each split segment, we create a new reference dict that inherits the
    original author (for dash-abbreviated entries) and gets a synthetic
    GROBID ID (original ID + "_split_N") so that it can be tracked.

    Returns:
        List of reference dicts. If the reference is not compound, returns
        a single-element list containing the original.
    """
    raw = ref.get("_raw_citation", "")
    if not raw or not _is_compound(raw):
        return [ref]

    original_authors = ref.get("author", [])
    original_grobid_id = ref.get("_grobid_id", "")

    # --- Try dash-year splitting first ---
    segments = _split_on_dash_year(raw)

    if len(segments) <= 1:
        # --- Try author-boundary splitting on the whole string ---
        segments = _split_on_author_boundary(raw)
    else:
        # --- Also apply author-boundary splitting to each dash-split segment ---
        # This catches cases like the Jansson/Wikander merge where a different
        # author is appended to the last dash-split segment.
        expanded_segments = []
        for seg in segments:
            sub_parts = _split_on_author_boundary(seg)
            expanded_segments.extend(sub_parts)
        segments = expanded_segments

    if len(segments) <= 1:
        return [ref]

    # --- Build individual reference dicts from segments ---
    results = []
    for i, segment in enumerate(segments):
        new_ref = _parse_raw_segment(segment, original_authors, original_grobid_id, i)
        if new_ref:
            results.append(new_ref)

    if not results:
        return [ref]

    # The first result inherits all structured data from the original.
    # Merge the original's structured fields into the first result.
    first = results[0]
    for key in ref:
        if key not in first or not first[key]:
            first[key] = ref[key]
    # But override the raw citation with just the first segment.
    first["_raw_citation"] = segments[0].strip()

    logger.debug(
        "Split compound reference %s into %d parts.",
        original_grobid_id, len(results),
    )

    return results


def _split_on_dash_year(raw: str) -> list[str]:
    """
    Split a raw citation string on dash-year boundaries.

    Input:  "Author, A. 1986. Title One. Place. -1987. Title Two. Place."
    Output: ["Author, A. 1986. Title One. Place.", "Author, A. 1987. Title Two. Place."]

    The author name from the beginning of the string is prepended to each
    split segment (since the dash replaces the repeated author name).
    """
    # Find the author prefix (everything before the first year).
    import re
    first_year_match = re.search(r'\b((?:19|20)\d{2})\b', raw)
    if not first_year_match:
        return [raw]

    # Extract the author portion (before the first year).
    author_prefix = raw[:first_year_match.start()].strip()

    # Split on dash-year patterns.
    parts = _COMPOUND_SPLIT_RE.split(raw)

    if len(parts) <= 1:
        return [raw]

    # parts alternates between text and captured year groups.
    # E.g., for "Author, A. 1986. Title. -1987. Title2."
    # parts might be ["Author, A. 1986. Title.", "1987", ". Title2."]
    segments = []

    # First segment: everything up to the first split point.
    segments.append(parts[0].strip())

    # Subsequent segments: year + following text, with author prepended.
    i = 1
    while i < len(parts):
        year = parts[i] if i < len(parts) else ""
        text = parts[i + 1] if i + 1 < len(parts) else ""
        segment = f"{author_prefix} {year}. {text.strip()}"
        segments.append(segment.strip())
        i += 2

    return [s for s in segments if s.strip()]


def _split_on_author_boundary(raw: str) -> list[str]:
    """
    Split a raw citation string where different authors were merged.

    Looks for patterns like "... Stockholm. Wikander, S. 1978. ..." where
    a new author name starts after a sentence-ending period.
    """
    splits = list(_AUTHOR_BOUNDARY_RE.finditer(raw))
    if not splits:
        return [raw]

    segments = []
    prev_end = 0
    for match in splits:
        # Only split if there's substantial text before this point.
        before = raw[prev_end:match.start()].strip()
        if len(before) > 30:
            segments.append(before)
            prev_end = match.start()

    # Add the final segment.
    remaining = raw[prev_end:].strip()
    if remaining:
        segments.append(remaining)

    return segments if len(segments) > 1 else [raw]


def _parse_raw_segment(
    segment: str,
    default_authors: list[dict],
    original_grobid_id: str,
    index: int,
) -> dict | None:
    """
    Parse a single raw citation segment into a reference dict.

    This is a lightweight parser for raw citation strings — much simpler
    than GROBID's full ML pipeline but sufficient for recovering split
    references.
    """
    import re
    segment = segment.strip()
    if not segment or len(segment) < 15:
        return None

    ref: dict[str, Any] = {}

    # Synthetic GROBID ID.
    ref["_grobid_id"] = f"{original_grobid_id}_split_{index}" if index > 0 else original_grobid_id
    ref["_raw_citation"] = segment

    # --- Extract year ---
    year_match = re.search(r'\b((?:19|20)\d{2})\b', segment)
    if year_match:
        ref["date"] = year_match.group(1)

    # --- Extract author ---
    # Try to parse "Family, Given." or "Family, G." at the start.
    author_match = re.match(
        r'^([A-ZÀ-Ö][a-zà-ö]+(?:\s+[A-ZÀ-Ö][a-zà-ö]+)?),\s*'  # Family name(s)
        r'([A-ZÀ-Ö]\.(?:\s*[A-ZÀ-Ö]\.)*)',                       # Initials
        segment,
    )
    if author_match:
        ref["author"] = [{"family": author_match.group(1), "given": author_match.group(2)}]
    elif default_authors:
        ref["author"] = default_authors
    else:
        ref["author"] = []

    # --- Extract title ---
    # Heuristic: title is the text between the year and the next period
    # followed by a publisher/container indicator.
    if year_match:
        after_year = segment[year_match.end():].strip().lstrip(".,: ")
        # Take text up to the first period that looks like end-of-title.
        title_match = re.match(r'^(.+?)[.]', after_year)
        if title_match:
            ref["title"] = title_match.group(1).strip()
        else:
            ref["title"] = after_year[:200].strip()
    else:
        ref["title"] = segment[:200].strip()

    # --- Entry type ---
    ref["entry_type"] = "misc"
    segment_lower = segment.lower()
    if any(kw in segment_lower for kw in ["journal", "vol.", "volume", "pp."]):
        ref["entry_type"] = "article"
    elif any(kw in segment_lower for kw in [" in ", "(ed.", "(eds.", "edited by"]):
        ref["entry_type"] = "incollection"

    return ref