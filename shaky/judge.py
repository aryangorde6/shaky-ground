"""Judge reliance: how much does each citing paper rely on the paper it cites?

The model sees only the citation sentences and answers yes/no questions about them (does it
dispute it? use its method or data? rely on its finding?) plus the number of the sentence
that shows it. Fixed rules turn the answers into a class, reliance.WEIGHTS turns the class
into a weight, and the code copies the sentence verbatim as the evidence. An edge that was
judged is never sent again.

    uv run python -m shaky.judge W2075481535 --provider groq --batch 12 --limit 24
    uv run python -m shaky.judge W2075481535 --provider gemini --batch 150
"""
import argparse
import re
from datetime import datetime, timezone

from falkordb import Graph

from . import db, llm
from .reliance import WEIGHTS

# The model answers concrete questions; the class follows from fixed rules (classify below),
# and the weight from reliance.WEIGHTS. Bump VERSION when the prompt or the rules change.
VERSION = 3
SYSTEM = """You read the sentences in which a citing paper cites a cited paper, and answer questions about them.

For each citation, answer:
- "critical": true if the citing paper disputes the cited paper, fails to reproduce or observe what it reported, questions its data, or mentions its retraction or the concerns about it.
- "uses": true if the citing paper uses the cited paper's method, protocol, buffer, antibody, data, mouse line, model or result in its own work (signs: "adapted from", "as described in", "following", "according to", "using the method of").
- "relies": true if the citing paper presents the cited paper's specific finding as evidence for a claim its own argument depends on. A citation that is one of a list of references for a general statement is NOT relied on.
- "unclear": true if the sentences do not show how the cited paper is used (for example, the text is cut off or seems to be about a different reference).
- "sentence": the 0-based index of the sentence that best supports your answers.
- "why": at most 12 words.

Examples:
- "Proteins were extracted with a buffer adapted from Smith et al." -> uses
- "We did not observe the 56 kDa band reported by Smith et al." -> critical
- "Smith et al. showed that X impairs memory, which led us to test whether..." -> relies
- "X has been implicated in synaptic loss (Jones; Smith et al.; Lee)." -> none of them

Answer in JSON only, with exactly one entry per citation id:
{"judgments": [{"id": "e1", "critical": false, "uses": false, "relies": false, "unclear": false, "sentence": 0, "why": "..."}]}"""


YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")                    # author-year: (Lesne et al., 2006)
NUMBERED = re.compile(r"[\[(]\s*\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*\s*[\])]")   # numbered: [4, 5, 17–20]
LIST_SIZE = 3


def count_refs(sentence: str) -> int:
    """How many references a sentence cites (a lower bound: superscripts are missed)."""
    n = len(YEAR.findall(sentence))
    for group in NUMBERED.findall(sentence):
        for part in re.findall(r"\d{1,3}(?:\s*[–-]\s*\d{1,3})?", group):
            ends = [int(x) for x in re.findall(r"\d{1,3}", part)]
            n += min(ends[-1] - ends[0] + 1, 10) if len(ends) == 2 and ends[1] > ends[0] else 1
    return n


def classify(j: dict, sentence: str) -> str:
    """Fixed rules, checked in this order: a critical citation is never counted as reliance,
    and a statement backed by a list of 3+ references doesn't rest on any one of them."""
    if j.get("critical") is True:
        return "CRITICAL"
    if j.get("uses") is True:
        return "FOUNDATIONAL"
    if j.get("relies") is True:
        return "SUPPORTING" if count_refs(sentence) < LIST_SIZE else "BACKGROUND"
    if j.get("unclear") is True:
        return "UNKNOWN"
    return "BACKGROUND"


TO_JUDGE = """
MATCH (a:Paper)-[c:CITES]->(b:Paper {id: $cited})
WHERE c.contexts IS NOT NULL AND c.judged_by IS NULL
RETURN a.id, a.title, c.contexts, c.intents, c.influential
ORDER BY a.cited_by DESC
LIMIT $limit
"""

SAVE = """
UNWIND $rows AS row
MATCH (a:Paper {id: row.src})-[c:CITES]->(b:Paper {id: $cited})
SET c.cls = row.cls, c.reliance = row.reliance, c.evidence = row.evidence, c.why = row.why,
    c.judged_by = row.judged_by, c.judged_at = row.judged_at
"""

MAX_SENTENCES = 4
MAX_CHARS = 500


def _prompt(cited: dict, batch: list) -> str:
    lines = [f'Cited paper: "{cited["title"]}" ({cited["year"]}, {cited["venue"]}).', "", "Citations:"]
    for i, (_, title, contexts, intents, influential) in enumerate(batch, 1):
        tags = ", ".join(intents) or "none"
        lines.append(f"e{i} (citing paper: \"{(title or '')[:120]}\"; intents: {tags}"
                     f"{'; marked influential' if influential else ''})")
        for j, sentence in enumerate(contexts[:MAX_SENTENCES]):
            lines.append(f"  [{j}] {sentence.strip()[:MAX_CHARS]}")
    return "\n".join(lines)


def _checked(reply: dict, batch: list) -> tuple[list[dict], int]:
    """Keep only well-formed judgments. Anything malformed stays unjudged and is retried later."""
    rows, seen = [], set()
    judged_by = f"{reply['provider']}:{reply['model']}:v{VERSION}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = reply["data"]
    items = data if isinstance(data, list) else data.get("judgments") or []   # some replies are a bare list
    for j in items:
        if not isinstance(j, dict):
            continue
        try:
            i = int(str(j["id"]).lstrip("e")) - 1
            k = int(j["sentence"])
        except (KeyError, ValueError, TypeError):
            continue
        if not 0 <= i < len(batch) or i in seen:
            continue
        src, _, contexts, _, _ = batch[i]
        if not 0 <= k < min(len(contexts), MAX_SENTENCES):
            continue
        cls = classify(j, contexts[k])
        seen.add(i)
        rows.append({"src": src, "cls": cls, "reliance": WEIGHTS[cls], "evidence": contexts[k],
                     "why": str(j.get("why", ""))[:200], "judged_by": judged_by, "judged_at": now})
    return rows, len(batch) - len(rows)


def judge_citations(g: Graph, cited_id: str, providers, batch_size: int, limit: int) -> dict:
    found = g.ro_query("MATCH (p:Paper {id: $id}) RETURN p.title, p.year, p.venue", {"id": cited_id}).result_set
    if not found:
        raise SystemExit(f"{cited_id}: not in the graph")
    cited = dict(zip(("title", "year", "venue"), found[0]))
    todo = g.ro_query(TO_JUDGE, {"cited": cited_id, "limit": limit}).result_set
    stats = {"to_judge": len(todo), "judged": 0, "not_judged": 0, "calls": 0}
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        try:
            reply = llm.chat_json(SYSTEM, _prompt(cited, batch), providers=providers,
                                  max_tokens=max(4096, 90 * len(batch)))
        except llm.LLMError as e:
            print(f"  stopped: {e}")
            stats["not_judged"] += len(todo) - start
            break
        stats["calls"] += 1
        rows, rejected = _checked(reply, batch)
        if rows:
            g.query(SAVE, {"rows": rows, "cited": cited_id})
        stats["judged"] += len(rows)
        stats["not_judged"] += rejected
        print(f"  batch {start // batch_size + 1}: {len(rows)} judged, {rejected} not, "
              f"by {reply['provider']} (usage {reply.get('usage', {}).get('total_tokens')})")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cited")
    ap.add_argument("--provider", choices=list(llm.PROVIDERS), default="groq")
    ap.add_argument("--fallback", action="store_true",
                    help="if the provider is out of quota, try the other one")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--limit", type=int, default=10_000)
    args = ap.parse_args()
    order = [args.provider] + ([p for p in llm.PROVIDERS if p != args.provider] if args.fallback else [])
    print(judge_citations(db.graph(), args.cited, order, args.batch, args.limit))
