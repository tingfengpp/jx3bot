"""Controlled real assembly/hook execution, without importing the bot launcher.

The production AST statements/function bodies execute unchanged. Only client
factory/config and external stop functions are injected, so no real bot,
workers, environment URI or database can be touched.
"""
import ast
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from src.infra.jjc_equipment_read_runtime import EquipmentReadRuntime
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from tests.jjc_equipment_fixtures import SCOPE, TIMESTAMP, MEMBER_REVISION
from tests.test_jjc_ranking_equipment_view import (
    Clock, FakeStats, FakeIdentities, FakeInspect, FakeSnapshots,
)

ROOT = Path(__file__).resolve().parents[1]


class Database:
    def __init__(self):
        self.collections = {"jjc_match_detail": object(), "jjc_equipment_snapshot": object(),
                            "role_identities": object(), "jjc_ranking_stat_summaries": object()}

    def __getattr__(self, name):
        if name not in self.collections:
            raise AssertionError("Unexpected collection: " + name)
        return self.collections[name]

    def __getitem__(self, name):
        return getattr(self, name)


class Client:
    def __init__(self, events=None, inspect_repo=None):
        self.db = Database()
        self.events = events if events is not None else []
        self.inspect_repo = inspect_repo
        self.close_calls = 0

    def get_default_database(self):
        return self.db

    def close(self):
        if self.inspect_repo is not None and self.inspect_repo.active:
            raise AssertionError("Optional client was closed before service work drained")
        self.close_calls += 1
        self.events.append("equipment-client")


def actual_equipment_assembly(factory, host_override=""):
    tree = ast.parse((ROOT / "src/services/jx3/singletons.py").read_text())
    names = {"jjc_equipment_read_runtime", "_equipment_db", "jjc_ranking_equipment_service"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_EquipmentReadDatabase":
            selected.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            selected.append(node)
    if len(selected) != 4:
        raise AssertionError("Equipment assembly boundary changed; update explicit real-code coverage")
    cfg = types.SimpleNamespace(JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=0,
        JJC_RANKING_DISPLAY_OVERLAY_ENABLED=1, JJC_EQUIPMENT_MONGO_HOST=host_override, MONGO_URI="mongodb://127.0.0.1:27029/jjc_kungfu_test")
    namespace = {"cfg": cfg, "EquipmentReadRuntime": EquipmentReadRuntime,
        "JjcRankingEquipmentViewService": JjcRankingEquipmentViewService,
        "JjcRankingStatsRepo": JjcRankingStatsRepo, "RoleIdentityRepo": RoleIdentityRepo,
        "JjcInspectRepo": JjcInspectRepo, "JjcMatchSnapshotRepo": JjcMatchSnapshotRepo,
        "policy_from_config": lambda: deepcopy(SCOPE)}
    module = ast.Module(body=selected, type_ignores=[])
    with patch("src.infra.jjc_equipment_read_runtime.AsyncIOMotorClient", factory):
        exec(compile(ast.fix_missing_locations(module), str(ROOT / "src/services/jx3/singletons.py"), "exec"), namespace)
    return namespace, cfg


def actual_shutdown(service=None, runtime=None, events=None):
    events = events if events is not None else []
    tree = ast.parse((ROOT / "bot.py").read_text())
    nodes = [deepcopy(n) for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
             and n.name in {"_shutdown_jjc_resources", "_shutdown_jjc_sync_workers"}]
    if len(nodes) != 2:
        raise AssertionError("Expected actual resource shutdown and registered wrapper")
    for node in nodes:
        node.decorator_list = []
    modules = {}
    assembly = types.ModuleType("src.services.jx3.singletons")
    assembly.jjc_foreground_client = object()
    if service is not None:
        assembly.jjc_ranking_equipment_service = service
    if runtime is not None:
        assembly.jjc_equipment_read_runtime = runtime
    modules[assembly.__name__] = assembly

    def register(module_name, names):
        module = types.ModuleType(module_name)
        for name, event in names.items():
            async def stop(*args, _event=event):
                events.append(_event)
            setattr(module, name, stop)
        modules[module_name] = module
    register("src.services.jx3.jjc_foreground_runtime", {"stop_jjc_foreground": "foreground"})
    register("src.services.jx3.jjc_sync_worker_runtime", {
        "stop_jjc_sync_dispatcher": "dispatcher", "stop_jjc_sync_workers": "workers"})
    register("src.services.jx3.jjc_kungfu_refresh_runtime", {"stop_kungfu_refresh": "kungfu"})
    register("src.services.jx3.jjc_live_refresh_runtime", {"stop_live_refresh": "live"})
    register("src.infra.mongo", {"close_mongo": "mongo"})
    logs = []
    namespace = {"nonebot": types.SimpleNamespace(logger=types.SimpleNamespace(
        warning=lambda *args: logs.append(args)))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(ROOT / "bot.py"), "exec"), namespace)
    return namespace["_shutdown_jjc_sync_workers"], modules, logs


class EquipmentAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_equipment_host_config_reaches_only_optional_runtime(self):
        calls=[];client=Client()
        def factory(uri,**kwargs):calls.append(uri);return client
        namespace,cfg=actual_equipment_assembly(factory,host_override="10.2.3.4")
        runtime=namespace["jjc_equipment_read_runtime"]
        self.addAsyncCleanup(runtime.close)
        self.addAsyncCleanup(namespace["jjc_ranking_equipment_service"].close)
        self.assertEqual([],calls)
        original=cfg.MONGO_URI
        cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED=1
        namespace["_equipment_db"]["jjc_match_detail"]
        self.assertEqual([original.replace("127.0.0.1","10.2.3.4")],calls)
        self.assertEqual(original,cfg.MONGO_URI)

    async def test_real_optional_assembly_is_lazy_and_injects_one_read_db_into_every_reader(self):
        calls = []
        client = Client()
        def factory(uri, **kwargs):
            calls.append((uri, kwargs)); return client
        namespace, cfg = actual_equipment_assembly(factory)
        service = namespace["jjc_ranking_equipment_service"]
        runtime = namespace["jjc_equipment_read_runtime"]
        self.addAsyncCleanup(runtime.close)
        self.addAsyncCleanup(service.close)
        proxy = namespace["_equipment_db"]
        self.assertEqual([], calls)
        self.assertIsNone(service.stats_repo.db, "Frozen compatibility reads must retain ordinary storage")
        for repo in (service.dynamic_stats_repo, service.identity_repo, service.inspect_repo,
                     service.inspect_repo.snapshot_repo, service.snapshot_repo):
            self.assertIs(proxy, repo.db)
        with self.assertRaises(RuntimeError):
            proxy["jjc_match_detail"]
        self.assertEqual([], calls)
        cfg.JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED = 1
        self.assertIs(client.db.jjc_match_detail, proxy["jjc_match_detail"])
        self.assertIs(client.db.jjc_equipment_snapshot, service.snapshot_repo.db.jjc_equipment_snapshot)
        self.assertEqual(1, len(calls))

    async def test_actual_shutdown_stops_service_before_client_and_preserves_old_cleanup_order(self):
        events = []
        class Service:
            async def close(self): events.append("equipment-service")
        class Runtime:
            async def close(self): events.append("equipment-runtime")
        stop, modules, unused = actual_shutdown(Service(), Runtime(), events)
        with patch.dict(sys.modules, modules):
            await stop()
        self.assertEqual(["equipment-service", "equipment-runtime", "foreground", "kungfu",
                          "dispatcher", "workers", "live", "mongo"], events)

    async def test_actual_shutdown_closes_client_and_other_resources_after_service_failure(self):
        events = []
        class Service:
            async def close(self):
                events.append("equipment-service"); raise RuntimeError("private-error-not-to-log")
        class Runtime:
            async def close(self): events.append("equipment-runtime")
        stop, modules, logs = actual_shutdown(Service(), Runtime(), events)
        with patch.dict(sys.modules, modules):
            await stop()
        self.assertEqual("equipment-runtime", events[1])
        self.assertEqual("mongo", events[-1])
        self.assertTrue(logs)
        self.assertNotIn("private-error-not-to-log", str(logs))

    async def test_actual_shutdown_is_safe_when_optional_assembly_was_never_initialized(self):
        events = []
        stop, modules, unused = actual_shutdown(events=events)
        with patch.dict(sys.modules, modules):
            await stop()
        self.assertEqual(["foreground", "kungfu", "dispatcher", "workers", "live", "mongo"], events)

    async def test_actual_shutdown_drains_real_service_build_before_closing_owned_client(self):
        clock = Clock(); stats = FakeStats(); inspector = FakeInspect()
        inspector.release = asyncio.Event()
        identities = FakeIdentities(stats.context["members"])
        events = []
        client = Client(events=events, inspect_repo=inspector)
        runtime = EquipmentReadRuntime(enabled=True,
            uri="mongodb://127.0.0.1:27029/jjc_kungfu_test", client_factory=lambda *a, **k: client)
        runtime.get_db()
        service = JjcRankingEquipmentViewService(stats_repo=stats, identity_repo=identities,
            inspect_repo=inspector, snapshot_repo=FakeSnapshots(), scope=SCOPE,
            clock=clock.wall, monotonic=clock.monotonic)
        task = asyncio.create_task(service.read_dynamic_view(timestamp=TIMESTAMP,
            expected_member_revision=MEMBER_REVISION, expected_frozen_source="local_win_v2"))
        self.addAsyncCleanup(service.close)
        self.addAsyncCleanup(runtime.close)
        try:
            await asyncio.wait_for(inspector.started.wait(), 1)
            stop, modules, unused = actual_shutdown(service, runtime, events)
            with patch.dict(sys.modules, modules):
                await asyncio.wait_for(stop(), 1)
            self.assertEqual(0, inspector.active)
            self.assertEqual("equipment-client", events[0])
            self.assertEqual(1, client.close_calls)
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


    async def test_actual_shutdown_drains_baseline_and_qualification_before_client_close(self):
        from tests.test_jjc_ranking_equipment_shutdown import GatedStats, service_for, operation
        for stage, mode in ((("baseline", 1), "build"), (("context", 1), "dynamic"),
                            (("latest", 1), "dynamic"), (("context", 2), "dynamic"),
                            (("latest", 2), "dynamic"), (("context", 1), "frozen")):
            with self.subTest(stage=stage, mode=mode):
                stats = GatedStats(stage)
                service = service_for(stats)
                events = []
                client = Client(events=events, inspect_repo=stats)
                runtime = EquipmentReadRuntime(enabled=True,
                    uri="mongodb://127.0.0.1:27029/jjc_kungfu_test", client_factory=lambda *a, **k: client)
                runtime.get_db()
                task = asyncio.create_task(operation(service, stats, mode))
                stopping = None
                try:
                    await asyncio.wait_for(stats.started.wait(), 1)
                    stop, modules, unused = actual_shutdown(service, runtime, events)
                    with patch.dict(sys.modules, modules):
                        stopping = asyncio.create_task(stop())
                        await asyncio.sleep(.02)
                        self.assertTrue(stats.cancelled.is_set())
                        self.assertEqual(0, client.close_calls)
                        self.assertFalse(stopping.done())
                        stats.release_cleanup.set()
                        await asyncio.wait_for(stopping, 1)
                    self.assertEqual(0, stats.active)
                    self.assertEqual(1, client.close_calls)
                    self.assertEqual("equipment-client", events[0])
                    self.assertTrue(task.done())
                finally:
                    stats.release_cleanup.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    if stopping: await asyncio.gather(stopping, return_exceptions=True)
                    await service.close()
                    await runtime.close()


    async def test_cancelled_shutdown_waiter_does_not_close_runtime_before_drain(self):
        from tests.test_jjc_ranking_equipment_shutdown import GatedStats, service_for, operation
        stats = GatedStats(("baseline", 1))
        service = service_for(stats)
        events = []
        client = Client(events=events, inspect_repo=stats)
        runtime = EquipmentReadRuntime(enabled=True,
            uri="mongodb://127.0.0.1:27029/jjc_kungfu_test", client_factory=lambda *a, **k: client)
        runtime.get_db()
        request = asyncio.create_task(operation(service, stats, "build"))
        stopping = second = None
        try:
            await asyncio.wait_for(stats.started.wait(), 1)
            stop, modules, unused = actual_shutdown(service, runtime, events)
            with patch.dict(sys.modules, modules):
                stopping = asyncio.create_task(stop())
                await asyncio.wait_for(stats.cancelled.wait(), 1)
                second = asyncio.create_task(service.close())
                stopping.cancel()
                await asyncio.sleep(.02)
                self.assertEqual(0, client.close_calls)
                self.assertFalse(stopping.done())
                self.assertFalse(second.done())
                stats.release_cleanup.set()
                with self.assertRaises(asyncio.CancelledError): await stopping
                await asyncio.wait_for(second, 1)
            self.assertEqual(0, stats.active)
            self.assertEqual(1, client.close_calls)
        finally:
            stats.release_cleanup.set()
            if not request.done(): request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            for task in (stopping, second):
                if task: await asyncio.gather(task, return_exceptions=True)
            await service.close()
            await runtime.close()
