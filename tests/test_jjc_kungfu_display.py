"""Snapshot coverage must remain visible; missing equipment is not purple gear."""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from jinja2 import Environment, FileSystemLoader
from src.renderers.jx3.jjc_ranking import render_combined_ranking_image, send_split_ranking_images


class KungfuDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_snapshot_labels_coverage_and_suppresses_incomplete_equipment(self):
        stats = {"top_50": {
            "kungfu_cache": {"total": 4, "fresh": 1, "stale": 1, "unknown": 2, "coverage": .5},
            "healer": {"list": [], "valid_count": 0, "equipment_incomplete": True},
            "dps": {"list": [["冰心诀", 2]], "valid_count": 2, "equipment_incomplete": True,
                    "members": {"冰心诀": [{"weapon_quality": None}]}}}}
        render = AsyncMock(return_value=b"image")
        await render_combined_ranking_image(env=None, render_template_image=render,
            current_season="season", stats=stats, week_info="week", show_legendary=True)
        template, context = render.call_args.args[1:]
        self.assertFalse(context["show_legendary"])
        html = Environment(loader=FileSystemLoader("templates")).get_template(template).render(**context)
        self.assertIn("未知 2", html)
        self.assertIn("心法覆盖 50.0%", html)
        self.assertNotIn("新鲜", html)
        self.assertNotIn("待刷新", html)
        self.assertNotIn("时效", html)
        self.assertIn("装备数据不完整", html)

    async def test_legacy_snapshot_labels_unrecorded_coverage_without_freshness(self):
        for stats in ({}, {"top_50": {}}):
            with self.subTest(stats=stats):
                render = AsyncMock(return_value=b"image")
                await render_combined_ranking_image(env=None, render_template_image=render,
                    current_season="season", stats=stats, week_info="week", show_legendary=True)
                context = render.call_args.args[2]
                self.assertTrue(context["show_legendary"])
                html = Environment(loader=FileSystemLoader("templates")).get_template(render.call_args.args[1]).render(**context)
                self.assertIn("心法覆盖率未记录", html)
                self.assertNotIn("新鲜", html)
                self.assertNotIn("待刷新", html)
                self.assertNotIn("时效", html)

    async def test_split_images_keep_coverage_without_freshness(self):
        stats = {key: {"kungfu_cache": {"coverage": .5, "fresh": 1, "stale": 1, "unknown": 2}}
                 for key in ("top_1000", "top_200", "top_100", "top_50")}
        env = Environment(loader=FileSystemLoader("templates"))
        render = AsyncMock(return_value=b"image")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await send_split_ranking_images(AsyncMock(), None, env=env,
                render_template_image=render, current_season="season", stats=stats,
                week_info="week", show_legendary=True)
        self.assertEqual(render.await_count, 8)
        for call in render.call_args_list:
            html = env.get_template(call.args[1]).render(**call.args[2])
            self.assertIn("心法覆盖 50.0% · 未知 2", html)
            self.assertNotIn("新鲜", html)
            self.assertNotIn("待刷新", html)
            self.assertNotIn("时效", html)

    def test_web_pages_match_and_equipment_filter_handles_incomplete_samples(self):
        page = Path("public/jjc-ranking-stats.html").read_text()
        self.assertEqual(page, Path("public/index.html").read_text())
        self.assertIn("function renderKungfuCoverage(", page)
        self.assertIn("装备数据不完整", page)

    def test_purple_list_does_not_request_or_label_unchecked_equipment(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required to execute the frontend list branch")
        page = Path("public/index.html").read_text()
        functions = page[page.index("        function renderListView("):page.index("        function syncOuterTabUI(")]
        equipment = page[page.index("        function hasIncompleteEquipment("):page.index("        function renderKungfuCoverage(")]
        harness = """
const assert = require('node:assert/strict');
let currentStats = {kungfu_statistics:{top_50:{healer:{equipment_incomplete:true},dps:{equipment_incomplete:true}}}};
let currentStatsTimestamp = 1793923200;
// This fixture exercises the unchanged legacy branch.
let equipmentViewState = null;
let grid = {innerHTML:''};
let calls = 0;
let rankingListCache = new Map();
const getListSourceConfig = () => ({api:'flat-members'});
const buildFlatMembersUrl = () => 'fixture';
const buildPeakScoreUrl = () => 'fixture';
const setStatus = () => {};
const unwrapResponse = x => x;
const fetch = async () => {calls++;return {ok:true,json:async()=>({items:[]})};};
const isPeakTab = () => false;
const rangeConfigs = [{key:'top_50',label:'前50'}];
const getOuterTabLabel = () => '推栏排名-紫武';
const formatTime = () => 'fixture time';
const escapeHtml = x => String(x);
"""
        check = """
(async () => {
  await loadRankingListView('purple','top_50');
  assert.equal(calls, 0, 'unchecked equipment must not load a purple list');
  assert.match(grid.innerHTML, /装备数据不完整/);
  renderListView({items:[]},'purple','top_50');
  assert.match(grid.innerHTML, /装备数据不完整/);
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run([node,"-e",harness+equipment+functions+check], capture_output=True, text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_equipment_decision_is_pure_and_shared_by_distribution_filter_and_export(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required for frontend behavior tests")
        page = Path("public/index.html").read_text()
        distribution = page[page.index("        function renderList("):page.index("        function buildFilteredStats(")]
        details = page[page.index("        function renderDetailContent("):page.index("        async function ensureDetailLoaded(")]
        export = page[page.index("        function buildDistributionExportMarkup("):page.index("        function prepareDistributionExportClone(")]
        harness = """
const assert = require('node:assert/strict');
const window = {currentSeasonLabel:'fixture season'};
const rangeConfigs = [{key:'top_50',label:'前50'}];
const getOuterTabLabel = () => 'fixture';
const escapeHtml = x => String(x);
const showLegendaryOverlay = true;
const grid = {innerHTML:'unchanged'};
const currentOuterTab = 'all';
const isLegendaryWeapon = () => false;
"""
        check = """
const lane = Object.freeze({list:[['冰心诀',2]],valid_count:2,legendary_count_map:{'冰心诀':1},equipment_incomplete:true});
const range = Object.freeze({healer:{list:[],valid_count:0},dps:lane,kungfu_cache:{coverage:1,fresh:2,stale:0,unknown:0}});
const before = JSON.stringify(range);
assert.equal(typeof hasIncompleteEquipment, 'function', 'equipment decision must be callable without rendering');
assert.equal(hasIncompleteEquipment(range.healer,range.dps),true);
assert.equal(hasIncompleteEquipment(undefined,null,{}),false, 'legacy snapshots retain old behavior');
assert.equal(hasIncompleteEquipment({equipment_incomplete:false}),false);
assert.equal(grid.innerHTML,'unchanged','decision must not write the DOM');
assert.match(renderKungfuCoverage(range),/装备数据不完整/);
assert.match(renderKungfuCoverage(range),/心法覆盖 100.0% · 未知 0/);
assert.equal(renderKungfuCoverage({}),'心法覆盖率未记录');
const card = renderRangeCard('top_50','前50',range,'all');
assert.match(card,/class="bar-legendary" style="width:0%"/);
assert.deepEqual(buildFilteredLane(lane,'purple').list,[]);
const markup = buildDistributionExportMarkup({kungfu_statistics:{top_50:range}},'all','fixture','fixture',true);
assert.match(markup,/装备数据不完整/);
assert.match(markup,/class="bar-legendary" style="width:0%"/);
const legacy = {list:[['冰心诀',2]],valid_count:2,legendary_count_map:{'冰心诀':1}};
assert.equal(buildFilteredLane(legacy,'purple').valid_count,1);
for (const output of [renderKungfuCoverage(range),renderKungfuCoverage({}),card,markup]) {
  assert.doesNotMatch(output,/新鲜|待刷新|时效/);
}
for (const state of ['fresh','stale','unknown',undefined]) {
  const member = Object.freeze({rank:1,name:'测试角色',server:'测试服',kungfu_freshness:state,score:2800});
  const detail = renderDetailContent([member]);
  assert.match(detail,/测试角色/);
  assert.match(detail,/2800/);
  assert.doesNotMatch(detail,/新鲜|待刷新|时效/);
}
assert.equal(JSON.stringify(range),before,'rendering and filtering must not mutate snapshot input');
"""
        actual_peak = page[page.index("        function isPeakTab("):page.index("        function getOuterTabLabel(")]
        display_helpers = page.split("// BEGIN RANKING DISPLAY HELPERS", 1)[1].split("// END RANKING DISPLAY HELPERS", 1)[0]
        icon_catalog = page[page.index("        const kungfuIconIdByName = {"):page.index("        const subtitle =")]
        icon_resolver = page[page.index("        function resolveKungfuIconId("):page.index("        function renderTeamKungfuIconsInline(")]
        result = subprocess.run([node,"-e",harness+icon_catalog+icon_resolver+actual_peak+display_helpers+details+distribution+export+check],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
