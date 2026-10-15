"""An agent that checks its sources before it cites them, through Shaky Ground's MCP server.

It is an ordinary MCP client: it starts the server over stdio, lists its tools and hands them to a model
(Groq's gpt-oss-120b by default; any OpenAI-compatible endpoint works). The model gets a claim and the
sources a writer wants to cite for it, must call check_foundations on each source, and answers with the
claim cited from the sources it keeps and a reason for every source it drops.

    uv run python examples/cite_check_agent.py
    uv run python examples/cite_check_agent.py --claim "..." 10.xxxx/aaa 10.xxxx/bbb

Needs GROQ_API_KEY (or GEMINI_API_KEY with --provider gemini) in .env, and the graph running.
"""
import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = {"groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
             "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-3.8-flash")}

CLAIM = "Soluble amyloid-β oligomers, more than plaques, drive memory loss in models of Alzheimer's disease."
SOURCES = ["10.1038/416535a", "10.1038/nature04533", "10.1016/j.neuroscience.2007.10.054", "10.1038/nm1782"]

SYSTEM = """You help a researcher cite sources. Before you cite any source, call check_foundations on it.
Then decide, from what the tool returned and nothing else:
- drop a source that is retracted itself, or whose verdict is RESTS ON RETRACTED WORK;
- a source whose verdict is NOT PROVEN or CITES RETRACTED WORK may be kept, with a note on what to read first;
- keep a source whose verdict is NO RETRACTED WORK FOUND or CLEAN, without calling it more than the check says.
Never invent a verdict or a paper. When every source is checked, call the answer tool once. The answer is
checked against these rules in code, and sent back if it breaks them."""

# the agent's own tool for its final answer (handled here, not sent to the MCP server)
ENTRY = {"type": "object", "properties": {"doi": {"type": "string"}, "why": {"type": "string", "description": "one short sentence"}},
         "required": ["doi", "why"]}
ANSWER = {"type": "function", "function": {
    "name": "answer", "description": "Give the final answer, once every source has been checked.",
    "parameters": {"type": "object", "properties": {
        "paragraph": {"type": "string", "description": "The claim, cited as [1], [2]... from the kept sources only"},
        "kept": {"type": "array", "items": ENTRY}, "dropped": {"type": "array", "items": ENTRY}},
        "required": ["paragraph", "kept", "dropped"]}}}

def norm(source: str) -> str:
    return str(source).strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def review(answer: dict, wanted: list[str], checked: dict) -> str:
    """The rules, enforced in code rather than trusted to the model: every source is checked before the
    answer, every source is placed, and none is kept that the rules drop."""
    missing = [s for s in wanted if s not in checked]
    if missing:
        return f"not checked yet: {', '.join(missing)}"
    kept = {norm(e.get("doi", "")) for e in answer.get("kept") or [] if isinstance(e, dict)}
    dropped = {norm(e.get("doi", "")) for e in answer.get("dropped") or [] if isinstance(e, dict)}
    problems = []
    if unplaced := [s for s in wanted if s not in kept | dropped]:
        problems.append(f"put every source in kept or dropped: {', '.join(unplaced)}")
    if wrong := [s for s in wanted if s in kept and (checked[s].get("paper_retracted")
                                                    or checked[s]["verdict"].startswith("RESTS"))]:
        problems.append(f"must be dropped (retracted itself, or resting on retracted work): {', '.join(wrong)}")
    return "; ".join(problems)


BOLD, DIM, RED, GREEN, AMBER, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"


class McpClient:
    """The client side of MCP over stdio: one JSON-RPC message per line."""

    def __init__(self, cmd: list[str], cwd: Path):
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.next_id = 0

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit("the MCP server stopped")
            msg = json.loads(line)
            if msg.get("id") == self.next_id:
                break
        if "error" in msg:
            raise SystemExit(f"MCP error: {msg['error']['message']}")
        return msg["result"]

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def chat(provider: str, messages: list, tools: list) -> dict:
    base, key_name, model = PROVIDERS[provider]
    key = os.getenv(key_name)
    if not key:
        raise SystemExit(f"{key_name} is not set (put it in .env)")
    body = {"model": os.getenv(f"{provider.upper()}_MODEL", model), "messages": messages, "tools": tools,
            "tool_choice": "auto", "temperature": 0}
    if provider == "groq":
        body["reasoning_effort"] = "medium"
    for attempt in range(4):
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=180, headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 429 and attempt < 3:      # a free tier's per-minute limit: wait as asked
            wait = min(float(r.headers.get("retry-after") or 15), 60)
            print(f"{DIM}  (rate-limited, waiting {wait:.0f}s){RESET}", flush=True)
            time.sleep(wait + 1)
            continue
        if r.status_code != 200:
            raise SystemExit(f"{provider}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()["choices"][0]["message"]
    raise SystemExit(f"{provider}: still rate-limited")


def show_result(text: str) -> str:
    """One line for the transcript: what the check found."""
    try:
        r = json.loads(text)
    except json.JSONDecodeError:
        return text[:160]
    if "verdict" not in r:
        return text[:160]
    colour = RED if r["verdict"].startswith("RESTS") else AMBER if r["verdict"] in ("NOT PROVEN", "CITES RETRACTED WORK") else GREEN
    paper = r.get("paper") or {}
    line = f"{colour}{BOLD}{r['verdict']}{RESET}"
    if r.get("paper_retracted"):
        line += f" · {RED}{BOLD}retracted itself{RESET}"
    line += f" · {(paper.get('title') or '')[:70]} ({paper.get('year')})"
    for a in r.get("retracted_ancestors") or []:
        line += f"\n      {DIM}reaches retracted:{RESET} {(a.get('title') or '')[:64]} ({a.get('year')}) as {a.get('tier')}"
    return line + (f"\n      {DIM}stored check {r.get('investigation')}{RESET}" if r.get("stored") else "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", default=SOURCES, help="DOIs (or OpenAlex IDs) the writer wants to cite")
    ap.add_argument("--claim", default=CLAIM)
    ap.add_argument("--provider", choices=PROVIDERS, default="groq")
    ap.add_argument("--max-steps", type=int, default=6)
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")

    mcp = McpClient([sys.executable, "-m", "shaky.mcp_server"], ROOT)
    info = mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "cite-check-agent", "version": "0.1.0"}})
    mcp.notify("notifications/initialized")
    listed = mcp.request("tools/list")["tools"]
    # this agent reads stored checks and never starts new ones, so the model sees only the paper argument
    check = next(t for t in listed if t["name"] == "check_foundations")
    tools = [{"type": "function", "function": {"name": check["name"], "description": check["description"], "parameters": {
        "type": "object", "properties": {"paper": check["inputSchema"]["properties"]["paper"]}, "required": ["paper"]}}}, ANSWER]
    print(f"{DIM}MCP server {info['serverInfo']['name']} {info['serverInfo']['version']} · "
          f"tools: {', '.join(t['name'] for t in listed)} · model: {PROVIDERS[args.provider][2]}{RESET}\n")
    print(f"{BOLD}claim{RESET}    {args.claim}")
    print(f"{BOLD}sources{RESET}  {', '.join(args.sources)}\n")

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Claim: {args.claim}\nSources I want to cite: {json.dumps(args.sources)}"}]
    wanted, checked, answer = [norm(x) for x in args.sources], {}, None
    for _ in range(args.max_steps):
        msg = chat(args.provider, messages, tools)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            messages.append({"role": "user", "content": "Call check_foundations on every source, then the answer tool."})
            continue
        for call in calls:
            name, raw = call["function"]["name"], call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
            if name == "answer":
                problems = review(arguments, wanted, checked)
                if problems:
                    print(f"{AMBER}{BOLD}↩ answer sent back:{RESET} {problems}\n", flush=True)
                else:
                    answer = arguments
                reply = f"Not accepted: {problems}" if problems else "Accepted."
            elif name == "check_foundations":
                paper = str(arguments.get("paper") or "")
                print(f"{BOLD}→ check_foundations{RESET}(paper={json.dumps(paper)})", flush=True)
                result = mcp.request("tools/call", {"name": name, "arguments": {"paper": paper}})
                reply = result["content"][0]["text"]
                if result.get("isError"):
                    print(f"   {RED}{reply}{RESET}\n", flush=True)
                else:
                    print(f"   {show_result(reply)}\n", flush=True)
                    r = json.loads(reply)
                    for key in (paper, r["paper"].get("doi") or "", r["paper"]["id"]):
                        checked[norm(key)] = r
            else:
                reply = f"There is no tool named {name}."
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": reply[:6000]})
        if answer is not None:
            break
    mcp.close()

    if answer is None:
        raise SystemExit("the model did not finish within --max-steps")
    out = answer
    print(f"{BOLD}cited paragraph{RESET}")
    print(textwrap.fill(out.get("paragraph", ""), 100, initial_indent="  ", subsequent_indent="  "))
    for e in out.get("kept", []):
        print(f"  {GREEN}{BOLD}kept{RESET}     {e.get('doi')}  {DIM}{e.get('why')}{RESET}")
    for e in out.get("dropped", []):
        print(f"  {RED}{BOLD}dropped{RESET}  {e.get('doi')}  {DIM}{e.get('why')}{RESET}")


if __name__ == "__main__":
    main()
