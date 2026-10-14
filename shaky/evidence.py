"""Attach citation sentences (evidence) to the CITES edges that point at one paper.

    uv run python -m shaky.evidence W2075481535
"""
import sys

from falkordb import Graph

from . import db, semanticscholar

SET_EVIDENCE = """
UNWIND $rows AS row
MATCH (a:Paper {id: row.src})-[c:CITES]->(b:Paper {id: $cited})
SET c.contexts = row.contexts, c.intents = row.intents, c.influential = row.influential,
    c.evidence_src = 'semantic_scholar'
RETURN count(c)
"""


def attach_s2(g: Graph, cited_id: str, refresh: bool = False) -> dict:
    doi = g.ro_query("MATCH (p:Paper {id: $id}) RETURN p.doi", {"id": cited_id}).result_set
    if not doi or not doi[0][0]:
        raise SystemExit(f"{cited_id}: not in the graph, or it has no DOI to look up")
    cites = semanticscholar.citations(doi[0][0], refresh=refresh)

    # Match each citing paper to a loaded Paper: by DOI, else by MAG ID (OpenAlex W = MAG).
    pairs = g.ro_query("MATCH (p:Paper) RETURN collect([p.doi, p.id])").result_set[0][0]
    by_doi = {d: i for d, i in pairs if d}
    loaded = {i for _, i in pairs}
    rows, unmatched = [], 0
    for c in cites:
        if not c["contexts"]:
            continue
        src = by_doi.get(c["doi"]) or (f"W{c['mag']}" if c["mag"] and f"W{c['mag']}" in loaded else None)
        if src is None:
            unmatched += 1
            continue
        rows.append({"src": src, "contexts": c["contexts"], "intents": c["intents"],
                     "influential": c["influential"]})
    rows = list({r["src"]: r for r in rows}.values())
    updated = g.query(SET_EVIDENCE, {"rows": rows, "cited": cited_id}).result_set[0][0] if rows else 0
    return {"s2_citations": len(cites), "with_sentences": sum(1 for c in cites if c["contexts"]),
            "matched_to_edges": updated, "not_in_graph": unmatched + len(rows) - updated}


if __name__ == "__main__":
    print(attach_s2(db.graph(), sys.argv[1]))
