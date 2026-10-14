"""Semantic Scholar: the sentences in which one paper cites another (the evidence for reliance).

With a key the limit is 1 request a second. Only about 1 citation in 5 comes with sentences;
the rest stay UNKNOWN, and the UI says so.
"""
from . import config
from .fetch import Source

api = Source("s2", "https://api.semanticscholar.org/graph/v1", min_interval=1.1,
             headers={"x-api-key": config.S2_API_KEY} if config.S2_API_KEY else None)

FIELDS = "contexts,intents,isInfluential,externalIds"
PAGE = 1000   # the maximum when contexts are requested


def citations(doi: str, refresh: bool = False) -> list[dict]:
    """Every citation of the paper with this DOI: the citing paper's IDs, plus its sentences."""
    out, offset = [], 0
    while offset is not None:
        page = api.get(f"paper/DOI:{doi}/citations",
                       {"fields": FIELDS, "offset": offset, "limit": PAGE}, refresh=refresh)
        for row in page.get("data") or []:
            ids = (row.get("citingPaper") or {}).get("externalIds") or {}
            out.append({
                "doi": (ids.get("DOI") or "").lower() or None,
                "mag": str(ids["MAG"]) if ids.get("MAG") else None,
                "contexts": [c for c in row.get("contexts") or [] if c and c.strip()],
                "intents": row.get("intents") or [],
                "influential": bool(row.get("isInfluential")),
            })
        offset = page.get("next")
    return out
