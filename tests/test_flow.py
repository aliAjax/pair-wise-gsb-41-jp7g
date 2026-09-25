import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class CatastropheClaimFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, number, policy="P-1", lat=30.1, loss=500000, urgent=False):
        return self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", policy, "R-" + number,
            lat, 121.1, loss, urgent, True,
        )

    def test_complete_claim_lifecycle_with_emergency_advance(self):
        claim = self.claim("C-001", urgent=True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, True)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.emergency_advance("sup1", "supervisor", claim["id"], 50000, claim["version"], "ADV-001")
        evidence = self.service.add_evidence("adjuster1", "adjuster", claim["id"], "a" * 64, "loss.jpg", "field")
        self.assertFalse(evidence["bulk_reuse"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "结构受损", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 280000, claim["version"])
        self.assertEqual("approved", claim["status"])
        self.assertEqual(280000, claim["final_payout"])
        self.assertEqual(1, len(self.service.state("sup1", "supervisor")["payments"]))

    def test_duplicate_and_version_conflict(self):
        first = self.claim("C-010", policy="P-10")
        second = self.claim("C-011", policy="P-10", lat=30.11)
        self.assertEqual("duplicate", second["status"])
        self.assertEqual(first["id"], second["duplicate_of"])
        triaged = self.service.triage_claim("sup1", "supervisor", first["id"], first["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", first["version"])
        self.assertEqual(409, ctx.exception.status)
        assigned = self.service.assign_claim("sup1", "supervisor", first["id"], "adjuster1", triaged["version"])
        self.assertEqual("assigned", assigned["status"])

    def test_bulk_forged_evidence_and_permissions(self):
        claims = [self.claim("C-%03d" % i, policy="P-%03d" % i, lat=30 + i / 100) for i in range(1, 4)]
        shared = "b" * 64
        last = None
        for claim in claims:
            last = self.service.add_evidence("intake1", "intake", claim["id"], shared, "same.pdf", "batch-import")
        self.assertTrue(last["bulk_reuse"])
        self.assertGreaterEqual(len(last["affected_claims"]), 3)
        with self.assertRaises(DomainError) as ctx:
            self.service.queue("viewer", "viewer")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.add_evidence("adjuster1", "adjuster", claims[0]["id"], "not-a-hash", "x", "field")
        self.assertEqual(400, ctx2.exception.status)


if __name__ == "__main__":
    unittest.main()
