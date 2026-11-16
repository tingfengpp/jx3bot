from __future__ import annotations

import asyncio
import functools
import inspect
from copy import deepcopy
import datetime
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bson import BSON, ObjectId
from bson.errors import InvalidDocument
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError, ExecutionTimeout, NetworkTimeout
from nonebot import logger

from src.infra.mongo import get_db as _get_db
from src.utils.jjc_equipment_schema import content_revision
from src.utils.jjc_equipment_schema import (
    MAX_SNAPSHOT_BSON_BYTES, SOURCE, exact_legacy_guard, validate_equipment_view,
)


def _strip_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


def _to_snapshot_metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    week_info = str(doc.get("week_info") or "")
    is_settlement = "结算" in week_info
    return {
        "timestamp": doc["timestamp"],
        "generated_at": doc.get("generated_at"),
        "ranking_cache_time": doc.get("ranking_cache_time"),
        "default_week": doc.get("default_week"),
        "current_season": doc.get("current_season"),
        "week_info": doc.get("week_info"),
        "is_settlement": is_settlement,
        "snapshot_kind": "settlement" if is_settlement else "daily",
        **{key: doc[key] for key in ("schema_version", "equipment_source", "equipment_view_revision", "member_revision") if key in doc},
    }


def _normalize_season(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


class EquipmentSnapshotError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


METADATA_READ_TIMEOUT = 1.5
METADATA_CLEANUP_TIMEOUT = 0.1
METADATA_MAX_SCAN_DOCUMENTS = 512
METADATA_LEAN_BATCH_SIZE = 512
METADATA_MAX_SCAN_BYTES = 32 * 1024 * 1024


def _bounded_metadata_read(method):
    @functools.wraps(method)
    async def bounded(*args, **kwargs):
        try:
            return await asyncio.wait_for(method(*args, **kwargs), timeout=METADATA_READ_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise EquipmentSnapshotError("read_failed") from exc
    return bounded


@dataclass(frozen=True)
class JjcRankingStatsRepo:
    db: Optional[AsyncIOMotorDatabase] = None
    suppress_errors: bool = True

    @staticmethod
    def _validate_equipment_document(
        document: Dict[str, Any], timestamp: int, *, allow_historical: bool = False,
    ) -> None:
        try:
            if (not isinstance(timestamp, int) or isinstance(timestamp, bool)
                    or not 0 < timestamp <= 9223372036854775807
                    or not isinstance(document, dict) or document.get("timestamp") != timestamp
                    or type(document.get("schema_version")) is not int or document["schema_version"] != 2
                    or document.get("equipment_complete") is not True
                    or document.get("equipment_source") != SOURCE):
                raise ValueError("invalid_snapshot_metadata")
            if document.get("ranking_display_kind") == "historical" and not allow_historical:
                raise EquipmentSnapshotError("historical")
            if document.get("ranking_display_kind") not in (("current", "historical") if allow_historical else ("current",)):
                raise ValueError("invalid_snapshot_kind")
            view = validate_equipment_view(document.get("equipment_view"))
            if (view["timestamp"] != timestamp or document.get("equipment_view_revision") != view["equipment_view_revision"]
                    or document.get("member_revision") != view["member_revision"]
                    or document.get("current_season") != view["scope"]["season_id"]
                    or document.get("kungfu_cache_scope") != view["scope"]):
                raise ValueError("invalid_snapshot_binding")
            if len(BSON.encode(document)) > MAX_SNAPSHOT_BSON_BYTES:
                raise EquipmentSnapshotError("snapshot_too_large")
        except EquipmentSnapshotError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, InvalidDocument) as exc:
            raise EquipmentSnapshotError("invalid_snapshot") from exc

    async def publish_equipment_snapshot(
        self, timestamp: int, summary_payload: Dict[str, Any], *,
        expected_revision: Optional[str] = None, expected_legacy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Strict single-document publication; never use legacy error suppression.

        The service owns latest-board qualification/publication serialization.
        This repository fences one timestamp and never upgrades historical data.
        """
        document = deepcopy(summary_payload)
        self._validate_equipment_document(document, timestamp)
        if expected_revision is not None and expected_legacy is not None:
            raise EquipmentSnapshotError("conflict")
        db = self.db if self.db is not None else _get_db()
        collection = db.jjc_ranking_stat_summaries
        current = await collection.find_one({"timestamp": timestamp})
        if isinstance(current, dict) and current.get("ranking_display_kind") == "historical":
            raise EquipmentSnapshotError("historical")
        insert = expected_revision is None and expected_legacy is None
        if insert:
            if current is not None:
                raise EquipmentSnapshotError("conflict")
            document.setdefault("_id", ObjectId())
            guard = None
        elif expected_legacy is not None:
            if (not isinstance(expected_legacy, dict) or expected_legacy.get("timestamp") != timestamp
                    or expected_legacy.get("schema_version", 1) != 1
                    or expected_legacy.get("ranking_display_kind") != "current"
                    or expected_legacy.get("current_season") != document["current_season"]
                    or expected_legacy.get("kungfu_cache_scope") != document["kungfu_cache_scope"]):
                raise EquipmentSnapshotError("invalid_snapshot")
            if current is None or current.get("schema_version", 1) != 1:
                raise EquipmentSnapshotError("conflict")
            try:
                guard = exact_legacy_guard(expected_legacy)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                raise EquipmentSnapshotError("invalid_snapshot") from exc
            document["_id"] = expected_legacy["_id"]
        else:
            if (not isinstance(expected_revision, str) or not expected_revision or current is None
                    or current.get("schema_version") != 2
                    or current.get("equipment_view_revision") != expected_revision):
                raise EquipmentSnapshotError("conflict")
            self._validate_equipment_document(current, timestamp)
            guard = {"_id": current["_id"], "schema_version": 2,
                     "equipment_view_revision": expected_revision,
                     "member_revision": current["member_revision"]}
            document["_id"] = current["_id"]
        document["published_at"] = datetime.datetime.now(datetime.timezone.utc)
        # BSON's millisecond datetime precision is the actual persisted value.
        # Normalize once so a lost acknowledgement can compare an exact readback.
        document = BSON.encode(document).decode()
        self._validate_equipment_document(document, timestamp)
        try:
            if insert:
                result = await collection.insert_one(document)
            else:
                result = await collection.replace_one(guard, document, upsert=False)
        except DuplicateKeyError as exc:
            raise EquipmentSnapshotError("conflict") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                observed = await collection.find_one({"timestamp": timestamp})
                self._validate_equipment_document(observed, timestamp)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise EquipmentSnapshotError("publish_unknown") from exc
            if observed != document:
                raise EquipmentSnapshotError("publish_unknown") from exc
            return {"status": "published", "revision": document["equipment_view_revision"], "recovered": True}
        if getattr(result, "acknowledged", True) is not True:
            raise EquipmentSnapshotError("publish_unknown")
        if not insert and result.matched_count != 1:
            raise EquipmentSnapshotError("conflict")
        return {"status": "published", "revision": document["equipment_view_revision"], "recovered": False}

    async def load_complete_view(
        self, timestamp: int, *, expected_view_revision: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not 0 < timestamp <= 9223372036854775807:
            raise EquipmentSnapshotError("invalid_snapshot")
        db = self.db if self.db is not None else _get_db()
        document = await db.jjc_ranking_stat_summaries.find_one({"timestamp": timestamp})
        if document is None or document.get("schema_version", 1) == 1:
            return None
        self._validate_equipment_document(document, timestamp, allow_historical=True)
        if expected_view_revision is not None and expected_view_revision != document["equipment_view_revision"]:
            raise EquipmentSnapshotError("version_mismatch")
        return deepcopy(document["equipment_view"])

    async def load_frozen_equipment_context(self, timestamp: int) -> Optional[Dict[str, Any]]:
        """Strict same-document bootstrap; legacy rosters must prove completeness."""
        db = self.db if self.db is not None else _get_db()
        summary = await db.jjc_ranking_stat_summaries.find_one({"timestamp": timestamp}, max_time_ms=1500)
        if summary is None:
            return None
        context = {"timestamp": timestamp, "summary": deepcopy(summary), "complete": False,
                   "scope": deepcopy(summary.get("kungfu_cache_scope")),
                   "ranking_display_kind": summary.get("ranking_display_kind"),
                   "frozen_source": "legacy_v1", "member_revision": None, "members": [], "frozen_view": None}
        if summary.get("schema_version", 1) == 2:
            self._validate_equipment_document(summary, timestamp, allow_historical=True)
            view = summary["equipment_view"]
            return {**context, "complete": True, "scope": deepcopy(view["scope"]),
                    "frozen_source": SOURCE, "member_revision": view["member_revision"],
                    "members": deepcopy(view["members"]), "frozen_view": deepcopy(view)}
        scope = context["scope"]
        if (not isinstance(scope, dict) or scope.get("season_id") != summary.get("current_season")
                or scope.get("pvp_type") != 3 or not scope.get("season_start")
                or summary.get("ranking_display_kind") != "current"):
            return context
        stats = summary.get("kungfu_statistics") or {}
        limits = {"top_1000": 1000, "top_200": 200, "top_100": 100, "top_50": 50}
        range_key = next((key for key in limits if isinstance(stats.get(key), dict)), None)
        if not range_key:
            return context
        expected = stats[range_key].get("total_players")
        if type(expected) is not int or not 0 < expected <= limits[range_key]:
            return context
        # Legacy rows may contain large unrelated upstream payloads. Only the
        # frozen identity/classification contract is needed for V2 conversion;
        # old detail/flat readers retain their existing complete projection.
        frozen_fields = (
            "rank", "name", "server", "score", "kungfu", "kungfu_id", "lane", "found",
            "global_id", "display_identity_id", "identity_id", "display_locator",
            "binding_revision", "display_scope", "kungfu_selected_source",
            "game_role_id", "global_role_id", "role_id", "zone",
            "kungfu_acquisition", "kungfu_publish_version", "kungfu_verified_at",
            "kungfu_source", "kungfu_freshness", "kungfu_unknown_reason",
        )
        docs = await db.jjc_ranking_stat_details.find({"timestamp": timestamp, "range": range_key},
            {"_id": 0, "lane": 1, "kungfu": 1,
             **{"members." + field: 1 for field in frozen_fields}}).max_time_ms(1500).to_list(None)
        members, seen = [], set()
        for doc in docs:
            for raw in doc.get("members") or []:
                if not isinstance(raw, dict):
                    return context
                member = deepcopy(raw)
                rank = member.get("rank")
                if type(rank) is not int or not 0 < rank <= expected or rank in seen:
                    return context
                seen.add(rank)
                member["lane"] = doc.get("lane") if doc.get("lane") in ("healer", "dps") else "unknown"
                member.setdefault("kungfu", doc.get("kungfu") if member["lane"] != "unknown" else "未知")
                member["binding_revision"] = content_revision({"scope": scope, **{key: member.get(key) for key in
                    ("rank", "global_id", "display_identity_id", "identity_id", "display_locator")}})
                members.append(member)
        if seen != set(range(1, expected+1)):
            return context
        # Legacy writers replace summary before details. Detect a concurrent
        # change and ask the caller to retry rather than mix known versions.
        observed = await db.jjc_ranking_stat_summaries.find_one({"timestamp": timestamp}, max_time_ms=1500)
        if observed != summary:
            raise EquipmentSnapshotError("version_mismatch")
        members.sort(key=lambda row: row["rank"])
        revision = content_revision({"scope": scope, "members": [{key: row.get(key) for key in
            ("rank", "name", "server", "score", "lane", "kungfu", "kungfu_id", "global_id", "display_identity_id", "identity_id", "display_locator")}
            for row in members]})
        return {**context, "complete": True, "members": members, "member_revision": revision}

    def _handle_error(self, message: str, exc: Exception) -> None:
        logger.warning(message)
        if not self.suppress_errors:
            raise exc

    # ------------------------------------------------------------------
    # save_snapshot – batch upsert entry point for service code
    # ------------------------------------------------------------------

    async def save_snapshot(
        self,
        timestamp: int,
        summary_payload: Dict[str, Any],
        detail_payloads: List[Dict[str, Any]],
        source: str = "ranking_job",
    ) -> None:
        await self.upsert_summary(timestamp, summary_payload, source=source)
        for detail in detail_payloads:
            await self.upsert_detail(
                timestamp,
                detail["range"],
                detail["lane"],
                detail["kungfu"],
                detail.get("members", []),
                source=source,
            )

    # ------------------------------------------------------------------
    # upsert helpers – usable by migration scripts too
    # ------------------------------------------------------------------

    async def upsert_summary(
        self,
        timestamp: int,
        payload: Dict[str, Any],
        source: str = "ranking_job",
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        doc = {
            "timestamp": timestamp,
            "generated_at": payload.get("generated_at"),
            "ranking_cache_time": payload.get("ranking_cache_time"),
            "default_week": payload.get("default_week"),
            "current_season": payload.get("current_season"),
            "week_info": payload.get("week_info"),
            "kungfu_statistics": payload.get("kungfu_statistics", {}),
            "source": source,
            "schema_version": 1,
            "updated_at": now,
        }
        for key in ("kungfu_cache_scope", "refresh_degraded", "ranking_display_kind"):
            if key in payload:
                doc[key] = payload[key]
        try:
            db = self.db if self.db is not None else _get_db()
            existing = await db.jjc_ranking_stat_summaries.find_one({"timestamp": timestamp}, {"schema_version": 1})
            if isinstance(existing, dict) and existing.get("schema_version") == 2:
                raise EquipmentSnapshotError("conflict")
            await db.jjc_ranking_stat_summaries.update_one(
                {"timestamp": timestamp, "schema_version": {"$ne": 2}},
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except EquipmentSnapshotError:
            raise
        except DuplicateKeyError as exc:
            # An upgrade between pre-read and upsert cannot be overwritten.
            raise EquipmentSnapshotError("conflict") from exc
        except Exception as exc:
            self._handle_error(
                "upsert jjc_ranking_stat_summaries 失败: timestamp={} error={}".format(
                    timestamp,
                    exc,
                ),
                exc,
            )

    async def upsert_detail(
        self,
        timestamp: int,
        range_key: str,
        lane: str,
        kungfu: str,
        members: List[Dict[str, Any]],
        source: str = "ranking_job",
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        doc = {
            "timestamp": timestamp,
            "range": range_key,
            "lane": lane,
            "kungfu": kungfu,
            "members": members,
            "source": source,
            "schema_version": 1,
            "updated_at": now,
        }
        try:
            db = self.db if self.db is not None else _get_db()
            await db.jjc_ranking_stat_details.update_one(
                {"timestamp": timestamp, "range": range_key, "lane": lane, "kungfu": kungfu},
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except Exception as exc:
            self._handle_error(
                "upsert jjc_ranking_stat_details 失败: "
                "timestamp={} range={} lane={} kungfu={} error={}".format(
                    timestamp,
                    range_key,
                    lane,
                    kungfu,
                    exc,
                ),
                exc,
            )

    # ------------------------------------------------------------------
    # load methods
    # ------------------------------------------------------------------

    async def load_publication_baseline(self, timestamp: int) -> Optional[Dict[str, Any]]:
        """Capture the exact original BSON document once, including its _id.

        This strict internal read is distinct from the public legacy summary
        projection; a second read to recover _id would weaken the CAS baseline.
        """
        db = self.db if self.db is not None else _get_db()
        return await db.jjc_ranking_stat_summaries.find_one(
            {"timestamp": timestamp}, max_time_ms=1500)

    async def load_summary(self, timestamp: int) -> Optional[Dict[str, Any]]:
        try:
            db = self.db if self.db is not None else _get_db()
            doc = await db.jjc_ranking_stat_summaries.find_one({"timestamp": timestamp})
            return _strip_id(doc)
        except Exception as exc:
            self._handle_error(
                "load jjc_ranking_stat_summaries 失败: timestamp={} error={}".format(
                    timestamp,
                    exc,
                ),
                exc,
            )
            return None

    async def load_detail(
        self,
        timestamp: int,
        range_key: str,
        lane: str,
        kungfu: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            db = self.db if self.db is not None else _get_db()
            doc = await db.jjc_ranking_stat_details.find_one(
                {
                    "timestamp": timestamp,
                    "range": range_key,
                    "lane": lane,
                    "kungfu": kungfu,
                }
            )
            return _strip_id(doc)
        except Exception as exc:
            self._handle_error(
                "load jjc_ranking_stat_details 失败: "
                "timestamp={} range={} lane={} kungfu={} error={}".format(
                    timestamp,
                    range_key,
                    lane,
                    kungfu,
                    exc,
                ),
                exc,
            )
            return None

    async def list_flat_members(
        self,
        timestamp: int,
        range_key: str,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        safe_limit = max(0, int(limit or 0))
        started_at = time.perf_counter()
        try:
            db = self.db if self.db is not None else _get_db()
            detail_query = {"timestamp": timestamp, "range": range_key}
            detail_count = 0
            count_documents = getattr(db.jjc_ranking_stat_details, "count_documents", None)
            if callable(count_documents):
                try:
                    detail_count = int(await count_documents(detail_query))
                except Exception:
                    detail_count = 0
            items: List[Dict[str, Any]] = []
            total = 0

            try:
                aggregate_started_at = time.perf_counter()
                items_pipeline = [
                    {"$project": {"_id": 0, "lane": 1, "kungfu": 1, "members": 1}},
                    {"$unwind": "$members"},
                    {
                        "$replaceRoot": {
                            "newRoot": {
                                "$mergeObjects": [
                                    "$members",
                                    {
                                        "lane": "$lane",
                                        "kungfu": "$kungfu",
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$sort": {
                            "rank": 1,
                            "server": 1,
                            "name": 1,
                        }
                    },
                ]
                if safe_limit:
                    items_pipeline.append({"$limit": safe_limit})

                cursor = db.jjc_ranking_stat_details.aggregate(
                    [
                        {"$match": detail_query},
                        {
                            "$facet": {
                                "items": items_pipeline,
                                "total": [
                                    {"$project": {"_id": 0, "members": 1}},
                                    {"$unwind": "$members"},
                                    {"$count": "value"},
                                ],
                            }
                        },
                    ],
                    allowDiskUse=True,
                )
                docs = await cursor.to_list(length=1)
                facet_doc = docs[0] if docs else {}
                raw_items = facet_doc.get("items") or []
                if isinstance(raw_items, list):
                    for doc in raw_items:
                        if isinstance(doc, dict):
                            items.append(doc)
                total_docs = facet_doc.get("total") or []
                if isinstance(total_docs, list) and total_docs:
                    total = int(total_docs[0].get("value") or 0)
                else:
                    total = len(items)
                aggregate_elapsed_ms = int((time.perf_counter() - aggregate_started_at) * 1000)
                logger.info(
                    "JJC flat-members aggregate done: timestamp={} range={} limit={} detail_count={} total={} item_count={} elapsed_ms={}".format(
                        timestamp,
                        range_key,
                        safe_limit,
                        detail_count,
                        total,
                        len(items),
                        aggregate_elapsed_ms,
                    )
                )
            except Exception:
                fallback_started_at = time.perf_counter()
                cursor = db.jjc_ranking_stat_details.find(
                    detail_query,
                    {"_id": 0, "lane": 1, "kungfu": 1, "members": 1},
                )
                async for doc in cursor:
                    lane = doc.get("lane")
                    detail_kungfu = doc.get("kungfu")
                    members = doc.get("members") or []
                    if not isinstance(members, list):
                        continue
                    for member in members:
                        if not isinstance(member, dict):
                            continue
                        item = dict(member)
                        item["lane"] = item.get("lane") or lane
                        item["kungfu"] = item.get("kungfu") or detail_kungfu
                        items.append(item)

                def rank_key(item: Dict[str, Any]) -> Any:
                    rank = item.get("rank")
                    try:
                        return int(rank)
                    except (TypeError, ValueError):
                        return 999999

                items.sort(key=lambda item: (rank_key(item), str(item.get("server") or ""), str(item.get("name") or "")))
                total = len(items)
                if safe_limit:
                    items = items[:safe_limit]
                fallback_elapsed_ms = int((time.perf_counter() - fallback_started_at) * 1000)
                logger.info(
                    "JJC flat-members fallback done: timestamp={} range={} limit={} detail_count={} total={} item_count={} elapsed_ms={}".format(
                        timestamp,
                        range_key,
                        safe_limit,
                        detail_count,
                        total,
                        len(items),
                        fallback_elapsed_ms,
                    )
                )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "JJC flat-members request done: timestamp={} range={} limit={} elapsed_ms={} total={} item_count={} detail_count={}".format(
                    timestamp,
                    range_key,
                    safe_limit,
                    elapsed_ms,
                    total,
                    len(items),
                    detail_count,
                )
            )
            return {
                "timestamp": timestamp,
                "range": range_key,
                "items": items,
                "item_count": len(items),
                "total": total,
                "detail_count": detail_count,
            }
        except Exception as exc:
            self._handle_error(
                "list jjc_ranking_stat_details flat members 失败: timestamp={} range={} error={}".format(
                    timestamp,
                    range_key,
                    exc,
                ),
                exc,
            )
            return {
                "timestamp": timestamp,
                "range": range_key,
                "items": [],
                "item_count": 0,
                "total": 0,
                "detail_count": 0,
            }

    # ------------------------------------------------------------------
    # list timestamps
    # ------------------------------------------------------------------

    async def _close_metadata_cursor(self, cursor):
        close = getattr(cursor, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    # Bounds the async close wait. Motor may still have an
                    # already submitted operation in its executor; this does
                    # not assert underlying thread/socket termination.
                    await asyncio.wait_for(result, timeout=METADATA_CLEANUP_TIMEOUT)
            except Exception as exc:
                raise EquipmentSnapshotError("read_failed") from exc

    async def _metadata_rows(self, cursor, *, batch_size=16):
        """Bound every metadata cursor and close on success, fault or cancellation."""
        started = time.monotonic()
        rows = []
        size = 0
        try:
            if type(batch_size) is not int or not 1 <= batch_size <= METADATA_LEAN_BATCH_SIZE:
                raise EquipmentSnapshotError("read_failed")
            if callable(getattr(cursor, "max_time_ms", None)):
                cursor = cursor.max_time_ms(1500)
            if callable(getattr(cursor, "batch_size", None)):
                cursor = cursor.batch_size(batch_size)
            async for doc in cursor:
                if time.monotonic() - started > METADATA_READ_TIMEOUT:
                    raise EquipmentSnapshotError("read_failed")
                size += len(BSON.encode(doc))
                if len(rows) >= METADATA_MAX_SCAN_DOCUMENTS or size > METADATA_MAX_SCAN_BYTES:
                    raise EquipmentSnapshotError("read_failed")
                rows.append(doc)
            return rows
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EquipmentSnapshotError("read_failed") from exc
        finally:
            await self._close_metadata_cursor(cursor)

    async def _readable_snapshot_query(self, db, *, season_values=None) -> Dict[str, Any]:
        """Validate bounded V2 payloads before exposing metadata/counts."""
        query = {"schema_version": 2}
        if season_values is not None:
            query["current_season"] = {"$in": season_values}
        timestamps = []
        started = time.monotonic()
        for doc in await self._metadata_rows(db.jjc_ranking_stat_summaries.find(query)):
            if time.monotonic() - started > METADATA_READ_TIMEOUT:
                raise EquipmentSnapshotError("read_failed")
            if doc.get("schema_version", 1) != 2:
                continue
            try:
                self._validate_equipment_document(doc, doc.get("timestamp"), allow_historical=True)
            except EquipmentSnapshotError:
                continue
            if time.monotonic() - started > METADATA_READ_TIMEOUT:
                raise EquipmentSnapshotError("read_failed")
            timestamps.append(doc["timestamp"])
        if time.monotonic() - started > METADATA_READ_TIMEOUT:
            raise EquipmentSnapshotError("read_failed")
        return {"$or": [{"schema_version": {"$exists": False}}, {"schema_version": 1},
                        {"schema_version": 2, "timestamp": {"$in": timestamps}}]}

    @_bounded_metadata_read
    async def list_seasons(self) -> Dict[str, Any]:
        try:
            db = self.db if self.db is not None else _get_db()
            readable = await self._readable_snapshot_query(db)
            cursor = db.jjc_ranking_stat_summaries.find(
                readable,
                {"current_season": 1, "timestamp": 1},
            ).sort([("timestamp", -1)])
            latest_timestamps: Dict[str, int] = {}
            for doc in await self._metadata_rows(cursor, batch_size=METADATA_LEAN_BATCH_SIZE):
                season = _normalize_season(doc.get("current_season"))
                timestamp = int(doc.get("timestamp") or 0)
                if season and timestamp > latest_timestamps.get(season, 0):
                    latest_timestamps[season] = timestamp
            seasons = [
                {"name": name, "latest_timestamp": timestamp}
                for name, timestamp in latest_timestamps.items()
            ]
            seasons.sort(key=lambda item: item["latest_timestamp"], reverse=True)
            return {
                "seasons": seasons,
                "default_season": seasons[0]["name"] if seasons else None,
            }
        except EquipmentSnapshotError:
            raise
        except (ExecutionTimeout, NetworkTimeout) as exc:
            raise EquipmentSnapshotError("read_failed") from exc
        except Exception as exc:
            self._handle_error("list jjc ranking seasons 失败: error={}".format(exc), exc)
            return {"seasons": [], "default_season": None}

    @_bounded_metadata_read
    async def list_season_history(self, season: str) -> Dict[str, Any]:
        season = str(season or "").strip()
        if not season:
            return {"season": season, "items": [], "total": 0}
        try:
            db = self.db if self.db is not None else _get_db()
            season_values: List[Any] = [season]
            try:
                numeric_season = int(season)
            except ValueError:
                numeric_season = None
            if numeric_season is not None and str(numeric_season) == season:
                season_values.append(numeric_season)
            readable = await self._readable_snapshot_query(db, season_values=season_values)
            projection = {
                "timestamp": 1,
                "generated_at": 1,
                "ranking_cache_time": 1,
                "default_week": 1,
                "current_season": 1,
                "week_info": 1,
                "schema_version": 1, "equipment_source": 1,
                "equipment_view_revision": 1, "member_revision": 1,
            }
            cursor = db.jjc_ranking_stat_summaries.find(
                {"$and": [readable, {"current_season": {"$in": season_values}}]}, projection
            ).sort([("timestamp", -1)])
            items: List[Dict[str, Any]] = []
            for doc in await self._metadata_rows(cursor, batch_size=METADATA_LEAN_BATCH_SIZE):
                if "timestamp" in doc:
                    items.append(_to_snapshot_metadata(doc))
            return {"season": season, "items": items, "total": len(items)}
        except EquipmentSnapshotError:
            raise
        except (ExecutionTimeout, NetworkTimeout) as exc:
            raise EquipmentSnapshotError("read_failed") from exc
        except Exception as exc:
            self._handle_error(
                "list jjc ranking season history 失败: season={} error={}".format(season, exc),
                exc,
            )
            return {"season": season, "items": [], "total": 0}

    @_bounded_metadata_read
    async def list_timestamps(
        self,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        with_meta: bool = False,
    ) -> Any:
        sort_order = [("timestamp", -1)]

        if with_meta:
            is_paged = True
            normalized_page = max(int(page or 1), 1)
            normalized_page_size = max(min(int(page_size or 100), 100), 1)
        else:
            is_paged = page is not None or page_size is not None

        try:
            db = self.db if self.db is not None else _get_db()
            readable = await self._readable_snapshot_query(db)

            if with_meta:
                normalized_page = max(int(page or 1), 1)
                normalized_page_size = max(min(int(page_size or 100), 100), 1)
                total = await db.jjc_ranking_stat_summaries.count_documents(readable, maxTimeMS=1500)
                skip = max(0, (normalized_page - 1) * normalized_page_size)
                projection = {
                    "timestamp": 1,
                    "generated_at": 1,
                    "ranking_cache_time": 1,
                    "default_week": 1,
                    "current_season": 1,
                    "week_info": 1,
                    "schema_version": 1, "equipment_source": 1,
                    "equipment_view_revision": 1, "member_revision": 1,
                }
                cursor = (
                    db.jjc_ranking_stat_summaries.find(readable, projection)
                    .sort(sort_order)
                    .skip(skip)
                    .limit(normalized_page_size)
                )
                items: List[Dict[str, Any]] = []
                for doc in await self._metadata_rows(cursor, batch_size=METADATA_LEAN_BATCH_SIZE):
                    items.append(_to_snapshot_metadata(doc))
                has_more = (skip + normalized_page_size) < total
                return {
                    "items": items,
                    "page": normalized_page,
                    "page_size": normalized_page_size,
                    "total": total,
                    "has_more": has_more,
                }

            if not is_paged:
                cursor = db.jjc_ranking_stat_summaries.find(
                    readable, {"timestamp": 1}
                ).sort(sort_order)
                result: List[int] = []
                for doc in await self._metadata_rows(cursor, batch_size=METADATA_LEAN_BATCH_SIZE):
                    result.append(doc["timestamp"])
                return result

            normalized_page = max(int(page or 1), 1)
            normalized_page_size = max(min(int(page_size or 20), 100), 1)
            total = await db.jjc_ranking_stat_summaries.count_documents(readable, maxTimeMS=1500)
            skip = max(0, (normalized_page - 1) * normalized_page_size)
            cursor = (
                db.jjc_ranking_stat_summaries.find(readable, {"timestamp": 1})
                .sort(sort_order)
                .skip(skip)
                .limit(normalized_page_size)
            )
            items: List[int] = []
            for doc in await self._metadata_rows(cursor, batch_size=METADATA_LEAN_BATCH_SIZE):
                items.append(doc["timestamp"])
            has_more = (skip + normalized_page_size) < total
            return {
                "items": items,
                "page": normalized_page,
                "page_size": normalized_page_size,
                "total": total,
                "has_more": has_more,
            }
        except EquipmentSnapshotError:
            raise
        except (ExecutionTimeout, NetworkTimeout) as exc:
            raise EquipmentSnapshotError("read_failed") from exc
        except Exception as exc:
            self._handle_error(
                "list jjc_ranking_stat_summaries 失败: error={}".format(exc),
                exc,
            )
            if with_meta:
                return {
                    "items": [],
                    "page": page or 1,
                    "page_size": page_size or 100,
                    "total": 0,
                    "has_more": False,
                }
            if not is_paged:
                return []
            return {
                "items": [],
                "page": page or 1,
                "page_size": page_size or 20,
                "total": 0,
                "has_more": False,
            }

    async def get_latest_current_summary(self, scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find the latest readable board without leaving a server cursor open."""
        db = self.db if self.db is not None else _get_db()
        base = {"current_season": scope["season_id"], "ranking_display_kind": {"$ne": "historical"}}
        rejected, rejected_keys = [], set()
        spent_io_ms = 0.0
        while True:
            remaining_ms = int(1500 - spent_io_ms)
            if remaining_ms <= 0:
                # Exhaustion is a failed read, never evidence of EOF. Wall-clock
                # IO is a conservative upper bound on cumulative Mongo work.
                raise EquipmentSnapshotError("read_failed")
            query = dict(base)
            if rejected:
                query["_id"] = {"$nin": list(rejected)}
            started = time.monotonic()
            # PyMongo find_one uses limit(-1): one batch and no server cursor
            # that would need a later killCursors within the cleanup budget.
            doc = await db.jjc_ranking_stat_summaries.find_one(
                query, sort=[("timestamp", -1)], max_time_ms=remaining_ms)
            spent_io_ms += (time.monotonic() - started) * 1000
            if doc is None:
                return None
            version = doc.get("schema_version", 1)
            readable = version == 1
            if version == 2:
                try:
                    self._validate_equipment_document(doc, doc.get("timestamp"))
                    readable = True
                except EquipmentSnapshotError:
                    pass
            if readable:
                return {key: deepcopy(doc[key]) for key in (
                    "timestamp", "current_season", "ranking_display_kind", "kungfu_cache_scope",
                    "schema_version", "equipment_source", "equipment_view_revision", "member_revision",
                ) if key in doc}
            if "_id" not in doc:
                raise EquipmentSnapshotError("read_failed")
            source_key = BSON.encode({"_id": doc["_id"]})
            if source_key in rejected_keys:
                raise EquipmentSnapshotError("read_failed")
            rejected_keys.add(source_key)
            # Exclude only sources actually proven unreadable. Mongo retains
            # its own timestamp sorting across mixed BSON/missing/array values.
            rejected.append(doc["_id"])

    async def load_display_members(self, timestamp: int, range_key: str, ranks: List[int]) -> List[Dict[str, Any]]:
        """One indexed snapshot/range read, including the unknown lane; errors propagate."""
        db = self.db if self.db is not None else _get_db()
        fields = ("rank", "kungfu_acquisition", "kungfu_publish_version",
                  "display_identity_id", "display_locator", "display_scope")
        pipeline = [
            {"$match": {"timestamp": timestamp, "range": range_key}},
            {"$project": {"_id": 0, "members": {"$filter": {
                "input": {"$ifNull": ["$members", []]}, "as": "member",
                "cond": {"$in": ["$$member.rank", ranks]}}}}},
            {"$unwind": "$members"},
            {"$replaceRoot": {"newRoot": "$members"}},
            {"$project": {"_id": 0, **{field: 1 for field in fields}}},
        ]
        return await db.jjc_ranking_stat_details.aggregate(pipeline).to_list(None)
