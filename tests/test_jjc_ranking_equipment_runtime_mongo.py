"""DEP02 isolated Mongo4.4 hello support + actual dedicated-runtime strict reads.

The observed hello advertises zlib support. This does not claim wire byte counts
or prove every OP_MSG was compressed; no private driver connection is inspected.
"""
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import unittest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.monitoring import CommandListener
from src.infra.jjc_equipment_read_runtime import EquipmentReadRuntime
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.services.jx3.jjc_ranking_equipment import validate_local_win
from tests.test_jjc_kungfu_refresh_mongo import checked_test_uri
from tests.test_jjc_ranking_equipment_mongo import create_equipment_source_indexes
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TARGET_GLOBAL_ID, frozen_member, match_doc


class CompressionCommands(CommandListener):
    def __init__(self): self.started_names = []; self.hello_replies = []
    def started(self, event): self.started_names.append(event.command_name)
    def succeeded(self, event):
        if event.command_name == "hello": self.hello_replies.append(dict(event.reply))
    def failed(self, event): pass


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"), "explicit isolated Mongo required")
class EquipmentRuntimeCompressionMongo(unittest.IsolatedAsyncioTestCase):
    async def test_real_runtime_hello_zlib_support_and_strict_local_read_are_compatible(self):
        safe = checked_test_uri(os.environ["JJC_TEST_MONGO_URI"])
        parts = urlsplit(safe)
        name = "jjc_kungfu_test_" + uuid4().hex
        uri = urlunsplit((parts.scheme, parts.netloc, "/"+name, parts.query, parts.fragment))
        writer = AsyncIOMotorClient(safe, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=2000)
        commands = CompressionCommands()
        calls = []
        def factory(target, **options):
            calls.append(dict(options))
            return AsyncIOMotorClient(target, event_listeners=[commands], **options)
        runtime = EquipmentReadRuntime(enabled=True, uri=uri, client_factory=factory)
        try:
            db = writer[name]
            await db.jjc_match_detail.insert_one(match_doc(765001))
            for side in ("team1","team2"):
                await db.jjc_match_detail.create_index([("data.detail."+side+".players_info.global_id",1),("_id",1)],
                    name="idx_equipment_"+side+"_global_source")
            await create_equipment_source_indexes(db)
            read_db = runtime.get_db()
            hello = await read_db.command({"hello": 1, "compression": ["zlib"]})
            self.assertIn("zlib", hello.get("compression", []), "explicit hello must confirm isolated server zlib support")
            snapshots = JjcMatchSnapshotRepo(db=read_db)
            repo = JjcInspectRepo(db=read_db, snapshot_repo=snapshots, participant_repo=None)
            candidates = await repo.list_local_win_candidates_batch([TARGET_GLOBAL_ID],
                season_start=SCOPE["season_start"], checked_at=CHECKED_AT, max_time_ms=1500)
            self.assertEqual([765001], [item["match_id"] for item in candidates])
            result = validate_local_win(member=frozen_member(), candidates=candidates, scope=SCOPE,
                checked_at=CHECKED_AT, equipment_snapshots={})
            self.assertEqual("available", result["state"])
            self.assertEqual("purple", result["packet"]["equipment_state"])
            self.assertEqual(765001, result["packet"]["match_id"])
            self.assertEqual({}, await snapshots.load_equipment_snapshots(["absent"], strict=True))
            self.assertEqual(1, len(commands.hello_replies))
            self.assertIn("zlib", commands.hello_replies[0].get("compression", []))
            self.assertNotIn("insert", commands.started_names, "runtime connection performs only strict reads/hello")
            self.assertEqual(["zlib"], calls[0].get("compressors"))
            self.assertEqual(1, calls[0].get("zlibCompressionLevel"))
        finally:
            await runtime.close()
            await writer.drop_database(name)
            writer.close()
