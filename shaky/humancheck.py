"""A second opinion on the judge: 30 judged links, labelled without seeing the model's answers.

The sample is drawn from every judged citation, a fixed number per class the model gave (so the
rare strong classes are covered), with a fixed seed, so anyone can draw the same 30. The labeller
(a person, or a model from another family) answers the judge's own questions from the same
sentences; judge.classify turns both sets of answers into classes with the same rules, and
`score` compares them.

    uv run python -m shaky.humancheck sample --out labels/sample.json
    uv run python -m shaky.humancheck score labels/sample.json labels/crosscheck.json
"""
import argparse
import json
import random
import sys

from falkordb import Graph

from . import db
from .judge import MAX_CHARS, MAX_SENTENCES, classify

SEED = 20261016
QUOTA = {"FOUNDATIONAL": 8, "SUPPORTING": 7, "CRITICAL": 4, "UNKNOWN": 3, "BACKGROUND": 8}
STRONG = {"FOUNDATIONAL", "SUPPORTING"}
CHOICES = {"critical", "uses", "relies", "unclear", "none"}   # the judge's questions, first that applies

JUDGED = """
MATCH (a:Paper)-[c:CITES]->(b:Paper)
WHERE c.judged_by IS NOT NULL AND c.contexts IS NOT NULL
RETURN a.id, a.title, a.year, b.id, b.title, b.year, b.venue, c.contexts, c.intents, c.influential,
       c.cls, c.evidence, c.why, c.judged_by
ORDER BY b.id, a.id
"""


def sample(g: Graph) -> list[dict]:
    """QUOTA links per class, drawn with SEED, in a shuffled order, with what the model saw."""
    by_cls: dict[str, list] = {}
    for row in g.ro_query(JUDGED, timeout=60_000).result_set:
        by_cls.setdefault(row[10], []).append(row)
    rng = random.Random(SEED)
    rows = [row for cls, n in QUOTA.items() for row in rng.sample(by_cls[cls], n)]
    rng.shuffle(rows)
    items = []
    for i, (src, title, year, dst, dst_title, dst_year, venue, contexts, intents, influential,
            cls, evidence, why, judged_by) in enumerate(rows, 1):
        shown = [s.strip()[:MAX_CHARS] for s in contexts[:MAX_SENTENCES]]
        items.append({
            "key": f"L{i:02d}", "citing": {"id": src, "title": title, "year": year},
            "cited": {"id": dst, "title": dst_title, "year": dst_year, "venue": venue},
            "sentences": shown, "intents": intents or [], "influential": bool(influential),
            "model": {"cls": cls, "sentence": next((k for k, s in enumerate(contexts[:MAX_SENTENCES])
                                                    if s == evidence), None),
                      "why": why, "judged_by": judged_by},
            "population": len(by_cls[cls]),
        })
    return items


def labelled_class(item: dict, label: dict) -> str:
    """The labeller's answer, through the same rules as the model's (judge.classify)."""
    choice = label["choice"]
    if choice not in CHOICES:
        raise ValueError(f"{item['key']}: unknown choice {choice!r}")
    k = label.get("sentence") or 0
    return classify({} if choice == "none" else {choice: True}, item["sentences"][k])


def score(items: list[dict], labels: dict) -> dict:
    rows = []
    for item in items:
        if item["key"] not in labels:
            raise ValueError(f"{item['key']} has no label")
        rows.append((item, labelled_class(item, labels[item["key"]])))
    per_class = {cls: [sum(h == cls for m, h in rows if m["model"]["cls"] == cls),
                       sum(m["model"]["cls"] == cls for m, _ in rows)] for cls in QUOTA}
    model_strong = [(m, h) for m, h in rows if m["model"]["cls"] in STRONG]
    model_weak = [(m, h) for m, h in rows if m["model"]["cls"] not in STRONG]
    return {
        "links": len(rows),
        "same_class": sum(h == m["model"]["cls"] for m, h in rows),
        "per_class": per_class,
        "strong_confirmed": [sum(h in STRONG for _, h in model_strong), len(model_strong)],
        "weak_confirmed": [sum(h not in STRONG for _, h in model_weak), len(model_weak)],
        "disagreements": [{"key": m["key"], "model": m["model"]["cls"], "labeller": h,
                           "citing": m["citing"]["id"], "cited": m["cited"]["id"]}
                          for m, h in rows if h != m["model"]["cls"]],
    }


def report(s: dict) -> str:
    lines = [f"Same class as the model: {s['same_class']} of {s['links']} links",
             f"Model said strong (FOUNDATIONAL/SUPPORTING), the labeller also: "
             f"{s['strong_confirmed'][0]} of {s['strong_confirmed'][1]}",
             f"Model said not strong, the labeller also: {s['weak_confirmed'][0]} of {s['weak_confirmed'][1]}",
             "Per class the model gave (same class / links): "
             + ", ".join(f"{cls} {a}/{n}" for cls, (a, n) in s["per_class"].items())]
    for d in s["disagreements"]:
        lines.append(f"  {d['key']}  model {d['model']:<12} labeller {d['labeller']:<12} {d['citing']} -> {d['cited']}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="draw the 30 links from the graph")
    sp.add_argument("--out", required=True)
    sc = sub.add_parser("score", help="compare the labels with the model's")
    sc.add_argument("sample")
    sc.add_argument("labels")
    sc.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd == "sample":
        items = sample(db.graph())
        with open(args.out, "w") as f:
            json.dump(items, f, indent=1, ensure_ascii=False)
        print(f"{len(items)} links written to {args.out}", file=sys.stderr)
    else:
        with open(args.sample) as f:
            items = json.load(f)
        with open(args.labels) as f:
            labels = json.load(f)
        result = score(items, labels)
        print(json.dumps(result, indent=1) if args.json else report(result))
