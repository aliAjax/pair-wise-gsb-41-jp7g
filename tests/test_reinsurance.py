import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class ReinsuranceLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, number, payout, event="TY-2026", policy=None, lat=30.1):
        claim = self.service.create_claim(
            "intake1", "intake", number, event, "A区", "flood", policy or ("P-" + number),
            "R-" + number, lat, 121.1, max(payout * 2, 100000), False, False,
        )
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"])
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"])
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.9, "全损", "赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        return self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", payout, claim["version"])

    def treaty(self, event="TY-2026", retention=100000, layers=None):
        layers = layers or [
            {"layer_order": 1, "reinsurer": "中再", "cede_ratio": 0.8, "payout_limit": 200000},
            {"layer_order": 2, "reinsurer": "慕再", "cede_ratio": 0.9, "payout_limit": 200000},
        ]
        return self.service.create_treaty("fin1", "finance", event, retention, layers)

    def test_waterfall_retention_layers_and_uncovered(self):
        self.approve("C-001", 1000000)
        view = self.treaty()
        totals = view["totals"]
        # 自留10万；一层最多吸收毛额 20万/0.8=25万，摊回20万；
        # 二层最多吸收 20万/0.9≈22.22万，摊回20万；剩余≈42.78万未覆盖
        self.assertAlmostEqual(1_000_000, totals["gross"], places=2)
        self.assertAlmostEqual(100_000, totals["retention"], places=2)
        self.assertAlmostEqual(400_000, totals["ceded"], places=2)
        self.assertAlmostEqual(427_777.78, totals["uncovered"], places=2)
        layers = {l["layer_order"]: l for l in view["layers"]}
        self.assertAlmostEqual(layers[1]["occupied"], 200_000, places=2)
        self.assertAlmostEqual(layers[2]["occupied"], 200_000, places=2)
        self.assertTrue(all(l["full"] for l in view["layers"]))
        row = view["claims"][0]
        buckets = {b["bucket"]: b for b in row["breakdown"] if b["bucket"] != "layer"}
        self.assertAlmostEqual(buckets["retention"]["gross_amount"], 100_000, places=2)
        self.assertAlmostEqual(buckets["uncovered"]["gross_amount"], 427_777.78, places=2)

    def test_confirm_blocked_lists_claim_layer_and_excess(self):
        self.approve("C-001", 1000000)
        view = self.treaty()
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_ledger("fin1", "finance", view["treaty"]["id"], view["treaty"]["version"])
        self.assertEqual(409, ctx.exception.status)
        details = ctx.exception.details
        self.assertAlmostEqual(details["uncovered_total"], 427_777.78, places=2)
        blocked = details["blocked_claims"]
        self.assertEqual(1, len(blocked))
        self.assertEqual("C-001", blocked[0]["claim_no"])
        self.assertAlmostEqual(blocked[0]["uncovered_amount"], 427_777.78, places=2)
        # 列出被占满的合约层
        orders = sorted(l["layer_order"] for l in details["layers"] if l["remaining"] <= 0.005)
        self.assertEqual([1, 2], orders)

    def test_confirm_success_when_fully_covered_and_persisted(self):
        self.approve("C-001", 450000)
        view = self.treaty()
        # 自留10万 + 一层摊20万(毛25万) + 二层摊9万(毛10万) = 45万，全覆盖
        self.assertAlmostEqual(view["totals"]["uncovered"], 0, places=2)
        confirmed = self.service.confirm_ledger("fin1", "finance", view["treaty"]["id"], view["treaty"]["version"])
        self.assertEqual("confirmed", confirmed["treaty"]["status"])
        # 重开（新会话/新连接）仍能逐案逐层核对
        reopened = self.service.get_ledger("auditor", "aud1", treaty_id=view["treaty"]["id"])
        self.assertEqual("confirmed", reopened["treaty"]["status"])
        self.assertEqual(1, len(reopened["claims"]))
        layer_rows = [b for b in reopened["claims"][0]["breakdown"] if b["bucket"] == "layer"]
        self.assertEqual(2, len(layer_rows))
        self.assertEqual("中再", layer_rows[0]["reinsurer"])
        self.assertAlmostEqual(layer_rows[0]["ceded_amount"], 200_000, places=2)
        self.assertAlmostEqual(layer_rows[1]["ceded_amount"], 90_000, places=2)

    def test_same_event_claims_share_limits_and_reopen_confirmed_treaty(self):
        # 合约先建，案件陆续核定
        view = self.treaty()
        treaty_id = view["treaty"]["id"]
        self.approve("C-001", 300000)
        view = self.service.get_ledger("finance", "fin1", treaty_id=treaty_id)
        # 自留10万 + 一层毛20万(摊16万)，层1余4万
        self.assertAlmostEqual(view["totals"]["retention"], 100_000, places=2)
        self.assertAlmostEqual(view["totals"]["ceded"], 160_000, places=2)
        self.assertAlmostEqual(view["totals"]["uncovered"], 0, places=2)
        confirmed = self.service.confirm_ledger("fin1", "finance", treaty_id, view["treaty"]["version"])
        self.assertEqual("confirmed", confirmed["treaty"]["status"])
        # 第二个案件核定推高层占用：自留已满，层1只能再吸收 4万/0.8=5万毛额，差额滚层2
        self.approve("C-002", 200000)
        reopened = self.service.get_ledger("finance", "fin1", treaty_id=treaty_id)
        self.assertEqual("pending", reopened["treaty"]["status"])
        self.assertGreater(reopened["treaty"]["version"], confirmed["treaty"]["version"])
        self.assertAlmostEqual(reopened["totals"]["ceded"], 335_000, places=2)
        c2 = next(c for c in reopened["claims"] if c["claim_no"] == "C-002")
        l1 = next(b for b in c2["breakdown"] if b["bucket"] == "layer" and b["layer_order"] == 1)
        l2 = next(b for b in c2["breakdown"] if b["bucket"] == "layer" and b["layer_order"] == 2)
        self.assertAlmostEqual(l1["gross_amount"], 50_000, places=2)
        self.assertAlmostEqual(l1["ceded_amount"], 40_000, places=2)
        self.assertAlmostEqual(l2["gross_amount"], 150_000, places=2)
        self.assertAlmostEqual(l2["ceded_amount"], 135_000, places=2)
        # 层2占用13.5万，仍有余量，可再次确认
        confirmed2 = self.service.confirm_ledger("fin1", "finance", treaty_id, reopened["treaty"]["version"])
        self.assertEqual("confirmed", confirmed2["treaty"]["status"])

    def test_later_claim_becomes_uncovered_and_blocked(self):
        # 自留10万 + 层1(80%,上限20万→毛25万) + 层2(100%,上限20万→毛20万) = 55万，恰好占满
        layers = [
            {"layer_order": 1, "reinsurer": "中再", "cede_ratio": 0.8, "payout_limit": 200000},
            {"layer_order": 2, "reinsurer": "临分", "cede_ratio": 1.0, "payout_limit": 200000},
        ]
        view = self.treaty(layers=layers)
        treaty_id = view["treaty"]["id"]
        self.approve("C-001", 550000)  # 占满全部程序且无零头
        self.approve("C-002", 100000)  # 无任何余量 -> 全额未覆盖
        view = self.service.get_ledger("finance", "fin1", treaty_id=treaty_id)
        self.assertAlmostEqual(view["totals"]["uncovered"], 100_000, places=2)
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_ledger("fin1", "finance", treaty_id, view["treaty"]["version"])
        blocked = ctx.exception.details["blocked_claims"]
        self.assertEqual(["C-002"], [b["claim_no"] for b in blocked])
        self.assertAlmostEqual(blocked[0]["uncovered_amount"], 100_000, places=2)

    def test_independent_events_have_independent_limits(self):
        self.approve("C-001", 300000, event="E-A")
        self.approve("C-002", 300000, event="E-B")
        self.treaty(event="E-A", retention=50000)
        self.treaty(event="E-B", retention=50000)
        for event in ("E-A", "E-B"):
            view = self.service.get_ledger("finance", "fin1", event_id=event)
            self.assertAlmostEqual(view["totals"]["gross"], 300000, places=2)
            self.assertAlmostEqual(view["totals"]["retention"], 50000, places=2)
            # 层1吸收25万毛额(摊20万)，无未覆盖
            self.assertAlmostEqual(view["totals"]["uncovered"], 0, places=2)

    def test_treaty_validation_and_permissions(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty("fin1", "finance", "E-X", 100000, [])
        self.assertEqual(400, ctx.exception.status)
        self.treaty(event="E-X")
        with self.assertRaises(DomainError) as ctx:
            self.treaty(event="E-X")
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.list_treaties("viewer", "v")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty("int1", "intake", "E-Y", 100000, [
                {"reinsurer": "中再", "cede_ratio": 0.5, "payout_limit": 10000},
            ])
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
