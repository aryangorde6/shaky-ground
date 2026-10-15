"""The investigation agent: follows the riskiest chains outward from a retracted paper.

Each round the model sees the riskiest papers whose own citers are not loaded yet (their
risk, how they cite the chain, how much of their downstream is unseen) and picks which to
expand, with a reason. Code checks every pick against the candidates and the budget, then
loads the paper's citers, attaches citation sentences, judges reliance, checks which strong
links carry the retracted part further (carry.py), and propagates the risk again. Everything the agent did is recorded in the graph:

    (:Investigation {id, mode, root, status, ...})-[:EXPLORED {round, reason, ...}]->(:Paper)

    uv run python -m shaky.agent W2075481535 --rounds 2 --per-round 2
"""
import argparse
import json
import math
import uuid
from datetime import datetime, timezone

from falkordb import Graph

from . import carry, db, evidence, judge, llm, load, openalex, resolve, risk

FRONTIER = """
MATCH (p:Paper)
WHERE p.risk >= $threshold AND p.expanded_at IS NULL AND p.cited_by > 0
OPTIONAL MATCH (p)-[c:CITES]->(next:Paper)
WHERE next.risk IS NOT NULL
WITH p, c, next ORDER BY next.risk * c.reliance DESC
WITH p, head(collect(c)) AS link
OPTIONAL MATCH (seen:Paper)-[:CITES]->(p)
WITH p, link, count(seen) AS loaded
RETURN p.id, p.title, p.year, p.cited_by, p.risk, p.is_review, link.cls, link.evidence, loaded, p.risk_proven,
       link.carries
ORDER BY p.risk * log(p.cited_by + 1) DESC
LIMIT $n
"""

PLANNER = """You direct an investigation into how far a retracted paper's influence spreads through the citation graph.

Each round you get the riskiest papers whose own citers have not been loaded yet. "risk" is the product of reliance weights along the paper's riskiest citation chain to the retracted paper (1.0 = it directly uses the retracted work's method or data); citations with no evidence count as 0.3. "proven risk" counts only citations whose sentences show the reliance. Expanding a paper loads every paper that cites it, reads how they cite it, and judges their reliance; it costs about 1 credit per 100 citers plus model calls.

Pick the papers whose expansion is most likely to reveal more work that genuinely depends on the retracted paper: prefer papers that build on it (FOUNDATIONAL or SUPPORTING links) and that many others cite. Weak links (BACKGROUND) rarely carry dependence further, and neither does a link marked "does not carry" (the paper relies on a part of the cited paper that did not come from the retracted work). A paper with an UNKNOWN link is worth expanding only if it is very widely cited. You may stop early if nothing is worth the cost.

Answer in JSON only: {"expand": ["W..."], "reason": "at most 30 words, for the investigation log", "stop": false}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidates_text(rows: list, root_title: str, budget: dict) -> str:
    lines = [f'Retracted paper: "{root_title}".',
             f"Budget left: {budget['expansions']} expansions, about {budget['credits']} credits.", "",
             "Candidates (id | year | cited by | citers loaded | risk | proven risk | link | evidence):"]
    for pid, title, year, cited_by, r, is_review, cls, ev, loaded, proven, carries in rows:
        kind = "review" if is_review else "paper"
        cls = f'{cls} ({"carries" if carries else "does not carry"})' if cls and carries is not None else cls
        lines.append(f'{pid} | {year} | {cited_by} | {loaded} | {r:.2f} | {proven or 0:.2f} | {cls or "-"} | '
                     f'{kind} "{(title or "")[:90]}" | "{(ev or "no citation sentence")[:160]}"')
    return "\n".join(lines)


def _expand(g: Graph, pid: str, judge_providers: list, judge_batch: int) -> dict:
    before = openalex.credits()["used"]
    citers = openalex.citers(pid)
    load.upsert_papers(g, citers)
    load.link_citations(g)
    load.link_retractions(g)
    try:
        ev = evidence.attach_s2(g, pid)
    except SystemExit as e:   # no DOI: nothing to look up, the edges stay UNKNOWN
        ev = {"skipped": str(e)}
    judged = judge.judge_citations(g, pid, judge_providers, judge_batch, 10_000)
    g.query("MATCH (p:Paper {id: $id}) SET p.expanded_at = $t", {"id": pid, "t": _now()})
    return {"citers": len(citers), "credits": openalex.credits()["used"] - before,
            "with_sentences": ev.get("matched_to_edges", 0), "judged": judged["judged"],
            "not_judged": judged["not_judged"]}


def investigate(g: Graph, root_id: str, rounds: int, per_round: int, credits: int,
                judge_providers: list, judge_batch: int) -> dict:
    found = g.ro_query("MATCH (p:Paper {id: $id}) RETURN p.title, p.retracted", {"id": root_id}).result_set
    if not found:
        raise SystemExit(f"{root_id}: not in the graph; load it first (python -m shaky.load {root_id})")
    root_title, retracted = found[0]
    if not retracted:
        raise SystemExit(f"{root_id}: not marked retracted, so there is nothing to trace")

    inv = uuid.uuid4().hex[:8]
    g.query("""MATCH (r:Paper {id: $root}) SET r.expanded_at = coalesce(r.expanded_at, $t)
               CREATE (i:Investigation {id: $id, mode: 'blast_radius', root: $root, created_at: $t,
                       status: 'running', budget_rounds: $rounds, budget_credits: $credits})""",
            {"root": root_id, "id": inv, "t": _now(), "rounds": rounds, "credits": credits})
    budget = {"expansions": rounds * per_round, "credits": credits}
    print(f"investigation {inv}: {risk.propagate(g, root_id)}")

    for rnd in range(1, rounds + 1):
        rows = g.ro_query(FRONTIER, {"n": 12, "threshold": risk.EXPLORE}).result_set
        if not rows:
            print("frontier empty")
            break
        reply = llm.chat_json(PLANNER, _candidates_text(rows, root_title, budget), providers=["groq", "gemini"])
        plan = reply["data"] if isinstance(reply["data"], dict) else {}
        allowed = {r[0]: r[3] for r in rows}
        picks = [p for p in plan.get("expand") or [] if p in allowed][:per_round]   # never act on an id it made up
        reason = str(plan.get("reason", ""))[:300]
        print(f"round {rnd}: picked {picks} ({reason})")
        if plan.get("stop") or not picks:
            break
        for pid in picks:
            est = math.ceil(allowed[pid] / 100)
            if budget["expansions"] <= 0 or est > budget["credits"]:
                print(f"  {pid}: over budget, skipped")
                continue
            done = _expand(g, pid, judge_providers, judge_batch)
            budget["expansions"] -= 1
            budget["credits"] -= done["credits"]
            g.query("""MATCH (i:Investigation {id: $inv}), (p:Paper {id: $pid})
                       CREATE (i)-[:EXPLORED {round: $rnd, reason: $reason, decided_by: $by, at: $t,
                               citers: $citers, credits: $credits, judged: $judged}]->(p)""",
                    {"inv": inv, "pid": pid, "rnd": rnd, "reason": reason, "t": _now(),
                     "by": f"{reply['provider']}:{reply['model']}", **{k: done[k] for k in ("citers", "credits", "judged")}})
            print(f"  {pid}: {done}")
        print(f"  propagate: {carry.settle(g, root_id)}")

    # A provider that was busy leaves edges unjudged: retry once, and report what is still open.
    explored = [r[0] for r in g.ro_query("MATCH (:Investigation {id: $id})-[:EXPLORED]->(p) RETURN p.id",
                                         {"id": inv}).result_set]
    for pid in explored:
        judge.judge_citations(g, pid, judge_providers, judge_batch, 10_000)
    final = carry.settle(g, root_id)
    unjudged = g.ro_query("""MATCH (:Investigation {id: $id})-[:EXPLORED]->(p)<-[c:CITES]-()
        WHERE c.contexts IS NOT NULL AND c.judged_by IS NULL RETURN count(c)""", {"id": inv}).result_set[0][0]
    papers = g.ro_query("MATCH (p:Paper) RETURN count(p)").result_set[0][0]
    tiers = {k: final[k] for k in ("builds_on", "passing", "not_shown", "disputes")}
    g.query("MATCH (i:Investigation {id: $id}) SET i.status = 'done', i.finished_at = $t, i.papers = $n, "
            "i.unjudged = $u, i += $tiers",
            {"id": inv, "t": _now(), "n": papers, "u": unjudged, "tiers": tiers})
    return {"investigation": inv, "papers": papers, **tiers,
            "unjudged_with_sentences": unjudged, "budget_left": budget}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="the retracted paper: a DOI, OpenAlex ID or PubMed ID")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--per-round", type=int, default=2)
    ap.add_argument("--credits", type=int, default=200, help="OpenAlex credits this investigation may spend")
    ap.add_argument("--judge", choices=list(llm.PROVIDERS), default="gemini")
    args = ap.parse_args()
    if args.rounds < 1 or args.per_round < 1:
        ap.error("--rounds and --per-round must be at least 1")
    batch = 130 if args.judge == "gemini" else 12
    try:
        root = resolve.resolve(args.root)["id"]
    except resolve.ResolveError as e:
        raise SystemExit(str(e))
    print(json.dumps(investigate(db.graph(), root, args.rounds, args.per_round, args.credits,
                                 [args.judge], batch), indent=1))
