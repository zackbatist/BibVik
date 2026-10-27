# Region matching

Turns the free-text **Region** field of the annotation into places. Each
comma-separated entry is matched to a Wikidata item, the item is walked up to
its country, and the country is placed in a hand-authored group. The result is
`analysis/region_matching/region_lookup.csv`, which `analysis/01_annotation.qmd` reads.

Everything is in one script, `match_regions.R`. The Quarto document runs none
of it: it `source()`s the script for its definitions and applies them to the
file. The script lives in its own directory because it calls Wikidata and
writes files, which a render should not do.

## Running it

From the repository root. All four stages need outbound network access.

```
Rscript analysis/region_matching/match_regions.R probe     # hit rate on the 40 commonest tokens
Rscript analysis/region_matching/match_regions.R match     # every token -> Wikidata item (about 10 minutes)
Rscript analysis/region_matching/match_regions.R nesting   # every item -> country and super-region
Rscript analysis/region_matching/match_regions.R all       # match, then nesting
```

With no argument it prints this list and does nothing. Run `probe` first when
the annotation changes a lot: if the hit rate on frequent Viking Age placenames
(Birka, Hedeby, Kaupang, Ribe, Uppåkra, Tissø) is low, the class filter
(`place_classes` in the script) is too narrow.

| Stage | Reads | Writes |
|---|---|---|
| `probe` | `data/references.csv` | nothing, prints a table |
| `match` | `data/references.csv` | `region_lookup.csv`, `region_lookup_needs_review.csv` (both in this directory) |
| `nesting` | `region_lookup.csv` | `region_lookup.csv` (adds `country`, `super_region`) |

`region_lookup_needs_review.csv` lists the matches that are least certain
(`exact_ambiguous`, `search_fallback`, `search_fallback_ambiguous`,
`no_match`). It is for reading, nothing else uses it.

## Terms

The analysis document and this README use these words and no others for these
things.

| Term | Means | Column or object |
|---|---|---|
| **Region field** | What the coder typed for a paper, from the study's own keywords. Free text, may be blank. | `Region` in `data/references.csv` |
| **region token** | One entry of the Region field after splitting on commas, trimming, and removing a trailing period. "Birka, Sweden" is two tokens. | `Region` in the tokens table; `region_token` in the lookup |
| **canonical token** | A token after spelling variants are folded (`Danmark` becomes `Denmark`). Counts and joins use this, so one place is one row. | `Region_normalized`; `variant_map` in the script |
| **matched place** | A canonical token that was matched to one Wikidata item, and so has an identifier, a label and (usually) a point. | `wikidata_qid`, `normalized_label`, `lat`, `lon`, `place_type` |
| **override** | A manual correction for a token whose automatic match was a different, same-named place elsewhere (a Repton in the United States for Repton in Derbyshire). | `region_overrides`; `match_method == "manual_override"` |
| **macro-region** | A token that is not plotted as a point, because one point cannot stand for it. An area name (Scandinavia, Viking world) also has no country and no super-region. A country name (Sweden, Norway) keeps its country and super-region. | `is_macro_region`; `macro_area_terms`, `country_name_terms` |
| **country** | The first ancestor of the matched place, following Wikidata's "located in" chain, that is itself a country. | `country` |
| **super-region** | A group of countries: Scandinavia, British Isles, Continental Europe, Eastern Europe, Baltic states, North Atlantic. Hand-authored, not from Wikidata. | `super_region`; `super_region_map` |

Two collisions to keep straight:

- **"Scandinavia" and "British Isles" are both a token and a super-region.**
  As a token it is what the authors wrote, and it is a macro-region, so it has
  no super-region of its own. As a super-region it is derived from a country.
  A paper tagged only "Scandinavia" has no super-region; a paper tagged
  "Birka" has the super-region Scandinavia.
- **"Region" is used for the annotation field, for a token and, in
  "macro-region" and "super-region", for two different levels.** Prose in the
  analysis says *token* for the first, and always writes the other two with
  their prefix.

Administrative regions (counties, parishes and similar units) are **not
resolved**. They differ in kind between countries and change over time through
consolidation and renaming, so nesting stops at country. The `admin_region`
column existed in earlier copies of the lookup; it is dropped on read and
removed from the file the next time `nesting` runs.

## What `region_lookup.csv` holds

One row per canonical token.

| Column | Set by | Meaning |
|---|---|---|
| `region_token` | `match` | The canonical token. |
| `wikidata_qid` | `match` | Wikidata item identifier, `NA` if unmatched. |
| `normalized_label` | `match` | The matched item's English label. |
| `lat`, `lon` | `match` | The matched item's coordinate. |
| `place_type` | `match` | The matched item's first "instance of" label. |
| `match_method` | `match` | `exact`: a label or alias matched exactly. `exact_ambiguous`: several items matched and none had the token as its label, the first was taken. `search_fallback`: no exact match, found through Wikidata search and checked against the place classes. `search_fallback_ambiguous`: the same with several candidates. `no_match`. `manual_override`: replaced from `region_overrides`. |
| `country` | `nesting` | Country of the matched place. An override can set it directly (Britain). |
| `super_region` | `nesting` | Group of that country. |

`is_macro_region` is not in the file. It is added when the file is read.

### What is applied when the file is read

`finish_region_lookup()` in the script is applied every time the analysis reads
the file. It drops `admin_region`, applies the overrides, normalizes country
names, recomputes `super_region` from `country`, and flags macro-regions. Every
macro-region loses its point (identifier, label, coordinates, place type) and
reads `no_match` in `match_method`. Only the area names also lose `country`
and `super_region`. Because this happens in memory, **editing any list below
and re-rendering takes effect at once, with no network**.

| To change | Edit in `match_regions.R` | Re-run |
|---|---|---|
| Fold two spellings into one token | `variant_map` | nothing if the canonical token already has a row; otherwise `match` |
| Correct a wrong match | `region_overrides`; set `country` in the row when the corrected item has no country ancestor | nothing; `nesting` too if the correction changes the country |
| Treat a token as a macro-region, or stop | `macro_area_terms` or `country_name_terms` | nothing |
| Group a country differently, add a country | `super_region_map` | nothing |
| Change a country's display name | `country_name_normalize` | nothing |
| Match more place classes | `place_classes` | `match`, then `nesting` |

The overrides are also applied before `nesting` resolves countries, so a
corrected token gets the country of the corrected item. The file as committed
predates that for the eight override tokens: it still carries the wrong match's
coordinates for them, or none (Baltic, Scotland, Masham and Repton have
coordinates in the United States; East Norway has none). It also still holds
rows for `Norge` and `Sverige`, which `variant_map` now folds into Norway and
Sweden, so nothing looks them up. The next `nesting` run writes the corrected
coordinates and the next `match` drops the two rows. Nothing in the repository
reads the file without going through `finish_region_lookup()`, so the analysis
is not affected in the meantime.

## Known issues

- **`East Denmark`'s override label** reads "Eastern Denmark (general,
  administrative region est. 2027)". It is shown as written in the lookup.