"""Foundation check: does a paper rest on retracted work, and through what chain? Or can we show it doesn't?

Upstream to depth 2: the paper's references, and their references. Looking a paper up by ID is
free, and a batch of 100 IDs costs 1 OpenAlex credit, so depth 2 costs about 1 credit per 100
second-hop references.

Verdicts, never stronger than the evidence:
    RESTS ON RETRACTED WORK   a chain to a retracted paper has every link FOUNDATIONAL or SUPPORTING
                              (a second link also shown, by two models, to carry the retracted part)
    NOT PROVEN                a retracted paper is in reach and a chain to it could reach that bar,
                              but some link on it has no usable evidence
    CITES RETRACTED WORK      every chain to retracted work has a passing (BACKGROUND) or critical link
    NO RETRACTED WORK FOUND   in the references that could be checked; says how many could not
    CLEAN · DEPTH 2           no retracted work, and every reference to depth 2 was checked
    NO DATA                   OpenAlex has no reference list for the paper

    uv run python -m shaky.foundation 10.1016/j.neuroscience.2007.10.054
"""
import argparse
import json
import uuid
from datetime import datetime, timezone

from falkordb import Graph

from . import carry, db, evidence, judge, load, openalex, resolve, risk

DEPTH = 2
ORDER = ["RESTS ON RETRACTED WORK", "NOT PROVEN", "CITES RETRACTED WORK"]   # worst first

ANCESTORS = """
MATCH (p:Paper {id: $id})-[:CITES*1..2]->(r:Paper)
WHERE r.retracted AND r.id <> $id
RETURN DISTINCT r.id, r.title, r.year
"""

# Papers between P and R on a two-link chain, with how P cites them.
MIDDLE = """
MATCH (p:Paper {id: $id})-[c:CITES]->(x:Paper)-[u:CITES]->(r:Paper {id: $r})
RETURN x.id, c.cls, u.cls, c.contexts, p.title
"""

DIRECT = """
MATCH (p:Paper {id: $id})-[c:CITES]->(r:Paper {id: $r}) RETURN c.cls, c.evidence
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _evidence(g: Graph, cited_id: str, from_ids: list[str], providers: list, batch: int) -> None:
    """Citation sentences for the links from from_ids into cited_id, then reliance judgments for them."""
    try:
        evidence.attach_s2(g, cited_id)
    except SystemExit:       # no DOI: nothing to look up, those links stay UNKNOWN
        return
    judge.judge_citations(g, cited_id, providers, batch, 10_000, only_from=from_ids)


def _load(g: Graph, p: dict, max_credits: int) -> dict:
    """The paper, its references and their references. Returns what could and could not be checked."""
    refs1 = openalex.works(p["refs"])
    found1 = {w["id"] for w in refs1}
    second = sorted({r for w in refs1 for r in w["refs"]} - found1 - {p["id"]})
    room = max(0, (max_credits - len(p["refs"]) // 100 - 1) * 100)
    refs2 = openalex.works(second[:room])
    load.upsert_papers(g, [p, *refs1, *refs2])
    load.link_citations(g)
    load.link_retractions(g)
    return {"references": len(p["refs"]), "references_found": len(refs1),
            "second_hop": len(second), "second_hop_found": len(refs2),
            "second_hop_skipped_for_budget": max(0, len(second) - room),
            "without_doi": sum(1 for w in [*refs1, *refs2] if not w["doi"])}


def _tier(g: Graph, pid: str, rid: str) -> tuple[str, dict | None]:
    proven = risk.chain(g, pid, rid, "risk_proven", DEPTH)
    upper = risk.chain(g, pid, rid, "upper", DEPTH)
    if proven and proven["risk"] >= risk.BUILDS_ON:
        return "RESTS ON RETRACTED WORK", proven
    if upper and upper["risk"] >= risk.BUILDS_ON:
        return "NOT PROVEN", upper
    return "CITES RETRACTED WORK", proven or upper     # passing, or only critical links


def check(g: Graph, text: str, providers=("groq", "gemini"), batch: int = 12, max_credits: int = 50) -> dict:
    p = resolve.resolve(text)
    pid, used = p["id"], openalex.credits()["used"]
    out = {"paper": {k: p[k] for k in ("id", "doi", "title", "year")}, "paper_retracted": p["oa_retracted"]}
    if not p["refs"]:
        load.upsert_papers(g, [p])
        return {**out, "verdict": "NO DATA", "why": "OpenAlex has no reference list for this paper"}
    coverage = _load(g, p, max_credits)

    ancestors = []
    for rid, rtitle, ryear in g.ro_query(ANCESTORS, {"id": pid}).result_set:
        middles = g.ro_query(MIDDLE, {"id": pid, "r": rid}).result_set
        # Evidence and judgments for the links into R, from P and from the papers in between ...
        _evidence(g, rid, [pid, *[m[0] for m in middles]], list(providers), batch)
        # ... then for P's links to the papers in between whose link to R could still matter
        # (a BACKGROUND or CRITICAL link caps the chain below the bar, so those are skipped) ...
        middles = g.ro_query(MIDDLE, {"id": pid, "r": rid}).result_set
        for x, _, onward, *_ in middles:
            if onward in ("FOUNDATIONAL", "SUPPORTING", "UNKNOWN"):
                _evidence(g, x, [pid], list(providers), batch)
        # ... and, where both links are strong, whether P relies on the part of the middle paper
        # that came from R (two votes).
        groups = []
        for x, cls, onward, contexts, ptitle in g.ro_query(MIDDLE, {"id": pid, "r": rid}).result_set:
            if cls in ("FOUNDATIONAL", "SUPPORTING") and onward in ("FOUNDATIONAL", "SUPPORTING") and contexts:
                if grp := carry.group(g, x, rid, [(pid, ptitle, contexts)]):
                    groups.append(grp)
        if groups:
            carry.judge_groups(g, rid, groups)
        tier, ch = _tier(g, pid, rid)
        direct = g.ro_query(DIRECT, {"id": pid, "r": rid}).result_set
        ancestors.append({"id": rid, "title": rtitle, "year": ryear, "tier": tier, "chain": ch,
                          "direct_link": direct[0][0] if direct else None})

    complete = (coverage["references_found"] == coverage["references"]
                and coverage["second_hop_found"] == coverage["second_hop"])
    if ancestors:
        verdict = min((a["tier"] for a in ancestors), key=ORDER.index)
    else:
        verdict = f"CLEAN · DEPTH {DEPTH}" if complete else "NO RETRACTED WORK FOUND"
    result = {**out, "verdict": verdict, "retracted_ancestors": ancestors, "coverage": coverage,
              "coverage_complete": complete, "credits": openalex.credits()["used"] - used}
    g.query("""CREATE (:Investigation {id: $id, mode: 'foundation_check', root: $root, created_at: $t,
               status: 'done', verdict: $verdict, ancestors: $n, credits: $credits})""",
            {"id": uuid.uuid4().hex[:8], "root": pid, "t": _now(), "verdict": verdict,
             "n": len(ancestors), "credits": result["credits"]})
    return result


def summary(r: dict) -> str:
    p = r["paper"]
    lines = [f"{p['title']} ({p['year']}) · {p['id']} · doi {p['doi']}", f"VERDICT: {r['verdict']}"]
    if r.get("why"):
        lines.append(f"  {r['why']}")
    for a in r.get("retracted_ancestors", []):
        lines.append(f"- {a['tier']}: {a['title']} ({a['year']}) {a['id']}")
        for i, link in enumerate((a["chain"] or {}).get("links", [])):
            cited = a["chain"]["papers"][i + 1]
            lines.append(f"    {link['cls']} -> {(cited['title'] or '')[:70]}: \"{(link['evidence'] or 'no citation sentence')[:160]}\"")
            if i > 0 and link.get("carry_votes"):
                lines.append(f"      carries? {', '.join(link['carry_votes'])}")
    if "coverage" in r:
        c = r["coverage"]
        lines.append(f"Checked: {c['references_found']} of {c['references']} references, "
                     f"{c['second_hop_found']} of {c['second_hop']} second-hop references "
                     f"({c['second_hop_skipped_for_budget']} skipped for the credit budget); "
                     f"{c['without_doi']} without a DOI (retraction flag from OpenAlex only). Credits: {r['credits']}.")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paper", help="a DOI, OpenAlex ID or PubMed ID")
    ap.add_argument("--credits", type=int, default=50, help="OpenAlex credits this check may spend")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        result = check(db.graph(), args.paper, max_credits=args.credits)
    except resolve.ResolveError as e:
        raise SystemExit(str(e))
    print(json.dumps(result, indent=1, default=str) if args.json else summary(result))
