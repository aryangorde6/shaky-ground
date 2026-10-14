"""Load papers, citations and retractions into FalkorDB.

Every write is a MERGE keyed by an ID, so loading the same data twice changes nothing.

Each Paper keeps `refs`, the OpenAlex IDs of everything it cites. A CITES edge is
created only when both papers are in the graph; the other IDs are the frontier
the agent can expand later.

    uv run python -m shaky.load W2075481535            # a paper, its references and its citers
    uv run python -m shaky.load doi:10.1038/nature04533 --max-citers 200
"""
import argparse
import time

from falkordb import Graph

from . import db, openalex, retractionwatch

BATCH = 250

UPSERT_PAPERS = """
UNWIND $rows AS row
MERGE (p:Paper {id: row.id})
SET p.doi = row.doi, p.title = row.title, p.year = row.year, p.cited_by = row.cited_by,
    p.type = row.type, p.is_review = row.is_review, p.venue = row.venue, p.oa_url = row.oa_url, p.refs = row.refs,
    p.retracted = coalesce(p.retracted, false) OR row.oa_retracted
"""

# New edges start as UNKNOWN (reliance 0.3) until there is evidence. A retraction
# notice cites a paper only to retract it, so that edge is CRITICAL (reliance 0).
# ON CREATE keeps a later judgment from being overwritten by a reload.
LINK_CITATIONS = """
MATCH (a:Paper)
UNWIND a.refs AS ref
MATCH (b:Paper {id: ref})
MERGE (a)-[c:CITES]->(b)
ON CREATE SET
    c.cls = CASE a.type WHEN 'retraction' THEN 'CRITICAL' ELSE 'UNKNOWN' END,
    c.reliance = CASE a.type WHEN 'retraction' THEN 0.0 ELSE 0.3 END
RETURN count(c)
"""

LINK_RETRACTIONS = """
UNWIND $rows AS row
MATCH (p:Paper {doi: row.doi})
MERGE (r:Retraction {record_id: row.record_id})
SET r.date = row.date, r.nature = row.nature, r.reason = row.reason, r.notice_doi = row.notice_doi
MERGE (r)-[:RETRACTS]->(p)
SET p.retracted = true
"""


def _batches(rows: list, size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def upsert_papers(g: Graph, papers: list[dict]) -> int:
    rows = list({p["id"]: p for p in papers}.values())   # one row per paper
    for batch in _batches(rows):
        g.query(UPSERT_PAPERS, {"rows": batch})
    return len(rows)


def link_citations(g: Graph) -> int:
    """Create every CITES edge whose two papers are both loaded. Returns the edge count."""
    return g.query(LINK_CITATIONS).result_set[0][0]


def link_retractions(g: Graph) -> int:
    """Attach Retraction Watch notices to loaded papers, matched by DOI."""
    # collect() returns one row, so the 10,000-row result limit never applies.
    dois = g.query("MATCH (p:Paper) WHERE p.doi IS NOT NULL RETURN collect(p.doi)").result_set[0][0]
    by_doi = retractionwatch.retractions_by_doi()
    rows = [{"doi": d, **notice} for d in dois if d in by_doi for notice in by_doi[d]]
    for batch in _batches(rows):
        g.query(LINK_RETRACTIONS, {"rows": batch})
    return len(rows)


def load_neighbourhood(g: Graph, key: str, max_citers: int | None = None,
                       refresh: bool = False) -> dict:
    """A paper, the papers it cites, and the papers that cite it, with every edge among them."""
    root = openalex.work(key, refresh=refresh)
    refs = openalex.works(root["refs"], refresh=refresh)
    citers = openalex.citers(root["id"], limit=max_citers, refresh=refresh)
    return {
        "root": root["id"],
        "papers": upsert_papers(g, [root, *refs, *citers]),
        "references": len(refs),
        "citers": len(citers),
        "cites_edges": link_citations(g),
        "retraction_notices": link_retractions(g),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", help="OpenAlex ID (W...) or doi:10...")
    ap.add_argument("--max-citers", type=int, default=None)
    ap.add_argument("--refresh", action="store_true", help="ignore the API cache")
    args = ap.parse_args()

    g = db.graph()
    db.ensure_schema(g)
    start = time.perf_counter()
    result = load_neighbourhood(g, args.key, args.max_citers, args.refresh)
    print(f"loaded in {time.perf_counter() - start:.1f}s: {result}")
    print(f"OpenAlex: {openalex.api.fetched} requests sent, {openalex.api.cached} from cache; "
          f"credits today {openalex.credits()}")
    print(db.summary(g))
