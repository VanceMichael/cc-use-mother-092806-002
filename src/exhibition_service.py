"""作品与场地领域服务。

覆盖乡村摄影巡展的作品登记与指纹去重、授权版本与到期控制、
场地容量、场次快照、巡展批次冻结、布展交接与撤展回收、
损坏赔付、权限隔离与联系人保护，以及可解释的授权追溯。

所有状态均可序列化为 JSON，进程重启后到期提醒、撤展清点和
赔付核对从持久化的水位标记继续执行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

# 角色
ROLE_OPERATIONS = "OPERATIONS"
ROLE_ARTIST = "ARTIST"
ROLE_HOST = "VENUE_HOST"
ROLE_CARRIER = "CARRIER"
ROLE_REVIEWER = "COPYRIGHT_REVIEWER"
ROLE_INSURANCE = "INSURANCE_REVIEWER"

# 授权状态
GRANT_PENDING = "PENDING_REVIEW"
GRANT_ACTIVE = "ACTIVE"
GRANT_REJECTED = "REJECTED"
GRANT_REVOKED = "REVOKED"

# 例外申请状态
EXC_PENDING = "PENDING"
EXC_APPROVED = "APPROVED"
EXC_REJECTED = "REJECTED"

# 场地 / 批次 / 场次 / 交接 / 赔付状态
VENUE_OPEN = "OPEN"
VENUE_CLOSED = "CLOSED"
BATCH_ACTIVE = "ACTIVE"
BATCH_FROZEN = "FROZEN"
SESSION_PLANNED = "PLANNED"
SESSION_OPENED = "OPENED"
SESSION_CLOSED = "CLOSED"
HANDOVER_INSTALL = "INSTALL"
HANDOVER_RETURN = "RETURN"
CLAIM_REPORTED = "REPORTED"
CLAIM_PAID = "PAID"
CLAIM_REJECTED = "REJECTED"

# 冻结原因
FREEZE_VENUE_CLOSED = "VENUE_CLOSED"
FREEZE_ARTWORK_DAMAGED = "ARTWORK_DAMAGED"
FREEZE_TRANSPORT_DELAY = "TRANSPORT_DELAY"

REMINDER_WINDOW_DAYS = 30
CONTACT_ACCESS_ROLES = frozenset({ROLE_OPERATIONS, ROLE_INSURANCE})


def fingerprint_bytes(raw: bytes) -> str:
    """计算原始文件的 SHA-256 指纹。"""
    return hashlib.sha256(raw).hexdigest()


class ServiceError(ValueError):
    """业务规则冲突，code 供调用方程序化区分。"""

    def __init__(self, message: str, code: str = "service_error"):
        super().__init__(message)
        self.code = code


@dataclass
class User:
    id: str
    name: str
    role: str


@dataclass
class Artist:
    id: str
    name: str
    # 联系方式属于敏感信息，不进入任何检索/列表结果
    contacts: dict[str, str] = field(default_factory=dict)


@dataclass
class Artwork:
    id: str
    title: str
    artist_id: str
    fingerprint: str
    uploaded_by: str
    uploaded_at: str


@dataclass
class LicenseGrant:
    id: str
    artwork_id: str
    version: int
    territory: list[str]
    valid_from: str
    valid_until: str
    submitted_by: str
    status: str = GRANT_ACTIVE
    decided_by: str | None = None
    decided_at: str | None = None
    reminded_at: str | None = None


@dataclass
class ExceptionRequest:
    id: str
    artwork_id: str
    session_id: str
    reason: str
    submitted_by: str
    status: str = EXC_PENDING
    reviewed_by: str | None = None
    reviewed_at: str | None = None


@dataclass
class Venue:
    id: str
    name: str
    region: str
    host_user_id: str
    capacity: int
    status: str = VENUE_OPEN


@dataclass
class Translation:
    id: str
    entity_kind: str
    entity_id: str
    lang: str
    field_name: str
    text: str
    version: int
    updated_by: str
    updated_at: str


@dataclass
class FreezeEvent:
    at: str
    reason: str
    actor: str
    resumed: bool = False
    resumed_at: str | None = None


@dataclass
class Batch:
    id: str
    name: str
    territory: str
    status: str = BATCH_ACTIVE
    events: list[FreezeEvent] = field(default_factory=list)


@dataclass
class Handover:
    id: str
    session_id: str
    artwork_id: str
    kind: str
    from_user: str
    to_user: str
    actor: str
    at: str
    condition: str


@dataclass
class DamageRecord:
    id: str
    session_id: str
    artwork_id: str
    batch_id: str
    reported_by: str
    at: str
    description: str
    estimated_amount: float


@dataclass
class Claim:
    id: str
    damage_id: str
    artwork_id: str
    session_id: str
    amount: float
    status: str = CLAIM_REPORTED
    reviewed_by: str | None = None
    reviewed_at: str | None = None


@dataclass
class Placement:
    artwork_id: str
    # 场次排定时刻固化的授权/例外与翻译版本，闭展后不可变
    basis_kind: str
    basis_id: str
    basis_version: int
    territory: str
    valid_from: str
    valid_until: str
    submitted_by: str
    approved_by: str | None
    decided_at: str
    translations: dict[str, int]
    install_handover_id: str | None = None
    return_handover_id: str | None = None


@dataclass
class Session:
    id: str
    title: str
    venue_id: str
    batch_id: str
    territory: str
    start_date: str
    end_date: str
    status: str = SESSION_PLANNED
    opened_at: str | None = None
    closed_at: str | None = None
    placements: dict[str, Placement] = field(default_factory=dict)


@dataclass
class AuditEvent:
    id: str
    at: str
    actor: str
    action: str
    target: str
    detail: dict[str, Any] = field(default_factory=dict)


class ExhibitionService:
    """作品与场地服务的全部用例入口。"""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or datetime.now
        self.users: dict[str, User] = {}
        self.artists: dict[str, Artist] = {}
        self.artworks: dict[str, Artwork] = {}
        self._fingerprints: dict[str, str] = {}
        self.grants: dict[str, LicenseGrant] = {}
        self.exceptions: dict[str, ExceptionRequest] = {}
        self.venues: dict[str, Venue] = {}
        self.translations: list[Translation] = []
        self.batches: dict[str, Batch] = {}
        self.sessions: dict[str, Session] = {}
        self.handovers: dict[str, Handover] = {}
        self.damages: dict[str, DamageRecord] = {}
        self.claims: dict[str, Claim] = {}
        self.audit: list[AuditEvent] = []
        # 到期提醒/撤展清单/赔付通知的水位标记，重启后据此续跑
        self.job_markers: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self._clock()

    def _next_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n}"

    def _audit(self, actor: str, action: str, target: str, **detail: Any) -> None:
        self.audit.append(
            AuditEvent(self._next_id("evt"), self._now().isoformat(), actor, action, target, detail)
        )

    def _user(self, user_id: str) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise ServiceError("用户不存在", "unknown_user")
        return user

    def _require_role(self, user_id: str, role: str) -> User:
        user = self._user(user_id)
        if user.role != role:
            raise ServiceError(f"用户角色不能执行该操作：{role}", "forbidden_role")
        return user

    def add_user(self, user_id: str, name: str, role: str) -> User:
        if user_id in self.users:
            raise ServiceError("用户已存在", "duplicate_user")
        user = User(user_id, name, role)
        self.users[user_id] = user
        return user

    def add_artist(self, artist_id: str, name: str, contacts: dict[str, str] | None = None) -> Artist:
        artist = Artist(artist_id, name, dict(contacts or {}))
        self.artists[artist_id] = artist
        return artist

    # ----------------------------------------------------------- 作品与授权

    def register_artwork(
        self,
        title: str,
        artist_id: str,
        submitted_by: str,
        territory: list[str],
        valid_from: date,
        valid_until: date,
        fingerprint: str | None = None,
        raw: bytes | None = None,
    ) -> dict[str, str]:
        """登记作品与首版授权。

        - 同一作品重复上传（指纹与授权地域、期限完全一致）返回原结果；
        - 指纹相同但授权地域或期限不同，新授权版本进入复核队列。
        """
        self._user(submitted_by)
        if artist_id not in self.artists:
            raise ServiceError("作者不存在", "unknown_artist")
        if valid_until < valid_from:
            raise ServiceError("授权终止日期早于起始日期", "invalid_period")
        fp = fingerprint or (fingerprint_bytes(raw) if raw is not None else None)
        if not fp:
            raise ServiceError("缺少原始文件指纹", "missing_fingerprint")

        existing = self._fingerprints.get(fp)
        if existing is not None:
            for grant in self._grants_of(existing):
                if (
                    set(grant.territory) == set(territory)
                    and grant.valid_from == valid_from.isoformat()
                    and grant.valid_until == valid_until.isoformat()
                ):
                    self._audit(submitted_by, "artwork.reupload_dedup", existing, grant_id=grant.id)
                    return {"status": "duplicate", "artwork_id": existing, "grant_id": grant.id}
            version = self._next_grant_version(existing)
            grant = LicenseGrant(
                id=self._next_id("lic"),
                artwork_id=existing,
                version=version,
                territory=sorted(territory),
                valid_from=valid_from.isoformat(),
                valid_until=valid_until.isoformat(),
                submitted_by=submitted_by,
                status=GRANT_PENDING,
            )
            self.grants[grant.id] = grant
            self._audit(
                submitted_by, "license.review_required", grant.id,
                artwork_id=existing, reason="fingerprint_collision_license_differs",
            )
            return {"status": "review", "artwork_id": existing, "grant_id": grant.id}

        artwork_id = self._next_id("art")
        self.artworks[artwork_id] = Artwork(
            id=artwork_id, title=title, artist_id=artist_id, fingerprint=fp,
            uploaded_by=submitted_by, uploaded_at=self._now().isoformat(),
        )
        self._fingerprints[fp] = artwork_id
        grant = LicenseGrant(
            id=self._next_id("lic"), artwork_id=artwork_id, version=1,
            territory=sorted(territory), valid_from=valid_from.isoformat(),
            valid_until=valid_until.isoformat(), submitted_by=submitted_by,
        )
        self.grants[grant.id] = grant
        self._audit(submitted_by, "artwork.registered", artwork_id, grant_id=grant.id)
        return {"status": "registered", "artwork_id": artwork_id, "grant_id": grant.id}

    def _grants_of(self, artwork_id: str) -> list[LicenseGrant]:
        return sorted(
            (g for g in self.grants.values() if g.artwork_id == artwork_id),
            key=lambda g: g.version,
        )

    def _next_grant_version(self, artwork_id: str) -> int:
        versions = [g.version for g in self.grants.values() if g.artwork_id == artwork_id]
        return (max(versions) + 1) if versions else 1

    def review_grant(self, grant_id: str, reviewer_id: str, approve: bool) -> LicenseGrant:
        """复核指纹冲突授权；复核人不能是提交人本人。"""
        grant = self.grants.get(grant_id)
        if grant is None:
            raise ServiceError("授权不存在", "unknown_grant")
        reviewer = self._require_role(reviewer_id, ROLE_REVIEWER)
        if grant.status != GRANT_PENDING:
            raise ServiceError("授权不在待复核状态", "grant_not_reviewable")
        if grant.submitted_by == reviewer.id:
            raise ServiceError("提交授权的人不能复核自己的申请", "self_review_forbidden")
        grant.status = GRANT_ACTIVE if approve else GRANT_REJECTED
        grant.decided_by = reviewer.id
        grant.decided_at = self._now().isoformat()
        self._audit(reviewer.id, "license.reviewed", grant.id, approve=approve)
        return grant

    def revoke_grant(self, grant_id: str, actor_id: str) -> LicenseGrant:
        self._require_role(actor_id, ROLE_OPERATIONS)
        grant = self.grants.get(grant_id)
        if grant is None or grant.status not in (GRANT_ACTIVE, GRANT_PENDING):
            raise ServiceError("授权不可撤销", "grant_not_revocable")
        grant.status = GRANT_REVOKED
        self._audit(actor_id, "license.revoked", grant.id)
        return grant

    def submit_exception(
        self, artwork_id: str, session_id: str, submitted_by: str, reason: str
    ) -> ExceptionRequest:
        self._user(submitted_by)
        if artwork_id not in self.artworks:
            raise ServiceError("作品不存在", "unknown_artwork")
        if session_id not in self.sessions:
            raise ServiceError("场次不存在", "unknown_session")
        if not reason.strip():
            raise ServiceError("例外申请必须说明理由", "empty_reason")
        exc = ExceptionRequest(
            id=self._next_id("exc"), artwork_id=artwork_id, session_id=session_id,
            reason=reason, submitted_by=submitted_by,
        )
        self.exceptions[exc.id] = exc
        self._audit(submitted_by, "exception.submitted", exc.id)
        return exc

    def review_exception(self, exception_id: str, reviewer_id: str, approve: bool) -> ExceptionRequest:
        """批准授权例外；提交人不能批准自己的例外。"""
        exc = self.exceptions.get(exception_id)
        if exc is None:
            raise ServiceError("例外申请不存在", "unknown_exception")
        reviewer = self._require_role(reviewer_id, ROLE_REVIEWER)
        if exc.status != EXC_PENDING:
            raise ServiceError("例外申请已处理", "exception_not_reviewable")
        if exc.submitted_by == reviewer.id:
            raise ServiceError("提交授权的人不能批准自己的例外", "self_review_forbidden")
        exc.status = EXC_APPROVED if approve else EXC_REJECTED
        exc.reviewed_by = reviewer.id
        exc.reviewed_at = self._now().isoformat()
        self._audit(reviewer.id, "exception.reviewed", exc.id, approve=approve)
        return exc

    # ----------------------------------------------------------- 场地与翻译

    def add_venue(
        self, venue_id: str, name: str, region: str, host_user_id: str, capacity: int
    ) -> Venue:
        host = self._user(host_user_id)
        if host.role != ROLE_HOST:
            raise ServiceError("场地负责人角色不正确", "host_role_required")
        if capacity < 1:
            raise ServiceError("场地容量必须为正数", "invalid_capacity")
        venue = Venue(venue_id, name, region, host_user_id, capacity)
        self.venues[venue_id] = venue
        return venue

    def add_translation(
        self, entity_kind: str, entity_id: str, lang: str, field_name: str,
        text: str, actor_id: str,
    ) -> Translation:
        """登记或修订译文；同一键每次修订产生不可变新版本。"""
        self._user(actor_id)
        version = 1 + max(
            (t.version for t in self.translations
             if (t.entity_kind, t.entity_id, t.lang, t.field_name)
             == (entity_kind, entity_id, lang, field_name)),
            default=0,
        )
        item = Translation(
            id=self._next_id("tr"), entity_kind=entity_kind, entity_id=entity_id,
            lang=lang, field_name=field_name, text=text, version=version,
            updated_by=actor_id, updated_at=self._now().isoformat(),
        )
        self.translations.append(item)
        return item

    def _current_translations(self, entity_kind: str, entity_id: str) -> dict[str, int]:
        latest: dict[str, int] = {}
        for t in self.translations:
            if t.entity_kind == entity_kind and t.entity_id == entity_id:
                latest[f"{t.lang}:{t.field_name}"] = t.version
        return latest

    # ----------------------------------------------------------- 批次与场次

    def create_batch(self, batch_id: str, name: str, territory: str) -> Batch:
        if batch_id in self.batches:
            raise ServiceError("批次已存在", "duplicate_batch")
        batch = Batch(batch_id, name, territory)
        self.batches[batch_id] = batch
        return batch

    def create_session(
        self, session_id: str, title: str, venue_id: str, batch_id: str,
        territory: str, start_date: date, end_date: date,
    ) -> Session:
        venue = self.venues.get(venue_id)
        if venue is None:
            raise ServiceError("场地不存在", "unknown_venue")
        if venue.status != VENUE_OPEN:
            raise ServiceError("场地已关闭，不能安排新场次", "venue_closed")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ServiceError("巡展批次不存在", "unknown_batch")
        if end_date < start_date:
            raise ServiceError("场次结束日期早于开始日期", "invalid_session_period")
        session = Session(
            id=session_id, title=title, venue_id=venue_id, batch_id=batch_id,
            territory=territory, start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        self.sessions[session_id] = session
        return session

    def _require_batch_active(self, session: Session) -> None:
        batch = self.batches[session.batch_id]
        if batch.status == BATCH_FROZEN:
            reason = batch.events[-1].reason if batch.events else "UNKNOWN"
            raise ServiceError(f"批次已冻结（{reason}），该场次暂停推进", "batch_frozen")

    def _usable_grant(
        self, artwork_id: str, territory: str, start: date, end: date
    ) -> LicenseGrant | None:
        for grant in self._grants_of(artwork_id):
            if grant.status != GRANT_ACTIVE:
                continue
            if territory not in grant.territory and "ALL" not in grant.territory:
                continue
            if date.fromisoformat(grant.valid_from) <= start and date.fromisoformat(grant.valid_until) >= end:
                return grant
        return None

    def place_artwork(
        self, session_id: str, artwork_id: str, actor_id: str, exception_id: str | None = None
    ) -> Placement:
        """把作品排入场次；排定瞬间固化授权依据与译文版本。"""
        session = self.sessions.get(session_id)
        if session is None:
            raise ServiceError("场次不存在", "unknown_session")
        self._user(actor_id)
        venue = self.venues[session.venue_id]
        if venue.status != VENUE_OPEN:
            raise ServiceError("场地已关闭，不能新增展品", "venue_closed")
        self._require_batch_active(session)
        if session.status != SESSION_PLANNED:
            raise ServiceError("场次已锁定，不能调整展品", "session_locked")
        if artwork_id not in self.artworks:
            raise ServiceError("作品不存在", "unknown_artwork")
        if artwork_id in session.placements:
            raise ServiceError("作品已在该场次中", "already_placed")
        if len(session.placements) >= venue.capacity:
            raise ServiceError("超出展点容量", "venue_capacity_exceeded")

        start = date.fromisoformat(session.start_date)
        end = date.fromisoformat(session.end_date)
        basis_kind = "GRANT"
        basis: LicenseGrant | ExceptionRequest
        grant = self._usable_grant(artwork_id, session.territory, start, end)

        if exception_id is not None:
            exc = self.exceptions.get(exception_id)
            if exc is None or exc.artwork_id != artwork_id or exc.session_id != session_id:
                raise ServiceError("例外申请与作品或场次不匹配", "exception_mismatch")
            if exc.status != EXC_APPROVED:
                raise ServiceError("例外申请未获批准", "exception_not_approved")
            basis_kind = "EXCEPTION"
            basis = exc
        elif grant is not None:
            basis = grant
        else:
            # 给出最贴近的拒绝原因，便于运营核对
            pending = [
                g for g in self._grants_of(artwork_id)
                if g.status == GRANT_PENDING and session.territory in g.territory
            ]
            if pending:
                raise ServiceError("授权尚在复核，不能排入新场次", "license_pending_review")
            covering_territory = [
                g for g in self._grants_of(artwork_id)
                if g.status == GRANT_ACTIVE
                and (session.territory in g.territory or "ALL" in g.territory)
            ]
            if covering_territory and date.fromisoformat(
                max(g.valid_until for g in covering_territory)
            ) < end:
                raise ServiceError("授权在展期结束前到期，不能排入新场次", "license_expiring")
            raise ServiceError("授权地域不覆盖该场次", "license_territory_mismatch")

        if basis_kind == "EXCEPTION":
            placement = Placement(
                artwork_id=artwork_id, basis_kind="EXCEPTION", basis_id=basis.id,
                basis_version=0, territory=session.territory,
                valid_from=session.start_date, valid_until=session.end_date,
                submitted_by=basis.submitted_by, approved_by=basis.reviewed_by,
                decided_at=basis.reviewed_at or self._now().isoformat(),
                translations=self._current_translations("ARTWORK", artwork_id),
            )
        else:
            placement = Placement(
                artwork_id=artwork_id, basis_kind="GRANT", basis_id=basis.id,
                basis_version=basis.version, territory=session.territory,
                valid_from=basis.valid_from, valid_until=basis.valid_until,
                submitted_by=basis.submitted_by, approved_by=basis.decided_by,
                decided_at=basis.decided_at or self.artworks[artwork_id].uploaded_at,
                translations=self._current_translations("ARTWORK", artwork_id),
            )
        session.placements[artwork_id] = placement
        self._audit(
            actor_id, "artwork.placed", f"{session_id}:{artwork_id}",
            basis=basis_kind, basis_id=placement.basis_id,
        )
        return placement

    def record_install_handover(
        self, session_id: str, artwork_id: str, from_user: str, to_user: str,
        actor_id: str, condition: str = "完好",
    ) -> Handover:
        """布展交接：承运人/运营方把作品交给场地负责人。"""
        session = self._session_with_placement(session_id, artwork_id)
        self._require_batch_active(session)
        self._user(from_user)
        self._user(to_user)
        placement = session.placements[artwork_id]
        if placement.install_handover_id is not None:
            raise ServiceError("布展交接已记录", "handover_exists")
        handover = Handover(
            id=self._next_id("ho"), session_id=session_id, artwork_id=artwork_id,
            kind=HANDOVER_INSTALL, from_user=from_user, to_user=to_user,
            actor=actor_id, at=self._now().isoformat(), condition=condition,
        )
        self.handovers[handover.id] = handover
        placement.install_handover_id = handover.id
        self._audit(actor_id, "handover.install", handover.id)
        return handover

    def open_session(self, session_id: str, actor_id: str) -> Session:
        self._require_role(actor_id, ROLE_OPERATIONS)
        session = self.sessions.get(session_id)
        if session is None:
            raise ServiceError("场次不存在", "unknown_session")
        self._require_batch_active(session)
        if session.status != SESSION_PLANNED:
            raise ServiceError("场次不在待开展状态", "session_not_planned")
        if not session.placements:
            raise ServiceError("场次还没有排入作品", "session_empty")
        missing = [
            art_id for art_id, p in session.placements.items()
            if p.install_handover_id is None
        ]
        if missing:
            raise ServiceError("存在未完成布展交接的作品", "install_handover_missing")
        session.status = SESSION_OPENED
        session.opened_at = self._now().isoformat()
        self._audit(actor_id, "session.opened", session_id)
        return session

    def close_session(self, session_id: str, actor_id: str) -> Session:
        """闭展：场次快照自此不可变，供日后解释与赔付追溯。"""
        self._require_role(actor_id, ROLE_OPERATIONS)
        session = self.sessions.get(session_id)
        if session is None:
            raise ServiceError("场次不存在", "unknown_session")
        self._require_batch_active(session)
        if session.status != SESSION_OPENED:
            raise ServiceError("场次不在展出状态", "session_not_opened")
        session.status = SESSION_CLOSED
        session.closed_at = self._now().isoformat()
        self._audit(actor_id, "session.closed", session_id)
        return session

    def record_return_handover(
        self, session_id: str, artwork_id: str, from_user: str, to_user: str,
        actor_id: str, condition: str = "完好",
    ) -> Handover:
        """撤展回收：闭展后作品由场地交回运营方/承运人。"""
        session = self._session_with_placement(session_id, artwork_id)
        self._require_batch_active(session)
        if session.status != SESSION_CLOSED:
            raise ServiceError("只有闭展场次才能记录撤展回收", "session_not_closed")
        self._user(from_user)
        self._user(to_user)
        placement = session.placements[artwork_id]
        if placement.return_handover_id is not None:
            raise ServiceError("撤展回收已记录", "handover_exists")
        handover = Handover(
            id=self._next_id("ho"), session_id=session_id, artwork_id=artwork_id,
            kind=HANDOVER_RETURN, from_user=from_user, to_user=to_user,
            actor=actor_id, at=self._now().isoformat(), condition=condition,
        )
        self.handovers[handover.id] = handover
        placement.return_handover_id = handover.id
        self._audit(actor_id, "handover.return", handover.id, condition=condition)
        return handover

    def _session_with_placement(self, session_id: str, artwork_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ServiceError("场次不存在", "unknown_session")
        if artwork_id not in session.placements:
            raise ServiceError("作品不在该场次中", "not_placed")
        return session

    # ------------------------------------------------------ 冻结、损坏与赔付

    def freeze_batch(self, batch_id: str, actor_id: str, reason: str) -> Batch:
        self._user(actor_id)
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ServiceError("批次不存在", "unknown_batch")
        if batch.status == BATCH_FROZEN:
            raise ServiceError("批次已处于冻结状态", "batch_already_frozen")
        batch.status = BATCH_FROZEN
        batch.events.append(FreezeEvent(at=self._now().isoformat(), reason=reason, actor=actor_id))
        self._audit(actor_id, "batch.frozen", batch_id, reason=reason)
        return batch

    def resume_batch(self, batch_id: str, actor_id: str) -> Batch:
        self._user(actor_id)
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ServiceError("批次不存在", "unknown_batch")
        if batch.status != BATCH_FROZEN:
            raise ServiceError("批次未冻结", "batch_not_frozen")
        batch.status = BATCH_ACTIVE
        event = batch.events[-1]
        event.resumed = True
        event.resumed_at = self._now().isoformat()
        self._audit(actor_id, "batch.resumed", batch_id, reason=event.reason)
        return batch

    def close_venue(self, venue_id: str, actor_id: str) -> dict[str, Any]:
        """关闭场地：只冻结在该场地尚有未完成场次的批次。"""
        self._require_role(actor_id, ROLE_OPERATIONS)
        venue = self.venues.get(venue_id)
        if venue is None:
            raise ServiceError("场地不存在", "unknown_venue")
        venue.status = VENUE_CLOSED
        affected: list[str] = []
        for session in self.sessions.values():
            if session.venue_id != venue_id or session.status == SESSION_CLOSED:
                continue
            batch = self.batches[session.batch_id]
            if batch.status == BATCH_ACTIVE and batch.id not in affected:
                self.freeze_batch(batch.id, actor_id, FREEZE_VENUE_CLOSED)
                affected.append(batch.id)
        self._audit(actor_id, "venue.closed", venue_id, frozen_batches=affected)
        return {"venue_id": venue_id, "frozen_batches": affected}

    def report_damage(
        self, session_id: str, artwork_id: str, actor_id: str,
        description: str, estimated_amount: float,
    ) -> DamageRecord:
        """作品受损：登记损坏与待核对赔付，并只冻结所在批次。"""
        session = self._session_with_placement(session_id, artwork_id)
        if estimated_amount < 0:
            raise ServiceError("估损金额不能为负", "invalid_amount")
        damage = DamageRecord(
            id=self._next_id("dmg"), session_id=session_id, artwork_id=artwork_id,
            batch_id=session.batch_id, reported_by=actor_id, at=self._now().isoformat(),
            description=description, estimated_amount=estimated_amount,
        )
        self.damages[damage.id] = damage
        claim = Claim(
            id=self._next_id("clm"), damage_id=damage.id, artwork_id=artwork_id,
            session_id=session_id, amount=estimated_amount,
        )
        self.claims[claim.id] = claim
        batch = self.batches[session.batch_id]
        if batch.status == BATCH_ACTIVE:
            self.freeze_batch(batch.id, actor_id, FREEZE_ARTWORK_DAMAGED)
        self._audit(actor_id, "damage.reported", damage.id, claim_id=claim.id)
        return damage

    def reconcile_claim(
        self, claim_id: str, reviewer_id: str, approved: bool, amount: float | None = None
    ) -> Claim:
        """保险责任核对：确认赔付或拒赔。"""
        self._require_role(reviewer_id, ROLE_INSURANCE)
        claim = self.claims.get(claim_id)
        if claim is None:
            raise ServiceError("赔付单不存在", "unknown_claim")
        if claim.status != CLAIM_REPORTED:
            raise ServiceError("赔付单已核对", "claim_already_reconciled")
        claim.status = CLAIM_PAID if approved else CLAIM_REJECTED
        if approved:
            claim.amount = claim.amount if amount is None else amount
        claim.reviewed_by = reviewer_id
        claim.reviewed_at = self._now().isoformat()
        self._audit(reviewer_id, "claim.reconciled", claim.id, approved=approved)
        return claim

    def pending_claims(self) -> list[Claim]:
        return [c for c in self.claims.values() if c.status == CLAIM_REPORTED]

    def deinstall_progress(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ServiceError("场次不存在", "unknown_session")
        items = []
        for art_id, p in session.placements.items():
            items.append({
                "artwork_id": art_id,
                "installed": p.install_handover_id is not None,
                "recovered": p.return_handover_id is not None,
            })
        return {
            "session_id": session_id,
            "status": session.status,
            "total": len(items),
            "recovered": sum(1 for i in items if i["recovered"]),
            "items": items,
        }

    # ------------------------------------------------------------- 到期与续跑

    def run_due_jobs(self, at: datetime | None = None) -> list[dict[str, Any]]:
        """处理到期事项。

        - 30 天内到期的生效授权生成一次到期提醒；
        - 刚闭展的场次生成撤展清点清单；
        - 新报案的赔付生成核对通知。
        水位标记持久化，重启后不重复生成、也不漏掉新增事项。
        冻结批次不影响跨批次的提醒与清单生成。
        """
        moment = at or self._now()
        today = moment.date()
        items: list[dict[str, Any]] = []

        for grant in sorted(self.grants.values(), key=lambda g: g.id):
            if grant.status != GRANT_ACTIVE or grant.reminded_at is not None:
                continue
            days_left = (date.fromisoformat(grant.valid_until) - today).days
            if 0 <= days_left <= REMINDER_WINDOW_DAYS:
                grant.reminded_at = moment.isoformat()
                key = f"reminder:{grant.id}"
                self.job_markers[key] = moment.isoformat()
                items.append({
                    "kind": "EXPIRY_REMINDER", "grant_id": grant.id,
                    "artwork_id": grant.artwork_id, "version": grant.version,
                    "valid_until": grant.valid_until, "days_left": days_left,
                })
                self._audit("system", "job.expiry_reminder", grant.id, days_left=days_left)

        for session in sorted(self.sessions.values(), key=lambda s: s.id):
            key = f"deinstall:{session.id}"
            if session.status == SESSION_CLOSED and key not in self.job_markers:
                self.job_markers[key] = moment.isoformat()
                items.append({
                    "kind": "DEINSTALL_CHECKLIST", "session_id": session.id,
                    "artwork_ids": sorted(session.placements),
                })
                self._audit("system", "job.deinstall_checklist", session.id)

        for claim in sorted(self.claims.values(), key=lambda c: c.id):
            key = f"claim-due:{claim.id}"
            if claim.status == CLAIM_REPORTED and key not in self.job_markers:
                self.job_markers[key] = moment.isoformat()
                items.append({
                    "kind": "CLAIM_RECONCILIATION", "claim_id": claim.id,
                    "artwork_id": claim.artwork_id, "session_id": claim.session_id,
                    "amount": claim.amount,
                })
                self._audit("system", "job.claim_due", claim.id)

        return items

    # ----------------------------------------------------- 检索隔离与联系人

    def search_artworks(self, query: str, actor_id: str) -> list[dict[str, Any]]:
        """检索作品；结果不包含任何作者联系方式，防止枚举。"""
        self._user(actor_id)
        hits = []
        for art in self.artworks.values():
            if query and query not in art.title:
                continue
            artist = self.artists[art.artist_id]
            hits.append({
                "artwork_id": art.id,
                "title": art.title,
                "artist_id": artist.id,
                "artist_name": artist.name,
                "grants": [
                    {
                        "grant_id": g.id, "version": g.version,
                        "territory": list(g.territory),
                        "valid_from": g.valid_from, "valid_until": g.valid_until,
                        "status": g.status,
                    }
                    for g in self._grants_of(art.id)
                ],
            })
        return hits

    def get_artist_contact(
        self, artist_id: str, channel: str, actor_id: str, reason: str
    ) -> str:
        """按业务理由读取联系方式；仅授权角色可调，每次访问留审计。"""
        user = self._user(actor_id)
        if user.role not in CONTACT_ACCESS_ROLES:
            raise ServiceError("无权查看联系方式", "contact_forbidden")
        if not reason.strip():
            raise ServiceError("查阅联系方式必须登记理由", "contact_reason_required")
        artist = self.artists.get(artist_id)
        if artist is None:
            raise ServiceError("作者不存在", "unknown_artist")
        value = artist.contacts.get(channel)
        if value is None:
            raise ServiceError("联系方式不存在", "contact_channel_missing")
        self._audit(
            actor_id, "contact.accessed", artist_id, channel=channel, reason=reason
        )
        return value

    def host_dashboard(self, user_id: str) -> dict[str, Any]:
        """村民只能看到本人负责场地的安排。"""
        user = self._require_role(user_id, ROLE_HOST)
        venue = next((v for v in self.venues.values() if v.host_user_id == user.id), None)
        if venue is None:
            raise ServiceError("该用户没有负责的场地", "no_venue")
        sessions_view = []
        for session in self.sessions.values():
            if session.venue_id != venue.id:
                continue
            sessions_view.append({
                "session_id": session.id,
                "title": session.title,
                "status": session.status,
                "start_date": session.start_date,
                "end_date": session.end_date,
                "batch_status": self.batches[session.batch_id].status,
                "arrangements": [
                    {
                        "artwork_id": art_id,
                        "title": self.artworks[art_id].title,
                        "installed": p.install_handover_id is not None,
                        "recovered": p.return_handover_id is not None,
                    }
                    for art_id, p in sorted(session.placements.items())
                ],
            })
        return {
            "venue_id": venue.id,
            "venue_name": venue.name,
            "capacity": venue.capacity,
            "status": venue.status,
            "sessions": sessions_view,
        }

    # --------------------------------------------------------------- 追溯解释

    def explain_placement(self, session_id: str, artwork_id: str) -> dict[str, Any]:
        """解释作品在某场次为何获准、由谁交接、何时回收及赔付情况。"""
        session = self._session_with_placement(session_id, artwork_id)
        placement = session.placements[artwork_id]
        venue = self.venues[session.venue_id]
        artwork = self.artworks[artwork_id]
        artist = self.artists[artwork.artist_id]

        if placement.basis_kind == "GRANT":
            grant = self.grants[placement.basis_id]
            basis_desc = (
                f"依据授权 {grant.id}（第{grant.version}版，地域 {','.join(grant.territory)}，"
                f"有效期 {grant.valid_from} 至 {grant.valid_until}）"
            )
            approver = grant.decided_by
            if approver:
                approver_name = self.users[approver].name
                basis_desc += f"，由 {self.users[grant.submitted_by].name} 提交、{approver_name} 复核批准"
            else:
                basis_desc += f"，由 {self.users[grant.submitted_by].name} 登记提交即生效"
        else:
            exc = self.exceptions[placement.basis_id]
            approver_name = self.users[exc.reviewed_by].name if exc.reviewed_by else "—"
            basis_desc = (
                f"依据授权例外 {exc.id}（理由：{exc.reason}），由 "
                f"{self.users[exc.submitted_by].name} 申请、{approver_name} 批准"
            )

        why = (
            f"作品《{artwork.title}》获准出现在场次「{session.title}」（{venue.name}，"
            f"{session.start_date} 至 {session.end_date}）：{basis_desc}；"
            f"场次地域 {session.territory} 与展期均在获准范围内，排定时已固化版本快照。"
        )

        handovers = []
        for handover_id in (placement.install_handover_id, placement.return_handover_id):
            if handover_id is None:
                continue
            h = self.handovers[handover_id]
            handovers.append({
                "handover_id": h.id,
                "kind": h.kind,
                "from": self.users[h.from_user].name,
                "to": self.users[h.to_user].name,
                "at": h.at,
                "condition": h.condition,
                "recorded_by": h.actor,
            })

        claim_view = None
        for claim in self.claims.values():
            if claim.artwork_id == artwork_id and claim.session_id == session_id:
                claim_view = {
                    "claim_id": claim.id, "status": claim.status, "amount": claim.amount,
                    "reviewed_by": claim.reviewed_by, "reviewed_at": claim.reviewed_at,
                }

        return {
            "artwork_id": artwork_id,
            "artwork_title": artwork.title,
            "artist_name": artist.name,
            "session_id": session_id,
            "session_title": session.title,
            "session_status": session.status,
            "venue": {"id": venue.id, "name": venue.name, "region": venue.region},
            "snapshot": {
                "basis_kind": placement.basis_kind,
                "basis_id": placement.basis_id,
                "basis_version": placement.basis_version,
                "territory": placement.territory,
                "valid_from": placement.valid_from,
                "valid_until": placement.valid_until,
                "submitted_by": placement.submitted_by,
                "approved_by": placement.approved_by,
                "decided_at": placement.decided_at,
                "translations": dict(placement.translations),
            },
            "handovers": handovers,
            "recovered_at": next(
                (h["at"] for h in handovers if h["kind"] == HANDOVER_RETURN), None
            ),
            "claim": claim_view,
            "why": why,
        }

    # -------------------------------------------------------------- 持久化

    def to_dict(self) -> dict[str, Any]:
        def dc(obj: Any) -> Any:
            if hasattr(obj, "__dataclass_fields__"):
                return {k: dc(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, dict):
                return {k: dc(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [dc(v) for v in obj]
            return obj

        return {
            "users": dc(list(self.users.values())),
            "artists": dc(list(self.artists.values())),
            "artworks": dc(list(self.artworks.values())),
            "fingerprints": dict(self._fingerprints),
            "grants": dc(list(self.grants.values())),
            "exceptions": dc(list(self.exceptions.values())),
            "venues": dc(list(self.venues.values())),
            "translations": dc(self.translations),
            "batches": dc(list(self.batches.values())),
            "sessions": dc(list(self.sessions.values())),
            "handovers": dc(list(self.handovers.values())),
            "damages": dc(list(self.damages.values())),
            "claims": dc(list(self.claims.values())),
            "audit": dc(self.audit),
            "job_markers": dict(self.job_markers),
            "counters": dict(self._counters),
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], clock: Callable[[], datetime] | None = None) -> "ExhibitionService":
        svc = cls(clock=clock)
        for row in data["users"]:
            svc.users[row["id"]] = User(**row)
        for row in data["artists"]:
            svc.artists[row["id"]] = Artist(**row)
        for row in data["artworks"]:
            svc.artworks[row["id"]] = Artwork(**row)
        svc._fingerprints = dict(data["fingerprints"])
        for row in data["grants"]:
            svc.grants[row["id"]] = LicenseGrant(**row)
        for row in data["exceptions"]:
            svc.exceptions[row["id"]] = ExceptionRequest(**row)
        for row in data["venues"]:
            svc.venues[row["id"]] = Venue(**row)
        for row in data["translations"]:
            svc.translations.append(Translation(**row))
        for row in data["batches"]:
            events = [FreezeEvent(**e) for e in row.pop("events")]
            batch = Batch(**row)
            batch.events = events
            svc.batches[batch.id] = batch
        for row in data["sessions"]:
            placements = {
                art_id: Placement(**p) for art_id, p in row.pop("placements").items()
            }
            session = Session(**row)
            session.placements = placements
            svc.sessions[session.id] = session
        for row in data["handovers"]:
            svc.handovers[row["id"]] = Handover(**row)
        for row in data["damages"]:
            svc.damages[row["id"]] = DamageRecord(**row)
        for row in data["claims"]:
            svc.claims[row["id"]] = Claim(**row)
        for row in data["audit"]:
            svc.audit.append(AuditEvent(**row))
        svc.job_markers = dict(data["job_markers"])
        svc._counters = dict(data["counters"])
        return svc

    @classmethod
    def load(cls, path: str | Path, clock: Callable[[], datetime] | None = None) -> "ExhibitionService":
        return cls.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8")), clock=clock
        )
