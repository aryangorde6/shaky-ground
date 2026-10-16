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
    uv run python -m shaky.foundation --list references.bib     # every DOI in a reference list
"""
import argparse
import json
import pathlib
import re
import uuid
from datetime import datetime, timezone

from falkordb import Graph

from . import carry, db, evidence, judge, load, openalex, resolve, risk
from .fetch import FetchError

DEPTH = 2
ORDER = ["RESTS ON RETRACTED WORK", "NOT PROVEN", "CITES RETRACTED WORK"]   # worst first
VERDICTS = {    # what each verdict means, for the UI and the MCP server
    "RESTS ON RETRACTED WORK": "A chain to a retracted paper has every link FOUNDATIONAL or SUPPORTING; "
                               "a second link was also shown, by two models, to carry the retracted part.",
    "NOT PROVEN": "A retracted paper is in reach and a chain to it could reach that bar, "
                  "but some link on it has no usable evidence.",
    "CITES RETRACTED WORK": "Every chain to retracted work has a passing (BACKGROUND) or critical link.",
    "NO RETRACTED WORK FOUND": "None in the references that could be checked. Not a clean bill: some could not be checked.",
    f"CLEAN · DEPTH {DEPTH}": f"No retracted work, and every reference to depth {DEPTH} was checked.",
    "NO DATA": "OpenAlex has no reference list for this paper, so nothing could be checked.",
}

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
        return _record(g, {**out, "verdict": "NO DATA", "why": "OpenAlex has no reference list for this paper",
                           "retracted_ancestors": [], "credits": openalex.credits()["used"] - used})
    coverage = _load(g, p, max_credits)

    ancestors = []
    for rid, rtitle, ryear in g.ro_query(ANCESTORS, {"id": pid}, timeout=30_000).result_set:
        middles = g.ro_query(MIDDLE, {"id": pid, "r": rid}, timeout=30_000).result_set
        # Evidence and judgments for the links into R, from P and from the papers in between ...
        _evidence(g, rid, [pid, *[m[0] for m in middles]], list(providers), batch)
        # ... then for P's links to the papers in between whose link to R could still matter
        # (a BACKGROUND or CRITICAL link caps the chain below the bar, so those are skipped) ...
        middles = g.ro_query(MIDDLE, {"id": pid, "r": rid}, timeout=30_000).result_set
        for x, _, onward, *_ in middles:
            if onward in ("FOUNDATIONAL", "SUPPORTING", "UNKNOWN"):
                _evidence(g, x, [pid], list(providers), batch)
        # ... and, where both links are strong, whether P relies on the part of the middle paper
        # that came from R (two votes).
        groups = []
        for x, cls, onward, contexts, ptitle in g.ro_query(MIDDLE, {"id": pid, "r": rid}, timeout=30_000).result_set:
            if cls in ("FOUNDATIONAL", "SUPPORTING") and onward in ("FOUNDATIONAL", "SUPPORTING") and contexts:
                if grp := carry.group(g, x, rid, [(pid, ptitle, contexts)]):
                    groups.append(grp)
        if groups:
            carry.judge_groups(g, rid, groups)
        tier, ch = _tier(g, pid, rid)
        direct = g.ro_query(DIRECT, {"id": pid, "r": rid}, timeout=30_000).result_set
        ancestors.append({"id": rid, "title": rtitle, "year": ryear, "tier": tier, "chain": ch,
                          "direct_link": direct[0][0] if direct else None})

    complete = (coverage["references_found"] == coverage["references"]
                and coverage["second_hop_found"] == coverage["second_hop"])
    if ancestors:
        verdict = min((a["tier"] for a in ancestors), key=ORDER.index)
    else:
        verdict = f"CLEAN · DEPTH {DEPTH}" if complete else "NO RETRACTED WORK FOUND"
    return _record(g, {**out, "verdict": verdict, "retracted_ancestors": ancestors, "coverage": coverage,
                       "coverage_complete": complete, "credits": openalex.credits()["used"] - used})


def _record(g: Graph, result: dict) -> dict:
    """Store the check as an Investigation. The whole result is kept, so the UI and the MCP server
    can show a past check without redoing it."""
    result["investigation"] = uuid.uuid4().hex[:8]
    g.query("""CREATE (:Investigation {id: $id, mode: 'foundation_check', root: $root, created_at: $t,
               status: 'done', verdict: $verdict, ancestors: $n, credits: $credits, result: $result})""",
            {"id": result["investigation"], "root": result["paper"]["id"], "t": _now(), "verdict": result["verdict"],
             "n": len(result["retracted_ancestors"]), "credits": result["credits"],
             "result": json.dumps(result, default=str)})
    return result


def summary(r: dict) -> str:
    p = r["paper"]
    lines = [f"{p['title']} ({p['year']}) · {p['id']} · doi {p['doi']}", f"VERDICT: {r['verdict']}"]
    if r.get("why"):
        lines.append(f"  {r['why']}")
    for a in r.get("retracted_ancestors", []):
        lines.append(f"- {a['tier']}: {a['title']} ({a['year']}) {a['id']}")
        links = (a["chain"] or {}).get("links", [])
        for i, link in enumerate(links):
            cited = a["chain"]["papers"][i + 1]
            lines.append(f"    {link['cls']} -> {(cited['title'] or '')[:70]}: \"{(link['evidence'] or 'no citation sentence')[:160]}\"")
            if i < len(links) - 1 and link.get("carry_votes"):
                lines.append(f"      carries? {', '.join(link['carry_votes'])}")
    if "coverage" in r:
        c = r["coverage"]
        lines.append(f"Checked: {c['references_found']} of {c['references']} references, "
                     f"{c['second_hop_found']} of {c['second_hop']} second-hop references "
                     f"({c['second_hop_skipped_for_budget']} skipped for the credit budget); "
                     f"{c['without_doi']} without a DOI (retraction flag from OpenAlex only). Credits: {r['credits']}.")
    return "\n".join(lines)


BIB_ENTRY = re.compile(r"^\s*@\s*(?!comment|string|preamble)\w+\s*\{", re.IGNORECASE | re.MULTILINE)


def dois_in(text: str) -> tuple[list[str], int]:
    """The DOIs in a pasted reference list or BibTeX file, in order and without repeats, and how many
    entries carry none (those can't be looked up, so they are reported as not checked). Entries are
    BibTeX entries when there are any, else the items of a plain list (see _text_entries)."""
    found = []
    for m in resolve.DOI.finditer(text):
        doi = m.group(1).rstrip(".,;)]}\"'").lower()
        if doi not in found:
            found.append(doi)
    entries = _bib_entries(text) or _text_entries(text)
    return found, sum(1 for e in entries if not resolve.DOI.search(e))


LIST_MARK = re.compile(r"^\s*(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)])\s")
LIST_HEADING = re.compile(r"^\s*(?:references|bibliography|works cited|literature cited)\s*:?\s*$", re.IGNORECASE)


def _text_entries(text: str) -> list[str]:
    """The references in a plain pasted list: its numbered items ([1], (1), 1. or 1)) when it is numbered,
    else its blocks when blank lines separate them, else one per line. A heading line is not a reference.
    When unsure, this splits too finely rather than too coarsely: an extra entry without a DOI only stops
    the list from being called CLEAN, while a missed one would let it be."""
    lines = [l for l in text.splitlines() if l.strip() and not LIST_HEADING.match(l)]
    if sum(1 for l in lines if LIST_MARK.match(l)) >= 2:
        items = []
        for l in lines:
            if LIST_MARK.match(l) or not items:
                items.append(l)
            else:
                items[-1] += " " + l.strip()
        return items
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip() and not LIST_HEADING.match(b)]
    return blocks if len(blocks) > 1 else lines


def _bib_entries(text: str) -> list[str]:
    """Each BibTeX entry's text, from @type{ to its matching closing brace."""
    out = []
    for m in BIB_ENTRY.finditer(text):
        depth, i = 0, m.end() - 1
        for i in range(m.end() - 1, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                break
        out.append(text[m.start():i + 1])
    return out


MAX_LIST = 100      # DOIs in one reference list


def check_list(g: Graph, text: str, max_credits: int = 200, stored=None, known=None, run: bool = True, **kw) -> dict:
    """A foundation check of every reference in a list, within one OpenAlex credit budget. The list's
    verdict is its worst entry's; entries that could not be checked are counted, never cleared.

    stored(doi) may return an earlier check of a reference to reuse instead of running it again; with
    run=False (a read-only server), a reference with no earlier check is NOT CHECKED, but known(doi) can
    still say whether that paper is retracted itself. A list that cites a retracted paper directly is at
    least CITES RETRACTED WORK."""
    dois, no_doi = dois_in(text)
    if not dois:
        raise resolve.ResolveError("no DOIs found: paste a reference list or BibTeX with DOIs in it")
    if len(dois) > MAX_LIST:
        raise resolve.ResolveError(f"{len(dois)} DOIs found; a list can hold at most {MAX_LIST}")
    entries, left = [], max_credits
    for doi in dois:
        r = stored(doi) if stored else None
        reused = r is not None
        if r is None:
            if not run:
                p = known(doi) if known else None
                entries.append({"doi": doi, "verdict": "NOT CHECKED",
                                "why": "not checked on this server before, and it is read only",
                                **({"id": p["id"], "title": p["title"], "year": p["year"],
                                    "paper_retracted": bool(p["retracted"])} if p else {})})
                continue
            if left <= 0:
                entries.append({"doi": doi, "verdict": "NOT CHECKED", "why": "credit budget used up"})
                continue
            try:
                r = check(g, doi, max_credits=left, **kw)
            except (resolve.ResolveError, FetchError) as e:
                entries.append({"doi": doi, "verdict": "NOT CHECKED", "why": str(e)})
                continue
            left -= r["credits"]
        ancestors = r.get("retracted_ancestors") or []
        entries.append({"doi": doi, "id": r["paper"]["id"], "title": r["paper"]["title"], "year": r["paper"].get("year"),
                        "verdict": r["verdict"], "paper_retracted": r.get("paper_retracted"),
                        "investigation": r.get("investigation"), "reused": reused,
                        "retracted_ancestors": len(ancestors),
                        "ancestors": [{k: a.get(k) for k in ("id", "title", "year", "tier")} for a in ancestors]})
    worst = ["RESTS ON RETRACTED WORK", "NOT PROVEN", "CITES RETRACTED WORK"]
    verdicts = [e["verdict"] for e in entries]
    retracted = sum(1 for e in entries if e.get("paper_retracted"))
    overall = next((v for v in worst if v in verdicts or (retracted and v == "CITES RETRACTED WORK")), None)
    if overall is None:
        overall = "NO RETRACTED WORK FOUND" if verdicts.count(f"CLEAN · DEPTH {DEPTH}") < len(entries) or no_doi \
            else f"CLEAN · DEPTH {DEPTH}"
    return {"verdict": overall, "references": len(dois) + no_doi, "with_doi": len(dois), "without_doi": no_doi,
            "cited_papers_retracted": retracted, "counts": {v: verdicts.count(v) for v in dict.fromkeys(verdicts)},
            "reused": sum(1 for e in entries if e.get("reused")), "entries": entries, "credits": max_credits - left}


def list_summary(r: dict) -> str:
    lines = [f"VERDICT for the list: {r['verdict']}",
             f"{r['references']} references: {r['with_doi']} with a DOI, {r['without_doi']} without (not checked); "
             f"{r['cited_papers_retracted']} of the cited papers are retracted themselves. Credits: {r['credits']}."]
    for e in r["entries"]:
        lines.append(f"- {e['verdict']}: {(e.get('title') or e['doi'])[:80]} ({e['doi']})"
                     + (" · ITSELF RETRACTED" if e.get("paper_retracted") else "")
                     + (" · earlier check" if e.get("reused") else "")
                     + (f" · {e['why']}" if e.get("why") else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paper", nargs="?", help="a DOI, OpenAlex ID or PubMed ID")
    ap.add_argument("--list", metavar="FILE", help="check every DOI in a reference list or BibTeX file instead")
    ap.add_argument("--credits", type=int, default=50, help="OpenAlex credits this check may spend (the whole list's budget with --list)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if bool(args.paper) == bool(args.list):
        ap.error("give either a paper or --list FILE")
    try:
        if args.list:
            result = check_list(db.graph(), pathlib.Path(args.list).read_text(), max_credits=args.credits)
        else:
            result = check(db.graph(), args.paper, max_credits=args.credits)
    except resolve.ResolveError as e:
        raise SystemExit(str(e))
    if args.json:
        print(json.dumps(result, indent=1, default=str))
    else:
        print(list_summary(result) if args.list else summary(result))
