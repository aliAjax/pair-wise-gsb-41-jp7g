import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class ReinsuranceCessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")
        # 事件 E1：自留 10 万；第一层 中再 80%、上限 20 万（吸收口径）；第二层 瑞再 90%、上限 20 万
        self.service.create_treaty(
            "fin1", "finance", "E1", "TR-E1", "E1分层合约", 100000,
            [
                {"layer_order": 1, "name": "第一层", "reinsurer": "中再产险", "cession_pct": 0.8, "payout_cap": 200000},
                {"layer_order": 2, "name": "第二层", "reinsurer": "瑞再", "cession_pct": 0.9, "payout_cap": 200000},
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, number, payout, event="E1", loss=None, policy=None):
        claim = self.service.create_claim(
            "intake1", "intake", number, event, "A区", "flood", policy or ("P-" + number), "R-" + number,
            30.1, 121.1, loss if loss is not None else payout * 2, False, True,
        )
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "受损", "赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        return self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", payout, claim["version"])

    def test_waterfall_retention_proportional_layers_and_rollover(self):
        result = self.approve("C-1", 500000)
        cession = result["cession"]
        # 自留先扣 10 万；第一层吸收 20 万，摊给中再 16 万，公司共保 4 万；
        # 差额 20 万滚入第二层，吸收 20 万，摊给瑞再 18 万，公司共保 2 万。
        self.assertEqual(100000, cession["retention"])
        self.assertEqual(340000, cession["ceded"])
        self.assertEqual(0, cession["uncovered"])
        self.assertEqual(200000, cession["layers"][0]["absorbed"])
        self.assertEqual(160000, cession["layers"][0]["ceded"])
        self.assertEqual(200000, cession["layers"][1]["absorbed"])
        self.assertEqual(180000, cession["layers"][1]["ceded"])
        state = self.service.reinsurance_state("finance", "E1")
        self.assertEqual(3, len(state["ledger"]))  # 自留 1 笔 + 两个合约层各 1 笔
        layer_entries = [e for e in state["ledger"] if e["bucket"] == "layer"]
        company_coinsurance = sum(e["absorbed"] - e["amount"] for e in layer_entries)  # 4万 + 2万
        self.assertAlmostEqual(
            500000,
            state["summary"][0]["retention_total"]
            + state["summary"][0]["ceded_total"]
            + state["summary"][0]["uncovered_total"]
            + company_coinsurance,
            places=2,
        )

    def test_event_aggregate_cap_blocks_with_claim_layer_overflow_details(self):
        self.approve("C-1", 500000)  # 两层各占满 20 万
        # 再来 20 万赔付：自留 10 万，剩 10 万两层都已满 -> 未覆盖
        with self.assertRaises(DomainError) as ctx:
            self.approve("C-2", 200000, policy="P-C2")
        self.assertEqual(409, ctx.exception.status)
        details = ctx.exception.details
        self.assertEqual(100000, details["uncovered"])
        reasons = {b["reason"] for b in details["blockers"]}
        self.assertIn("layer_exhausted", reasons)
        self.assertIn("uncovered", reasons)
        overflow_layers = {b["layer_order"] for b in details["blockers"] if b["reason"] == "layer_exhausted"}
        self.assertEqual({1, 2}, overflow_layers)
        for b in details["blockers"]:
            if b["reason"] == "uncovered":
                self.assertEqual(100000, b["overflow"])
        # 被挡时不落账
        state = self.service.reinsurance_state("finance", "E1")
        self.assertEqual(1, len({e["claim_id"] for e in state["ledger"]}))

    def test_accept_uncovered_persists_flagged_entries(self):
        self.approve("C-1", 500000)
        claim = self.service.create_claim(
            "intake1", "intake", "C-2", "E1", "A区", "flood", "P-C2", "R-C2", 30.1, 121.1, 900000, False, True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "受损", "赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        result = self.service.finalize_claim(
            "sup1", "supervisor", claim["id"], "approve", 200000, claim["version"], accept_uncovered=True)
        self.assertEqual(100000, result["cession"]["uncovered"])
        state = self.service.reinsurance_state("finance", "E1")
        second = [e for e in state["ledger"] if e["claim_no"] == "C-2"]
        buckets = [e["bucket"] for e in second]
        self.assertIn("uncovered", buckets)
        self.assertEqual(100000, sum(e["amount"] for e in second if e["bucket"] == "uncovered"))

    def test_partial_capacity_shared_across_claims(self):
        # 第一案 25 万：自留 10 万，剩 15 万全部被第一层吸收（中再摊 12 万）
        r1 = self.approve("C-1", 250000, loss=300000)
        self.assertEqual(120000, r1["cession"]["ceded"])
        # 第二案 25 万：自留 10 万；第一层只剩 5 万容量，摊 4 万；
        # 溢出 10 万滚入第二层，摊 9 万。
        r2 = self.approve("C-2", 250000, loss=300000, policy="P-C2")
        l1, l2 = r2["cession"]["layers"]
        self.assertEqual(50000, l1["absorbed"])
        self.assertEqual(40000, l1["ceded"])
        self.assertEqual(100000, l2["absorbed"])
        self.assertEqual(90000, l2["ceded"])
        self.assertEqual(0, r2["cession"]["uncovered"])
        state = self.service.reinsurance_state("finance", "E1")
        layer1 = state["treaties"][0]["layers"][0]
        self.assertEqual(200000, layer1["absorbed_used"])
        self.assertEqual(0, layer1["remaining_capacity"])

    def test_no_treaty_means_fully_retained_compatible_flow(self):
        result = self.approve("C-9", 123456, event="OTHER", policy="P-9")
        self.assertEqual(123456, result["final_payout"])
        self.assertEqual(123456, result["cession"]["retention"])
        self.assertEqual(0, result["cession"]["ceded"])

    def test_preview_does_not_occupy_capacity(self):
        preview = self.service.preview_cession("finance", "E1", 500000)
        self.assertEqual(340000, preview["ceded"])
        state = self.service.reinsurance_state("finance", "E1")
        self.assertEqual(0, len(state["ledger"]))
        self.assertEqual(200000, state["treaties"][0]["layers"][0]["remaining_capacity"])

    def test_recompute_after_cap_increase_covers_gap(self):
        self.approve("C-1", 500000)
        with self.assertRaises(DomainError):
            self.approve("C-2", 200000, policy="P-C2")
        # C-2 用强制落账保留未覆盖 10 万
        claim = next(c for c in self.service.queue("finance") if c["claim_no"] == "C-2" and c["status"] == "review")
        self.service.finalize_claim(
            "sup1", "supervisor", claim["id"], "approve", 200000, claim["version"], accept_uncovered=True)
        # 第二层上限提高到 30 万后重算，缺口消失
        self.service.update_treaty_layer("fin1", "finance", "E1", 2, payout_cap=300000)
        recomputed = self.service.recompute_cession("fin1", "finance", "E1", commit=True)
        self.assertEqual(0, recomputed["total_uncovered"])
        state = self.service.reinsurance_state("finance", "E1")
        self.assertEqual(0, state["summary"][0]["uncovered_total"])
        self.assertEqual(0, len([e for e in state["ledger"] if e["bucket"] == "uncovered"]))

    def test_recompute_dry_run_does_not_persist(self):
        self.approve("C-1", 500000)
        dry = self.service.recompute_cession("fin1", "finance", "E1", accept_uncovered=True, commit=False)
        self.assertFalse(dry["committed"])
        state = self.service.reinsurance_state("finance", "E1")
        self.assertEqual(3, len(state["ledger"]))

    def test_treaty_config_validation_and_permissions(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty(
                "fin1", "finance", "E2", "TR-E2", "空合约", 1000, [])
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty(
                "fin1", "finance", "E3", "TR-E3", "坏比例", 1000,
                [{"layer_order": 1, "reinsurer": "X", "cession_pct": 1.5, "payout_cap": 1000}])
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty(
                "x", "adjuster", "E4", "TR-E4", "无权", 1000,
                [{"layer_order": 1, "reinsurer": "X", "cession_pct": 0.5, "payout_cap": 1000}])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.create_treaty(
                "fin1", "finance", "E1", "TR-DUP", "重复事件", 1000,
                [{"layer_order": 1, "reinsurer": "X", "cession_pct": 0.5, "payout_cap": 1000}])
        self.assertEqual(409, ctx.exception.status)

    def test_cap_cannot_shrink_below_used(self):
        self.approve("C-1", 250000, loss=300000)  # 第一层吸收 15 万
        with self.assertRaises(DomainError) as ctx:
            self.service.update_treaty_layer("fin1", "finance", "E1", 1, payout_cap=100000)
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(150000, ctx.exception.details["used"])

    def test_ledger_survives_reopen(self):
        self.approve("C-1", 500000)
        fresh = CatastropheClaimService(Path(self.tmp.name) / "test.db")
        state = fresh.reinsurance_state("auditor", "E1")
        self.assertEqual(1, len(state["treaties"]))
        self.assertEqual(3, len(state["ledger"]))
        rows = [(e["bucket"], e["layer_order"], e["reinsurer"], e["amount"]) for e in state["ledger"]]
        self.assertEqual(("retention", None, None, 100000), rows[0])
        self.assertEqual(("layer", 1, "中再产险", 160000), rows[1])
        self.assertEqual(("layer", 2, "瑞再", 180000), rows[2])


if __name__ == "__main__":
    unittest.main()
