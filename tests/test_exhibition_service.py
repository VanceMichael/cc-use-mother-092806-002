"""作品与场地服务的业务规则测试。"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from src.exhibition_service import (
    BATCH_ACTIVE,
    BATCH_FROZEN,
    CLAIM_PAID,
    FREEZE_TRANSPORT_DELAY,
    FREEZE_VENUE_CLOSED,
    GRANT_PENDING,
    SESSION_CLOSED,
    SESSION_OPENED,
    SESSION_PLANNED,
    ExhibitionService,
    ServiceError,
    fingerprint_bytes,
)


class Clock:
    """可拨快的测试时钟。"""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: int = 0, **kw) -> None:
        self.now += timedelta(days=days, **kw)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(datetime(2026, 10, 1, 9, 0))
        self.svc = ExhibitionService(clock=self.clock)
        # 用户：运营、作者、复核人、保险、村民、承运人
        self.svc.add_user("u-ops", "运营小周", "OPERATIONS")
        self.svc.add_user("u-art", "摄影师阿麦", "ARTIST")
        self.svc.add_user("u-rev", "版权审核老黎", "COPYRIGHT_REVIEWER")
        self.svc.add_user("u-ins", "保险审核小潘", "INSURANCE_REVIEWER")
        self.svc.add_user("u-host", "洪江村村民韦姐", "VENUE_HOST")
        self.svc.add_user("u-host2", "尧古村村民莫哥", "VENUE_HOST")
        self.svc.add_user("u-car", "承运人阿强", "CARRIER")
        self.svc.add_artist(
            "art-1", "阿麦",
            {"phone": "13800000000", "email": "amai@example.test"},
        )
        self.svc.add_artist("art-2", "木卡", {"phone": "13900000000"})
        self.svc.add_venue("v-hongjiang", "洪江村粮仓", "贵州荔波", "u-host", 3)
        self.svc.add_venue("v-yaogu", "尧古村河岸", "贵州荔波", "u-host2", 2)
        self.svc.create_batch("b-guizhou", "贵州本土巡展", "CN-GZ")
        self.svc.create_batch("b-central-asia", "中亚巡展", "KZ")

    def _register(self, fp_suffix: str = b"a", until: date | None = None,
                  territory=("CN-GZ", "KZ"), by: str = "u-art",
                  title: str = "梯田晨雾"):
        return self.svc.register_artwork(
            title=title, artist_id="art-1", submitted_by=by,
            territory=list(territory),
            valid_from=date(2026, 10, 1),
            valid_until=until or date(2026, 12, 31),
            raw=b"file:" + fp_suffix,
        )

    def _session(self, sid="s1", venue="v-hongjiang", batch="b-guizhou",
                 territory="CN-GZ", start=date(2026, 10, 5),
                 end=date(2026, 11, 5)):
        return self.svc.create_session(sid, "洪江秋季展", venue, batch,
                                       territory, start, end)


class ArtworkRegistrationTest(ServiceCase):
    def test_duplicate_upload_returns_original_result(self) -> None:
        first = self._register(b"same")
        second = self.svc.register_artwork(
            "梯田晨雾", "art-1", "u-art", ["CN-GZ", "KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"file:same",
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["artwork_id"], first["artwork_id"])
        self.assertEqual(second["grant_id"], first["grant_id"])
        self.assertEqual(len(self.svc.artworks), 1)

    def test_same_fingerprint_different_license_enters_review(self) -> None:
        first = self._register(b"same", territory=("CN-GZ",))
        second = self.svc.register_artwork(
            "梯田晨雾", "art-1", "u-art", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"file:same",
        )
        self.assertEqual(second["status"], "review")
        self.assertEqual(second["artwork_id"], first["artwork_id"])
        grant = self.svc.grants[second["grant_id"]]
        self.assertEqual(grant.status, GRANT_PENDING)
        self.assertEqual(grant.version, 2)

    def test_pending_license_cannot_be_used_until_reviewed(self) -> None:
        self._register(b"same", territory=("CN-GZ",))
        review = self.svc.register_artwork(
            "梯田晨雾", "art-1", "u-art", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"file:same",
        )
        self._session("kz1", batch="b-central-asia", territory="KZ")
        with self.assertRaisesRegex(ServiceError, "复核"):
            self.svc.place_artwork("kz1", review["artwork_id"], "u-ops")
        self.svc.review_grant(review["grant_id"], "u-rev", approve=True)
        placement = self.svc.place_artwork("kz1", review["artwork_id"], "u-ops")
        self.assertEqual(placement.basis_version, 2)

    def test_submitter_cannot_review_own_grant(self) -> None:
        self._register(b"same", territory=("CN-GZ",))
        # 复核人本人提交了新地域授权，就不能再复核它
        review = self.svc.register_artwork(
            "梯田晨雾", "art-1", "u-rev", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"file:same",
        )
        with self.assertRaisesRegex(ServiceError, "不能复核自己"):
            self.svc.review_grant(review["grant_id"], "u-rev", approve=True)

    def test_submitter_cannot_approve_own_exception(self) -> None:
        self._register(b"x", territory=("CN-GZ",), until=date(2026, 10, 10))
        self._session()
        # 由复核人本人提交的例外，不能由其本人批准
        exc = self.svc.submit_exception(
            self._register_result_artwork(), "s1", "u-rev", "撤展补偿展出"
        )
        with self.assertRaisesRegex(ServiceError, "不能批准自己的例外"):
            self.svc.review_exception(exc.id, "u-rev", approve=True)
        # 由另一位复核人批准才合法
        self.svc.add_user("u-rev-other", "版权审核小韦", "COPYRIGHT_REVIEWER")
        self.svc.review_exception(exc.id, "u-rev-other", approve=True)

    def _register_result_artwork(self) -> str:
        return next(iter(self.svc.artworks))


class LicenseEnforcementTest(ServiceCase):
    def test_expired_license_blocks_new_session(self) -> None:
        self._register(b"a", until=date(2026, 10, 20))
        self._session(start=date(2026, 10, 5), end=date(2026, 11, 5))
        with self.assertRaisesRegex(ServiceError, "到期"):
            self.svc.place_artwork("s1", "art-1", "u-ops")

    def test_territory_mismatch_blocks_placement(self) -> None:
        self._register(b"a", territory=("CN-GZ",))
        self._session("kz1", batch="b-central-asia", territory="KZ")
        with self.assertRaisesRegex(ServiceError, "地域"):
            self.svc.place_artwork("kz1", "art-1", "u-ops")

    def test_closed_exhibition_keeps_its_snapshot(self) -> None:
        res = self._register(b"a", until=date(2026, 12, 31))
        self.svc.add_translation("ARTWORK", res["artwork_id"], "kz", "title",
                                 "Таңғы туман", "u-ops")
        self._session()
        self.svc.place_artwork("s1", res["artwork_id"], "u-ops")
        self.svc.record_install_handover(
            "s1", res["artwork_id"], "u-car", "u-host", "u-ops"
        )
        self.svc.open_session("s1", "u-ops")
        self.clock.advance(days=35)
        self.svc.close_session("s1", "u-ops")
        snapshot_before = self.svc.explain_placement("s1", res["artwork_id"])["snapshot"]

        # 闭展后授权到期、再出新版本，均不影响历史场次
        grant = self.svc.grants[res["grant_id"]]
        grant.valid_until = "2026-11-01"
        self.svc.add_translation("ARTWORK", res["artwork_id"], "kz", "title",
                                 "修订版译名", "u-ops")
        snapshot_after = self.svc.explain_placement("s1", res["artwork_id"])["snapshot"]
        self.assertEqual(snapshot_before, snapshot_after)
        self.assertEqual(snapshot_after["valid_until"], "2026-12-31")
        with self.assertRaisesRegex(ServiceError, "已锁定"):
            self.svc.place_artwork("s1", "art-2", "u-ops")

    def test_revoked_license_blocks_new_placements(self) -> None:
        res = self._register(b"a")
        self.svc.revoke_grant(res["grant_id"], "u-ops")
        self._session()
        with self.assertRaisesRegex(ServiceError, "地域"):
            self.svc.place_artwork("s1", res["artwork_id"], "u-ops")


class VenueAndCapacityTest(ServiceCase):
    def test_capacity_is_enforced(self) -> None:
        ids = []
        for i in range(3):
            res = self.svc.register_artwork(
                f"作品{i}", "art-1", "u-art", ["CN-GZ"],
                date(2026, 10, 1), date(2026, 12, 31), raw=f"f{i}".encode(),
            )
            ids.append(res["artwork_id"])
        self._session()
        for art_id in ids:
            self.svc.place_artwork("s1", art_id, "u-ops")
        extra = self.svc.register_artwork(
            "第四幅", "art-2", "u-art", ["CN-GZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"extra",
        )
        with self.assertRaisesRegex(ServiceError, "容量"):
            self.svc.place_artwork("s1", extra["artwork_id"], "u-ops")

    def test_closed_venue_rejects_new_sessions(self) -> None:
        self.svc.close_venue("v-hongjiang", "u-ops")
        with self.assertRaisesRegex(ServiceError, "场地已关闭"):
            self._session()

    def test_closing_venue_freezes_only_affected_batches(self) -> None:
        # 洪江（b-guizhou）有未完成场次；尧古用中亚批次
        self._register(b"a")
        self._session("s1")
        self.svc.place_artwork("s1", "art-1", "u-ops")
        res2 = self.svc.register_artwork(
            "河岸", "art-2", "u-art", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"b",
        )
        self._session("s2", venue="v-yaogu", batch="b-central-asia", territory="KZ")
        self.svc.place_artwork("s2", res2["artwork_id"], "u-ops")

        result = self.svc.close_venue("v-hongjiang", "u-ops")
        self.assertEqual(result["frozen_batches"], ["b-guizhou"])
        self.assertEqual(self.svc.batches["b-guizhou"].status, BATCH_FROZEN)
        self.assertEqual(self.svc.batches["b-central-asia"].status, BATCH_ACTIVE)
        # 另一批次照常开展
        self.svc.record_install_handover("s2", res2["artwork_id"], "u-car", "u-host2", "u-ops")
        opened = self.svc.open_session("s2", "u-ops")
        self.assertEqual(opened.status, SESSION_OPENED)
        # 被冻结批次不能推进
        with self.assertRaisesRegex(ServiceError, "批次已冻结"):
            self.svc.record_install_handover("s1", "art-1", "u-car", "u-host", "u-ops")

    def test_resume_batch_continues_work(self) -> None:
        self._register(b"a")
        self._session()
        self.svc.place_artwork("s1", "art-1", "u-ops")
        self.svc.freeze_batch("b-guizhou", "u-ops", FREEZE_TRANSPORT_DELAY)
        with self.assertRaisesRegex(ServiceError, "批次已冻结"):
            self.svc.record_install_handover("s1", "art-1", "u-car", "u-host", "u-ops")
        self.svc.resume_batch("b-guizhou", "u-ops")
        self.svc.record_install_handover("s1", "art-1", "u-car", "u-host", "u-ops")
        self.assertEqual(self.svc.open_session("s1", "u-ops").status, SESSION_OPENED)
        event = self.svc.batches["b-guizhou"].events[-1]
        self.assertTrue(event.resumed)
        self.assertEqual(event.reason, FREEZE_TRANSPORT_DELAY)


class FreezeIsolationTest(ServiceCase):
    def test_damage_freezes_only_its_batch(self) -> None:
        r1 = self._register(b"a")
        r2 = self.svc.register_artwork(
            "河岸", "art-2", "u-art", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"b",
        )
        self._session("s1")
        self.svc.place_artwork("s1", r1["artwork_id"], "u-ops")
        self._session("s2", venue="v-yaogu", batch="b-central-asia", territory="KZ")
        self.svc.place_artwork("s2", r2["artwork_id"], "u-ops")
        self.svc.record_install_handover("s1", r1["artwork_id"], "u-car", "u-host", "u-ops")
        self.svc.open_session("s1", "u-ops")
        self.svc.close_session("s1", "u-ops")
        self.svc.record_return_handover(
            "s1", r1["artwork_id"], "u-host", "u-car", "u-ops", condition="画框破损"
        )
        damage = self.svc.report_damage(
            "s1", r1["artwork_id"], "u-ops", "画框破损、相纸折痕", 1200.0
        )
        self.assertEqual(self.svc.batches["b-guizhou"].status, BATCH_FROZEN)
        self.assertEqual(self.svc.batches["b-central-asia"].status, BATCH_ACTIVE)
        claim = next(c for c in self.svc.claims.values() if c.damage_id == damage.id)
        reconciled = self.svc.reconcile_claim(claim.id, "u-ins", True, 1000.0)
        self.assertEqual(reconciled.status, CLAIM_PAID)
        self.assertEqual(reconciled.amount, 1000.0)
        with self.assertRaisesRegex(ServiceError, "角色不能执行"):
            self.svc.reconcile_claim(claim.id, "u-host", True)


class HandoverAndRecoveryTest(ServiceCase):
    def test_return_requires_closed_session_and_is_idempotent_blocked(self) -> None:
        r = self._register(b"a")
        self._session()
        self.svc.place_artwork("s1", r["artwork_id"], "u-ops")
        with self.assertRaisesRegex(ServiceError, "布展交接"):
            self.svc.open_session("s1", "u-ops")
        self.svc.record_install_handover("s1", r["artwork_id"], "u-car", "u-host", "u-ops")
        self.svc.open_session("s1", "u-ops")
        with self.assertRaisesRegex(ServiceError, "闭展"):
            self.svc.record_return_handover(
                "s1", r["artwork_id"], "u-host", "u-car", "u-ops"
            )
        self.svc.close_session("s1", "u-ops")
        self.svc.record_return_handover(
            "s1", r["artwork_id"], "u-host", "u-car", "u-ops"
        )
        with self.assertRaisesRegex(ServiceError, "撤展回收已记录"):
            self.svc.record_return_handover(
                "s1", r["artwork_id"], "u-host", "u-car", "u-ops"
            )
        progress = self.svc.deinstall_progress("s1")
        self.assertEqual(progress["recovered"], 1)
        self.assertEqual(progress["total"], 1)

    def test_explain_placement_tells_full_story(self) -> None:
        r = self._register(b"a")
        self.svc.add_translation("ARTWORK", r["artwork_id"], "kz", "title",
                                 "Таңғы туман", "u-ops")
        self._session()
        self.svc.place_artwork("s1", r["artwork_id"], "u-ops")
        self.svc.record_install_handover(
            "s1", r["artwork_id"], "u-car", "u-host", "u-ops", condition="完好"
        )
        self.svc.open_session("s1", "u-ops")
        self.svc.close_session("s1", "u-ops")
        self.clock.advance(days=2)
        self.svc.record_return_handover(
            "s1", r["artwork_id"], "u-host", "u-car", "u-ops", condition="完好"
        )
        explanation = self.svc.explain_placement("s1", r["artwork_id"])
        self.assertIn("阿麦", explanation["why"])
        self.assertIn("授权", explanation["why"])
        self.assertIn("CN-GZ", explanation["why"])
        self.assertEqual(len(explanation["handovers"]), 2)
        self.assertEqual(
            [h["kind"] for h in explanation["handovers"]], ["INSTALL", "RETURN"]
        )
        self.assertEqual(explanation["handovers"][0]["from"], "承运人阿强")
        self.assertEqual(explanation["handovers"][0]["to"], "洪江村村民韦姐")
        self.assertIsNotNone(explanation["recovered_at"])
        self.assertIn("kz:title", explanation["snapshot"]["translations"])


class DueJobsTest(ServiceCase):
    def test_expiry_reminder_and_restart_resume(self) -> None:
        # 授权 20 天后到期，处于 30 天提醒窗口
        res = self._register(b"a", until=date(2026, 10, 21))
        self._session(end=date(2026, 10, 20))
        self.svc.place_artwork("s1", res["artwork_id"], "u-ops")
        items = self.svc.run_due_jobs()
        kinds = [i["kind"] for i in items]
        self.assertIn("EXPIRY_REMINDER", kinds)

        # 冻结批次不影响到期提醒
        self.svc.freeze_batch("b-guizhou", "u-car", FREEZE_TRANSPORT_DELAY)
        second = self.svc.run_due_jobs()
        self.assertEqual(second, [])

        # 落盘后重启：已提醒的不再重复
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.svc.save(path)
            restored = ExhibitionService.load(path, clock=self.clock)
            self.assertEqual(restored.run_due_jobs(), [])
            # 新增临近到期授权，重启后仍能被发现
            new = restored.register_artwork(
                "近河", "art-2", "u-art", ["CN-GZ"],
                date(2026, 9, 1), date(2026, 10, 25), raw=b"new",
            )
            jobs = restored.run_due_jobs()
            self.assertEqual(
                [j["grant_id"] for j in jobs if j["kind"] == "EXPIRY_REMINDER"],
                [new["grant_id"]],
            )

    def test_deinstall_checklist_and_claim_survive_restart(self) -> None:
        r = self._register(b"a")
        self._session()
        self.svc.place_artwork("s1", r["artwork_id"], "u-ops")
        self.svc.record_install_handover("s1", r["artwork_id"], "u-car", "u-host", "u-ops")
        self.svc.open_session("s1", "u-ops")
        self.svc.close_session("s1", "u-ops")
        self.svc.report_damage("s1", r["artwork_id"], "u-ops", "受潮", 800.0)
        jobs = self.svc.run_due_jobs()
        self.assertEqual(
            sorted(j["kind"] for j in jobs),
            ["CLAIM_RECONCILIATION", "DEINSTALL_CHECKLIST"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.svc.save(path)
            restored = ExhibitionService.load(path, clock=self.clock)
            self.assertEqual(restored.run_due_jobs(), [])
            pending = restored.pending_claims()
            self.assertEqual(len(pending), 1)
            restored.reconcile_claim(pending[0].id, "u-ins", True, 800.0)
            progress = restored.deinstall_progress("s1")
            self.assertEqual(progress["status"], SESSION_CLOSED)
            self.assertFalse(progress["items"][0]["recovered"])


class AccessControlTest(ServiceCase):
    def test_host_sees_only_own_venue(self) -> None:
        r1 = self._register(b"a")
        self._session("s1")
        self.svc.place_artwork("s1", r1["artwork_id"], "u-ops")
        r2 = self.svc.register_artwork(
            "河岸", "art-2", "u-art", ["KZ"],
            date(2026, 10, 1), date(2026, 12, 31), raw=b"b",
        )
        self._session("s2", venue="v-yaogu", batch="b-central-asia", territory="KZ")
        self.svc.place_artwork("s2", r2["artwork_id"], "u-ops")

        dash = self.svc.host_dashboard("u-host")
        self.assertEqual(dash["venue_id"], "v-hongjiang")
        self.assertEqual([s["session_id"] for s in dash["sessions"]], ["s1"])
        self.assertEqual(dash["sessions"][0]["arrangements"][0]["title"], "梯田晨雾")
        dash2 = self.svc.host_dashboard("u-host2")
        self.assertEqual([s["session_id"] for s in dash2["sessions"]], ["s2"])
        with self.assertRaisesRegex(ServiceError, "角色不能执行"):
            self.svc.host_dashboard("u-ops")

    def test_search_results_do_not_enumerate_contacts(self) -> None:
        self._register(b"a")
        hits = self.svc.search_artworks("梯田", "u-host")
        self.assertEqual(len(hits), 1)
        flat = repr(hits)
        self.assertNotIn("13800000000", flat)
        self.assertNotIn("amai@example.test", flat)
        # 空查询也不能成为枚举联系方式的通道
        self.assertNotIn("13800000000", repr(self.svc.search_artworks("", "u-art")))

    def test_contact_access_is_role_gated_and_audited(self) -> None:
        with self.assertRaisesRegex(ServiceError, "无权查看联系方式"):
            self.svc.get_artist_contact("art-1", "phone", "u-host", "撤展联系")
        with self.assertRaisesRegex(ServiceError, "查阅联系方式必须登记理由"):
            self.svc.get_artist_contact("art-1", "phone", "u-ops", "  ")
        phone = self.svc.get_artist_contact("art-1", "phone", "u-ins", "理赔核对")
        self.assertEqual(phone, "13800000000")
        access = [e for e in self.svc.audit if e.action == "contact.accessed"]
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].detail["reason"], "理赔核对")


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_sha256(self) -> None:
        self.assertEqual(
            fingerprint_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


if __name__ == "__main__":
    unittest.main()
