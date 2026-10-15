"""Risk: how much each paper depends on a retracted paper, through chains of citations.

The risk of a chain is the product of the reliance weights along it; a paper's risk is its
riskiest chain of up to MAX_HOPS citations. Two ways to get it, which always agree:

- propagate(): risk flows backwards along citations, one hop per pass. Each pass is one
  query over the edges, so the cost grows with edges, not with the number of chains.
  It writes Paper.risk (possible) and Paper.risk_proven (evidence only) for one root at a
  time, and labels the papers proven to build on the root :Contaminated.
- chain(): one paper's riskiest chain, by reduce() over all its paths to the root, with the
  evidence on every link.
"""
from falkordb import Graph

from .reliance import WEIGHTS

MAX_HOPS = 3
EXPLORE = 0.05     # the agent considers expanding papers whose possible risk is at least this

# Proven risk >= BUILDS_ON means every link of the chain was judged FOUNDATIONAL or SUPPORTING:
# such a chain of up to MAX_HOPS links has risk >= 0.6^3 = 0.216, while any chain with a
# BACKGROUND link has risk <= 0.15. So a paper that only mentions the root in passing never
# counts as building on it.
BUILDS_ON = 0.2
assert WEIGHTS["SUPPORTING"] ** MAX_HOPS >= BUILDS_ON > WEIGHTS["BACKGROUND"]

# Two numbers per paper, so no answer claims more than its evidence:
# - risk:        possible dependence; an edge with no evidence (UNKNOWN) counts at its 0.3
# - risk_proven: dependence shown by citation sentences; UNKNOWN edges count as 0
# A link beyond the first counts at full weight in risk_proven only once carry.py has shown that
# it relies on the part that came from the root; until then, at most as a passing mention. A
# link shown NOT to carry counts at most as a passing mention in both numbers.
# {end} is the cited paper of link c.
CARRIES = "({end}.id = $root OR (c.carry_root = $root AND c.carries = true))"
NOT_CARRIES = "({end}.id <> $root AND c.carry_root = $root AND c.carries = false)"
CAPPED = "CASE WHEN c.reliance < $passing THEN c.reliance ELSE $passing END"
VARIANTS = {   # property: (which links count, the weight of link c)
    "risk": ("c.reliance > 0", f"CASE WHEN {NOT_CARRIES} THEN {CAPPED} ELSE c.reliance END"),
    "risk_proven": ("c.reliance > 0 AND c.cls <> 'UNKNOWN'", f"CASE WHEN {CARRIES} THEN c.reliance ELSE {CAPPED} END"),
    # the most a chain could be if every link without evidence turned out FOUNDATIONAL (chain() only)
    "upper": ("c.reliance > 0", f"CASE WHEN c.cls = 'UNKNOWN' THEN 1.0 WHEN {NOT_CARRIES} THEN {CAPPED} ELSE c.reliance END"),
}
PROPAGATED = ("risk", "risk_proven")

PROPAGATE = """
MATCH (a:Paper)-[c:CITES]->(b:Paper)
WHERE b.{prop} > 0 AND {counts}
WITH a, max(b.{prop} * {weight}) AS r
WHERE r > coalesce(a.{prop}, 0.0)
SET a.{prop} = r
RETURN count(a)
"""

CHAIN = """
MATCH p = (a:Paper {{id: $id}})-[:CITES*1..{hops}]->(:Paper {{id: $root}})
WHERE all(c IN relationships(p) WHERE {counts})
WITH p, reduce(r = 1.0, c IN relationships(p) | r * {weight}) AS risk
ORDER BY risk DESC
LIMIT 1
RETURN risk,
       [n IN nodes(p) | {{id: n.id, title: n.title, year: n.year, retracted: n.retracted}}],
       [c IN relationships(p) | {{cls: c.cls, reliance: c.reliance, evidence: c.evidence, judged_by: c.judged_by,
                                  carries: c.carries, carry_evidence: c.carry_evidence, carry_votes: c.carry_votes}}]
"""


def _params(root_id: str) -> dict:
    return {"root": root_id, "passing": WEIGHTS["BACKGROUND"]}


def propagate(g: Graph, root_id: str, hops: int = MAX_HOPS) -> dict:
    """Recompute Paper.risk and Paper.risk_proven for everything that depends on root_id (writes).
    :Contaminated marks the papers proven to build on it (risk_proven >= BUILDS_ON)."""
    g.query("MATCH (p:Paper) WHERE p.risk IS NOT NULL OR p.risk_proven IS NOT NULL "
            "SET p.risk = NULL, p.risk_proven = NULL, p.risk_root = NULL")
    g.query("MATCH (p:Contaminated) REMOVE p:Contaminated")
    if not g.query("MATCH (p:Paper {id: $id}) SET p.risk = 1.0, p.risk_proven = 1.0, p.risk_root = $id RETURN p",
                   {"id": root_id}).result_set:
        raise ValueError(f"{root_id} is not in the graph")
    passes = {}
    for prop in PROPAGATED:
        counts, weight = VARIANTS[prop]
        query = PROPAGATE.format(prop=prop, counts=counts, weight=weight.format(end="b"))
        passes[prop] = [g.query(query, _params(root_id)).result_set[0][0] for _ in range(hops)]
    g.query("MATCH (p:Paper) WHERE p.risk IS NOT NULL SET p.risk_root = $id", {"id": root_id})
    g.query("MATCH (p:Paper) WHERE p.id <> $id AND p.risk_proven >= $t SET p:Contaminated",
            {"t": BUILDS_ON, "id": root_id})
    return {"root": root_id, "updated_per_pass": passes, **tiers(g, root_id)}


def chain(g: Graph, paper_id: str, root_id: str, variant: str = "risk_proven", hops: int = MAX_HOPS) -> dict | None:
    """The paper's riskiest chain to the root, scored as one of VARIANTS, or None if there is none."""
    counts, weight = VARIANTS[variant]
    query = CHAIN.format(hops=hops, counts=counts, weight=weight.format(end="endNode(c)"))
    rows = g.ro_query(query, {"id": paper_id, **_params(root_id)}, timeout=10_000).result_set
    if not rows:
        return None
    risk, papers, links = rows[0]
    return {"risk": risk, "papers": papers, "links": links}


# Every paper with a chain to the root falls in exactly one tier, decided by evidence alone:
#   builds_on  proven: some chain has every link judged FOUNDATIONAL or SUPPORTING (:Contaminated)
#   passing    proven chains exist, but each one has a BACKGROUND link
#   not_shown  no chain has evidence on every link yet
# disputes counts the direct citers whose link is CRITICAL; they carry no risk at all. Retraction
# notices are left out: a notice copies its paper's reference list, so it "cites" the root without disputing it.
TIERS = """
MATCH (p:Paper) WHERE p.risk_root = $id AND p.id <> $id
WITH sum(CASE WHEN p.risk_proven >= $t THEN 1 ELSE 0 END) AS builds_on,
     sum(CASE WHEN p.risk_proven > 0 AND p.risk_proven < $t THEN 1 ELSE 0 END) AS passing,
     sum(CASE WHEN coalesce(p.risk_proven, 0) = 0 THEN 1 ELSE 0 END) AS not_shown
OPTIONAL MATCH (d:Paper)-[:CITES {cls: 'CRITICAL'}]->(:Paper {id: $id}) WHERE d.type <> 'retraction'
RETURN builds_on, passing, not_shown, count(DISTINCT d)
"""


def tiers(g: Graph, root_id: str) -> dict:
    b, p, n, d = g.ro_query(TIERS, {"id": root_id, "t": BUILDS_ON}).result_set[0]
    return {"builds_on": int(b), "passing": int(p), "not_shown": int(n), "disputes": int(d)}


# Brute force, for checking propagate(): every chain of up to MAX_HOPS links, scored with reduce().
BRUTE = """
MATCH p = (a:Paper)-[:CITES*1..{hops}]->(:Paper {{id: $root}})
WHERE a.id <> $root AND all(c IN relationships(p) WHERE {counts})
RETURN a.id, max(reduce(r = 1.0, c IN relationships(p) | r * {weight}))
"""


def check(g: Graph, root_id: str, hops: int = MAX_HOPS) -> dict:
    """Compare the stored risk numbers with brute force over every chain. Returns mismatches per number."""
    out = {}
    for prop in PROPAGATED:
        counts, weight = VARIANTS[prop]
        query = BRUTE.format(hops=hops, counts=counts, weight=weight.format(end="endNode(c)"))
        brute = dict(g.ro_query(query, _params(root_id), timeout=300_000).result_set)
        stored = dict(g.ro_query(f"MATCH (a:Paper) WHERE a.id <> $root AND a.risk_root = $root AND a.{prop} > 0 "
                                 f"RETURN a.id, a.{prop}", {"root": root_id}).result_set)
        bad = [k for k in brute.keys() | stored.keys() if abs(brute.get(k, 0) - stored.get(k, 0)) > 1e-9]
        out[prop] = {"papers": len(brute), "mismatches": len(bad)}
    return out


if __name__ == "__main__":
    import json
    import sys
    import time

    from . import db
    graph, root = db.graph(), sys.argv[1]
    start = time.perf_counter()
    print(json.dumps(propagate(graph, root)), f"({time.perf_counter() - start:.2f} s)")
    start = time.perf_counter()
    print(json.dumps(check(graph, root)), f"brute force ({time.perf_counter() - start:.1f} s)")
