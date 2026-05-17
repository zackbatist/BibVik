I'm continuing work on the BibVik Citation Analysis toolkit after a month-long break following a workshop. The full codebase is in the attached zip. Here is the current status and what needs to happen next.

**Project**: BibVik — citation graph analysis for Viking Age archaeology. Seed paper is Lund & Sindbæk (2022) "Crossing the Maelstrom." Corpus is ~380 F1 PDFs, diverse publication types, multiple languages (English, Norwegian, Swedish, Danish, German, French).

**What works**: 5-method citation detection (GROBID bibliography, GROBID inline markers, regex, LLM body scan, LLM footnote extraction), CrossRef + LLM reference resolution, multi-generational graph construction with deduplication and Zotero matching, compound reference splitting, author/title normalization, completeness scoring, graceful Ctrl-C cancellation with partial state saving, caching of already-processed papers across runs.

**Tech stack**: Python 3.14, GROBID 0.8.1 via Docker, Ollama with qwen3.5:35b (thinking mode disabled via `"think": false`), CrossRef API.

**Top priority**: Producing a robust, validated citation graph. This is the foundation for a paper. Context analysis and cluster analysis are secondary — table them for now, but capture data during graph generation that will support them later.

**What needs to happen now, in priority order:**

All of the following serve the primary goal: producing a robust, validated citation graph.

1. **Clean up the codebase.** The code has accumulated overlapping outputs, redundant functions, and unclear log messages from iterative development. It needs to be streamlined with shared infrastructure, clear separation of concerns, and comprehensible output.

2. **Add testing.** Write a sampling and manual audit function to test the accuracy of the citation graph — are the right references being extracted, are they being matched correctly, are duplicates being caught? This should support drawing a stratified random sample and presenting it for human review.

3. **Capture additional structured data during graph generation.** While processing each paper, also extract: page numbers of citations, section/subsection headings where citations appear, language of the paper, and author affiliations. These support later analysis of contexts and author profiles.

4. **Document language of each paper.** Essential for later LLM-based context analysis — non-English papers need a documented procedure for processing or normalizing.

5. **Efficiency.** The full run currently takes days on a laptop. May have access to a 10-GPU cluster, but the tool should also work in environments without that resource. The LLM body scan is the bottleneck.

6. **Handle OCR failures.** Two PDFs (Baastrup 2014, Bergstøl 2004) fail GROBID with `[NO_BLOCKS]` because they're scanned images without a text layer. Add `ocrmypdf` as an automatic fallback — when GROBID returns this error, run OCR on the PDF and retry. This should be generalizable to any scanned PDF in the corpus, not just these two.

7. **Author affiliations.** Extract from each paper's header where available. Lower priority but supports analysis of evolving research profiles.

**Key design constraints:**
- Non-Latin characters throughout the corpus (Scandinavian, German, French names and titles)
- Author names must be properly decomposed into given/family components
- Citekeys must be unique and consistently generated
- All decisions and development actions must be logged
- Code must be systematic and transparent — this is a research project, not a product

**Known issues:**
- Two PDFs always fail GROBID (scanned, no text layer) — needs OCR fallback
- GROBID Docker container crashes on long runs, needs monitoring/restart; ARM emulation on M-series Macs may contribute
- LLM body scan is slow; true batching (multiple paragraphs per prompt) was attempted and broke response parsing; current approach is per-paragraph with caching
- Deduplication is imperfect; author name variants across languages may create duplicates despite normalization
- Graph state file grows large (~4MB at 50 papers); may need a database backend for the full corpus

The attached `Exported_Items.csv` is the Zotero export used for PDF↔citekey matching.

---