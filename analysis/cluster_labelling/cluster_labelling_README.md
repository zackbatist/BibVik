# Cluster labelling: standalone

Girvan-Newman groups papers into clusters based on citation structure: which papers cite each other, not based on what they're about. That leaves every cluster needing a human-readable name before anyone can actually use it: "Community 101" tells you nothing; "Viking Age Diaspora and Migration" does. This pipeline generates that name automatically, using a local LLM to read each cluster's most representative titles and write a short label and description.

It has no dependency on the annotation codebook (the F1 topic/method/source coding from `01_annotation.qmd`); labels come entirely from the citation graph and paper titles, so they stand as an independent description of what a cluster actually contains. Comparing these labels against the codebook is a separate, later step (`03_joined_descriptions.qmd`), not something this pipeline does itself.

Not part of the Quarto render chain, same convention as `gn_analysis/`: running an LLM over every cluster is slow and this whole pipeline lives outside the document's render chain for that reason.

Independent of any specific partition. Takes any `node_id,community_id` CSV as input: Girvan-Newman's `communities.csv`, or Louvain/Leiden membership exported from `02_network_structure.qmd`. Nothing here cares which method produced the grouping being described.

## Rationale

Girvan-Newman groups papers by citation. A cluster is a group of papers closely connected in the citation graph, papers that cite each other or are cited by the same others, not a group defined by subject or topic. Because that connection is structural, a cluster can be made up of papers about entirely different things, and there's nothing in the grouping itself that tells a reader what unites them. Someone still has to work out what each cluster is about.

A language model was chosen for this task because, given the papers most central to a cluster, it can sort them into groups by shared subject matter and produce a specific written label and description grounded in that grouping. A systematic process gives every cluster the same fixed steps, so results are consistent, mistakes are traceable, and the whole thing can be checked and fixed at once.

The design addresses each of these limitations directly rather than assuming they will not arise. The model is shown only the papers most central to a cluster by citation, not whatever it might infer independently from the titles. It is required to record its grouping of a cluster's papers before producing a label, so the label follows from that grouping rather than from an initial impression. And any claim it makes about its own grouping is subsequently checked against the grouping itself, rather than accepted as given.

This check applies uniformly across clusters, regardless of whether a label appears self-evidently correct, since the model's apparent confidence is not evidence that its output is grounded.

Some clusters will not fit the process cleanly regardless of how well it performs. A single paper's disproportionate influence can obscure the rest of a cluster. A genuine secondary theme may be too minor to warrant separate mention. A cluster may simply resist reduction to a single throughline. The process is intended to surface these cases rather than conceal them behind a label more confident than the evidence supports.

None of this guarantees that the resulting labels are correct. What it guarantees is that each label results from an accountable process: that it reflects what was actually done with the evidence provided, and that where a cluster proved genuinely difficult to characterize, that difficulty is visible rather than obscured by an assured-sounding description.

## How it works

Two scripts: `select_representatives.py` picks which papers to show the LLM, `label_clusters.py` turns them into a label.

**`select_representatives.py`**

1. For each cluster, build the subgraph induced by just that cluster's own members (an edge only counts if both endpoints are in the cluster).
2. Rank papers by degree within that subgraph: how many other papers in the *same* cluster cite them or are cited by them.
3. Keep the top `--top-n` (default 10).
4. If the top-ranked paper's degree is at least `--hub-ratio-threshold` (default 5.0) times the second-ranked paper's, fill the remaining slots preferring papers with no direct citation edge to that top paper, falling back to its satellites only once independent papers run out.

**`label_clusters.py`**

1. Send every cluster's selected titles into one LLM call (not one call per cluster).
2. The prompt states the corpus's own shared baseline directly and instructs the model that a label reducible to it has failed.
3. The model sorts every title it was given into named groups, before writing anything.
4. The label and description come from whichever group is largest.
5. A second group becomes `secondary_label` only if it has at least 3 titles; anything smaller goes into `unaccounted_titles`.
6. `titles_missing_from_groups` compares every title actually given to the model against everything in its output, and flags any title absent from every group.
7. `secondary_group_size` recomputes each secondary label's actual title count and flags any below the 3-title floor.

## Outputs and outcomes

**`results/representatives.csv`**

- Produced by: `select_representatives.py`
- Read by: `label_clusters.py`

| Column | Description |
|---|---|
| `community_id` | Which cluster this row belongs to. |
| `name` | Citekey of the paper. |
| `title` | The paper's title, as shown to the labeling LLM. |
| `rank` | Position within this cluster's selected sample, 1 = highest within-cluster degree (or, for a hub-dominated cluster, first among the independence-preferred picks). |
| `within_cluster_degree` | How many other papers in this same cluster cite this paper or are cited by it; the actual ranking metric. |
| `cluster_size` | Total member count of the cluster, for context on how small a slice `--top-n` selected. |

**`results/node_labels.csv`**

- Produced by: `label_clusters.py`.
- Read by: no other file or script right now.

| Column | Description |
|---|---|
| `node_id` | Citekey of an individual paper; every member of a labeled cluster gets a row, not just the sampled titles above. |
| `community_id` | Which cluster this paper belongs to. |
| `label` | That cluster's primary label (secondary labels aren't assigned per paper; see "How it works"). |

**`results/cluster_labels.csv`**

- Produced by: `label_clusters.py`.
- Read by: `02_network_structure.qmd`.

`label_clusters.py` builds this from `representatives.csv`. The graph and its tooltips show a real label when one exists, falling back to "Community N" otherwise.

| Column | Description |
|---|---|
| `community_id` | Which cluster this row is for. |
| `label` | The short name generated for the cluster's largest title-group. |
| `description` | 1-3 sentences explaining that label. |
| `secondary_label` | A second label, only present if a second title-group had at least 3 titles; empty string otherwise. |
| `secondary_description` | Matching description for the secondary theme; empty if there isn't one. |
| `unaccounted_titles` | Titles the model placed in their own single-member group rather than folding into either theme; pipe-separated. |
| `groups_json` | The model's complete sort-before-labeling output, as JSON: every group it formed and which titles went into each. |
| `titles_missing_from_groups` | Titles that were given to the model but never appear anywhere in `groups_json`; a real sorting failure, confirmed by comparing input to output directly, not by trusting what the model reported. Empty when nothing was missed. |
| `secondary_group_size` | The actual number of titles in the group `secondary_label` was drawn from, recomputed independently from `groups_json`. Should always read ≥3; a lower number means the 3-title floor was violated. |

## Usage

`select_representatives.py` needs `data/bibliography.json` (titles) and `data/citation_edgelist.csv` (the citation graph). The partition file is found automatically (the fragmentation-onset cut under `../gn_analysis/results/multi_cut/`, same logic `02_network_structure.qmd` uses for `gn_earliest`) unless `--partition-csv` is given explicitly.

```bash
cd analysis/cluster_labelling
python3 -m venv .venv && source .venv/bin/activate
pip install requests   # sentence-transformers not needed for this step

python select_representatives.py \
    ../../data/bibliography.json \
    ../../data/citation_edgelist.csv \
    results/ \
    --top-n 10 --max-clusters 25

ollama serve   # in a separate terminal, if not already running

python label_clusters.py \
    results/representatives.csv \
    ../gn_analysis/results/multi_cut/communities_round_<N>.csv \
    results/ \
    --model qwen3.5:35b
```

`select_representatives.py` finds the partition file itself:
auto-detects the smallest-round `communities_round_<N>.csv` under
`../gn_analysis/results/multi_cut/`, the same fragmentation-onset cut
`02_network_structure.qmd` uses as `gn_earliest`. Pass
`--partition-csv <path>` to use a different one, or if that directory
doesn't exist yet (run `gn_analysis/` first). `label_clusters.py`
still takes the partition CSV as an explicit argument (to expand
labels out to every member node); check the terminal output from
`select_representatives.py` for which file it picked and pass the same
one to `label_clusters.py`.

`--max-clusters` scopes to the N largest clusters (by member count),
for a faster first pass before running against the full set.

Model name is whatever is actually pulled locally (`ollama list`); double-check the exact tag exists before running; published model
names don't always match what's shown in general documentation
(`qwen3:35b` does not exist as a tag on this system, `qwen3.5:35b`
does, a different model family, not a typo).

## Status

Confirmed by hand, checking real output against real titles, not a systematic audit:

- Hub-independence rule changed a real cluster's sample and its label on a real 25-cluster run. At 50 clusters, 44 of 50 (88%) were hub-dominated by the `--hub-ratio-threshold` measure: the fix is active on most of what gets labeled at this scale, not a rare edge case.
- Sort-first `groups` fixed every gap it was tested against: a missed hub paper, a dropped theory title, a buried secondary theme.
- The 3-title floor on `secondary_label` was added after 45% of secondary labels on one 25-cluster run were backed by only 1-2 titles. A written prompt instruction alone did not fix it (re-run: 19 of 25 clusters still got a secondary label, 5 with only 2-title support). Code-level enforcement did: `secondary_label`/`secondary_description` are now derived directly from `groups` by rank rather than trusted from the model's own text, discarding the model's own writing entirely. Verified against a real 25-cluster run: every one of the 12 clusters that got a secondary label had `secondary_group_size` ≥3, every one of the 13 that didn't had <3, a full, exact match with no exceptions.
- The earlier, separate embedding-based method (see "Earlier method" below) was audited once: roughly 14 of 25 clusters fully accurate, 6 with a silently-dropped outlier, 4 with a missed second theme, 2 genuinely incoherent.
- A recurring sorting gap (see "Sort-first labeling" above) has now missed the same transliterated-Russian title across multiple separate runs, with the self-count instruction active every time. Worth noting: every recurring miss (this title; "Nordnorske spillsaker" in cluster 87 on one run) sits in a multilingual title list; the same cluster is also hub-dominated, Danish/Swedish/Russian/English titles mixed together, several with garbled OCR-style text. This looks less like a sorting-logic gap and more like a model comprehension limit on non-English, non-Scandinavian-script titles specifically, though not investigated further.

Not yet done:

- `--second-model` (a second full LLM run for cross-model label
  disagreement checking, built earlier) has not been re-tested against
  the sort-first version. Current assessment: likely lower priority
  than before; the `groups` field now gives a much more specific,
  directly-readable way to check a label's reasoning than comparing
  two final label strings via a coarse word-overlap heuristic
  (`check_agreement()`). Not removed, but not part of the default
  workflow going forward.
- Only tested at 5, 25, and 50 clusters so far, never the full ~315
  qualifying / ~800 total corpus. One call for the full corpus is
  unlikely to work at all; batching/reconciliation (tried once
  earlier, inconclusively, and removed) would need to be rebuilt for
  that scale. In practice, full corpus coverage may not be needed:
  `02_network_structure.qmd` only individually visualizes the top
  `gn_top_n` (currently 20) communities by size; everything else
  renders as grey "Other" regardless of whether it has a label.
- `--top-n` (10) and `--hub-ratio-threshold` (5.0) both match
  reasonable defaults (the paper's own top-N; a plausible dominance
  cutoff) but neither has been independently tuned against
  alternatives on this corpus.
- No systematic accuracy check across more than a handful of clusters,
  every verification described above was done by hand, cluster by
  cluster, checking real titles against real output. The accuracy
  figures elsewhere in this file (14/25 accurate, etc.) describe the
  earlier embedding-based method's output, not this one.

---

## Earlier method (superseded)

Before the citation-ranking method above, this pipeline used sentence embeddings: `embed_titles.py` embedded every title, `select_representatives.py` picked titles by embedding distance to the cluster centroid (or by farthest-point sampling for spread, `--sampling diverse`/`centroid`), and `label_clusters.py` used a custom single-pass batch prompt (no groups-based sorting, no secondary-label floor). Multiple additional mechanisms were tried on top of that method and abandoned after direct testing: per-cluster mode with multi-model stability checks, hierarchical/reconciliation for scaling past one call, self-critique, few-shot examples, per-title outlier flagging, and automated cluster-splitting detection. Each is documented with its actual test result in "What was tried, tested, and removed" below; kept because a future pass on this project could otherwise reinvent one of these and hit the same wall.

A manual audit of that method's output (25 largest clusters, before dual-label support existed) found roughly 14 of 25 clusters fully accurate, ~6 with one silently-ignored outlier title, ~4 with a real second theme the label dropped, and 2 genuinely incoherent grab-bag clusters. One of those incoherent cases was traced to a real structural cause, not a labeling failure: two topically unrelated titles (`grosjean2007`, on Holocene glacier archaeology in the Swiss Alps, and `piotrowski2006`, on Holocaust memory studies) turned out to share no direct citation link at all, but both cited the same third paper: a multi-author interdisciplinary piece on "The Archaeology of Ice" (`solli2011`). Girvan-Newman had correctly grouped them by real citation-graph proximity; they simply aren't about the same thing. No amount of prompt tuning fixes a cluster whose coherence comes from one shared bridging citation rather than a shared topic; the "within-cluster betweenness centrality" idea (surfacing which paper in a cluster is structurally bridging otherwise-unrelated content, so the model can name that explicitly) was proposed as a way to handle this but was never built. This finding is structural, not tied to which selection method is used, and likely still applies to the current citation-ranking method too; not yet checked.

## What was tried, tested, and removed

This pipeline went through several more elaborate designs before
settling on its current shape. Recorded here with actual specifics,
not just "several things were tried," since a future pass on this
project could easily reinvent one of these and hit the same wall.

The raw label output from every test referenced below is also
preserved as individual CSVs in `results/test_runs/`
(reconstructed from what was pasted into the development conversation;
the actual run outputs only ever existed on the machine that
generated them). The full label-by-label comparison across every test
is below, so the claims in this section can be checked directly
against what each test actually produced, not just taken on faith.

### Label comparison across all reconstructed tests

Rows ordered by test coverage — clusters tested across the most
runs sit at the top. Empty cell = cluster not included in that
test's scope (T0's 3 unstable exclusions — 22, 49, 292 — are also
empty for T0 specifically, even though 49/292 appear in later tests).

| Cluster | Tests covered | T0 Original per-cluster | T2 Critique | T3 Hierarchical | T4 Diverse | T5 Diverse+Outlier | T6 25-cluster | T7 Dual v1 (empty) | T8 Dual v2 (fixed) |
|---|---|---|---|---|---|---|---|---|---|
| 14 | 8 | Climate Forcing and Reconstructions | Millennial Climate Forcing and Reconstruction | Millennial Climate Forcing and Reconstruction | Millennial Paleoclimatic Reconstructions | Millennial Paleoclimatic Reconstructions | Millennial Paleoclimatic Reconstructions | Millennial Paleoclimatic Reconstructions | Millennial Paleoclimatic Reconstructions |
| 67 | 8 | Icelandic Climate History | Icelandic Environmental and Climatic History | Icelandic Environmental History and Climate-Society | Environmental History and Human-Environment Interaction | Environmental History and Human-Environment Interaction | Environmental Humanities and Settlement History | North Atlantic Settlement and Environment | North Atlantic Environmental History |
| 86 | 8 | Archaeological Methodologies and Practices | Archaeological Excavation Methodologies | Excavation Methodologies and Stratigraphic Recording | Archaeological Methods and Material Analysis | Archaeological Methods and Material Analysis | Urban Archaeology and Material Analysis | Archaeological Methods and Material Analysis | Archaeological Methodology and Soil Science |
| 101 | 8 | Viking Age Expansion and Society | Viking Expansion and Settlement Patterns | Viking Expansion and Settlement Dynamics | Viking Age Migration and Diaspora Studies | Viking Age Migration and Settlement Patterns | Viking Age Diaspora and Migration | Viking Age Migration and Diaspora | Viking Age Bioarchaeology and Migration |
| 171 | 8 | Archaeogenetics and Migration Debates | Archaeogenetics and Migration Histories | Archaeogenetics and Migration Debates | Genetic Genealogy and Bioarchaeological Ethics | Ancient DNA and Population Genetics | Ancient DNA and Population Genetics | Ancient DNA and Population Genetics | Ancient DNA and Population Genetics |
| 11 | 4 | Sami and Norse Archaeological Studies | — | — | — | — | Scandinavian Prehistoric Technology and Ritual | Norwegian Prehistory and Sami Studies | Norwegian Prehistoric Archaeology and Sami Studies |
| 39 | 4 | Germanic Antiquity and Early Medieval Scandinavia | — | — | — | — | Early Medieval Kingship and Trade | Early Medieval Political Centers and Kingship | Early Medieval Political Geography and Trade |
| 43 | 4 | Nordic Mythology and Iconography | — | — | — | — | Old Norse Mythology and Literature | Old Norse Mythology and Literature | Old Norse Mythology and Iconography |
| 8 | 3 | Viking Age Archaeology and Culture | — | — | — | — | Proto-Urban Centers and Craft Production | Viking Age Towns and Craft Production | — |
| 9 | 3 | Pacific Island Archaeology and History | — | — | — | — | Maritime Trade and Regional Archaeology | Maritime Trade and Regional Archaeology | — |
| 49 | 3 | — | — | — | — | — | Childhood, Gender, and Social Identity | Childhood, Gender, and Social Roles | Childhood, Gender, and Social Roles |
| 52 | 3 | Viking Age Archaeology and Culture | — | — | — | — | Heroic Poetry and Royal Power | Viking Age Art and Symbolism | — |
| 68 | 3 | Norse Greenland Adaptation | — | — | — | — | Environmental Adaptation and Risk | Environmental Archaeology and Subsistence | — |
| 87 | 3 | Prehistoric and Early Historic Archaeology | — | — | — | — | Archaeological Chemistry and Material Science | Archaeological Chemistry and Material Science | — |
| 110 | 3 | Genomic and Historical Insights into Marine Species | — | — | — | — | Ancient Genomics and Bioinformatics | Ancient Genomics and Bioinformatics | — |
| 146 | 3 | Automated Remote Sensing in Archaeology | — | — | — | — | Remote Sensing and Landscape Modeling | Remote Sensing and Landscape Archaeology | — |
| 149 | 3 | Medieval Archaeology and History | — | — | — | — | Viking Age Urban Development | Urban Archaeology and Town Development | — |
| 151 | 3 | Viking Age and Medieval North Atlantic Settlements | — | — | — | — | Island Environments and Resilience | Climate Change and Societal Resilience | — |
| 172 | 3 | Medieval Settlement and Farm Structures | — | — | — | — | Excavation Reports and Site Stratigraphy | Excavation Reports and Settlement Archaeology | — |
| 216 | 3 | Viking Age Archaeology | — | — | — | — | Insular Art and Metalwork | Insular Metalwork and Ornamentation | — |
| 277 | 3 | Cultural Evolution and Environmental Change | — | — | — | — | Niche Construction and Human Adaptation | Human Adaptation and Niche Construction | — |
| 282 | 3 | Prehistoric Migration and Identity | — | — | — | — | Migration Theory and Network Analysis | Prehistoric Migrations and Cultural Transformations | — |
| 287 | 3 | Runestone and Norse Studies | — | — | — | — | Runes, Writing, and Literacy | Runes, Writing, and Literacy | — |
| 292 | 3 | — | — | — | — | — | Archaeological Theory and Ethics | Theory and Philosophy of Science | Theory of Science and Materiality |
| 222 | 2 | — | — | — | — | — | Stable Isotope Analysis in Bioarchaeology | Stable Isotope Analysis in Bioarchaeology | — |

Notice cluster 171 shifting from "migration histories/debates"
(centroid sampling, T0/T2/T3) to explicitly naming ethics/identity
content once diverse sampling was introduced (T4 onward) — this is
the concrete evidence behind "diverse sampling surfaces real content
centroid sampling was hiding," not just an assertion. Also notice T0's
row for clusters 8, 52, and 216 (not shown in the truncated view above
unless scrolled to) — all three converged on "Viking Age Archaeology
and Culture" or near-identical phrasing under the original per-cluster
mode, the concrete duplicate-label problem that motivated the move to
batch mode.

### Individual test tables (full label + description text)

Each test's complete output, for the clusters it actually covered.
Descriptions included in full — these are the primary evidence
behind every claim made in the narrative above.

#### T0 Original per-cluster (`results/test_runs/test0_ORIGINAL_percluster_25clusters_22accepted.csv`)

| community_id | label | description | n_runs_agreeing |
|---|---|---|---|
| 101 | Viking Age Expansion and Society | This cluster unites titles focused on the causes, mechanisms, and consequences of Viking expansion across Northern Europe and the British Isles. The collection broadly covers themes of migration, settlement patterns, economic activities, and the transformation of social and political structures during the Viking Age. | 8 |
| 14 | Climate Forcing and Reconstructions | The titles focus on understanding climate responses to various natural forcings such as volcanic eruptions, solar variability, and greenhouse gases. They also emphasize the reconstruction of past climate conditions using paleoclimate data and models, highlighting the interplay between external forcings and climate variability over long timescales. | 8 |
| 67 | Icelandic Climate History | The titles focus on the historical climate of Iceland, spanning various periods and integrating environmental, social, and documentary evidence. They explore climatic changes, their impacts on society, and the use of historical records to reconstruct past climates. | 8 |
| 86 | Archaeological Methodologies and Practices | The titles collectively reflect a focus on various methodologies, practices, and theoretical approaches within archaeology. They emphasize techniques for recording, analyzing, and interpreting archaeological data, including excavation, stratigraphy, and digital methods, highlighting the integration of theory and practice in the field. | 8 |
| 171 | Archaeogenetics and Migration Debates | These titles collectively explore the intersection of ancient DNA analysis and archaeological theory regarding human migration in Europe. They highlight a tension between genetic evidence and traditional typological thinking, while critically examining the role of nationalism and colonialism in interpreting these historical narratives. | 8 |
| 11 | Sami and Norse Archaeological Studies | The titles focus on archaeological research related to Sami culture and Norse history, exploring themes such as cultural identity, historical contexts, and material remains. They highlight the intersection of archaeology with social, political, and cultural narratives, particularly in northern Scandinavia. | 8 |
| 39 | Germanic Antiquity and Early Medieval Scandinavia | This cluster unites scholarly works focusing on the political structures, settlement histories, and cultural identities of early medieval Scandinavia and Germanic peoples. The titles collectively explore themes of rulership, kingship, and social formation through diverse lenses including archaeology, linguistics, literature, and historical analysis. | 8 |
| 43 | Nordic Mythology and Iconography | This cluster unites scholarly works focused on the visual representation and textual analysis of Norse mythology, heroic legends, and religious iconography in Scandinavia. The titles collectively examine artifacts such as gold bracteates, picture stones, and helmets to explore the transition from pagan to Christian cultures between the Migration Period and the Middle Ages. | 8 |
| 277 | Cultural Evolution and Environmental Change | The titles explore the intersection of cultural evolution, historical ecology, and environmental change, emphasizing how cultural practices and historical knowledge shape human adaptation to ecological challenges. They highlight the role of cultural niche construction, historical perspectives, and interdisciplinary approaches in understanding long-term social-ecological dynamics. | 8 |
| 287 | Runestone and Norse Studies | The titles collectively focus on runestones, Norse culture, and historical linguistics, exploring themes such as runic inscriptions, Viking-era artifacts, and the transition to Christianity. They span various academic disciplines including history, philology, and archaeology, reflecting a broad interest in Scandinavian antiquity and its cultural transformations. | 8 |
| 172 | Medieval Settlement and Farm Structures | This cluster unites studies focusing on the archaeology and history of rural settlements, farms, and domestic architecture across Northern Europe and Iceland. The titles collectively examine specific excavation sites, building types like stoves and byres, and the evolution of habitation from the Viking Age through the Middle Ages. | 8 |
| 149 | Medieval Archaeology and History | The titles focus on archaeological findings and historical studies from the medieval period, including urban development, monastic sites, and regional history. They reflect a broad interest in the material culture and historical evolution of Denmark and its surrounding areas during the Viking and medieval eras. | 8 |
| 9 | Pacific Island Archaeology and History | The titles collectively focus on the archaeology, history, and cultural practices of Pacific Island societies, spanning pre-European contact to colonial periods. They explore themes such as maritime cultures, ceremonial structures, and the intersection of history with anthropology and ethnohistory. | 8 |
| 52 | Viking Age Archaeology and Culture | This cluster unites titles focused on the material culture, rituals, and historical narratives of the Viking Age across Scandinavia. The works collectively examine specific archaeological sites, such as ship burials and settlement remains, alongside literary traditions and runic inscriptions to explore power structures and identity in the region. | 8 |
| 110 | Genomic and Historical Insights into Marine Species | The titles collectively reflect a focus on genomic studies of marine species, particularly Atlantic cod, and historical analyses using ancient DNA. They explore genetic diversity, ecological adaptation, and population dynamics, often integrating archaeological and historical data to understand species' past and present distributions. | 8 |
| 282 | Prehistoric Migration and Identity | The titles explore themes of prehistoric migration, cultural exchange, and the formation of social identities through archaeological and genetic perspectives. They examine how movement and interaction shaped human societies, often intersecting with questions of ancestry, cultural continuity, and the material expression of identity. | 8 |
| 8 | Viking Age Archaeology and Culture | The titles collectively focus on the Viking Age, exploring archaeological findings, cultural exchanges, and societal structures from various regions. They highlight interdisciplinary approaches, including material culture, settlement patterns, and historical interpretations, reflecting a broad interest in the period's archaeological and cultural legacy. | 8 |
| 216 | Viking Age Archaeology | The titles collectively reflect archaeological studies from the Viking Age, focusing on various sites and artifacts such as settlements, trade centers, and craftsmanship. They highlight findings related to daily life, religious objects, and trade routes, showcasing a broad range of archaeological interests within this historical period. | 8 |
| 68 | Norse Greenland Adaptation | The titles explore various aspects of Norse adaptation to the environment in Greenland, including agricultural practices, climate impacts, and interactions with local populations. They highlight interdisciplinary approaches such as isotopic analysis, palynology, and genetic studies to understand the complexities of Norse survival and transformation in the region. | 8 |
| 146 | Automated Remote Sensing in Archaeology | This cluster unites titles focused on applying advanced remote sensing technologies, particularly LiDAR and aerial imagery, to archaeological prospection. The common thread is the shift toward automated and machine learning-driven methods for detecting, extracting, and analyzing landscape features and monuments. | 8 |
| 151 | Viking Age and Medieval North Atlantic Settlements | The titles explore archaeological and historical aspects of Viking and medieval settlements in the North Atlantic, focusing on regions like Iceland, the Faroe Islands, and Northern Norway. They highlight themes such as farming practices, shielings, and environmental interactions during these periods. | 8 |
| 87 | Prehistoric and Early Historic Archaeology | The titles focus on archaeological studies from prehistoric and early historic periods, covering various regions in Scandinavia and Northern Europe. They explore topics such as burial practices, material culture, and settlement patterns, often integrating archaeological findings with environmental and historical contexts. | 8 |

#### T2 Critique (`results/test_runs/test2_batch_critique_centroid.csv`)

| community_id | label | description | revised |
|---|---|---|---|
| 101 | Viking Expansion and Settlement Patterns | This cluster organizes titles around the specific historical processes of Viking Age diaspora, including migration triggers, settlement societies in regions like England and Scotland, and the political structures of chieftains and kings during the expansion era. | False |
| 14 | Millennial Climate Forcing and Reconstruction | The dominant axis here is the scientific reconstruction of past climate variability over the last millennium, specifically focusing on the mechanisms of natural forcing such as volcanic eruptions and solar variability rather than human impacts. | False |
| 67 | Icelandic Environmental and Climatic History | Unlike the broader climate forcing studies, this cluster is strictly geographically focused on Iceland, examining the specific interplay between local climatic changes, sea-ice records, and the socio-economic history of the island from the settlement period to the 18th century. | False |
| 86 | Archaeological Excavation Methodologies | These titles share a focus on the technical and theoretical frameworks of archaeological practice, including stratigraphy, site recording, microstratigraphy, and the management of residuality and spatial data during fieldwork. | False |
| 171 | Archaeogenetics and Migration Histories | This cluster is defined by the application of genetic data, specifically ancient DNA, to resolve questions regarding population movements, identity, and migration patterns across Neolithic and European history. | False |

#### T3 Hierarchical (`results/test_runs/test3_hierarchical_centroid.csv`)

| community_id | label | description | revised |
|---|---|---|---|
| 101 | Viking Expansion and Settlement Dynamics | This cluster is organized around the processes, causes, and consequences of Viking Age diaspora, focusing on the transition from raiding to settlement across Northern Europe and the British Isles. The titles collectively examine the triggers for expansion, the establishment of trading centers, and the resulting social and political transformations in regions like Scotland, England, and Norway. | False |
| 14 | Millennial Climate Forcing and Reconstruction | The dominant axis here is the technical reconstruction of past climate variability and the analysis of natural forcing mechanisms like volcanic eruptions and solar activity over the last millennium. Titles focus on the methodologies for separating forced climate signals from chaotic variability and the application of these reconstructions in paleoclimate modeling simulations. | False |
| 67 | Icelandic Environmental History and Climate-Society | This cluster is specifically anchored in the environmental history of Iceland, linking climatic fluctuations to socio-economic transformations and human adaptation from the settlement period through the late eighteenth century. The titles integrate tephrochronology, glacier records, and documentary evidence to explore the coupled social-ecological systems unique to the Icelandic context. | False |
| 86 | Excavation Methodologies and Stratigraphic Recording | This cluster is organized around the technical frameworks, theoretical debates, and practical procedures for archaeological excavation and data recording. The titles collectively address specific systems like single-context and microstratigraphy, alongside digital delineation, geoarchaeological approaches, and the reflexive nature of fieldwork rather than specific artifacts or historical periods. | False |
| 171 | Archaeogenetics and Migration Debates | This cluster is defined by the intersection of ancient DNA analysis and the theoretical discourse on population movements in European prehistory. The titles examine the application of genetic data to reconstruct migration histories, the persistence of typological thinking, and the critical reconciliation of genomic findings with archaeological material cultures and identity narratives. | False |

#### T4 Diverse (`results/test_runs/test4_batch_diverse.csv`)

| community_id | label | description |
|---|---|---|
| 101 | Viking Age Migration and Diaspora Studies | This cluster focuses on the movement of people, specifically the Viking diaspora, migration patterns, and the sociopolitical mechanisms of chiefdoms. It combines theoretical frameworks on migration causation with specific archaeological evidence of mobility, such as isotopic analysis of teeth and the study of portable ornaments like brooches and arm-rings. |
| 14 | Millennial Paleoclimatic Reconstructions | The titles here are dedicated to the technical reconstruction of past climate conditions over the last millennium using proxy data like tree rings, corals, and ice cores. The cluster emphasizes statistical methods, model validation, and the detection of specific climate phenomena such as ENSO teleconnections and solar activity variations. |
| 67 | Environmental History and Human-Environment Interaction | This cluster bridges environmental data with historical and archaeological records to examine how climate variability and extreme events shaped human societies. It covers specific regional case studies, such as seal exploitation in the Baltic and drought impacts in California and Patagonia, alongside theoretical discussions on the Anthropocene and environmental humanities. |
| 86 | Archaeological Methods and Material Analysis | The unifying axis is the technical methodology of archaeological investigation, ranging from field recording and soil profile description to laboratory techniques like Micro-XRF and diatom analysis. It includes studies on specific material cultures, such as ironworks and horse gear, and theoretical approaches to urbanization and residuality in the archaeological record. |
| 171 | Genetic Genealogy and Bioarchaeological Ethics | This cluster centers on the application of ancient DNA (aDNA) and population genetics to reconstruct past demographics and migration, while critically engaging with the ethics, marketing, and social implications of these technologies. It includes debates on the construction of identity, the demonization of historical groups, and the methodological challenges of using genetic markers as population proxies. |

#### T5 Diverse+Outlier (`results/test_runs/test5_batch_diverse_outlier.csv`)

| community_id | label | description |
|---|---|---|
| 101 | Viking Age Migration and Settlement Patterns | This cluster organizes around the movement of people and the establishment of settlements during the Viking Age, covering diaspora, colonization sites like L'Anse aux Meadows, and the political economy of chiefdoms. While the majority of titles focus on migration and settlement dynamics, the title regarding lead and strontium analysis of teeth appears thematically distinct and likely does not belong to this specific axis. |
| 14 | Millennial Paleoclimatic Reconstructions | The dominant axis here is the reconstruction of past climate conditions over the last millennium using proxy data such as tree rings, corals, and ice cores. The cluster focuses on statistical methods and specific climatic events like droughts and ENSO teleconnections, though the title on wavelets and the one on erroneous model fields appear to be methodological outliers that do not fit the specific paleoclimatic reconstruction theme. |
| 67 | Environmental History and Human-Environment Interaction | This cluster explores the reciprocal relationship between human societies and their environments, including specific case studies on seal exploitation, agricultural productivity, and volcanic impacts on settlement. The titles focus on how climate and natural resources shaped daily life and historical events, while the flagged titles on ethnicity theory and the Anthropocene definition seem to be theoretical abstractions that do not align with the empirical environmental history focus of the group. |
| 86 | Archaeological Methods and Material Analysis | The unifying theme is the technical application of archaeological science and fieldwork methods, ranging from soil profile description and micro-XRF analysis to the study of specific artifacts like ironworks and horse gear. Although the cluster includes titles on theoretical approaches to urbanization and residuality, the flagged title on diatoms and the one on 'Le geste et la parole' appear to be methodological or linguistic outliers that do not fit the core focus on material analysis and field techniques. |
| 171 | Ancient DNA and Population Genetics | This cluster is defined by the use of genetic data to reconstruct population histories, migration routes, and biological traits in prehistoric and medieval Europe. The titles discuss aDNA research, kinship, and the intersection of genetics with social identity, while the flagged titles on the 'maritime mode of production' and the 'manufacture of knowledge' appear to be theoretical or sociological works that do not fit the specific genetic analysis theme. |

#### T6 25-cluster (`results/test_runs/test6_batch_diverse_25clusters.csv`)

| community_id | label | description |
|---|---|---|
| 101 | Viking Age Diaspora and Migration | This cluster focuses on the movement of people and the formation of diasporas during the Viking Age, utilizing methods like strontium analysis and lead isotope studies to trace origins and settlement patterns. |
| 14 | Millennial Paleoclimatic Reconstructions | Titles here center on reconstructing past climate conditions over the last millennium using diverse proxy data such as tree rings, corals, and ice cores to analyze temperature and precipitation variations. |
| 67 | Environmental Humanities and Settlement History | This group explores the intersection of environmental change and human settlement, specifically examining the colonization of the North Atlantic and the impact of climate variability on medieval societies. |
| 86 | Urban Archaeology and Material Analysis | The cluster addresses methodological approaches to urban archaeology, including soil analysis, micro-XRF techniques, and the study of specific urban sites like Ribe and Haithabu. |
| 171 | Ancient DNA and Population Genetics | This cluster is defined by the application of ancient DNA (aDNA) and population genetics to understand migration, ethnicity, and the demographic history of past populations. |
| 292 | Archaeological Theory and Ethics | Titles in this group engage with theoretical frameworks regarding material culture, the ethics of archaeological practice, and the philosophical implications of heritage and modernity. |
| 11 | Scandinavian Prehistoric Technology and Ritual | This cluster examines specific prehistoric technologies, such as reindeer hunting and flint dagger production, alongside ritual practices and the role of the Sami people in Scandinavian history. |
| 39 | Early Medieval Kingship and Trade | The focus here is on the political structures of early medieval Scandinavia, including the nature of kingship, the development of central places, and trade networks involving the Frisians and Baltic regions. |
| 43 | Old Norse Mythology and Literature | This cluster is dedicated to the study of Old Norse literary sources, mythological figures, and the interpretation of runic stones and picture stones through a literary and religious lens. |
| 49 | Childhood, Gender, and Social Identity | Titles explore the social roles of children and gender dynamics in the Viking Age, analyzing skeletal evidence and literary texts to understand childhood development and gendered identities. |
| 222 | Stable Isotope Analysis in Bioarchaeology | This group is united by the use of stable isotope analysis (carbon, nitrogen, strontium) to reconstruct diet, mobility, and weaning practices in past human and animal populations. |
| 277 | Niche Construction and Human Adaptation | The cluster investigates the co-evolution of humans and their environments, focusing on how cultural practices and biological evolution interact to shape adaptation and resilience. |
| 287 | Runes, Writing, and Literacy | This group examines the transition from oral tradition to written records, focusing on the spread of Christianity through runes, the logic of writing systems, and the interpretation of inscriptions. |
| 172 | Excavation Reports and Site Stratigraphy | Titles consist primarily of interim reports and methodological discussions regarding specific excavation sites, focusing on stratigraphy, artifact classification, and site-specific findings. |
| 149 | Viking Age Urban Development | This cluster analyzes the emergence and development of Viking Age towns, covering urban planning, pottery chronology, and the economic status of settlements like Ribe and Odense. |
| 9 | Maritime Trade and Regional Archaeology | The focus is on maritime networks, trade routes, and regional archaeological surveys, particularly concerning the Baltic, Pacific Islands, and the development of specific coastal settlements. |
| 52 | Heroic Poetry and Royal Power | This group explores the intersection of heroic literature, such as the Edda and skaldic poetry, with archaeological evidence of royal power, central places, and ceremonial buildings. |
| 110 | Ancient Genomics and Bioinformatics | Titles center on the technical and analytical aspects of ancient DNA research, including bioinformatics tools, genomic analysis of marine species, and the demographic history of populations. |
| 282 | Migration Theory and Network Analysis | This cluster critically examines theories of migration, network structures, and the social construction of landscapes, often challenging traditional narratives of population movement. |
| 8 | Proto-Urban Centers and Craft Production | The focus is on the development of early urban centers like Ladoga and Ribe, with a specific emphasis on craft production, trade goods, and the stratigraphy of early medieval settlements. |
| 216 | Insular Art and Metalwork | This group analyzes the stylistic and technical aspects of insular art, particularly metalwork, brooches, and enamel work, within the context of Scandinavian and Irish interactions. |
| 68 | Environmental Adaptation and Risk | Titles explore how societies adapted to environmental risks and uncertainties, examining agricultural intensification, resource management, and cultural responses to climate change. |
| 146 | Remote Sensing and Landscape Modeling | This cluster utilizes advanced geospatial technologies, including remote sensing and machine learning algorithms, to model landscapes, identify archaeological features, and analyze spatial data. |
| 151 | Island Environments and Resilience | The focus is on the specific challenges and adaptations of island societies, analyzing environmental impacts, agricultural models, and the resilience of North Atlantic settlements. |
| 87 | Archaeological Chemistry and Material Science | This group applies chemical and physical analysis techniques, such as GC-MS and FTIR, to identify organic residues, production methods, and the composition of archaeological materials. |

#### T7 Dual v1 (empty) (`results/test_runs/test7_batch_25clusters_duallabel_v1_empty.csv`)

| community_id | label | description | secondary_label | secondary_description |
|---|---|---|---|---|
| 101 | Viking Age Migration and Diaspora | This cluster focuses on the movement of people and the formation of diaspora communities during the Viking Age, utilizing methods like lead and strontium isotope analysis to trace origins. It also touches on the political economy of chiefdoms and the role of women in ornament production within these mobile societies. |  |  |
| 14 | Millennial Paleoclimatic Reconstructions | The titles center on statistical and proxy-based methods for reconstructing past climate conditions, including temperature variations, droughts, and solar activity over the last millennium. Techniques such as dendroclimatology, wavelets, and radiocarbon modeling are prominent. |  |  |
| 67 | North Atlantic Settlement and Environment | This cluster examines the environmental context and challenges of early European colonization in the North Atlantic, including Iceland and Greenland. It integrates archaeological evidence of settlement with historical records of climate events like volcanic eruptions and droughts. |  |  |
| 86 | Archaeological Methods and Material Analysis | Titles here focus on the technical aspects of archaeological fieldwork and laboratory analysis, including soil description, micro-XRF, and the study of specific materials like iron and diatoms. It also covers theoretical approaches to urban archaeology and residuality. |  |  |
| 171 | Ancient DNA and Population Genetics | This cluster is dominated by the application of ancient DNA (aDNA) and population genetics to understand migration, ethnicity, and kinship in prehistory. It critically evaluates the construction of knowledge around these genetic markers and their intersection with social identity. |  |  |
| 292 | Theory and Philosophy of Science | The titles explore epistemological and theoretical frameworks, including the nature of modernity, ethics in archaeology, and the construction of scientific knowledge. It also touches on environmental humanities and the response of heritage institutions to climate change. |  |  |
| 11 | Norwegian Prehistory and Sami Studies | This cluster focuses on the prehistory of Norway, specifically the Stone Age and the history of the Sami people. It includes typological analyses of artifacts, geophysical surveys, and studies of reindeer hunting and cultural transmission in the region. |  |  |
| 39 | Early Medieval Political Centers and Kingship | Titles examine the formation of early medieval states, the nature of kingship, and the development of central places in Scandinavia and the Baltic region. It includes analysis of poetry, place names, and archaeological evidence for political structures. |  |  |
| 43 | Old Norse Mythology and Literature | This cluster is dedicated to the study of Old Norse myths, legends, and literary texts, including the Eddas and sagas. It explores themes of gender, chaos, and religious practices through the analysis of picture stones and textual interpretation. |  |  |
| 49 | Childhood, Gender, and Social Roles | The cluster investigates the social roles of children and gender dynamics in the Viking Age and broader historical contexts. It combines bioarchaeological studies of skeletal remains with theoretical discussions on masculinity, femininity, and social justice. |  |  |
| 222 | Stable Isotope Analysis in Bioarchaeology | This cluster focuses on the application of stable isotope analysis (C, N, O, Sr) to reconstruct diet, mobility, and weaning practices in past populations. It includes methodological studies on bone diagenesis and the interpretation of isotopic signatures. |  |  |
| 277 | Human Adaptation and Niche Construction | Titles explore the co-evolution of humans and their environments, focusing on adaptation, niche construction, and the impact of natural hazards on societal resilience. It draws on comparative studies of hunter-gatherers and complex societies. |  |  |
| 287 | Runes, Writing, and Literacy | This cluster examines the development and use of runes and writing systems in medieval Scandinavia and beyond. It covers the transition from oral to written traditions, the organization of society through text, and the interpretation of rune stones. |  |  |
| 172 | Excavation Reports and Settlement Archaeology | The cluster consists primarily of interim and final reports on specific archaeological excavations, particularly in Iceland and the North Atlantic. It details settlement structures, artifact distributions, and radiocarbon dating results from these sites. |  |  |
| 149 | Urban Archaeology and Town Development | This cluster focuses on the emergence and development of medieval towns, particularly in Denmark and the Baltic region. It analyzes urban planning, pottery chronologies, and the relationship between rural and urban economies. |  |  |
| 9 | Maritime Trade and Regional Archaeology | Titles cover maritime archaeology, trade routes, and regional studies in the Baltic and Pacific. It includes specific analyses of trade goods, shipbuilding, and the interaction between different cultural regions during the Viking Age. |  |  |
| 52 | Viking Age Art and Symbolism | This cluster centers on the interpretation of Viking Age art, including metalwork, jewelry, and poetic imagery. It explores the symbolism of objects like the Sutton Hoo shield and the role of art in expressing power and identity. |  |  |
| 110 | Ancient Genomics and Bioinformatics | The cluster is heavily technical, focusing on the computational methods and genomic analysis of ancient DNA, particularly for marine species like cod and human populations. It covers alignment algorithms, variant calling, and population structure analysis. |  |  |
| 282 | Prehistoric Migrations and Cultural Transformations | This cluster examines large-scale prehistoric migrations, such as the Corded Ware culture, and their impact on cultural and genetic landscapes. It also discusses the theoretical frameworks for understanding migration and the construction of identity. |  |  |
| 8 | Viking Age Towns and Craft Production | Titles focus on the economic and craft aspects of Viking Age towns, including pottery production, textile manufacturing, and proto-urban centers. It includes specific case studies of sites like Ribe and Kaupang. |  |  |
| 216 | Insular Metalwork and Ornamentation | This cluster is dedicated to the study of metal artifacts, particularly brooches and jewelry from the Insular and Scandinavian regions. It includes typological studies, provenance analysis, and the examination of specific artifact groups. |  |  |
| 68 | Environmental Archaeology and Subsistence | The cluster explores the relationship between human societies and their environments, focusing on subsistence strategies, agricultural intensification, and the impact of climate on settlement. It includes studies of animal hair, bows, and landscape management. |  |  |
| 146 | Remote Sensing and Landscape Archaeology | This cluster focuses on the application of advanced remote sensing and spatial analysis techniques to archaeological landscapes. It covers the use of GIS, machine learning, and high-resolution imaging to identify and interpret archaeological features. |  |  |
| 151 | Climate Change and Societal Resilience | Titles examine the impact of climate change, particularly the Little Ice Age, on societal resilience and adaptation in the North Atlantic. It includes studies of land management, grazing models, and the conversion of societies to Christianity. |  |  |
| 87 | Archaeological Chemistry and Material Science | This cluster focuses on the chemical and physical analysis of archaeological materials, including organic residues, glues, and biomarkers. It covers techniques like GC-MS and FTIR to understand production processes and material use in prehistory. |  |  |

#### T8 Dual v2 (fixed) (`results/test_runs/test8_batch_10clusters_duallabel_v2_fixed.csv`)

| community_id | label | description | secondary_label | secondary_description |
|---|---|---|---|---|
| 101 | Viking Age Bioarchaeology and Migration | This cluster focuses on the application of scientific methods, particularly isotopic analysis of teeth and bones, to trace migration patterns and diet during the Viking Age. It also includes broader theoretical works on the political economy of chiefdoms and the role of women in ornament production. | Political Economy and Social Theory | Several titles address the theoretical frameworks of how chiefs gain power, the political economy of prehistory, and the specific social roles of women in the production and distribution of gold ornaments, distinct from the bioarchaeological focus. |
| 14 | Millennial Paleoclimatic Reconstructions | The dominant theme is the reconstruction of past climate conditions over the last millennium using diverse proxy data, including tree rings, corals, and ice cores. The cluster emphasizes statistical methods, model validation, and the detection of specific climate phenomena like ENSO and solar activity. |  |  |
| 67 | North Atlantic Environmental History | This cluster examines the intersection of human settlement and environmental change in the North Atlantic, specifically focusing on Iceland, the Faroes, and Greenland. It integrates archaeological evidence of faunal exploitation with documentary sources regarding weather patterns and volcanic events. | Theoretical Environmental Humanities | A significant portion of the cluster is dedicated to theoretical discussions on the Anthropocene, the need for integrated environmental humanities, and the functional distinctness of current geological epochs from the Holocene. |
| 86 | Archaeological Methodology and Soil Science | The titles center on the technical aspects of archaeological fieldwork and analysis, including soil profile description, redoximorphic features, and the recording of urban residuality. It also covers specific analytical techniques like Micro-XRF and the study of diatoms for environmental reconstruction. | Urbanization and State Formation | Several titles discuss broader theoretical and historical questions regarding the rise of urbanization, state formation, and cooperation, alongside the specific case studies of ironworks and horse gear. |
| 171 | Ancient DNA and Population Genetics | This cluster is dominated by the application of ancient DNA (aDNA) and population genetics to reconstruct migration, settlement history, and demographic changes in Europe. It critically evaluates the methodology of using radiocarbon calibrations as population proxies and addresses the ethics of genetic genealogy. | Social Construction of Race and Ethnicity | A substantial group of titles explores the social and political construction of race, ethnicity, and criminalization, including the demonization of specific groups and the marketing of genetic ancestry, distinct from the biological data analysis. |
| 292 | Theory of Science and Materiality | The cluster focuses on the philosophy of science, the construction of knowledge, and the materiality of objects, drawing from works on modernity, the world without us, and the public sphere. It includes theoretical critiques of rationality in biology and the ethics of recognition. | Climate and Heritage Policy | A secondary theme addresses the practical implications of climate change on World Heritage sites and the management of natural resources, particularly in Africa and the context of glacier fluctuations. |
| 11 | Norwegian Prehistoric Archaeology and Sami Studies | This cluster is heavily focused on the prehistory of Norway, specifically the Stone Age, reindeer hunting, and the material culture of the Sami people. It includes typological analyses of artifacts and reports on specific archaeological registrations and excavations in the region. | Religious Deposition and Ritual | The cluster also contains significant titles regarding the deposition of silver as offerings and the broader conceptual framework of gods, powers, and people in prehistoric religious practices. |
| 39 | Early Medieval Political Geography and Trade | The titles explore the formation of early medieval kingdoms, the role of kingship, and the development of towns and central places in Scandinavia and the Baltic region. It also covers specific trade connections involving the Frisians and the composition of early medieval texts. | Anglo-Saxon and Continental Hagiography | A distinct group of titles focuses on Anglo-Saxon poetry and the lives of continental missionaries and saints, such as Willibrord and Boniface, representing a different geographical and textual focus. |
| 43 | Old Norse Mythology and Iconography | This cluster is dedicated to the interpretation of Old Norse mythology, including the Edda, the Wieland legend, and the gender dynamics of gods like Loki. It heavily features the analysis of Gotlandic picture stones and their dating using advanced imaging techniques. | Comparative Religion and Textual Analysis | The cluster also includes comparative studies of Old Norse and Finnish religions, cultic place-names, and the intertextual relationship between the Vǫluspá and the Book of Revelation. |
| 49 | Childhood, Gender, and Social Roles | The cluster examines the social roles of children and adolescents in the Viking Age and other foraging societies, including sibling caretaking and the distribution of knowledge. It also explores the construction of masculinity and gender politics in international relations and historical contexts. | Forensic and Skeletal Analysis | A secondary theme involves the technical analysis of skeletal remains, including exercise-induced bone changes, adverse childhood experiences, and the identification of victims in mass graves, distinct from the social role focus. |


## Known limitations (earlier embedding-based method)

The earlier method's scaling limit and remaining open questions, never resolved: no built-in way to split work across multiple calls and reconcile labels for a cluster count too large for one call's context window (hierarchical mode attempted this once, inconclusively, and isn't in the current code); final embedding model choice, and whether to also embed `author` or `booktitle`/`journaltitle` alongside `title`; whether `--sampling diverse` actually improved label quality relative to `centroid` beyond the hand-checks noted above; whether `--top-n 15` was the right sample size for that method.

## Keep out of git

```
analysis/cluster_labelling/results/title_embeddings.npz
```

(Large binary, regenerable from `data/bibliography.json` at any time.
`representatives.csv` and `cluster_labels.csv` are small and fine to
commit.)