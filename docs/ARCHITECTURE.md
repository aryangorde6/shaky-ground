# Shaky Ground: how the code runs

How execution moves between the modules: the entry points, what runs at import, the call trees of the pipeline steps, the path of one web request and one MCP tool call, and which module writes what in the graph. Modules are in `shaky/`, the browser code in `web/app.js`.

How to read the call trees: `→` means "calls", indentation means "inside that call", `[A | B]` means one or the other.

---

## 1. The map in one picture

```
           DATA SOURCES                    BUILD PIPELINE (writes)                     SERVING (reads)
 OpenAlex ─────────┐                                                              ┌── web.py ── browser (web/app.js)
 Retraction Watch ─┼─ fetch.py ─ openalex.py ─┐                                   │
 Semantic Scholar ─┘   (cache,   semanticscholar.py   load → evidence → judge     ├── mcp_server.py ── any MCP client
                        retries) retractionwatch.py      → carry ⇄ risk           │     (examples/cite_check_agent.py)
 Gemini / Groq ───────── llm.py ──────────────────────── → agent                  │
                                                         → foundation             views.py  (one read layer for both)
                                                         → report                     │
                                        └──────────────── FalkorDB graph ────────────┘
 OPS:  snapshot.py (copy the graph to Cloud) · preflight.py (13 checks) · bench.py (latency) · CI + keep-alive
```

There are two halves. The **pipeline** is the command-line modules that load data and write judgments and risk into the graph. **Serving** is the web UI and MCP server, which only read the graph, unless `ALLOW_RUNS=1`.

## 2. Entry points

Every entry point is a module run with `python -m` (through `uv run`). Each module's `if __name__ == "__main__":` block is the entry; it parses arguments, opens the graph with `db.graph()` and calls one main function.

| Command | File | Main function it calls | Writes to the graph? |
|---|---|---|---|
| `python -m shaky.db` | `db.py` | `ensure_schema`, `summary` | indexes only |
| `python -m shaky.load <paper>` | `load.py` | `load_neighbourhood` | papers, citations, retractions |
| `python -m shaky.evidence W…` | `evidence.py` | `attach_s2` | sentences on edges |
| `python -m shaky.judge W…` | `judge.py` | `judge_citations` | class, weight, evidence on edges |
| `python -m shaky.carry W…` | `carry.py` | `settle` | carry votes on edges, risk on papers |
| `python -m shaky.risk W…` | `risk.py` | `propagate`, then `check` | risk on papers |
| `python -m shaky.agent <paper>` | `agent.py` | `investigate` | everything above + Investigation/EXPLORED |
| `python -m shaky.report <paper>` | `report.py` | `build` | no (writes a markdown file) |
| `python -m shaky.foundation <paper>` / `--list FILE` | `foundation.py` | `check` / `check_list` | loads references, judgments, an Investigation |
| `python -m shaky.web` | `web.py` | `main` | only with `ALLOW_RUNS=1` |
| `python -m shaky.mcp_server` | `mcp_server.py` | `main` | only with `ALLOW_RUNS=1` |
| `python -m shaky.snapshot export\|restore PATH` | `snapshot.py` | `export` / `restore` | restore writes the whole graph |
| `python -m shaky.preflight` | `preflight.py` | `main` | no |
| `python -m shaky.bench W…` | `bench.py` | `median_ms` over 5 queries | yes: it runs `propagate` 20 times (same result each time) |
| `python -m shaky.resolve <text>` / `shaky.retractionwatch` | `resolve.py` / `retractionwatch.py` | `resolve` / `retractions_by_doi` | no (debug helpers) |
| `python examples/cite_check_agent.py` | `cite_check_agent.py` | `main` | no (starts the MCP server as a child process) |
| `python -m unittest` | `tests/test_rules.py` | the offline tests of the rules | no database needed |

The order to run them in for a new retracted paper (README "Run it locally"):
`db` → `load` → `evidence` → `judge` → `carry` → `risk` → `agent` → `report` / `foundation` → `bench` → `web` / `mcp_server`.

## 3. What runs when a module is imported (before any function is called)

- **`config.py`** runs `load_dotenv(ENV_FILE or ".env")` and reads every setting into constants once. So `ENV_FILE=.env.cloud` must be set when the process starts; changing it later has no effect.
- **`fetch.py`** turns httpx's logger down to WARNING, because httpx logs full URLs, keys included.
- **`openalex.py`** and **`semanticscholar.py`** each create one `Source` object, `api` (an httpx client with its rate limit and secret parameters).
- **`llm.py`** builds `PROVIDERS` (URL, key, model name) from the settings.
- **`risk.py`** runs `assert WEIGHTS["SUPPORTING"] ** MAX_HOPS >= BUILDS_ON > WEIGHTS["BACKGROUND"]`: if someone changes the weights so the tiers break, every import fails.
- **`web.py`** creates the request semaphore `SLOTS` (size `API_SLOTS`, default 4).
- **Nothing connects to the database at import.** CLI modules connect in their entry block; `web.py` and `mcp_server.py` connect on the first request, so the server starts while the database is down and answers 503 until it is back.

Who imports whom, from the bottom up:

```
config ← fetch ← openalex, semanticscholar        reliance (the weights table, no imports)
config ← db, llm, retractionwatch
openalex ← resolve
db, openalex, resolve, retractionwatch, reliance ← load
db, semanticscholar ← evidence
db, llm, reliance ← judge
reliance ← risk
db, llm, risk ← carry
carry, db, evidence, judge, llm, load, openalex, resolve, risk ← agent
carry, db, evidence, judge, load, openalex, resolve, risk ← foundation
db, resolve, risk, reliance ← report
config, foundation, report, resolve, risk ← views
config, db, foundation, report, risk, views, reliance, resolve, (redis) ← web
config, db, foundation, risk, views, reliance, resolve ← mcp_server
config, db, risk ← snapshot ← preflight          agent, db, risk ← bench
```

So the web server's process imports the whole pipeline (through `views` → `foundation`). On a read-only server none of the writing code is reached.

## 4. The build pipeline, call by call

### 4.1 Load: `python -m shaky.load W2075481535`

```
load.__main__
├─ resolve.resolve(text)
│  ├─ resolve.lookup_key(text) → ("openalex", "W2075481535")   (a title raises ResolveError)
│  └─ openalex.work(key)
│     └─ openalex.api.get("works/W…", select)            fetch.py
│        ├─ Source._cache_file(...) exists? → return the cached JSON        fetch.py
│        └─ else Source._request → _throttle → httpx GET → retry on 429/5xx  fetch.py
│     └─ openalex.paper(work) → Paper properties incl. refs, oa_retracted
├─ db.graph() → db.client() → FalkorDB(...).select_graph("shaky")
├─ db.ensure_schema(g) → existing_indexes → CREATE INDEX for missing ones
└─ load_neighbourhood(g, id)
   ├─ openalex.work(id)                  (the root; a cache hit now)
   ├─ openalex.works(root.refs)          100 IDs per request
   ├─ openalex.citers(id)                cursor paging, 100 per page
   ├─ upsert_papers(g, [root, *refs, *citers])  → UNWIND/MERGE in batches of 250
   ├─ link_citations(g)    → LINK_CITATIONS: a CITES edge for every refs ID that is loaded,
   │                         ON CREATE cls UNKNOWN 0.3 (CRITICAL 0 from a retraction notice)
   └─ link_retractions(g)
      ├─ collect every loaded DOI (one row)
      ├─ retractionwatch.retractions_by_doi()
      │  └─ download() → the CSV, if older than 7 days    retractionwatch.py
      └─ LINK_RETRACTIONS: MERGE (:Retraction)-[:RETRACTS]->(p), SET p.retracted = true
then: print openalex.credits() and db.summary(g)
```

### 4.2 Evidence: `python -m shaky.evidence W2075481535`

```
evidence.__main__ → attach_s2(g, "W2075481535")
├─ read the paper's DOI from the graph
├─ semanticscholar.citations(doi)  → pages of 1,000 via offset/next → contexts, intents, influential
├─ read every loaded paper's [doi, id] (one collect() row)
├─ match each citing paper: by DOI, else W + MAG ID
└─ SET_EVIDENCE: put contexts/intents/influential on (citer)-[:CITES]->(paper)
```

### 4.3 Judge: `python -m shaky.judge W2075481535 --provider gemini --batch 130`

```
judge.__main__ → judge_citations(g, cited, providers, batch, limit)
├─ TO_JUDGE: edges into the paper with contexts and no judged_by, most-cited citers first
└─ for each batch:
   ├─ _prompt(cited, batch) → "e1 … [0] sentence …"
   ├─ llm.chat_json(SYSTEM, prompt, providers, max_tokens)
   │  └─ for each provider: cache file? → return | no key → next | POST /chat/completions
   │     → 429: wait (≤ 90 s) or try the next provider | 5xx: retry after 20 s, 40 s | parse JSON → write cache
   ├─ _checked(reply, batch)   drop malformed entries
   │  └─ classify(j, sentence) → count_refs(sentence) for the list rule
   │     → WEIGHTS[cls]; evidence = contexts[k], copied verbatim
   └─ SAVE: SET cls, reliance, evidence, why, judged_by, judged_at
```

### 4.4 Carry and risk: `python -m shaky.carry W2075481535`, then `python -m shaky.risk W2075481535`

```
carry.__main__ → settle(g, root)
├─ risk.propagate(g, root)
│  ├─ clear risk, risk_proven, risk_root and the :Contaminated label on every paper
│  ├─ seed the root: risk = risk_proven = 1.0, risk_root = root
│  ├─ for prop in ("risk", "risk_proven"): run PROPAGATE 3 times (one hop each)   risk.py
│  │     weight of a link = VARIANTS[prop] (a later link is capped at 0.15 until it carries)
│  ├─ SET risk_root on every paper that got risk; SET :Contaminated where risk_proven ≥ 0.2
│  └─ tiers(g, root) → TIERS query → builds_on / passing / not_shown / disputes   risk.py
└─ up to 2 more rounds (MAX_HOPS − 1):
   ├─ judge_carry(g, root)
   │  ├─ TO_JUDGE: strong links A→B where B already builds on the root, not judged for this root
   │  ├─ _groups → UPSTREAM per B: B's own link on its proven chain ("what B took")
   │  └─ judge_groups(g, root, groups)
   │     ├─ for judge in ("groq", "gemini"): _vote(judge, groups)
   │     │  └─ _batches (8 per call for Groq, 60 for Gemini) → _prompt → llm.chat_json(effort="medium")
   │     │     → _answers (drop malformed)
   │     ├─ _combine(votes): both yes → True, both no → False, else None
   │     └─ SAVE: carries, carry_root, carry_votes, carry_evidence, carry_why, carry_by, carry_at
   ├─ stop if there was nothing to judge, or nothing could be judged
   └─ risk.propagate(g, root) again (newly carried links now count at full weight)

risk.__main__: propagate(g, root), then check(g, root)
└─ BRUTE: every chain of up to 3 links scored with reduce(), compared paper by paper
   with the stored numbers → {"papers": N, "mismatches": 0}
```

### 4.5 Agent: `python -m shaky.agent 10.1038/nature04533 --rounds 2 --per-round 2 --credits 50 --judge groq`

```
agent.__main__ → resolve.resolve → investigate(g, root, rounds, per_round, credits, [judge], batch)
├─ the root must be in the graph and marked retracted
├─ CREATE (:Investigation {mode: 'blast_radius', status: 'running', ...})
├─ risk.propagate(g, root)
├─ for each round:
│  ├─ FRONTIER: 12 papers with risk ≥ 0.05, not yet expanded, ordered by risk × log(cited_by + 1)
│  ├─ _candidates_text(rows) → one line per candidate (risk, proven risk, link class, sentence)
│  ├─ llm.chat_json(PLANNER, …, providers=["groq", "gemini"]) → {"expand": [...], "reason", "stop"}
│  ├─ keep only IDs from the list, at most per_round; stop if asked or nothing is left
│  └─ for each pick within the budget (ceil(cited_by / 100) credits):
│     ├─ _expand(g, pid)
│     │  ├─ openalex.citers(pid) → load.upsert_papers → load.link_citations → load.link_retractions
│     │  ├─ evidence.attach_s2(g, pid)
│     │  ├─ judge.judge_citations(g, pid, ...)
│     │  └─ SET p.expanded_at
│     └─ CREATE (i)-[:EXPLORED {round, reason, decided_by, citers, credits, judged}]->(p)
│  └─ carry.settle(g, root)   (propagate → carry check → propagate)
├─ retry judge.judge_citations once for every explored paper (a busy provider leaves gaps)
├─ carry.settle(g, root)
└─ SET Investigation status 'done', the papers count, the unjudged count, the tiers → return a summary
```

### 4.6 Foundation check: `python -m shaky.foundation 10.1016/j.neuroscience.2007.10.054`

```
foundation.__main__ → check(g, text, max_credits=50)
├─ resolve.resolve(text) → p (with p.refs); note the credits used so far
├─ no refs → upsert the paper → _record(NO DATA)
├─ _load(g, p, max_credits)
│  ├─ openalex.works(p.refs)          → references (depth 1)
│  ├─ their refs, minus those already found → as many as the budget allows → openalex.works(...)  (depth 2)
│  ├─ load.upsert_papers → load.link_citations → load.link_retractions
│  └─ coverage: how many were found, skipped for budget, or have no DOI
├─ ANCESTORS: retracted papers within 2 links
├─ for each retracted ancestor R:
│  ├─ MIDDLE: papers X with P→X→R
│  ├─ _evidence(g, R, [P, *X]) → evidence.attach_s2(R) → judge.judge_citations(R, only_from=…)
│  ├─ for X whose link to R is strong or UNKNOWN: _evidence(g, X, [P])
│  ├─ where P→X and X→R are both strong: carry.group(...) → carry.judge_groups(g, R, groups)
│  ├─ _tier(g, P, R) → risk.chain(…, "risk_proven", 2) and risk.chain(…, "upper", 2)
│  │     proven ≥ 0.2 → RESTS ON | upper ≥ 0.2 → NOT PROVEN | else CITES RETRACTED WORK
│  └─ DIRECT: does P cite R directly, and how
├─ verdict = the worst ancestor tier, or CLEAN · DEPTH 2 if coverage is complete, else NO RETRACTED WORK FOUND
├─ _record(g, result) → CREATE (:Investigation {mode: 'foundation_check', result: <JSON>})
└─ summary(result) → printed

--list FILE → check_list(g, text, max_credits=200)
├─ dois_in(text) → DOIs in order, and how many entries have no DOI
│  └─ _bib_entries(text) (BibTeX, brace matching) | _text_entries(text) (numbered items → blocks → lines)
├─ for each DOI: stored check? (only the web server passes `stored`) | run → check(...) within the credits left
└─ the list verdict = the worst entry; a listed paper retracted itself → at least CITES RETRACTED WORK;
   any entry without a DOI → never CLEAN → list_summary(result)
```

### 4.7 Report: `python -m shaky.report 10.1038/nature04533 --out data/reports/lesne.md`

```
report.__main__ → resolve.resolve → build(g, root, limit)
├─ ROOT (title, venue, retraction dates and reasons), risk.tiers, COVERAGE, EXPANDED
├─ BUILDS_ON (:Contaminated) → risk.chain(...) for each one → quoted sentence + judging model per link
├─ RETRACTED_DOWNSTREAM, DISPUTES, NOT_SHOWN (top 10), EXPLORED (the agent's decisions)
└─ markdown text, written to --out
```

## 5. The web UI, from start-up to a click

### 5.1 Start-up

```
web.__main__ → main()
├─ ThreadingHTTPServer((HOST, PORT), Handler)       one thread per request
├─ Thread(warm, daemon=True)                        builds the demo view before the first visitor
│  └─ views.blast(graph(), DEMO_ROOT)               (failure is logged, never fatal)
└─ serve_forever()
```

On Railway the Dockerfile's `CMD` is `.venv/bin/python -m shaky.web`, with `HOST=0.0.0.0 PORT=7860 ALLOW_RUNS=0`.

### 5.2 The browser opens the page

```
GET /            → Handler.do_GET → STATIC["/"] → web/index.html (with the CSP header)   web.py
index.html loads → style.css, Google Fonts, Cytoscape 3.30.2 (jsDelivr, SRI-checked), app.js
app.js init()
├─ api("/api/config")  → allow_runs, demo_root, weights, verdict texts
├─ wait for the fonts (at most 1.5 s), wire up the search box, tabs, filters, Escape
├─ setMode("blast" | "check")                                             app.js
└─ loadBlast(?paper= or demo_root)                                        app.js
   └─ api("/api/blast?paper=W2075481535")  → the server path in 5.3
      → drawBlast(view) → ensureCy() + radial(nodes) → Cytoscape draws the rings   app.js
      → renderBlastPanel(view) → the tier counts, coverage, agent log           app.js
```

### 5.3 One API request on the server: `GET /api/blast?paper=W2075481535`

```
Handler.do_GET                                                            web.py
└─ routes["/api/blast"] → _api(fn)
   ├─ SLOTS.acquire(timeout=30)  → no slot in 30 s → 503 "busy"
   ├─ fn():
   │  ├─ graph()                    connect on first use (under a lock)
   │  ├─ _arg(qs, "paper")          missing → ResolveError → 400
   │  ├─ views.find(g, text)        → resolve.lookup_key; a PubMed ID → resolve.resolve (OpenAlex);
   │  │                               then MATCH by doi or id; not there → NotFound → 404   views.py
   │  └─ views.blast(g, root, passing=40, not_shown=25)
   │     ├─ ensure_root(g, root)    risk_root is another paper? read only → NotAllowed → 403;
   │     │                          else risk.propagate under the write lock   views.py
   │     ├─ cached(): _stamp(g) (3 count queries) + the _cache entry, less than 120 s old?   views.py
   │     └─ miss → with _building: check the cache again → _build(...)       views.py
   │        ├─ report.ROOT, report.COVERAGE, DISPUTE_IDS
   │        ├─ which papers to show: report.BUILDS_ON + report.RETRACTED_DOWNSTREAM + disputes
   │        │                         + TOP_PASSING (40) + report.NOT_SHOWN (25)
   │        ├─ _papers(ids)                                                views.py
   │        ├─ ThreadPoolExecutor(4).map(best) → risk.chain(g, pid, root, variant) per paper
   │        ├─ each paper's ring (hops) and the next paper towards the root, for the layout
   │        ├─ nodes with tier(p, root); LINKS_AMONG → edges, marked on_chain  views.py
   │        ├─ risk.tiers(g, root), investigations(g, root) (report.EXPLORED)   views.py
   │        └─ store in _cache
   ├─ _json(200, view) → _send: Content-Type, CSP, nosniff, no-store
   ├─ except ResolveError 400 | NotAllowed 403 | NotFound 404 | redis connection/timeout 503 | anything else 500 (traceback to the log)
   └─ finally SLOTS.release()
```

### 5.4 The other clicks

| In the page | Browser function | Request | Server path |
|---|---|---|---|
| Click a paper | `onNode` → `paperCard`, `chainList`, `carryBox` | `GET /api/paper?id=…&root=…` | `views.find` ×2 → `views.paper` → `risk.chain` for "risk_proven" and "risk" |
| Click a link | `onEdge` → `onEdgeIds` → `edgeCard` | `GET /api/edge?a=…&b=…` | `views.find` ×2 → `views.edge` (every property of that CITES edge) |
| Foundation-check tab | `setMode("check")` → `showCheckHome` → `checkHome` | `GET /api/checks` | `views.checks` (latest stored check per paper) |
| Open a stored check | `openCheck` → `showCheck`, `drawCheck` | `GET /api/check?id=…` | `views.check_result` (the stored result JSON) |
| "Check list" | `runList` → `showList`, `drawList` | `POST /api/list {"text": …}` | `do_POST` (size ≤ 300 KB, JSON object) → `views.check_list` → `foundation.check_list(..., stored=…)`; read only, also `known=…, run=False` |
| A new check (only with runs allowed) | `runCheck` | `POST /api/check {"paper": …}` | `views.run_check` → `foundation.check` under the write lock. Read only: the button is disabled, and a direct POST gets 403 |

Every function that sends a request in `app.js` bumps `state.req` and ignores an answer that arrives for an older request.

## 6. The MCP server, and the agent that uses it

### 6.1 One tool call

```
mcp_server.__main__ → main()
└─ for each line on stdin: json.loads → handle(msg)
   ├─ no id (a notification)        → no reply
   ├─ "initialize"                  → the client's protocol version if we speak it, else 2025-06-18; tools capability; instructions
   ├─ "ping"                        → {}
   ├─ "tools/list"                  → TOOLS (3 tools with JSON schemas)
   └─ "tools/call" → _call(name, args)
      ├─ unknown tool → JSON-RPC error -32602
      ├─ _bad_args(name, args) → missing / unknown / wrong type / outside the schema's minimum–maximum → isError
      ├─ with redirect_stdout(stderr):   (our modules print; stdout is the protocol)
      │  ├─ check_foundations(paper, evidence, fresh)
      │  │  └─ views.find → views.latest_check (stored) | views.run_check (new; needs ALLOW_RUNS)
      │  │     → _brief(result) unless evidence=true; + the verdict's meaning
      │  ├─ retraction_blast_radius(paper, limit) → views.blast → builds-on list, disputes, agent decisions
      │  └─ explain_chain(paper, retracted) → views.find ×2 → risk.chain for "risk_proven" and "risk"
      └─ {"content": [text], "structuredContent": data, "isError": …} → one JSON line on stdout
```

`graph()` in `mcp_server.py` is wrapped in `functools.cache`, so the first tool call connects, and the protocol tests never do.

### 6.2 The example agent: `python examples/cite_check_agent.py`

```
cite_check_agent.main()
├─ McpClient([python, "-m", "shaky.mcp_server"])   starts the server as a child process (stdio)
├─ initialize → notifications/initialized → tools/list
├─ the model gets check_foundations (with only its "paper" argument) and its own "answer" tool
└─ up to --max-steps turns: chat(provider, messages, tools)
   ├─ check_foundations call → mcp.request("tools/call") → the result goes back to the model
   └─ answer call → review(answer, wanted, checked)
      ├─ a source not checked, or not placed, or a retracted / RESTS ON source kept → sent back
      └─ otherwise accepted → print the cited paragraph, kept and dropped sources
```

## 7. Operations

```
snapshot export PATH → export(g, path)                                    snapshot.py
├─ manifest: counts(g) + the tiers of every root that has risk
└─ for each kind (paper, retraction, investigation, cites, retracts, explored):
   _read(g, kind) pages of 5,000 internal IDs → one JSON line per row (papers without `refs`)

ENV_FILE=.env.cloud snapshot restore PATH → restore(g, path)
├─ refuse a graph that isn't empty unless --replace → delete it → db.ensure_schema (indexes first)
├─ read the lines in order, CREATE in batches of 2,000 (nodes before edges, so every edge finds its ends)
├─ put the :Contaminated label back
└─ compare counts(g) with the manifest → exit 1 on any difference

preflight → main()                                                        preflight.py
├─ FalkorDB answers · the graph exists · counts = the manifest · the indexes exist
├─ the demo paper is in the graph, retracted, with its risk computed, and its tiers = the manifest
├─ GRAPH.MEMORY USAGE ≤ 80 MB (100 MB free tier minus 20 MB for query memory)
└─ with --url: the page (200, "Shaky Ground"), a CSP header, /api/config, and /api/blast tiers = the graph's
   → "N passed, M failed", exit 1 on any failure

bench W… → 20 runs each, median ms: propagate, blast radius (top 50), tiers, one chain, the agent's frontier

GitHub Actions: tests.yml (every push: checkout → setup-uv → uv sync → unittest)
                keep-alive.yml (cron "23 */3 * * *": curl the live /api/checks, so the free Cloud instance never sits idle a day)
```

## 8. Who writes what in the graph

| Property or element | Written by | When |
|---|---|---|
| `Paper` id, doi, title, year, cited_by, type, is_review, venue, oa_url, refs | `load.upsert_papers` | every load (agent and foundation check too) |
| `Paper.retracted` | `load.upsert_papers` (the OpenAlex flag), `load.link_retractions` (a Retraction Watch match) | every load; never set back to false |
| `Retraction` node, `RETRACTS` edge | `load.link_retractions` | every load |
| `CITES` edge, first `cls` / `reliance` (UNKNOWN 0.3 or CRITICAL 0) | `load.link_citations` (`ON CREATE` only) | every load |
| `CITES` contexts, intents, influential, evidence_src | `evidence.attach_s2` | evidence step, agent, foundation check |
| `CITES` cls, reliance, evidence, why, judged_by, judged_at | `judge.judge_citations` (SAVE) | judge step, agent, foundation check |
| `CITES` carries, carry_root, carry_via, carry_votes, carry_evidence, carry_why, carry_by, carry_at | `carry.judge_groups` (SAVE) | carry step, agent (`settle`), foundation check |
| `Paper` risk, risk_proven, risk_root, label `:Contaminated` | `risk.propagate` | carry step, risk step, agent, `views.ensure_root` (runs allowed) |
| `Paper.expanded_at` | `agent._expand`, `agent.investigate` (the root) | agent |
| `Investigation` (mode blast_radius), `EXPLORED` edges | `agent.investigate` | agent |
| `Investigation` (mode foundation_check, with `result` JSON) | `foundation._record` | every foundation check |
| Indexes | `db.ensure_schema` | `shaky.db`, `shaky.load`, `snapshot.restore` |

The web UI, the MCP server, `report` and `preflight` only read. The exceptions are `views.ensure_root`, `views.run_check` and `views.check_list` when runs are allowed.

## 9. One chain, end to end (the Mainardi example)

1. **`load`** brings in Lesné and its citers. The edge Mainardi 2014 → Lesné is created as UNKNOWN 0.3.
2. **`evidence`** puts Mainardi's sentence on that edge: "…with a lysis buffer adapted from Lesne et al. (2006)".
3. **`judge`**: the model says `uses: true`, so `classify` gives FOUNDATIONAL and `reliance` becomes 1.0, with the sentence stored verbatim.
4. **`risk.propagate`**: Mainardi's `risk_proven` = 1.0 × 1.0 = 1.0, at least 0.2, so it gets `:Contaminated` (builds on, ring 1).
5. **`agent`** expands Mainardi: its citers are loaded, their sentences fetched and judged. Say a citer C is judged SUPPORTING (0.6) on C → Mainardi.
6. **`propagate`** again: that later link hasn't been shown to carry, so it's capped at 0.15, and C's `risk_proven` = 1.0 × 0.15 = 0.15. That's *passing*, not builds on.
7. **`carry.judge_carry`** picks C → Mainardi, because Mainardi builds on the root and the link is strong. `UPSTREAM` finds what Mainardi took (the buffer). Both models compare it with what C cites Mainardi for.
   - If C cites Mainardi's Aβ-oligomer measurements (made with that buffer), both say yes, so `carries = true`.
   - If C cites the brain-wave recordings, both say no, so `carries = false`.
   - If they split, `carries = null`.
8. **`propagate`** once more: if it carries, C's `risk_proven` = 1.0 × 0.6 = 0.6, so C becomes *builds on*, and in the UI it sits on ring 2 next to Mainardi. If not, C stays *passing* at 0.15.
9. **The UI** draws C → Mainardi → Lesné as an orange chain. Clicking C calls `/api/paper` → `risk.chain`, which shows both sentences, both judging models and the two carry votes.
