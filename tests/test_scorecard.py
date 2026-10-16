"""Scorecards over the synthetic dataset: the rules should tell the intended story."""
import random
import re
import unittest
from datetime import date

from tests import helpers  # noqa: F401
from common import scorecard as sc
import generate_synthetic_data as gen

TODAY = date(2026, 10, 15)


def records(include_demo: bool = False) -> list[dict]:
    ncrs, demo = gen.build(TODAY, 7)
    rng = random.Random(8)
    out = []
    for n in ncrs + ([demo] if include_demo else []):
        text = gen.render(n, rng, TODAY)
        grab = lambda label: (re.search(rf"{label}: (\d{{4}}-\d\d-\d\d)", text) or [None, ""])[1]  # noqa: E731
        out.append({
            "ncr_id": n.ncr_id, "supplier_id": n.supplier_id,
            "supplier_name": gen.SUPPLIER[n.supplier_id][1], "commodity": gen.SUPPLIER[n.supplier_id][2],
            "detected_at": n.detected.isoformat(), "severity": n.template.severity,
            "status": n.status, "defect_code": n.template.defect_code,
            "defect_description": n.template.defects[n.variant % len(n.template.defects)],
            "root_cause_category": n.template.category,
            "response_due_at": grab("8D response due"),
            "response_received_at": grab("8D response received"),
            "closed_at": (re.search(r"SCAR closed (\d{4}-\d\d-\d\d)", text) or [None, ""])[1] if n.status == "closed" else "",
        })
    return out


SUPPLIERS = [dict(zip(["supplier_id", "supplier_name", "commodity", "tier", "location",
                       "approval_status", "annual_spend_band"], s)) for s in gen.SUPPLIERS]
VOIDING = {"pattern_label": "BGA voiding", "supplier_ids": ["SUP-0203", "SUP-0219"]}
PLATING = {"pattern_label": "Thin gold plating", "supplier_ids": ["SUP-0107", "SUP-0112", "SUP-0145"]}


class ScorecardTests(unittest.TestCase):
    def test_every_supplier_scored_and_sorted_by_risk(self):
        cards = sc.scorecards(records(), SUPPLIERS, [VOIDING], TODAY)
        self.assertEqual(len(cards), len(SUPPLIERS))
        levels = [sc.LEVEL_ORDER[c["risk"]] for c in cards]
        self.assertEqual(levels, sorted(levels))
        for c in cards:
            self.assertTrue(c["reasons"], c["supplier_id"])

    def test_story_before_and_after_demo(self):
        before = {c["supplier_id"]: c for c in sc.scorecards(records(), SUPPLIERS, [VOIDING], TODAY)}
        after = {c["supplier_id"]: c for c in sc.scorecards(records(True), SUPPLIERS, [VOIDING, PLATING], TODAY)}
        # BGA voiding pattern makes both PCBA suppliers red.
        self.assertEqual(before["SUP-0203"]["risk"], "red")
        self.assertEqual(before["SUP-0219"]["risk"], "red")
        # Probation supplier is red with that reason.
        self.assertEqual(before["SUP-0340"]["risk"], "red")
        self.assertTrue(any("probation" in r["text"] for r in before["SUP-0340"]["reasons"]))
        # Ardent: two open critical plating NCRs.
        self.assertEqual(before["SUP-0107"]["risk"], "red")
        # Mesa is not red until its new NCR joins the plating pattern.
        self.assertNotEqual(before["SUP-0112"]["risk"], "red")
        self.assertEqual(after["SUP-0112"]["risk"], "red")
        self.assertTrue(any("cross-supplier" in r["text"] for r in after["SUP-0112"]["reasons"]))

    def test_rules_spread_across_levels(self):
        cards = sc.scorecards(records(), SUPPLIERS, [VOIDING], TODAY)
        counts = {lvl: sum(c["risk"] == lvl for c in cards) for lvl in ("red", "amber", "green")}
        self.assertGreaterEqual(counts["green"] + counts["amber"], 4, counts)
        self.assertLessEqual(counts["red"], 6, counts)

    def test_detail_trend_and_pareto(self):
        d = sc.detail("SUP-0107", records(), SUPPLIERS, [VOIDING], TODAY)
        self.assertEqual(len(d["trend"]), 12)
        self.assertEqual(d["trend"][-1]["month"], "2026-10")
        self.assertEqual(sum(p["count"] for p in d["pareto_defects"]), len(d["ncrs"]))
        self.assertEqual(d["pareto_defects"][-1]["cum_pct"], 100)
        self.assertEqual(d["ncrs"], sorted(d["ncrs"], key=lambda r: r["detected_at"], reverse=True))

    def test_overdue_response_rule(self):
        rec = {"ncr_id": "N1", "supplier_id": "S", "detected_at": "2026-08-01", "severity": "minor",
               "status": "open", "response_due_at": "2026-08-31", "response_received_at": ""}
        card = sc.supplier_card("S", {"approval_status": "approved"}, [rec], [], TODAY)
        self.assertEqual(card["risk"], "red")
        self.assertIn("N1", card["reasons"][0]["text"])


if __name__ == "__main__":
    unittest.main()
