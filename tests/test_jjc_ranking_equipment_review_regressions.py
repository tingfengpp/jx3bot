"""Regressions independently identified by implementation reviewers; local Mongo only."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import config
from tests.test_jjc_ranking_equipment_policy import validate, aggregate
from tests.jjc_equipment_fixtures import SCOPE, CHECKED_AT, TIMESTAMP, match_doc, equipped_member
from tests import test_jjc_ranking_equipment_mongo as mongo
from tests.test_jjc_ranking_equipment_mongo import snapshot_document, canonical_role_match
from src.services.jx3.jjc_ranking_equipment import public_equipment_view, apply_revocations

class ReviewPolicyTests(unittest.TestCase):
    def test_b02_old_first_time_alias_does_not_hide_current_conflict(self):
        good=match_doc(99,when=CHECKED_AT-1000)
        for first,other in ((SCOPE['season_start']-1,CHECKED_AT-1),(CHECKED_AT-1,SCOPE['season_start']-1)):
            bad=match_doc(100,when=first)
            bad['data']['detail']['basic_info']['start_time']=other
            with self.subTest(first=first):
                self.assertEqual('data_inconsistent',validate([good,bad])['state'])

    def test_b05_explicit_other_brackets_do_not_obscure_latest_3v3_win(self):
        good=match_doc(99,when=CHECKED_AT-1000)
        for bracket in (2,5):
            other=match_doc(100,when=CHECKED_AT-1)
            other['data']['detail']['basic_info']['match_type']=bracket
            for rows in ([good,other],[other,good]):
                self.assertEqual(99,(validate(rows)['packet'] or {}).get('match_id'))

    def test_b05_excluded_bracket_is_independent_of_its_time_or_match_id(self):
        good=match_doc(99,when=CHECKED_AT-1000)
        for bracket in (2,5):
            for fault in ('missing','future','conflict','invalid_id'):
                other=match_doc(100,when=CHECKED_AT-1)
                other['data']['detail']['basic_info']['match_type']=bracket
                if fault=='missing': other['data'].pop('match_time',None)
                elif fault=='future': other['data']['match_time']=CHECKED_AT+100
                elif fault=='conflict': other['data']['detail']['time']=SCOPE['season_start']-1
                else: other['match_id']='invalid'
                for rows in ([good,other],[other,good]):
                    with self.subTest(bracket=bracket,fault=fault):
                        self.assertEqual(99,(validate(rows)['packet'] or {}).get('match_id'))

    def test_b03_frozen_public_projection_does_not_reclassify_after_policy_change(self):
        view=aggregate([equipped_member(1,'legendary')]);before=deepcopy(view)
        with patch.object(config,'JJC_LEGENDARY_WEAPON_NAMES',set()):
            shown=public_equipment_view(view)
            self.assertEqual('legendary',shown['members'][0]['local_win_packet']['equipment_state'])
            self.assertEqual(1,shown['ranges']['top_1000']['L'])
            self.assertEqual([],shown['ranges']['top_1000']['purple_ranks'])
        self.assertEqual(before,view)

    def test_b07_public_match_id_preserves_full_int64_in_javascript(self):
        import json, subprocess
        row=equipped_member(1,'purple')
        row['local_win_packet']['match_id']=9223372036854775807
        public=public_equipment_view(aggregate([row]))
        mid=public['members'][0]['local_win_packet']['match_id']
        result=subprocess.run(['node','-e',"const v=JSON.parse(process.argv[1]);if(v.members[0].local_win_packet.match_id!=='9223372036854775807')process.exit(1)",json.dumps(public)],capture_output=True)
        self.assertEqual(0,result.returncode)
        self.assertEqual('9223372036854775807',mid)

class ReviewMongoTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=mongo.EquipmentActualMongoTests.asyncSetUp
    asyncTearDown=mongo.EquipmentActualMongoTests.asyncTearDown
    frozen=mongo.EquipmentActualMongoTests.frozen

    async def test_b01_real_legacy_upgrade_keeps_original_complete_cas_baseline(self):
        original={'timestamp':TIMESTAMP,'schema_version':1,'ranking_display_kind':'current',
            'current_season':SCOPE['season_id'],'kungfu_cache_scope':deepcopy(SCOPE),
            'nullable':None,'extra':['literal','$value']}
        result=await self.db.jjc_ranking_stat_summaries.insert_one(deepcopy(original))
        view=await self.frozen();expected=view.pop('_publication_expected')
        self.assertEqual(result.inserted_id,expected['expected_legacy'].get('_id'))
        published=await self.service.stats_repo.publish_equipment_snapshot(TIMESTAMP,snapshot_document(view),**expected)
        self.assertEqual(view['equipment_view_revision'],(await self.service.stats_repo.load_complete_view(TIMESTAMP))['equipment_view_revision'])

    async def test_b04_malformed_opponent_roster_is_confirmed_inconsistent_not_read_failure(self):
        await self.frozen()
        bad=canonical_role_match(self.members[0],9999001,CHECKED_AT-1)
        bad['data']['detail']['team2']['players_info']=7
        await self.db.jjc_match_detail.insert_one(bad)
        with patch.object(type(self.service.inspect_repo),'list_local_win_candidates_batch',return_value=[bad]):
            view=await self.frozen()
        self.assertEqual('data_inconsistent',view['members'][0]['equipment_status'])
        self.assertIsNone(view['members'][0]['local_win_packet'])
