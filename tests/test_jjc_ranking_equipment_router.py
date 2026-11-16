"""V2 actual FastAPI/HTTPX contracts with real injected equipment service.

Run separately from legacy modules which replace FastAPI/sys.modules. Repository
and clock assembly is replaced; selection, aggregation, service, HTTP and public
serialization execute their real implementations. No bot or database is started.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import config
from src.services.jx3.jjc_ranking_equipment import aggregate_equipment_view
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from src.storage.mongo_repos.jjc_ranking_stats_repo import EquipmentSnapshotError
from tests.jjc_equipment_fixtures import (
    CHECKED_AT, MEMBER_REVISION, SCOPE, TIMESTAMP, match_doc, ten_member_golden,
)
from tests.test_jjc_ranking_equipment_view import Clock, FakeIdentities, FakeInspect, FakeSnapshots


class RouteStats:
    def __init__(self):
        self.view = aggregate_equipment_view(timestamp=TIMESTAMP, scope=SCOPE,
            members=ten_member_golden(), checked_at=CHECKED_AT-10,
            completed_at=CHECKED_AT-9, member_revision=MEMBER_REVISION)
        self.summary = {"timestamp": TIMESTAMP, "schema_version": 2,
            "current_season": SCOPE["season_id"], "ranking_display_kind": "current",
            "kungfu_cache_scope": deepcopy(SCOPE), "equipment_source": "local_win_v2",
            "equipment_complete": True, "member_revision": MEMBER_REVISION,
            "equipment_view_revision": self.view["equipment_view_revision"],
            "equipment_view": deepcopy(self.view), "generated_at": CHECKED_AT-10,
            "kungfu_statistics": {}}
        self.calls = []
        self.latest = TIMESTAMP
        self.load_detail = AsyncMock(return_value={"members": [{"rank": 999, "name": "legacy-row-must-not-be-used"}]})
        self.list_flat_members = AsyncMock(return_value={"items": [{"rank": 999, "name": "legacy-row-must-not-be-used"}], "total": 1})

    async def load_summary(self, timestamp):
        self.calls.append(("summary", timestamp))
        return deepcopy(self.summary) if timestamp == TIMESTAMP else None

    async def load_complete_view(self, timestamp, *, expected_view_revision=None):
        self.calls.append(("complete", timestamp, expected_view_revision))
        if timestamp != TIMESTAMP:
            return None
        if expected_view_revision is not None and expected_view_revision != self.view["equipment_view_revision"]:
            raise EquipmentSnapshotError("version_mismatch")
        return deepcopy(self.view)

    async def load_frozen_equipment_context(self, timestamp):
        self.calls.append(("context", timestamp))
        if timestamp != TIMESTAMP:
            return None
        return {"timestamp": TIMESTAMP, "scope": deepcopy(SCOPE), "summary": deepcopy(self.summary),
            "member_revision": self.summary["member_revision"],
            "frozen_source": self.summary.get("equipment_source", "legacy_v1"),
            "members": deepcopy(self.view["members"]), "frozen_view": deepcopy(self.view),
            "ranking_display_kind": self.summary["ranking_display_kind"], "complete": True}

    async def get_latest_current_summary(self, scope):
        self.calls.append(("latest", deepcopy(scope)))
        return {"timestamp": self.latest, "current_season": scope["season_id"],
            "ranking_display_kind": "current", "kungfu_cache_scope": deepcopy(scope)}


class EquipmentRouterAsgiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).resolve().parents[1]
        self.stats = RouteStats()
        self.clock = Clock()
        self.identities = FakeIdentities(self.stats.view["members"])
        self.inspect = FakeInspect()
        self.snapshots = FakeSnapshots()
        rows = []
        for member in self.stats.view["members"]:
            state = member["local_win_packet"]["equipment_state"]
            row = match_doc(200+member["rank"], equipment=state if state != "unknown" else None)
            players = [p for key in ("team1", "team2")
                       for p in row["data"]["detail"][key]["players_info"]]
            for index, player in enumerate(players):
                player["global_id"] = str(800000000000000000 + member["rank"]*10 + index)
            players[0]["global_id"] = member["global_id"]
            rows.append(row)
        self.inspect.rows = rows
        self.service = JjcRankingEquipmentViewService(stats_repo=self.stats,
            identity_repo=self.identities, inspect_repo=self.inspect, snapshot_repo=self.snapshots,
            scope=deepcopy(SCOPE), enabled=True, overlay_enabled=True,
            clock=self.clock.wall, monotonic=self.clock.monotonic)
        self.addAsyncCleanup(self.service.close)
        self.legacy_display = types.SimpleNamespace(read=AsyncMock(
            side_effect=AssertionError("V2 must not call the legacy single-card reader")))
        singletons = types.ModuleType("src.services.jx3.singletons")
        singletons.jjc_ranking_inspect_service = types.SimpleNamespace()
        singletons.jjc_ranking_display_service = self.legacy_display
        singletons.jjc_ranking_equipment_service = self.service
        singletons.jjc_ranking_equipment_view_service = self.service
        api = types.ModuleType("src.api")
        api.__path__ = [str(root / "src/api")]
        self.modules = patch.dict(sys.modules,
            {"src.services.jx3.singletons": singletons, "src.api": api})
        self.modules.start(); self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("_equipment_actual_asgi_router",
                                                     root / "src/api/routers/jjc_ranking_stats.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.repo_patch = patch.object(self.module, "JjcRankingStatsRepo", return_value=self.stats)
        self.repo_patch.start(); self.addCleanup(self.repo_patch.stop)
        self.flag_patch = patch.object(config, "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", 1, create=True)
        self.flag_patch.start(); self.addCleanup(self.flag_patch.stop)
        self.overlay_patch = patch.object(config, "JJC_RANKING_DISPLAY_OVERLAY_ENABLED", 1, create=True)
        self.overlay_patch.start(); self.addCleanup(self.overlay_patch.stop)
        app = FastAPI(); app.include_router(self.module.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")
        self.addAsyncCleanup(self.client.aclose)

    async def test_metadata_read_failure_is_503_envelope_in_all_list_modes(self):
        for action, params in (("seasons", {}), ("season-history", {"season": "synthetic"}),
                ("list", {}), ("list", {"with_meta": "true"}), ("list", {"page": 1})):
            repo = types.SimpleNamespace(**{name: AsyncMock(side_effect=EquipmentSnapshotError("read_failed"))
                for name in ("list_seasons", "list_season_history", "list_timestamps")})
            with self.subTest(action=action,params=params), patch.object(self.service,"snapshot_read_repo",return_value=repo):
                response=await self.client.get('/api/jjc/ranking-stats',params={"action":action,**params})
                self.assertEqual(503,response.status_code)
                self.assertEqual('read_failed',response.json()['status_msg'])

    def dynamic_params(self, **overrides):
        return {"timestamp": str(TIMESTAMP), "equipment_contract": "2",
            "expected_member_revision": MEMBER_REVISION,
            "expected_frozen_source": "local_win_v2", **overrides}

    async def dynamic(self, **overrides):
        return await self.client.get("/api/jjc/ranking-stats/equipment-view",
                                     params=self.dynamic_params(**overrides))

    def assert_envelope(self, response, status=200, *, success=True):
        self.assertEqual(status, response.status_code)
        body = response.json()
        self.assertEqual({"status_code", "status_msg", "data"}, set(body))
        self.assertEqual(body["status_code"] == 0, success)
        return body["data"]

    async def test_tc49_dynamic_endpoint_uses_real_whole_board_selection_and_aggregation(self):
        response = await self.dynamic()
        data = self.assert_envelope(response)
        metrics = data["view"]["ranges"]["top_1000"]
        self.assertEqual((10, 2, 5, 3), tuple(metrics[k] for k in ("N", "L", "P", "U")))
        self.assertEqual(.2, metrics["legendary_ratio"])
        self.assertEqual(.7, metrics["equipment_coverage"])
        self.assertEqual([3, 4, 5, 6, 7], metrics["purple_ranks"])
        self.assertTrue(self.inspect.calls, "The router must execute the actual injected service")
        self.assertNotIn("display_locator", response.text)
        self.assertNotIn("identity_id", response.text)
        self.assertNotIn("global_id", response.text)

    async def test_tc39_missing_and_malformed_parameters_return_400_envelopes_before_reads(self):
        base = self.dynamic_params()
        inputs = [{k: v for k, v in base.items() if k != omitted} for omitted in base]
        inputs += [self.dynamic_params(**changes) for changes in (
            {"timestamp": "²"}, {"timestamp": "0"}, {"timestamp": "-1"},
            {"timestamp": "9"*4301}, {"timestamp": "9223372036854775808"},
            {"equipment_contract": "1"}, {"equipment_contract": "2.0"},
            {"expected_member_revision": ""}, {"expected_member_revision": "x"*5000},
            {"expected_frozen_source": "arbitrary-source"}, {"global_id": "700000000000000001"},
            {"scope": "arbitrary-scope"})]
        for params in inputs:
            with self.subTest(params={k: str(v)[:40] for k, v in params.items()}):
                response = await self.client.get("/api/jjc/ranking-stats/equipment-view", params=params)
                self.assert_envelope(response, 400, success=False)
        self.assertEqual([], self.stats.calls)
        self.assertEqual([], self.inspect.calls)

    async def test_tc31_disabled_dynamic_is_200_without_accessing_repositories(self):
        self.service.overlay_enabled = False
        config.JJC_RANKING_DISPLAY_OVERLAY_ENABLED = 0
        data = self.assert_envelope(await self.dynamic())
        self.assertFalse(data["overlay_enabled"])
        self.assertIsNone(data["view"])
        self.assertEqual([], self.stats.calls)
        self.assertEqual([], self.inspect.calls)

    async def test_tc33_manifest_mismatch_is_reload_response_not_other_members_equipment(self):
        response = await self.dynamic(expected_member_revision="old-members")
        data = self.assert_envelope(response)
        self.assertTrue(data["reload_required"])
        self.assertFalse(data["retired"])
        self.assertIsNone(data["view"])
        self.assertEqual(MEMBER_REVISION, data["current_member_revision"])
        self.assertEqual([], self.inspect.calls)

    async def test_tc34_retired_is_200_with_latest_timestamp_and_no_dynamic_view(self):
        self.stats.latest = TIMESTAMP+100
        data = self.assert_envelope(await self.dynamic())
        self.assertTrue(data["retired"])
        self.assertEqual(TIMESTAMP+100, data["latest_timestamp"])
        self.assertIsNone(data["view"])
        self.assertEqual([], self.inspect.calls)

    async def test_tc35_read_failure_is_503_and_does_not_leak_private_exception(self):
        self.inspect.error = OSError("private-server-and-credentials-must-not-escape")
        response = await self.dynamic()
        self.assert_envelope(response, 503, success=False)
        self.assertEqual("read_failed", response.json()["status_msg"])
        self.assertNotIn("private-server", response.text)
        self.assertNotIn("credentials", response.text)

    async def test_tc40_old_read_without_capability_does_not_receive_schema2_fields(self):
        for cap in (None, "1"):
            params = {"action": "read", "timestamp": str(TIMESTAMP)}
            if cap is not None:
                params["equipment_contract"] = cap
            response = await self.client.get("/api/jjc/ranking-stats", params=params)
            self.assert_envelope(response, 409, success=False)
            self.assertEqual("equipment_contract_upgrade_required", response.json()["status_msg"])
            self.assertNotIn("equipment_view", response.json()["data"])

    async def test_tc26_frozen_read_contains_same_revision_and_public_manifest(self):
        response = await self.client.get("/api/jjc/ranking-stats", params={
            "action": "read", "timestamp": str(TIMESTAMP), "equipment_contract": "2"})
        data = self.assert_envelope(response)
        self.assertEqual(MEMBER_REVISION, data["member_revision"])
        self.assertIn("frozen_source", data, "Frozen read must advertise its exact source manifest")
        self.assertEqual("local_win_v2", data["frozen_source"])
        self.assertEqual(self.stats.view["equipment_view_revision"], data["equipment_view"]["equipment_view_revision"])
        self.assertEqual((2, 5), tuple(data["equipment_view"]["ranges"]["top_1000"][k] for k in ("L", "P")))
        self.assertNotIn("display_locator", response.text)
        self.assertNotIn("identity_id", response.text)

    async def test_tc26_detail_and_flat_require_matching_frozen_revision(self):
        for endpoint, extra in (("details", {"lane": "dps", "kungfu": "山居剑意"}),
                                ("flat-members", {})):
            for revision in (None, "different-revision"):
                params = {"timestamp": str(TIMESTAMP), "range": "top_1000", "equipment_contract": "2", **extra}
                if revision is not None:
                    params["expected_view_revision"] = revision
                response = await self.client.get("/api/jjc/ranking-stats/"+endpoint, params=params)
                self.assert_envelope(response, 409, success=False)
                self.assertEqual("version_mismatch", response.json()["status_msg"])
        self.stats.load_detail.assert_not_awaited()
        self.stats.list_flat_members.assert_not_awaited()

    async def test_tc26_frozen_detail_and_flat_are_derived_from_same_complete_view(self):
        for endpoint, extra, key in (("details", {"lane": "dps", "kungfu": "山居剑意"}, "members"),
                                    ("flat-members", {}, "items")):
            params = {"timestamp": str(TIMESTAMP), "range": "top_1000", "equipment_contract": "2",
                "expected_view_revision": self.stats.view["equipment_view_revision"], **extra}
            response = await self.client.get("/api/jjc/ranking-stats/"+endpoint, params=params)
            data = self.assert_envelope(response)
            self.assertIn("equipment_view_revision", data, "Split frozen reads must advertise their pinned revision")
            self.assertEqual(self.stats.view["equipment_view_revision"], data["equipment_view_revision"])
            self.assertEqual(10, len(data[key]))
            self.assertNotIn("display_locator", response.text)
        self.stats.load_detail.assert_not_awaited()
        self.stats.list_flat_members.assert_not_awaited()

    async def test_tc40_old_detail_and_flat_cannot_receive_v2_rows(self):
        for endpoint, extra in (("details", {"lane": "dps", "kungfu": "山居剑意"}),
                                ("flat-members", {})):
            response = await self.client.get("/api/jjc/ranking-stats/"+endpoint, params={
                "timestamp": str(TIMESTAMP), "range": "top_1000", **extra})
            self.assert_envelope(response, 409, success=False)
            self.assertEqual("equipment_contract_upgrade_required", response.json()["status_msg"])
        self.stats.load_detail.assert_not_awaited()
        self.stats.list_flat_members.assert_not_awaited()

    async def test_tc40_current_displays_server_guard_prevents_v2_single_card_updates(self):
        response = await self.client.get("/api/jjc/ranking-stats/current-displays", params={
            "timestamp": str(TIMESTAMP), "range": "top_1000", "ranks": "1,2"})
        data = self.assert_envelope(response)
        self.assertFalse(data["overlay_enabled"])
        self.legacy_display.read.assert_not_awaited()
        self.assertEqual([], self.inspect.calls)

    async def test_v2_frozen_split_endpoints_validate_ascii_int64_timestamp(self):
        for endpoint, extra in (("details", {"lane": "dps", "kungfu": "山居剑意"}),
                                ("flat-members", {})):
            for stamp in ("²", "0", "9223372036854775808", "9"*4301):
                with self.subTest(endpoint=endpoint, stamp=stamp[:25]):
                    response = await self.client.get("/api/jjc/ranking-stats/"+endpoint, params={
                        "timestamp": stamp, "range": "top_1000", "equipment_contract": "2",
                        "expected_view_revision": self.stats.view["equipment_view_revision"], **extra})
                    self.assert_envelope(response, 400, success=False)

    async def test_v2_unknown_classification_details_are_preserved(self):
        members = deepcopy(self.stats.view["members"])
        members[-1].update(lane="unknown", kungfu=None, kungfu_id=None, found=False)
        self.stats.view = aggregate_equipment_view(timestamp=TIMESTAMP, scope=SCOPE,
            members=members, checked_at=CHECKED_AT-10, completed_at=CHECKED_AT-9,
            member_revision=MEMBER_REVISION)
        self.stats.summary.update(equipment_view=deepcopy(self.stats.view),
            equipment_view_revision=self.stats.view["equipment_view_revision"])
        response = await self.client.get("/api/jjc/ranking-stats/details", params={
            "timestamp": str(TIMESTAMP), "range": "top_1000", "equipment_contract": "2",
            "expected_view_revision": self.stats.view["equipment_view_revision"],
            "lane": "unknown", "kungfu": "未知"})
        data = self.assert_envelope(response)
        self.assertEqual([10], [m["rank"] for m in data["members"]])

    async def test_tc31_frozen_v2_remains_readable_when_local_disabled_and_latest_legacy_requires_upgrade(self):
        config.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED = 0
        self.service.enabled = False
        response = await self.client.get("/api/jjc/ranking-stats", params={
            "action": "read", "timestamp": str(TIMESTAMP), "equipment_contract": "2"})
        data = self.assert_envelope(response)
        self.assertEqual(self.stats.view["equipment_view_revision"], data["equipment_view"]["equipment_view_revision"])
        self.assertEqual([], self.inspect.calls, "Frozen reading must not initialize a local-win build")
        config.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED = 1
        self.service.enabled = True
        self.stats.summary["schema_version"] = 1
        self.stats.summary.pop("equipment_source")
        response = await self.client.get("/api/jjc/ranking-stats", params={
            "action": "read", "timestamp": str(TIMESTAMP)})
        self.assert_envelope(response, 409, success=False)
        self.assertEqual("equipment_contract_upgrade_required", response.json()["status_msg"])
        response = await self.client.get("/api/jjc/ranking-stats/current-displays", params={
            "timestamp": str(TIMESTAMP), "range": "top_1000", "ranks": "1"})
        self.assertFalse(self.assert_envelope(response)["overlay_enabled"])
        self.legacy_display.read.assert_not_awaited()

    async def test_tc48_schema1_historical_read_remains_complete_legacy(self):
        self.stats.summary = {"timestamp": TIMESTAMP, "schema_version": 1,
            "ranking_display_kind": "historical", "current_season": "old-season",
            "kungfu_statistics": {"top_50": {"total_players": 12}}}
        response = await self.client.get("/api/jjc/ranking-stats", params={
            "action": "read", "timestamp": str(TIMESTAMP)})
        data = self.assert_envelope(response)
        self.assertEqual("historical", data["ranking_display_kind"])
        self.assertEqual(12, data["kungfu_statistics"]["top_50"]["total_players"])
        self.assertEqual([], self.inspect.calls)
