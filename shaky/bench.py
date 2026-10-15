"""Median latency of the queries the agent and the report run, on the graph as it is now.

    uv run python -m shaky.bench W2075481535
"""
import statistics
import sys
import time

from . import agent, db, risk

RUNS = 20

BLAST_RADIUS = """
MATCH (p:Contaminated) WHERE p.risk_root = $root
RETURN p.id, p.title, p.year, p.risk_proven ORDER BY p.risk_proven DESC, p.cited_by DESC LIMIT 50
"""


def median_ms(fn) -> float:
    times = []
    for _ in range(RUNS):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(times), 1)


if __name__ == "__main__":
    g, root = db.graph(), sys.argv[1]
    papers, edges = (g.ro_query(q).result_set[0][0] for q in
                     ("MATCH (p:Paper) RETURN count(p)", "MATCH ()-[c:CITES]->() RETURN count(c)"))
    top = g.ro_query(BLAST_RADIUS, {"root": root}).result_set
    deepest = g.ro_query("MATCH (p:Contaminated) WHERE p.risk_root = $root AND NOT (p)-[:CITES]->(:Paper {id: $root}) "
                         "RETURN p.id LIMIT 1", {"root": root}).result_set
    sample = (deepest or top)[0][0]
    print(f"graph: {papers} papers, {edges} citations; {RUNS} runs each, median ms")
    print("  propagate (writes both risk numbers for every paper):", median_ms(lambda: risk.propagate(g, root)))
    print("  blast radius (top 50 papers proven to build on it):",
          median_ms(lambda: g.ro_query(BLAST_RADIUS, {"root": root})))
    print("  tiers (builds on / passing / not shown / disputes):", median_ms(lambda: risk.tiers(g, root)))
    print(f"  chain for one paper ({sample}, reduce over its paths):", median_ms(lambda: risk.chain(g, sample, root)))
    print("  agent frontier (12 candidates):",
          median_ms(lambda: g.ro_query(agent.FRONTIER, {"n": 12, "threshold": risk.EXPLORE})))
