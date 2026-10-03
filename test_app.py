import base64
import json
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _obj_and_claim(self):
        obj = self.store.create_object("staff", "M-2024-1", "画作", "纸本", "市博物馆", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        return obj, claim

    def test_register_shares_and_pending_when_not_full(self):
        obj, claim = self._obj_and_claim()
        # 默认一人独占 100%（已确认）。
        self.assertEqual(claim["share_status"], "confirmed")
        # 审查员登记多位继承人，合计不足 100% → 停在待补齐。
        res = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "王氏长房", "share": 40},
            {"claimant_id": "claimant2", "display_name": "王氏次房", "share": 30},
        ])
        self.assertEqual(res["share_status"], "pending")
        self.assertEqual(res["total"], 70.0)
        # 待补齐时可以进入审查，但不能完成返还。
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接返还。")
        self.assertEqual(ctx.exception.code, "shares_not_confirmed")

    def test_share_change_voids_confirmation_and_redistributes(self):
        obj, claim = self._obj_and_claim()
        # 第一次登记，合计 100% → 确认。
        r1 = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 50},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 50},
        ])
        self.assertEqual(r1["share_status"], "confirmed")
        rev1 = r1["revision"]
        # 份额一改动，原确认先作废并重新分配；这次合计不足 → 待补齐。
        r2 = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 40},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 30},
        ])
        self.assertEqual(r2["share_status"], "pending")
        self.assertGreater(r2["revision"], rev1)
        # 上一版确认已作废。
        shares = self.store.get_claim_shares("reviewer1", claim["id"])
        statuses = {d["status"] for d in shares["distributions"]}
        self.assertIn("void", statuses)
        self.assertEqual(shares["share_status"], "pending")

    def test_redistribute_retry_after_supplement(self):
        obj, claim = self._obj_and_claim()
        self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 40},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 30},
        ])
        # 重新分配失败（不足 100%），可重试。
        with self.assertRaises(BusinessError) as ctx:
            self.store.redistribute_claim("reviewer1", claim["id"])
        self.assertEqual(ctx.exception.code, "shares_incomplete")
        # 补齐份额后重试成功。
        self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 40},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 30},
            {"claimant_id": "claimant3", "display_name": "丙", "share": 30},
        ])
        res = self.store.redistribute_claim("reviewer1", claim["id"])
        self.assertEqual(res["share_status"], "confirmed")
        self.assertEqual(res["total"], 100.0)

    def test_claimant_cannot_modify_shares(self):
        obj, claim = self._obj_and_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_claim_shares("claimant1", claim["id"], [
                {"claimant_id": "claimant1", "display_name": "甲", "share": 100},
            ])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.redistribute_claim("claimant1", claim["id"])
        self.assertEqual(ctx.exception.status, 403)

    def test_public_cannot_see_share_details(self):
        obj, claim = self._obj_and_claim()
        self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 50},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 50},
        ])
        # 公众访问份额明细接口被拒。
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_claim_shares("public", claim["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 公众在藏品视图里也看不到份额字段。
        public_view = self.store.get_object("public", obj["id"])
        self.assertEqual(len(public_view["claims"]), 1)
        self.assertNotIn("parties", public_view["claims"][0])
        self.assertNotIn("share_status", public_view["claims"][0])
        # 审查员能看到完整份额。
        reviewer_view = self.store.get_object("reviewer1", obj["id"])
        self.assertIn("parties", reviewer_view["claims"][0])
        self.assertEqual(len(reviewer_view["claims"][0]["parties"]), 2)

    def test_concurrent_edits_last_wins_but_previous_kept(self):
        obj, claim = self._obj_and_claim()
        self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 60},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 40},
        ])
        # 另一位审查员同时修改份额（后提交为准）。
        self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 30},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 70},
        ])
        shares = self.store.get_claim_shares("reviewer1", claim["id"])
        # 当前以最新一次提交为准。
        self.assertEqual(shares["parties"][0]["share"], 30)
        self.assertEqual(shares["parties"][1]["share"], 70)
        # 上一版仍然保留（追加版本史）。
        self.assertGreaterEqual(len(shares["revisions"]), 2)
        self.assertEqual(shares["revisions"][-1]["total"], 100.0)

    def test_withdraw_party_releases_share(self):
        obj, claim = self._obj_and_claim()
        r = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"claimant_id": "claimant1", "display_name": "甲", "share": 50},
            {"claimant_id": "claimant2", "display_name": "乙", "share": 50},
        ])
        self.assertEqual(r["share_status"], "confirmed")
        party_id = [p for p in self.store.get_claim_shares("reviewer1", claim["id"])["parties"]
                    if p["claimant_id"] == "claimant2"][0]["id"]
        w = self.store.withdraw_party("reviewer1", claim["id"], party_id)
        self.assertEqual(w["share_status"], "pending")
        self.assertEqual(w["total"], 50.0)

    def test_legacy_claims_backfilled_to_single_owner(self):
        # 直接插入一条没有份额记录的旧主张，模拟历史数据。
        obj = self.store.create_object("staff", "M-2020-9", "瓷器", "瓶", "市博物馆", "旧藏品。")
        with self.store.connect() as conn:
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'resolved_return', ?,?)""",
                (obj["id"], "claimant1", "旧权利人", "返还", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
            )
            legacy_id = cur.lastrowid
        # 重新初始化触发回填。
        self.store.init_schema()
        shares = self.store.get_claim_shares("reviewer1", legacy_id)
        self.assertEqual(shares["share_status"], "confirmed")
        self.assertEqual(shares["total"], 100.0)
        self.assertEqual(len(shares["parties"]), 1)
        self.assertEqual(shares["parties"][0]["claimant_id"], "claimant1")
        self.assertEqual(shares["parties"][0]["share"], 100.0)

    def test_invalid_share_payloads_rejected(self):
        obj, claim = self._obj_and_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_claim_shares("reviewer1", claim["id"], [
                {"claimant_id": "claimant1", "display_name": "甲", "share": 150},
            ])
        self.assertEqual(ctx.exception.code, "invalid_share")
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_claim_shares("reviewer1", claim["id"], [
                {"claimant_id": "claimant1", "display_name": "甲", "share": 60},
                {"claimant_id": "claimant1", "display_name": "甲重复", "share": 40},
            ])
        self.assertEqual(ctx.exception.code, "duplicate_party")
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_claim_shares("reviewer1", claim["id"], [
                {"claimant_id": "public", "display_name": "公众", "share": 100},
            ])
        self.assertEqual(ctx.exception.code, "invalid_party_role")

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
