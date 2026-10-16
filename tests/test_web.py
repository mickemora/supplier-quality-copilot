import json
import unittest

from tests import helpers  # noqa: F401
from common import scar_doc
from common.agent_stream import collect


class ScarDocTests(unittest.TestCase):
    def test_round_trip(self):
        meta = {"title": 'Thin gold: "plating"', "supplier_id": "SUP-0112",
                "ncr_ids": ["NCR-2026-0039"], "status": scar_doc.PENDING,
                "drafted_at": "2026-10-16T01:45:00+00:00"}
        text = scar_doc.render(meta, "## Problem\nThin gold.")
        parsed, body = scar_doc.parse(text)
        self.assertEqual(parsed, meta)
        self.assertEqual(body.strip(), "## Problem\nThin gold.")

    def test_reads_day1_format(self):
        legacy = ('---\ntitle: "SCAR x"\nsupplier_id: SUP-0107\nncr_ids: ["A", "B"]\n'
                  "status: PENDING_REVIEW\ndrafted_at: 2026-10-15T10:00:00+00:00\n---\n\nBody")
        meta, body = scar_doc.parse(legacy)
        self.assertEqual(meta["ncr_ids"], ["A", "B"])
        self.assertEqual(meta["drafted_at"], "2026-10-15T10:00:00+00:00")
        self.assertEqual(body, "Body")

    def test_no_front_matter(self):
        self.assertEqual(scar_doc.parse("just text"), ({}, "just text"))


class AgentStreamTests(unittest.TestCase):
    def test_collects_answer_citations_and_steps(self):
        events = [
            {"trace": {"trace": {"orchestrationTrace": {"rationale": {"text": "Check alerts first"}}}}},
            {"trace": {"trace": {"orchestrationTrace": {"invocationInput": {
                "actionGroupInvocationInput": {"function": "query_quality_data",
                                               "parameters": [{"name": "sql", "value": "SELECT 1"}]}}}}}},
            {"trace": {"trace": {"orchestrationTrace": {"invocationInput": {
                "knowledgeBaseLookupInput": {"text": "gold plating fix"}}}}}},
            {"trace": {"trace": {"orchestrationTrace": {"observation": {"knowledgeBaseLookupOutput": {
                "retrievedReferences": [{"location": {"s3Location": {"uri": "s3://b/kb/NCR-2025-0015.txt"}}}]}}}}}},
            {"chunk": {"bytes": b"Per-shift titration fixed it.", "attribution": {"citations": [
                {"retrievedReferences": [{"location": {"s3Location": {"uri": "s3://b/kb/NCR-2025-0015.txt"}},
                                          "metadata": {"supplier_id": "SUP-0145"},
                                          "content": {"text": "D5 ..."}}]}]}}},
        ]
        result = collect({"completion": events})
        self.assertEqual(result["answer"], "Per-shift titration fixed it.")
        self.assertEqual(result["citations"][0]["ncr_id"], "NCR-2025-0015")
        kinds = [s["kind"] for s in result["steps"]]
        self.assertEqual(kinds, ["thinking", "tool", "kb_search", "kb_result"])
        self.assertEqual(result["steps"][1]["input"], {"sql": "SELECT 1"})
        self.assertEqual(result["steps"][3]["ncr_ids"], ["NCR-2025-0015"])
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
