"""One chat call to a free OpenAI-compatible provider, with JSON output, a disk cache,
and fallback to the next provider when one is rate-limited or out of quota.

Gemini: 20 requests a day but huge batches. Groq: 1,000 a day but 8K tokens a minute.
"""
import hashlib
import json
import os
import time

import httpx

from . import config

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai",
               config.GEMINI_API_KEY, os.getenv("GEMINI_MODEL", "gemini-3.8-flash")),
    "groq": ("https://api.groq.com/openai/v1",
             config.GROQ_API_KEY, os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
}
CACHE_DIR = config.DATA_DIR / "cache" / "llm"


class LLMError(Exception):
    pass


def chat_json(system: str, user: str, providers=("groq", "gemini"), max_tokens: int = 8192,
              effort: str = "low") -> dict:
    """Returns {"data": <parsed JSON>, "provider", "model", "usage"}. The same prompt is sent
    to the same model only once. effort is the model's reasoning effort: low, medium or high."""
    errors = []
    for name in providers:
        base, key, model = PROVIDERS[name]
        key_parts = [name, model, system, user] + ([effort] if effort != "low" else [])
        digest = hashlib.sha1(json.dumps(key_parts).encode()).hexdigest()
        file = CACHE_DIR / f"{name}-{digest[:16]}.json"
        if file.exists():
            return json.loads(file.read_text())
        if not key:
            errors.append(f"{name}: no key")
            continue
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
                "reasoning_effort": effort, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        for attempt in range(3):
            try:
                r = httpx.post(f"{base}/chat/completions", json=body, timeout=300,
                               headers={"Authorization": f"Bearer {key}"})
            except httpx.TransportError as e:
                errors.append(f"{name}: {type(e).__name__}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                wait = r.headers.get("retry-after", "")
                if attempt < 2 and wait.replace(".", "", 1).isdigit() and float(wait) <= 90:
                    print(f"  {name}: rate-limited, waiting {float(wait):.0f}s")
                    time.sleep(float(wait) + 1)
                    continue
                errors.append(f"{name}: HTTP 429 {r.text[:200].replace(key, '<key>')}")
                break
            if r.status_code in (500, 502, 503, 504) and attempt < 2:
                delay = 20 * (attempt + 1)
                print(f"  {name}: HTTP {r.status_code} (busy), retrying in {delay}s")
                time.sleep(delay)
                continue
            if r.status_code != 200:
                errors.append(f"{name}: HTTP {r.status_code} {r.text[:300].replace(key, '<key>')}")
                break
            reply = r.json()
            try:
                data = json.loads(reply["choices"][0]["message"]["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                errors.append(f"{name}: reply was not JSON")
                break
            result = {"data": data, "provider": name, "model": model, "usage": reply.get("usage")}
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(result))
            return result
    raise LLMError("; ".join(errors))
