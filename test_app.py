import base64
import tempfile
import threading
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

    def test_co_claim_shares_pending_then_confirmed(self):
        obj = self.store.create_object("staff", "M-2010-1", "油画", "绘画", "库房", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "李氏家族", "返还油画")
        # 新主张默认登记人一人独占 100%，已确认
        view = self.store.get_object("reviewer1", obj["id"])
        self.assertEqual(view["claims"][0]["allocation"], {"version": 1, "status": "confirmed", "total_percent": 100.0,
                                                           "created_by": "claimant1", "created_at": view["claims"][0]["allocation"]["created_at"]})
        self.assertEqual(view["claims"][0]["shares"][0]["party"], "李氏家族")
        # 登记共同继承人份额：合计 80% 不足 100%，停在待补齐，原确认作废
        result = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"party": "李氏家族", "share_percent": 0},
            {"party": "李甲", "share_percent": 50},
            {"party": "李乙", "share_percent": 30},
        ])
        self.assertEqual(result["allocation"]["status"], "pending_completion")
        self.assertEqual(result["allocation"]["total_percent"], 80.0)
        history = self.store.claim_allocations("reviewer1", claim["id"])
        self.assertEqual([h["status"] for h in history], ["voided", "pending_completion"])
        self.assertEqual(history[0]["shares"], [{"party": "李氏家族", "share_percent": 100.0}])
        # 李乙退出，合计降到 50%，仍是待补齐
        result = self.store.set_claim_shares("reviewer1", claim["id"], [{"party": "李乙", "share_percent": 0}])
        self.assertEqual(result["allocation"]["total_percent"], 50.0)
        self.assertEqual(result["allocation"]["status"], "pending_completion")
        # 补齐到 100% 后确认
        result = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"party": "李乙", "share_percent": 40},
            {"party": "李丙", "share_percent": 10},
        ])
        self.assertEqual(result["allocation"]["status"], "confirmed")
        self.assertEqual(result["allocation"]["total_percent"], 100.0)
        shares = {s["party"]: s["share_percent"] for s in result["shares"]}
        self.assertEqual(shares, {"李甲": 50.0, "李乙": 40.0, "李丙": 10.0})

    def test_share_modify_forbidden_for_claimant_and_hidden_from_public(self):
        obj = self.store.create_object("staff", "M-2011-2", "玉雕", "玉器", "库房", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "赵氏家族", "返还玉雕")
        # 主张人越权改份额要拒绝，公众更无权
        for user in ("claimant1", "public"):
            with self.assertRaises(BusinessError) as ctx:
                self.store.set_claim_shares(user, claim["id"], [{"party": "赵甲", "share_percent": 50}])
            self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.reallocate_claim_shares("claimant1", claim["id"])
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_claim_shares("reviewer1", claim["id"], [{"party": "赵氏家族", "share_percent": 0}, {"party": "赵甲", "share_percent": 60}])
        # 公众看不到份额明细
        public_claim = self.store.get_object("public", obj["id"])["claims"][0]
        self.assertNotIn("shares", public_claim)
        self.assertNotIn("allocation", public_claim)
        # 主张人本人可以看到自己主张的份额
        claimant_claim = self.store.get_object("claimant1", obj["id"])["claims"][0]
        self.assertIn("shares", claimant_claim)
        self.assertEqual(claimant_claim["allocation"]["status"], "pending_completion")

    def test_share_overflow_rolls_back_and_retry(self):
        obj = self.store.create_object("staff", "M-2012-3", "石碑", "石刻", "库房", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "孙氏家族", "返还石碑")
        # 合计 120% 超过 100%，重新分配失败
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_claim_shares("reviewer1", claim["id"], [
                {"party": "孙氏家族", "share_percent": 0},
                {"party": "孙甲", "share_percent": 60},
                {"party": "孙乙", "share_percent": 60},
            ])
        self.assertEqual(ctx.exception.code, "share_overflow")
        # 失败后整体回滚：仍是初始的一人独占确认，可安全重试
        view = self.store.get_object("reviewer1", obj["id"])
        self.assertEqual(view["claims"][0]["allocation"]["status"], "confirmed")
        self.assertEqual(view["claims"][0]["allocation"]["total_percent"], 100.0)
        self.assertEqual(len(self.store.claim_allocations("reviewer1", claim["id"])), 1)
        # 修正份额后重试成功
        result = self.store.set_claim_shares("reviewer1", claim["id"], [
            {"party": "孙氏家族", "share_percent": 0},
            {"party": "孙甲", "share_percent": 60},
            {"party": "孙乙", "share_percent": 40},
        ])
        self.assertEqual(result["allocation"]["status"], "confirmed")
        # 显式重分配端点：按当前份额重算，生成新版本
        retry = self.store.reallocate_claim_shares("reviewer1", claim["id"])
        self.assertEqual(retry["allocation"]["status"], "confirmed")
        self.assertEqual(retry["allocation"]["total_percent"], 100.0)
        history = self.store.claim_allocations("reviewer1", claim["id"])
        self.assertEqual([h["status"] for h in history], ["voided", "voided", "confirmed"])

    def test_concurrent_share_updates_last_wins_and_history_kept(self):
        obj = self.store.create_object("staff", "M-2013-4", "瓷器", "陶瓷", "库房", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还瓷器")
        errors = []

        def update(user, shares):
            try:
                self.store.set_claim_shares(user, claim["id"], shares)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # 两个审查员同时修改：后提交者基于最新状态分配，两人份额都保留
        t1 = threading.Thread(target=update, args=("reviewer1", [{"party": "王氏家族", "share_percent": 0}, {"party": "王甲", "share_percent": 50}]))
        t2 = threading.Thread(target=update, args=("reviewer2", [{"party": "王氏家族", "share_percent": 0}, {"party": "王乙", "share_percent": 50}]))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        shares = {s["party"]: s["share_percent"] for s in self.store.get_object("reviewer1", obj["id"])["claims"][0]["shares"]}
        self.assertEqual(shares, {"王甲": 50.0, "王乙": 50.0})
        history = self.store.claim_allocations("reviewer1", claim["id"])
        self.assertEqual(len(history), 3)  # 初始确认 + 两次并发修改各留一版
        self.assertEqual([h["status"] for h in history], ["voided", "voided", "confirmed"])
        self.assertEqual(history[-1]["total_percent"], 100.0)
        # 同一方被先后修改时，后提交的以最新为准，上一版留档
        self.store.set_claim_shares("reviewer1", claim["id"], [{"party": "王甲", "share_percent": 60}, {"party": "王乙", "share_percent": 40}])
        self.store.set_claim_shares("reviewer2", claim["id"], [{"party": "王甲", "share_percent": 70}, {"party": "王乙", "share_percent": 30}])
        shares = {s["party"]: s["share_percent"] for s in self.store.get_object("reviewer1", obj["id"])["claims"][0]["shares"]}
        self.assertEqual(shares["王甲"], 70.0)
        history = self.store.claim_allocations("reviewer1", claim["id"])
        self.assertEqual(len(history), 5)
        previous = [h for h in history if h["version"] == 4][0]
        self.assertEqual(previous["status"], "voided")
        self.assertEqual({s["party"]: s["share_percent"] for s in previous["shares"]}["王甲"], 60.0)

    def test_legacy_claim_without_shares_backfilled(self):
        obj = self.store.create_object("staff", "M-1995-5", "佛像", "雕塑", "库房", "公开简介。")
        # 模拟旧数据：直接写入一条没有任何份额记录的主张，并重放迁移
        with self.store.connect() as conn:
            cur = conn.execute(
                "INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (obj["id"], "claimant1", "旧主张人", "返还佛像", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
            )
            claim_id = cur.lastrowid
            conn.execute("PRAGMA user_version=0")
        self.store.init_schema()
        # 缺份额的旧主张按一人独占 100% 回填并确认
        allocs = self.store.claim_allocations("reviewer1", claim_id)
        self.assertEqual(len(allocs), 1)
        self.assertEqual(allocs[0]["status"], "confirmed")
        self.assertEqual(allocs[0]["total_percent"], 100.0)
        self.assertEqual(allocs[0]["shares"], [{"party": "旧主张人", "share_percent": 100.0}])
        legacy = [c for c in self.store.get_object("reviewer1", obj["id"])["claims"] if c["id"] == claim_id][0]
        self.assertEqual(legacy["shares"][0]["party"], "旧主张人")
        # 迁移幂等：再次初始化不会重复回填
        self.store.init_schema()
        self.assertEqual(len(self.store.claim_allocations("reviewer1", claim_id)), 1)


if __name__ == "__main__":
    unittest.main()
