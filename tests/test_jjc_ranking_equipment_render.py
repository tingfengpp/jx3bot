"""E4 QQ presentation contracts, with explicit service-produced golden metrics.

Templates render locally through Jinja; send and browser image generation are mocked.
Actual Chromium image QA remains the later E7 requirement.
"""
from copy import deepcopy
import re
import unittest
from unittest.mock import AsyncMock, patch
from jinja2 import Environment, FileSystemLoader

from src.renderers.jx3 import jjc_ranking as renderer
from tests.jjc_equipment_fixtures import ten_member_golden


def golden_stats():
    metrics={"N":10,"L":2,"P":5,"U":3,"legendary_ratio":.2,"equipment_coverage":.7,"purple_ranks":[3,4,5,6,7]}
    current={"equipment_source":"local_win_v2","equipment_view_revision":"golden-content-v2",
        "equipment_metrics":deepcopy(metrics),"total_players":10,"total_valid_count":10,
        "kungfu_cache":{"coverage":1.0,"unknown":0},
        "healer":{"valid_count":0,"list":[],"members":{},"equipment_incomplete":True,"equipment_metrics":{}},
        "dps":{"valid_count":10,"list":[("山居剑意",10)],"distribution":{"山居剑意":10},
               "members":{"山居剑意":ten_member_golden()},"equipment_incomplete":True,
               "equipment_metrics":{"山居剑意":deepcopy(metrics)},"legendary_count_map":{"山居剑意":2}}}
    return {key:deepcopy(current) for key in ("top_50","top_100","top_200","top_1000")}


class EquipmentRenderPurePresentationTests(unittest.TestCase):
    def test_tc30_template_rows_use_twenty_percent_aggregate_not_reverse_legacy_weapons(self):
        data=golden_stats()["top_50"]
        with patch.object(renderer,"is_jjc_legendary_weapon",side_effect=AssertionError("V2 renderer must not reclassify legacy weapons")):
            rows=renderer._prepare_template_data(data,"dps")
        self.assertEqual(1,len(rows))
        self.assertEqual("山居剑意",rows[0][0])
        self.assertEqual(10,rows[0][1])
        self.assertEqual(20.0,rows[0][4])

    def test_tc30_coverage_label_reports_equipment_seventy_percent_and_unknown_three(self):
        text=renderer._coverage_label(golden_stats()["top_50"])
        self.assertRegex(text,r"装备覆盖\s*70(?:\.0)?%")
        self.assertRegex(text,r"(?:装备未知|未知装备|未知)\s*3")
        self.assertIn("心法",text)

    def test_tc30_v2_unknowns_do_not_hide_whole_equipment_statistics(self):
        self.assertFalse(renderer._equipment_incomplete(golden_stats()["top_50"]))

    def test_legacy_incomplete_snapshot_keeps_existing_suppression(self):
        legacy=golden_stats()["top_50"]
        legacy.pop("equipment_source");legacy.pop("equipment_view_revision");legacy.pop("equipment_metrics")
        for lane in ("healer","dps"):
            legacy[lane].pop("equipment_metrics")
        self.assertTrue(renderer._equipment_incomplete(legacy))

    def test_v2_zero_known_equipment_is_zero_confirmed_ratio_with_unknown_coverage(self):
        data=golden_stats()["top_50"]
        metrics={"N":10,"L":0,"P":0,"U":10,"legendary_ratio":0.0,"equipment_coverage":0.0,"purple_ranks":[]}
        data["equipment_metrics"]=deepcopy(metrics);data["dps"]["equipment_metrics"]["山居剑意"]=deepcopy(metrics)
        self.assertEqual(0,renderer._prepare_template_data(data,"dps")[0][4])
        self.assertFalse(renderer._equipment_incomplete(data))
        self.assertRegex(renderer._coverage_label(data),r"装备覆盖\s*0(?:\.0)?%")


class EquipmentActualJinjaTemplateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env=Environment(loader=FileSystemLoader("templates"))

    async def test_tc30_combined_template_shows_same_twenty_percent_and_equipment_coverage(self):
        stats=golden_stats();original=deepcopy(stats);captures=[]
        async def render(env,template,context,**kwargs):
            html=env.get_template(template).render(**context)
            captures.append((template,deepcopy(context),html))
            return b"local-combined-image"
        payload=await renderer.render_combined_ranking_image(env=self.env,render_template_image=render,
            current_season="合成赛季",stats=stats,week_info="测试周",show_legendary=True)
        self.assertEqual(b"local-combined-image",payload["image_bytes"])
        self.assertEqual(1,len(captures));template,context,html=captures[0]
        self.assertTrue(context["show_legendary"])
        self.assertFalse(context["equipment_incomplete"])
        self.assertEqual(20,context["top_50_dps"][0][4])
        self.assertRegex(html,r"width:\s*20(?:\.0)?%")
        self.assertRegex(html,r"装备覆盖\s*70(?:\.0)?%")
        self.assertNotIn("装备数据不完整，橙武占比暂不展示",html)
        self.assertEqual(original,stats)

    async def test_tc30_all_eight_split_templates_consume_same_metrics_without_hiding_unknowns(self):
        captures=[];bot=type("SafeBot",(),{"send":AsyncMock()})()
        async def render(env,template,context,**kwargs):
            captures.append((template,deepcopy(context),env.get_template(template).render(**context)))
            return b"local-split-image"
        with patch("asyncio.sleep",new=AsyncMock()):
            await renderer.send_split_ranking_images(bot,None,env=self.env,render_template_image=render,
                current_season="合成赛季",stats=golden_stats(),week_info="测试周",show_legendary=True)
        self.assertEqual(8,len(captures))
        self.assertGreaterEqual(bot.send.await_count,8)  # Existing final summary message remains allowed.
        for template,context,html in captures:
            with self.subTest(template=template):
                self.assertTrue(context["show_legendary"])
                self.assertFalse(context["equipment_incomplete"])
                self.assertRegex(context["coverage_label"],r"装备覆盖\s*70(?:\.0)?%")
                self.assertNotIn("装备数据不完整，橙武占比暂不展示",html)
                if "DPS" in template:
                    self.assertRegex(html,r"width:\s*20(?:\.0)?%")

    async def test_legacy_combined_incomplete_snapshot_still_hides_legendary_percent(self):
        data=golden_stats()
        for item in data.values():
            item.pop("equipment_source");item.pop("equipment_view_revision");item.pop("equipment_metrics")
            for lane in ("healer","dps"): item[lane].pop("equipment_metrics")
        capture=AsyncMock(return_value=b"legacy-image")
        await renderer.render_combined_ranking_image(env=self.env,render_template_image=capture,
            current_season="合成赛季",stats=data,week_info="测试周",show_legendary=True)
        context=capture.await_args.args[2]
        self.assertFalse(context["show_legendary"])
        self.assertTrue(context["equipment_incomplete"])


if __name__ == "__main__":unittest.main()
