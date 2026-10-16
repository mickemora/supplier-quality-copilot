"""End-to-end check of the storyline without AWS: generator -> signatures -> clustering.

Titan embeddings are replaced by a bag-of-words vector, so the threshold here is
lower than the deployed one. What matters is the logic: before the demo NCR only the
BGA voiding pattern is active; after it, the gold plating pattern appears too.
"""
import math
import re
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from tests import helpers  # noqa: F401
from common.similarity import find_patterns, signature_text, top_k_similar
import generate_synthetic_data as gen

TODAY = date(2026, 10, 15)
STOP = {"the", "a", "of", "and", "to", "on", "in", "was", "were", "for", "with", "at", "by",
        "not", "after", "is", "be", "from", "as", "its", "no"}


def field(text: str, label: str) -> str:
    m = re.search(rf"^{label}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def parse(path: Path) -> dict:
    text = path.read_text()
    desc = text.split("DESCRIPTION OF NONCONFORMANCE\n", 1)[1].split("\n", 1)[0]
    cause = re.search(r"root cause[^:\n]*:\s*(.+)", text, re.IGNORECASE).group(1)
    return {
        "ncr_id": field(text, "NCR No."),
        "supplier_id": re.search(r"\((SUP-\d+)\)", text).group(1),
        "commodity": "",  # excluded so cross-commodity matches rely on mechanism text
        "detected_at": field(text, "Date detected"),
        "defect_description": desc,
        "root_cause_category": "",
        "root_cause_detail": cause,
    }


def bow(text: str, vocab: dict) -> list[float]:
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP]
    counts = Counter(words)
    vec = [0.0] * len(vocab)
    for w, c in counts.items():
        vec[vocab[w]] = float(c)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_all(records: list[dict]) -> list[dict]:
    sigs = [signature_text(r) for r in records]
    vocab = {}
    for s in sigs:
        for w in re.findall(r"[a-z]+", s.lower()):
            vocab.setdefault(w, len(vocab))
    return [{**r, "vector": bow(s, vocab)} for r, s in zip(records, sigs)]


class PatternStorylineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        ncrs, demo = gen.build(TODAY, 7)
        import random
        rng = random.Random(8)
        out = Path(cls.tmp.name)
        cls.seed_files = []
        for n in ncrs:
            p = out / f"{n.ncr_id}.txt"
            p.write_text(gen.render(n, rng))
            cls.seed_files.append(p)
        cls.demo_file = out / f"{demo.ncr_id}.txt"
        cls.demo_file.write_text(gen.render(demo, rng))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def patterns(self, files):
        items = embed_all([parse(f) for f in files])
        return items, find_patterns(items, threshold=0.45, recent_window_days=60, today=TODAY)

    def labels(self, patterns, items):
        by_id = {i["ncr_id"]: i for i in items}
        out = set()
        for p in patterns:
            text = " ".join(by_id[n]["defect_description"].lower() for n in p["ncr_ids"])
            out.add("plating" if "gold" in text else "voiding" if "bga" in text else "other")
        return out

    def test_seed_only_has_voiding_pattern(self):
        items, patterns = self.patterns(self.seed_files)
        self.assertEqual(self.labels(patterns, items), {"voiding"})

    def test_demo_ncr_activates_plating_pattern(self):
        items, patterns = self.patterns(self.seed_files + [self.demo_file])
        self.assertEqual(self.labels(patterns, items), {"voiding", "plating"})
        plating = next(p for p in patterns if "SUP-0112" in p["supplier_ids"])
        self.assertEqual(set(plating["supplier_ids"]), {"SUP-0107", "SUP-0112", "SUP-0145"})
        self.assertEqual(plating["ncr_count"], 4)

    def test_similar_ncrs_finds_historical_fix(self):
        items, _ = self.patterns(self.seed_files + [self.demo_file])
        target = next(i for i in items if i["supplier_id"] == "SUP-0112"
                      and "gold" in i["defect_description"].lower())
        matches = top_k_similar(target, items, k=3)
        self.assertIn("SUP-0145", {m["supplier_id"] for m in matches})
        self.assertNotIn("vector", matches[0])


class GeneratorTests(unittest.TestCase):
    def test_deterministic_and_fictional(self):
        a, demo_a = gen.build(TODAY, 7)
        b, demo_b = gen.build(TODAY, 7)
        self.assertEqual([n.ncr_id for n in a], [n.ncr_id for n in b])
        self.assertEqual(demo_a.ncr_id, demo_b.ncr_id)
        self.assertEqual(len({n.ncr_id for n in a}), len(a))
        self.assertTrue(all(n.detected <= TODAY for n in a))


if __name__ == "__main__":
    unittest.main()
