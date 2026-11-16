"""DEP14 fixed-field failure diagnostics never serialize exception/identity secrets."""
import asyncio,importlib,io,logging,math,re,secrets
from contextlib import contextmanager
from unittest.mock import patch
import unittest
from pymongo.errors import ExecutionTimeout
from nonebot import logger as loguru_logger
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from tests.jjc_equipment_fixtures import SCOPE
SECRET_NONCE=secrets.token_hex(24)
SECRET='mongodb://'+secrets.token_hex(8)+':'+SECRET_NONCE+'@diagnostic.invalid/db?token='+SECRET_NONCE
STAGES=('context','latest','qualify_before','qualify_after','binding_before','binding_after','binding_cached','candidates','snapshots','construct','read')
@contextmanager
def captured():
    stream=io.StringIO();handler=logging.StreamHandler(stream);root=logging.getLogger();old=root.level;root.setLevel(logging.DEBUG);root.addHandler(handler)
    extra=[];sink=loguru_logger.add(lambda message:extra.append(str(message)),format='{message}',level='DEBUG')
    try:yield lambda:stream.getvalue()+'\n'+''.join(extra)
    finally:root.removeHandler(handler);root.setLevel(old);loguru_logger.remove(sink)
class FixedFailureDiagnostics(unittest.TestCase):
    def module(self):
        try:return importlib.import_module('src.utils.jjc_equipment_read_diagnostics')
        except ModuleNotFoundError:self.fail('fixed-field equipment read diagnostics module required')
    def test_fixed_stage_and_known_exception_code_and_finite_duration_are_logged(self):
        module=self.module()
        for stage in STAGES:
            with captured() as logs:event=module.report_equipment_read_failure(stage,ExecutionTimeout(SECRET,code=50),.0125)
            self.assertEqual({'stage','exception_kind','code','elapsed_seconds'},set(event));self.assertEqual(stage,event['stage']);self.assertEqual('ExecutionTimeout',event['exception_kind']);self.assertEqual(50,event['code']);self.assertEqual(.0125,event['elapsed_seconds'])
            value=logs();self.assertIn(stage,value);self.assertIn('ExecutionTimeout',value);self.assertRegex(value,r'code["\'=:\s]+50(?:\D|$)');self.assertIn('elapsed',value.lower());self.assertNotIn(SECRET,value);self.assertNotIn(SECRET_NONCE,value)
    def test_unknown_exception_name_message_repr_and_args_are_never_rendered(self):
        class SecretError(Exception):
            def __str__(self):raise AssertionError('str(exception) is forbidden')
            def __repr__(self):raise AssertionError('repr(exception) is forbidden')
        SecretError.__name__=SECRET_NONCE
        error=SecretError(SECRET)
        with captured() as logs:self.module().report_equipment_read_failure('context',error,.001)
        value=logs();self.assertIn('other',value.lower());self.assertNotIn(SECRET_NONCE,value);self.assertNotIn('mongodb',value)
    def test_untrusted_stage_code_and_elapsed_are_normalized_without_leaking_values(self):
        module=self.module()
        for code in (True,False,'50',-1,10**80):
            error=Exception(SECRET);error.code=code
            with captured() as logs:event=module.report_equipment_read_failure(SECRET,error,float('nan'))
            self.assertEqual('read',event['stage']);self.assertIsNone(event['code']);self.assertFalse(isinstance(event['elapsed_seconds'],bool));self.assertTrue(math.isfinite(event['elapsed_seconds']) and event['elapsed_seconds']>=0)
            value=logs();self.assertNotIn(SECRET_NONCE,value);self.assertIn('read',value);self.assertNotRegex(value.lower(),r'\b(nan|inf|infinity|true|false)\b');self.assertNotIn(str(10**80),value)
        for elapsed in (-1,float('inf'),float('-inf')):
            with captured() as logs:module.report_equipment_read_failure('read',Exception(SECRET),elapsed)
            self.assertNotRegex(logs().lower(),r'\b(nan|inf|infinity)\b');self.assertNotIn(SECRET,logs());self.assertNotIn(SECRET_NONCE,logs())
    def test_logger_failure_cannot_escape_and_cancelled_error_does_not_log(self):
        module=self.module()
        with patch.object(module.logger,'warning',side_effect=RuntimeError('synthetic-log-failure')):
            module.report_equipment_read_failure('read',Exception(SECRET),.1)
        with captured() as logs:self.assertIsNone(module.report_equipment_read_failure('read',asyncio.CancelledError(SECRET),.1))
        self.assertEqual('',logs().strip())

class ObservedFailureBoundary(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service=JjcRankingEquipmentViewService(stats_repo=object(),identity_repo=object(),inspect_repo=object(),snapshot_repo=object(),scope=SCOPE)
        self.addAsyncCleanup(service.close);self.assertTrue(callable(getattr(service,'_observe',None)),'service needs a failure-only observation boundary');return service
    async def test_success_passes_through_with_no_log_and_original_budgets(self):
        service=self.service();marker=object()
        async def success():return marker
        with captured() as logs:self.assertIs(marker,await service._observe('candidates',success()))
        self.assertEqual('',logs().strip());self.assertEqual(8,service.build_timeout);self.assertEqual(10,service.request_timeout)
    async def test_failure_logs_only_safe_stage_and_reraises_exact_original_error(self):
        service=self.service();error=RuntimeError(SECRET)
        async def failure():raise error
        with captured() as logs:
            with self.assertRaises(RuntimeError) as actual:await service._observe('snapshots',failure())
        self.assertIs(error,actual.exception);self.assertIn('snapshots',logs());self.assertNotIn(SECRET,logs());self.assertNotIn(SECRET_NONCE,logs())
    async def test_cancellation_is_propagated_unchanged_without_failure_log(self):
        service=self.service();error=asyncio.CancelledError(SECRET)
        async def failure():raise error
        with captured() as logs:
            with self.assertRaises(asyncio.CancelledError) as actual:await service._observe('read',failure())
        self.assertIs(error,actual.exception);self.assertEqual('',logs().strip())
