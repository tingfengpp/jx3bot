from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from nonebot import logger
from src.utils.jjc_query_observability import foreground_safe_logger

logger = foreground_safe_logger(logger, component="profile_repo")

from src.infra.mongo import get_db as _get_db

SCHEMA_VERSION = 1


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


@dataclass(frozen=True)
class RoleJjcCacheRepo:
    """JJC 角色画像缓存仓储。

    关联 role_identities.identity_key，存储心法、武器、队友等可变缓存信息。
    """

    db: Optional[AsyncIOMotorDatabase] = None

    def _col(self):
        db = self.db if self.db is not None else _get_db()
        return db.role_jjc_cache

    # ---- 读取 ----

    async def load_by_identity_key(self, identity_key: str) -> Optional[Dict[str, Any]]:
        """按 identity_key 直接读取缓存。"""
        doc = await self._col().find_one({"identity_key": identity_key})
        if doc:
            doc.pop("_id", None)
        return doc

    async def load_by_best_identity(
        self,
        server: Optional[str] = None,
        name: Optional[str] = None,
        zone: Optional[str] = None,
        game_role_id: Optional[str] = None,
        global_id: Optional[str] = None,
        global_role_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按可用 ID 优先级查找 JJC 缓存：global_id > global_role_id > zone+game_role_id > server+name。"""
        replay_gid = (global_id or "").strip()
        if replay_gid:
            doc = await self._col().find_one({"global_id": replay_gid})
            if doc:
                doc.pop("_id", None)
                return doc

        gid = (global_role_id or "").strip()
        if gid:
            doc = await self._col().find_one({"global_role_id": gid})
            if doc:
                doc.pop("_id", None)
                return doc

        z = (zone or "").strip()
        grid = (game_role_id or "").strip()
        if z and grid:
            doc = await self._col().find_one({"zone": z, "game_role_id": grid})
            if doc:
                doc.pop("_id", None)
                return doc

        ns = _normalize(server or "")
        nn = _normalize(name or "")
        if ns and nn:
            doc = await self._col().find_one({"normalized_server": ns, "normalized_name": nn})
            if doc:
                doc.pop("_id", None)
                return doc

        return None

    # ---- 写入 ----

    async def save(self, identity_key: str, cache_data: Dict[str, Any]) -> None:
        """写入或更新 JJC 缓存。cache_data 中的字段会直接 $set 到文档。

        cache_data 可选包含：server, name, zone, game_role_id, role_id, global_id, global_role_id,
        kungfu, kungfu_id, kungfu_pinyin, kungfu_indicator, kungfu_match_history,
        kungfu_selected_source, weapon, weapon_icon, weapon_quality, weapon_checked,
        teammates, teammates_checked, match_history_checked, match_history_win_samples,
        source, expires_at 等。
        """
        now = datetime.now(timezone.utc)
        set_fields = dict(cache_data)
        set_fields["identity_key"] = identity_key
        set_fields.setdefault("checked_at", now)
        set_fields.setdefault("schema_version", SCHEMA_VERSION)
        # 写入规范化字段，按 server/name 查询时使用
        if "server" in cache_data:
            set_fields.setdefault("normalized_server", _normalize(cache_data["server"]))
        if "name" in cache_data:
            set_fields.setdefault("normalized_name", _normalize(cache_data["name"]))

        try:
            await self._col().update_one(
                {"identity_key": identity_key},
                {"$set": set_fields},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("JJC 缓存保存失败: identity_key={} error={}", identity_key, exc)
            raise

    # ---- 迁移 ----

    async def migrate_identity_key(self, old_key: str, new_key: str) -> None:
        """将 role_jjc_cache 中 identity_key 从旧值迁移到新值。

        若 new_key 已有缓存，则将旧文档中的非关键字段合并到目标文档（不覆盖目标已有字段），
        随后删除旧文档，避免产生重复 identity_key。
        """
        try:
            old_doc = await self._col().find_one({"identity_key": old_key})
            if not old_doc:
                return
            new_doc = await self._col().find_one({"identity_key": new_key})
            if new_doc:
                _PROTECTED = {
                    "_id", "identity_key", "server", "name", "zone",
                    "game_role_id", "global_id", "global_role_id", "normalized_server",
                    "normalized_name", "role_id",
                }
                merge = {
                    k: v for k, v in old_doc.items()
                    if k not in _PROTECTED and k not in new_doc
                }
                if merge:
                    await self._col().update_one(
                        {"identity_key": new_key}, {"$set": merge},
                    )
                await self._col().delete_one({"_id": old_doc["_id"]})
                logger.info(
                    "JJC 缓存 identity_key 迁移(合并): old={} new={} merged_fields={}",
                    old_key, new_key, list(merge.keys()),
                )
            else:
                result = await self._col().update_many(
                    {"identity_key": old_key},
                    {"$set": {"identity_key": new_key}},
                )
                if result.modified_count:
                    logger.info(
                        "JJC 缓存 identity_key 迁移: old={} new={} modified_count={}",
                        old_key, new_key, result.modified_count,
                    )
        except Exception as exc:
            logger.warning(
                "JJC 缓存 identity_key 迁移失败: old={} new={} error={}",
                old_key, new_key, exc,
            )
            raise

    # ---- 清理 ----

    async def cleanup_expired(self, ttl_seconds: int = 604800) -> int:
        """清理过期缓存（TTL 索引的兜底清理）。返回删除数。

        优先按 expires_at 判断，无 expires_at 则按 checked_at 判断。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        try:
            result = await self._col().delete_many({
                "$or": [
                    {"expires_at": {"$lt": cutoff}},
                    {
                        "expires_at": None,
                        "checked_at": {"$lt": cutoff},
                    },
                ]
            })
            if result.deleted_count:
                logger.info("JJC 缓存清理完成: deleted={} cutoff={}", result.deleted_count, cutoff.isoformat())
            return result.deleted_count
        except Exception as exc:
            logger.warning("JJC 缓存清理失败: {}", exc)
            return 0
