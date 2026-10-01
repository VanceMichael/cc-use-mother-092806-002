"""作品与场地领域服务。

覆盖摄影作品与作者登记、授权版本（地域/期限）、原始文件指纹去重与复核、
展点容量与场次排期、布展交接链、翻译文本、巡展批次冻结、保险赔付、授权
例外审批、到期提醒、撤展清点、村民场地视角与联系方式保护。

所有状态通过 :mod:`src.store` 持久化；服务对象可以在任意时刻用同一份状态
文件重建，到期提醒、撤展清点和赔付核对在重启后继续接续。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Callable

from src.store import JsonStore


class DomainError(ValueError):
    """业务规则被违反时抛出，消息面向运营人员。"""


# 角色
OPERATOR = "operator"          # 摄影大展运营方
AUTHOR = "author"              # 摄影师与机构
VENUE_MANAGER = "venue_manager"  # 村落场地负责人
CARRIER = "carrier"            # 巡展承运人
REVIEWER = "reviewer"          # 保险与版权审核人员

# 授权/批次/场次状态
GRANT_ACTIVE = "active"
GRANT_PENDING = "pending_review"
GRANT_REJECTED = "rejected"
GRANT_SUPERSEDED = "superseded"

BATCH_ACTIVE = "active"
BATCH_FROZEN = "frozen"

VENUE_OPEN = "open"
VENUE_CLOSED = "closed"

SHOW_SCHEDULED = "scheduled"
SHOW_COMPLETED = "completed"

EXCEPTION_PENDING = "pending"
EXCEPTION_APPROVED = "approved"
EXCEPTION_REJECTED = "rejected"

CLAIM_OPEN = "open"
CLAIM_VERIFIED = "verified"
CLAIM_PAID = "paid"

# 冻结原因
FREEZE_VENUE_CLOSED = "venue_closed"
FREEZE_DAMAGE = "artwork_damaged"
FREEZE_CUSTOMS = "cross_border_delay"

# 交接动作
HANDOVER_INSTALL = "installed"
HANDOVER_RETURN = "returned"
HANDOVER_DAMAGED = "damaged"

_REVIEW_ROLES = frozenset({REVIEWER})
_CONTACT_ROLES = frozenset({OPERATOR, REVIEWER})


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def grant_terms_signature(
    territories: list[str], valid_from: str, valid_until: str, scope: str
) -> str:
    """授权关键条款的规范化签名，与列表和字段顺序无关。"""
    payload = {
        "territories": sorted(territories),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "scope": scope,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ExhibitionService:
    """作品与场地服务的命令与查询入口。"""

    def __init__(self, store: JsonStore, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock().isoformat()

    def _today(self) -> date:
        return self.clock().date()

    def _next_id(self, kind: str, prefix: str) -> str:
        counters = self.store.collection("counters")
        counters[kind] = counters.get(kind, 0) + 1
        return f"{prefix}{counters[kind]:04d}"

    def _flush(self) -> None:
        self.store.flush()

    @staticmethod
    def _actor(actor: dict[str, str]) -> dict[str, str]:
        if not isinstance(actor, dict) or not actor.get("id") or not actor.get("role"):
            raise DomainError("缺少操作人身份")
        return {"id": actor["id"], "role": actor["role"]}

    def _require_role(self, actor: dict[str, str], roles: frozenset[str]) -> dict[str, str]:
        actor = self._actor(actor)
        if actor["role"] not in roles:
            raise DomainError("当前角色无权执行该操作")
        return actor

    def _must_get(self, section: str, key: str, label: str) -> dict[str, Any]:
        value = self.store.get(section, key)
        if value is None:
            raise DomainError(f"{label}不存在：{key}")
        return value

    def _audit(self, action: str, actor: dict[str, str], detail: dict[str, Any]) -> None:
        self.store.state.setdefault("audit", []).append(
            {"at": self._now(), "action": action, "actor": actor["id"], **detail}
        )

    # ------------------------------------------------------------------
    # 作者与联系方式保护
    # ------------------------------------------------------------------

    def register_artist(
        self, name: str, contact: str | None = None, role: str = AUTHOR
    ) -> dict[str, Any]:
        """登记摄影师或机构。联系方式存入受保护区，不进入任何检索结果。"""
        if not name or not name.strip():
            raise DomainError("作者名称不能为空")
        artist_id = self._next_id("artist", "A")
        artist = {"id": artist_id, "name": name.strip(), "role": role}
        self.store.put("artists", artist_id, artist)
        if contact:
            self.store.state.setdefault("contacts", {})[artist_id] = contact
        self._flush()
        return dict(artist)

    def reveal_contact(self, actor: dict[str, str], artist_id: str) -> str:
        """查看敏感联系方式仅限运营方与审核人员，且每次访问留痕。"""
        actor = self._require_role(actor, _CONTACT_ROLES)
        self._must_get("artists", artist_id, "作者")
        contact = self.store.state.get("contacts", {}).get(artist_id)
        if contact is None:
            raise DomainError("该作者未登记联系方式")
        self._audit("reveal_contact", actor, {"artist_id": artist_id})
        self._flush()
        return contact

    def search_artists(self, keyword: str) -> list[dict[str, Any]]:
        """按名称检索作者；结果一律不含联系方式，无法借检索枚举联系人。"""
        keyword = (keyword or "").strip().lower()
        results = []
        for artist in self.store.collection("artists").values():
            if not keyword or keyword in artist["name"].lower():
                results.append({"id": artist["id"], "name": artist["name"], "role": artist["role"]})
        return results

    def search_catalog(self, keyword: str) -> list[dict[str, Any]]:
        """检索作品目录，只返回标题与作者名称等公开字段。"""
        keyword = (keyword or "").strip().lower()
        artists = self.store.collection("artists")
        results = []
        for work in self.store.collection("artworks").values():
            author = artists.get(work["author_id"], {})
            if not keyword or keyword in work["title"].lower() or keyword in author.get("name", "").lower():
                results.append(
                    {
                        "id": work["id"],
                        "title": work["title"],
                        "author_id": work["author_id"],
                        "author_name": author.get("name"),
                    }
                )
        return results

    # ------------------------------------------------------------------
    # 作品登记、指纹去重与授权版本
    # ------------------------------------------------------------------

    def submit_artwork(
        self,
        *,
        title: str,
        author_id: str,
        fingerprint: str,
        territories: list[str],
        valid_from: str,
        valid_until: str,
        submitted_by: dict[str, str],
        scope: str = "exhibition",
    ) -> dict[str, Any]:
        """提交作品与首版/重复授权。

        - 同一作品（指纹相同）且授权条款完全相同：返回原结果，不产生新版本；
        - 指纹相同但授权条款不同：生成新版本并进入复核，复核期间不可使用；
        - 指纹不同：建立新作品与授权版本。
        """
        submitter = self._actor(submitted_by)
        if not title or not title.strip():
            raise DomainError("作品标题不能为空")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise DomainError("原始文件指纹应为64位十六进制摘要")
        fingerprint = fingerprint.lower()
        self._must_get("artists", author_id, "作者")
        if _parse_day(valid_until) < _parse_day(valid_from):
            raise DomainError("授权到期日不能早于起始日")
        if not territories:
            raise DomainError("授权地域不能为空")

        terms_sig = grant_terms_signature(territories, valid_from, valid_until, scope)
        fp_index = self.store.collection("fingerprint_index")
        grant_index = self.store.collection("grant_index")
        index_key = f"{fingerprint}|{terms_sig}"

        existing = fp_index.get(fingerprint)
        if existing is None:
            artwork_id = self._next_id("artwork", "W")
            artwork = {
                "id": artwork_id,
                "title": title.strip(),
                "author_id": author_id,
                "fingerprint": fingerprint,
                "grant_ids": [],
                "current_grant_id": None,
                "created_at": self._now(),
                "created_by": submitter["id"],
            }
            self.store.put("artworks", artwork_id, artwork)
            fp_index[fingerprint] = {"artwork_id": artwork_id}
            grant = self._create_grant(
                artwork, author_id, territories, valid_from, valid_until, scope,
                terms_sig, submitter, version=1, status=GRANT_ACTIVE,
                review_note=None,
            )
            artwork["current_grant_id"] = grant["id"]
            self._flush()
            return {"artwork_id": artwork_id, "grant_id": grant["id"], "duplicate": False, "status": GRANT_ACTIVE}

        # 指纹相同：同一作品的再次提交
        artwork_id = existing["artwork_id"]
        artwork = self._must_get("artworks", artwork_id, "作品")

        # 完全重复上传（同作品、同作者、同条款且已生效）：幂等返回原结果
        existing_grant_id = grant_index.get(index_key)
        if (
            existing_grant_id is not None
            and artwork["author_id"] == author_id
            and self.store.get("grants", existing_grant_id)["status"] == GRANT_ACTIVE
        ):
            existing_grant = self._must_get("grants", existing_grant_id, "授权")
            self._flush()
            return {
                "artwork_id": artwork_id,
                "grant_id": existing_grant_id,
                "duplicate": True,
                "status": existing_grant["status"],
            }

        # 复核中的同条款版本重复提交：返回原待办，不制造新待办
        pending_same = next(
            (
                gid for gid in artwork["grant_ids"]
                if (g := self.store.get("grants", gid))["status"] == GRANT_PENDING
                and g["terms_signature"] == terms_sig
            ),
            None,
        )
        if pending_same is not None:
            self._flush()
            return {
                "artwork_id": artwork_id,
                "grant_id": pending_same,
                "duplicate": True,
                "status": GRANT_PENDING,
                "review_required": True,
            }

        # 指纹相同但授权条款不同，或提交作者冲突：进入复核
        if artwork["author_id"] != author_id:
            note = "指纹相同但提交作者与登记作者不一致"
        else:
            note = "指纹相同但授权地域、期限或范围发生变化"
        grant = self._create_grant(
            artwork, author_id, territories, valid_from, valid_until, scope,
            terms_sig, submitter, version=len(artwork["grant_ids"]) + 1,
            status=GRANT_PENDING, review_note=note,
        )
        self._flush()
        return {
            "artwork_id": artwork_id,
            "grant_id": grant["id"],
            "duplicate": False,
            "status": GRANT_PENDING,
            "review_required": True,
            "note": note,
        }

    def _create_grant(
        self, artwork: dict[str, Any], author_id: str, territories: list[str],
        valid_from: str, valid_until: str, scope: str, terms_sig: str,
        submitter: dict[str, str], *, version: int, status: str, review_note: str | None,
    ) -> dict[str, Any]:
        grant_id = self._next_id("grant", "G")
        grant = {
            "id": grant_id,
            "artwork_id": artwork["id"],
            "author_id": author_id,
            "version": version,
            "territories": sorted(territories),
            "valid_from": valid_from,
            "valid_until": valid_until,
            "scope": scope,
            "terms_signature": terms_sig,
            "status": status,
            "submitted_by": submitter["id"],
            "submitted_at": self._now(),
            "review_note": review_note,
            "reviewed_by": None,
            "reviewed_at": None,
        }
        self.store.put("grants", grant_id, grant)
        artwork["grant_ids"].append(grant_id)
        # 幂等索引只收录已生效授权，待复核版本不占用条款键
        if status == GRANT_ACTIVE:
            self.store.collection("grant_index")[
                f"{artwork['fingerprint']}|{terms_sig}"
            ] = grant_id
        return grant

    def review_grant(
        self, actor: dict[str, str], grant_id: str, decision: str, note: str = ""
    ) -> dict[str, Any]:
        """版权审核人员对“同指纹、异授权”的版本进行复核。"""
        actor = self._require_role(actor, _REVIEW_ROLES)
        grant = self._must_get("grants", grant_id, "授权")
        if grant["status"] != GRANT_PENDING:
            raise DomainError("该授权版本不在待复核状态")
        if decision not in ("approve", "reject"):
            raise DomainError("复核结论应为 approve 或 reject")
        grant["status"] = GRANT_ACTIVE if decision == "approve" else GRANT_REJECTED
        grant["reviewed_by"] = actor["id"]
        grant["reviewed_at"] = self._now()
        grant["review_note"] = note or grant["review_note"]
        if decision == "approve":
            artwork = self._must_get("artworks", grant["artwork_id"], "作品")
            previous_id = artwork.get("current_grant_id")
            if previous_id and previous_id != grant_id:
                previous = self._must_get("grants", previous_id, "授权")
                previous["status"] = GRANT_SUPERSEDED
                previous["superseded_by"] = grant_id
                previous["superseded_at"] = grant["reviewed_at"]
            artwork["current_grant_id"] = grant_id
            self.store.collection("grant_index")[
                f"{artwork['fingerprint']}|{grant['terms_signature']}"
            ] = grant_id
        self._audit(
            "review_grant", actor,
            {"grant_id": grant_id, "decision": decision, "artwork_id": grant["artwork_id"]},
        )
        self._flush()
        return dict(grant)

    def list_pending_reviews(self) -> list[dict[str, Any]]:
        return [
            {"grant_id": g["id"], "artwork_id": g["artwork_id"], "note": g["review_note"],
             "submitted_at": g["submitted_at"]}
            for g in self.store.collection("grants").values()
            if g["status"] == GRANT_PENDING
        ]

    # ------------------------------------------------------------------
    # 展点与场次
    # ------------------------------------------------------------------

    def register_venue(
        self, name: str, village: str, manager_id: str, capacity: int
    ) -> dict[str, Any]:
        if not name or not name.strip():
            raise DomainError("展点名称不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise DomainError("展点容量必须为正整数")
        manager = self._must_get("artists", manager_id, "场地负责人")
        if manager.get("role") != VENUE_MANAGER:
            raise DomainError("展点负责人账号必须是村落场地负责人角色")
        venue_id = self._next_id("venue", "V")
        venue = {
            "id": venue_id,
            "name": name.strip(),
            "village": village,
            "manager_id": manager_id,
            "capacity": capacity,
            "status": VENUE_OPEN,
            "manager_name": manager["name"],
        }
        self.store.put("venues", venue_id, venue)
        self._flush()
        return dict(venue)

    def schedule_show(
        self,
        name: str,
        venue_id: str,
        start_date: str,
        end_date: str,
        territory: str,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        """在展点排一个场次（村落现场或中亚巡展站点）。"""
        actor = self._require_role(actor, frozenset({OPERATOR}))
        if not name or not name.strip():
            raise DomainError("场次名称不能为空")
        venue = self._must_get("venues", venue_id, "展点")
        start, end = _parse_day(start_date), _parse_day(end_date)
        if end < start:
            raise DomainError("场次结束日不能早于开始日")
        show_id = self._next_id("show", "S")
        show = {
            "id": show_id,
            "name": name.strip(),
            "venue_id": venue_id,
            "start_date": start_date,
            "end_date": end_date,
            "territory": territory,
            "status": SHOW_SCHEDULED,
            "created_by": actor["id"],
            "created_at": self._now(),
            "completed_at": None,
            "snapshot": None,
        }
        self.store.put("shows", show_id, show)
        self._flush()
        return dict(show)

    def close_venue(self, venue_id: str, actor: dict[str, str], reason: str = "") -> list[str]:
        """关闭场地：展点停用，并只冻结仍在该场地的在途/在场批次。"""
        actor = self._require_role(actor, frozenset({OPERATOR, VENUE_MANAGER}))
        venue = self._must_get("venues", venue_id, "展点")
        venue["status"] = VENUE_CLOSED
        venue["closed_at"] = self._now()
        frozen = []
        shows_at_venue = {
            sid for sid, show in self.store.collection("shows").items()
            if show["venue_id"] == venue_id and show["status"] != SHOW_COMPLETED
        }
        for batch in self.store.collection("batches").values():
            if batch["show_id"] in shows_at_venue and batch["status"] == BATCH_ACTIVE:
                self._freeze_batch(batch, FREEZE_VENUE_CLOSED, actor["id"], reason or "场地关闭")
                frozen.append(batch["id"])
        self._flush()
        return frozen

    def reopen_venue(self, venue_id: str, actor: dict[str, str]) -> None:
        actor = self._require_role(actor, frozenset({OPERATOR}))
        venue = self._must_get("venues", venue_id, "展点")
        venue["status"] = VENUE_OPEN
        venue.pop("closed_at", None)
        self._flush()

    def venue_schedule(self, actor: dict[str, str], venue_id: str) -> dict[str, Any]:
        """村民视角：场地负责人只能看到自己场地的安排。"""
        actor = self._actor(actor)
        venue = self._must_get("venues", venue_id, "展点")
        if actor["role"] == VENUE_MANAGER and actor["id"] != venue["manager_id"]:
            raise DomainError("只能查看本场地的安排")
        if actor["role"] not in (VENUE_MANAGER, OPERATOR, CARRIER):
            raise DomainError("无权查看场地安排")
        items = []
        for show in self.store.collection("shows").values():
            if show["venue_id"] != venue_id:
                continue
            admitted = [
                {
                    "artwork_id": key.split("@", 1)[0],
                    "decided_at": record["decided_at"],
                }
                for key, record in self.store.collection("admission").items()
                if record["show_id"] == show["id"] and record["decision"] == "approved"
            ]
            items.append(
                {
                    "show_id": show["id"],
                    "name": show["name"],
                    "start_date": show["start_date"],
                    "end_date": show["end_date"],
                    "status": show["status"],
                    "admitted_count": len(admitted),
                    "admitted": admitted,
                }
            )
        return {
            "venue_id": venue_id,
            "venue_name": venue["name"],
            "village": venue["village"],
            "capacity": venue["capacity"],
            "status": venue["status"],
            "shows": items,
        }

    # ------------------------------------------------------------------
    # 巡展批次与冻结
    # ------------------------------------------------------------------

    def create_batch(
        self, name: str, show_id: str, artwork_ids: list[str],
        actor: dict[str, str], *, cross_border: bool = False, note: str = "",
    ) -> dict[str, Any]:
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER}))
        show = self._must_get("shows", show_id, "场次")
        if show["status"] == SHOW_COMPLETED:
            raise DomainError("已完成的场次不能再编排批次")
        for artwork_id in artwork_ids:
            self._must_get("artworks", artwork_id, "作品")
        batch_id = self._next_id("batch", "B")
        batch = {
            "id": batch_id,
            "name": name,
            "show_id": show_id,
            "artwork_ids": list(artwork_ids),
            "cross_border": cross_border,
            "status": BATCH_ACTIVE,
            "freeze_reason": None,
            "frozen_by": None,
            "frozen_at": None,
            "note": note,
            "created_by": actor["id"],
            "created_at": self._now(),
        }
        self.store.put("batches", batch_id, batch)
        self._flush()
        return dict(batch)

    def _freeze_batch(self, batch: dict[str, Any], reason: str, by: str, note: str) -> None:
        batch["status"] = BATCH_FROZEN
        batch["freeze_reason"] = reason
        batch["frozen_by"] = by
        batch["frozen_at"] = self._now()
        batch["freeze_note"] = note

    def freeze_batch(self, batch_id: str, actor: dict[str, str], reason: str, note: str = "") -> None:
        """手动冻结单个批次，其他批次与展点不受影响。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER, REVIEWER}))
        batch = self._must_get("batches", batch_id, "批次")
        if batch["status"] == BATCH_FROZEN:
            raise DomainError("批次已处于冻结状态")
        self._freeze_batch(batch, reason, actor["id"], note)
        self._flush()

    def unfreeze_batch(self, batch_id: str, actor: dict[str, str], note: str = "") -> None:
        """解除冻结后，到期提醒、布展与赔付流程照常接续。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER}))
        batch = self._must_get("batches", batch_id, "批次")
        if batch["status"] != BATCH_FROZEN:
            raise DomainError("批次未处于冻结状态")
        batch["status"] = BATCH_ACTIVE
        batch.setdefault("freeze_history", []).append(
            {"reason": batch["freeze_reason"], "frozen_at": batch["frozen_at"],
             "released_by": actor["id"], "released_at": self._now(), "note": note}
        )
        batch["freeze_reason"] = None
        batch["frozen_by"] = None
        batch["frozen_at"] = None
        self._flush()

    def report_customs_delay(self, batch_id: str, actor: dict[str, str], note: str = "") -> None:
        """跨境运输延误：仅冻结该批次。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER}))
        batch = self._must_get("batches", batch_id, "批次")
        if not batch.get("cross_border"):
            raise DomainError("该批次不是跨境运输批次")
        if batch["status"] == BATCH_FROZEN:
            raise DomainError("批次已处于冻结状态")
        self._freeze_batch(batch, FREEZE_CUSTOMS, actor["id"], note or "中亚跨境运输延误")
        self._flush()

    def report_damage(
        self, batch_id: str, artwork_id: str, actor: dict[str, str], note: str = ""
    ) -> dict[str, Any]:
        """作品受损：登记损坏交接、只冻结所属批次，并生成待理赔线索。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER, VENUE_MANAGER}))
        batch = self._must_get("batches", batch_id, "批次")
        self._must_get("artworks", artwork_id, "作品")
        if artwork_id not in batch["artwork_ids"]:
            raise DomainError("受损作品不属于该批次")
        handover = self._record_handover(
            batch, artwork_id, HANDOVER_DAMAGED, actor, note=note, persist=False
        )
        if batch["status"] != BATCH_FROZEN:
            self._freeze_batch(batch, FREEZE_DAMAGE, actor["id"], note or f"作品{artwork_id}受损")
        self._flush()
        return handover

    # ------------------------------------------------------------------
    # 授权例外：提交人不得批准自己的例外
    # ------------------------------------------------------------------

    def request_exception(
        self, artwork_id: str, show_id: str, submitted_by: dict[str, str],
        reason: str, allowance: str,
    ) -> dict[str, Any]:
        """申请授权例外（如授权覆盖不到中亚地域、展期末段超出期限）。"""
        submitter = self._actor(submitted_by)
        self._must_get("artworks", artwork_id, "作品")
        self._must_get("shows", show_id, "场次")
        if allowance not in ("territory", "expiry"):
            raise DomainError("例外类型应为 territory 或 expiry")
        exception_id = self._next_id("exception", "X")
        record = {
            "id": exception_id,
            "artwork_id": artwork_id,
            "show_id": show_id,
            "allowance": allowance,
            "reason": reason,
            "status": EXCEPTION_PENDING,
            "submitted_by": submitter["id"],
            "submitted_at": self._now(),
            "decided_by": None,
            "decided_at": None,
            "decision_note": None,
        }
        self.store.put("exceptions", exception_id, record)
        self._flush()
        return dict(record)

    def decide_exception(
        self, actor: dict[str, str], exception_id: str, decision: str, note: str = ""
    ) -> dict[str, Any]:
        """审核人员批准/驳回例外；提交人不能批准自己的申请。"""
        actor = self._require_role(actor, _REVIEW_ROLES)
        record = self._must_get("exceptions", exception_id, "授权例外")
        if record["status"] != EXCEPTION_PENDING:
            raise DomainError("该例外申请已有结论")
        if decision not in ("approve", "reject"):
            raise DomainError("结论应为 approve 或 reject")
        if record["submitted_by"] == actor["id"]:
            raise DomainError("提交授权的人不能批准自己的例外")
        record["status"] = EXCEPTION_APPROVED if decision == "approve" else EXCEPTION_REJECTED
        record["decided_by"] = actor["id"]
        record["decided_at"] = self._now()
        record["decision_note"] = note
        self._audit("decide_exception", actor, {"exception_id": exception_id, "decision": decision})
        self._flush()
        return dict(record)

    def _approved_exception(self, artwork_id: str, show_id: str, allowance: str) -> dict | None:
        for record in self.store.collection("exceptions").values():
            if (
                record["artwork_id"] == artwork_id
                and record["show_id"] == show_id
                and record["allowance"] == allowance
                and record["status"] == EXCEPTION_APPROVED
            ):
                return record
        return None

    # ------------------------------------------------------------------
    # 准入裁决：授权到期闸门、地域、容量、冻结
    # ------------------------------------------------------------------

    def _admission_key(self, artwork_id: str, show_id: str) -> str:
        return f"{artwork_id}@{show_id}"

    def _approved_count(self, show_id: str) -> int:
        return sum(
            1
            for record in self.store.collection("admission").values()
            if record["show_id"] == show_id and record["decision"] == "approved"
        )

    def _frozen_batches_for(self, artwork_id: str, show_id: str) -> list[dict[str, Any]]:
        return [
            batch
            for batch in self.store.collection("batches").values()
            if batch["show_id"] == show_id
            and artwork_id in batch["artwork_ids"]
            and batch["status"] == BATCH_FROZEN
        ]

    def admit(
        self, artwork_id: str, show_id: str, actor: dict[str, str]
    ) -> dict[str, Any]:
        """裁决一幅作品能否进入某场次，并把理由落盘以便追溯。"""
        actor = self._require_role(actor, frozenset({OPERATOR, REVIEWER}))
        artwork = self._must_get("artworks", artwork_id, "作品")
        show = self._must_get("shows", show_id, "场次")
        venue = self._must_get("venues", show["venue_id"], "展点")

        reasons: list[str] = []
        allowances: list[str] = []

        if show["status"] == SHOW_COMPLETED:
            reasons.append("场次已完成，不能新增作品")

        grant = (
            self.store.get("grants", artwork["current_grant_id"])
            if artwork.get("current_grant_id")
            else None
        )
        if grant is None:
            reasons.append("作品没有生效中的授权版本")
        elif grant["status"] == GRANT_PENDING:
            reasons.append("授权版本正在复核，尚未生效")
        elif grant["status"] == GRANT_REJECTED:
            reasons.append("当前授权版本已被复核驳回")

        if grant is not None and grant["status"] == GRANT_ACTIVE and show["status"] != SHOW_COMPLETED:
            show_end = _parse_day(show["end_date"])
            valid_until = _parse_day(grant["valid_until"])
            if show_end > valid_until:
                exception = self._approved_exception(artwork_id, show_id, "expiry")
                if exception is None:
                    reasons.append(
                        f"授权于{grant['valid_until']}到期，不能覆盖展期至{show['end_date']}的新场次"
                    )
                else:
                    allowances.append(f"到期例外{exception['id']}已由{exception['decided_by']}批准")
            if show["territory"] not in grant["territories"]:
                exception = self._approved_exception(artwork_id, show_id, "territory")
                if exception is None:
                    reasons.append(
                        f"授权地域{grant['territories']}不包含场次地域{show['territory']}"
                    )
                else:
                    allowances.append(f"地域例外{exception['id']}已由{exception['decided_by']}批准")

        if venue["status"] == VENUE_CLOSED:
            reasons.append("展点已关闭")

        if self._approved_count(show_id) >= venue["capacity"]:
            reasons.append(f"展点容量{venue['capacity']}组已满")

        frozen_batches = self._frozen_batches_for(artwork_id, show_id)
        if frozen_batches:
            reasons.append(
                "相关批次冻结："
                + "，".join(f"{b['id']}（{b['freeze_reason']}）" for b in frozen_batches)
            )

        approved = not reasons
        record = {
            "artwork_id": artwork_id,
            "show_id": show_id,
            "decision": "approved" if approved else "rejected",
            "reasons": reasons,
            "allowances": allowances,
            "grant_id": grant["id"] if grant else None,
            "grant_version": grant["version"] if grant else None,
            "decided_by": actor["id"],
            "decided_at": self._now(),
        }
        self.store.put("admission", self._admission_key(artwork_id, show_id), record)
        self._flush()
        if not approved:
            raise DomainError("准入未通过：" + "；".join(reasons))
        return dict(record)

    # ------------------------------------------------------------------
    # 布展交接与撤回回收
    # ------------------------------------------------------------------

    def _record_handover(
        self, batch: dict[str, Any], artwork_id: str, action: str,
        actor: dict[str, str], *, from_party: str = "", to_party: str = "",
        note: str, persist: bool,
    ) -> dict[str, Any]:
        handover_id = self._next_id("handover", "H")
        record = {
            "id": handover_id,
            "batch_id": batch["id"],
            "show_id": batch["show_id"],
            "artwork_id": artwork_id,
            "action": action,
            "from_party": from_party,
            "to_party": to_party,
            "by": actor["id"],
            "at": self._now(),
            "note": note,
        }
        self.store.put("handovers", handover_id, record)
        if persist:
            self._flush()
        return record

    def install_artwork(
        self, batch_id: str, artwork_id: str, actor: dict[str, str],
        from_party: str, to_party: str, note: str = "",
    ) -> dict[str, Any]:
        """布展交接：凭准入记录入场，冻结批次或关闭场地不得布展。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER, VENUE_MANAGER}))
        batch = self._must_get("batches", batch_id, "批次")
        show = self._must_get("shows", batch["show_id"], "场次")
        self._must_get("artworks", artwork_id, "作品")
        if artwork_id not in batch["artwork_ids"]:
            raise DomainError("作品不属于该批次")
        admission = self.store.get("admission", self._admission_key(artwork_id, batch["show_id"]))
        if admission is None or admission["decision"] != "approved":
            raise DomainError("作品未获准进入该场次，不能布展")
        if batch["status"] == BATCH_FROZEN:
            raise DomainError(f"批次已冻结（{batch['freeze_reason']}），暂不能布展")
        venue = self._must_get("venues", show["venue_id"], "展点")
        if venue["status"] == VENUE_CLOSED:
            raise DomainError("展点已关闭，不能布展")
        if show["status"] == SHOW_COMPLETED:
            raise DomainError("场次已完成，不能再布展")
        if self._custody_state(artwork_id, batch["show_id"]) == HANDOVER_INSTALL:
            raise DomainError("作品已布展且尚未撤回")
        return self._record_handover(
            batch, artwork_id, HANDOVER_INSTALL, actor,
            from_party=from_party, to_party=to_party, note=note, persist=True,
        )

    def return_artwork(
        self, batch_id: str, artwork_id: str, actor: dict[str, str],
        from_party: str, to_party: str, note: str = "",
    ) -> dict[str, Any]:
        """作品撤回/回收登记，记录由谁交接、何时回收。"""
        actor = self._require_role(actor, frozenset({OPERATOR, CARRIER, VENUE_MANAGER}))
        batch = self._must_get("batches", batch_id, "批次")
        self._must_get("artworks", artwork_id, "作品")
        if artwork_id not in batch["artwork_ids"]:
            raise DomainError("作品不属于该批次")
        if self._custody_state(artwork_id, batch["show_id"]) != HANDOVER_INSTALL:
            raise DomainError("作品当前不在布展状态，无需撤回")
        return self._record_handover(
            batch, artwork_id, HANDOVER_RETURN, actor,
            from_party=from_party, to_party=to_party, note=note, persist=True,
        )

    def _show_handovers(self, artwork_id: str, show_id: str) -> list[dict[str, Any]]:
        return [
            h for h in self.store.collection("handovers").values()
            if h["artwork_id"] == artwork_id and h["show_id"] == show_id
        ]

    def _custody_state(self, artwork_id: str, show_id: str) -> str | None:
        state = None
        for h in sorted(self._show_handovers(artwork_id, show_id), key=lambda r: r["at"]):
            if h["action"] == HANDOVER_INSTALL:
                state = HANDOVER_INSTALL
            elif h["action"] == HANDOVER_RETURN:
                state = HANDOVER_RETURN
            elif h["action"] == HANDOVER_DAMAGED:
                # 损坏不改变在场/撤回的清点状态，单独在交接链中可见
                pass
        return state

    def custody_chain(self, artwork_id: str, show_id: str) -> list[dict[str, Any]]:
        return [dict(h) for h in sorted(self._show_handovers(artwork_id, show_id), key=lambda r: r["at"])]

    # ------------------------------------------------------------------
    # 翻译文本（随完成场次固化版本）
    # ------------------------------------------------------------------

    def add_translation(
        self, artwork_id: str, language: str, text: str, actor: dict[str, str]
    ) -> dict[str, Any]:
        """为作品登记展签译文；相同文本重复提交返回原版本。"""
        actor = self._require_role(actor, frozenset({OPERATOR}))
        self._must_get("artworks", artwork_id, "作品")
        if not language or not text or not text.strip():
            raise DomainError("译文语言与内容不能为空")
        text_hash = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        existing = self.store.collection("translations").get(f"{artwork_id}|{language}")
        if existing and existing["text_hash"] == text_hash:
            return dict(existing)
        version = (existing["version"] + 1) if existing else 1
        record = {
            "id": existing["id"] if existing else self._next_id("translation", "T"),
            "artwork_id": artwork_id,
            "language": language,
            "text": text.strip(),
            "text_hash": text_hash,
            "version": version,
            "updated_by": actor["id"],
            "updated_at": self._now(),
        }
        self.store.put("translations", f"{artwork_id}|{language}", record)
        self._flush()
        return dict(record)

    # ------------------------------------------------------------------
    # 撤展清点与完成快照
    # ------------------------------------------------------------------

    def teardown_check(self, show_id: str) -> dict[str, Any]:
        """撤展清点：核对每幅准入作品是已回收、仍在场还是已受损。"""
        self._must_get("shows", show_id, "场次")
        approved_ids = [
            key.split("@", 1)[0]
            for key, record in self.store.collection("admission").items()
            if record["show_id"] == show_id and record["decision"] == "approved"
        ]
        returned, outstanding, damaged = [], [], []
        for artwork_id in approved_ids:
            chain = self._show_handovers(artwork_id, show_id)
            if any(h["action"] == HANDOVER_DAMAGED for h in chain):
                damaged.append(artwork_id)
            if self._custody_state(artwork_id, show_id) == HANDOVER_RETURN:
                returned.append(artwork_id)
            else:
                outstanding.append(artwork_id)
        return {
            "show_id": show_id,
            "approved": approved_ids,
            "returned": returned,
            "outstanding": outstanding,
            "damaged": damaged,
            "complete": not outstanding,
        }

    def complete_show(self, show_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """完成展览：固化当时授权、译文与交接版本，此后不再接受新增。"""
        actor = self._require_role(actor, frozenset({OPERATOR}))
        show = self._must_get("shows", show_id, "场次")
        if show["status"] == SHOW_COMPLETED:
            raise DomainError("场次已完成")
        translations = self.store.collection("translations")
        items = []
        for key, admission in self.store.collection("admission").items():
            if admission["show_id"] != show_id or admission["decision"] != "approved":
                continue
            artwork_id = key.split("@", 1)[0]
            grant = self.store.get("grants", admission["grant_id"])
            chain = self._show_handovers(artwork_id, show_id)
            installed = next((h for h in chain if h["action"] == HANDOVER_INSTALL), None)
            returned = next((h for h in reversed(chain) if h["action"] == HANDOVER_RETURN), None)
            work = self._must_get("artworks", artwork_id, "作品")
            item_translations = {}
            for tkey, t in translations.items():
                if t["artwork_id"] == artwork_id:
                    item_translations[t["language"]] = {
                        "translation_id": t["id"],
                        "version": t["version"],
                        "text_hash": t["text_hash"],
                    }
            items.append(
                {
                    "artwork_id": artwork_id,
                    "title": work["title"],
                    "fingerprint": work["fingerprint"],
                    "grant": {
                        "grant_id": grant["id"],
                        "version": grant["version"],
                        "territories": list(grant["territories"]),
                        "valid_from": grant["valid_from"],
                        "valid_until": grant["valid_until"],
                        "scope": grant["scope"],
                    },
                    "allowances": list(admission.get("allowances", [])),
                    "installed": None if installed is None else {
                        "handover_id": installed["id"], "by": installed["by"],
                        "from_party": installed["from_party"], "to_party": installed["to_party"],
                        "at": installed["at"],
                    },
                    "returned": None if returned is None else {
                        "handover_id": returned["id"], "by": returned["by"],
                        "from_party": returned["from_party"], "to_party": returned["to_party"],
                        "at": returned["at"],
                    },
                    "translations": item_translations,
                }
            )
        snapshot = {
            "completed_at": self._now(),
            "completed_by": actor["id"],
            "teardown": self.teardown_check(show_id),
            "items": items,
        }
        show["status"] = SHOW_COMPLETED
        show["completed_at"] = snapshot["completed_at"]
        show["snapshot"] = snapshot
        self._flush()
        return snapshot

    # ------------------------------------------------------------------
    # 保险与赔付
    # ------------------------------------------------------------------

    def create_policy(
        self, batch_id: str, coverage: float, insurer: str, actor: dict[str, str]
    ) -> dict[str, Any]:
        actor = self._require_role(actor, frozenset({OPERATOR, REVIEWER}))
        self._must_get("batches", batch_id, "批次")
        if not isinstance(coverage, (int, float)) or coverage <= 0:
            raise DomainError("保额必须为正数")
        policies = self.store.collection("insurance").setdefault("policies", {})
        policy_id = self._next_id("policy", "P")
        record = {
            "id": policy_id,
            "batch_id": batch_id,
            "coverage": float(coverage),
            "insurer": insurer,
            "created_by": actor["id"],
            "created_at": self._now(),
        }
        policies[policy_id] = record
        self._flush()
        return dict(record)

    def file_claim(
        self, artwork_id: str, batch_id: str, amount: float, actor: dict[str, str], note: str = ""
    ) -> dict[str, Any]:
        """凭损坏交接记录登记赔付申请；冻结/重启都不丢失。"""
        actor = self._require_role(actor, frozenset({OPERATOR, VENUE_MANAGER}))
        batch = self._must_get("batches", batch_id, "批次")
        self._must_get("artworks", artwork_id, "作品")
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise DomainError("赔付金额必须为正数")
        damage_events = [
            h for h in self.store.collection("handovers").values()
            if h["artwork_id"] == artwork_id and h["batch_id"] == batch_id
            and h["action"] == HANDOVER_DAMAGED
        ]
        if not damage_events:
            raise DomainError("没有损坏交接记录，不能申请赔付")
        claims = self.store.collection("insurance").setdefault("claims", {})
        # 同一损坏事件重复申报幂等
        damage_id = damage_events[-1]["id"]
        for existing in claims.values():
            if existing["damage_handover_id"] == damage_id:
                return dict(existing)
        claim_id = self._next_id("claim", "C")
        record = {
            "id": claim_id,
            "artwork_id": artwork_id,
            "batch_id": batch_id,
            "show_id": batch["show_id"],
            "amount": float(amount),
            "status": CLAIM_OPEN,
            "damage_handover_id": damage_id,
            "filed_by": actor["id"],
            "filed_at": self._now(),
            "verified_by": None,
            "verified_at": None,
            "settled_by": None,
            "settled_at": None,
            "note": note,
        }
        claims[claim_id] = record
        self._flush()
        return dict(record)

    def _policy_for_batch(self, batch_id: str) -> dict[str, Any] | None:
        policies = self.store.collection("insurance").get("policies", {})
        matching = [p for p in policies.values() if p["batch_id"] == batch_id]
        return matching[-1] if matching else None

    def verify_claim(self, claim_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """赔付核对：损坏记录、在场批次与保额三项一致方可通过。"""
        actor = self._require_role(actor, _REVIEW_ROLES)
        claims = self.store.collection("insurance").setdefault("claims", {})
        claim = claims.get(claim_id)
        if claim is None:
            raise DomainError(f"赔付申请不存在：{claim_id}")
        if claim["status"] != CLAIM_OPEN:
            raise DomainError("赔付申请不在待核对状态")
        damage = self.store.get("handovers", claim["damage_handover_id"])
        if damage is None or damage["action"] != HANDOVER_DAMAGED:
            raise DomainError("损坏交接记录缺失或不匹配")
        policy = self._policy_for_batch(claim["batch_id"])
        if policy is None:
            raise DomainError("该批次没有有效保险单")
        if claim["amount"] > policy["coverage"]:
            raise DomainError(
                f"赔付金额{claim['amount']}超出保额{policy['coverage']}"
            )
        claim["status"] = CLAIM_VERIFIED
        claim["verified_by"] = actor["id"]
        claim["verified_at"] = self._now()
        claim["policy_id"] = policy["id"]
        self._audit("verify_claim", actor, {"claim_id": claim_id, "policy_id": policy["id"]})
        self._flush()
        return dict(claim)

    def settle_claim(self, claim_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """核对通过后结案赔付。"""
        actor = self._require_role(actor, frozenset({OPERATOR, REVIEWER}))
        claims = self.store.collection("insurance").setdefault("claims", {})
        claim = claims.get(claim_id)
        if claim is None:
            raise DomainError(f"赔付申请不存在：{claim_id}")
        if claim["status"] != CLAIM_VERIFIED:
            raise DomainError("赔付申请尚未通过核对")
        claim["status"] = CLAIM_PAID
        claim["settled_by"] = actor["id"]
        claim["settled_at"] = self._now()
        self._flush()
        return dict(claim)

    def list_claims(self, status: str | None = None) -> list[dict[str, Any]]:
        claims = self.store.collection("insurance").get("claims", {})
        return [
            dict(claim) for claim in claims.values()
            if status is None or claim["status"] == status
        ]

    # ------------------------------------------------------------------
    # 授权到期提醒（重启后继续接续）
    # ------------------------------------------------------------------

    def expiry_reminders(self, today: str | None = None, window_days: int = 30) -> list[dict[str, Any]]:
        """找出窗口内到期或已逾期未续的生效授权并登记提醒。

        逾期授权仍会阻断新场次，因此每天都会继续提醒直到续签；同一天对
        同一授权重复扫描不重复产生提醒。
        """
        today_date = _parse_day(today) if today else self._today()
        if window_days < 0:
            raise DomainError("提醒窗口不能为负数")
        reminded = self.store.collection("reminders")
        due = []
        for grant in self.store.collection("grants").values():
            if grant["status"] != GRANT_ACTIVE:
                continue
            valid_until = _parse_day(grant["valid_until"])
            delta = valid_until.toordinal() - today_date.toordinal()
            if delta > window_days:
                continue
            key = f"{grant['id']}@{today_date.isoformat()}"
            if key in reminded:
                continue
            record = {
                "grant_id": grant["id"],
                "artwork_id": grant["artwork_id"],
                "valid_until": grant["valid_until"],
                "days_left": delta,
                "reminded_on": today_date.isoformat(),
                "created_at": self._now(),
            }
            reminded[key] = record
            due.append(record)
        self._flush()
        return due

    # ------------------------------------------------------------------
    # 可解释性：一幅作品在某次展览中的完整裁决与交接
    # ------------------------------------------------------------------

    def explain(self, artwork_id: str, show_id: str) -> dict[str, Any]:
        """解释作品为何获准（或未获准）、由谁交接、何时回收，引用快照版本。"""
        artwork = self._must_get("artworks", artwork_id, "作品")
        show = self._must_get("shows", show_id, "场次")
        admission = self.store.get("admission", self._admission_key(artwork_id, show_id))
        chain = self.custody_chain(artwork_id, show_id)
        result: dict[str, Any] = {
            "artwork_id": artwork_id,
            "show_id": show_id,
            "show_status": show["status"],
            "admission": dict(admission) if admission else None,
            "custody_chain": chain,
        }
        if show["status"] == SHOW_COMPLETED and show.get("snapshot"):
            snap_item = next(
                (item for item in show["snapshot"]["items"] if item["artwork_id"] == artwork_id),
                None,
            )
            result["completed_snapshot"] = snap_item
        return result
