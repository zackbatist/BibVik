"""
label_clusters.py — LLM labeling, as a distinct, downstream step
structurally separate from the embedding/clustering step (mechanical
structure first, interpretive label applied to its output after, never
the reverse).

Every cluster's representative titles go into a single prompt, one
call, so the model can see all clusters at once and avoid assigning
the same or a near-identical label to two different clusters — a
per-cluster call, blind to every other cluster's titles or label, has
no way to prevent that, since it never sees what any other cluster
was named. Real duplicate-label collisions were the trigger for this
design.

The prompt does not mention the annotation codebook, its categories,
or any example labels drawn from it, in any form — this is the
concrete mechanism by which "independent of the codebook" is enforced.

The prompt also states the corpus's shared umbrella theme (Viking Age,
medieval Scandinavia) directly and instructs the model that a label
true of nearly every cluster fails and must be replaced with something
naming what's actually distinctive about that specific cluster — the
direct fix for labels defaulting to the always-true, uninformative
broad description.

The prompt also permits an optional secondary label/description per
cluster, for the case (confirmed real, found on this corpus's own top
clusters) where a cluster's titles genuinely split into two
substantial themes rather than one dominant theme plus a few stray
outliers — forcing a single label onto that kind of cluster either
drops one whole theme silently or produces a vague label that names
neither theme clearly. Most clusters are expected to use only the
primary label; the secondary fields are empty unless the model judges
a real second theme is present.

Optional dual-model agreement check (--second-model): runs the exact
same batch call twice, once per model, and compares each cluster's
label between the two runs. This is the one verification mechanism
from earlier development that had real evidence behind it — in an
earlier per-cluster design, two models disagreeing on a cluster
correctly flagged a case where one model had fixated on a single
unusual title and produced a confidently wrong label. That mechanism
was dropped at the time only because per-cluster calls couldn't
prevent duplicate labels across clusters, a different problem this
batch design already solves — bringing the disagreement check back on
top of the batch design keeps the duplicate-label fix while restoring
the one check with actual proof behind it. Off by default (one call
instead of two, half the cost) since it hasn't been tested against
this specific design yet.

No stability check across repeated runs of the SAME model, no human
review step: this is a single automated pass per model, by design —
no manual verification of any label happens here even with
--second-model, which flags disagreement but does not resolve which
model is right.

Input:  representatives_csv (from select_representatives.py)
        partition_csv (node_id, community_id — the same file passed to
        select_representatives.py, needed here to expand cluster-level
        labels back out to every member node)
Output: results/cluster_labels.csv — one row per cluster the call
        returned a label for: community_id, label, description,
        secondary_label, secondary_description (the last two are empty
        strings for clusters that did not need a second label),
        unaccounted_titles (titles, pipe-separated, the model judged
        don't connect to either theme — empty string if none; forces
        the model to explicitly account for every title instead of
        silently dropping ones that don't fit the dominant paper's
        framing, a real failure mode confirmed on this corpus where a
        hub-dominated cluster's description covered only 6 of 9
        non-hub titles and silently omitted the rest), groups_json
        (the model's full sort-before-labeling breakdown),
        titles_missing_from_groups (mechanically-verified sorting gaps,
        distinct from unaccounted_titles — see below), secondary_group_size
        (the actual title count behind secondary_label — always
        derived from groups by rank, not from the model's own
        secondary_label/secondary_description text, which was found
        unreliable: a real 25-cluster run showed the model sometimes
        formed a valid 3+ title group in groups but never wrote it into
        secondary_label, or named a group as secondary_label that had
        fewer than 3 titles. secondary_group_size should always be
        either empty, meaning fewer than 2 groups existed, or the true
        size of whichever group is actually being reported)
        results/node_labels.csv — every member node of a labeled
        cluster, expanded via the partition CSV: node_id, community_id,
        label (primary label only — see the note in main() for why the
        secondary label isn't assigned per node)
        results/label_agreement.csv (only with --second-model) — one
        row per cluster: community_id, label_model1, label_model2,
        agree (a rough embedding-free string/keyword-overlap heuristic
        — see check_agreement() docstring for what "agree" actually
        checks and its limits)

Usage:
    ollama serve   # if not already running
    python label_clusters.py results/representatives.csv \
        ../gn_analysis/results/multi_cut/communities_round_10207.csv \
        results/ --model qwen3.5:35b

    # with the dual-model disagreement check:
    python label_clusters.py results/representatives.csv \
        ../gn_analysis/results/multi_cut/communities_round_10207.csv \
        results/ --model qwen3.5:35b --second-model qwen3:8b
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

LABEL_PROMPT = """You are analyzing {n_clusters} clusters of academic paper titles from a citation graph. Each cluster's titles were grouped together by an automated clustering method based on semantic similarity, not by topic labels or any predefined taxonomy. All clusters below are from the same corpus, which is entirely about Viking Age and medieval Scandinavia.

## Clusters

{clusters_block}

## Task

Write a label and short description for EVERY cluster listed above. You can see all {n_clusters} clusters at once — use that. A label like "Viking Age Archaeology" or "Norse Studies" is true of nearly every cluster here and communicates nothing about any one of them; do not assign the same or a near-identical label to two different clusters. If you notice two clusters are genuinely about to get the same label, that is a signal to look harder at what actually distinguishes them — a specific method, material, site, region, time-slice, or theoretical angle — and sharpen both labels around that difference before finalizing either one.

For each cluster, work out its dominant axis first (the one specific thing that actually organizes its titles), then write a label built from that axis, not from the shared corpus-wide period or field. Titles in a real cluster usually share one such axis even when their surface topics vary.

For each cluster, before writing anything, first sort its titles into groups by what they're actually about — do this explicitly, not silently in your head. Do not start from the highest-degree title (the one listed first, with the highest citation count within its cluster) and build outward from its framing; that produces a label that restates one paper's title instead of synthesizing the cluster. Instead, read every title in the cluster once, then decide how many distinct groups the titles actually form: most clusters will turn out to be one real group with at most one or two genuine outliers, but do not assume that going in — some clusters genuinely split into two or three substantial groups of comparable size, and the sorting step is what reveals that, not an assumption made in advance. A title with a high citation count is informative (it tells you what the cluster's internal structure is organized around) but it does not get to define the theme by itself — the theme is defined by which group has the most titles, checked after sorting, not by which title has the highest degree.

Once sorted: the largest group's shared topic is the primary label and description. A second group becomes secondary_label/secondary_description ONLY if it has at least 3 titles — a group of 1 or 2 titles is not "several," it does not get promoted to a secondary theme, and it goes in unaccounted_titles instead, even if those 1-2 titles share a clear, nameable topic with each other. This is a hard numeric floor, not a judgment call: a two-title group that looks like a real theme is still too thin to report as secondary_label — note it in unaccounted_titles, where a reader can still see what those titles are about, without your description implying it's a substantial part of the cluster. If a title doesn't fit any group of 3 or more, it goes in unaccounted_titles. Do not describe an unaccounted title inside the primary or secondary description as a passing mention ("...and also touches on X") — either it belongs to a group of 3+ (primary or secondary) or it belongs in unaccounted_titles; there is no third, softer category.

Before finalizing a cluster's entry, count the titles across every group in "groups" and add the count of any single-title groups you folded into unaccounted_titles. That total must equal the number of titles given for that cluster. If it doesn't, you have dropped a title somewhere — go back through the cluster's title list one more time, find the one you missed, and add it to whichever group actually fits it (or its own group, if nothing fits). Do this count silently before responding; do not report the count itself, just make sure it is correct before the JSON is finalized.

Respond with one JSON object per line — NOT a single JSON object wrapping every cluster, and NOT a JSON array. Each line is a complete, independent, self-contained JSON object for exactly one cluster, including that cluster's own ID:
{{"community_id": "<cluster_id, as given in the input, as a string>", "groups": [{{"theme": "<short phrase for what this group of titles shares>", "titles": ["<exact title text of every title sorted into this group>"]}}, ...], "label": "<a short phrase, 2-6 words, built from the LARGEST group in \"groups\">", "description": "<1-3 sentences naming the axis and explaining how the largest group's titles relate to it>", "secondary_label": "<optional — a second short phrase, ONLY if a second group in \"groups\" is substantial (several titles); otherwise empty string>", "secondary_description": "<optional — 1-3 sentences for the second group; otherwise empty string>", "unaccounted_titles": ["<exact title text of every title that ended up in its own single-member group — these should match the titles NOT covered by \"label\" or \"secondary_label\"; empty list only if every title sorted into the primary or secondary group>"]}}

Each line must be valid, complete JSON on its own — a reader should be able to parse line 1 successfully even if every line after it were missing. Do not add a trailing comma, a wrapping array bracket, or any separator between lines other than a single newline. Write one complete line per cluster, finish that line fully before starting the next, and do not begin a new cluster's line until the previous one is completely finished and valid.

Some titles contain a double-quote character as part of the title itself (for example, a title that quotes another work's name, or is itself a quoted phrase within a longer citation). When copying such a title into a JSON string, escape every internal double-quote as \\" so the string stays syntactically valid — never copy a title's internal quote mark as a bare " inside a JSON string, since that breaks the string and everything after it in that line. This applies wherever exact title text is reproduced: in "titles" arrays, in "unaccounted_titles", and anywhere a title is quoted directly in "description" or "secondary_description".

The "groups" field is not optional scratch work — it is where the actual sorting happens, and label/description/secondary_label/secondary_description/unaccounted_titles must be a direct, checkable summary of what "groups" contains, not an independent judgment made separately from it. Every title given for a cluster must appear in exactly one group inside "groups".

Output one line per cluster ID given above, in any order. Respond ONLY with the JSON lines, one per cluster. No preamble, no markdown fences, no surrounding array or object, no blank lines between entries."""


def load_representatives(path: Path) -> dict[str, list[str]]:
    titles_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            titles_by_cluster[row["community_id"]].append(row["title"])
    return titles_by_cluster


def load_partition(path: Path) -> dict[str, list[str]]:
    """community_id -> [node_id, ...], for expanding cluster-level labels
    back out to every member node (not just the representative subset)."""
    members_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            members_by_cluster[row["community_id"]].append(row["node_id"])
    return members_by_cluster


def query_ollama(
    base_url: str, model: str, prompt: str, temperature: float,
    timeout: int, num_ctx: int, num_predict: int,
) -> str | None:
    # Streaming, not a single blocking response: non-streamed
    # (stream=False) responses were found to truncate at the same
    # relative point across five separate attempts at 50 clusters,
    # regardless of how high --num-predict, --num-ctx, or --timeout
    # were raised — evidence the cap was on Ollama's non-streamed
    # response handling itself, not on any of those settings.
    # Streaming reads the response incrementally as it's generated,
    # with no dependency on a single-response size limit.
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    try:
        resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout, stream=True)
    except requests.RequestException as e:
        print(f"request failed: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"Ollama returned status {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return None

    chunks = []
    saw_done = False
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunks.append(obj.get("response", ""))
            if obj.get("done"):
                saw_done = True
                break
    except requests.RequestException as e:
        print(f"stream interrupted after {sum(len(c) for c in chunks)} chars: {e}", file=sys.stderr)
        # Still return what was received — a stream cut short by a
        # network hiccup partway through is a different, cheaper
        # failure to recover from than a full blocking timeout that
        # discards everything generated up to that point.

    if not saw_done:
        # The stream ended (iter_lines() stopped yielding) without
        # Ollama ever sending done:true, and without requests raising
        # any exception either. This is a real, distinct failure mode:
        # confirmed to happen silently — no error, no timeout, no
        # non-200 status — when a large-context local generation dies
        # partway through (process killed under memory pressure, Ollama
        # itself crashing, connection reset without a clean FIN). The
        # response text this returns may look complete enough to almost
        # parse as JSON but is actually truncated mid-generation.
        print(f"WARNING: stream ended without a done:true signal after "
              f"{sum(len(c) for c in chunks)} chars received. This usually means "
              f"Ollama's process died or was killed mid-generation (check "
              f"memory pressure / Activity Monitor, and the ollama serve "
              f"terminal for a crash message), not that num_predict was "
              f"reached normally.", file=sys.stderr)

    text = "".join(chunks)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    return text or None


def attempt_unescaped_quote_repair(line: str) -> dict | None:
    """Attempts to fix the specific, confirmed failure where a title's
    own literal double-quote character was copied into a JSON string
    without escaping it (e.g. a title itself formatted as `"Some
    Paper". I: Some Book`), which breaks the string boundary at that
    exact character and makes the rest of the line fail to parse even
    though the response was otherwise complete and correct.

    Strategy: walks the line character by character rebuilding a
    properly-escaped version, treating every double-quote as a literal
    character to escape UNLESS it is one of the small set of quote
    characters that legitimately delimit a JSON key or string boundary
    (immediately preceded by `{{`, `[`, `,`, `:`, or whitespace after
    those, or immediately followed by `:`, `,`, `}}`, `]`, or
    whitespace before those). This heuristic is not perfect and can
    misfire on unusual formatting, but the repaired result is always
    checked by confirming it actually parses as valid JSON before
    being trusted; if it still doesn't parse, this returns None and
    the caller's normal "line failed" handling applies, same as before
    this function existed. Confirmed on the real failing case that
    motivated this: successfully repairs it."""
    result_chars = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch != '"':
            result_chars.append(ch)
            i += 1
            continue

        prev_nonspace = None
        for j in range(len(result_chars) - 1, -1, -1):
            if not result_chars[j].isspace():
                prev_nonspace = result_chars[j]
                break
        next_nonspace = None
        for j in range(i + 1, n):
            if not line[j].isspace():
                next_nonspace = line[j]
                break

        is_boundary = (
            prev_nonspace in (None, "{", "[", ",", ":")
            or next_nonspace in (None, ":", ",", "}", "]")
        )
        if is_boundary:
            result_chars.append(ch)
        else:
            result_chars.append('\\"')
        i += 1

    candidate = "".join(result_chars)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def parse_batch_json(raw: str, expected_ids: set[str]) -> dict[str, dict] | None:
    """Parse newline-delimited JSON (JSONL): one complete, independent
    JSON object per line, each carrying its own community_id, groups,
    label, description, secondary_label, secondary_description, and
    unaccounted_titles.

    This replaced an earlier single-object format
    ({{cluster_id: {{...}}, ...}}) after five consecutive attempts at
    50 clusters in one call all failed the same way: Ollama's own
    server logs confirmed generation completed cleanly (200, no
    truncation flag) but the model had stopped partway through the
    single giant JSON object, leaving it syntactically invalid as a
    whole — so json.loads() on the complete response failed and EVERY
    cluster in the batch was lost, including ones the model had
    already finished writing correctly. JSONL fixes this structurally:
    each line is parsed independently, so a premature stop loses only
    the one incomplete line at the end, not every cluster that came
    before it in the same response.

    groups is the model's explicit sort of every title into thematic
    groups, done BEFORE writing label/description — this is the actual
    mechanism (not the after-the-fact unaccounted_titles check alone)
    meant to stop a high-citation-count title from dominating the
    label by default: the model has to decide which group is largest
    only after sorting everything, not build outward from whichever
    title it read first. secondary_label/secondary_description are
    optional per cluster — most clusters will have them empty.
    unaccounted_titles is a list (possibly empty) of titles that ended
    up alone in their own group — this makes "did the description
    cover every title" checkable programmatically (see main()'s
    coverage-warning output).

    Returns only entries that are well-formed and whose ID was
    actually asked for; logs any expected ID the model dropped or
    whose line failed to parse. Returns None only if literally zero
    lines parsed into a usable entry — any partial success returns
    whatever did parse, which is the whole point of the format change."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    result = {}
    n_lines_seen = 0
    n_lines_failed = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        n_lines_seen += 1
        # A line may itself be truncated mid-object (the exact failure
        # this format is meant to make recoverable, not eliminate) —
        # skip just that one line, not the whole response. It may also
        # fail for a different, confirmed reason: a title containing
        # its own literal double-quote character (real case found:
        # `"Ett svenskt praktfynd..."`.) reproduced by the model
        # without escaping it, breaking the JSON string boundary
        # despite the response being otherwise complete and correct.
        # attempt_unescaped_quote_repair() targets that specific,
        # confirmed pattern before this line is given up on.
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            repaired = attempt_unescaped_quote_repair(line)
            if repaired is not None:
                entry = repaired
            else:
                n_lines_failed += 1
                continue
        if not isinstance(entry, dict):
            n_lines_failed += 1
            continue
        cid = entry.get("community_id")
        if cid is None:
            n_lines_failed += 1
            continue
        cid = str(cid)
        if cid not in expected_ids:
            continue
        if "label" not in entry or "description" not in entry:
            n_lines_failed += 1
            continue
        unaccounted = entry.get("unaccounted_titles")
        if not isinstance(unaccounted, list):
            unaccounted = []
        groups = entry.get("groups")
        if not isinstance(groups, list):
            groups = []
        result[cid] = {
            "label": entry["label"],
            "description": entry["description"],
            "secondary_label": entry.get("secondary_label") or "",
            "secondary_description": entry.get("secondary_description") or "",
            "unaccounted_titles": [str(t) for t in unaccounted],
            "groups": groups,
        }

    if n_lines_failed:
        print(f"{n_lines_failed} of {n_lines_seen} response line(s) failed to parse "
              f"(likely the response's final line, cut off mid-generation) — those "
              f"clusters are missing from this batch's result, everything else parsed "
              f"normally.", file=sys.stderr)

    if not result:
        return None

    missing = expected_ids - set(result.keys())
    if missing:
        print(f"response missing {len(missing)} expected cluster(s): {sorted(missing)}",
              file=sys.stderr)
    return result


def label_one_batch(
    cluster_ids: list[str], titles_by_cluster: dict[str, list[str]],
    base_url: str, model: str, temperature: float, timeout: int,
    num_ctx: int, num_predict: int, batch_label: str = "",
) -> dict[str, dict] | None:
    """Runs the LLM call and all per-cluster post-processing (sorting-gap
    check, secondary_label derivation by rank) for one batch of cluster
    IDs. Extracted from what was previously inline in main() so batching
    (see main()'s --batch-size) can call this once per sub-batch and
    merge results, instead of one call needing to hold every cluster's
    structure at once.

    Real motivation, not a hypothetical: five consecutive attempts at
    labeling 50 clusters in a single call all failed at nearly the same
    point (~18,000 output tokens, partway through the second cluster's
    entry) regardless of --num-predict, --num-ctx, or which machine ran
    it (confirmed on both a local M2 and a cluster GPU with 24GB
    dedicated VRAM). Checking the serving container's own logs directly
    confirmed generation completed cleanly and Ollama returned a normal
    200 response — the model itself decided its answer was finished at
    that point, producing malformed/incomplete JSON, not a resource
    limit being hit. This is a model-reliability-at-task-length problem,
    not a hardware problem, and no combination of the above flags fixed
    it. 25-cluster batches completed successfully and repeatedly
    throughout the same testing, which is why that's the default here.

    Returns None on total failure (call or parse failure) for this
    specific batch — the caller decides whether to abort the whole run
    or skip this batch and continue with the rest."""
    print(f"Labeling {len(cluster_ids)} cluster(s){batch_label} ({model}, "
          f"T={temperature})...", file=sys.stderr)

    clusters_block_parts = []
    for cid in cluster_ids:
        titles_block = "\n".join(f"  - {t}" for t in titles_by_cluster[cid])
        clusters_block_parts.append(f"### Cluster {cid}\n{titles_block}")
    clusters_block = "\n\n".join(clusters_block_parts)
    prompt = LABEL_PROMPT.format(n_clusters=len(cluster_ids), clusters_block=clusters_block)

    raw = query_ollama(base_url, model, prompt, temperature, timeout, num_ctx, num_predict)
    if raw is None:
        print(f"Call failed for batch{batch_label} — skipping.", file=sys.stderr)
        return None

    print(f"Raw response: {len(raw)} chars (~{len(raw) // 4} tokens) for "
          f"{len(cluster_ids)} cluster(s){batch_label}, "
          f"~{(len(raw) // 4) // max(len(cluster_ids), 1)} tokens/cluster.", file=sys.stderr)

    parsed = parse_batch_json(raw, expected_ids=set(cluster_ids))
    if parsed is None:
        print(f"Response was not valid JSON for batch{batch_label} — skipping. "
              f"Raw response follows:\n" + raw[:2000], file=sys.stderr)
        return None

    sorting_gaps = {}
    for cid in cluster_ids:
        if cid not in parsed:
            continue
        given_titles = set(titles_by_cluster[cid])
        grouped_titles = set()
        for group in parsed[cid]["groups"]:
            if isinstance(group, dict) and isinstance(group.get("titles"), list):
                grouped_titles.update(str(t) for t in group["titles"])
        missing_from_groups = given_titles - grouped_titles
        if missing_from_groups:
            sorting_gaps[cid] = missing_from_groups
            parsed[cid]["sorting_gap_titles"] = missing_from_groups

    secondary_group_sizes = {}
    for cid in cluster_ids:
        if cid not in parsed:
            continue
        entry = parsed[cid]
        groups = entry["groups"]
        sorted_groups = sorted(
            (g for g in groups if isinstance(g, dict)),
            key=lambda g: len(g.get("titles", [])),
            reverse=True,
        )
        original_unaccounted = set(entry["unaccounted_titles"])

        if len(sorted_groups) < 2:
            entry["secondary_label"] = ""
            entry["secondary_description"] = ""
            secondary_group_sizes[cid] = 0
            continue

        second = sorted_groups[1]
        second_titles = [str(t) for t in second.get("titles", [])]
        secondary_group_sizes[cid] = len(second_titles)

        if len(second_titles) >= 3:
            theme = str(second.get("theme", "")).strip()
            entry["secondary_label"] = theme
            entry["secondary_description"] = (
                f"A second group of {len(second_titles)} titles shares this theme."
            )
            entry["unaccounted_titles"] = [
                t for t in entry["unaccounted_titles"] if t not in second_titles
            ]
        else:
            entry["secondary_label"] = ""
            entry["secondary_description"] = ""
            merged = list(original_unaccounted) + [
                t for t in second_titles if t not in original_unaccounted
            ]
            entry["unaccounted_titles"] = merged

    for cid in parsed:
        parsed[cid]["secondary_group_size"] = secondary_group_sizes.get(cid, "")
        parsed[cid].setdefault("sorting_gap_titles", set())

    return parsed


def check_duplicate_labels(labels_by_cluster: dict[str, str]) -> dict[str, list[str]]:
    """Batching (see main()'s --batch-size) reintroduces a real risk
    the single-call design existed specifically to prevent: a batch has
    no visibility into any other batch's labels, so nothing stops two
    clusters in different batches from independently landing on the
    same or a near-identical label. This is a cheap, final, separate
    check across ALL batches' finished labels — not another LLM call
    (that would need a whole second pass and its own reliability
    testing this pipeline hasn't done); instead, a direct application
    of the existing check_agreement() word-overlap heuristic between
    every pair of labels. Same limits apply as documented there (false
    positives on real synonyms, no semantic understanding) — flags
    something worth a manual look, not a proven duplicate.

    Returns {label: [other cluster_ids with a flagged-similar label]}
    for every label involved in at least one flagged pair — empty dict
    if nothing was flagged."""
    flagged: dict[str, list[str]] = {}
    items = list(labels_by_cluster.items())
    for i in range(len(items)):
        cid_a, label_a = items[i]
        if not label_a:
            continue
        for j in range(i + 1, len(items)):
            cid_b, label_b = items[j]
            if not label_b:
                continue
            if check_agreement(label_a, label_b):
                flagged.setdefault(cid_a, []).append(cid_b)
                flagged.setdefault(cid_b, []).append(cid_a)
    return flagged


def check_agreement(label1: str, label2: str) -> bool:
    """Rough, embedding-free agreement check between two labels for the
    same cluster: lowercase, strip punctuation, split into words, drop
    short stopwords, and check whether the two label's word sets
    overlap by at least half of the smaller label's word count. This is
    a coarse heuristic, not a semantic comparison — "Viking Age
    Migration" and "Norse Population Movement" mean the same thing but
    share zero words and would be marked disagreeing. It will produce
    false disagreements on real synonyms and, in principle, could also
    produce false agreement on two labels that share surface words but
    differ in what they actually claim. Treat a flagged disagreement as
    "worth a look," not as proof the labels actually differ in meaning
    — and treat the absence of a flag as "the two labels share enough
    words to look similar," not as confirmation they're both correct.
    A proper fix would re-use the sentence-embedding model already in
    this pipeline (embed_titles.py) to compare labels by meaning rather
    than word overlap; not done here to avoid adding sentence-
    transformers as a dependency of label_clusters.py specifically,
    which currently only needs requests."""
    stopwords = {"the", "and", "of", "in", "a", "an", "to", "for", "on", "at", "from"}

    def words(label: str) -> set[str]:
        cleaned = re.sub(r"[^\w\s]", "", label.lower())
        return {w for w in cleaned.split() if w not in stopwords and len(w) > 2}

    w1, w2 = words(label1), words(label2)
    if not w1 or not w2:
        return w1 == w2
    overlap = len(w1 & w2)
    smaller = min(len(w1), len(w2))
    return overlap >= (smaller / 2)


def label_batch_with_retry(
    cluster_ids: list[str], titles_by_cluster: dict[str, list[str]],
    base_url: str, model: str, temperature: float, timeout: int,
    num_ctx: int, num_predict: int, batch_label: str,
) -> dict[str, dict] | None:
    """Runs one call for this batch (via label_one_batch). Since the
    response format is JSONL (see parse_batch_json), a single call can
    return a PARTIAL result — some clusters parsed successfully,
    others didn't (typically the last one or two, cut off mid-
    generation). Whatever succeeded is kept as-is.

    For clusters still missing after that one call, this does NOT
    retry the same-size batch again — retrying an unchanged request
    and hoping temperature-driven randomness produces a different
    outcome was tried and considered too weak a mechanism to justify
    keeping: it risks reproducing the exact same failure for no real
    reason to expect otherwise. Instead, missing clusters go straight
    to splitting the remaining set in half and calling this function
    again on each half (recursively, so a half that still fails splits
    again) — this is the part with real justification: if a batch is
    failing because of its own size/complexity, a genuinely smaller
    task is a different, easier request, not a repeat of the same one.
    A single cluster that still fails at that point is accepted as
    failed; the caller (main()) already handles partial failure by
    excluding only the specific clusters that never succeeded, not
    aborting the whole run.

    Returns the merged dict of every cluster that succeeded across
    this call and any splits, or None only if nothing in this batch
    (down to individual clusters) ever succeeded."""
    result = label_one_batch(
        cluster_ids, titles_by_cluster, base_url, model, temperature,
        timeout, num_ctx, num_predict, batch_label,
    )
    merged: dict[str, dict] = dict(result) if result else {}
    remaining = [cid for cid in cluster_ids if cid not in merged]

    if not remaining:
        return merged

    if len(remaining) <= 1:
        print(f"Cluster{batch_label} ({remaining[0]}) failed and cannot be split further — "
              f"giving up on it.", file=sys.stderr)
        return merged or None

    mid = len(remaining) // 2
    left, right = remaining[:mid], remaining[mid:]
    print(f"{len(remaining)} of {len(cluster_ids)} cluster(s) in batch{batch_label} missing "
          f"after one call — splitting into two smaller batches of {len(left)} and "
          f"{len(right)} and calling each separately, since a smaller batch is a genuinely "
          f"easier task, not a repeat of the same request.", file=sys.stderr)

    left_result = label_batch_with_retry(
        left, titles_by_cluster, base_url, model, temperature, timeout,
        num_ctx, num_predict, f"{batch_label} [split-left]",
    )
    right_result = label_batch_with_retry(
        right, titles_by_cluster, base_url, model, temperature, timeout,
        num_ctx, num_predict, f"{batch_label} [split-right]",
    )

    if left_result:

        merged.update(left_result)
    if right_result:
        merged.update(right_result)
    return merged or None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("representatives_csv", type=Path)
    parser.add_argument("partition_csv", type=Path,
                         help="node_id,community_id CSV — same file passed "
                              "to select_representatives.py")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3.5:35b")
    parser.add_argument("--batch-size", type=int, default=10,
                         help="Clusters per LLM call (default: 10). "
                              "Five consecutive attempts at 50 clusters in "
                              "one call all failed at ~18,000 output tokens "
                              "regardless of --num-predict/--num-ctx, on two "
                              "different machines — confirmed (via the "
                              "serving container's own logs) to be the "
                              "model ending its own generation early, not a "
                              "resource limit. 25-cluster batches completed "
                              "reliably in earlier testing, but a later "
                              "25-cluster run failed on its very first "
                              "attempt too — evidence 25 is itself sometimes "
                              "too large, not just an edge case only visible "
                              "at 50+. Lowered to 10 (divides evenly into "
                              "common totals like 50) as the actual fix (a "
                              "smaller task the model can more reliably "
                              "sustain to completion), not left to the "
                              "automatic split-on-failure logic to discover "
                              "reactively after a failed attempt. More "
                              "clusters than this run as multiple sequential "
                              "calls, merged afterward — see label_one_batch() "
                              "and the duplicate-label check this "
                              "reintroduces, run automatically at the end "
                              "regardless of batch count.")
    parser.add_argument("--second-model", default=None,
                         help="If set, runs the exact same batch call(s) a "
                              "second time with this model and writes "
                              "results/label_agreement.csv flagging clusters "
                              "where the two models' labels disagree (see "
                              "check_agreement() for what 'disagree' means "
                              "and its limits). Doubles the cost of the run. "
                              "The primary output (cluster_labels.csv, "
                              "node_labels.csv) still comes from --model "
                              "only — the second model's run is a check, "
                              "not an alternative source of the accepted "
                              "labels.")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--timeout", type=int, default=1800,
                         help="Call timeout in seconds (default: 1800 = 30 min).")
    parser.add_argument("--num-ctx", type=int, default=32768,
                         help="Context window in tokens, covering prompt + "
                              "response together (default: 32768). Raise this "
                              "if labeling more clusters than fit comfortably "
                              "— Ollama's server default (much smaller) "
                              "silently truncates on overflow rather than "
                              "erroring.")
    parser.add_argument("--num-predict", type=int, default=16384,
                         help="Max response tokens (default: 16384) — "
                              "needs to fit a label, description, and the "
                              "full \"groups\" breakdown (which repeats "
                              "every title's exact text) for every cluster "
                              "in the request. Raised from the earlier "
                              "default of 8192 once the prompt started "
                              "requiring the groups field, which roughly "
                              "doubles output size per cluster. Confirmed NOT "
                              "to be the limiting factor for large batches; "
                              "see --batch-size docstring.")
    parser.add_argument("--only-clusters", default=None,
                         help="Comma-separated cluster IDs to label, e.g. "
                              "'110,22' — labels only these, ignoring every "
                              "other cluster in representatives_csv. Built "
                              "for the real case of a full run completing "
                              "with a small number of clusters lost to "
                              "truncation (JSONL means this is now rare, "
                              "usually the last line or two of a batch): "
                              "instead of rerunning all 50 to fill in 2, run "
                              "just the missing ones. Existing rows in "
                              "cluster_labels.csv / node_labels.csv are "
                              "preserved and merged with — the output is not "
                              "overwritten wholesale, only the requested "
                              "cluster IDs are added or replaced.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    titles_by_cluster = load_representatives(args.representatives_csv)
    members_by_cluster = load_partition(args.partition_csv)
    cluster_ids = list(titles_by_cluster.keys())

    if args.only_clusters:
        requested = [c.strip() for c in args.only_clusters.split(",") if c.strip()]
        missing_from_input = [c for c in requested if c not in titles_by_cluster]
        if missing_from_input:
            print(f"--only-clusters requested {missing_from_input} but "
                  f"representatives_csv has no rows for them — check the ID(s) "
                  f"are correct.", file=sys.stderr)
        cluster_ids = [c for c in requested if c in titles_by_cluster]
        if not cluster_ids:
            print("None of the requested --only-clusters IDs were found in "
                  "representatives_csv — nothing to do.", file=sys.stderr)
            sys.exit(1)

    print(f"{len(cluster_ids)} clusters to label"
          f"{' (--only-clusters filter applied)' if args.only_clusters else ''}.",
          file=sys.stderr)

    batches = [
        cluster_ids[i:i + args.batch_size]
        for i in range(0, len(cluster_ids), args.batch_size)
    ]
    if len(batches) > 1:
        print(f"Split into {len(batches)} batch(es) of up to {args.batch_size} "
              f"cluster(s) each — see --batch-size for why.", file=sys.stderr)

    parsed: dict[str, dict] = {}
    failed_batches: list[list[str]] = []
    for batch_num, batch_ids in enumerate(batches, start=1):
        batch_label = f" (batch {batch_num}/{len(batches)})" if len(batches) > 1 else ""
        batch_result = label_batch_with_retry(
            batch_ids, titles_by_cluster, args.base_url, args.model,
            args.temperature, args.timeout, args.num_ctx, args.num_predict,
            batch_label,
        )
        if batch_result is None:
            failed_batches.append(batch_ids)
            continue
        parsed.update(batch_result)

    if not parsed:
        print("Every batch failed — nothing written.", file=sys.stderr)
        sys.exit(1)
    if failed_batches:
        failed_ids = [cid for batch in failed_batches for cid in batch]
        print(f"{len(failed_ids)} cluster(s) across {len(failed_batches)} "
              f"failed batch(es) have no label and are excluded from output: "
              f"{failed_ids}", file=sys.stderr)

    sorting_gaps = {cid: entry["sorting_gap_titles"] for cid, entry in parsed.items()
                     if entry.get("sorting_gap_titles")}
    secondary_group_sizes = {cid: entry["secondary_group_size"] for cid, entry in parsed.items()}

    # Merge with whatever already exists in cluster_labels.csv, rather
    # than overwriting it wholesale — the real case this exists for:
    # --only-clusters is used to fill in a small number of clusters
    # that were missing from a previous full run, and that previous
    # run's 48 (or however many) good rows must survive this one.
    cluster_labels_path = args.output_dir / "cluster_labels.csv"
    existing_rows: dict[str, dict] = {}
    if cluster_labels_path.exists():
        with open(cluster_labels_path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                existing_rows[row["community_id"]] = row
        print(f"Merging with {len(existing_rows)} existing row(s) already in "
              f"{cluster_labels_path}.", file=sys.stderr)

    new_rows = {}
    for cid in cluster_ids:
        if cid in parsed:
            new_rows[cid] = {
                "community_id": cid,
                "label": parsed[cid]["label"],
                "description": parsed[cid]["description"],
                "secondary_label": parsed[cid]["secondary_label"],
                "secondary_description": parsed[cid]["secondary_description"],
                "unaccounted_titles": " | ".join(parsed[cid]["unaccounted_titles"]),
                "groups_json": json.dumps(parsed[cid]["groups"], ensure_ascii=False),
                "titles_missing_from_groups": " | ".join(sorted(sorting_gaps.get(cid, []))),
                "secondary_group_size": secondary_group_sizes.get(cid, ""),
            }

    # New rows for a cluster overwrite an existing row for the same
    # cluster (a targeted rerun is meant to replace, not duplicate,
    # a previously-failed or previously-run entry); existing rows for
    # clusters not touched this run are kept as-is.
    all_rows = {**existing_rows, **new_rows}
    # Preserve a stable, sensible order: original representatives_csv
    # order for clusters that were in this run's input, then any
    # existing clusters (e.g. from the original full run) not
    # requested this time, in their prior order.
    ordered_ids = list(dict.fromkeys(list(cluster_ids) + list(existing_rows.keys())))

    with open(cluster_labels_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["community_id", "label", "description",
                           "secondary_label", "secondary_description",
                           "unaccounted_titles", "groups_json",
                           "titles_missing_from_groups", "secondary_group_size"],
        )
        writer.writeheader()
        for cid in ordered_ids:
            if cid in all_rows:
                writer.writerow(all_rows[cid])

    # node_labels.csv carries only the primary label per node — a node
    # is a member of one cluster, and the cluster's primary label is its
    # main identity; the secondary theme (when present) describes a
    # sub-pattern within the cluster, not a separate assignment for
    # individual nodes, since this pipeline has no way to tell which
    # specific member nodes belong to which half of a dual-labeled
    # cluster (that would need per-title, not per-cluster, information
    # the model was never asked to provide).
    #
    # Built from all_rows (the merged label set), not just parsed (this
    # run's new labels only) — same reason as cluster_labels.csv above:
    # a --only-clusters rerun must not drop node expansions for
    # clusters labeled in a previous run and not touched this time.
    node_labels_path = args.output_dir / "node_labels.csv"
    node_rows = []
    for cid, row in all_rows.items():
        for node_id in members_by_cluster.get(cid, []):
            node_rows.append({"node_id": node_id, "community_id": cid, "label": row["label"]})
    with open(node_labels_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["node_id", "community_id", "label"])
        writer.writeheader()
        writer.writerows(node_rows)

    n_dual = sum(1 for entry in parsed.values() if entry["secondary_label"])
    n_with_unaccounted = sum(1 for entry in parsed.values() if entry["unaccounted_titles"])
    print(f"Wrote {len(parsed)} of {len(cluster_ids)} cluster labels to {cluster_labels_path} "
          f"and expanded to {len(node_rows)} node(s) in {node_labels_path}.")
    if n_dual:
        print(f"{n_dual} cluster(s) received a secondary label (genuinely split into "
              f"two themes): "
              + ", ".join(cid for cid, entry in parsed.items() if entry["secondary_label"]))
    if n_with_unaccounted:
        print(f"{n_with_unaccounted} cluster(s) have titles the model judged don't fit "
              f"either theme (see unaccounted_titles column) — these are titles the model "
              f"explicitly flagged as not covered, not a guarantee every other title is "
              f"genuinely well-covered too: "
              + ", ".join(cid for cid, entry in parsed.items() if entry["unaccounted_titles"]))
    if sorting_gaps:
        print(f"{len(sorting_gaps)} cluster(s) have a title that was given to the model but "
              f"never appears in ANY group in its \"groups\" output — a real sorting gap, "
              f"distinct from unaccounted_titles (that field only reflects what the model "
              f"claims it checked; this is a mechanical check of what it actually did): "
              + "; ".join(f"{cid}: {sorted(titles)}" for cid, titles in sorting_gaps.items()))

    n_with_secondary = sum(1 for entry in parsed.values() if entry["secondary_label"])
    if n_with_secondary:
        print(f"{n_with_secondary} cluster(s) got a secondary_label: "
              + ", ".join(cid for cid, entry in parsed.items() if entry["secondary_label"])
              + f". secondary_label/secondary_description are now always derived directly "
                f"from \"groups\" by rank (the largest non-primary group, if it has 3+ "
                f"titles) — not from whatever text the model itself wrote into those two "
                f"fields, which was found unreliable on real data (the model sometimes "
                f"formed a valid group in \"groups\" but never used it as secondary_label, "
                f"or named a group as secondary_label that didn't meet the 3-title floor). "
                f"Check secondary_group_size for the actual title count behind each one.")

    if len(batches) > 1:
        dup_flags = check_duplicate_labels(
            {cid: entry["label"] for cid, entry in parsed.items()}
        )
        if dup_flags:
            reported = set()
            pairs = []
            for cid, others in dup_flags.items():
                for other in others:
                    pair = tuple(sorted((cid, other)))
                    if pair not in reported:
                        reported.add(pair)
                        pairs.append(pair)
            print(f"{len(dup_flags)} cluster(s) across different batches got labels flagged "
                  f"as similar by a word-overlap check (same heuristic as check_agreement(), "
                  f"same limits — false positives on real synonyms, no semantic "
                  f"understanding; worth a manual look, not proof of an actual duplicate). "
                  f"This check exists because batching removes the single-call design's "
                  f"built-in protection against two different batches independently giving "
                  f"unrelated clusters the same generic label: "
                  + "; ".join(f"{a} ({parsed[a]['label']!r}) vs {b} ({parsed[b]['label']!r})"
                               for a, b in pairs))
        else:
            print("No cross-batch label similarity flagged by the duplicate-label check.")

    if args.second_model:
        print(f"Running second-model check ({args.second_model})...", file=sys.stderr)
        parsed2: dict[str, dict] = {}
        for batch_num, batch_ids in enumerate(batches, start=1):
            batch_label = f" (batch {batch_num}/{len(batches)})" if len(batches) > 1 else ""
            batch_result2 = label_one_batch(
                batch_ids, titles_by_cluster, args.base_url, args.second_model,
                args.temperature, args.timeout, args.num_ctx, args.num_predict,
                batch_label,
            )
            if batch_result2 is not None:
                parsed2.update(batch_result2)

        if not parsed2:
            print("Second-model run produced nothing — no agreement check written.",
                  file=sys.stderr)
        else:
            agreement_path = args.output_dir / "label_agreement.csv"
            n_disagree = 0
            with open(agreement_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["community_id", f"label_{args.model}",
                                   f"label_{args.second_model}", "agree"],
                )
                writer.writeheader()
                for cid in cluster_ids:
                    label1 = parsed.get(cid, {}).get("label", "")
                    label2 = parsed2.get(cid, {}).get("label", "")
                    agree = check_agreement(label1, label2) if label1 and label2 else False
                    if not agree:
                        n_disagree += 1
                    writer.writerow({
                        "community_id": cid,
                        f"label_{args.model}": label1,
                        f"label_{args.second_model}": label2,
                        "agree": agree,
                    })
            print(f"Wrote agreement check to {agreement_path}. "
                  f"{n_disagree} of {len(cluster_ids)} cluster(s) flagged as "
                  f"disagreeing between {args.model} and {args.second_model} "
                  f"— worth a manual look at those specifically before trusting "
                  f"their accepted label (from --model) at face value.")


if __name__ == "__main__":
    main()