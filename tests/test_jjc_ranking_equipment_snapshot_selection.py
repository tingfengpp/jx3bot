"""DEP14 consume-only hydration; complete match validation remains the authority."""
from copy import deepcopy
import unittest
from src.services.jx3 import jjc_ranking_equipment as domain
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService
from tests import test_jjc_ranking_equipment_view as fixtures
from tests.jjc_equipment_fixtures import SCOPE,TIMESTAMP,CHECKED_AT,MEMBER_REVISION,match_doc,frozen_member,weapon

def source(mid=200,side='team1'):
    row=match_doc(mid,when=SCOPE['season_start']+mid,target_team=side)
    for name in ('team1','team2'):
        for index,player in enumerate(row['data']['detail'][name]['players_info']):
            player['equipment_snapshot_hash']='hash-'+str(mid)+'-'+name+'-'+str(index);player['armors']=[]
    return row

def snapshots(row):
    return {player['equipment_snapshot_hash']:{'snapshot_hash':player['equipment_snapshot_hash'],'armors':[weapon('legendary' if index%2==0 else 'purple')],'unrelated_full_document':{'kept':True}}
            for side in ('team1','team2') for index,player in enumerate(row['data']['detail'][side]['players_info'])}

class RequiredSnapshotHashes(unittest.TestCase):
    def required(self,member,row):
        helper=getattr(domain,'required_local_win_snapshot_hashes',None)
        self.assertTrue(callable(helper),'pure consumed-win snapshot helper required')
        before=deepcopy((member,row));result=helper(member,row);self.assertEqual(before,(member,row));return set(result)
    def test_both_sides_and_every_winner_target_only_require_own_three(self):
        for side in ('team1','team2'):
            row=source(side=side);team=row['data']['detail'][side]['players_info'];all_rows=snapshots(row)
            for player in team:
                member=frozen_member(global_id=player['global_id']);selected=domain.validate_local_win(member=member,candidates=[row],scope=SCOPE,checked_at=CHECKED_AT)
                self.assertIsNotNone(selected['packet'])
                hashes=self.required(member,row);self.assertEqual({p['equipment_snapshot_hash'] for p in team},hashes)
                full=domain.validate_local_win(member=member,candidates=[row],scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots=all_rows)
                reduced=domain.validate_local_win(member=member,candidates=[row],scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={key:all_rows[key] for key in hashes})
                self.assertEqual(full,reduced)
    def test_inline_nonempty_even_invalid_never_borrows_snapshot_but_empty_or_nonlist_does(self):
        for armors,required in (([weapon('purple')],False),([{}],False),([None],False),(['bad'],False),([],True),(None,True),({},True),('bad',True)):
            with self.subTest(armors=armors):
                row=source();own=row['data']['detail']['team1']['players_info']
                for player in own:player['armors']=[weapon('purple')]
                own[0]['armors']=deepcopy(armors);hashes=self.required(frozen_member(),row)
                self.assertEqual({own[0]['equipment_snapshot_hash']} if required else set(),hashes)
                full=snapshots(row);reduced={key:full[key] for key in hashes}
                oracle=lambda values:domain.validate_local_win(member=frozen_member(),candidates=[row],scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots=values)
                self.assertEqual(oracle(full),oracle(reduced))
                if isinstance(armors,list) and armors and armors!=[weapon('purple')]:self.assertEqual('unknown',oracle(full)['packet']['equipment_state'])
    def test_missing_snapshot_is_unknown_and_no_different_hash_is_borrowed(self):
        row=source();hashes=self.required(frozen_member(),row);self.assertEqual(3,len(hashes))
        packet=domain.validate_local_win(member=frozen_member(),candidates=[row],scope=SCOPE,checked_at=CHECKED_AT,equipment_snapshots={})['packet']
        self.assertEqual('unknown',packet['equipment_state']);self.assertEqual(2,len(packet['teammates']))

class SnapshotGetter:
    def __init__(self,rows,all_rows=False):self.rows=deepcopy(rows);self.all_rows=all_rows;self.old_calls=[];self.calls=[]
    async def load_equipment_snapshots(self,hashes,*,strict=False):
        self.old_calls.append((list(hashes),strict));return deepcopy(self.rows if self.all_rows else {key:self.rows[key] for key in hashes if key in self.rows})
    async def load_local_win_equipment_snapshots(self,hashes):
        self.calls.append(list(hashes));return deepcopy(self.rows if self.all_rows else {key:self.rows[key] for key in hashes if key in self.rows})

class ConsumptionService(unittest.IsolatedAsyncioTestCase):
    async def build(self,members,rows,snapshot_rows,*,all_rows=False):
        clock=fixtures.Clock();inspect=fixtures.FakeInspect();inspect.rows=rows;snap=SnapshotGetter(snapshot_rows,all_rows)
        service=JjcRankingEquipmentViewService(stats_repo=fixtures.FakeStats(members[0]),identity_repo=fixtures.FakeIdentities(members),inspect_repo=inspect,snapshot_repo=snap,scope=deepcopy(SCOPE),clock=clock.wall,monotonic=clock.monotonic)
        try:view=await service._construct(timestamp=TIMESTAMP,members=members,scope=SCOPE,member_revision=MEMBER_REVISION)
        finally:await service.close()
        return view,snap,inspect
    async def test_same_match_multiple_winners_dedup_consumed_hashes_and_full_view_matches_all_hydrate(self):
        for side in ('team1','team2'):
            row=source(side=side);team=row['data']['detail'][side]['players_info'];members=[frozen_member(index+1,global_id=p['global_id']) for index,p in enumerate(team)]
            team[0]['armors']=[weapon('purple')];team[1]['armors']=[{}]
            full=snapshots(row);view,repo,inspect=await self.build(members,[row],full)
            oracle,_,_=await self.build(members,[row],full,all_rows=True)
            self.assertEqual(oracle,view);self.assertEqual([],repo.old_calls)
            self.assertEqual([[team[2]['equipment_snapshot_hash']]],repo.calls)
            self.assertEqual(3,view['ranges']['top_1000']['N']);self.assertEqual(1,view['ranges']['top_1000']['U'])
            self.assertEqual({p['global_id'] for p in team},set(inspect.calls[0]['global_ids']))
    async def test_only_latest_win_is_hydrated_and_missing_latest_snapshot_does_not_fallback(self):
        older=source(100);newest=source(200);full=snapshots(older)
        view,repo,_=await self.build([frozen_member()],[older,newest],full)
        expected={p['equipment_snapshot_hash'] for p in newest['data']['detail']['team1']['players_info']}
        self.assertEqual([],repo.old_calls);self.assertEqual([sorted(expected)],repo.calls)
        self.assertEqual(200,view['members'][0]['local_win_packet']['match_id']);self.assertEqual('unknown',view['members'][0]['local_win_packet']['equipment_state'])
    async def test_incomplete_opponent_roster_or_newer_inconsistent_evidence_blocks_all_hydration(self):
        for mode in ('incomplete_opponent','future'):
            older=source(100);newer=source(200)
            if mode=='incomplete_opponent':newer['data']['detail']['team2']['players_info'].pop()
            else:newer['data']['match_time']=CHECKED_AT+1
            view,repo,_=await self.build([frozen_member()],[older,newer],snapshots(older)|snapshots(newer))
            self.assertIsNone(view['members'][0]['local_win_packet']);self.assertEqual('data_inconsistent',view['members'][0]['equipment_status']);self.assertEqual([],repo.calls);self.assertEqual([],repo.old_calls)
