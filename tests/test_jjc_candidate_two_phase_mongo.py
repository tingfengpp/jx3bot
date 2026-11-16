"""Real Mongo races across covered enumeration and strict source reads."""
import os
import unittest

from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from tests import test_jjc_candidate_certified_reduce_mongo as corpus
from tests.jjc_equipment_fixtures import TARGET_GLOBAL_ID, SCOPE, CHECKED_AT


class BeforeReadCursor:
    def __init__(self,inner,before):self.inner=inner;self.before=before;self.started=False;self.closed=False
    def hint(self,value):self.inner.hint(value);return self
    def max_time_ms(self,value):self.inner.max_time_ms(value);return self
    def batch_size(self,value):self.inner.batch_size(value);return self
    def __aiter__(self):return self
    async def __anext__(self):
        if not self.started:
            self.started=True;await self.before()
        return await self.inner.__anext__()
    async def close(self):await self.inner.close();self.closed=True


class RaceCollection:
    def __init__(self,real,source_id,mode):
        self.real=real;self.source_id=source_id;self.mode=mode;self.wrappers=[];self.changed=False
    def find(self,query,projection=None,**kwargs):
        cursor=self.real.find(query,projection,**kwargs)
        if "_id" in query and self.mode=="delete":
            async def disappear():
                await self.real.delete_one({"_id":self.source_id});self.changed=True
            cursor=BeforeReadCursor(cursor,disappear);self.wrappers.append(cursor)
        elif "_id" in query and self.mode=="identity":
            async def change_identity():
                await self.real.update_one({"_id":self.source_id},{"$set":{
                    "data.detail.team1.players_info.0.global_id":"899999999999999999"}})
                self.changed=True
            cursor=BeforeReadCursor(cursor,change_identity);self.wrappers.append(cursor)
        return cursor
    def aggregate(self,pipeline,**kwargs):
        cursor=self.real.aggregate(pipeline,**kwargs)
        if self.mode=="identity":
            async def change_identity():
                await self.real.update_one({"_id":self.source_id},{"$set":{
                    "data.detail.team1.players_info.0.global_id":"899999999999999999"}})
                self.changed=True
            cursor=BeforeReadCursor(cursor,change_identity);self.wrappers.append(cursor)
        return cursor


class RaceDB:
    def __init__(self,db,collection):self.db=db;self.jjc_match_detail=collection
    def __getattr__(self,name):return getattr(self.db,name)


@unittest.skipUnless(os.environ.get("JJC_TEST_MONGO_URI"),"explicit isolated loopback URI required")
class TwoPhaseActualMongoTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=corpus.CertifiedReductionMongoTests.asyncSetUp
    asyncTearDown=corpus.CertifiedReductionMongoTests.asyncTearDown
    async def setup_race(self,mode):
        row=corpus.canonical(100,SCOPE["season_start"]+100)
        result=await self.db.jjc_match_detail.insert_one(row)
        collection=RaceCollection(self.db.jjc_match_detail,result.inserted_id,mode)
        return JjcInspectRepo(db=RaceDB(self.db,collection)),collection
    async def read(self,repo):
        return await repo.list_local_win_candidates_batch([TARGET_GLOBAL_ID],season_start=SCOPE["season_start"],
            checked_at=CHECKED_AT,season_id=SCOPE["season_id"])

    async def test_changed_identity_is_rechecked_after_enumeration(self):
        repo,collection=await self.setup_race("identity")
        rows=await self.read(repo)
        from tests.jjc_equipment_fixtures import frozen_member
        from src.services.jx3.jjc_ranking_equipment import validate_local_win
        self.assertEqual("no_local_win",validate_local_win(member=frozen_member(),candidates=rows,scope=SCOPE,checked_at=CHECKED_AT)["state"])
        self.assertTrue(collection.changed)
        self.assertTrue(all(cursor.closed for cursor in collection.wrappers))

    async def test_real_source_disappearance_is_strict_not_partial_success(self):
        repo,collection=await self.setup_race("delete")
        with self.assertRaisesRegex(ValueError,"candidate_source_disappeared"):await self.read(repo)
        self.assertTrue(collection.changed)
        self.assertTrue(all(cursor.closed for cursor in collection.wrappers))


if __name__=="__main__":unittest.main()
