"""Preflight: is a deployment ready to demo? Run it before recording and before submitting.

    uv run python -m shaky.preflight --snapshot data/snapshot.jsonl.gz
    uv run python -m shaky.preflight --snapshot data/snapshot.jsonl.gz --url https://<the live site>

Checks the FalkorDB that the settings point at (FALKORDB_HOST, GRAPH_NAME): it answers, the graph
holds what the snapshot holds, the indexes exist, the demo paper's risk is computed and its tiers
are the snapshot's, and the graph fits the free Cloud tier. With --url it also checks the live site
serves the page and the same answer. Prints PASS or FAIL per check; exits 1 if any check fails.
"""
import argparse
import gzip
import json
import sys
import time
import urllib.request

from . import config, db, risk, snapshot

CLOUD_FREE_MB = 100
QUERY_HEADROOM_MB = 20     # query memory comes out of the same 100 MB (FalkorDB on Discord, Oct 15)
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}", flush=True)
    return bool(ok)


def get(url: str) -> tuple[int, dict, bytes, float]:
    start = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "shaky-preflight"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read(), time.perf_counter() - start
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), time.perf_counter() - start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", help="the snapshot the deployment was restored from (its manifest is the expectation)")
    ap.add_argument("--url", help="the live site, e.g. https://shaky-ground-production.up.railway.app")
    args = ap.parse_args()
    manifest = None
    if args.snapshot:
        with gzip.open(args.snapshot, "rt", encoding="utf-8") as f:
            manifest = json.loads(f.readline())

    where = f"{config.FALKORDB_HOST}:{config.FALKORDB_PORT}, graph '{config.GRAPH_NAME}'"
    try:
        client = db.client()
        graphs = client.list_graphs()
    except Exception as e:
        check(False, f"FalkorDB answers at {where}: {type(e).__name__}")
        sys.exit(1)
    check(True, f"FalkorDB answers at {where}")
    if not check(config.GRAPH_NAME in graphs, f"graph '{config.GRAPH_NAME}' exists"):
        sys.exit(1)
    g = db.graph()

    got = snapshot.counts(g)
    if manifest:
        bad = {k: (v, got.get(k)) for k, v in manifest["counts"].items() if got.get(k) != v}
        check(not bad, f"counts match the snapshot of {manifest['exported_at']}: {got}" + (f"; differ: {bad}" if bad else ""))
    else:
        check(got["paper"] > 0 and got["cites"] > 0, f"graph is not empty: {got}")

    idx = db.existing_indexes(g)
    missing = [i for i in db.INDEXES if i not in idx]
    check(not missing, f"indexes present{'; missing ' + str(missing) if missing else ''}")

    root = config.DEMO_ROOT
    rows = g.ro_query("MATCH (p:Paper {id: $id}) RETURN p.retracted, p.risk_root, p.title", {"id": root}).result_set
    if check(rows, f"demo paper {root} is in the graph"):
        retracted, risk_root, title = rows[0]
        check(retracted, f"demo paper is marked retracted: {title[:60]}")
        check(risk_root == root, f"risk is computed for the demo paper (risk_root = {risk_root})")
        start = time.perf_counter()
        tiers = risk.tiers(g, root)
        ms = (time.perf_counter() - start) * 1000
        expected = (manifest or {}).get("tiers", {}).get(root)
        check(expected is None or tiers == expected,
              f"tiers {tiers} in {ms:.0f} ms" + (f" (snapshot: {expected})" if expected and tiers != expected else ""))

    try:
        mem = client.connection.execute_command("GRAPH.MEMORY", "USAGE", config.GRAPH_NAME)
        size = dict(zip(mem[::2], mem[1::2])).get(b"total_graph_sz_mb", dict(zip(mem[::2], mem[1::2])).get("total_graph_sz_mb"))
        check(int(size) <= CLOUD_FREE_MB - QUERY_HEADROOM_MB,
              f"graph uses {int(size)} MB of FalkorDB Cloud's free {CLOUD_FREE_MB} MB "
              f"(at most {CLOUD_FREE_MB - QUERY_HEADROOM_MB}, leaving room for query memory)")
    except Exception as e:
        print(f"  (memory not reported by this server: {type(e).__name__})")

    if args.url:
        base = args.url.rstrip("/")
        status, headers, body, sec = get(base + "/")
        check(status == 200 and b"Shaky Ground" in body, f"site serves the page ({status}, {sec:.2f} s)")
        check("Content-Security-Policy" in headers, "site sends a Content-Security-Policy")
        status, _, body, sec = get(base + "/api/config")
        cfg = json.loads(body) if status == 200 else {}
        check(status == 200, f"/api/config answers ({sec:.2f} s); allow_runs = {cfg.get('allow_runs')}")
        if cfg.get("allow_runs"):
            print("  note: this site may spend API credits and model calls (ALLOW_RUNS=1)")
        status, _, body, sec = get(f"{base}/api/blast?paper={cfg.get('demo_root', root)}")
        site = json.loads(body).get("tiers") if status == 200 else None
        check(status == 200 and site == risk.tiers(g, root), f"site's blast radius matches the graph ({sec:.2f} s): {site}")

    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
