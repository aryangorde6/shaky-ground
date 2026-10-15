"""The rules that turn model answers and pasted text into decisions. No network, no database.

    uv run python -m unittest -v
"""
import unittest

from shaky import carry, judge, resolve, risk
from shaky.reliance import WEIGHTS


class ResolveShapes(unittest.TestCase):
    def test_accepted(self):
        cases = {
            "10.1038/nature04533": ("doi", "doi:10.1038/nature04533"),
            "doi:10.1038/NATURE04533": ("doi", "doi:10.1038/nature04533"),
            "https://doi.org/10.1038/nature04533.": ("doi", "doi:10.1038/nature04533"),
            "https://dx.doi.org/10.1038%2Fnature04533": ("doi", "doi:10.1038/nature04533"),
            "W2075481535": ("openalex", "W2075481535"),
            "https://openalex.org/W2075481535": ("openalex", "W2075481535"),
            "https://api.openalex.org/works/w2075481535": ("openalex", "W2075481535"),
            "pmid:16541076": ("pmid", "pmid:16541076"),
            "https://pubmed.ncbi.nlm.nih.gov/16541076/": ("pmid", "pmid:16541076"),
        }
        for text, expected in cases.items():
            self.assertEqual(resolve.lookup_key(text), expected, text)

    def test_refused(self):
        for text in ["", "A specific amyloid-beta protein assembly in the brain impairs memory", "W12x", "pmid:"]:
            with self.assertRaises(resolve.ResolveError, msg=text):
                resolve.lookup_key(text)


class Reliance(unittest.TestCase):
    def test_count_refs(self):
        self.assertEqual(judge.count_refs("as shown before (Lesné et al., 2006)."), 1)
        self.assertEqual(judge.count_refs("implicated in synaptic loss (Jones, 2001; Smith, 2006; Lee, 2010)."), 3)
        self.assertEqual(judge.count_refs("reported earlier [4, 5, 17–20]."), 6)

    def test_classify_order(self):
        one = "Smith et al. (2006) showed that X impairs memory."
        many = "X impairs memory (Jones, 2001; Smith, 2006; Lee, 2010)."
        self.assertEqual(judge.classify({"critical": True, "uses": True}, one), "CRITICAL")   # critical first
        self.assertEqual(judge.classify({"uses": True, "relies": True}, one), "FOUNDATIONAL")
        self.assertEqual(judge.classify({"relies": True}, one), "SUPPORTING")
        self.assertEqual(judge.classify({"relies": True}, many), "BACKGROUND")   # one of a list of 3+
        self.assertEqual(judge.classify({"unclear": True}, one), "UNKNOWN")
        self.assertEqual(judge.classify({}, one), "BACKGROUND")

    def test_builds_on_threshold_separates_the_classes(self):
        # every chain of FOUNDATIONAL/SUPPORTING links clears the bar; any chain with a BACKGROUND link does not
        self.assertGreaterEqual(WEIGHTS["SUPPORTING"] ** risk.MAX_HOPS, risk.BUILDS_ON)
        self.assertLess(WEIGHTS["BACKGROUND"] * WEIGHTS["FOUNDATIONAL"] ** (risk.MAX_HOPS - 1), risk.BUILDS_ON)


class Carry(unittest.TestCase):
    def test_both_votes_needed(self):
        self.assertIs(carry._combine([True, True]), True)
        self.assertIs(carry._combine([False, False]), False)
        self.assertIsNone(carry._combine([True, False]))
        self.assertIsNone(carry._combine([True, None]))

    def test_malformed_answers_are_dropped(self):
        ids = [("W1", ["s0", "s1"]), ("W2", ["s0"])]
        reply = {"data": {"took": [{"b": "B1", "took": "a buffer"}], "judgments": [
            {"id": "e1", "same_part": True, "sentence": 1, "cited_for": "oligomer levels"},
            {"id": "e2", "same_part": False, "sentence": 5},     # no such sentence
            {"id": "e9", "same_part": True, "sentence": 0},      # no such citation
            "not a dict",
        ]}}
        out = carry._answers(reply, ids)
        self.assertEqual(list(out), [0])
        self.assertIs(out[0]["carries"], True)
        self.assertEqual(out[0]["took"], {"B1": "a buffer"})


if __name__ == "__main__":
    unittest.main()
