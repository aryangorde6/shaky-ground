"""How much a citing paper relies on the paper it cites. The LLM picks a class; this table,
not the model, turns it into the number that the risk math multiplies."""

WEIGHTS = {
    "FOUNDATIONAL": 1.0,   # uses its method, data or result as a basis
    "SUPPORTING": 0.6,     # cites it as evidence for a claim it relies on
    "BACKGROUND": 0.15,    # passing mention or general context
    "CRITICAL": 0.0,       # disputes it, fails to replicate it, or notes the retraction
    "UNKNOWN": 0.3,        # no evidence yet, or the evidence doesn't say
}
