"""MCP server: lets any AI agent ask Shaky Ground before it cites a paper. Standard library only.

It speaks the Model Context Protocol over stdio (one JSON-RPC message per line) and offers three tools:

    check_foundations         does a paper rest on retracted work, through what chain?
    retraction_blast_radius   which papers build on a retracted paper?
    explain_chain             one paper's riskiest chain to a retracted paper, sentence by sentence

Every answer is read from the graph: counts from queries, sentences quoted from citing papers, and
the model that judged each link. Register it with an MCP client, e.g.

    {"command": "uv", "args": ["run", "--directory", "/path/to/shaky-ground", "python", "-m", "shaky.mcp_server"]}
"""
import contextlib
import functools
import json
import sys
import traceback

from . import config, db, foundation, risk, views
from .reliance import WEIGHTS
from .resolve import ResolveError

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")    # protocol versions this server can speak
SERVER = {"name": "shaky-ground", "version": "0.1.0"}
INSTRUCTIONS = ("Shaky Ground traces how much papers depend on retracted research through citation chains, judged "
                "from the sentences in which each paper cites the next. Verdicts never claim more than the evidence: "
                "'not shown' and NOT PROVEN mean the evidence is missing, not that the paper is clean.")

PAPER = {"type": "string", "description": "A DOI (10.xxxx/... or https://doi.org/...), an OpenAlex ID (W...) or a PubMed ID (pmid:...)"}
TOOLS = [
    {"name": "check_foundations",
     "title": "Check a paper's foundations",
     "description": ("Does this paper rest on retracted work? Checks its references and their references (depth 2) "
                     "against Retraction Watch and OpenAlex, and judges each link from the citing sentences. Returns a "
                     "verdict (RESTS ON RETRACTED WORK, NOT PROVEN, CITES RETRACTED WORK, NO RETRACTED WORK FOUND, "
                     "CLEAN · DEPTH 2, NO DATA), each retracted paper in reach, whether the paper is retracted itself, "
                     "and how much could be checked. With evidence true, each chain comes with the citing sentences "
                     "and the model that judged each link. Returns the latest stored check unless fresh is true; a "
                     "new check can take a few minutes."),
     "inputSchema": {"type": "object", "properties": {
         "paper": PAPER,
         "evidence": {"type": "boolean", "description": "Include every chain with its quoted sentences (longer)", "default": False},
         "fresh": {"type": "boolean", "description": "Run a new check even if one is stored", "default": False}},
         "required": ["paper"]}},
    {"name": "retraction_blast_radius",
     "title": "Blast radius of a retracted paper",
     "description": ("Which papers build on a retracted paper? Counts the papers in each tier (build on it: every link "
                     "of a chain FOUNDATIONAL or SUPPORTING; cite it in passing; dispute it; not shown either way) "
                     "and lists the papers that build on it with their chain length and risk. The paper must already "
                     "be loaded in the graph."),
     "inputSchema": {"type": "object", "properties": {
         "paper": PAPER,
         "limit": {"type": "integer", "description": "How many papers to list", "default": 20, "minimum": 1, "maximum": 100}},
         "required": ["paper"]}},
    {"name": "explain_chain",
     "title": "Explain a dependence chain",
     "description": ("Why does one paper depend on a retracted one? Returns its riskiest chain of citations to the "
                     "retracted paper (up to 3 links), with each link's class, weight, the citing sentence, the model "
                     "that judged it, and for links beyond the first, whether two models agreed the dependence carries on."),
     "inputSchema": {"type": "object", "properties": {
         "paper": PAPER,
         "retracted": {**PAPER, "description": "The retracted paper. " + PAPER["description"]}},
         "required": ["paper", "retracted"]}},
]

@functools.cache
def graph():      # connected on first use, so the protocol can be tested without a database
    return db.graph()


def _brief(result: dict) -> dict:
    """A check without its chains: what an agent needs to decide whether to cite."""
    out = {k: result[k] for k in ("paper", "verdict", "paper_retracted", "coverage", "why", "details_missing",
                                  "investigation", "credits") if k in result}
    out["retracted_ancestors"] = [
        {"id": a.get("id"), "title": a.get("title"), "year": a.get("year"), "tier": a.get("tier"),
         "cited_directly_as": a.get("direct_link"), "links": len((a.get("chain") or {}).get("links") or [])}
        for a in result.get("retracted_ancestors") or []]
    out["evidence"] = "call again with evidence=true, or explain_chain, for the citing sentences"
    return out


def check_foundations(paper: str, evidence: bool = False, fresh: bool = False) -> dict:
    result, stored = None, False
    if not fresh:
        try:
            result = views.latest_check(graph(), views.find(graph(), paper))
            stored = result is not None
        except views.NotFound:
            pass
    if result is None:
        result = views.run_check(graph(), paper)
    return {**(result if evidence else _brief(result)), "meaning": foundation.VERDICTS.get(result["verdict"]), "stored": stored}


def retraction_blast_radius(paper: str, limit: int = 20) -> dict:
    v = views.blast(graph(), views.find(graph(), paper))
    builds = sorted((n for n in v["nodes"] if n["tier"] == "builds_on"),
                    key=lambda n: (n["hops"], -(n["cited_by"] or 0)))
    disputes = [n for n in v["nodes"] if n["disputes"]]
    return {
        "paper": v["root"], "tiers": v["tiers"], "coverage": v["coverage"],
        "builds_on_means": f"proven risk >= {risk.BUILDS_ON}: every link of some chain judged FOUNDATIONAL or SUPPORTING",
        "builds_on": [{"id": n["id"], "title": n["title"], "year": n["year"], "cited_by": n["cited_by"],
                       "retracted": n["retracted"], "links": n["hops"], "chain_risk": n["chain_risk"]}
                      for n in builds[:max(1, min(100, limit))]],
        "disputes": [{"id": n["id"], "title": n["title"], "year": n["year"]} for n in disputes],
        "agent_investigations": [{"id": i["id"], "rounds": [{k: r[k] for k in ("round", "paper", "title", "reason")}
                                                            for r in i["rounds"]]} for i in v["investigations"]],
        "next": "explain_chain(paper=<an id above>, retracted=<this paper>) shows the evidence on every link",
    }


def explain_chain(paper: str, retracted: str) -> dict:
    pid, root = views.find(graph(), paper), views.find(graph(), retracted)
    out = {"paper": pid, "retracted": root}
    for variant, label in (("risk_proven", "proven"), ("risk", "possible")):
        c = risk.chain(graph(), pid, root, variant)
        if c:
            out[label] = {"risk": c["risk"], "papers": c["papers"],
                          "links": [{k: link.get(k) for k in ("cls", "reliance", "evidence", "judged_by",
                                                              "carries", "carry_votes", "carry_evidence")}
                                    for link in c["links"]]}
    if "proven" not in out and "possible" not in out:
        out["note"] = f"no chain of up to {risk.MAX_HOPS} citations from {pid} to {root} in the graph"
    else:
        weights = ", ".join(f"{cls} {w}" for cls, w in WEIGHTS.items())
        out["reading"] = (f"risk is the product of the link weights ({weights}); 'proven' counts only links with "
                          "evidence, and beyond the first link only links two models agreed carry the retracted part on")
    return out


HANDLERS = {"check_foundations": check_foundations, "retraction_blast_radius": retraction_blast_radius,
            "explain_chain": explain_chain}


def _bad_args(name: str, args: dict) -> str | None:
    schema = next(tool for tool in TOOLS if tool["name"] == name)["inputSchema"]
    missing = [k for k in schema["required"] if k not in args]
    unknown = [k for k in args if k not in schema["properties"]]
    wrong = [k for k, v in args.items() if k in schema["properties"]
             and not isinstance(v, {"string": str, "boolean": bool, "integer": int}[schema["properties"][k]["type"]])]
    if missing or unknown or wrong:
        return f"bad arguments: missing {missing}, unknown {unknown}, wrong type {wrong}"
    return None


def _call(name: str, args: dict) -> dict:
    if name not in HANDLERS:
        raise KeyError(name)
    if problem := _bad_args(name, args):
        return {"content": [{"type": "text", "text": problem}], "isError": True}
    try:
        # our modules print progress; stdout carries the protocol, so send it to stderr
        with contextlib.redirect_stdout(sys.stderr):
            data = HANDLERS[name](**args)
        text, error = json.dumps(data, indent=1, ensure_ascii=False, default=str), False
    except (ResolveError, views.NotFound, views.NotAllowed) as e:
        data, text, error = None, f"{type(e).__name__}: {e}", True
    except Exception as e:      # report the failure to the agent instead of crashing the server
        traceback.print_exc(file=sys.stderr)
        data, text, error = None, f"the tool failed: {type(e).__name__}", True
    out = {"content": [{"type": "text", "text": text}], "isError": error}
    if data is not None:
        out["structuredContent"] = json.loads(json.dumps(data, default=str))
    return out


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, the response out (None for notifications)."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:      # a notification, e.g. notifications/initialized: nothing to answer
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in VERSIONS else VERSIONS[1],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": SERVER, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = _call(params.get("name"), params.get("arguments") or {})
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"}}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    out = sys.stdout
    print(f"shaky-ground MCP server on stdio (graph '{config.GRAPH_NAME}', "
          f"{'runs allowed' if config.ALLOW_RUNS else 'read only'})", file=sys.stderr, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches are not supported"}}
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
