"""
exporter.py — Export citation graph to multiple formats.

Generates GraphML and GEXF from bibliography.json and _graph_state.json.
Both formats preserve node attributes (title, author, year, generation,
entry_type, completeness) and directed edges (cited_by relationships).

GraphML is the recommended format for R/igraph analysis.
GEXF is recommended for Gephi visualization.

Usage via run.py:
    python3 run.py --export

Or directly:
    python3 -m bibvik.exporter --bib PATH --output-dir PATH
"""

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_author(entry: dict) -> str:
    authors = entry.get("author", [])
    if not authors:
        return ""
    a = authors[0]
    return f"{a.get('family', '')} {a.get('given', '')}".strip()


def _year(entry: dict) -> str:
    d = entry.get("date") or entry.get("year") or ""
    return str(d)[:4]


def _safe(val) -> str:
    if val is None:
        return ""
    return str(val)


def _build_export_set(bib: dict) -> dict:
    """
    Build the dict of entries to export as nodes, plus track which
    tombstoned entries need to be included as "ghost" nodes because
    they have outbound citations (they cited other works, even though
    their own bibliographic record was deleted as unresolvable/merged).

    A bibliography record can be tombstoned (_deleted: True) for two
    different reasons that must be handled differently on export:

      1. Unresolvable/garbage entry (e.g. no title, no raw citation) —
         the record itself is junk, but if it was an F1/F2 paper that
         genuinely cited other works, those citation edges are real
         and must survive.
      2. Merged into another entry (_merged_into set) — the citing
         relationships were already remapped onto the surviving
         "keep" entry by corrections.py, so this tombstone's own
         outbound presence would double-count if also kept as a
         ghost node. These are excluded from ghost-node treatment.

    Returns a dict of citekey -> entry for every node that should
    appear in the exported graph: all active (non-deleted) entries,
    plus deleted-but-unmerged entries that appear as a citer
    somewhere (so their outbound edges have a valid source node).
    """
    active = {ck: e for ck, e in bib.items() if not e.get("_deleted")}

    # Citekeys that appear as a citer anywhere in the bibliography.
    citer_keys: set[str] = set()
    for entry in bib.values():
        citer_keys.update(entry.get("cited_by", []))

    export_set = dict(active)

    for ck in citer_keys:
        if ck in export_set:
            continue
        entry = bib.get(ck)
        if entry is None:
            # Citekey referenced in a cited_by list but never existed
            # as a bibliography record at all — genuinely orphaned,
            # nothing to export it as. Logged, not silently dropped.
            logger.warning(
                "cited_by references unknown citekey %r — edge(s) from "
                "it cannot be exported", ck
            )
            continue
        if entry.get("_merged_into"):
            # Already remapped onto the keep entry — including this as
            # a ghost node too would double-count its outbound edges.
            continue
        # Tombstoned (deleted, not merged) entry that cited other
        # works — include as a ghost node so those edges are preserved.
        export_set[ck] = entry

    return export_set


def _is_ghost(entry: dict) -> bool:
    return bool(entry.get("_deleted"))


# ── GraphML export ────────────────────────────────────────────────────────────

GRAPHML_NODE_ATTRS = [
    ("title",       "string"),
    ("year",        "string"),
    ("generation",  "string"),
    ("entry_type",  "string"),
    ("first_author","string"),
    ("doi",         "string"),
    ("completeness","double"),
    ("in_degree",   "int"),
    ("out_degree",  "int"),
    ("is_ghost",    "boolean"),
]


def export_graphml(bib: dict, output_path: Path):
    """Export bibliography as directed GraphML for igraph/Gephi.

    `bib` should already be the full export set (active + ghost citer
    entries) as built by _build_export_set — see run_export.
    """

    # Pre-compute degree
    in_degree:  dict[str, int] = {ck: 0 for ck in bib}
    out_degree: dict[str, int] = {ck: 0 for ck in bib}
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                out_degree[citing_ck] = out_degree.get(citing_ck, 0) + 1
                in_degree[ck] = in_degree.get(ck, 0) + 1

    NS = "http://graphml.graphdrawing.org/graphml"
    root = ET.Element("graphml", xmlns=NS)

    # Key declarations
    for i, (attr, atype) in enumerate(GRAPHML_NODE_ATTRS):
        ET.SubElement(root, "key", {
            "id":     f"d{i}",
            "for":    "node",
            "attr.name": attr,
            "attr.type": atype,
        })

    graph_el = ET.SubElement(root, "graph", {
        "id":             "G",
        "edgedefault":    "directed",
    })

    # Nodes
    attr_ids = {attr: f"d{i}" for i, (attr, _) in enumerate(GRAPHML_NODE_ATTRS)}

    for ck, entry in bib.items():
        node = ET.SubElement(graph_el, "node", id=ck)
        vals = {
            "title":        _safe(entry.get("title")),
            "year":         _year(entry),
            "generation":   _safe(entry.get("generation")),
            "entry_type":   _safe(entry.get("entry_type")),
            "first_author": _first_author(entry),
            "doi":          _safe(entry.get("doi")),
            "completeness": _safe(entry.get("completeness", {}).get("score", "")),
            "in_degree":    str(in_degree.get(ck, 0)),
            "out_degree":   str(out_degree.get(ck, 0)),
            "is_ghost":     "true" if _is_ghost(entry) else "false",
        }
        for attr, key_id in attr_ids.items():
            data = ET.SubElement(node, "data", key=key_id)
            data.text = vals[attr]

    # Edges: cited_by[x] means x cites this entry → edge x → ck
    edge_id = 0
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                ET.SubElement(graph_el, "edge", {
                    "id":     f"e{edge_id}",
                    "source": citing_ck,
                    "target": ck,
                })
                edge_id += 1

    xml_str = minidom.parseString(
        ET.tostring(root, encoding="unicode")
    ).toprettyxml(indent="  ")

    output_path.write_text(xml_str, encoding="utf-8")
    logger.info("GraphML written to %s  (%d nodes, %d edges)", output_path, len(bib), edge_id)
    return len(bib), edge_id


# ── GEXF export ───────────────────────────────────────────────────────────────

def export_gexf(bib: dict, output_path: Path):
    """Export bibliography as directed GEXF for Gephi.

    `bib` should already be the full export set (active + ghost citer
    entries) as built by _build_export_set — see run_export.
    """

    # Pre-compute degree
    in_degree:  dict[str, int] = {ck: 0 for ck in bib}
    out_degree: dict[str, int] = {ck: 0 for ck in bib}
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                out_degree[citing_ck] = out_degree.get(citing_ck, 0) + 1
                in_degree[ck] = in_degree.get(ck, 0) + 1

    gexf = ET.Element("gexf", {
        "xmlns":              "http://gexf.net/1.3",
        "xmlns:xsi":          "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:schemaLocation": "http://gexf.net/1.3 http://gexf.net/1.3/gexf.xsd",
        "version":            "1.3",
    })

    graph = ET.SubElement(gexf, "graph", defaultedgetype="directed")

    # Attribute declarations
    node_attrs_el = ET.SubElement(graph, "attributes", {"class": "node"})
    gexf_attrs = [
        ("0", "title",        "string"),
        ("1", "year",         "string"),
        ("2", "generation",   "string"),
        ("3", "entry_type",   "string"),
        ("4", "first_author", "string"),
        ("5", "doi",          "string"),
        ("6", "completeness", "float"),
        ("7", "in_degree",    "integer"),
        ("8", "out_degree",   "integer"),
        ("9", "is_ghost",     "boolean"),
    ]
    for attr_id, name, atype in gexf_attrs:
        ET.SubElement(node_attrs_el, "attribute", {
            "id": attr_id, "title": name, "type": atype
        })

    # Nodes
    nodes_el = ET.SubElement(graph, "nodes")
    for ck, entry in bib.items():
        label = entry.get("title") or ck
        node = ET.SubElement(nodes_el, "node", {"id": ck, "label": label[:120]})
        attvalues = ET.SubElement(node, "attvalues")
        vals = [
            ("0", _safe(entry.get("title"))),
            ("1", _year(entry)),
            ("2", _safe(entry.get("generation"))),
            ("3", _safe(entry.get("entry_type"))),
            ("4", _first_author(entry)),
            ("5", _safe(entry.get("doi"))),
            ("6", _safe(entry.get("completeness", {}).get("score", ""))),
            ("7", str(in_degree.get(ck, 0))),
            ("8", str(out_degree.get(ck, 0))),
            ("9", "true" if _is_ghost(entry) else "false"),
        ]
        for attr_id, val in vals:
            ET.SubElement(attvalues, "attvalue", {"for": attr_id, "value": val})

    # Edges
    edges_el = ET.SubElement(graph, "edges")
    edge_id = 0
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                ET.SubElement(edges_el, "edge", {
                    "id":     str(edge_id),
                    "source": citing_ck,
                    "target": ck,
                })
                edge_id += 1

    xml_str = minidom.parseString(
        ET.tostring(gexf, encoding="unicode")
    ).toprettyxml(indent="  ")

    output_path.write_text(xml_str, encoding="utf-8")
    logger.info("GEXF written to %s  (%d nodes, %d edges)", output_path, len(bib), edge_id)
    return len(bib), edge_id


# ── CSV edgelist export ───────────────────────────────────────────────────────

def export_edgelist(bib: dict, output_path: Path):
    """Export directed edgelist as CSV: source,target

    `bib` should already be the full export set (active + ghost citer
    entries) as built by _build_export_set — see run_export.
    """
    import csv
    edges = []
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                edges.append((citing_ck, ck))

    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "target"])
        w.writerows(edges)

    logger.info("Edgelist written to %s  (%d edges)", output_path, len(edges))
    return len(edges)


# ── Node table CSV export ─────────────────────────────────────────────────────

def export_node_table(bib: dict, output_path: Path):
    """Export node attributes as CSV.

    `bib` should already be the full export set (active + ghost citer
    entries) as built by _build_export_set — see run_export.
    """
    import csv

    in_degree:  dict[str, int] = {ck: 0 for ck in bib}
    out_degree: dict[str, int] = {ck: 0 for ck in bib}
    for ck, entry in bib.items():
        for citing_ck in entry.get("cited_by", []):
            if citing_ck in bib:
                out_degree[citing_ck] = out_degree.get(citing_ck, 0) + 1
                in_degree[ck] = in_degree.get(ck, 0) + 1

    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "citekey", "title", "year", "generation", "entry_type",
            "first_author", "doi", "completeness", "in_degree", "out_degree",
            "is_ghost",
        ])
        w.writeheader()
        for ck, entry in bib.items():
            w.writerow({
                "citekey":      ck,
                "title":        _safe(entry.get("title")),
                "year":         _year(entry),
                "generation":   _safe(entry.get("generation")),
                "entry_type":   _safe(entry.get("entry_type")),
                "first_author": _first_author(entry),
                "doi":          _safe(entry.get("doi")),
                "completeness": _safe(entry.get("completeness", {}).get("score", "")),
                "in_degree":    in_degree.get(ck, 0),
                "out_degree":   out_degree.get(ck, 0),
                "is_ghost":     _is_ghost(entry),
            })

    logger.info("Node table written to %s  (%d nodes)", output_path, len(bib))
    return len(bib)


# ── Main export function ──────────────────────────────────────────────────────

def run_export(bib: dict, output_dir: Path) -> dict:
    """Export citation graph to all formats. Returns dict of counts.

    Builds one export set (active entries + tombstoned-but-unmerged
    entries that appear as citers) and reuses it across all four
    export functions, so node/edge counts are consistent between
    formats. See _build_export_set for why tombstoned citers are
    included: deleting a bibliography record (e.g. an unresolvable
    ghost entry) does not mean the paper it represented never cited
    anything — only that its own metadata is untrustworthy.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    export_set = _build_export_set(bib)
    n_ghosts = sum(1 for e in export_set.values() if _is_ghost(e))
    if n_ghosts:
        logger.info(
            "%d tombstoned entries retained as ghost nodes to preserve "
            "their outbound citation edges", n_ghosts
        )

    results = {}

    n, e = export_graphml(export_set, output_dir / "citation_graph.graphml")
    results["graphml"] = {"nodes": n, "edges": e}

    n, e = export_gexf(export_set, output_dir / "citation_graph.gexf")
    results["gexf"] = {"nodes": n, "edges": e}

    e = export_edgelist(export_set, output_dir / "citation_edgelist.csv")
    results["edgelist"] = {"edges": e}

    n = export_node_table(export_set, output_dir / "citation_nodes.csv")
    results["nodes_csv"] = {"nodes": n}

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Export citation graph")
    parser.add_argument("--bib",        default="bibliography.json")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    bib = json.loads(Path(args.bib).read_text(encoding="utf-8"))
    run_export(bib, Path(args.output_dir))