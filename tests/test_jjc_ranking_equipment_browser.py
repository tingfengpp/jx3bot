"""E6 real two-entry HTML/UMD interaction tests with local fixture HTTP.

These tests prove UI behavior, not Mongo/ASGI integration. E7 owns the real
chain. All requests are intercepted; html2canvas here is a lifecycle stub, NOT
actual image-export evidence. Production application state is never injected.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright
from src.services.jx3.jjc_ranking_equipment import aggregate_equipment_view, public_equipment_view
from tests.jjc_equipment_fixtures import (
    SCOPE, CHECKED_AT, TIMESTAMP, MEMBER_REVISION, ten_member_golden, equipped_member,
)

ROOT = Path(__file__).resolve().parents[1]


def make_view(members=None, *, timestamp=TIMESTAMP, member_revision=MEMBER_REVISION, checked_at=CHECKED_AT):
    members = ten_member_golden() if members is None else deepcopy(members)
    for row in members:
        packet = row.get("local_win_packet")
        if packet:
            packet["team_ready"] = True
            packet["self_icon_id"] = packet.get("self_kungfu_id")
            packet["teammates"] = [
                {"role_name":"合成队友甲","server":"合成服务器","kungfu_id":10786,"icon_id":10786},
                {"role_name":"合成队友乙","server":"合成服务器","kungfu_id":10626,"icon_id":10626},
            ]
    return public_equipment_view(aggregate_equipment_view(
        timestamp=timestamp,scope=deepcopy(SCOPE),members=members,checked_at=checked_at,
        completed_at=checked_at+.1,member_revision=member_revision))


CANVAS_STUB = r"""
window.__equipmentExports=[];
window.html2canvas=async(node,options)=>{
 const grid=document.querySelector('#grid');
 window.__equipmentExports.push({revision:grid?.dataset.equipmentRevision,
  text:node.textContent,weaponTitles:[...node.querySelectorAll('img[alt=weapon]')].map(x=>x.title)});
 if(window.__holdEquipmentExport)await new Promise(resolve=>{window.__releaseEquipmentExport=resolve;});
 return {toDataURL:()=> 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jp1sAAAAASUVORK5CYII='};
};
"""


class EquipmentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.frozen = make_view()
        self.view = deepcopy(self.frozen)
        self.requests = []
        self.pageerrors = []
        self.console_errors = []
        self.response_flags = {}
        self.equipment_view_requested = asyncio.Event()
        self.fail_dynamic = False
        self.fail_bootstrap = False
        self.legacy_bootstrap = False
        self.extra_bootstraps = {}
        self.legacy_display_enabled = False
        self.entry = "jjc-ranking-stats.html"
        self.bootstrap_manifest = {"member_revision":MEMBER_REVISION,"frozen_source":"local_win_v2"}

    def views(self):
        return [r for r in self.requests if r[0].endswith('/equipment-view')]

    def bootstrap(self):
        # Deliberately wrong legacy statistics expose accidental source mixing.
        wrong = {"list":[["山居剑意",999]],"valid_count":999,"min_score":1,
                 "legendary_count_map":{"山居剑意":998},"equipment_incomplete":True}
        result = {"timestamp":self.frozen["timestamp"],"schema_version":2,
            "current_season":SCOPE["season_id"],"default_week":1,"week_info":"第1周",
            "ranking_cache_time":self.frozen["timestamp"],"generated_at":CHECKED_AT,
            "ranking_display_kind":"current","kungfu_cache_scope":deepcopy(SCOPE),
            "equipment_source":"local_win_v2","equipment_complete":True,
            "member_revision":self.bootstrap_manifest["member_revision"],
            "frozen_source":self.bootstrap_manifest["frozen_source"],
            "equipment_view_revision":self.frozen["equipment_view_revision"],
            "bootstrap_manifest":deepcopy(self.bootstrap_manifest),
            "equipment_view":deepcopy(self.frozen),
            "kungfu_statistics":{k:{"dps":deepcopy(wrong),"healer":{"list":[],"valid_count":0}}
                                 for k in self.frozen["ranges"]}}
        if self.legacy_bootstrap:
            result.update(schema_version=1,equipment_source="legacy_v1",frozen_source="legacy_v1")
            result.pop("equipment_view",None)
            result.pop("equipment_view_revision",None)
            result.pop("equipment_complete",None)
            result["equipment_members"]=[{k:deepcopy(v) for k,v in row.items() if k!='local_win_packet'}
                                         for row in self.frozen["members"]]
            for metrics in result["kungfu_statistics"].values():
                metrics["total_players"]=10
                metrics["dps"].update(list=[["山居剑意",10]],valid_count=10,legendary_count_map={"山居剑意":0})
        return result

    async def new_page(self, entry="jjc-ranking-stats.html", width=1440, wait_for_view=True):
        self.entry = entry
        context = await self.browser.new_context(viewport={"width":width,"height":950},accept_downloads=True)
        self.addAsyncCleanup(context.close)
        page = await context.new_page()
        page.set_default_timeout(4000)
        page.on('pageerror',lambda error:self.pageerrors.append(str(error)))
        page.on('console',lambda msg:self.console_errors.append(msg.text) if msg.type=='error' else None)
        await page.clock.install()
        await page.add_init_script("""(() => {
            const nativeSet = window.setTimeout.bind(window), nativeClear = window.clearTimeout.bind(window);
            window.__legacyDisplayTimers = new Set();
            window.setTimeout = (fn, ms, ...args) => {
                let id = nativeSet(() => {window.__legacyDisplayTimers.delete(id); fn(...args);}, ms);
                if (ms === 15000) window.__legacyDisplayTimers.add(id);
                return id;
            };
            window.clearTimeout = id => {window.__legacyDisplayTimers.delete(id); nativeClear(id);};
        })();""")

        async def route(route):
            parsed=urlparse(route.request.url)
            path,query=parsed.path,parse_qs(parsed.query)
            if path.endswith('.html'):
                await route.fulfill(body=(ROOT/'public'/Path(path).name).read_text(),content_type='text/html')
                return
            if path.endswith('jjc-equipment-view.js'):
                await route.fulfill(body=(ROOT/'public/jjc-equipment-view.js').read_text(),content_type='application/javascript')
                return
            if 'html2canvas' in path:
                await route.fulfill(body=CANVAS_STUB,content_type='application/javascript')
                return
            if path.endswith('.css'):
                file=ROOT/'public'/Path(path).name
                await route.fulfill(body=file.read_text() if file.exists() else '',content_type='text/css')
                return
            if '/api/jjc/ranking-stats' in path:
                self.requests.append((path,deepcopy(query)))
                if path.endswith('/equipment-view'):
                    self.equipment_view_requested.set()
                    if self.fail_dynamic:
                        await route.fulfill(status=503,body=json.dumps({'status_code':1,'status_msg':'read_failed',
                            'data':{'read_failed':True,'revocations':deepcopy(self.response_flags.get('revocations',[]))}}),content_type='application/json')
                        return
                    data={"timestamp":self.frozen['timestamp'],"mode":"dynamic","overlay_enabled":True,
                          "retired":False,"reload_required":False,"latest_timestamp":self.frozen['timestamp'],
                          "current_member_revision":self.bootstrap_manifest['member_revision'],
                          "current_frozen_source":self.bootstrap_manifest['frozen_source'],
                          "checked_at":self.view['checked_at'],"view":deepcopy(self.view),**deepcopy(self.response_flags)}
                elif path.endswith('/current-displays'):
                    data={'overlay_enabled':self.legacy_display_enabled,'timestamp':self.frozen['timestamp'],
                          'range':query.get('range',['top_1000'])[0],'items':[]}
                elif path.endswith('/details') and self.legacy_display_enabled:
                    rows=deepcopy(self.bootstrap()['equipment_members'])
                    for row in rows:
                        row.update(kungfu_acquisition='cache',kungfu_publish_version=7)
                    data={'members':rows}
                elif path.endswith('/details') or path.endswith('/flat-members'):
                    await route.fulfill(status=409,body=json.dumps({'status_code':1,'status_msg':'version_mismatch','data':None}),
                                        content_type='application/json')
                    return
                elif path.endswith('/peak-score'):
                    data={'items':[],'total':0,'item_count':0,'kungfu_statistics':{'dps':{'list':[],'valid_count':0},
                          'healer':{'list':[],'valid_count':0}}}
                elif query.get('action')==['seasons']:
                    data={'seasons':[{'name':SCOPE['season_id']}],'default_season':SCOPE['season_id']}
                elif query.get('action')==['season-history']:
                    data={'season':SCOPE['season_id'],'items':[{'timestamp':self.frozen['timestamp'],
                          'current_season':SCOPE['season_id'],'default_week':1,'week_info':'第1周',
                          'snapshot_kind':'daily','ranking_display_kind':'current'}]}
                    for stamp,payload in self.extra_bootstraps.items():
                        data['items'].append({'timestamp':stamp,'current_season':SCOPE['season_id'],
                            'default_week':1,'week_info':'第1周','snapshot_kind':'daily',
                            'ranking_display_kind':payload.get('ranking_display_kind','historical')})
                elif query.get('action')==['read']:
                    stamp=int(query.get('timestamp',[0])[0])
                    if self.fail_bootstrap and stamp==self.frozen['timestamp']:
                        await route.fulfill(status=503,body=json.dumps({'status_code':1,'status_msg':'read_failed','data':None}),
                                            content_type='application/json')
                        return
                    if query.get('equipment_contract')!=['2']:
                        await route.fulfill(status=409,body=json.dumps({'status_code':1,
                            'status_msg':'equipment_contract_upgrade_required','data':None}),content_type='application/json')
                        return
                    data=deepcopy(self.extra_bootstraps.get(stamp)) if stamp in self.extra_bootstraps else self.bootstrap()
                else:
                    data=[]
                await route.fulfill(body=json.dumps({'status_code':0,'status_msg':'success','data':data},ensure_ascii=False),
                                    content_type='application/json')
                return
            if '/api/announcements' in path:
                await route.fulfill(body=json.dumps({'status_code':0,'data':{'announcement':None,'items':[],'has_more':False}}),
                                    content_type='application/json')
                return
            # Every remaining URL (including remote icons) is fulfilled locally;
            # no request falls through to the user's network.
            await route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="purple"/></svg>',
                                content_type='image/svg+xml')

        await context.route('**/*',route)
        await page.goto('https://equipment.fixture/'+entry)
        if wait_for_view:
            await page.locator('#grid[data-equipment-revision]').wait_for()
        else:
            await page.wait_for_function("()=>document.querySelector('#snapshot-select')?.value")
        await self.settle(page)
        return page

    async def settle(self,page):
        await page.wait_for_timeout(80)

    def detail(self,page):
        return page.locator('.detail[data-range="top_50"][data-lane="dps"]').first

    async def assert_purple(self,page,count,ranks):
        detail=self.detail(page)
        self.assertEqual(str(count),(await detail.locator('.bar-value').inner_text()).strip())
        if not await detail.evaluate('el=>el.open'):
            await detail.locator('summary').click()
        await self.settle(page)
        self.assertEqual(count,await detail.locator('.detail-item').count())
        names=await detail.locator('.detail-item').evaluate_all('nodes=>nodes.map(n=>n.dataset.roleName)')
        self.assertEqual({'合成角色%d'%rank for rank in ranks},set(names))
        self.assertEqual(0,await detail.locator('img[alt=weapon][title="烂柯"]').count(),
                         'purple members must never display a legendary card')
        return detail

    async def test_tc40_tc42_both_entrypoints_use_complete_gold_view_and_never_legacy_details(self):
        for entry in ('jjc-ranking-stats.html','index.html'):
            with self.subTest(entry=entry):
                page=await self.new_page(entry)
                self.assertEqual('10',(await self.detail(page).locator('.bar-value').inner_text()).strip())
                self.assertNotIn('999',await page.locator('#grid').inner_text())
                self.assertTrue(self.views(),'complete dynamic view requested on initial page')
                params=self.views()[-1][1]
                self.assertEqual(['2'],params['equipment_contract'])
                self.assertEqual([MEMBER_REVISION],params['expected_member_revision'])
                self.assertEqual(['local_win_v2'],params['expected_frozen_source'])
                await page.locator('[data-tab="purple"]').click()
                detail=await self.assert_purple(page,5,[3,4,5,6,7])
                self.assertRegex(await page.locator('#grid').inner_text(),r'70(?:\.0+)?%')
                self.assertEqual(10,await detail.locator('img[alt=teammate]').count())
                self.assertFalse(any(p.endswith(('/details','/flat-members','/current-displays')) for p,q in self.requests),
                                 'V2 expanding/filtering cannot fetch independently versioned old data')
                self.assertEqual([],self.pageerrors)
                await page.close()

    async def test_tc41_tc45_purple_to_legendary_is_atomic_and_keeps_expansion_mobile(self):
        page=await self.new_page(width=390)
        await page.locator('[data-tab="purple"]').click()
        detail=await self.assert_purple(page,5,[3,4,5,6,7])
        await detail.scroll_into_view_if_needed()
        await page.evaluate(r"""()=>{
          window.__equipmentBadFrames=[];
          window.__equipmentObserver=new MutationObserver(()=>{
            if(document.querySelector('[data-tab=purple]')?.classList.contains('active')) {
              const bad=[...document.querySelectorAll('.detail-item img[alt=weapon]')].some(x=>x.title==='烂柯');
              if(bad)window.__equipmentBadFrames.push('legendary visible in purple');
            }
          });
          window.__equipmentObserver.observe(document.querySelector('#grid'),{childList:true,subtree:true,attributes:true});
        }""")
        rows=ten_member_golden();rows[2]=equipped_member(3,'legendary')
        self.view=make_view(rows,checked_at=CHECKED_AT+30)
        await page.clock.fast_forward(15000)
        await self.settle(page)
        detail=await self.assert_purple(page,4,[4,5,6,7])
        self.assertTrue(await detail.evaluate('el=>el.open'),'whole-view commit must preserve expansion')
        self.assertEqual([],await page.evaluate('window.__equipmentBadFrames'))
        self.assertEqual(self.view['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))
        self.assertLessEqual(await page.evaluate('document.documentElement.scrollWidth'),390)
        self.assertEqual([],self.pageerrors)

    async def test_tc43_tc47_failed_read_retains_whole_then_retirement_restores_frozen(self):
        page=await self.new_page()
        await page.locator('[data-tab="purple"]').click()
        await self.assert_purple(page,5,[3,4,5,6,7])
        rows=ten_member_golden();rows[2]=equipped_member(3,'legendary')
        self.view=make_view(rows,checked_at=CHECKED_AT+30)
        await page.clock.fast_forward(15000);await self.settle(page)
        await self.assert_purple(page,4,[4,5,6,7])
        accepted=self.view['equipment_view_revision']
        self.fail_dynamic=True
        await page.clock.fast_forward(15000);await self.settle(page)
        await self.assert_purple(page,4,[4,5,6,7])
        self.assertEqual(accepted,await page.locator('#grid').get_attribute('data-equipment-revision'))
        self.fail_dynamic=False
        self.response_flags={'retired':True,'overlay_enabled':False,'latest_timestamp':TIMESTAMP+100,'view':None}
        await page.clock.fast_forward(60000);await self.settle(page)
        await self.assert_purple(page,5,[3,4,5,6,7])
        self.assertEqual(self.frozen['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))
        count=len(self.views())
        await page.clock.fast_forward(60000);await self.settle(page)
        self.assertEqual(count,len(self.views()),'retired page must stop dynamic polling')
        self.assertEqual([],self.pageerrors)

    async def test_tc33_tc44_same_timestamp_manifest_reload_and_highest_score_isolation(self):
        page=await self.new_page()
        old_reads=len([1 for p,q in self.requests if q.get('action')==['read']])
        self.bootstrap_manifest={'member_revision':'replacement-members','frozen_source':'local_win_v2'}
        rows=ten_member_golden();rows[0]['name']='替换成员';rows[0]['binding_revision']='replacement-binding'
        self.frozen=make_view(rows,member_revision='replacement-members',checked_at=CHECKED_AT+40)
        self.view=deepcopy(self.frozen)
        # Successful server comparison itself must detect old expected manifest;
        # no per-instance old cache is involved in this fixture.
        await page.clock.fast_forward(15000);await self.settle(page)
        reads=[q for p,q in self.requests if q.get('action')==['read']]
        self.assertGreater(len(reads),old_reads,'new member manifest requires a complete bootstrap reload')
        self.assertEqual(self.view['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))
        detail=self.detail(page)
        if not await detail.evaluate('el=>el.open'):
            await detail.locator('summary').click()
        await self.settle(page)
        self.assertIn('替换成员',await detail.inner_text())
        await page.locator('[data-tab="peak-game"]').click()
        await self.settle(page)
        count=len(self.views())
        await page.clock.fast_forward(60000);await self.settle(page)
        self.assertEqual(count,len(self.views()),'highest-score tab must never use current-win equipment')
        self.assertEqual([],self.pageerrors)

    async def test_u01_reload_bootstrap_failure_retries_thirty_sixty_and_pauses_when_hidden(self):
        for entry in ('jjc-ranking-stats.html','index.html'):
            with self.subTest(entry=entry):
                self.fail_bootstrap=False
                page=await self.new_page(entry)
                # A complete same-timestamp replacement is known to the server;
                # fetching its new bootstrap initially fails.
                old_revision=self.frozen['equipment_view_revision']
                rows=ten_member_golden();rows[0]['name']='重载成功的新成员'
                replacement='members-reloaded-'+entry
                self.frozen=make_view(rows,member_revision=replacement,checked_at=CHECKED_AT+50)
                self.view=deepcopy(self.frozen)
                self.bootstrap_manifest={'member_revision':replacement,'frozen_source':'local_win_v2'}
                self.fail_bootstrap=True
                def reads():
                    return sum(q.get('action')==['read'] for path,q in self.requests)
                previous=reads()
                await page.clock.fast_forward(15000);await self.settle(page)
                self.assertGreater(reads(),previous,'manifest mismatch must attempt new complete bootstrap')
                first=reads()
                await page.clock.fast_forward(30000);await self.settle(page)
                self.assertGreater(reads(),first,'failed bootstrap must retry after 30s')
                second=reads()
                await page.clock.fast_forward(60000);await self.settle(page)
                self.assertGreater(reads(),second,'subsequent failed bootstrap must retry after 60s')
                self.assertNotEqual(old_revision,await page.locator('#grid').get_attribute('data-equipment-revision'))
                self.assertEqual(0,await page.locator('.detail-item,.bar-value').count(),
                                 'known obsolete members and statistics must not be restored while pending')
                await page.locator('.tab-export-btn[data-export-mode="all"]').click()
                self.assertEqual(0,await page.evaluate('window.__equipmentExports.length'),
                                 'obsolete pending membership cannot be exported')
                await page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
                count=reads()
                await page.clock.fast_forward(120000);await self.settle(page)
                self.assertEqual(count,reads(),'hidden pages cannot continue reload retries')
                self.fail_bootstrap=False
                await page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'))")
                await page.wait_for_function("revision=>document.querySelector('#grid')?.dataset.equipmentRevision===revision",
                                             arg=self.view['equipment_view_revision'])
                self.assertGreater(reads(),count,'restoring visibility must resume pending bootstrap')
                self.assertEqual([],self.pageerrors)
                await page.close()

    async def test_u01_switching_snapshot_cancels_old_pending_bootstrap_epoch(self):
        historical=make_view(timestamp=TIMESTAMP-100,member_revision='historical-roster')
        historical_payload=self.bootstrap()
        historical_payload.update(timestamp=TIMESTAMP-100,ranking_cache_time=TIMESTAMP-100,
            equipment_view=historical,equipment_view_revision=historical['equipment_view_revision'],
            member_revision='historical-roster',ranking_display_kind='historical',
            bootstrap_manifest={'member_revision':'historical-roster','frozen_source':'local_win_v2'})
        self.extra_bootstraps[TIMESTAMP-100]=historical_payload
        page=await self.new_page()
        rows=ten_member_golden();rows[0]['name']='主榜已换成员'
        self.frozen=make_view(rows,member_revision='pending-roster',checked_at=CHECKED_AT+50)
        self.view=deepcopy(self.frozen)
        self.bootstrap_manifest={'member_revision':'pending-roster','frozen_source':'local_win_v2'}
        self.fail_bootstrap=True
        await page.clock.fast_forward(15000);await self.settle(page)
        await page.locator('#snapshot-select').select_option(str(TIMESTAMP-100))
        await page.wait_for_function("revision=>document.querySelector('#grid')?.dataset.equipmentRevision===revision",
                                     arg=historical['equipment_view_revision'])
        old_reads=sum(q.get('action')==['read'] and q.get('timestamp')==[str(TIMESTAMP)] for path,q in self.requests)
        self.fail_bootstrap=False
        await page.clock.fast_forward(120000);await self.settle(page)
        self.assertEqual(old_reads,sum(q.get('action')==['read'] and q.get('timestamp')==[str(TIMESTAMP)] for path,q in self.requests))
        self.assertEqual(str(TIMESTAMP-100),await page.locator('#snapshot-select').input_value())
        self.assertEqual(historical['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))
        self.assertEqual([],self.pageerrors)

    async def test_u02_convertible_legacy_waits_for_first_complete_v2_and_never_renders_old_stats_on_error(self):
        self.legacy_bootstrap=True
        self.bootstrap_manifest={'member_revision':MEMBER_REVISION,'frozen_source':'legacy_v1'}
        self.fail_dynamic=True
        page=await self.new_page(wait_for_view=False)
        # A selected option precedes async bootstrap/fetch dispatch. Wait for the
        # actual request and applied error state, rather than an 80ms sleep.
        await asyncio.wait_for(self.equipment_view_requested.wait(),timeout=4)
        await page.wait_for_function("() => typeof equipmentViewState!=='undefined' && equipmentViewState?.failed===true && !equipmentViewState.current")
        self.assertTrue(self.views(),'convertible legacy must attempt whole equipment-view')
        self.assertEqual(0,await page.locator('.bar-value,.detail-item,.ranking-row-list').count(),
                         'no complete V2 view means whole loading/error, not legacy statistics')
        self.assertNotIn('999',await page.locator('#grid').inner_text())
        self.assertIsNone(await page.locator('#grid').get_attribute('data-equipment-revision'))
        await page.locator('.tab-export-btn[data-export-mode="all"]').click()
        self.assertEqual(0,await page.evaluate('window.__equipmentExports.length'))
        self.fail_dynamic=False
        await page.clock.fast_forward(30000);await self.settle(page)
        await page.wait_for_function("revision=>document.querySelector('#grid')?.dataset.equipmentRevision===revision",
                                     arg=self.view['equipment_view_revision'])
        self.assertEqual('10',(await self.detail(page).locator('.bar-value').inner_text()).strip())
        await page.locator('[data-tab="purple"]').click()
        await self.assert_purple(page,5,[3,4,5,6,7])
        self.assertEqual([],self.pageerrors)

    async def test_tc46_export_freezes_entire_revision_and_restores_polling(self):
        page=await self.new_page()
        await page.evaluate('window.__holdEquipmentExport=true')
        before=await page.locator('#grid').get_attribute('data-equipment-revision')
        await page.locator('.export-btn').first.click()
        await page.wait_for_function('window.__equipmentExports?.length===1')
        count=len(self.views())
        rows=ten_member_golden();rows[2]=equipped_member(3,'legendary')
        self.view=make_view(rows,checked_at=CHECKED_AT+50)
        await page.clock.fast_forward(45000);await self.settle(page)
        self.assertEqual(count,len(self.views()),'export must suspend dynamic checks')
        self.assertEqual(before,await page.evaluate('window.__equipmentExports[0].revision'))
        self.assertEqual(before,await page.locator('#grid').get_attribute('data-equipment-revision'))
        async with page.expect_download():
            await page.evaluate('window.__releaseEquipmentExport()')
        await self.settle(page)
        self.assertGreater(len(self.views()),count,'finally must reactivate the whole-view poller')
        self.assertEqual(self.view['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))
        self.assertEqual([],self.pageerrors)

    async def test_u03_confirmed_legacy_fallback_resumes_old_poller_without_manual_tab(self):
        for entry in ('jjc-ranking-stats.html','index.html'):
            with self.subTest(entry=entry):
                self.requests.clear()
                self.legacy_bootstrap=True
                self.legacy_display_enabled=True
                self.bootstrap_manifest={'member_revision':MEMBER_REVISION,'frozen_source':'legacy_v1'}
                self.response_flags={'overlay_enabled':False,'reason':'disabled','view':None}
                page=await self.new_page(entry,wait_for_view=False)
                await page.locator('.detail summary').first.wait_for()
                self.assertGreater(await page.evaluate('window.__legacyDisplayTimers.size'),0,
                    'confirmed legacy fallback must immediately resume the legacy scheduler before user interaction')
                await self.detail(page).locator('summary').click()
                await page.wait_for_function("()=>document.querySelector('.detail[data-range=top_50] .detail-list')?.dataset.loaded === 'true'")
                await self.settle(page)
                legacy=lambda: [x for x in self.requests if x[0].endswith('/current-displays')]
                self.assertGreater(len(legacy()),0,'legacy visible cards must immediately request current-displays')
                before=len(legacy())
                await page.clock.run_for(15001)
                await self.settle(page)
                self.assertGreater(len(legacy()),before,'legacy polling must continue at 15 seconds')
                self.assertEqual([],self.pageerrors)
                await page.close()


    async def test_u04_revoked_convertible_legacy_never_resurrects_on_disabled_or_retired(self):
        for entry,reason,revoked_reason in (('jjc-ranking-stats.html','disabled','binding_invalid'),
                                          ('index.html','retired','data_inconsistent'),
                                          ('jjc-ranking-stats.html','disabled','no_local_win')):
            with self.subTest(entry=entry,reason=reason):
                self.requests.clear()
                self.legacy_bootstrap=True
                self.response_flags={}
                self.bootstrap_manifest={'member_revision':MEMBER_REVISION,'frozen_source':'legacy_v1'}
                page=await self.new_page(entry)
                member=self.view['members'][0]
                instruction={'timestamp':self.view['timestamp'],'scope':deepcopy(self.view['scope']),
                    'member_revision':MEMBER_REVISION,'rank':member['rank'],
                    'binding_revision':member['binding_revision'],'confirmed_at':CHECKED_AT+100,
                    'reason':revoked_reason}
                self.response_flags={'revocations':[instruction]}
                await page.clock.fast_forward(15000);await self.settle(page)
                detail=self.detail(page)
                await detail.locator('summary').click()
                await self.settle(page)
                revoked=detail.locator('.detail-item[data-role-name="合成角色1"]')
                self.assertEqual(1,await revoked.count())
                self.assertEqual(0,await revoked.locator('img[alt=weapon]').count())
                self.response_flags={'overlay_enabled':False,'view':None,'reason':reason,'retired':reason=='retired'}
                await page.clock.fast_forward(15000);await self.settle(page)
                self.assertEqual(0,await page.locator('.bar-value,.detail-item,.ranking-row-list').count(),
                    'legacy cannot restore old statistics or cards after an applicable confirmed revocation')
                self.assertIsNone(await page.locator('#grid').get_attribute('data-equipment-revision'))
                await page.locator('.tab-export-btn[data-export-mode="all"]').click()
                self.assertEqual(0,await page.evaluate('window.__equipmentExports.length'),
                    'unsafe legacy fallback must remain unexportable')
                await page.clock.fast_forward(60000);await self.settle(page)
                self.assertEqual(0,await page.locator('.bar-value,.detail-item').count())
                self.assertFalse(any(path.endswith('/current-displays') for path,q in self.requests))
                self.assertEqual([],self.pageerrors)
                await page.close()


    async def test_u04_first_dynamic_failure_revocation_blocks_legacy_fallback(self):
        for entry in ('jjc-ranking-stats.html','index.html'):
            with self.subTest(entry=entry):
                self.requests.clear()
                self.legacy_bootstrap=True
                self.bootstrap_manifest={'member_revision':MEMBER_REVISION,'frozen_source':'legacy_v1'}
                member=self.view['members'][0]
                instruction={'timestamp':self.view['timestamp'],'scope':deepcopy(self.view['scope']),
                    'member_revision':MEMBER_REVISION,'rank':member['rank'],
                    'binding_revision':member['binding_revision'],'confirmed_at':CHECKED_AT+100,'reason':'binding_invalid'}
                self.fail_dynamic=True
                self.response_flags={'revocations':[instruction]}
                page=await self.new_page(entry,wait_for_view=False)
                self.assertEqual(0,await page.locator('.bar-value').count())
                self.fail_dynamic=False
                self.response_flags={'overlay_enabled':False,'reason':'disabled','view':None}
                await page.clock.fast_forward(60000);await self.settle(page)
                self.assertEqual(0,await page.locator('.bar-value,.detail-item').count(),
                    'bootstrap membership must fence confirmed revocations even before the first successful dynamic view')
                await page.locator('.tab-export-btn[data-export-mode="all"]').click()
                self.assertEqual(0,await page.evaluate('window.__equipmentExports.length'))
                self.assertFalse(any(path.endswith('/current-displays') for path,q in self.requests))
                self.assertEqual([],self.pageerrors)
                await page.close()


if __name__=='__main__':
    unittest.main()
