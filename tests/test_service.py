"""作品与场地服务的端到端业务规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.service import (
    AUTHOR,
    CARRIER,
    CLAIM_PAID,
    CLAIM_VERIFIED,
    FREEZE_CUSTOMS,
    FREEZE_DAMAGE,
    FREEZE_VENUE_CLOSED,
    GRANT_ACTIVE,
    GRANT_PENDING,
    GRANT_REJECTED,
    OPERATOR,
    REVIEWER,
    SHOW_COMPLETED,
    VENUE_CLOSED,
    VENUE_MANAGER,
    DomainError,
    ExhibitionService,
)
from src.store import JsonStore


FP1 = "a" * 64
FP2 = "b" * 64
FP3 = "c" * 64


class MutableClock:
    def __init__(self, moment: str):
        self.value = datetime.fromisoformat(moment).replace(tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, moment: str) -> None:
        self.value = datetime.fromisoformat(moment).replace(tzinfo=timezone.utc)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.json"
        self.clock = MutableClock("2026-10-01T08:00:00+00:00")
        self.svc = self._service(self.db_path)
        # 参与方
        self.operator = {"id": "op1", "role": OPERATOR}
        self.reviewer = {"id": "rv1", "role": REVIEWER}
        self.other_reviewer = {"id": "rv2", "role": REVIEWER}
        self.carrier = {"id": "car1", "role": CARRIER}
        self.author = self._artist("摄影师蒙晓", "13800000000")
        self.other_author = self._artist("摄影师阿荔", "13800000001")
        self.manager = self._artist("洪江村场地负责人", "13800000002", role=VENUE_MANAGER)
        self.other_manager = self._artist("拉桥村场地负责人", "13800000003", role=VENUE_MANAGER)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _service(self, path: Path) -> ExhibitionService:
        return ExhibitionService(JsonStore(path), clock=self.clock)

    def _reload(self) -> ExhibitionService:
        self.svc = self._service(self.db_path)
        return self.svc

    def _artist(self, name: str, contact: str, role: str = AUTHOR) -> dict:
        return self.svc.register_artist(name, contact, role=role)

    def _venue(self, name: str = "洪江粮仓展点", capacity: int = 2, manager_id: str | None = None):
        return self.svc.register_venue(
            name, "荔波县洪江村", manager_id or self.manager["id"], capacity
        )

    def _work(
        self, fp: str = FP1, author_id: str | None = None,
        territories=("贵州", "中国"), valid_until: str = "2026-12-31",
        valid_from: str = "2026-09-01", submitter=None, scope: str = "exhibition",
        title: str = "村寨晨光",
    ):
        return self.svc.submit_artwork(
            title=title,
            author_id=author_id or self.author["id"],
            fingerprint=fp,
            territories=list(territories),
            valid_from=valid_from,
            valid_until=valid_until,
            submitted_by=submitter or {"id": "sub1", "role": AUTHOR},
            scope=scope,
        )

    def _show(
        self, name: str = "洪江首展", territory: str = "贵州",
        start: str = "2026-10-01", end: str = "2026-12-30", venue_id: str | None = None,
    ):
        venue_id = venue_id or self._venue()["id"]
        return self.svc.schedule_show(name, venue_id, start, end, territory, self.operator)


class SubmissionTest(ServiceCase):
    def test_identical_resubmit_returns_original_result(self) -> None:
        first = self._work()
        again = self._work()
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["artwork_id"], again["artwork_id"])
        self.assertEqual(first["grant_id"], again["grant_id"])
        works = list(self.svc.store.collection("artworks").values())
        self.assertEqual(len(works), 1)
        self.assertEqual(len(works[0]["grant_ids"]), 1)

    def test_same_fingerprint_different_terms_enters_review(self) -> None:
        self._work(valid_until="2026-12-31")
        result = self._work(valid_until="2027-06-30")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["status"], GRANT_PENDING)
        pending = self.svc.list_pending_reviews()
        self.assertEqual(len(pending), 1)
        # 复核期间新版本不可使用：仍沿用第一版授权
        work = self.svc.store.get("artworks", result["artwork_id"])
        current = self.svc.store.get("grants", work["current_grant_id"])
        self.assertEqual(current["valid_until"], "2026-12-31")

    def test_same_fingerprint_different_author_enters_review(self) -> None:
        self._work()
        result = self._work(author_id=self.other_author["id"])
        self.assertEqual(result["status"], GRANT_PENDING)
        self.assertIn("作者", result["note"])

    def test_approve_review_switches_current_version(self) -> None:
        self._work(valid_until="2026-12-31")
        result = self._work(valid_until="2027-06-30")
        first_grant_id = self.svc.store.get("artworks", result["artwork_id"])["grant_ids"][0]
        self.svc.review_grant(self.reviewer, result["grant_id"], "approve", "展期延长属实")
        work = self.svc.store.get("artworks", result["artwork_id"])
        current = self.svc.store.get("grants", work["current_grant_id"])
        self.assertEqual(current["status"], GRANT_ACTIVE)
        self.assertEqual(current["valid_until"], "2027-06-30")
        old = self.svc.store.get("grants", first_grant_id)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], result["grant_id"])
        self.assertEqual(len(self.svc.list_pending_reviews()), 0)

    def test_reject_review_keeps_old_version(self) -> None:
        self._work(valid_until="2026-12-31")
        result = self._work(territories=("贵州", "中国", "哈萨克斯坦"))
        self.svc.review_grant(self.reviewer, result["grant_id"], "reject", "材料不足")
        grant = self.svc.store.get("grants", result["grant_id"])
        self.assertEqual(grant["status"], GRANT_REJECTED)
        work = self.svc.store.get("artworks", result["artwork_id"])
        self.assertNotEqual(work["current_grant_id"], result["grant_id"])

    def test_different_fingerprint_is_new_artwork(self) -> None:
        first = self._work(FP1)
        second = self._work(FP2, title="河畔暮影")
        self.assertNotEqual(first["artwork_id"], second["artwork_id"])
        self.assertEqual(second["status"], GRANT_ACTIVE)


class AdmissionTest(ServiceCase):
    def test_authorized_work_is_admitted_and_explained(self) -> None:
        work = self._work()
        show = self._show()
        decision = self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.assertEqual(decision["decision"], "approved")
        explanation = self.svc.explain(work["artwork_id"], show["id"])
        self.assertEqual(explanation["admission"]["grant_version"], 1)
        self.assertEqual(explanation["admission"]["reasons"], [])

    def test_expired_grant_blocks_new_show(self) -> None:
        work = self._work(valid_until="2026-11-30")
        show = self._show(end="2026-12-30")
        with self.assertRaisesRegex(DomainError, "授权于2026-11-30到期"):
            self.svc.admit(work["artwork_id"], show["id"], self.operator)
        explanation = self.svc.explain(work["artwork_id"], show["id"])
        self.assertEqual(explanation["admission"]["decision"], "rejected")

    def test_territory_gate_blocks_central_asia_without_exception(self) -> None:
        work = self._work(territories=("贵州", "中国"))
        show = self._show(name="中亚撒马尔罕站", territory="乌兹别克斯坦",
                          start="2027-01-10", end="2027-02-10")
        with self.assertRaisesRegex(DomainError, "不包含场次地域"):
            self.svc.admit(work["artwork_id"], show["id"], self.operator)

    def test_capacity_blocks_after_venue_full(self) -> None:
        venue = self._venue(capacity=1)
        show = self.svc.schedule_show("容量展", venue["id"], "2026-10-01", "2026-12-30",
                                      "贵州", self.operator)
        w1 = self._work(FP1, title="作品一")
        w2 = self._work(FP2, title="作品二")
        self.svc.admit(w1["artwork_id"], show["id"], self.operator)
        with self.assertRaisesRegex(DomainError, "容量"):
            self.svc.admit(w2["artwork_id"], show["id"], self.operator)

    def test_closed_venue_rejects_admission(self) -> None:
        venue = self._venue()
        show = self.svc.schedule_show("闭馆展", venue["id"], "2026-10-01", "2026-12-30",
                                      "贵州", self.operator)
        work = self._work()
        self.svc.close_venue(venue["id"], self.operator, "汛期安全")
        with self.assertRaisesRegex(DomainError, "展点已关闭"):
            self.svc.admit(work["artwork_id"], show["id"], self.operator)


class ExceptionTest(ServiceCase):
    def test_submitter_cannot_approve_own_exception(self) -> None:
        work = self._work(valid_until="2026-11-30")
        show = self._show(end="2026-12-30")
        request = self.svc.request_exception(
            work["artwork_id"], show["id"],
            {"id": "rv1", "role": REVIEWER}, "展期与撤展冲突", "expiry",
        )
        with self.assertRaisesRegex(DomainError, "不能批准自己的例外"):
            self.svc.decide_exception(self.reviewer, request["id"], "approve")

    def test_exception_approved_by_another_reviewer_allows_expiry(self) -> None:
        work = self._work(valid_until="2026-11-30")
        show = self._show(end="2026-12-30")
        request = self.svc.request_exception(
            work["artwork_id"], show["id"], self.reviewer, "展期与撤展冲突", "expiry"
        )
        self.svc.decide_exception(self.other_reviewer, request["id"], "approve", "同意延展1个月")
        decision = self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.assertEqual(decision["decision"], "approved")
        self.assertTrue(any("到期例外" in note for note in decision["allowances"]))

    def test_rejected_exception_still_blocks(self) -> None:
        work = self._work(territories=("贵州", "中国"))
        show = self._show(name="中亚塔什干站", territory="乌兹别克斯坦",
                          start="2027-01-10", end="2027-02-10")
        request = self.svc.request_exception(
            work["artwork_id"], show["id"], self.operator, "随团展出", "territory"
        )
        self.svc.decide_exception(self.reviewer, request["id"], "reject")
        with self.assertRaisesRegex(DomainError, "不包含场次地域"):
            self.svc.admit(work["artwork_id"], show["id"], self.operator)


class FreezeTest(ServiceCase):
    def _two_batches(self):
        """两个场次、两个批次，分别在不同展点。"""
        venue1 = self._venue(name="洪江粮仓展点", manager_id=self.manager["id"])
        venue2 = self.svc.register_venue("拉桥河畔展点", "荔波县拉桥村",
                                         self.other_manager["id"], 5)
        s1 = self.svc.schedule_show("洪江展", venue1["id"], "2026-10-01", "2026-12-30",
                                    "贵州", self.operator)
        s2 = self.svc.schedule_show("拉桥展", venue2["id"], "2026-10-01", "2026-12-30",
                                    "贵州", self.operator)
        w1 = self._work(FP1, title="作品一")
        w2 = self._work(FP2, title="作品二")
        self.svc.admit(w1["artwork_id"], s1["id"], self.operator)
        self.svc.admit(w2["artwork_id"], s2["id"], self.operator)
        b1 = self.svc.create_batch("洪江批次", s1["id"], [w1["artwork_id"]], self.operator)
        b2 = self.svc.create_batch("拉桥批次", s2["id"], [w2["artwork_id"]], self.operator)
        return s1, s2, w1, w2, b1, b2

    def test_venue_close_freezes_only_affected_batch(self) -> None:
        s1, s2, w1, w2, b1, b2 = self._two_batches()
        frozen = self.svc.close_venue(s1["venue_id"], self.operator, "民居修缮")
        self.assertEqual(frozen, [b1["id"]])
        self.assertEqual(self.svc.store.get("batches", b1["id"])["status"], "frozen")
        self.assertEqual(self.svc.store.get("batches", b2["id"])["status"], "active")
        self.assertEqual(self.svc.store.get("venues", s1["venue_id"])["status"], VENUE_CLOSED)
        # 其他展点照常推进：可以布展
        self.svc.install_artwork(b2["id"], w2["artwork_id"], self.carrier,
                                 from_party="承运方", to_party="拉桥场地")

    def test_damage_freezes_only_its_batch(self) -> None:
        s1, s2, w1, w2, b1, b2 = self._two_batches()
        self.svc.report_damage(b1["id"], w1["artwork_id"], self.carrier, "画框受潮")
        self.assertEqual(self.svc.store.get("batches", b1["id"])["freeze_reason"], FREEZE_DAMAGE)
        self.assertEqual(self.svc.store.get("batches", b2["id"])["status"], "active")

    def test_customs_delay_only_for_cross_border_batch(self) -> None:
        work = self._work(FP1, territories=("贵州", "中国", "哈萨克斯坦"),
                          valid_until="2027-12-31")
        domestic_show = self._show()
        border_show = self.svc.schedule_show(
            "中亚阿拉木图站", domestic_show["venue_id"], "2027-01-10", "2027-03-01",
            "哈萨克斯坦", self.operator,
        )
        self.svc.admit(work["artwork_id"], border_show["id"], self.operator)
        domestic = self.svc.create_batch("省内批次", domestic_show["id"], [work["artwork_id"]],
                                         self.operator)
        border = self.svc.create_batch("中亚批次", border_show["id"], [work["artwork_id"]],
                                       self.operator, cross_border=True)
        with self.assertRaisesRegex(DomainError, "不是跨境"):
            self.svc.report_customs_delay(domestic["id"], self.carrier)
        self.svc.report_customs_delay(border["id"], self.carrier, "霍尔果斯口岸滞留")
        self.assertEqual(
            self.svc.store.get("batches", border["id"])["freeze_reason"], FREEZE_CUSTOMS
        )
        self.assertEqual(self.svc.store.get("batches", domestic["id"])["status"], "active")

    def test_frozen_batch_blocks_install_until_unfreeze(self) -> None:
        s1, s2, w1, w2, b1, b2 = self._two_batches()
        self.svc.freeze_batch(b1["id"], self.operator, FREEZE_DAMAGE, "等待定损")
        with self.assertRaisesRegex(DomainError, "批次已冻结"):
            self.svc.install_artwork(b1["id"], w1["artwork_id"], self.carrier,
                                     from_party="承运方", to_party="洪江场地")
        self.svc.unfreeze_batch(b1["id"], self.operator, "定损完成")
        handover = self.svc.install_artwork(b1["id"], w1["artwork_id"], self.carrier,
                                            from_party="承运方", to_party="洪江场地")
        self.assertEqual(handover["action"], "installed")
        self.assertEqual(len(self.svc.store.get("batches", b1["id"])["freeze_history"]), 1)


class HandoverTest(ServiceCase):
    def _installed(self):
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch(
            "首展批次", show["id"], [work["artwork_id"]], self.operator
        )
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.install_artwork(batch["id"], work["artwork_id"], self.carrier,
                                 from_party="承运方老周", to_party="场地负责人")
        return work, show, batch

    def test_install_requires_admission(self) -> None:
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator)
        with self.assertRaisesRegex(DomainError, "未获准"):
            self.svc.install_artwork(batch["id"], work["artwork_id"], self.carrier,
                                     from_party="承运方", to_party="场地")

    def test_return_and_explain_chain(self) -> None:
        work, show, batch = self._installed()
        returned = self.svc.return_artwork(batch["id"], work["artwork_id"], self.manager,
                                           from_party="场地负责人", to_party="承运方老周",
                                           note="撤展回收")
        self.assertEqual(returned["action"], "returned")
        explanation = self.svc.explain(work["artwork_id"], show["id"])
        actions = [h["action"] for h in explanation["custody_chain"]]
        self.assertEqual(actions, ["installed", "returned"])
        self.assertEqual(explanation["custody_chain"][1]["to_party"], "承运方老周")
        # 已回收的作品不能重复撤回
        with self.assertRaisesRegex(DomainError, "无需撤回"):
            self.svc.return_artwork(batch["id"], work["artwork_id"], self.manager,
                                    from_party="场地", to_party="承运方")


class CompletionSnapshotTest(ServiceCase):
    def test_completed_show_freezes_version_and_rejects_new_use(self) -> None:
        work = self._work(valid_until="2026-12-31")
        show = self._show(end="2026-12-20")
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.install_artwork(batch["id"], work["artwork_id"], self.carrier,
                                 from_party="承运方", to_party="场地")
        self.svc.add_translation(work["artwork_id"], "ru", "Деревенский рассвет", self.operator)
        self.svc.return_artwork(batch["id"], work["artwork_id"], self.manager,
                                from_party="场地", to_party="承运方")
        snapshot = self.svc.complete_show(show["id"], self.operator)
        completed = self.svc.store.get("shows", show["id"])
        self.assertEqual(completed["status"], SHOW_COMPLETED)
        item = snapshot["items"][0]
        self.assertEqual(item["grant"]["valid_until"], "2026-12-31")
        self.assertIn("ru", item["translations"])
        self.assertEqual(item["returned"]["to_party"], "承运方")

        # 完成后即使作者更新了授权/译文，也不能对该场次新增使用
        self.svc.add_translation(work["artwork_id"], "ru", "ОБНОВЛЁННЫЙ текст", self.operator)
        with self.assertRaisesRegex(DomainError, "已完成"):
            self.svc.admit(work["artwork_id"], show["id"], self.operator)
        with self.assertRaisesRegex(DomainError, "已完成"):
            self.svc.create_batch("补拍批次", show["id"], [work["artwork_id"]], self.operator)
        # 解释仍指向当时固化的版本
        explanation = self.svc.explain(work["artwork_id"], show["id"])
        self.assertEqual(
            explanation["completed_snapshot"]["translations"]["ru"]["version"], 1
        )

    def test_complete_blocked_while_artwork_outstanding(self) -> None:
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.install_artwork(batch["id"], work["artwork_id"], self.carrier,
                                 from_party="承运方", to_party="场地")
        check = self.svc.teardown_check(show["id"])
        self.assertFalse(check["complete"])
        self.assertEqual(check["outstanding"], [work["artwork_id"]])
        self.svc.return_artwork(batch["id"], work["artwork_id"], self.manager,
                                from_party="场地", to_party="承运方")
        check = self.svc.teardown_check(show["id"])
        self.assertTrue(check["complete"])


class ClaimTest(ServiceCase):
    def _damaged_with_policy(self):
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator,
                                      cross_border=True)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.create_policy(batch["id"], 20000.0, "村展互保计划", self.operator)
        self.svc.report_damage(batch["id"], work["artwork_id"], self.carrier, "运输受潮")
        return work, show, batch

    def test_claim_requires_damage_record(self) -> None:
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.create_policy(batch["id"], 20000.0, "村展互保计划", self.operator)
        with self.assertRaisesRegex(DomainError, "损坏交接记录"):
            self.svc.file_claim(work["artwork_id"], batch["id"], 5000.0, self.operator)

    def test_verify_and_settle_claim(self) -> None:
        work, show, batch = self._damaged_with_policy()
        claim = self.svc.file_claim(work["artwork_id"], batch["id"], 8000.0, self.operator)
        # 同一损坏事件重复申报幂等
        again = self.svc.file_claim(work["artwork_id"], batch["id"], 8000.0, self.operator)
        self.assertEqual(again["id"], claim["id"])
        verified = self.svc.verify_claim(claim["id"], self.reviewer)
        self.assertEqual(verified["status"], CLAIM_VERIFIED)
        paid = self.svc.settle_claim(claim["id"], self.operator)
        self.assertEqual(paid["status"], CLAIM_PAID)
        # 撤展清点把受损作品标出
        check = self.svc.teardown_check(show["id"])
        self.assertIn(work["artwork_id"], check["damaged"])

    def test_verify_rejects_amount_over_coverage(self) -> None:
        work, show, batch = self._damaged_with_policy()
        claim = self.svc.file_claim(work["artwork_id"], batch["id"], 99999.0, self.operator)
        with self.assertRaisesRegex(DomainError, "超出保额"):
            self.svc.verify_claim(claim["id"], self.reviewer)

    def test_verify_without_policy_fails(self) -> None:
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.report_damage(batch["id"], work["artwork_id"], self.carrier, "画框破裂")
        claim = self.svc.file_claim(work["artwork_id"], batch["id"], 3000.0, self.operator)
        with self.assertRaisesRegex(DomainError, "没有有效保险单"):
            self.svc.verify_claim(claim["id"], self.reviewer)


class PrivacyTest(ServiceCase):
    def test_search_results_never_contain_contacts(self) -> None:
        for artist in self.svc.search_artists("摄影"):
            self.assertNotIn("contact", artist)
        for item in self.svc.search_catalog(""):
            self.assertNotIn("contact", item)
        raw = self.db_path.read_text(encoding="utf-8")
        # 联系方式单独存放在 contacts 保护区，而不是作者主档
        self.assertIn("contacts", raw)

    def test_contact_reveal_requires_role_and_is_audited(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.reveal_contact({"id": self.manager["id"], "role": VENUE_MANAGER},
                                    self.author["id"])
        contact = self.svc.reveal_contact(self.reviewer, self.author["id"])
        self.assertEqual(contact, "13800000000")
        actions = [row["action"] for row in self.svc.store.state["audit"]]
        self.assertIn("reveal_contact", actions)

    def test_venue_manager_sees_only_own_venue(self) -> None:
        v1 = self._venue(manager_id=self.manager["id"])
        v2 = self.svc.register_venue("拉桥河畔展点", "荔波县拉桥村",
                                     self.other_manager["id"], 5)
        own = {"id": self.manager["id"], "role": VENUE_MANAGER}
        other = {"id": self.other_manager["id"], "role": VENUE_MANAGER}
        self.svc.venue_schedule(own, v1["id"])
        with self.assertRaisesRegex(DomainError, "本场地"):
            self.svc.venue_schedule(own, v2["id"])
        self.svc.venue_schedule(other, v2["id"])


class ReminderTest(ServiceCase):
    def test_expiry_reminders_window_and_idempotent(self) -> None:
        self._work(valid_until="2026-10-20")  # 19天后到期
        self._work(FP2, valid_until="2027-06-30", title="远期作品")
        due = self.svc.expiry_reminders(window_days=30)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["days_left"], 19)
        # 同一天重复扫描不再产生提醒
        again = self.svc.expiry_reminders(window_days=30)
        self.assertEqual(again, [])
        # 越过到期日后该授权进入到期/过期，仍被列出（delta 可为负）
        self.clock.advance("2026-10-21T08:00:00+00:00")
        overdue = self.svc.expiry_reminders(window_days=30)
        self.assertEqual(len(overdue), 1)
        self.assertLessEqual(overdue[0]["days_left"], -1)


class PersistenceTest(ServiceCase):
    def test_restart_resumes_reminders_teardown_and_claims(self) -> None:
        work = self._work()
        show = self._show()
        batch = self.svc.create_batch("批次", show["id"], [work["artwork_id"]], self.operator,
                                      cross_border=True)
        self.svc.admit(work["artwork_id"], show["id"], self.operator)
        self.svc.create_policy(batch["id"], 20000.0, "村展互保计划", self.operator)
        self.svc.install_artwork(batch["id"], work["artwork_id"], self.carrier,
                                 from_party="承运方", to_party="场地")
        self.svc.report_damage(batch["id"], work["artwork_id"], self.carrier, "受潮")
        claim = self.svc.file_claim(work["artwork_id"], batch["id"], 6000.0, self.operator)
        self.clock.advance("2026-12-15T08:00:00+00:00")
        self.svc.expiry_reminders(window_days=30)

        # 模拟服务重启
        restarted = self._reload()
        # 冻结状态与赔付核对接续
        self.assertEqual(batch_status(restarted, batch["id"]), "frozen")
        verified = restarted.verify_claim(claim["id"], self.reviewer)
        self.assertEqual(verified["status"], CLAIM_VERIFIED)
        restarted.settle_claim(claim["id"], self.operator)
        # 撤展清点仍准确
        check = restarted.teardown_check(show["id"])
        self.assertIn(work["artwork_id"], check["outstanding"])
        self.assertIn(work["artwork_id"], check["damaged"])
        # 到期提醒已登记，不重复
        self.assertEqual(restarted.expiry_reminders(today="2026-12-15", window_days=30), [])
        # 解冻后流程照常接续：回收 -> 完成
        restarted.unfreeze_batch(batch["id"], self.operator)
        restarted.return_artwork(batch["id"], work["artwork_id"], self.manager,
                                 from_party="场地", to_party="承运方")
        snapshot = restarted.complete_show(show["id"], self.operator)
        self.assertEqual(snapshot["teardown"]["complete"], True)


def batch_status(svc: ExhibitionService, batch_id: str) -> str:
    return svc.store.get("batches", batch_id)["status"]


if __name__ == "__main__":
    unittest.main()
