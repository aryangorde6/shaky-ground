"""The Retraction Watch database (published by Crossref): retraction notices, matched to papers by DOI.

One CSV of every notice (~70 MB), downloaded once and refreshed weekly.
"""
import csv
import functools
import time
from collections import defaultdict
from datetime import datetime

import httpx

from . import config

URL = "https://api.labs.crossref.org/data/retractionwatch"
CSV_PATH = config.DATA_DIR / "retraction_watch.csv"
MAX_AGE_DAYS = 7


def download(refresh: bool = False):
    fresh = CSV_PATH.exists() and time.time() - CSV_PATH.stat().st_mtime < MAX_AGE_DAYS * 86400
    if fresh and not refresh:
        return CSV_PATH
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CSV_PATH.with_suffix(".tmp")
    with httpx.stream("GET", URL, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    tmp.replace(CSV_PATH)
    return CSV_PATH


def _date(text: str) -> str:
    """'6/24/2024 0:00' -> '2024-06-24'"""
    return datetime.strptime(text.split()[0], "%m/%d/%Y").date().isoformat()


def _reason(text: str) -> str:
    """'+Concerns/Issues about Image;Investigation by Journal/Publisher;' -> 'Concerns/Issues about Image; Investigation by Journal/Publisher'"""
    return "; ".join(part.strip().lstrip("+") for part in text.split(";") if part.strip())


@functools.cache   # parsing the CSV takes ~3 s; do it once per process
def retractions_by_doi() -> dict[str, list[dict]]:
    """DOI -> its retraction notices, for every retracted paper that was not later reinstated.

    Author names, institutions and countries are left out on purpose:
    Shaky Ground scores papers, never people.
    """
    notices = defaultdict(list)
    with open(download(), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            doi = row["OriginalPaperDOI"].strip().lower()
            if doi.startswith("10."):   # skips blank and "unavailable"
                notices[doi].append(row)

    out = {}
    for doi, rows in notices.items():
        retractions = [r for r in rows if r["RetractionNature"] == "Retraction"]
        if not retractions:
            continue
        last = max(_date(r["RetractionDate"]) for r in retractions)
        if any(r["RetractionNature"] == "Reinstatement" and _date(r["RetractionDate"]) > last
               for r in rows):
            continue
        out[doi] = [{
            "record_id": int(r["Record ID"]),
            "date": _date(r["RetractionDate"]),
            "nature": r["RetractionNature"],
            "reason": _reason(r["Reason"]),
            "notice_doi": r["RetractionDOI"].strip().lower() or None,
        } for r in retractions]
    return out


if __name__ == "__main__":
    by_doi = retractions_by_doi()
    print(f"{CSV_PATH}: {sum(map(len, by_doi.values()))} retraction notices "
          f"for {len(by_doi)} papers")
    print("Lesné 2006:", by_doi.get("10.1038/nature04533"))
