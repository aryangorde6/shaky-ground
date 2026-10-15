"""Does dependence carry through a chain? Only if each later link relies on the part that came from the retracted paper.

Mainardi et al. 2014 extracted proteins with a buffer adapted from Lesné et al. 2006, so its link
to Lesné is FOUNDATIONAL. A paper that cites Mainardi for its electrophysiology relies on
Mainardi, but not on anything Mainardi took from Lesné. So for every FOUNDATIONAL or SUPPORTING
link A -> B where B already builds on the retracted root, two different models each get B's
sentences citing its upstream paper (what B took) and A's sentences citing B, and answer: does
A rely on that same part of B?

    CITES.carries      true only if both models say yes, false only if both say no, else null
    CITES.carry_votes  each model's answer, e.g. ["groq:yes", "gemini:no"]
    CITES.carry_root   the root it was judged for; risk.propagate ignores judgments for other roots

Until a link is shown to carry, risk.propagate counts it at most as a passing mention (0.15),
so a paper is proven to build on the root only through links that were each checked.

    uv run python -m shaky.carry W2075481535
"""
import argparse
from datetime import datetime, timezone

from falkordb import Graph

from . import db, llm, risk

VERSION = 5
JUDGES = ("groq", "gemini")   # two model families; one model alone flipped answers between runs
EFFORT = "medium"             # at low effort a model wrote the right phrases but often the wrong answer
# Citations per call and max output tokens. Groq counts max_tokens against its 8K tokens a minute,
# so prompt + output must fit; Gemini takes large batches but allows only 20 calls a day.
LIMITS = {"groq": (8, 4500), "gemini": (60, 32000)}
SYSTEM = """A paper B depends on an earlier paper that was later retracted. You get the sentences in which B cites that paper, and the sentences in which other papers cite B. Most papers report several results; usually only some of them rest on what B took.

First, for each B, say in at most 15 words what B took from the retracted paper and what B used it for ("took").
Then, for each citing paper:
- "cited_for": in at most 12 words, the result, method or data of B that the citing paper uses.
- "same_part": true only if cited_for is what B took, or a result B obtained with it. A different result of B, even on the same topic, is not the same part.
- "unclear": true if the sentences do not show what B is cited for.
- "sentence": the 0-based index of the sentence that shows it.

Example: B took an extraction buffer from the retracted paper to measure protein X, and also recorded brain waves.
- "Exercise lowers protein X in old mice (B)" -> same_part (measured with that buffer)
- "Exercise changes brain waves (B)" -> not same_part (a different result of B)
- "Electrodes were implanted as in B" -> not same_part (B's electrode method did not come from the retracted paper)

Answer in JSON only, with exactly one judgment per citation id:
{"took": [{"b": "B1", "took": "..."}], "judgments": [{"id": "e1", "cited_for": "...", "same_part": false, "unclear": false, "sentence": 0}]}"""

# Strong links into papers that already build on the root, not yet judged for this root by this VERSION.
TO_JUDGE = """
MATCH (a:Paper)-[c:CITES]->(b:Paper)
WHERE b.risk_root = $root AND b.id <> $root AND b.risk_proven >= $t
  AND c.cls IN ['FOUNDATIONAL', 'SUPPORTING']
  AND NOT (coalesce(c.carry_root, '') = $root AND coalesce(c.carry_by, '') ENDS WITH $version)
RETURN b.id, a.id, a.title, c.contexts
ORDER BY b.id, a.cited_by DESC
"""

# What B took from upstream: the link on B's own proven chain (same effective weight as risk.propagate).
UPSTREAM = """
MATCH (b:Paper {id: $b})-[u:CITES]->(x:Paper)
WHERE u.cls IN ['FOUNDATIONAL', 'SUPPORTING'] AND (x.id = $root OR (u.carry_root = $root AND u.carries = true))
  AND abs(x.risk_proven * u.reliance - b.risk_proven) < 1e-9
RETURN b.title, x.id, x.title, u.cls, coalesce(u.contexts, [u.evidence])
LIMIT 1
"""

SAVE = """
UNWIND $rows AS row
MATCH (a:Paper {id: row.src})-[c:CITES]->(b:Paper {id: row.dst})
SET c.carries = row.carries, c.carry_root = $root, c.carry_via = row.via, c.carry_votes = row.votes,
    c.carry_evidence = row.evidence, c.carry_why = row.why, c.carry_by = row.by, c.carry_at = row.at
"""

MAX_SENTENCES = 4
MAX_CHARS = 500


def _prompt(groups: list) -> tuple[str, list]:
    """groups: [(upstream, [(src, title, contexts), ...]), ...] -> prompt text, and the citation behind each eN."""
    lines, ids = [], []
    for n, (up, citers) in enumerate(groups, 1):
        b_title, _, x_title, cls, took = up
        lines += [f'B{n}: "{(b_title or "")[:120]}"', f'  B{n} cites "{(x_title or "")[:100]}" ({cls}):']
        lines += [f"    - {sentence.strip()[:MAX_CHARS]}" for sentence in took[:MAX_SENTENCES]]
        lines.append(f"  Papers citing B{n}:")
        for src, title, contexts in citers:
            ids.append((src, contexts))
            lines.append(f'  e{len(ids)} (citing paper: "{(title or "")[:120]}")')
            for j, sentence in enumerate(contexts[:MAX_SENTENCES]):
                lines.append(f"    [{j}] {sentence.strip()[:MAX_CHARS]}")
        lines.append("")
    return "\n".join(lines), ids


def _answers(reply: dict, ids: list) -> dict:
    """eN index -> {"carries": True/False/None, "sentence", "cited_for", "took"}; malformed entries are dropped."""
    data = reply["data"]
    items = data if isinstance(data, list) else data.get("judgments") or []
    took = {str(t.get("b")): str(t.get("took", ""))
            for t in (data.get("took") or [] if isinstance(data, dict) else []) if isinstance(t, dict)}
    out = {}
    for j in items:
        if not isinstance(j, dict):
            continue
        try:
            i = int(str(j["id"]).lstrip("e")) - 1
            k = int(j["sentence"])
        except (KeyError, ValueError, TypeError):
            continue
        if not 0 <= i < len(ids) or i in out or not 0 <= k < min(len(ids[i][1]), MAX_SENTENCES):
            continue
        carries = True if j.get("same_part") is True else None if j.get("unclear") is True else False
        out[i] = {"carries": carries, "sentence": k, "cited_for": str(j.get("cited_for", ""))[:120], "took": took}
    return out


def _combine(votes: list) -> bool | None:
    """Both yes -> carries; both no -> does not; any disagreement or unclear -> not shown either way."""
    if all(v is True for v in votes):
        return True
    if all(v is False for v in votes):
        return False
    return None


def _groups(g: Graph, root_id: str, todo: list, stats: dict) -> list:
    """[(B, B's upstream link, [(src, title, contexts), ...]), ...] for every B with a checked upstream link."""
    by_b = {}
    for b, src, title, contexts in todo:
        by_b.setdefault(b, []).append((src, title, contexts))
    groups = []
    for b, citers in by_b.items():
        up = g.ro_query(UPSTREAM, {"b": b, "root": root_id}).result_set
        if up:
            groups.append((b, up[0], citers))
        else:                       # no checked upstream link: nothing to compare against
            stats["not_judged"] += len(citers)
    return groups


def _batches(groups: list, size: int) -> list:
    """At most size citations per call; a large group is split, each part repeating B's header."""
    batches, current, n = [], [], 0
    for b, up, citers in groups:
        for start in range(0, len(citers), size):
            part = citers[start:start + size]
            if current and n + len(part) > size:
                batches.append(current)
                current, n = [], 0
            current.append((b, up, part))
            n += len(part)
    return batches + ([current] if current else [])


def _vote(judge: str, groups: list, stats: dict) -> dict:
    """One judge's answers: (src, B) -> answer. Each judge batches on its own; links it could not answer are left out."""
    size, max_out = LIMITS[judge]
    out = {}
    for batch in _batches(groups, size):
        text, ids = _prompt([(up, citers) for _, up, citers in batch])
        links = [(b, up[1], f"B{n}") for n, (b, up, citers) in enumerate(batch, 1) for _ in citers]   # per eN
        try:
            reply = llm.chat_json(SYSTEM, text, providers=[judge], max_tokens=max_out, effort=EFFORT)
        except llm.LLMError as e:   # no fallback: a stand-in model would not be an independent vote
            print(f"  {judge} stopped: {e}")
            break
        stats["calls"] += 1
        for i, answer in _answers(reply, ids).items():
            b, via, label = links[i]
            out[(ids[i][0], b)] = {**answer, "via": via, "took": answer["took"].get(label, ""),
                                   "evidence": ids[i][1][answer["sentence"]],
                                   "by": f"{reply['provider']}:{reply['model']}"}
    return out


def judge_carry(g: Graph, root_id: str, judges=JUDGES) -> dict:
    """Judge every strong link into a paper that already builds on the root (see TO_JUDGE)."""
    todo = g.ro_query(TO_JUDGE, {"root": root_id, "t": risk.BUILDS_ON, "version": f":c{VERSION}"}).result_set
    stats = {"to_judge": len(todo), "carries": 0, "does_not": 0, "not_shown": 0, "not_judged": 0, "calls": 0}
    return judge_groups(g, root_id, _groups(g, root_id, todo, stats), judges, stats)


# One given link B -> X, as the "what B took" part of a group (for chains that were not propagated).
LINK = """
MATCH (b:Paper {id: $b})-[u:CITES]->(x:Paper {id: $x})
RETURN b.title, x.id, x.title, u.cls, coalesce(u.contexts, [u.evidence])
"""


def group(g: Graph, b: str, x: str, citers: list) -> tuple | None:
    """(B, B's link to X, citers) for judge_groups; citers are (src, title, contexts)."""
    rows = g.ro_query(LINK, {"b": b, "x": x}).result_set
    return (b, rows[0], citers) if rows else None


def judge_groups(g: Graph, root_id: str, groups: list, judges=JUDGES, stats: dict | None = None) -> dict:
    """Both judges vote on every citer in groups; the combined answers are saved for root_id."""
    if stats is None:
        stats = {"to_judge": sum(len(c) for _, _, c in groups), "carries": 0, "does_not": 0, "not_shown": 0,
                 "not_judged": 0, "calls": 0}
    votes = {judge: _vote(judge, groups, stats) for judge in judges}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for b, _, citers in groups:
        for src, _, _ in citers:
            answers = [votes[judge].get((src, b)) for judge in judges]
            if None in answers:
                stats["not_judged"] += 1      # retried next time
                continue
            first = answers[0]
            carries = _combine([a["carries"] for a in answers])
            rows.append({"src": src, "dst": b, "via": first["via"], "carries": carries,
                         "votes": [f"{j}:{'yes' if a['carries'] else 'unclear' if a['carries'] is None else 'no'}"
                                   for j, a in zip(judges, answers)],
                         "evidence": first["evidence"],
                         "why": f'cited for: {first["cited_for"]}; B took: {first["took"][:150]}',
                         "by": "+".join(a["by"] for a in answers) + f":c{VERSION}", "at": now})
            stats["carries" if carries else "not_shown" if carries is None else "does_not"] += 1
    if rows:
        g.query(SAVE, {"rows": rows, "root": root_id})
    return stats


def settle(g: Graph, root_id: str, judges=JUDGES) -> dict:
    """Propagate, then check the links that could carry dependence one hop further, until none are left."""
    result = risk.propagate(g, root_id)
    for _ in range(risk.MAX_HOPS - 1):
        done = judge_carry(g, root_id, judges)
        print(f"  carry: {done}")
        if done["to_judge"] == 0 or done["to_judge"] == done["not_judged"]:
            break
        result = risk.propagate(g, root_id)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--judges", nargs="+", choices=list(LIMITS), default=list(JUDGES))
    args = ap.parse_args()
    print(settle(db.graph(), args.root, args.judges))
