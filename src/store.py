"""作品与场地服务的持久化存储。

数据以单个 JSON 文件落盘，写入采用临时文件加原子替换，保证服务重启后
状态完整接续（到期提醒、撤展清点、赔付核对都依赖同一份状态）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _empty_state() -> dict[str, Any]:
    return {
        "artists": {},          # author_id -> 作者信息（含受保护联系方式）
        "artworks": {},         # artwork_id -> 作品主档（指纹、当前版本指针等）
        "grants": {},           # grant_id -> 授权版本（不可变）
        "venues": {},           # venue_id -> 展点（容量、状态）
        "shows": {},            # show_id -> 场次（展点、展期、冻结状态、完成快照）
        "batches": {},          # batch_id -> 巡展批次
        "handovers": {},        # handover_id -> 布展/撤展交接记录
        "translations": {},     # translation_id -> 翻译文本
        "insurance": {},        # insurance_id -> 保险单与赔付记录
        "exceptions": {},       # exception_id -> 授权例外申请
        "counters": {},         # 各序列计数器
        "admission": {},        # (artwork_id, show_id) 的准入裁决记录与理由
        "contacts": {},         # artist_id -> 受保护联系方式（不进入检索结果）
        "reminders": {},        # grant_id@日期 -> 到期提醒（幂等）
        "audit": [],            # 敏感访问与审批留痕

        # 幂等索引：fingerprint -> {"id": artwork_id, "grant": grant_id}
        "fingerprint_index": {},
        # 幂等索引：(fingerprint, grant 关键条款签名) -> grant_id
        "grant_index": {},
    }


class JsonStore:
    """原子写入的 JSON 状态库。

    所有写入在内存中完成后调用 :meth:`flush` 落盘；服务提供方在每个
    变更命令末尾统一 flush，命令之间状态一致。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = _empty_state()
            self.flush()

    def collection(self, name: str) -> dict[str, Any]:
        return self.state[name]

    def get(self, section: str, key: str) -> Any:
        return self.state[section].get(key)

    def put(self, section: str, key: str, value: Any) -> None:
        self.state[section][key] = value

    def remove(self, section: str, key: str) -> Any:
        return self.state[section].pop(key, None)

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
