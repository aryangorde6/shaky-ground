"""OpenAlex: papers, the papers they cite, and the papers that cite them.

Costs (credits, 10,000 a day): a lookup by ID is free; a list call is 1 credit
per page of up to 100 works.
"""
from . import config
from .fetch import Source

api = Source("openalex", "https://api.openalex.org", min_interval=0.1,
             secret_params={"api_key": config.OPENALEX_API_KEY})

# Only the fields we store. No authorships: Shaky Ground scores papers, never people.
SELECT = ",".join([
    "id", "doi", "display_name", "publication_year", "cited_by_count", "type",
    "is_retracted", "open_access", "primary_location", "referenced_works",
])


def short_id(url: str | None) -> str | None:
    """https://openalex.org/W2075481535 -> W2075481535"""
    return url.rsplit("/", 1)[-1] if url else None


def clean_doi(doi: str | None) -> str | None:
    """https://doi.org/10.1038/NATURE04533 -> 10.1038/nature04533"""
    if not doi:
        return None
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        doi = doi.removeprefix(prefix)
    return doi or None


def paper(work: dict) -> dict:
    """An OpenAlex work as Paper properties. `refs` = IDs of the works it cites."""
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "id": short_id(work["id"]),
        "doi": clean_doi(work.get("doi")),
        "title": work.get("display_name"),
        "year": work.get("publication_year"),
        "cited_by": work.get("cited_by_count") or 0,
        "type": work.get("type"),
        "is_review": work.get("type") == "review",
        "venue": source.get("display_name"),
        "oa_url": (work.get("open_access") or {}).get("oa_url"),
        # OpenAlex also flags retraction notices themselves; a notice is not a retracted paper.
        "oa_retracted": bool(work.get("is_retracted")) and work.get("type") != "retraction",
        "refs": [short_id(r) for r in work.get("referenced_works") or []],
    }


def work(key: str, refresh: bool = False) -> dict:
    """One work by OpenAlex ID (W...) or DOI ("doi:10..."). Free."""
    return paper(api.get(f"works/{key}", {"select": SELECT}, refresh=refresh))


def works(ids: list[str], refresh: bool = False) -> list[dict]:
    """Many works by ID, 100 per request (1 credit each). Merged or deleted IDs are skipped."""
    ids = list(dict.fromkeys(ids))
    out = []
    for i in range(0, len(ids), 100):
        page = api.get("works", {"filter": "openalex:" + "|".join(ids[i:i + 100]),
                                 "per_page": 100, "select": SELECT}, refresh=refresh)
        out += [paper(w) for w in page["results"]]
    return out


def citers(work_id: str, limit: int | None = None, refresh: bool = False) -> list[dict]:
    """Works that cite `work_id`, 100 per request (1 credit each), via cursor paging."""
    out, cursor = [], "*"
    while cursor and (limit is None or len(out) < limit):
        page = api.get("works", {"filter": f"cites:{work_id}", "per_page": 100,
                                 "cursor": cursor, "select": SELECT}, refresh=refresh)
        out += [paper(w) for w in page["results"]]
        cursor = page["meta"].get("next_cursor") if page["results"] else None
    return out[:limit] if limit is not None else out


def credits() -> dict:
    """Today's credit use (never cached). Resets at midnight UTC, 5:30 AM IST."""
    limits = api.get("rate-limit", cache=False)["rate_limit"]
    return {"used": limits["credits_used"], "remaining": limits["credits_remaining"]}
