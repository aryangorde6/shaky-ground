"""A blast-radius report for one retracted paper, built only from what is in the graph.

Every statement in it is a count from a query or a sentence quoted from a citing paper, with
the model that judged it. No model writes any of the text.

    uv run python -m shaky.report 10.1038/nature04533 --out data/reports/lesne.md
"""
import argparse
import pathlib

from falkordb import Graph

from . import db, resolve, risk
from .reliance import WEIGHTS

ROOT = """
MATCH (r:Paper {id: $root})
OPTIONAL MATCH (x:Retraction)-[:RETRACTS]->(r)
RETURN r.title, r.year, r.venue, r.doi, r.cited_by, r.retracted, collect(x.date), collect(x.reason)
"""

COVERAGE = """
MATCH (a:Paper)-[c:CITES]->(:Paper {id: $root})
RETURN count(c), sum(CASE WHEN c.contexts IS NOT NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN c.judged_by IS NOT NULL THEN 1 ELSE 0 END)
"""

EXPANDED = """
MATCH (p:Paper) WHERE p.risk_root = $root AND p.id <> $root AND p.expanded_at IS NOT NULL
RETURN p.id, p.title, p.year ORDER BY p.expanded_at
"""

BUILDS_ON = """
MATCH (p:Contaminated) WHERE p.risk_root = $root
RETURN p.id, p.title, p.year, p.cited_by, p.retracted ORDER BY p.cited_by DESC
"""

RETRACTED_DOWNSTREAM = """
MATCH (p:Paper) WHERE p.risk_root = $root AND p.id <> $root AND p.retracted
OPTIONAL MATCH (p)-[c:CITES]->(:Paper {id: $root})
RETURN p.id, p.title, p.year, coalesce(p.risk_proven, 0), c.cls ORDER BY coalesce(p.risk_proven, 0) DESC, p.year
"""

DISPUTES = """
MATCH (d:Paper)-[c:CITES {cls: 'CRITICAL'}]->(:Paper {id: $root}) WHERE d.type <> 'retraction'
RETURN d.title, d.year, d.type, c.evidence, c.judged_by ORDER BY d.year
"""

NOT_SHOWN = """
MATCH (p:Paper) WHERE p.risk_root = $root AND p.id <> $root AND coalesce(p.risk_proven, 0) = 0
OPTIONAL MATCH (p)-[c:CITES]->(:Paper {id: $root})
RETURN p.id, p.title, p.year, p.cited_by, p.is_review, c IS NOT NULL ORDER BY p.cited_by DESC LIMIT $n
"""

EXPLORED = """
MATCH (i:Investigation {root: $root})-[e:EXPLORED]->(p:Paper)
RETURN i.id, i.created_at, e.round, p.id, p.title, e.reason, e.decided_by, e.citers, e.credits, e.judged
ORDER BY i.created_at, e.round
"""

CLASS_MEANING = {
    "FOUNDATIONAL": "uses its method, data or result",
    "SUPPORTING": "relies on its finding as evidence",
    "BACKGROUND": "mentions it in passing",
    "CRITICAL": "disputes it or reports the retraction",
    "UNKNOWN": "no citation sentence available, or the sentences don't show how it is used",
}


def _q(text: str | None, n: int = 300) -> str:
    """A quoted sentence on one line, shortened."""
    text = " ".join((text or "").split())
    return f"“{text[:n]}{'…' if len(text) > n else ''}”"


def _title(t: str | None) -> str:
    return " ".join((t or "(untitled)").split()).replace("|", "/")


def _model(judged_by: str | None) -> str:
    """'gemini:gemini-3.8-flash:v3' -> 'gemini-3.8-flash'"""
    parts = (judged_by or "").split(":")
    return parts[1] if len(parts) > 1 else "loader"


def build(g: Graph, root_id: str, limit: int = 30) -> str:
    q = lambda query, **p: g.ro_query(query, {"root": root_id, **p}).result_set
    rows = q(ROOT)
    if not rows:
        raise SystemExit(f"{root_id} is not in the graph; load it first (python -m shaky.load {root_id})")
    title, year, venue, doi, cited_by, retracted, dates, reasons = rows[0]
    t = risk.tiers(g, root_id)
    loaded, with_sentences, judged = q(COVERAGE)[0]
    expanded = q(EXPANDED)
    builds = q(BUILDS_ON)
    chains = {pid: risk.chain(g, pid, root_id) for pid, *_ in builds}
    direct = sum(1 for c in chains.values() if c and len(c["links"]) == 1)
    retracted_builders = sum(1 for *_, r in builds if r)

    out = [f"# Shaky Ground report: {_title(title)}", "",
           f"{year}, *{venue}*. DOI [{doi}](https://doi.org/{doi}) · OpenAlex [{root_id}](https://openalex.org/{root_id})", ""]
    if dates and dates[0]:
        out += [f"**Retracted {max(dates)}** (Retraction Watch). Reasons: {reasons[0]}.", ""]
    elif retracted:
        out += ["**Marked retracted by OpenAlex** (no Retraction Watch notice matched by DOI).", ""]
    else:
        out += ["**Not marked retracted** by OpenAlex or Retraction Watch.", ""]

    out += ["## Answer", "",
            f"- **{t['builds_on']} papers build on it**: each has a chain of citations in which every link was judged "
            f"FOUNDATIONAL (uses its method, data or result) or SUPPORTING (relies on its finding), from the citing "
            f"paper's own sentences. {direct} cite it directly"
            + (f"; {t['builds_on'] - direct} through another paper, where two models agreed that they rely on the "
               f"part of that paper that came from it." if t['builds_on'] > direct else "."),
            f"- **{retracted_builders} of those were themselves retracted.**",
            f"- **{t['passing']:,} cite it only in passing**: every chain with evidence has a BACKGROUND link.",
            f"- **{t['disputes']} dispute it** or report the concerns about it.",
            f"- **{t['not_shown']:,} are not shown either way**: no chain has usable evidence on every link. "
            f"They are neither cleared nor counted as building on it.", "",
            "## What was checked", "",
            f"- OpenAlex lists **{cited_by:,}** works citing it; **{loaded:,}** are loaded. "
            f"**{int(with_sentences):,}** have citation sentences (Semantic Scholar), and **{int(judged):,}** of those are judged.",
            f"- Chains up to {risk.MAX_HOPS} citations long. Papers whose own citers were loaded too: "
            f"**{len(expanded)}**" + (": " + "; ".join(f"{_title(t_)[:70]} ({y})" for _, t_, y in expanded) if expanded else "") + ".",
            "- Papers that are not loaded are not counted anywhere in this report.", ""]

    out += [f"## Papers that build on it ({t['builds_on']})", ""]
    ordered = sorted(builds, key=lambda b: (len(chains[b[0]]["links"]) if chains[b[0]] else 9, -(b[3] or 0)))
    for pid, ptitle, pyear, pcited, pretracted in ordered[:limit]:
        c = chains[pid]
        flag = " · **RETRACTED**" if pretracted else ""
        out.append(f"### {_title(ptitle)} ({pyear}){flag}")
        out.append(f"[{pid}](https://openalex.org/{pid}) · cited by {pcited:,} · chain risk {c['risk']:.2f}" if c else pid)
        for i, link in enumerate(c["links"] if c else []):
            cited = c["papers"][i + 1]
            out.append(f"- **{link['cls']}** → *{_title(cited['title'])[:80]}* ({cited['year']}): {_q(link['evidence'])} "
                       f"· judged by `{_model(link['judged_by'])}`")
            if i > 0:
                votes = ", ".join(link["carry_votes"] or [])
                out.append(f"  - relies on the part that came from the retracted paper ({votes}): "
                           f"{_q(link['carry_evidence'], 200)}")
        out.append("")
    if len(builds) > limit:
        out += [f"…and {len(builds) - limit} more (run with `--limit {len(builds)}`).", ""]

    down = q(RETRACTED_DOWNSTREAM)
    out += [f"## Retracted papers downstream ({len(down)})", "", "| Paper | Year | Tier | Its link to the root |", "|---|---|---|---|"]
    for pid, ptitle, pyear, proven, cls in down:
        tier = "builds on" if proven >= risk.BUILDS_ON else "passing" if proven > 0 else "not shown"
        out.append(f"| [{_title(ptitle)[:90]}](https://openalex.org/{pid}) | {pyear} | {tier} | {cls or 'through another paper'} |")
    out.append("")

    disputes = q(DISPUTES)
    out += [f"## Papers that dispute it ({len(disputes)})", ""]
    for dtitle, dyear, dtype, ev, by in disputes[:limit]:
        out.append(f"- *{_title(dtitle)[:100]}* ({dyear}, {dtype}): {_q(ev, 220)} · `{_model(by)}`")
    out.append("")

    unknown = q(NOT_SHOWN, n=10)
    out += ["## Not shown: the most-cited papers without evidence", "",
            "These might build on it or might not: no citation sentence was available, or the sentences didn't "
            "show how it is used. Citation counts are OpenAlex's.", ""]
    for pid, ptitle, pyear, pcited, review, cites_root in unknown:
        how = "cites it directly" if cites_root else "linked through another paper"
        out.append(f"- *{_title(ptitle)[:100]}* ({pyear}{', review' if review else ''}) · cited by {pcited:,} · {how}")
    out.append("")

    explored = q(EXPLORED)
    if explored:
        out += ["## What the agent explored", "", "| Investigation | Round | Paper expanded | Why (the agent's words) | Citers | Credits |",
                "|---|---|---|---|---|---|"]
        for inv, at, rnd, pid, ptitle, reason, by, citers, credits, _ in explored:
            out.append(f"| `{inv}` {at[:10]} | {rnd} | {_title(ptitle)[:60]} | {(reason or '').replace('|', '/')} "
                       f"(`{by.split(':')[-1]}`) | {citers} | {credits} |")
        out.append("")

    out += ["## How to read this", "",
            "Each citation is judged from the sentences in which the citing paper cites the cited one. A model answers "
            "yes/no questions about those sentences; fixed rules turn the answers into a class, and a table turns the "
            "class into a weight. The risk of a chain is the product of its weights.", "",
            "| Class | Meaning | Weight |", "|---|---|---|"]
    out += [f"| {cls} | {CLASS_MEANING[cls]} | {w} |" for cls, w in WEIGHTS.items()]
    out += ["", f"A paper builds on the retracted one if its proven risk is at least {risk.BUILDS_ON}: with these weights, "
            "that means every link of some chain is FOUNDATIONAL or SUPPORTING. Beyond the first link, a link counts "
            "only if two different models agreed that it relies on the part of the cited paper that came from the "
            "retracted work. Shaky Ground scores papers, never people.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="the retracted paper: a DOI, OpenAlex ID or PubMed ID")
    ap.add_argument("--limit", type=int, default=30, help="papers listed per section")
    ap.add_argument("--out", help="write the report here instead of printing it")
    args = ap.parse_args()
    try:
        root = resolve.resolve(args.root)["id"]
    except resolve.ResolveError as e:
        raise SystemExit(str(e))
    text = build(db.graph(), root, args.limit)
    if args.out:
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    else:
        print(text)
