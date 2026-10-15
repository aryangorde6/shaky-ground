"""Turn whatever someone pastes into one OpenAlex work, or a clear error. Never guess.

Accepted, all free lookups by ID:
    10.1038/nature04533   doi:10.1038/nature04533   https://doi.org/10.1038/nature04533
    W2075481535           https://openalex.org/W2075481535
    pmid:16541076         https://pubmed.ncbi.nlm.nih.gov/16541076/
A title is refused rather than searched: a search returns the closest match, which may be a
different paper, and every answer after that would be about the wrong paper.

    uv run python -m shaky.resolve "https://doi.org/10.1038/nature04533"
"""
import re
import sys
from urllib.parse import unquote

from . import openalex
from .fetch import FetchError

DOI = re.compile(r"\b(10\.\d{4,9}/\S+)", re.IGNORECASE)
OPENALEX = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:works/)?)?(W\d+)/?$", re.IGNORECASE)
PMID = re.compile(r"^(?:pmid:\s*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)(\d{1,9})/?$", re.IGNORECASE)

ACCEPTED = "a DOI (10.xxxx/... or https://doi.org/...), an OpenAlex ID (W...) or a PubMed ID (pmid:...)"


class ResolveError(ValueError):
    pass


def lookup_key(text: str) -> tuple[str, str]:
    """(kind, OpenAlex lookup key) for the pasted text; raises ResolveError if it is none of the accepted shapes."""
    text = unquote(text.strip()).strip("<>\"' ")
    if m := OPENALEX.match(text):
        return "openalex", m.group(1).upper()
    if m := PMID.match(text):
        return "pmid", f"pmid:{m.group(1)}"
    if m := DOI.search(text):
        doi = m.group(1).rstrip(".,;)]}").lower()    # punctuation pasted from the end of a sentence
        return "doi", f"doi:{doi}"
    raise ResolveError(f"can't read {text[:80]!r} as a paper: paste {ACCEPTED}")


def resolve(text: str) -> dict:
    """The OpenAlex work for the pasted text, as Paper properties plus how it was read."""
    kind, key = lookup_key(text)
    try:
        work = openalex.work(key)
    except FetchError as e:
        if e.status == 404:
            raise ResolveError(f"{kind} {key.split(':', 1)[-1]} is not in OpenAlex") from None
        raise
    return {**work, "input_kind": kind, "lookup": key}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        try:
            p = resolve(arg)
            print(f"{arg!r} -> {p['id']} ({p['input_kind']}): {p['title']} ({p['year']}), doi {p['doi']}")
        except ResolveError as e:
            print(f"{arg!r} -> error: {e}")
