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

from . import db, openalex, resolve, retractionwatch
from .reliance import WEIGHTS

BATCH = 250

UPSERT_PAPERS = """
UNWIND $rows AS row
MERGE (p:Paper {id: row.id})
SET p.doi = row.doi, p.title = row.title, p.year = row.year, p.cited_by = row.cited_by,
    p.type = row.type, p.is_review = row.is_review, p.venue = row.venue, p.oa_url = row.oa_url, p.refs = row.refs,
    p.retracted = coalesce(p.retracted, false) OR row.oa_retracted
"""

# New edges start as UNKNOWN until there is evidence. A retraction
# notice cites a paper only to retract it, so that edge is CRITICAL.
# ON CREATE keeps a later judgment from being overwritten by a reload.
LINK_CITATIONS = """
MATCH (a:Paper)
UNWIND a.refs AS ref
MATCH (b:Paper {id: ref})
MERGE (a)-[c:CITES]->(b)
ON CREATE SET
    c.cls = CASE a.type WHEN 'retraction' THEN 'CRITICAL' ELSE 'UNKNOWN' END,
    c.reliance = CASE a.type WHEN 'retraction' THEN $critical ELSE $unknown END
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
    weights = {"critical": WEIGHTS["CRITICAL"], "unknown": WEIGHTS["UNKNOWN"]}
    return g.query(LINK_CITATIONS, weights).result_set[0][0]


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
    ap.add_argument("paper", help="a DOI, OpenAlex ID or PubMed ID, in any common shape")
    ap.add_argument("--max-citers", type=int, default=None)
    ap.add_argument("--refresh", action="store_true", help="ignore the API cache")
    args = ap.parse_args()

    try:
        found = resolve.resolve(args.paper)
    except resolve.ResolveError as e:
        raise SystemExit(str(e))
    print(f"{args.paper} -> {found['id']}: {found['title']} ({found['year']})")
    g = db.graph()
    db.ensure_schema(g)
    start = time.perf_counter()
    result = load_neighbourhood(g, found["id"], args.max_citers, args.refresh)
    print(f"loaded in {time.perf_counter() - start:.1f}s: {result}")
    print(f"OpenAlex: {openalex.api.fetched} requests sent, {openalex.api.cached} from cache; "
          f"credits today {openalex.credits()}")
    print(db.summary(g))
