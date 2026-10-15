"""Copy the graph to another FalkorDB, e.g. the live deployment on FalkorDB Cloud.

The free Cloud tier holds at most 100 MB and keeps nothing across a restart, so the deployment is
reloaded from a snapshot file. A snapshot keeps every node, edge and property the UI and the MCP
server read, and leaves out each paper's raw reference list (`refs`, which only the loaders use):
that list is most of the graph's memory.

    uv run python -m shaky.snapshot export data/snapshot.jsonl.gz
    ENV_FILE=.env.cloud uv run python -m shaky.snapshot restore data/snapshot.jsonl.gz   # the Cloud's settings

The first line of the file is a manifest with the counts; restore checks them, and preflight.py
checks a deployment against them.
"""
import argparse
import gzip
import json
from datetime import datetime, timezone

from falkordb import Graph

from . import config, db, risk

DROP = {"refs"}        # Paper properties the deployment never reads
BATCH = 2000

# kind: (the pattern to read, the variable whose internal ID pages it, what to return, query that writes a batch)
KINDS = {
    "paper": ("(p:Paper)", "p", "properties(p), labels(p)",
              "UNWIND $rows AS r CREATE (p:Paper) SET p += r.props"),
    "retraction": ("(x:Retraction)", "x", "properties(x)",
                   "UNWIND $rows AS r CREATE (x:Retraction) SET x += r.props"),
    "investigation": ("(i:Investigation)", "i", "properties(i)",
                      "UNWIND $rows AS r CREATE (i:Investigation) SET i += r.props"),
    "cites": ("(a:Paper)-[c:CITES]->(b:Paper)", "c", "a.id, b.id, properties(c)",
              "UNWIND $rows AS r MATCH (a:Paper {id: r.a}), (b:Paper {id: r.b}) CREATE (a)-[c:CITES]->(b) SET c += r.props"),
    "retracts": ("(x:Retraction)-[e:RETRACTS]->(p:Paper)", "e", "x.record_id, p.id, properties(e)",
                 "UNWIND $rows AS r MATCH (x:Retraction {record_id: r.a}), (p:Paper {id: r.b}) CREATE (x)-[e:RETRACTS]->(p) SET e += r.props"),
    "explored": ("(i:Investigation)-[e:EXPLORED]->(p:Paper)", "e", "i.id, p.id, properties(e)",
                 "UNWIND $rows AS r MATCH (i:Investigation {id: r.a}), (p:Paper {id: r.b}) CREATE (i)-[e:EXPLORED]->(p) SET e += r.props"),
}
PAGE = 5000      # below the server's result-set cap (10,000 rows by default), which would cut a read short


def _read(g: Graph, kind: str):
    """Every row of one kind, a page of internal IDs at a time."""
    pattern, var, ret, _ = KINDS[kind]
    top = g.ro_query(f"MATCH {pattern} RETURN max(ID({var}))").result_set[0][0]
    for lo in range(0, (top if top is not None else -1) + 1, PAGE):
        yield from g.ro_query(f"MATCH {pattern} WHERE ID({var}) >= $lo AND ID({var}) < $hi RETURN {ret}",
                              {"lo": lo, "hi": lo + PAGE}, timeout=120_000).result_set
EXTRA_LABELS = ["Contaminated"]    # labels a Paper can carry besides :Paper


def counts(g: Graph) -> dict:
    """What a deployment must hold; preflight.py compares these with the manifest."""
    one = lambda q: g.ro_query(q).result_set[0][0]
    return {"paper": one("MATCH (p:Paper) RETURN count(p)"),
            "retraction": one("MATCH (x:Retraction) RETURN count(x)"),
            "investigation": one("MATCH (i:Investigation) RETURN count(i)"),
            "cites": one("MATCH ()-[c:CITES]->() RETURN count(c)"),
            "retracts": one("MATCH ()-[e:RETRACTS]->() RETURN count(e)"),
            "explored": one("MATCH ()-[e:EXPLORED]->() RETURN count(e)"),
            "contaminated": one("MATCH (p:Contaminated) RETURN count(p)")}


def export(g: Graph, path: str) -> dict:
    roots = [r[0] for r in g.ro_query("MATCH (p:Paper) WHERE p.risk_root IS NOT NULL RETURN DISTINCT p.risk_root").result_set]
    manifest = {"kind": "manifest", "graph": config.GRAPH_NAME, "dropped": sorted(DROP),
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts(g),
                "tiers": {root: risk.tiers(g, root) for root in roots}}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(manifest) + "\n")
        for kind in KINDS:
            for row in _read(g, kind):
                if kind == "paper":
                    props, labels = row
                    rec = {"props": {k: v for k, v in props.items() if k not in DROP},
                           "labels": [lb for lb in labels if lb in EXTRA_LABELS]}
                elif len(row) == 1:
                    rec = {"props": row[0]}
                else:
                    rec = {"a": row[0], "b": row[1], "props": row[2]}
                f.write(json.dumps({"kind": kind, **rec}, ensure_ascii=False) + "\n")
    return manifest


def restore(g: Graph, path: str, replace: bool = False) -> dict:
    """Load a snapshot into an empty graph (or replace the graph's contents with replace=True)."""
    exists = config.GRAPH_NAME in db.client().list_graphs()
    have = g.query("MATCH (n) RETURN count(n)").result_set[0][0] if exists else 0
    if have and not replace:
        raise SystemExit(f"graph '{config.GRAPH_NAME}' already holds {have} nodes; pass --replace to overwrite it")
    if exists:
        g.delete()
    db.ensure_schema(g)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        manifest = json.loads(f.readline())
        rows, labelled = [], {lb: [] for lb in EXTRA_LABELS}
        current = None
        for line in f:
            rec = json.loads(line)
            kind = rec.pop("kind")
            # the file holds one kind after another, nodes before edges: finish a kind before the next one
            # starts, so every edge finds both of its ends
            if (kind != current or len(rows) >= BATCH) and rows:
                g.query(KINDS[current][3], {"rows": rows})
                rows = []
            current = kind
            for lb in rec.pop("labels", []):
                labelled[lb].append(rec["props"]["id"])
            rows.append(rec)
        if rows:
            g.query(KINDS[current][3], {"rows": rows})
    for lb, ids in labelled.items():
        for i in range(0, len(ids), BATCH):
            g.query(f"UNWIND $ids AS id MATCH (p:Paper {{id: id}}) SET p:{lb}", {"ids": ids[i:i + BATCH]})
    got = counts(g)
    bad = {k: (v, got.get(k)) for k, v in manifest["counts"].items() if got.get(k) != v}
    return {"manifest": manifest["counts"], "restored": got, "mismatches": bad}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "restore"])
    ap.add_argument("path")
    ap.add_argument("--replace", action="store_true", help="restore: overwrite a graph that is not empty")
    args = ap.parse_args()
    g = db.graph()
    if args.action == "export":
        print(json.dumps(export(g, args.path), indent=1))
    else:
        out = restore(g, args.path, args.replace)
        print(json.dumps(out, indent=1))
        if out["mismatches"]:
            raise SystemExit("restore did not reproduce the snapshot's counts")
