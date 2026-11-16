"""E6 actual shared browser-module contracts, executed by real Node.

Written before the UMD implementation. This suite never emulates production JS,
starts a browser, opens a socket, or loads the bot. A missing module is NOT RED.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from src.services.jx3.jjc_ranking_equipment import aggregate_equipment_view, public_equipment_view
from tests.jjc_equipment_fixtures import (
    SCOPE, CHECKED_AT, TIMESTAMP, MEMBER_REVISION, ten_member_golden, equipped_member,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "public/jjc-equipment-view.js"


def fixture_view(members=None, **overrides):
    args = dict(timestamp=TIMESTAMP, scope=deepcopy(SCOPE),
                members=ten_member_golden() if members is None else members,
                checked_at=CHECKED_AT, completed_at=CHECKED_AT+.1,
                member_revision=MEMBER_REVISION)
    args.update(overrides)
    return public_equipment_view(aggregate_equipment_view(**args))


PRELUDE = r"""
const fs=require('node:fs'), assert=require('node:assert/strict');
const api=require(process.argv[1]);
const f=JSON.parse(fs.readFileSync(0,'utf8'));
const clone=value=>JSON.parse(JSON.stringify(value));
const initial=()=>({frozen:clone(f.view),current:clone(f.view),
 manifest:{member_revision:f.view.member_revision,frozen_source:'local_win_v2'},
 failed:false,reloadRequired:false,revocations:[]});
const response=(view=f.view,extra={})=>({mode:'dynamic',overlay_enabled:true,retired:false,
 reload_required:false,current_member_revision:view.member_revision,
 current_frozen_source:'local_win_v2',view:clone(view),...extra});
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
"""


POLLER = r"""
function setup(extra={}) {
 const s={requests:[],applied:[],errors:[],timers:new Map(),next:1,
 context:{timestamp:String(f.view.timestamp),manifest:{member_revision:f.view.member_revision,
 frozen_source:'local_win_v2'},visible:true,tab:'all',exporting:false}};
 const options={getContext:()=>s.context,
 fetchView:async request=>{s.requests.push(request);return response();},
 applyResponse:data=>s.applied.push(data),onError:error=>s.errors.push(error),
 schedule:(fn,delay)=>{const id=s.next++;s.timers.set(id,{fn,delay});return id;},
 cancelSchedule:id=>s.timers.delete(id),...extra};
 s.poller=api.createPoller(options);
 s.fire=async()=>{const [id,timer]=s.timers.entries().next().value||[];
  assert.ok(timer,'a bounded timer should be scheduled');s.timers.delete(id);timer.fn();await flush();};
 return s;
}
"""


class EquipmentFrontendNodeTests(unittest.TestCase):
    def node(self, source, *, view=None, **fixture):
        self.assertTrue(MODULE.is_file(), "create the legal UMD stub before collecting business RED")
        node = shutil.which("node")
        self.assertIsNotNone(node, "real Node is required; environment errors are not RED")
        data = {"view": fixture_view() if view is None else view, **fixture}
        script = PRELUDE + "\n(async()=>{\n" + source + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([node, "-e", script, str(MODULE)],
                                input=json.dumps(data,ensure_ascii=False),text=True,
                                capture_output=True,cwd=ROOT,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_contract_and_public_helpers_are_real_module_exports(self):
        self.node(r"""
assert.equal(api.CONTRACT,2);
for(const name of ['equipmentStatistics','visibleMembers','applyResponse','freezeView','createPoller'])
 assert.equal(typeof api[name],'function',name+' must be exported');
""")

    def test_tc15_tc41_same_view_drives_counts_ratio_coverage_and_p_members(self):
        self.node(r"""
const before=clone(f.view),stats=api.equipmentStatistics(f.view,'all');
const range=stats.top_50,m=range.equipment_metrics;
assert.deepEqual([m.N,m.L,m.P,m.U],[10,2,5,3]);
assert.equal(m.legendary_ratio,.2);assert.equal(m.equipment_coverage,.7);
assert.equal(range.dps.valid_count,10);
assert.equal(range.dps.legendary_count_map['山居剑意'],2);
assert.equal(range.dps.equipment_metrics['山居剑意'].U,3);
const all=api.visibleMembers(f.view,'top_50','all');
assert.equal(all.length,10);assert.equal(all.filter(x=>x.equipment_state==='legendary').length,2);
const purple=api.visibleMembers(f.view,'top_50','purple');
assert.deepEqual(purple.map(x=>x.rank),[3,4,5,6,7]);
assert.ok(purple.every(x=>x.equipment_state==='purple'&&x.weapon_quality===4));
assert.deepEqual(f.view,before,'render conversion may not mutate source view');
""")

    def test_tc41_purple_distribution_is_p_not_n_minus_l_and_preserves_source_coverage(self):
        self.node(r"""
const range=api.equipmentStatistics(f.view,'purple').top_50;
assert.equal(range.dps.valid_count,5);
assert.deepEqual(range.dps.list,[['山居剑意',5]]);
assert.equal(range.total_players,5);
assert.deepEqual([range.equipment_metrics.N,range.equipment_metrics.L,range.equipment_metrics.P,range.equipment_metrics.U],[5,0,5,0]);
assert.deepEqual([range.source_equipment_metrics.N,range.source_equipment_metrics.U],[10,3]);
assert.equal(range.source_equipment_metrics.equipment_coverage,.7);
assert.equal(api.visibleMembers(f.view,'top_50','purple').length,range.dps.valid_count);
""")

    def test_tc42_unknown_equipment_never_uses_opposite_legacy_weapon(self):
        self.node(r"""
// Inject a stale legacy L weapon into a U member. It must remain U, not L/P.
const view=clone(f.view);view.members[7].weapon_name='烂柯';
view.members[7].weapon_quality=5;view.members[7].weapon_icon='https://example.invalid/old.png';
const row=api.visibleMembers(view,'top_50','all').find(x=>x.rank===8);
assert.equal(row.equipment_state,'unknown');assert.equal(row.weapon_name,null);
assert.equal(row.weapon_quality,null);assert.equal(row.weapon_icon,null);
assert.ok(!api.visibleMembers(view,'top_50','purple').some(x=>x.rank===8));
""")

    def test_tc16_ranges_take_original_rank_before_equipment_filter(self):
        rows=[equipped_member(i,"purple") for i in (49,50,51,99,100,101,199,200,201,999,1000)]
        self.node(r"""
const expected={top_50:[49,50],top_100:[49,50,51,99,100],
 top_200:[49,50,51,99,100,101,199,200],
 top_1000:[49,50,51,99,100,101,199,200,201,999,1000]};
const stats=api.equipmentStatistics(f.view,'purple');
for(const [range,ranks] of Object.entries(expected)) {
 assert.deepEqual(api.visibleMembers(f.view,range,'purple').map(x=>x.rank),ranks);
 assert.equal(stats[range].equipment_metrics.N,ranks.length);
}
""",view=fixture_view(rows))

    def test_tc17_unknown_classification_keeps_freeze_and_p_set_without_new_lane(self):
        rows=ten_member_golden()
        rows[4].update(lane="unknown",kungfu=None,kungfu_id=None,found=False)
        self.node(r"""
const all=api.equipmentStatistics(f.view,'all').top_50;
const purple=api.equipmentStatistics(f.view,'purple').top_50;
assert.equal(all.dps.valid_count,9);assert.equal(all.unknown_members.length,1);
assert.deepEqual(purple.unknown_members.map(m=>m.rank),[5]);
const member=api.visibleMembers(f.view,'top_50','purple').find(m=>m.rank===5);
assert.equal(member.lane,'unknown');assert.equal(member.kungfu,null);assert.equal(member.found,false);
assert.equal(member.score,f.view.members.find(m=>m.rank===5).score);
assert.equal(member.rank,5,'filtering cannot renumber the original rank');
""",view=fixture_view(rows))

    def test_tc15_zero_members_has_null_ratios(self):
        self.node(r"""
for(const mode of ['all','purple']) {
 const stats=api.equipmentStatistics(f.view,mode);
 assert.deepEqual(Object.keys(stats).sort(),['top_100','top_1000','top_200','top_50']);
 for(const range of Object.values(stats)) {
 assert.deepEqual([range.equipment_metrics.N,range.equipment_metrics.L,range.equipment_metrics.P,range.equipment_metrics.U],[0,0,0,0]);
 assert.equal(range.equipment_metrics.legendary_ratio,null);
 assert.equal(range.equipment_metrics.equipment_coverage,null);
 }
}
""",view=fixture_view([]))

    def test_tc43_same_revision_success_clears_failure_without_mutation(self):
        self.node(r"""
const state=initial();state.failed=true;const before=clone(state);
const next=api.applyResponse(state,response());
assert.equal(next.failed,false);assert.equal(next.reloadRequired,false);
assert.equal(next.current.equipment_view_revision,f.view.equipment_view_revision);
assert.deepEqual(state,before);assert.notEqual(next,state);
""")

    def test_tc36_tc43_read_failed_keeps_complete_view(self):
        self.node(r"""
const state=initial(),next=api.applyResponse(state,{status:'read_failed',read_failed:true});
assert.equal(next.failed,true);assert.deepEqual(next.current,state.current);
assert.deepEqual(api.equipmentStatistics(next.current,'purple'),api.equipmentStatistics(state.current,'purple'));
const empty={...initial(),current:null,frozen:null};
assert.equal(api.applyResponse(empty,{read_failed:true}).current,null);
""")

    def test_tc47_disabled_and_retired_restore_whole_frozen_not_just_icons(self):
        rows=ten_member_golden();rows[2]=equipped_member(3,"legendary")
        self.node(r"""
for(const flags of [{overlay_enabled:false},{retired:true,overlay_enabled:false}]) {
 const state=initial();state.current=clone(f.newer);
 const next=api.applyResponse(state,{...flags,view:null});
 assert.deepEqual(next.current,state.frozen);
 assert.equal(api.equipmentStatistics(next.current,'all').top_50.equipment_metrics.L,2);
 assert.deepEqual(api.visibleMembers(next.current,'top_50','purple').map(x=>x.rank),[3,4,5,6,7]);
}
""",newer=fixture_view(rows))

    def test_tc33_manifest_mismatch_reload_is_cross_instance_safe(self):
        self.node(r"""
const state=initial(),nextView=clone(f.view);nextView.member_revision='new-members';
// The server need not remember any old content cache to return this manifest.
const next=api.applyResponse(state,response(nextView,{reload_required:true,
 reason:'member_revision_changed',view:null,current_member_revision:'new-members'}));
assert.equal(next.reloadRequired,true);assert.equal(next.current,null);
assert.equal(api.freezeView(next),null,'known obsolete members cannot be exported');
for(const incoming of [{overlay_enabled:false,view:null},{retired:true,overlay_enabled:false,view:null},
 {status:'read_failed',read_failed:true}]) {
 const pending=api.applyResponse(next,incoming);
 assert.equal(pending.reloadRequired,true);assert.equal(pending.current,null,
  'reload pending cannot resurrect an obsolete frozen view');
}
""")

    def test_tc33_client_also_detects_manifest_mismatch_without_reload_hint(self):
        self.node(r"""
const state=initial(),view=clone(f.view);view.member_revision='other-members';
const next=api.applyResponse(state,response(view,{current_member_revision:'other-members'}));
assert.equal(next.reloadRequired,true);assert.equal(next.current,null);
const changedSource=api.applyResponse(state,response(f.view,{current_frozen_source:'legacy_v1'}));
assert.equal(changedSource.reloadRequired,true);assert.equal(changedSource.current,null);
""")

    def test_tc33_dynamic_source_is_not_compared_with_frozen_source(self):
        self.node(r"""
const state=initial();state.manifest.frozen_source='legacy_v1';
const next=api.applyResponse(state,response(f.view,{current_frozen_source:'legacy_v1'}));
assert.equal(next.reloadRequired,false);assert.equal(next.current.source,'local_win_v2');
""")

    def test_tc37_confirmed_revocation_applies_atomically_even_on_failed_read(self):
        self.node(r"""
const state=initial(),revocation={timestamp:f.view.timestamp,scope:clone(f.view.scope),
 member_revision:f.view.member_revision,rank:1,binding_revision:f.view.members[0].binding_revision,
 confirmed_at:f.view.completed_at+1,reason:'binding_invalid'};
const next=api.applyResponse(state,{read_failed:true,revocations:[revocation]});
assert.equal(next.failed,true);
const metrics=api.equipmentStatistics(next.current,'all').top_50.equipment_metrics;
assert.deepEqual([metrics.N,metrics.L,metrics.P,metrics.U],[10,1,5,4]);
const row=api.visibleMembers(next.current,'top_50','all').find(m=>m.rank===1);
assert.equal(row.weapon_icon,null);assert.equal(row.equipment_state,'unknown');
const restored=api.applyResponse(next,{overlay_enabled:false,view:null});
assert.equal(api.equipmentStatistics(restored.current,'all').top_50.equipment_metrics.L,1,
 'disabled fallback must retain applicable revocations');
assert.equal(state.current.members[0].local_win_packet.equipment_state,'legendary');
""")

    def test_tc38_revocation_cannot_cross_rank_member_binding_or_scope(self):
        self.node(r"""
for(const extra of [{timestamp:f.view.timestamp+1},{member_revision:'different'},
 {binding_revision:'wrong-binding'},{scope:{...f.view.scope,season_id:'other-season'}}]) {
 const revocation={timestamp:f.view.timestamp,scope:clone(f.view.scope),member_revision:f.view.member_revision,
  rank:1,binding_revision:f.view.members[0].binding_revision,confirmed_at:f.view.completed_at+1,
  reason:'binding_invalid',...extra};
 const next=api.applyResponse(initial(),{read_failed:true,revocations:[revocation]});
 assert.equal(api.equipmentStatistics(next.current,'all').top_50.equipment_metrics.L,2);
}
""")

    def test_tc38_only_newer_successful_same_binding_view_can_release_revocation(self):
        self.node(r"""
const instruction={timestamp:f.view.timestamp,scope:clone(f.view.scope),
 member_revision:f.view.member_revision,rank:1,binding_revision:f.view.members[0].binding_revision,
 confirmed_at:f.view.completed_at+10,reason:'binding_invalid'};
const revoked=api.applyResponse(initial(),{read_failed:true,revocations:[instruction]});
const stale=api.applyResponse(revoked,response());
assert.equal(api.equipmentStatistics(stale.current,'all').top_50.equipment_metrics.L,1,
 'older successful content must not release a later confirmed revocation');
const newer=clone(f.view);newer.checked_at=instruction.confirmed_at+1;
newer.completed_at=instruction.confirmed_at+2;
const restored=api.applyResponse(stale,response(newer));
assert.equal(api.equipmentStatistics(restored.current,'all').top_50.equipment_metrics.L,2,
 'successful revalidation of the same frozen binding can release its temporary mask');
""")

    def test_tc46_freeze_export_is_full_deep_copy(self):
        self.node(r"""
const state=initial(),frozen=api.freezeView(state),before=clone(frozen);
assert.equal(frozen.equipment_view_revision,state.current.equipment_view_revision);
assert.deepEqual(frozen.ranges,state.current.ranges);
state.current.members[0].name='later';state.current.ranges.top_50.L=999;
assert.deepEqual(frozen,before);assert.notEqual(frozen,state.current);
""")

    def test_tc44_poller_hidden_peak_exporting_or_no_manifest_do_not_fetch(self):
        self.node(POLLER+r"""
for(const changes of [{visible:false},{tab:'peak-game'},{exporting:true},{timestamp:null},{manifest:null}]) {
 const s=setup();s.context={...s.context,...changes};s.poller.activate();await flush();
 assert.equal(s.requests.length,0);
 s.poller.suspend();s.context={timestamp:String(f.view.timestamp),manifest:initial().manifest,
  visible:true,tab:'all',exporting:false};
 s.poller.activate();await flush();assert.equal(s.requests.length,1);
 s.poller.suspend();assert.equal(s.timers.size,0);
}
""")

    def test_tc44_poller_one_request_manifest_constraints_and_fifteen_second_schedule(self):
        self.node(POLLER+r"""
let release;const s=setup({fetchView:request=>{s.requests.push(request);return new Promise(resolve=>{release=resolve;});}});
s.poller.activate();await flush();s.poller.tick();s.poller.tick();await flush();
assert.equal(s.requests.length,1,'one request only while pending');
const request=s.requests[0];assert.equal(String(request.timestamp),String(f.view.timestamp));
assert.equal(request.expected_member_revision,f.view.member_revision);
assert.equal(request.expected_frozen_source,'local_win_v2');assert.ok(request.signal);
release(response());await flush();assert.equal(s.applied.length,1);
assert.deepEqual([...s.timers.values()].map(x=>x.delay),[15000]);
s.poller.suspend();assert.equal(s.timers.size,0);
""")

    def test_tc44_old_request_cannot_apply_after_suspend_switch_and_reactivation(self):
        self.node(POLLER+r"""
let release;const s=setup({fetchView:request=>{s.requests.push(request);
 if(s.requests.length===1)return new Promise(resolve=>{release=resolve;});
 const view=clone(f.view);view.timestamp=Number(request.timestamp);
 return Promise.resolve(response(view,{marker:'new'}));}});
s.poller.activate();await flush();const old=s.requests[0];
s.poller.suspend();assert.equal(old.signal.aborted,true);
s.context={...s.context,timestamp:String(f.view.timestamp+1)};
s.poller.activate();await flush();release(response(f.view,{marker:'old'}));await flush();
if(s.requests.length===1&&s.timers.size)await s.fire();
assert.ok(!s.applied.some(r=>r.marker==='old'),'cancelled success cannot reach applyResponse');
assert.ok(s.applied.some(r=>r.marker==='new'),'reactivated context must eventually commit');
assert.equal(String(s.requests.at(-1).timestamp),String(f.view.timestamp+1));
s.poller.suspend();
""")

    def test_tc43_poller_failure_backoff_recovers_to_fifteen_seconds(self):
        self.node(POLLER+r"""
let failures=2;const s=setup({fetchView:async request=>{s.requests.push(request);
 if(failures-->0)throw Error('synthetic local failure');return response();}});
s.poller.activate();await flush();assert.equal(s.errors.length,1);
assert.deepEqual([...s.timers.values()].map(x=>x.delay),[30000]);
await s.fire();assert.equal(s.errors.length,2);
assert.deepEqual([...s.timers.values()].map(x=>x.delay),[60000]);
await s.fire();assert.equal(s.applied.length,1);
assert.deepEqual([...s.timers.values()].map(x=>x.delay),[15000]);
s.poller.suspend();
""")

    def test_tc43_disabled_retired_and_reload_stop_old_manifest_polling(self):
        self.node(POLLER+r"""
for(const flags of [{overlay_enabled:false},{retired:true,overlay_enabled:false},
 {reload_required:true,current_member_revision:'new-members',view:null}]) {
 const s=setup({fetchView:async request=>{s.requests.push(request);return response(f.view,flags);}});
 s.poller.activate();await flush();assert.equal(s.applied.length,1);
 assert.equal(s.timers.size,0,'old manifest polling must stop until bootstrap reactivates');
 await s.poller.tick();await flush();assert.equal(s.requests.length,1);
 s.poller.suspend();
}
""")

    def test_tc46_suspend_during_export_blocks_response_and_restarts_once(self):
        self.node(POLLER+r"""
let release;const s=setup({fetchView:request=>{s.requests.push(request);
 if(s.requests.length===1)return new Promise(resolve=>{release=resolve;});return Promise.resolve(response());}});
s.poller.activate();await flush();s.context.exporting=true;s.poller.suspend();
release(response());await flush();assert.equal(s.applied.length,0);
s.context.exporting=false;s.poller.activate();await flush();
assert.equal(s.requests.length,2);assert.equal(s.applied.length,1);
assert.equal(s.timers.size,1);s.poller.suspend();
""")


    def test_u04_revocation_applicability_checks_all_identity_dimensions(self):
        self.node(r"""
const instruction={timestamp:f.view.timestamp,scope:clone(f.view.scope),member_revision:f.view.member_revision,
 rank:f.view.members[0].rank,binding_revision:f.view.members[0].binding_revision,reason:'binding_invalid'};
for(const reason of ['binding_invalid','data_inconsistent','no_local_win'])
 assert.equal(api.hasApplicableRevocations(f.view,[{...instruction,reason}]),true);
for(const mismatch of [{timestamp:instruction.timestamp+1},{scope:{...instruction.scope,season_id:'old'}},
 {member_revision:'old'},{rank:1001},{binding_revision:'old'},{reason:'read_failed'}])
 assert.equal(api.hasApplicableRevocations(f.view,[{...instruction,...mismatch}]),false);
assert.equal(api.hasApplicableRevocations(f.view,[]),false);
assert.equal(api.hasApplicableRevocations(null,[instruction]),false);
""")


if __name__ == '__main__':
    unittest.main()
