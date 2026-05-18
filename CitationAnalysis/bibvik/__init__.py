"""
BibVik Citation Analysis Toolkit

A multi-generational citation graph analysis toolkit for studying citational
practices in Viking Age archaeology and beyond.

Core modules:
    detector    — 5-method citation detection (GROBID bib, inline, regex, LLM body, LLM footnote)
    resolver    — LLM-based resolution for unmatched citations (context inference)
    enricher    — CrossRef and OpenAlex enrichment for identified entries (--enrich)
    graph       — Multi-generational citation graph with deduplication
    tei_parser  — GROBID TEI-XML parsing with compound reference splitting
    normalize   — Title and author name normalization
    audit       — Stratified random sampling for manual graph validation
"""

__version__ = "0.2.0"