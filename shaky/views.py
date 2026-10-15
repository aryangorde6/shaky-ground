"""What the web UI and the MCP server show, as plain dicts: read from the graph, never written by a model.

Every number comes from a query and every sentence is quoted from a citing paper. The two functions
that can write (recomputing risk for another root, running a foundation check) need ALLOW_RUNS.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from falkordb import Graph

from . import config, foundation, report, resolve, risk

TTL = 120          # seconds a blast-radius view is reused while the graph's counts stay the same
_cache: dict = {}
_writes = threading.Lock()     # one write job at a time: risk is stored for one root at a time

NODE = """p.id, p.title, p.year, p.cited_by, p.retracted, p.is_review, p.type, p.venue, p.doi,
          p.risk, p.risk_proven, p.risk_root, p.expanded_at"""
NODE_KEYS = ["id", "title", "year", "cited_by", "retracted", "is_review", "type", "venue", "doi",
             "risk", "risk_proven", "risk_root", "expanded_at"]

DISPUTE_IDS = """
MATCH (d:Paper)-[:CITES {cls: 'CRITICAL'}]->(:Paper {id: $root}) WHERE d.type <> 'retraction' RETURN d.id
"""

TOP_PASSING = """
MATCH (p:Paper) WHERE p.risk_root = $root AND p.id <> $root AND p.risk_proven > 0 AND p.risk_proven < $t
RETURN p.id ORDER BY p.cited_by DESC, p.id LIMIT $n
"""

LINKS_AMONG = """
MATCH (a:Paper)-[c:CITES]->(b:Paper) WHERE a.id IN $ids AND b.id IN $ids
RETURN a.id, b.id, c.cls, c.reliance, c.carries, c.carry_root, c.evidence IS NOT NULL
"""

EDGE = """
MATCH (a:Paper {id: $a})-[c:CITES]->(b:Paper {id: $b})
RETURN a.title, a.year, b.title, b.year, b.retracted, properties(c)
"""

CHECKS = """
MATCH (i:Investigation {mode: 'foundation_check'})
OPTIONAL MATCH (p:Paper {id: i.root})
RETURN i.id, i.created_at, i.verdict, i.root, p.title, p.year, i.result IS NOT NULL
ORDER BY i.created_at DESC LIMIT $n
"""


class NotFound(LookupError):
    pass


class NotAllowed(PermissionError):
    pass


def _rows(g: Graph, query: str, **params) -> list:
    return g.ro_query(query, params).result_set


def _papers(g: Graph, ids) -> dict[str, dict]:
    rows = _rows(g, f"MATCH (p:Paper) WHERE p.id IN $ids RETURN {NODE}", ids=list(ids))
    return {r[0]: dict(zip(NODE_KEYS, r)) for r in rows}


def tier(p: dict, root_id: str) -> str | None:
    """builds_on / passing / not_shown, as in risk.TIERS; None if the paper has no chain to this root."""
    if p["id"] == root_id:
        return "root"
    if p.get("risk_root") != root_id:
        return None
    proven = p.get("risk_proven") or 0
    return "builds_on" if proven >= risk.BUILDS_ON else "passing" if proven > 0 else "not_shown"


def find(g: Graph, text: str) -> str:
    """The ID of the paper in the graph that the pasted text names. A DOI or OpenAlex ID needs no
    network; a PubMed ID is looked up in OpenAlex (free)."""
    kind, key = resolve.lookup_key(text)
    if kind == "pmid":
        key = resolve.resolve(text)["id"]
    rows = (_rows(g, "MATCH (p:Paper {doi: $k}) RETURN p.id", k=key[4:]) if kind == "doi"
            else _rows(g, "MATCH (p:Paper {id: $k}) RETURN p.id", k=key))
    if not rows:
        raise NotFound(f"{key.removeprefix('doi:')} is not in the graph yet (load it with: python -m shaky.load {key})")
    return rows[0][0]


def _stamp(g: Graph) -> tuple:
    return tuple(_rows(g, q)[0][0] for q in ("MATCH (p:Paper) RETURN count(p)",
                                              "MATCH ()-[c:CITES]->() RETURN count(c)",
                                              "MATCH (i:Investigation) RETURN count(i)"))


def ensure_root(g: Graph, root_id: str) -> None:
    """Risk is stored for one root at a time; recompute it if another root was propagated last."""
    current = _rows(g, "MATCH (p:Paper {id: $id}) RETURN p.risk_root", id=root_id)
    if not current:
        raise NotFound(f"{root_id} is not in the graph")
    if current[0][0] == root_id:
        return
    if not config.ALLOW_RUNS:
        raise NotAllowed(f"risk is computed for another paper on this server; it is read only (ALLOW_RUNS=0)")
    with _writes:
        risk.propagate(g, root_id)


def blast(g: Graph, root_id: str, passing: int = 40, not_shown: int = 25) -> dict:
    """The papers that depend on root_id, as a graph to draw: every paper that builds on it, every
    retracted paper downstream, every dispute, the most-cited passing and not-shown papers, and each
    one's riskiest chain to the root."""
    ensure_root(g, root_id)
    key, stamp = (root_id, passing, not_shown), _stamp(g)
    if (hit := _cache.get(key)) and hit[0] == stamp and time.time() - hit[1] < TTL:
        return hit[2]
    start = time.perf_counter()
    q = lambda query, **p: _rows(g, query, root=root_id, **p)

    rows = q(report.ROOT)
    title, year, venue, doi, cited_by, retracted, dates, reasons = rows[0]
    loaded, with_sentences, judged = q(report.COVERAGE)[0]
    disputes = {r[0] for r in q(DISPUTE_IDS)}
    picked = [r[0] for r in q(report.BUILDS_ON)]
    picked += [r[0] for r in q(report.RETRACTED_DOWNSTREAM)]
    picked += sorted(disputes)
    picked += [r[0] for r in q(TOP_PASSING, t=risk.BUILDS_ON, n=passing)]
    picked += [r[0] for r in q(report.NOT_SHOWN, n=not_shown)]
    picked = list(dict.fromkeys(p for p in picked if p != root_id))
    props = _papers(g, picked)

    def best(pid):    # the proven chain if there is one, else the possible one
        variant = "risk_proven" if (props[pid]["risk_proven"] or 0) > 0 else "risk"
        return pid, variant, risk.chain(g, pid, root_id, variant)

    chains, on_chain = {}, set()
    with ThreadPoolExecutor(8) as pool:    # independent read queries; FalkorDB runs them in parallel
        found = list(pool.map(best, picked))
    for pid, variant, c in found:
        if c:
            chains[pid] = {"variant": variant, "risk": c["risk"], "ids": [n["id"] for n in c["papers"]]}
            on_chain.update(zip(chains[pid]["ids"], chains[pid]["ids"][1:]))
    # Where each paper sits in the drawing: how many links from the root, and the next paper towards it.
    # A paper's own chain decides; a paper shown only because it is on others' chains takes the shortest.
    hops, nxt = {root_id: 0}, {root_id: None}
    for c in chains.values():
        for i, nid in enumerate(c["ids"][1:-1], start=1):
            if len(c["ids"]) - 1 - i < hops.get(nid, 99):
                hops[nid], nxt[nid] = len(c["ids"]) - 1 - i, c["ids"][i + 1]
    for pid, c in chains.items():
        hops[pid], nxt[pid] = len(c["ids"]) - 1, c["ids"][1]
    for pid in disputes:      # drawn next to the root, by the link that disputes it
        on_chain.add((pid, root_id))
        hops[pid], nxt[pid] = 1, root_id

    all_ids = {root_id, *picked, *hops}
    props.update(_papers(g, all_ids - props.keys()))
    nodes = []
    for pid in all_ids:
        p = props[pid]
        nodes.append({**{k: p[k] for k in NODE_KEYS if k != "risk_root"},
                      "tier": tier(p, root_id), "disputes": pid in disputes, "hops": hops.get(pid), "next": nxt.get(pid),
                      "shown_for": "chain" if pid not in picked and pid != root_id else "tier",
                      "chain_risk": chains.get(pid, {}).get("risk"), "chain_variant": chains.get(pid, {}).get("variant")})
    edges = []
    for a, b, cls, reliance, carries, carry_root, has_ev in _rows(g, LINKS_AMONG, ids=list(all_ids)):
        edges.append({"source": a, "target": b, "cls": cls, "reliance": reliance,
                      "carries": carries if carry_root == root_id else None,
                      "has_evidence": has_ev, "on_chain": (a, b) in on_chain})

    view = {
        "root": {"id": root_id, "title": title, "year": year, "venue": venue, "doi": doi, "cited_by": cited_by,
                 "retracted": retracted,
                 "retractions": [{"date": d, "reason": r} for d, r in zip(dates, reasons) if d]},
        "tiers": risk.tiers(g, root_id),
        "coverage": {"citers_listed": cited_by, "loaded": loaded, "with_sentences": int(with_sentences or 0),
                     "judged": int(judged or 0)},
        "builds_on_threshold": risk.BUILDS_ON, "max_hops": risk.MAX_HOPS,
        "nodes": nodes, "edges": edges,
        "investigations": investigations(g, root_id),
        "computed_ms": round((time.perf_counter() - start) * 1000),
    }
    _cache[key] = (stamp, time.time(), view)
    return view


def paper(g: Graph, pid: str, root_id: str) -> dict:
    """One paper: what it is, its tier, and its riskiest chains to the root with the evidence on every link."""
    props = _papers(g, [pid])
    if not props:
        raise NotFound(f"{pid} is not in the graph")
    p = props[pid]
    direct = _rows(g, "MATCH (a:Paper {id: $a})-[c:CITES]->(b:Paper {id: $b}) RETURN c.cls, c.evidence",
                   a=pid, b=root_id)
    citers = _rows(g, "MATCH (x:Paper)-[:CITES]->(p:Paper {id: $id}) RETURN count(x)", id=pid)[0][0]
    out = {**{k: p[k] for k in NODE_KEYS if k != "risk_root"}, "tier": tier(p, root_id),
           "citers_loaded": citers, "direct_link": {"cls": direct[0][0], "evidence": direct[0][1]} if direct else None,
           "chains": {}}
    if pid != root_id:
        for variant in ("risk_proven", "risk"):
            if c := risk.chain(g, pid, root_id, variant):
                out["chains"][variant] = c
    return out


def edge(g: Graph, a: str, b: str) -> dict:
    """One citation: how paper a cites paper b, the sentences, the class and who judged it, and the carry votes."""
    rows = _rows(g, EDGE, a=a, b=b)
    if not rows:
        raise NotFound(f"{a} does not cite {b} in the graph")
    a_title, a_year, b_title, b_year, b_retracted, c = rows[0]
    c = dict(c)
    return {"source": {"id": a, "title": a_title, "year": a_year},
            "target": {"id": b, "title": b_title, "year": b_year, "retracted": b_retracted},
            **{k: c.get(k) for k in ("cls", "reliance", "evidence", "contexts", "intents", "influential",
                                     "evidence_src", "why", "judged_by", "judged_at",
                                     "carries", "carry_root", "carry_via", "carry_votes", "carry_evidence",
                                     "carry_why", "carry_by", "carry_at")}}


def investigations(g: Graph, root_id: str) -> list[dict]:
    """The agent's blast-radius investigations from this root: what it expanded each round, and why."""
    out = {}
    for inv, at, rnd, pid, ptitle, reason, by, citers, credits, judged in _rows(g, report.EXPLORED, root=root_id):
        out.setdefault(inv, {"id": inv, "created_at": at, "rounds": []})["rounds"].append(
            {"round": rnd, "paper": pid, "title": ptitle, "reason": reason, "decided_by": by,
             "citers": citers, "credits": credits, "judged": judged})
    return list(out.values())


def checks(g: Graph, n: int = 20) -> list[dict]:
    """The latest foundation check of each paper, newest first; a check with a stored result wins over
    a later one without."""
    rows = [{"investigation": i, "created_at": at, "verdict": v, "paper": pid, "title": t, "year": y, "has_result": r}
            for i, at, v, pid, t, y, r in _rows(g, CHECKS, n=200)]
    latest = {}
    for c in sorted(rows, key=lambda c: (c["has_result"], c["created_at"]), reverse=True):
        latest.setdefault(c["paper"], c)
    return sorted(latest.values(), key=lambda c: c["created_at"], reverse=True)[:n]


def check_result(g: Graph, inv: str) -> dict:
    rows = _rows(g, "MATCH (i:Investigation {id: $id, mode: 'foundation_check'}) RETURN i.result, i.verdict, i.root",
                 id=inv)
    if not rows:
        raise NotFound(f"no foundation check {inv}")
    result, verdict, pid = rows[0]
    if result:
        return json.loads(result)
    p = _papers(g, [pid]).get(pid, {"id": pid, "title": None, "year": None, "doi": None})
    return {"paper": {k: p.get(k) for k in ("id", "doi", "title", "year")}, "investigation": inv, "verdict": verdict,
            "retracted_ancestors": [], "details_missing": "this check was run before results were stored; run it again"}


def latest_check(g: Graph, pid: str) -> dict | None:
    """The latest stored foundation check of a paper that has a result, or None."""
    for c in checks(g, n=200):
        if c["paper"] == pid and c["has_result"]:
            return check_result(g, c["investigation"])
    return None


def check_list(g: Graph, text: str, credits: int = 200) -> dict:
    """A reference list: each DOI's latest stored check, and a new check for the rest when runs are
    allowed (otherwise the rest are NOT CHECKED)."""
    latest = {c["paper"]: c["investigation"] for c in checks(g, n=200) if c["has_result"]}

    def stored(doi: str) -> dict | None:
        try:
            pid = find(g, doi)
        except (NotFound, resolve.ResolveError):
            return None
        return check_result(g, latest[pid]) if pid in latest else None

    def known(doi: str) -> dict | None:
        try:
            return _papers(g, [find(g, doi)]).popitem()[1]
        except (NotFound, resolve.ResolveError, KeyError):
            return None

    if not config.ALLOW_RUNS:
        return foundation.check_list(g, text, stored=stored, known=known, run=False)
    with _writes:
        return foundation.check_list(g, text, max_credits=credits, stored=stored)


def run_check(g: Graph, text: str, credits: int = 50) -> dict:
    """A new foundation check (writes: loads references, judges citations). Needs ALLOW_RUNS."""
    if not config.ALLOW_RUNS:
        raise NotAllowed("this server is read only (ALLOW_RUNS=0): run checks locally with python -m shaky.foundation")
    resolve.lookup_key(text)        # refuse bad input before taking the lock
    with _writes:
        return foundation.check(g, text, max_credits=credits)
