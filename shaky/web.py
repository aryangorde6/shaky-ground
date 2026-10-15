"""The web UI: one static page (web/) and a small JSON API over views.py. Standard library only.

    uv run python -m shaky.web            # http://127.0.0.1:8000
    HOST=0.0.0.0 PORT=7860 ALLOW_RUNS=0 uv run python -m shaky.web     # a public, read-only deployment

GET  /api/config                      what this server allows, and the paper it opens on
GET  /api/blast?paper=<doi or id>     the blast radius of a retracted paper, as nodes and edges
GET  /api/paper?id=W..&root=W..       one paper, with its riskiest chains to the root
GET  /api/edge?a=W..&b=W..            one citation: sentences, class, model, carry votes
GET  /api/checks                      recent foundation checks
GET  /api/check?id=<investigation>    one foundation check's result
POST /api/check {"paper": "<doi>"}    run a foundation check (needs ALLOW_RUNS=1)
POST /api/list {"text": "<refs>"}     check every DOI in a reference list or BibTeX (stored checks are reused;
                                      read only, references never checked here are NOT CHECKED)
"""
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, foundation, report, risk, views
from .reliance import WEIGHTS
from .resolve import ResolveError

WEB = Path(__file__).resolve().parent.parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"), "/example.bib": ("example.bib", "text/plain")}
# the page runs only its own script and Cytoscape from jsDelivr (Cytoscape sets inline styles on its canvas);
# its fonts come from Google Fonts
CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
MAX_BODY = {"/api/check": 10_000, "/api/list": 300_000}
# At most this many API requests touch the graph at once; the rest wait their turn. Query memory counts
# against FalkorDB Cloud's 100 MB, so a burst of visitors must not run a burst of queries.
SLOTS = threading.BoundedSemaphore(int(os.getenv("API_SLOTS", "4")))
SLOT_WAIT = 30

graph = db.graph()


def _int(qs: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(qs.get(name, [default])[0])))
    except ValueError:
        return default


def _arg(qs: dict, name: str) -> str:
    value = (qs.get(name) or [""])[0].strip()
    if not value:
        raise ResolveError(f"missing ?{name}=")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "ShakyGround"

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _api(self, fn) -> None:
        if not SLOTS.acquire(timeout=SLOT_WAIT):
            return self._json(503, {"error": "the server is busy; try again in a moment"})
        try:
            self._json(200, fn())
        except ResolveError as e:
            self._json(400, {"error": str(e)})
        except views.NotAllowed as e:
            self._json(403, {"error": str(e)})
        except views.NotFound as e:
            self._json(404, {"error": str(e)})
        except Exception:      # details go to the server log, not to the page
            traceback.print_exc()
            self._json(500, {"error": "something went wrong on the server; see its log"})
        finally:
            SLOTS.release()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path in STATIC:
            name, kind = STATIC[url.path]
            return self._send(200, (WEB / name).read_bytes(), kind)
        routes = {
            "/api/config": lambda: {"allow_runs": config.ALLOW_RUNS, "demo_root": config.DEMO_ROOT,
                                    "graph": config.GRAPH_NAME, "weights": WEIGHTS, "meaning": report.CLASS_MEANING,
                                    "verdicts": foundation.VERDICTS, "builds_on": risk.BUILDS_ON,
                                    "max_hops": risk.MAX_HOPS, "depth": foundation.DEPTH},
            "/api/blast": lambda: views.blast(graph, views.find(graph, _arg(qs, "paper")),
                                              _int(qs, "passing", 40, 0, 200), _int(qs, "not_shown", 25, 0, 200)),
            "/api/paper": lambda: views.paper(graph, views.find(graph, _arg(qs, "id")), views.find(graph, _arg(qs, "root"))),
            "/api/edge": lambda: views.edge(graph, views.find(graph, _arg(qs, "a")), views.find(graph, _arg(qs, "b"))),
            "/api/checks": lambda: views.checks(graph),
            "/api/check": lambda: views.check_result(graph, _arg(qs, "id")),
        }
        if url.path in routes:
            return self._api(routes[url.path])
        self._json(404, {"error": f"no such page: {url.path}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in MAX_BODY:
            return self._json(404, {"error": "no such endpoint"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY[path]:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "the body must be a JSON object, e.g. {\"paper\": \"<doi>\"}"})
        if path == "/api/list":
            return self._api(lambda: views.check_list(graph, str(body.get("text") or "")))
        self._api(lambda: views.run_check(graph, str(body.get("paper") or ""),
                                          max(1, min(100, int(body.get("credits") or 50)))))

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main() -> None:
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    # build the demo view before the first visitor asks for it
    threading.Thread(target=lambda: views.blast(graph, config.DEMO_ROOT), daemon=True).start()
    print(f"Shaky Ground on http://{host}:{port} (graph '{config.GRAPH_NAME}', "
          f"{'runs allowed' if config.ALLOW_RUNS else 'read only'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
