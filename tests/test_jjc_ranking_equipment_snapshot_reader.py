"""DEP13 actual API/frozen reader boundaries; no production/default DB access."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock,patch
import config
from src.services.jx3.jjc_ranking_equipment_view import EquipmentViewError
from src.storage.mongo_repos.jjc_ranking_stats_repo import EquipmentSnapshotError
from tests import test_jjc_ranking_equipment_router as router_fixture
from tests.test_jjc_ranking_equipment_assembly import actual_equipment_assembly,Client
from tests.jjc_equipment_fixtures import TIMESTAMP

class SnapshotReaderRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await router_fixture.EquipmentRouterAsgiTests.asyncSetUp(self)
        self.ordinary=self.stats;self.dedicated=router_fixture.RouteStats()
        self.service.dynamic_stats_repo=self.dedicated
        self.service.enabled=lambda:getattr(config,'JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED',0)==1
        for label,repo in (('ordinary',self.ordinary),('dedicated',self.dedicated)):
            repo.list_seasons=AsyncMock(return_value={'seasons':[{'name':label}]})
            repo.list_season_history=AsyncMock(return_value={'items':[{'label':label}],'total':1})
            repo.list_timestamps=AsyncMock(side_effect=lambda *args,_label=label,**kwargs:({'items':[{'label':_label}],'page':1,'page_size':20,'total':1,'has_more':False} if kwargs else [_label]))
    def modes(self):
        return [('seasons',{},'list_seasons'),('season-history',{'season':'synthetic'},'list_season_history'),('list',{},'list_timestamps'),('list',{'page':1},'list_timestamps'),('list',{'with_meta':'true'},'list_timestamps')]
    async def test_local_enabled_routes_every_metadata_mode_to_dedicated_independent_flags(self):
        for query in (0,1):
            for overlay in (0,1):
                with patch.object(config,'JJC_RANKING_DISPLAY_QUERY_ENABLED',query,create=True),patch.object(config,'JJC_RANKING_DISPLAY_OVERLAY_ENABLED',overlay,create=True):
                    for action,params,method in self.modes():
                        with self.subTest(query=query,overlay=overlay,action=action,params=params):
                            response=await self.client.get('/api/jjc/ranking-stats',params={'action':action,**params})
                            self.assertEqual(200,response.status_code)
                            self.assertIn('dedicated',response.text);self.assertNotIn('ordinary',response.text)
        for method in ('list_seasons','list_season_history','list_timestamps'):self.assertEqual(0,getattr(self.ordinary,method).await_count)
    async def test_local_disabled_routes_metadata_to_ordinary_without_optional_use(self):
        for method in ('list_seasons','list_season_history','list_timestamps'):getattr(self.dedicated,method).side_effect=AssertionError('optional read forbidden while LOCAL0')
        with patch.object(config,'JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED',0):
            for action,params,method in self.modes():
                response=await self.client.get('/api/jjc/ranking-stats',params={'action':action,**params})
                self.assertEqual(200,response.status_code);self.assertIn('ordinary',response.text)
        for method in ('list_seasons','list_season_history','list_timestamps'):self.assertEqual(0,getattr(self.dedicated,method).await_count)
    async def test_dedicated_metadata_failure_is_503_without_ordinary_fallback(self):
        for method in ('list_seasons','list_season_history','list_timestamps'):getattr(self.dedicated,method).side_effect=EquipmentSnapshotError('read_failed')
        for action,params,method in self.modes():
            response=await self.client.get('/api/jjc/ranking-stats',params={'action':action,**params})
            self.assertEqual(503,response.status_code);self.assertEqual('read_failed',response.json()['status_msg'])
        for method in ('list_seasons','list_season_history','list_timestamps'):self.assertEqual(0,getattr(self.ordinary,method).await_count)
        with patch.object(self.module,'_equipment_service',return_value=None):
            response=await self.client.get('/api/jjc/ranking-stats',params={'action':'seasons'})
            self.assertEqual(503,response.status_code)
            self.assertEqual('read_failed',response.json()['status_msg'])
        self.assertEqual(0,self.ordinary.list_seasons.await_count)
    async def test_generic_no_code_metadata_and_frozen_failures_are_controlled_without_fallback(self):
        for method in ('list_seasons','list_season_history','list_timestamps'):getattr(self.dedicated,method).side_effect=RuntimeError('synthetic-native-runtime')
        for action,params,method in self.modes():
            response=await self.client.get('/api/jjc/ranking-stats',params={'action':action,**params})
            self.assertEqual(503,response.status_code);self.assertEqual('read_failed',response.json()['status_msg'])
        self.dedicated.load_frozen_equipment_context=AsyncMock(side_effect=RuntimeError('synthetic-context-runtime'))
        with self.assertRaises(EquipmentViewError) as error:await self.service.read_frozen_snapshot(TIMESTAMP)
        self.assertEqual('read_failed',error.exception.code);self.assertEqual([],self.ordinary.calls)
        for method in ('list_seasons','list_season_history','list_timestamps'):self.assertEqual(0,getattr(self.ordinary,method).await_count)

    async def test_cancelled_metadata_and_frozen_reads_propagate_without_fallback(self):
        self.dedicated.list_seasons.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.client.get('/api/jjc/ranking-stats',params={'action':'seasons'})
        self.dedicated.load_frozen_equipment_context=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await self.service.read_frozen_snapshot(TIMESTAMP)
        self.assertEqual(0,self.ordinary.list_seasons.await_count);self.assertEqual([],self.ordinary.calls)

    async def test_protocol_checks_and_noncontract_summary_preload_use_selected_reader(self):
        self.ordinary.load_summary=AsyncMock(side_effect=AssertionError('ordinary protocol preload forbidden'))
        self.dedicated.load_summary=AsyncMock(wraps=self.dedicated.load_summary)
        paths=[('/api/jjc/ranking-stats',{'action':'read','timestamp':TIMESTAMP}),('/api/jjc/ranking-stats/details',{'timestamp':TIMESTAMP,'range':'top_1000','lane':'dps','kungfu':'山居剑意'}),('/api/jjc/ranking-stats/flat-members',{'timestamp':TIMESTAMP,'range':'top_1000'})]
        for path,params in paths:
            response=await self.client.get(path,params=params)
            self.assertEqual(409,response.status_code);self.assertEqual('equipment_contract_upgrade_required',response.json()['status_msg'])
        self.assertEqual(0,self.ordinary.load_summary.await_count);self.assertEqual(3,self.dedicated.load_summary.await_count)
        self.dedicated.load_summary.side_effect=RuntimeError('synthetic-summary-runtime')
        for path,params in paths:
            response=await self.client.get(path,params=params)
            self.assertEqual(503,response.status_code);self.assertEqual('read_failed',response.json()['status_msg'])
        self.assertEqual(0,self.ordinary.load_summary.await_count)

    async def test_local_disabled_metadata_never_even_resolves_optional_service(self):
        with patch.object(config,'JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED',0),patch.object(self.module,'_equipment_service',side_effect=AssertionError('LOCAL0 optional assembly resolution forbidden')):
            for action,params,method in self.modes():
                response=await self.client.get('/api/jjc/ranking-stats',params={'action':action,**params})
                self.assertEqual(200,response.status_code);self.assertIn('ordinary',response.text)

    async def test_frozen_snapshot_uses_selected_repo_with_identical_contract_and_no_fallback(self):
        expected=await self.service.read_frozen_snapshot(TIMESTAMP)
        self.ordinary.calls.clear();self.dedicated.calls.clear()
        actual=await self.service.read_frozen_snapshot(TIMESTAMP)
        self.assertEqual(expected,actual)
        self.assertEqual([],self.ordinary.calls)
        self.assertEqual([('context',TIMESTAMP)],self.dedicated.calls)
        self.dedicated.load_frozen_equipment_context=AsyncMock(side_effect=EquipmentSnapshotError('read_failed'))
        with self.assertRaises(EquipmentViewError):await self.service.read_frozen_snapshot(TIMESTAMP)
        self.assertEqual([],self.ordinary.calls)
        with patch.object(config,'JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED',0):
            self.assertEqual(expected,await self.service.read_frozen_snapshot(TIMESTAMP))
        self.assertEqual([('context',TIMESTAMP)],self.ordinary.calls)
    async def test_selected_frozen_payload_still_rejects_mismatched_revision(self):
        self.ordinary.calls.clear();self.dedicated.calls.clear()
        with self.assertRaises(EquipmentViewError):await self.service.read_frozen_snapshot(TIMESTAMP,expected_view_revision='mismatch')
        self.assertEqual([],self.ordinary.calls);self.assertEqual([('context',TIMESTAMP)],self.dedicated.calls)

class SnapshotReaderAssembly(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_selector_is_flag_aware_lazy_and_keeps_publication_repo(self):
        calls=[];client=Client()
        def factory(uri,**kwargs):calls.append((uri,kwargs));return client
        namespace,cfg=actual_equipment_assembly(factory)
        service=namespace['jjc_ranking_equipment_service'];runtime=namespace['jjc_equipment_read_runtime']
        self.addAsyncCleanup(runtime.close);self.addAsyncCleanup(service.close)
        original=service.stats_repo;dedicated=service.dynamic_stats_repo
        select=getattr(service,'snapshot_read_repo',None)
        self.assertTrue(callable(select),'flag-aware snapshot reader selection required')
        self.assertIs(original,select());self.assertEqual([],calls)
        for overlay in (0,1):
            cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=1;cfg.JJC_RANKING_DISPLAY_OVERLAY_ENABLED=overlay
            self.assertIs(dedicated,select());self.assertIs(original,service.stats_repo)
        cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=0
        self.assertIs(original,select());self.assertEqual([],calls)
        self.assertLessEqual(service.build_timeout,8);self.assertLessEqual(service.request_timeout,10)
