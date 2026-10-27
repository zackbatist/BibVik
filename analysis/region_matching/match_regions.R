# Region matching and resolution for the BibVik annotation analysis.
#
# Turns the free-text Region field in data/references.csv into places:
# each comma-separated entry (a "region token") is matched to a Wikidata
# item, the item is walked up to its country, and the country is grouped
# into a super-region. See README.md in this directory for what every term
# means and what every column in region_lookup.csv, written beside this
# script, holds.
#
# Run from the repository root:
#
#   Rscript analysis/region_matching/match_regions.R probe     # hit rate on the 40 commonest tokens
#   Rscript analysis/region_matching/match_regions.R match     # tokens -> Wikidata items  (~10 min)
#   Rscript analysis/region_matching/match_regions.R nesting   # items -> country, super-region
#   Rscript analysis/region_matching/match_regions.R all       # match, then nesting
#
# All four need outbound network access. With no argument the script
# prints this list and does nothing.
#
# analysis/01_annotation.qmd does not run any stage. It source()s this
# file for the definitions below (variant_map, region_overrides,
# macro_region_terms, super_region_map and the functions that use them),
# so each is defined once, here. Everything above the final block is
# definitions only: sourcing the file has no side effects beyond loading
# tidyverse and here.

suppressPackageStartupMessages({
  library(tidyverse)
  library(here)
})

region_lookup_path <- here("analysis", "region_matching", "region_lookup.csv")
region_review_path <- here("analysis", "region_matching", "region_lookup_needs_review.csv")

# ---------------------------------------------------------------------------
# 1. Tokens
# ---------------------------------------------------------------------------

# Spelling and language variants folded to one canonical token before
# matching and before counting. Add to this list as more are found during
# review; it is not exhaustive, just the duplicates visible in the
# corpus-wide token frequency table.
variant_map <- c(
  "Danmark" = "Denmark",
  "Norge" = "Norway",
  "Sverige" = "Sweden"
)

# One row per (citekey, region token): split on commas, trimmed, blanks
# dropped, trailing period removed ("Mälaren." is a citation-formatting
# artifact, not part of the place name), then variants folded into
# `Region_normalized`. `df` needs the columns citekey and Region.
read_region_tokens <- function(df) {
  df %>%
    select(citekey, Region) %>%
    filter(!is.na(Region), Region != "") %>%
    separate_rows(Region, sep = ",") %>%
    mutate(Region = str_trim(Region)) %>%
    filter(Region != "") %>%
    mutate(Region = str_remove(Region, "\\.$"),
           Region_normalized = coalesce(unname(variant_map[Region]), Region))
}

# ---------------------------------------------------------------------------
# 2. Manual overrides
# ---------------------------------------------------------------------------

# Tokens that matched (by exact label or by search fallback) a real
# Wikidata place that shares the corpus token's name but is a different,
# wrong place: a same-name town elsewhere, not the site the paper means.
# Each row is a directly verified Wikidata item and coordinate for what the
# token means in this corpus. Applied by every stage that writes the lookup
# file and again by finish_region_lookup() on read, so adding or fixing a
# row here takes effect at the next render without re-running the match.
# If a new row changes the QID, run the nesting stage afterwards so the
# item's country is resolved from the right place.
#
# The `country` column is filled only where the matched item cannot give it.
# "Britain" matches the island of Great Britain, which has no country
# ancestor in Wikidata, so its row sets the United Kingdom directly. The
# override has no coordinate: Britain is a country-name token and is kept
# off the map anyway.
region_overrides <- tibble::tribble(
  ~region_token,   ~wikidata_qid,  ~normalized_label,          ~lat,      ~lon,       ~place_type, ~country,
  "East Norway",   "Q216065",      "Eastern Norway (Østlandet)", 60.25,   10.67,      "region",    NA,
  "Repton",        "Q1018787",     "Repton",                    52.83983, -1.55061,  "village",   NA,
  "Baltic",        "Q545",         "Baltic Sea",                58,       20,         "sea",       NA,
  "North Norway",  "Q214041",      "Northern Norway",           69.667,   19.0,       "region",    NA,
  "Scotland",      "Q22",          "Scotland",                  56,       -4,         "country",   NA,
  "Masham",        "Q2330678",     "Masham",                    54.22,   -1.656,      "town",      NA,
  "West Sweden",   "Q4992082",     "West Sweden (Västsverige)", 57.558,   12.602,     "region",    NA,
  "East Denmark",  "Q131281582",   "Eastern Denmark (general, administrative region est. 2027)", 55.44, 11.56, "region", NA,
  "Britain",       "Q145",         "United Kingdom",            NA,       NA,         NA,          "United Kingdom"
)

apply_region_overrides <- function(lookup) {
  # `country` is missing from the file until the nesting stage has run once.
  if (!"country" %in% names(lookup)) lookup$country <- NA_character_
  lookup %>%
    left_join(region_overrides, by = "region_token", suffix = c("", "_override")) %>%
    mutate(
      wikidata_qid = coalesce(wikidata_qid_override, wikidata_qid),
      normalized_label = coalesce(normalized_label_override, normalized_label),
      lat = coalesce(lat_override, lat),
      lon = coalesce(lon_override, lon),
      place_type = coalesce(place_type_override, place_type),
      country = coalesce(country_override, country),
      match_method = if_else(!is.na(wikidata_qid_override), "manual_override", match_method)
    ) %>%
    select(-ends_with("_override"))
}

# ---------------------------------------------------------------------------
# 3. Macro-region terms
# ---------------------------------------------------------------------------

# Tokens that cannot honestly be plotted as a point, whatever gazetteer is
# used. They are flagged is_macro_region and lose their point fields (QID,
# label, coordinates, place type) in finish_region_lookup(). Wikidata does
# return a coordinate for several of them (Eurasia lands in Kazakhstan, 50N
# 80E), which is coincidental placement of a continent-scale concept, not a
# site.
#
# Two kinds of term are listed, in two vectors because they differ in what
# else they lose:
#
# - macro_area_terms: names for an area, or a part of a country, that no
#   single point or country represents (Scandinavia, Viking world, South
#   Sweden). They also lose `country` and `super_region`. "East Baltic" is
#   here because its Wikidata entry (Q5327781) is a disambiguation page,
#   not a single place.
# - country_name_terms: names of one country. Wikidata matches them
#   correctly, but a country has no site-level coordinate, so they are kept
#   off the point map. They keep `country` and `super_region`, so a paper
#   tagged only "Sweden" counts toward Sweden in the by-country views.
macro_area_terms <- c(
  "Scandinavia", "South Scandinavia", "North Atlantic", "North Europe",
  "Northwest Europe", "East Europe", "Viking world", "British Isles",
  "Scandinavian diaspora", "the North", "Arctic Canada", "Arctic Norway",
  "Central East Europe", "Central East Sweden", "East Arctic",
  "Middle Sweden", "North Norway and Sweden", "North Scandinavia",
  "Northwest Slavic area", "Norway and Sweden", "Sami areas",
  "Southeast Norway", "Southwest Norway", "Southwest Sweden",
  "West Scandinavia", "West-Balts", "East Baltic", "Eurasia",
  "British isles", "Europe", "Northern Europe", "Northern Hemisphere",
  "Oceania", "Pacific Ocean", "South Norway", "South Sweden",
  "Southwest Finland", "West Europe"
)

country_name_terms <- c(
  "Britain", "Denmark", "England", "Finland", "Germany", "Norway",
  "Poland", "Russia", "Sweden"
)

macro_region_terms <- c(macro_area_terms, country_name_terms)

# ---------------------------------------------------------------------------
# 4. Country and super-region
# ---------------------------------------------------------------------------

# Wikidata sometimes labels a country item with its formal name; folded to
# the common name once so every downstream use sees one value per country.
country_name_normalize <- c(
  "Kingdom of Denmark" = "Denmark",
  "Kingdom of the Netherlands" = "Netherlands",
  "Kingdom of Sweden" = "Sweden",
  "Kingdom of Norway" = "Norway",
  "Republic of Ireland" = "Ireland",
  "Kingdom of Belgium" = "Belgium"
)

# Hand-authored, not derived from Wikidata: there is no single reliable
# Wikidata class for these groupings (Scandinavia, British Isles, Baltic
# states). They are historical and cultural, not administrative. Extend as
# more countries appear in the resolved data; the nesting stage lists any
# country that has no entry yet.
super_region_map <- c(
  "Sweden" = "Scandinavia", "Norway" = "Scandinavia", "Denmark" = "Scandinavia",
  "Iceland" = "Scandinavia", "Finland" = "Scandinavia", "Greenland" = "Scandinavia",
  "United Kingdom" = "British Isles", "Ireland" = "British Isles",
  "Russia" = "Eastern Europe", "Ukraine" = "Eastern Europe",
  "Poland" = "Eastern Europe", "Lithuania" = "Baltic states",
  "Latvia" = "Baltic states", "Estonia" = "Baltic states",
  "Germany" = "Continental Europe", "Netherlands" = "Continental Europe",
  "Belgium" = "Continental Europe",
  "France" = "Continental Europe", "Canada" = "North Atlantic"
)

normalize_country <- function(country) {
  coalesce(unname(country_name_normalize[country]), country)
}

lookup_super_region <- function(country) {
  unname(super_region_map[country])
}

# ---------------------------------------------------------------------------
# 5. Reading the lookup
# ---------------------------------------------------------------------------

# Everything that is applied on read, in memory, so that editing any list
# above and re-rendering is instant. region_lookup.csv holds what the
# network stages produced; this function turns it into the table the
# analysis uses.
finish_region_lookup <- function(lookup) {
  point_columns <- c("wikidata_qid", "normalized_label", "lat", "lon", "place_type")

  lookup %>%
    # Administrative regions are retired. Older copies of the file still
    # carry the column, so it is dropped on read.
    select(-any_of("admin_region")) %>%
    apply_region_overrides() %>%
    mutate(country = normalize_country(country),
           super_region = lookup_super_region(country),
           is_macro_region = region_token %in% macro_region_terms) %>%
    # Every macro-region loses its point; only area names also lose their
    # country and super-region.
    mutate(across(all_of(point_columns), ~ if_else(is_macro_region, NA, .)),
           across(c(country, super_region),
                  ~ if_else(region_token %in% macro_area_terms, NA, .)),
           match_method = if_else(is_macro_region, "no_match", match_method))
}

# ---------------------------------------------------------------------------
# 6. Wikidata stages (network)
# ---------------------------------------------------------------------------

# Place-like Wikidata classes a match may belong to: archaeological site
# (Q839954), human settlement (Q486972), location (Q17334923) generally,
# island (Q23442), geographic region (Q82794), tumulus / burial mound
# (Q34023). The tumulus class was added after an initial probe missed
# Gokstad and Oseberg, which Wikidata classifies as tumuli. Deliberately
# broad on class and narrow on label: better to over-fetch candidates and
# filter in R than to miss a real match by being too restrictive in SPARQL.
place_classes <- "wd:Q839954 wd:Q486972 wd:Q17334923 wd:Q23442 wd:Q82794 wd:Q34023"

# Label or alias match in English, Swedish, Danish, Norwegian and
# Icelandic, the languages a Scandinavian place name is most likely to
# appear under.
place_query_template <- sprintf('
SELECT ?item ?itemLabel ?coord ?instanceOfLabel WHERE {
  VALUES ?searchterm { "%%s"@en "%%s"@sv "%%s"@da "%%s"@nb "%%s"@is }
  ?item rdfs:label|skos:altLabel ?searchterm .
  ?item wdt:P31/wdt:P279* ?instanceOf .
  VALUES ?instanceOf { %s }
  OPTIONAL { ?item wdt:P625 ?coord . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 5
', place_classes)

# WikidataQueryServiceR comes from CRAN, with a GitHub fallback if the CRAN
# mirror is blocked.
require_wikidata <- function() {
  pkg <- "WikidataQueryServiceR"
  if (!requireNamespace(pkg, quietly = TRUE)) {
    install.packages(pkg, repos = "https://cloud.r-project.org")
  }
  if (!requireNamespace(pkg, quietly = TRUE)) {
    if (!requireNamespace("remotes", quietly = TRUE)) {
      install.packages("remotes", repos = "https://cloud.r-project.org")
    }
    remotes::install_github("bearloga/WikidataQueryServiceR")
  }
  if (!requireNamespace(pkg, quietly = TRUE)) {
    stop(
      pkg, " failed to install from both CRAN and GitHub. This points to an ",
      "environment issue rather than a package issue, most likely no outbound ",
      "network access at all from this machine. Run ",
      "install.packages(\"", pkg, "\", repos = \"https://cloud.r-project.org\") ",
      "directly in the console to see the real underlying error."
    )
  }
  if (!requireNamespace("httr", quietly = TRUE)) {
    install.packages("httr", repos = "https://cloud.r-project.org")
  }
}

query_wikidata <- function(q) WikidataQueryServiceR::query_wikidata(q)

# Fallback for tokens with no exact label or alias match. A SPARQL CONTAINS
# scan was tried first and timed out at Wikidata's scale even with the
# class filter applied first, so this uses wbsearchentities, the API behind
# Wikidata's own search box, which is backed by a real search index. It
# cannot filter by class, so the class check happens as a second step on
# each candidate it returns (check_place_class). This is what catches
# "Gokstad": the real item is titled "Gokstad Mound", so an exact match on
# the bare token never hits.
search_entities <- function(token, limit = 5) {
  res <- tryCatch(
    httr::GET(
      "https://www.wikidata.org/w/api.php",
      query = list(
        action = "wbsearchentities", search = token, language = "en",
        type = "item", limit = limit, format = "json"
      ),
      httr::timeout(15)
    ),
    error = function(e) NULL
  )
  if (is.null(res) || httr::http_error(res)) return(tibble())
  parsed <- tryCatch(httr::content(res, as = "parsed", type = "application/json"),
                     error = function(e) NULL)
  if (is.null(parsed) || length(parsed$search) == 0) return(tibble())
  tibble(
    qid = map_chr(parsed$search, ~ .x$id %||% NA_character_),
    label = map_chr(parsed$search, ~ .x$label %||% NA_character_)
  )
}

check_place_class <- function(qids) {
  if (length(qids) == 0) return(tibble())
  values_clause <- paste(paste0("wd:", qids), collapse = " ")
  q <- sprintf('
SELECT ?item ?itemLabel ?coord ?instanceOfLabel WHERE {
  VALUES ?item { %s }
  ?item wdt:P31/wdt:P279* ?instanceOf .
  VALUES ?instanceOf { %s }
  OPTIONAL { ?item wdt:P625 ?coord . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}', values_clause, place_classes)
  tryCatch(query_wikidata(q), error = function(e) tibble())
}

# Coverage probe: before matching every token, check the hit rate on the
# most frequent ones. This is the empirical check for whether Wikidata is
# workable here. If the hit rate on real, frequent Viking Age placenames
# (Birka, Hedeby, Kaupang, Ribe, Uppåkra, Tissø) is low, the class filter
# may be too narrow and place_classes needs revising before a full match.
# A high count under "search" is expected and fine: it means the class
# filter is right but the corpus token is a shortened form of the item's
# real label ("Gokstad" for "Gokstad Mound").
probe_regions <- function(region_tokens, n = 40) {
  require_wikidata()
  if (!requireNamespace("R.utils", quietly = TRUE)) {
    install.packages("R.utils", repos = "https://cloud.r-project.org")
  }
  top_tokens <- region_tokens %>%
    count(Region_normalized, sort = TRUE) %>%
    slice_max(n, n = n, with_ties = FALSE) %>%
    pull(Region_normalized)

  probe_one <- function(token) {
    cat("Querying:", token, "... ")
    q <- do.call(sprintf, c(list(place_query_template), as.list(rep(token, 5))))
    res <- tryCatch(
      R.utils::withTimeout(query_wikidata(q), timeout = 20, onTimeout = "silent"),
      error = function(e) NULL
    )
    if (!is.null(res) && nrow(res) > 0) {
      cat(nrow(res), "candidate(s) [exact]\n")
      return(tibble(token = token, hit = TRUE, n_candidates = nrow(res), match_type = "exact"))
    }
    # Exact match failed: try the search fallback before giving up.
    candidates <- search_entities(token)
    if (nrow(candidates) == 0) {
      cat("no match\n")
      return(tibble(token = token, hit = FALSE, n_candidates = 0, match_type = "none"))
    }
    res_c <- check_place_class(candidates$qid)
    if (nrow(res_c) == 0) {
      cat("no match (search hit, wrong class)\n")
      return(tibble(token = token, hit = FALSE, n_candidates = 0, match_type = "none"))
    }
    cat(nrow(res_c), "candidate(s) [search]\n")
    tibble(token = token, hit = TRUE, n_candidates = nrow(res_c), match_type = "search")
  }

  probe_results <- map_dfr(top_tokens, probe_one)
  cat("Coverage probe: ", sum(probe_results$hit), " / ", nrow(probe_results),
      " of the top ", length(top_tokens), " tokens returned at least one candidate\n", sep = "")
  cat("  exact:", sum(probe_results$match_type == "exact"),
      " | search fallback:", sum(probe_results$match_type == "search"),
      " | no match:", sum(probe_results$match_type == "none"), "\n")
  print(probe_results, n = 50)
  invisible(probe_results)
}

# Full match: every token to a Wikidata item. Writes the lookup table and a
# manual-review export. Overrides, macro-region flags, country and
# super-region are not applied here; the nesting stage and
# finish_region_lookup() do that.
match_regions <- function(region_tokens) {
  require_wikidata()
  unique_tokens <- region_tokens %>%
    distinct(Region_normalized) %>%
    arrange(Region_normalized) %>%
    pull(Region_normalized)

  match_one <- function(token) {
    q <- do.call(sprintf, c(list(place_query_template), as.list(rep(token, 5))))
    res <- tryCatch(query_wikidata(q), error = function(e) NULL)

    if (is.null(res) || nrow(res) == 0) {
      # Exact match failed: try the search fallback before giving up.
      candidates <- search_entities(token)
      res_c <- if (nrow(candidates) > 0) check_place_class(candidates$qid) else tibble()
      if (nrow(res_c) == 0) {
        return(tibble(
          region_token = token, wikidata_qid = NA_character_,
          normalized_label = NA_character_, lat = NA_real_, lon = NA_real_,
          place_type = NA_character_, match_method = "no_match"
        ))
      }
      res <- res_c
      fallback_used <- TRUE
    } else {
      fallback_used <- FALSE
    }

    # Multiple candidates: prefer the one whose returned label exactly
    # equals the token over an arbitrary first result, since an exact-label
    # match among several candidates is a real signal, not a coin flip.
    # Falls back to the first row only when no label matches exactly; that
    # case is flagged ambiguous below, since the pick is genuinely uncertain.
    exact_label_match <- which(str_to_lower(res$itemLabel) == str_to_lower(token))
    best <- if (length(exact_label_match) > 0) res[exact_label_match[1], ] else res[1, ]
    resolved_by_label <- length(exact_label_match) > 0

    coord <- best$coord
    lat <- NA_real_; lon <- NA_real_
    if (!is.na(coord) && str_detect(coord, "^Point")) {
      nums <- str_match(coord, "Point\\(([-0-9.]+) ([-0-9.]+)\\)")
      lon <- as.numeric(nums[2]); lat <- as.numeric(nums[3])
    }
    is_ambiguous <- nrow(res) > 1 && !resolved_by_label
    method <- if (fallback_used) {
      if (is_ambiguous) "search_fallback_ambiguous" else "search_fallback"
    } else {
      if (is_ambiguous) "exact_ambiguous" else "exact"
    }
    tibble(
      region_token = token,
      wikidata_qid = str_extract(best$item, "Q[0-9]+$"),
      normalized_label = best$itemLabel,
      lat = lat, lon = lon,
      place_type = best$instanceOfLabel,
      match_method = method
    )
  }

  region_matches <- map_dfr(unique_tokens, match_one)

  cat("Matched:", sum(region_matches$match_method != "no_match"), "/", nrow(region_matches), "\n")
  cat("Ambiguous (multiple candidates, first taken):",
      sum(region_matches$match_method == "exact_ambiguous"), "\n")
  cat("No match:", sum(region_matches$match_method == "no_match"), "\n")
  print(region_matches %>% filter(match_method == "no_match") %>% pull(region_token))

  write_csv(region_matches, region_lookup_path)
  cat("Written to", region_lookup_path, ":", nrow(region_matches), "rows\n")

  region_matches %>%
    filter(match_method %in% c("exact_ambiguous", "search_fallback",
                               "search_fallback_ambiguous", "no_match")) %>%
    write_csv(region_review_path)
  invisible(region_matches)
}

# Nesting: resolve each matched item to its country (walked up P131* to the
# first ancestor of class country, Q6256), then to its super-region through
# super_region_map. Administrative regions are not resolved: they differ in
# kind between countries and change over time through consolidation and
# renaming, so nesting stops at country.
#
# Overrides are applied first, so country is resolved from the corrected
# item for the tokens in region_overrides, not from the wrong match.
resolve_nesting <- function() {
  require_wikidata()
  lookup <- read_csv(region_lookup_path, show_col_types = FALSE) %>%
    # Drop any nesting columns from a prior run, otherwise the join below
    # produces country.x / country.y duplicates. admin_region is listed too:
    # the column is retired, and dropping it here removes it from the file.
    select(-any_of("admin_region")) %>%
    apply_region_overrides() %>%
    # The overrides' own country is re-applied on read by
    # finish_region_lookup(). Any country from a prior run is dropped here,
    # otherwise the join below produces country.x / country.y duplicates.
    select(-any_of(c("country", "super_region")))

  qids_to_resolve <- lookup %>%
    filter(!is.na(wikidata_qid)) %>%
    distinct(wikidata_qid) %>%
    pull(wikidata_qid)
  cat("Resolving nesting for", length(qids_to_resolve), "matched places...\n")

  # The country ancestor is wrapped in OPTIONAL: some matched items
  # (islands, seas) have no P131 ancestor that is a country, and without
  # OPTIONAL they would drop out of the result instead of returning with
  # country = NA.
  resolve_batch <- function(qids) {
    values_clause <- paste(paste0("wd:", qids), collapse = " ")
    q <- sprintf('
SELECT ?item ?countryLabel WHERE {
  VALUES ?item { %s }
  OPTIONAL {
    ?item wdt:P131* ?countryCandidate .
    ?countryCandidate wdt:P31 wd:Q6256 .
    BIND(?countryCandidate AS ?country)
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}', values_clause)
    res <- tryCatch(query_wikidata(q), error = function(e) tibble())
    # An empty or malformed result (no "item" column) must not reach
    # distinct() / transmute() below: return a zero-row tibble of the
    # right shape instead, so map_dfr can still bind it with real batches.
    if (!"item" %in% names(res)) {
      return(tibble(item = character(), countryLabel = character()))
    }
    res
  }

  qid_batches <- split(qids_to_resolve, ceiling(seq_along(qids_to_resolve) / 20))
  nesting_results <- map_dfr(seq_along(qid_batches), function(i) {
    cat("  batch", i, "of", length(qid_batches), "...\n")
    resolve_batch(qid_batches[[i]])
  })

  if (nrow(nesting_results) == 0) {
    cat("No nesting results returned at all. Check network access and the query directly.\n")
    nesting_lookup <- tibble(wikidata_qid = character(), country = character())
  } else {
    nesting_lookup <- nesting_results %>%
      distinct(item, .keep_all = TRUE) %>%
      transmute(wikidata_qid = str_extract(item, "Q[0-9]+$"),
                country = normalize_country(countryLabel))
  }
  cat("Resolved country for", sum(!is.na(nesting_lookup$country)), "/",
      length(qids_to_resolve), "matched places\n")

  nesting_lookup <- nesting_lookup %>%
    mutate(super_region = lookup_super_region(country))

  n_no_super_region <- sum(!is.na(nesting_lookup$country) & is.na(nesting_lookup$super_region))
  if (n_no_super_region > 0) {
    cat(n_no_super_region, "resolved countries have no super_region mapping yet.",
        "Add them to super_region_map:\n")
    nesting_lookup %>% filter(!is.na(country) & is.na(super_region)) %>%
      distinct(country) %>% print()
  }

  nested <- lookup %>% left_join(nesting_lookup, by = "wikidata_qid")
  write_csv(nested, region_lookup_path)
  cat("Written", region_lookup_path, "with country, super_region columns:", nrow(nested), "rows\n")
  invisible(nested)
}

# ---------------------------------------------------------------------------
# 7. Command line
# ---------------------------------------------------------------------------

region_matching_cli <- function(args) {
  usage <- paste(
    "Usage: Rscript analysis/region_matching/match_regions.R <stage>",
    "  probe    coverage check on the 40 commonest region tokens",
    "  match    match every token to a Wikidata item; writes region_lookup.csv beside this script",
    "  nesting  resolve each matched item to country and super-region; rewrites the file",
    "  all      match, then nesting",
    "All stages need outbound network access.",
    sep = "\n")
  stage <- if (length(args) >= 1) args[[1]] else ""
  if (!stage %in% c("probe", "match", "nesting", "all")) {
    cat(usage, "\n")
    return(invisible(NULL))
  }
  region_tokens <- read_region_tokens(read.csv(here("data", "references.csv"), na.strings = ""))
  if (stage == "probe") probe_regions(region_tokens)
  if (stage %in% c("match", "all")) match_regions(region_tokens)
  if (stage %in% c("nesting", "all")) resolve_nesting()
}

# Runs only when the file is executed with Rscript, not when it is
# source()d from a Quarto chunk.
if (sys.nframe() == 0L) region_matching_cli(commandArgs(trailingOnly = TRUE))