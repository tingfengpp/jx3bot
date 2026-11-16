"""E4 generation/publication/caller ordering; every external effect is injected."""
import asyncio
from copy import deepcopy
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from src.services.jx3 import jjc_ranking_equipment as policy
from src.services.jx3.jjc_ranking import JjcRankingService
from src.plugins.jx3bot_handlers.jjc_ranking import register
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TIMESTAMP, MEMBER_REVISION, ten_member_golden
from tests import test_status_monitor_jobs_admin_notification as jobs_fixtures
_load_jobs = jobs_fixtures._load_jobs


def golden_view():
    return policy.aggregate_equipment_view(timestamp=TIMESTAMP, scope=deepcopy(SCOPE),
        members=ten_member_golden(), checked_at=CHECKED_AT, completed_at=CHECKED_AT+.1,
        member_revision=MEMBER_REVISION)


def view_stats(view):
    # A test-local shape placeholder supports business RED before the new adapter exists.
    adapter = getattr(policy, "equipment_view_statistics", lambda view: {})
    return adapter(view)


def make_service(equipment_service):
    service = JjcRankingService(token="", ticket="", jjc_query_url="", arena_time_tag_url="",
        arena_ranking_url="", match_detail_url="", jjc_ranking_cache_duration=10,
        kungfu_cache_duration=10, current_season=SCOPE["season_id"], current_season_start="2027-01-15",
        kungfu_healer_list=["灵素"], kungfu_dps_list=["山居剑意"], kungfu_pinyin_to_chinese={},
        tuilan_request=Mock(side_effect=AssertionError("upstream forbidden")),
        defget_get=AsyncMock(side_effect=AssertionError("upstream forbidden")))
    # Frozen dataclass currently has no optional dependency; do not count TypeError as RED.
    object.__setattr__(service, "equipment_service", equipment_service)
    return service


class EquipmentStatisticsAdapterTests(unittest.TestCase):
    def test_adapter_golden_metrics_and_purple_set_are_one_view(self):
        view = golden_view(); original = deepcopy(view); stats = view_stats(view)
        self.assertIn("top_50", stats)
        for key in ("top_50", "top_100", "top_200", "top_1000"):
            current = stats[key]
            self.assertEqual("local_win_v2", current["equipment_source"])
            self.assertEqual(view["equipment_view_revision"], current["equipment_view_revision"])
            self.assertEqual((10,2,5,3), tuple(current["equipment_metrics"][k] for k in ("N","L","P","U")))
            self.assertEqual([3,4,5,6,7], current["equipment_metrics"]["purple_ranks"])
            self.assertEqual(2, current["dps"]["legendary_count_map"]["山居剑意"])
            self.assertEqual(10, current["dps"]["valid_count"])
            self.assertAlmostEqual(.2, current["dps"]["equipment_metrics"]["山居剑意"]["legendary_ratio"])
        self.assertEqual(original, view)

    def test_adapter_preserves_unknown_classification_members_in_population(self):
        members = ten_member_golden()
        members[2].update(lane="unknown", found=False, kungfu="未知", kungfu_id=None)
        view = policy.aggregate_equipment_view(timestamp=TIMESTAMP, scope=deepcopy(SCOPE), members=members,
            checked_at=CHECKED_AT, completed_at=CHECKED_AT+.1, member_revision=MEMBER_REVISION)
        stats = view_stats(view)
        self.assertIn("top_50", stats)
        self.assertEqual(10, stats["top_50"]["total_players"])
        self.assertEqual([3], [m["rank"] for m in stats["top_50"]["unknown_members"]])
        self.assertIn(3, stats["top_50"]["equipment_metrics"]["purple_ranks"])


class RankingEquipmentGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.view = golden_view()
        self.equipment = SimpleNamespace(build_frozen_view=AsyncMock(return_value=deepcopy(self.view)))
        self.service = make_service(self.equipment)
        self.members = ten_member_golden()
        self.ranking = {"code":0, "cache_time":TIMESTAMP,
            "data":[{"personInfo":{"server":m["server"],"roleName":m["name"],
                     "gameRoleId":str(100+m["rank"]),"zone":"合成大区"},"score":m["score"]} for m in self.members]}
        by_name = {m["name"]: m for m in self.members}
        async def kungfu(server, name, **kwargs):
            m = by_name[name]
            return {**deepcopy(m), "found":True, "kungfu":"山居剑意", "kungfu_id":10145}
        self.query = AsyncMock(side_effect=kungfu)
        self.enqueue = AsyncMock(return_value=True)
        for patcher in [
            patch("src.services.jx3.jjc_ranking.cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED",1,create=True),
            patch("src.services.jx3.jjc_ranking.cfg.JJC_RANKING_KUNGFU_CACHE_ENABLED",0),
            patch("src.services.jx3.jjc_ranking.cfg.JJC_KUNGFU_REFRESH_ENABLED",0),
            patch("src.services.jx3.jjc_ranking.logger",MagicMock()),
            patch("src.services.jx3.jjc_ranking.get_db",side_effect=AssertionError("default Mongo forbidden")),
            patch.object(JjcRankingService,"get_user_kungfu",new=self.query),
            patch.object(JjcRankingService,"_enqueue_ranking_member_for_sync",new=self.enqueue),
        ]:
            patcher.start(); self.addCleanup(patcher.stop)

    async def test_tc17_v2_build_receives_complete_frozen_rankings_and_drives_same_stats(self):
        result = await self.service.get_ranking_kungfu_data(deepcopy(self.ranking))
        self.assertFalse(result.get("error"), result)
        self.equipment.build_frozen_view.assert_awaited_once()
        call = self.equipment.build_frozen_view.await_args
        self.assertEqual(TIMESTAMP, call.kwargs["timestamp"])
        members = call.kwargs["members"]
        self.assertEqual(list(range(1,11)), [m["rank"] for m in members])
        self.assertEqual([m["score"] for m in self.members], [m["score"] for m in members])
        self.assertEqual([10145]*10, [m["kungfu_id"] for m in members])
        self.assertEqual(self.view["equipment_view_revision"], result["equipment_view"]["equipment_view_revision"])
        self.assertEqual((10,2,5,3), tuple(result["kungfu_statistics"]["top_50"]["equipment_metrics"][k] for k in ("N","L","P","U")))
        self.assertEqual(10, self.enqueue.await_count)

    async def test_tc50_flag_off_and_explicit_history_never_use_new_equipment_service(self):
        for flag, historical in ((0,False),(1,True)):
            self.equipment.build_frozen_view.reset_mock()
            with self.subTest(flag=flag,historical=historical), patch("src.services.jx3.jjc_ranking.cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED",flag):
                result = await self.service.get_ranking_kungfu_data(dict(self.ranking,explicit_tag=historical))
                self.assertFalse(result.get("error"),result)
                self.equipment.build_frozen_view.assert_not_awaited()
                self.assertNotIn("equipment_view",result)

    async def test_tc28_new_equipment_failure_does_not_undo_original_sync_requests(self):
        self.equipment.build_frozen_view.side_effect = RuntimeError("synthetic-equipment-read-failed")
        result = await self.service.get_ranking_kungfu_data(deepcopy(self.ranking))
        self.assertTrue(result.get("error"),result)
        self.assertEqual(10,self.enqueue.await_count)
        self.assertNotIn("equipment_view",result)

    async def test_tc27_v2_publication_uses_single_complete_repo_and_preserves_sync_compensation(self):
        repo = SimpleNamespace(publish_equipment_snapshot=AsyncMock(return_value={"status":"published"}),
                               save_snapshot=AsyncMock(side_effect=AssertionError("legacy split writer forbidden for V2")),
                               get_latest_current_summary=AsyncMock(return_value=None))
        compensation = AsyncMock()
        with patch("src.services.jx3.jjc_ranking.JjcRankingStatsRepo",return_value=repo), patch.object(JjcRankingService,"_enqueue_ranking_members_for_sync",new=compensation):
            task = self.service.save_ranking_stats(self.ranking,view_stats(self.view),"测试周",payload={"equipment_view":self.view})
            self.assertIsNotNone(task,"V2 save must expose its publication outcome")
            await task
        repo.publish_equipment_snapshot.assert_awaited_once()
        repo.save_snapshot.assert_not_awaited()
        compensation.assert_awaited_once()

    async def test_tc27_v2_publish_error_reaches_awaiting_caller(self):
        repo = SimpleNamespace(publish_equipment_snapshot=AsyncMock(side_effect=RuntimeError("synthetic-publish-failed")),
                               save_snapshot=AsyncMock(), get_latest_current_summary=AsyncMock(return_value=None))
        with patch("src.services.jx3.jjc_ranking.JjcRankingStatsRepo",return_value=repo), patch.object(JjcRankingService,"_enqueue_ranking_members_for_sync",new=AsyncMock()):
            task = self.service.save_ranking_stats(self.ranking,view_stats(self.view),"测试周",payload={"equipment_view":self.view})
            self.assertIsNotNone(task)
            with self.assertRaisesRegex(RuntimeError,"synthetic-publish-failed"):
                await task


class SafeDailyAndManualCallerTests(unittest.IsolatedAsyncioTestCase):
    def daily(self):
        module = _load_jobs()
        view = golden_view(); stats = view_stats(view)
        bot, notify = jobs_fixtures.StatusMonitorJobsAdminNotificationTests._prepare_ranking_job(module,
            ranking_result={"code":0,"defaultWeek":1,"cache_time":TIMESTAMP},
            ranking_data={"kungfu_statistics":stats or {"top_50":{"dps":{}}},"equipment_view":view},
            payload={"image_bytes":b"image","scope_desc":"范围","total_valid_data":10,"processed_label":"角色"})
        return module,bot,notify,view

    async def test_tc27_daily_payload_includes_v2_even_without_old_optional_flags(self):
        module,bot,notify,view = self.daily()
        async def save(): return {"status":"published"}
        module.jjc_ranking_service.save_ranking_stats = Mock(side_effect=lambda **kwargs: asyncio.create_task(save()))
        await module.push_daily_jjc_ranking()
        call = module.jjc_ranking_service.save_ranking_stats.call_args
        self.assertIn("payload",call.kwargs)
        self.assertEqual(view["equipment_view_revision"],call.kwargs["payload"]["equipment_view"]["equipment_view_revision"])

    async def test_tc29_offline_daily_still_waits_for_publication(self):
        module,bot,notify,view = self.daily(); module.get_driver = lambda: SimpleNamespace(bots={})
        finished = []
        async def save(): await asyncio.sleep(0); finished.append(True)
        module.jjc_ranking_service.save_ranking_stats = Mock(side_effect=lambda **kwargs:asyncio.create_task(save()))
        await module.push_daily_jjc_ranking()
        self.assertEqual([True],finished)
        module.render_combined_ranking_image.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

    async def test_tc27_daily_none_save_is_failure_for_v2_not_permission_to_render(self):
        module,bot,notify,view = self.daily()
        module.jjc_ranking_service.save_ranking_stats = Mock(return_value=None)
        await module.push_daily_jjc_ranking()
        module.render_combined_ranking_image.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

    def manual(self,save):
        class Matcher:
            callback = None
            def handle(self):
                def keep(func): self.callback=func; return func
                return keep
        matcher=Matcher(); bot=SimpleNamespace(send=AsyncMock()); event=SimpleNamespace(get_plaintext=lambda:"竞技排名")
        render=AsyncMock(); result={"equipment_view":golden_view(),"kungfu_statistics":view_stats(golden_view()) or {"top_50":{"dps":{}}}}
        register(matcher,query_jjc_ranking=AsyncMock(return_value={"code":0,"cache_time":TIMESTAMP,"defaultWeek":1}),
            calculate_season_week_info=lambda *args:"测试周",get_ranking_kungfu_data=AsyncMock(return_value=result),
            save_ranking_stats=save,generate_split_ranking_images=AsyncMock(),generate_combined_ranking_image=render)
        return matcher.callback,bot,event,render

    async def test_tc27_manual_render_waits_for_real_save_completion(self):
        gate=asyncio.get_running_loop().create_future(); save=Mock(return_value=gate)
        callback,bot,event,render=self.manual(save); task=asyncio.create_task(callback(bot,event))
        try:
            await asyncio.sleep(0)
            render.assert_not_awaited()
        finally:
            if not gate.done(): gate.set_result({"status":"published"})
            await task
        render.assert_awaited_once()
        self.assertIn("payload",save.call_args.kwargs)

    async def test_tc27_manual_publish_failure_never_renders_new_image(self):
        gate=asyncio.get_running_loop().create_future(); gate.set_exception(RuntimeError("synthetic-publish-failed"))
        callback,bot,event,render=self.manual(Mock(return_value=gate))
        try:
            await callback(bot,event)
            render.assert_not_awaited()
        finally:
            gate.exception()  # Consume exception even while the old caller incorrectly ignores it.


if __name__ == "__main__": unittest.main()
