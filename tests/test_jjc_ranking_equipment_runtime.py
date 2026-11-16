"""Lazy, bounded equipment-only client ownership; no real client may be built."""
import unittest

from src.infra.jjc_equipment_read_runtime import EquipmentReadRuntime


class FakeClient:
    def __init__(self, *, failure=None):
        self.db = object()
        self.close_calls = 0
        self.failure = failure

    def get_default_database(self):
        if self.failure:
            raise self.failure
        return self.db

    def close(self):
        self.close_calls += 1


class Factory:
    def __init__(self, client=None, error=None):
        self.client = client or FakeClient()
        self.error = error
        self.calls = []

    def __call__(self, uri, **kwargs):
        self.calls.append((uri, kwargs))
        if self.error:
            raise self.error
        return self.client


class EquipmentReadRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, enabled=True, factory=None):
        factory = factory or Factory()
        runtime = EquipmentReadRuntime(enabled=enabled,
            uri="mongodb://127.0.0.1:27029/jjc_kungfu_test", client_factory=factory)
        return runtime, factory

    async def test_tc31_constructor_is_lazy_and_disabled_use_never_creates_client(self):
        runtime, factory = self.runtime(enabled=False)
        self.assertEqual([], factory.calls)
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        self.assertEqual([], factory.calls)
        await runtime.close()
        self.assertEqual(0, factory.client.close_calls)

    async def test_tc31_enabled_first_read_owns_one_client_with_native_limits(self):
        runtime, factory = self.runtime()
        self.assertEqual([], factory.calls)
        db = runtime.get_db()
        self.assertIs(db, factory.client.db)
        self.assertIs(db, runtime.get_db())
        self.assertEqual(1, len(factory.calls))
        options = factory.calls[0][1]
        self.assertLessEqual(options["maxPoolSize"], 4)
        self.assertLessEqual(options["serverSelectionTimeoutMS"], 1500)
        self.assertLessEqual(options["connectTimeoutMS"], 1500)
        self.assertLessEqual(options["socketTimeoutMS"], 2000)
        self.assertLessEqual(options["waitQueueTimeoutMS"], 500)
        await runtime.close()
        await runtime.close()
        self.assertEqual(1, factory.client.close_calls)

    async def test_tc31_enabled_callable_is_checked_lazily(self):
        state = {"enabled": False}
        runtime, factory = self.runtime(enabled=lambda: state["enabled"])
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        self.assertEqual([], factory.calls)
        state["enabled"] = True
        self.assertIs(runtime.get_db(), factory.client.db)
        await runtime.close()

    async def test_tc31_partial_initialization_failure_closes_created_client(self):
        client = FakeClient(failure=RuntimeError("synthetic-default-db-failure"))
        runtime, factory = self.runtime(factory=Factory(client=client))
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        self.assertEqual(1, client.close_calls)
        await runtime.close()
        self.assertEqual(1, client.close_calls, "Partial failure must not double-close ownership")

    async def test_tc31_factory_failure_can_close_safely_without_other_client(self):
        runtime, factory = self.runtime(factory=Factory(error=RuntimeError("synthetic-client-factory-failure")))
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        await runtime.close()
        self.assertEqual(0, factory.client.close_calls)

    async def test_tc31_close_before_first_read_prevents_late_initialization(self):
        runtime, factory = self.runtime()
        await runtime.close()
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        self.assertEqual([], factory.calls)

    async def test_tc31_close_after_initialized_prevents_new_db_handles(self):
        runtime, factory = self.runtime()
        runtime.get_db()
        await runtime.close()
        with self.assertRaises(RuntimeError):
            runtime.get_db()
        self.assertEqual(1, len(factory.calls))
        self.assertEqual(1, factory.client.close_calls)


    async def test_dep02_equipment_client_requests_zlib_without_relaxing_native_limits(self):
        runtime, factory = self.runtime()
        try:
            self.assertEqual([], factory.calls)
            runtime.get_db()
            options = factory.calls[0][1]
            self.assertEqual(["zlib"], options.get("compressors"))
            self.assertEqual(1, options.get("zlibCompressionLevel"))
            for key, value in {"maxPoolSize": 4, "minPoolSize": 0,
                    "serverSelectionTimeoutMS": 1500, "connectTimeoutMS": 1500,
                    "socketTimeoutMS": 2000, "waitQueueTimeoutMS": 500, "connect": False}.items():
                self.assertEqual(value, options[key], key)
        finally:
            await runtime.close()
