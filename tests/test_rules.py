"""The rules that turn model answers and pasted text into decisions. No network, no database.

    uv run python -m unittest -v
"""
import unittest

from shaky import carry, foundation, judge, mcp_server, resolve, risk
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


class ReferenceLists(unittest.TestCase):
    def test_dois_in_bibtex_and_text(self):
        bib = """@article{a, title={X}, doi = {10.1016/j.neuroscience.2007.10.054},}
@article{b, title={Y}, doi="10.1109/CVPR.2016.90"}
@book{c, title={No {DOI} here}}
@comment{ignored}
Also cited: https://doi.org/10.1016/j.expneurol.2016.06.006. Again: 10.1016/J.NEUROSCIENCE.2007.10.054"""
        dois, without = foundation.dois_in(bib)
        self.assertEqual(dois, ["10.1016/j.neuroscience.2007.10.054", "10.1109/cvpr.2016.90",
                                "10.1016/j.expneurol.2016.06.006"])
        self.assertEqual(without, 1)     # entry c; the DOI after it belongs to no entry

    def test_nothing_to_check_is_refused(self):
        with self.assertRaises(resolve.ResolveError):
            foundation.check_list(None, "just some words, no references")


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


class McpProtocol(unittest.TestCase):
    """The JSON-RPC layer of the MCP server; none of these touch the database."""

    def test_handshake_and_tools(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        names = [t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["check_foundations", "retraction_blast_radius", "explain_chain"])
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_version_gets_one_we_speak(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertIn(r["result"]["protocolVersion"], mcp_server.VERSIONS)

    def test_errors(self):
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(r["error"]["code"], -32602)
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "explain_chain", "arguments": {"paper": "W1", "limit": 3}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("missing ['retracted']", r["result"]["content"][0]["text"])
        self.assertIn("unknown ['limit']", r["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
