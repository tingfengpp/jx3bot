from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from nonebot import logger

from src.infra.mongo import get_db as _get_db
from src.services.jx3.match_detail_snapshots import TALENT_SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class JjcMatchSnapshotRepo:
    db: Optional[AsyncIOMotorDatabase] = None

    async def save_equipment_snapshot(
        self, snapshot_hash: str, armors: List[Dict[str, Any]], seen_at: Optional[float] = None
    ) -> None:
        db = self.db if self.db is not None else _get_db()
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            await db.jjc_equipment_snapshot.update_one(
                {"snapshot_hash": snapshot_hash},
                {
                    "$setOnInsert": {
                        "armors": armors,
                        "created_at": now,
                        "schema_version": 1,
                    },
                    "$set": {
                        "last_seen_at": seen_at if seen_at is not None else now,
                    },
                },
                upsert=True,
            )
        except Exception as exc:
            logger.warning("保存装备快照失败: hash={} error={}", snapshot_hash, exc)
            raise

    async def save_talent_snapshot(
        self, snapshot_hash: str, talents: List[Dict[str, Any]], seen_at: Optional[float] = None
    ) -> None:
        db = self.db if self.db is not None else _get_db()
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            await db.jjc_talent_snapshot.update_one(
                {"snapshot_hash": snapshot_hash},
                {
                    "$setOnInsert": {
                        "talents": talents,
                        "created_at": now,
                        "schema_version": TALENT_SNAPSHOT_SCHEMA_VERSION,
                    },
                    "$set": {
                        "last_seen_at": seen_at if seen_at is not None else now,
                    },
                },
                upsert=True,
            )
        except Exception as exc:
            logger.warning("保存奇穴快照失败: hash={} error={}", snapshot_hash, exc)
            raise

    async def load_local_win_equipment_snapshots(self, snapshot_hashes):
        """Complete, native-bounded equipment reads; no legacy loader side effects."""
        if not snapshot_hashes:
            return {}
        db = self.db if self.db is not None else _get_db()
        cursor = db.jjc_equipment_snapshot.find({"snapshot_hash": {"$in": snapshot_hashes}})
        primary_error = None
        try:
            cursor = cursor.max_time_ms(1500).batch_size(512)
            result = {}
            async for doc in cursor:
                result[doc["snapshot_hash"]] = doc
            return result
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                # Cancelling the async close wait is not proof that a submitted
                # driver operation stopped; the dedicated socket budget remains.
                await asyncio.wait_for(cursor.close(), timeout=0.1)
            except BaseException:
                if primary_error is None:
                    raise

    async def load_equipment_snapshots(
        self, snapshot_hashes: List[str], *, strict: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        if not snapshot_hashes:
            return {}
        db = self.db if self.db is not None else _get_db()
        try:
            cursor = db.jjc_equipment_snapshot.find(
                {"snapshot_hash": {"$in": snapshot_hashes}}
            )
            result: Dict[str, Dict[str, Any]] = {}
            async for doc in cursor:
                result[doc["snapshot_hash"]] = doc
            return result
        except Exception as exc:
            if strict:
                raise
            logger.warning("批量读取装备快照失败: hashes={} error={}", snapshot_hashes, exc)
            return {}

    async def load_talent_snapshots(
        self, snapshot_hashes: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        if not snapshot_hashes:
            return {}
        db = self.db if self.db is not None else _get_db()
        try:
            cursor = db.jjc_talent_snapshot.find(
                {"snapshot_hash": {"$in": snapshot_hashes}}
            )
            result: Dict[str, Dict[str, Any]] = {}
            async for doc in cursor:
                result[doc["snapshot_hash"]] = doc
            return result
        except Exception as exc:
            logger.warning("批量读取奇穴快照失败: hashes={} error={}", snapshot_hashes, exc)
            return {}
