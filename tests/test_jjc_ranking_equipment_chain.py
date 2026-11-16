"""E7: isolated Mongo4.4 -> actual repositories/service/ASGI -> both real HTMLs.

Only assembly (real components bound to one owned DB), external traffic, and QQ
send are substituted. Equipment builders, aggregation, publication, router,
shared JS, HTML, html2canvas and template screenshot rendering are real.
"""
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

from bson import BSON, ObjectId
import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from playwright.async_api import async_playwright, Browser
from pymongo.monitoring import CommandListener
from jinja2 import Environment, FileSystemLoader

import config
from src.services.jx3.jjc_ranking_equipment import equipment_view_statistics
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from src.services.jx3.jjc_ranking_display import JjcRankingDisplayService
from src.services.jx3.jjc_kungfu_policy import KungfuPolicy
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.renderers.jx3.jjc_ranking import render_combined_ranking_image, send_split_ranking_images
from src.renderers.jx3.image import render_template_image
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import create_equipment_source_indexes
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TIMESTAMP, frozen_member, weapon

ROOT=Path(__file__).resolve().parents[1]
CANVAS=Path('/tmp/jjc-equipment-test-20261113/html2canvas-1.4.1.min.js')
WRITE_COMMANDS={'insert','update','delete','findAndModify','create','createIndexes','drop','dropDatabase','renameCollection'}
PNG=b'\x89PNG\r\n\x1a\n'


class RecordingCommands(CommandListener):
    def __init__(self):
        self.events=[]
    def started(self,event):
        self.events.append({'name':event.command_name,'database':event.database_name})
    def succeeded(self,event):
        pass
    def failed(self,event):
        pass


class Clock:
    def __init__(self):
        self.now=float(CHECKED_AT)
        self.mono=100.0
    def wall(self):
        return self.now
    def monotonic(self):
        return self.mono
    def advance(self,seconds=20):
        self.now+=seconds
        self.mono+=seconds


def scene(rank, *, match_id=None, match_time=None, state='purple', equipment_hash=None):
    target=frozen_member(rank)
    gid=target['global_id']
    people=[]
    for index in range(6):
        people.append({'global_id':gid if index==0 else str(800000000000000000+rank*100+index),
            'role_id':str(10000+rank*10+index),'role_name':target['name'] if index==0 else '合成队友%d-%d'%(rank,index),
            'server':target['server'],'zone':'合成大区','kungfu_id':[10145,10786,10626,10003,10176,10698][index],
            'armors':[weapon(state)] if index==0 and state!='unknown' else ([weapon()] if index else [])})
    if equipment_hash:
        people[0].pop('armors',None)
        people[0]['equipment_snapshot_hash']=equipment_hash
    mid=match_id if match_id is not None else 1000+rank
    when=match_time if match_time is not None else SCOPE['season_start']+rank*10
    return {'match_id':mid,'cached_at':CHECKED_AT-5,'data':{'match_id':mid,'match_time':when,
        'detail':{'match_id':mid,'season_id':SCOPE['season_id'],'basic_info':{'match_type':3},
                  'team1':{'won':True,'players_info':people[:3]},
                  'team2':{'won':False,'players_info':people[3:]}}}}


@unittest.skipUnless(os.environ.get('JJC_TEST_MONGO_URI'),'explicit isolated local Mongo URI required')
class EquipmentRealChainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri=checked_test_uri(os.environ['JJC_TEST_MONGO_URI'])
        parsed=urlparse(self.uri)
        self.assertIn(parsed.hostname,{'127.0.0.1','localhost','::1'})
        self.assertEqual(27029,parsed.port)
        self.db_name='jjc_kungfu_test_'+uuid4().hex
        self.commands=RecordingCommands()
        self.client=AsyncIOMotorClient(self.uri,maxPoolSize=4,serverSelectionTimeoutMS=1500,
            connectTimeoutMS=1500,socketTimeoutMS=2000,event_listeners=[self.commands])
        self.db=self.client[self.db_name]
        self.addAsyncCleanup(self.cleanup_db)
        info=await self.client.server_info()
        self.assertTrue(info['version'].startswith('4.4.'),'Mongo4.4 evidence is required, not a newer-version substitute')
        self.mongo_version=info['version']
        self.clock=Clock()
        self.stats=JjcRankingStatsRepo(db=self.db,suppress_errors=False)
        self.identities=RoleIdentityRepo(db=self.db)
        self.snapshots=JjcMatchSnapshotRepo(db=self.db)
        self.inspect=JjcInspectRepo(db=self.db,snapshot_repo=self.snapshots)
        self.service=JjcRankingEquipmentViewService(stats_repo=self.stats,identity_repo=self.identities,
            inspect_repo=self.inspect,snapshot_repo=self.snapshots,scope=deepcopy(SCOPE),enabled=True,
            overlay_enabled=True,clock=self.clock.wall,monotonic=self.clock.monotonic)
        self.addAsyncCleanup(self.service.close)
        await self.db.jjc_ranking_stat_summaries.create_index('timestamp',unique=True)
        await self.db.jjc_match_detail.create_index('match_id',unique=True)
        for side in ('team1','team2'):
            await self.db.jjc_match_detail.create_index('data.detail.'+side+'.players_info.global_id')
            await self.db.jjc_match_detail.create_index([('data.detail.'+side+'.players_info.global_id',1),('_id',1)],
                name='idx_equipment_'+side+'_global_source')
        await create_equipment_source_indexes(self.db)
        await self.db.jjc_equipment_snapshot.create_index('snapshot_hash',unique=True)
        self.members=[]
        for rank in range(1,11):
            member=frozen_member(rank)
            object_id=ObjectId()
            member.update(identity_id=str(object_id),display_identity_id=str(object_id))
            self.members.append(member)
            await self.db.role_identities.insert_one({'_id':object_id,'global_id':member['global_id'],
                'name':member['name'],'server':member['server'],**deepcopy(member['display_locator'])})
            state='legendary' if rank<=2 else 'purple' if rank<=7 else 'unknown'
            doc=scene(rank,state=state,equipment_hash='late-equipment-rank8' if rank==8 else None)
            await self.db.jjc_match_detail.insert_one(doc)
        frozen=await self.service.build_frozen_view(timestamp=TIMESTAMP,members=self.members)
        expected=frozen.pop('_publication_expected')
        summary={'timestamp':TIMESTAMP,'schema_version':2,'current_season':SCOPE['season_id'],
            'default_week':1,'week_info':'第1周','ranking_display_kind':'current',
            'generated_at':CHECKED_AT,'ranking_cache_time':TIMESTAMP,'kungfu_cache_scope':deepcopy(SCOPE),
            'member_revision':frozen['member_revision'],'equipment_source':'local_win_v2','equipment_complete':True,
            'equipment_view_revision':frozen['equipment_view_revision'],'equipment_view':frozen}
        result=await self.stats.publish_equipment_snapshot(TIMESTAMP,summary,**expected)
        self.assertEqual('published',result['status'])
        self.frozen=await self.stats.load_complete_view(TIMESTAMP)
        self.assertEqual([10,2,5,3],[self.frozen['ranges']['top_50'][k] for k in ('N','L','P','U')])
        self.api_events=[]
        self.route_errors=[]
        self.page_errors=[]
        self.asset_urls=[]
        self.outputs=[]
        base=Path(os.environ.get('JJC_EQUIPMENT_EVIDENCE_DIR','/tmp/jjc-equipment-test-20261113/chain-evidence'))
        self.evidence=base/('python-%d.%d'%sys.version_info[:2])
        self.evidence.mkdir(parents=True,exist_ok=True)
        self.addCleanup(lambda: self.write_evidence('observed',{
            'mongo':self.mongo_version,'database':self.db_name,'api_events':self.api_events,
            'route_errors':self.route_errors,'page_errors':self.page_errors,'outputs':self.outputs}))
        self.patches=[]
        for name,value in [('JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED',1),('JJC_RANKING_DISPLAY_OVERLAY_ENABLED',1)]:
            flag=patch.object(config,name,value,create=True)
            flag.start();self.addCleanup(flag.stop)
        singletons=types.ModuleType('src.services.jx3.singletons')
        singletons.jjc_ranking_equipment_view_service=self.service
        singletons.jjc_ranking_equipment_service=self.service
        singletons.jjc_ranking_inspect_service=types.SimpleNamespace()
        singletons.jjc_ranking_display_service=JjcRankingDisplayService(self.stats,self.identities,KungfuPolicy(SCOPE),True,self.clock.wall)
        namespace=types.ModuleType('src.api');namespace.__path__=[str(ROOT/'src/api')]
        modules=patch.dict(sys.modules,{'src.services.jx3.singletons':singletons,'src.api':namespace})
        modules.start();self.addCleanup(modules.stop)
        spec=importlib.util.spec_from_file_location('_equipment_chain_router_'+uuid4().hex,ROOT/'src/api/routers/jjc_ranking_stats.py')
        self.router_module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.router_module)
        repos=patch.object(self.router_module,'JjcRankingStatsRepo',return_value=self.stats)
        repos.start();self.addCleanup(repos.stop)
        app=FastAPI();app.include_router(self.router_module.router)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local-asgi.invalid')
        self.addAsyncCleanup(self.http.aclose)
        self.playwright=await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser=await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.assertTrue(CANVAS.is_file(),'real locally prepared html2canvas is required')

    async def cleanup_db(self):
        try:
            self.assertTrue(self.db_name.startswith('jjc_kungfu_test_') and len(self.db_name)==len('jjc_kungfu_test_')+32)
            await self.client.drop_database(self.db_name)
        finally:
            self.client.close()

    async def fingerprint(self):
        result={}
        for name in ('role_identities','jjc_match_detail','jjc_equipment_snapshot','jjc_ranking_stat_summaries',
                     'jjc_ranking_stat_details','jjc_kungfu_refresh_queue','jjc_sync_queue','jjc_display_match_prefetch'):
            docs=await self.db[name].find({}).sort('_id',1).to_list(None)
            result[name]=hashlib.sha256(BSON.encode({'rows':docs})).hexdigest()
        return result

    def assert_read_window(self,start):
        events=self.commands.events[start:]
        self.assertFalse([e for e in events if e['database']==self.db_name and e['name'] in WRITE_COMMANDS],
                         'the actual GET chain must issue zero Mongo writes')
        self.assertFalse([e for e in events if e['database'] not in {self.db_name,'admin'}],
                         'all Mongo traffic must stay in the one owned namespace')

    async def page(self,entry,width):
        context=await self.browser.new_context(viewport={'width':width,'height':950},accept_downloads=True)
        self.addAsyncCleanup(context.close)
        page=await context.new_page();page.set_default_timeout(6000)
        page.on('pageerror',lambda error:self.page_errors.append(str(error)))
        await page.clock.install()
        async def route(route):
            parsed=urlparse(route.request.url);path=parsed.path
            if '/api/jjc/' in path:
                api_path=path[len('/jx3bot'):] if path.startswith('/jx3bot/') else path
                try:
                    result=await self.http.request(route.request.method,api_path,params=parsed.query,
                                                   content=route.request.post_data)
                    body=result.json()
                    self.api_events.append({'entry':entry,'path':api_path,'status':result.status_code,'body':body})
                    await route.fulfill(status=result.status_code,body=result.content,content_type='application/json')
                except Exception as exc:
                    self.route_errors.append(type(exc).__name__+': '+str(exc))
                    await route.fulfill(status=500,body='actual local ASGI error')
                return
            headers={'Access-Control-Allow-Origin':'*'}
            if path.endswith('.html'):
                await route.fulfill(body=(ROOT/'public'/Path(path).name).read_text(),content_type='text/html',headers=headers)
            elif path.endswith('jjc-equipment-view.js'):
                await route.fulfill(body=(ROOT/'public/jjc-equipment-view.js').read_text(),content_type='application/javascript',headers=headers)
            elif 'html2canvas' in path:
                await route.fulfill(body=CANVAS.read_text(),content_type='application/javascript',headers=headers)
            elif path.endswith('.css'):
                file=ROOT/'public'/Path(path).name
                await route.fulfill(body=file.read_text() if file.exists() else '',content_type='text/css',headers=headers)
            elif '/api/announcements' in path:
                await route.fulfill(body=json.dumps({'status_code':0,'data':{'announcement':None,'items':[],'has_more':False}}),
                                    content_type='application/json',headers=headers)
            else:
                self.asset_urls.append(route.request.url)
                await route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="#64a87a"/></svg>',
                                    content_type='image/svg+xml',headers=headers)
        await context.route('**/*',route)
        await page.goto('https://equipment-chain.fixture/'+entry)
        await page.locator('#grid[data-equipment-revision]').wait_for()
        await page.wait_for_timeout(100)
        return page

    def detail(self,page):
        return page.locator('.detail[data-range="top_50"][data-lane="dps"]').first

    async def assert_p(self,page,ranks):
        detail=self.detail(page)
        self.assertEqual(str(len(ranks)),(await detail.locator('.bar-value').inner_text()).strip())
        if not await detail.evaluate('el=>el.open'):
            await detail.locator('summary').click()
        await page.wait_for_timeout(80)
        self.assertEqual(len(ranks),await detail.locator('.detail-item').count())
        names=await detail.locator('.detail-item').evaluate_all('rows=>rows.map(row=>row.dataset.roleName)')
        self.assertEqual({'合成角色%d'%rank for rank in ranks},set(names))
        self.assertEqual(0,await detail.locator('img[alt=weapon][title="烂柯"]').count())

    async def assert_api_revision(self,page,entry,expected):
        # Real Motor/ASGI work cannot be synchronized by an arbitrary browser
        # 80ms sleep. Await the actual response and its DOM commit within a bound.
        deadline=asyncio.get_running_loop().time()+6
        latest=None
        while asyncio.get_running_loop().time()<deadline:
            views=[e['body']['data']['view'] for e in self.api_events if e['entry']==entry
                   and e['path'].endswith('/equipment-view') and e['status']==200
                   and isinstance(e['body'].get('data'),dict) and isinstance(e['body']['data'].get('view'),dict)]
            if views:
                latest=views[-1]
                if expected==[latest['ranges']['top_50'][k] for k in ('N','L','P','U')]:
                    break
            await asyncio.sleep(.02)
        self.assertIsNotNone(latest,'browser must use the actual ASGI equipment-view endpoint')
        self.assertEqual(expected,[latest['ranges']['top_50'][k] for k in ('N','L','P','U')])
        await page.wait_for_function("revision=>document.querySelector('#grid')?.dataset.equipmentRevision===revision",
                                     arg=latest['equipment_view_revision'],timeout=6000)
        self.assertEqual(latest['equipment_view_revision'],await page.locator('#grid').get_attribute('data-equipment-revision'))

    def check_png(self,path):
        data=path.read_bytes();self.assertTrue(data.startswith(PNG))
        width,height=struct.unpack('>II',data[16:24])
        self.assertGreaterEqual(width,300);self.assertGreaterEqual(height,100)
        self.outputs.append({'path':str(path),'width':width,'height':height,
                             'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})

    async def test_tc49_two_actual_pages_follow_same_match_snapshot_then_new_win_with_real_png_exports(self):
        before=await self.fingerprint();start=len(self.commands.events)
        pages=[]
        for entry,width in [('jjc-ranking-stats.html',1440),('index.html',390)]:
            page=await self.page(entry,width);pages.append((entry,page))
            await self.assert_api_revision(page,entry,[10,2,5,3])
            await page.locator('[data-tab="purple"]').click()
            await self.assert_p(page,[3,4,5,6,7])
            await page.screenshot(path=str(self.evidence/(entry+'-initial-purple.png')),full_page=True)
        self.assert_read_window(start);self.assertEqual(before,await self.fingerprint())
        # A referenced equipment snapshot arrives for the SAME already selected
        # rank8 winning match: U -> P must update all four ranges and both pages.
        await self.db.jjc_equipment_snapshot.insert_one({'snapshot_hash':'late-equipment-rank8','armors':[weapon()]})
        self.clock.advance(20)
        before=await self.fingerprint();start=len(self.commands.events)
        for entry,page in pages:
            await page.clock.fast_forward(15000);await page.wait_for_timeout(100)
            await self.assert_api_revision(page,entry,[10,2,6,2]);await self.assert_p(page,[3,4,5,6,7,8])
        self.assert_read_window(start);self.assertEqual(before,await self.fingerprint())
        # A newer winning match changes rank3 P -> L. No prepared API or page
        # state is supplied: the real repository/service must find the new row.
        await self.db.jjc_match_detail.insert_one(scene(3,match_id=9003,
            match_time=CHECKED_AT+5,state='legendary'))
        self.clock.advance(20)
        before=await self.fingerprint();start=len(self.commands.events)
        for entry,page in pages:
            await page.clock.fast_forward(15000);await page.wait_for_timeout(100)
            await self.assert_api_revision(page,entry,[10,3,5,2]);await self.assert_p(page,[4,5,6,7,8])
            await page.screenshot(path=str(self.evidence/(entry+'-updated-purple.png')),full_page=True)
            async with page.expect_download() as download_info:
                await page.locator('.export-btn').first.click()
            download=await download_info.value
            output=self.evidence/(entry+'-real-range-export.png')
            await download.save_as(str(output));self.check_png(output)
        # An actual html2canvas whole-page export, independently from screenshots.
        entry,page=pages[0]
        async with page.expect_download() as download_info:
            await page.locator('.tab-export-btn[data-export-mode="purple"]').click()
        output=self.evidence/'desktop-real-whole-export.png'
        await (await download_info.value).save_as(str(output));self.check_png(output)
        self.assert_read_window(start);self.assertEqual(before,await self.fingerprint())
        self.assertEqual([],self.route_errors);self.assertEqual([],self.page_errors)
        self.assertFalse([e for e in self.api_events if e['path'].endswith(('/details','/flat-members','/current-displays'))],
                         'V2 page must never request separately versioned legacy card data')
        self.write_evidence('chain',{'mongo':self.mongo_version,'outputs':self.outputs,
            'api_events':self.api_events,'read_only':True,'database':self.db_name,
            'sources':[str(ROOT/'public/jjc-ranking-stats.html'),str(ROOT/'public/index.html'),
                       str(ROOT/'public/jjc-equipment-view.js'),str(CANVAS)]})

    async def test_tc30_real_combined_and_eight_split_qq_pngs_without_any_send(self):
        context=await self.service.read_frozen_snapshot(TIMESTAMP)
        stats=context['kungfu_statistics']
        self.assertEqual(.2,stats['top_50']['equipment_metrics']['legendary_ratio'])
        self.assertEqual(.7,stats['top_50']['equipment_metrics']['equipment_coverage'])
        env=Environment(loader=FileSystemLoader(str(ROOT/'templates')))
        rendered=[];network=[]
        original_new_page=Browser.new_page
        async def isolated_new_page(browser,*args,**kwargs):
            page=await original_new_page(browser,*args,**kwargs)
            async def deny(route):
                network.append(route.request.url)
                await route.abort()
            await page.route('**/*',deny)
            return page
        async def record_render(environment,template,values,**kwargs):
            image=await render_template_image(environment,template,values,**kwargs)
            output=self.evidence/('qq-%02d.png'%len(rendered))
            output.write_bytes(image);self.check_png(output)
            rendered.append({'template':template,'context':deepcopy(values),'path':str(output)})
            return image
        sender=types.SimpleNamespace(send=AsyncMock())
        with patch.object(Browser,'new_page',isolated_new_page):
            combined=await render_combined_ranking_image(env=env,render_template_image=record_render,
                current_season=SCOPE['season_id'],stats=stats,week_info='第1周',show_legendary=True)
            self.assertTrue(combined['image_bytes'].startswith(PNG))
            await send_split_ranking_images(sender,types.SimpleNamespace(user_id=1),env=env,
                render_template_image=record_render,current_season=SCOPE['season_id'],stats=stats,
                week_info='第1周',show_legendary=True)
        self.assertEqual(9,len(rendered),'one real combined and eight real split images')
        self.assertEqual(9,sender.send.await_count,'all send calls stay in the mock')
        self.assertEqual([],network,'QQ template rendering may not access remote resources')
        self.write_evidence('qq-observed',{'mongo':self.mongo_version,'outputs':self.outputs,'rendered':rendered,
            'view_revision':self.frozen['equipment_view_revision'],'send_mocked':True})
        for item in rendered:
            values=item['context']
            self.assertFalse(values.get('equipment_incomplete'))
            self.assertTrue(values['show_legendary'])
            labels=' '.join(values.get('coverage_labels',{}).values())+' '+values.get('coverage_label','')
            self.assertRegex(labels,r'70(?:\.0+)?%')
            self.assertIn('3',labels)
            for key,data in values.items():
                if key.endswith('_dps'):
                    self.assertEqual(20.0,data[0][4])
        self.write_evidence('qq',{'mongo':self.mongo_version,'outputs':self.outputs,'rendered':rendered,
            'view_revision':self.frozen['equipment_view_revision'],'send_mocked':True})

    def write_evidence(self,name,data):
        (self.evidence/(name+'-evidence.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n')


if __name__=='__main__':
    unittest.main()
