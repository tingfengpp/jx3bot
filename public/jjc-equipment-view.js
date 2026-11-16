/* Whole-board equipment contract. No requests or DOM side effects at import. */
(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.JjcEquipmentView = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";
    const RANGES = {top_50: 50, top_100: 100, top_200: 200, top_1000: 1000};
    const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
    const canonical = value => JSON.stringify(value && typeof value === "object" && !Array.isArray(value)
        ? Object.fromEntries(Object.keys(value).sort().map(key => [key, JSON.parse(canonical(value[key]))]))
        : Array.isArray(value) ? value.map(item => JSON.parse(canonical(item))) : value);
    const stateOf = member => ["legendary", "purple"].includes(member.local_win_packet?.equipment_state)
        ? member.local_win_packet.equipment_state : "unknown";
    function visibleMembers(view, rangeKey, mode = "all") {
        if (!view || !RANGES[rangeKey]) return [];
        return (view.members || []).filter(m => m.rank <= RANGES[rangeKey] && (mode !== "purple" || stateOf(m) === "purple"))
            .sort((a, b) => a.rank - b.rank).map(member => {
                const shown = clone(member), packet = member.local_win_packet || {}, known = stateOf(member) !== "unknown";
                Object.assign(shown, {equipment_source: "local_win_v2", equipment_state: stateOf(member), weapon_name: known ? packet.weapon_name ?? null : null,
                    weapon_quality: known ? packet.weapon_quality ?? null : null, weapon_icon: known ? packet.weapon_icon ?? null : null,
                    self_kungfu_id: packet.self_kungfu_id ?? null, self_icon_id: packet.self_icon_id ?? null,
                    teammates: clone(packet.teammates || []), weapon_checked: known, teammates_checked: packet.team_ready === true});
                return shown;
            });
    }
    function metrics(rows) {
        const N = rows.length, L = rows.filter(m => stateOf(m) === "legendary").length,
            P = rows.filter(m => stateOf(m) === "purple").length;
        return {N, L, P, U: N - L - P, legendary_ratio: N ? L / N : null,
            equipment_coverage: N ? (L + P) / N : null,
            purple_ranks: rows.filter(m => stateOf(m) === "purple").map(m => m.rank)};
    }
    function equipmentStatistics(view, mode = "all") {
        if (!view) return {};
        const stats = {};
        for (const key of Object.keys(RANGES)) {
            const rows = visibleMembers(view, key, mode), all = visibleMembers(view, key, "all"), counts = metrics(rows);
            const scores = rows.map(m => m.score).filter(n => typeof n === "number" && Number.isFinite(n));
            const result = {equipment_source: "local_win_v2", equipment_view_revision: view.equipment_view_revision,
                equipment_metrics: counts, source_equipment_metrics: metrics(all), purple_ranks: counts.purple_ranks,
                total_players: rows.length, unknown_members: rows.filter(m => !["healer", "dps"].includes(m.lane)),
                kungfu_cache: {coverage: all.length ? all.filter(m => ["healer", "dps"].includes(m.lane)).length / all.length : null,
                    unknown: all.filter(m => !["healer", "dps"].includes(m.lane)).length}};
            for (const lane of ["healer", "dps"]) {
                const groups = {};
                for (const row of rows.filter(m => m.lane === lane)) (groups[row.kungfu || "未知"] ||= []).push(row);
                const names = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length || groups[a][0].rank - groups[b][0].rank);
                result[lane] = {equipment_source: "local_win_v2", equipment_view_revision: view.equipment_view_revision,
                    equipment_incomplete: false, valid_count: rows.filter(m => m.lane === lane).length,
                    distribution: Object.fromEntries(names.map(k => [k, groups[k].length])),
                    list: names.map(k => [k, groups[k].length]), members: groups,
                    min_score: scores.length ? Math.min(...scores) : null,
                    equipment_metrics: Object.fromEntries(names.map(k => [k, metrics(groups[k])])),
                    legendary_count_map: Object.fromEntries(names.map(k => [k, metrics(groups[k]).L]))};
            }
            result.total_valid_count = result.healer.valid_count + result.dps.valid_count;
            stats[key] = result;
        }
        return stats;
    }
    function applies(view, instruction, member) {
        return instruction.timestamp === view.timestamp && instruction.member_revision === view.member_revision
            && canonical(instruction.scope) === canonical(view.scope) && member.rank === instruction.rank
            && member.binding_revision === instruction.binding_revision;
    }
    function hasApplicableRevocations(view, instructions) {
        return Boolean(view && (instructions || []).some(instruction =>
            ["binding_invalid", "data_inconsistent", "no_local_win"].includes(instruction.reason)
            && (view.members || []).some(member => applies(view, instruction, member))));
    }
    function mask(view, instructions) {
        if (!view) return null;
        const next = clone(view);
        for (const instruction of instructions) for (const member of next.members || []) {
            if (!applies(next, instruction, member)) continue;
            member.local_win_packet = {...(member.local_win_packet || {}), source: "local_win_v2", equipment_state: "unknown",
                weapon_name: null, weapon_quality: null, weapon_icon: null, teammates: [],
                self_kungfu_id: null, self_icon_id: null, match_id: null, match_time: null,
                missing_reasons: {binding: instruction.reason}};
            member.equipment_status = instruction.reason;
        }
        next.ranges = Object.fromEntries(Object.keys(RANGES).map(key => {
            const selected = visibleMembers(next, key), value = metrics(selected), groups = {};
            for (const m of selected) ((groups[m.lane || "unknown"] ||= {})[m.kungfu || "未知"] ||= []).push(m);
            value.groups = Object.fromEntries(Object.entries(groups).map(([lane, names]) =>
                [lane, Object.fromEntries(Object.entries(names).map(([name, members]) => [name, metrics(members)]))]));
            return [key, value];
        }));
        return next;
    }
    function applyResponse(state, response) {
        const next = clone(state), incoming = clone(response || {});
        next.revocations ||= [];
        for (const instruction of incoming.revocations || []) {
            if (!["binding_invalid", "data_inconsistent", "no_local_win"].includes(instruction.reason)) continue;
            const same = value => value.timestamp === instruction.timestamp && value.member_revision === instruction.member_revision
                && value.rank === instruction.rank && value.binding_revision === instruction.binding_revision
                && canonical(value.scope) === canonical(instruction.scope);
            const previous = next.revocations.findIndex(same);
            if (previous < 0) next.revocations.push(instruction);
            else if (next.revocations[previous].confirmed_at <= instruction.confirmed_at) next.revocations[previous] = instruction;
        }
        const manifest = next.manifest || {};
        const changed = incoming.current_member_revision != null && incoming.current_member_revision !== manifest.member_revision
            || incoming.current_frozen_source != null && incoming.current_frozen_source !== manifest.frozen_source;
        if (incoming.reload_required || changed || next.reloadRequired) {
            next.reloadRequired = true; next.current = null;
            next.failed = Boolean(incoming.read_failed || incoming.status === "read_failed");
            return next;
        }
        if (incoming.read_failed || incoming.status === "read_failed") {
            next.failed = true; next.current = mask(next.current, next.revocations); return next;
        }
        next.failed = false;
        if (incoming.overlay_enabled === false || incoming.retired) next.current = mask(next.frozen, next.revocations);
        else if (incoming.view) {
            // A later successful verification may release this binding's mask,
            // even if content_revision is unchanged. A stale success cannot.
            next.revocations = next.revocations.filter(instruction => {
                const member = (incoming.view.members || []).find(m => applies(incoming.view, instruction, m));
                return !member || !(incoming.view.checked_at > instruction.confirmed_at
                    && ["legendary", "purple"].includes(stateOf(member)));
            });
            next.current = mask(incoming.view, next.revocations);
        }
        return next;
    }
    function freezeView(state) { return state.reloadRequired ? null : clone(state.current); }
    function createPoller(options) {
        let active = false, stopped = false, epoch = 0, timer = null, pending = null, failures = 0;
        const cancel = () => {if (timer != null) options.cancelSchedule(timer); timer = null;};
        const schedule = delay => {cancel(); if (active && !stopped) timer = options.schedule(() => {timer = null; tick();}, delay);};
        async function tick() {
            if (!active || stopped || pending || timer != null) return;
            const context = options.getContext();
            if (!context?.visible || context.exporting || context.tab === "peak-game" || !context.timestamp || !context.manifest) {schedule(15000); return;}
            const own = epoch, controller = new AbortController(); pending = controller;
            try {
                const data = await options.fetchView({timestamp: context.timestamp,
                    expected_member_revision: context.manifest.member_revision,
                    expected_frozen_source: context.manifest.frozen_source, signal: controller.signal});
                const now = options.getContext();
                if (!active || own !== epoch || controller.signal.aborted || !now.visible || now.exporting
                    || String(now.timestamp) !== String(context.timestamp) || canonical(now.manifest) !== canonical(context.manifest)) return;
                options.applyResponse(data); failures = 0;
                if (data.overlay_enabled === false || data.retired || data.reload_required) stopped = true;
            } catch (error) {
                if (active && own === epoch && !controller.signal.aborted) {failures++; options.onError(error);}
            } finally {
                pending = null;
                if (own !== epoch) {if (active && !stopped) schedule(0);}
                else schedule(failures ? Math.min(60000, 15000 * 2 ** failures) : 15000);
            }
        }
        return {activate() {active = true; stopped = false; failures = 0; cancel(); tick();},
            suspend() {active = false; epoch++; cancel(); if (pending) pending.abort();}, tick};
    }
    return {CONTRACT: 2, equipmentStatistics, visibleMembers, applyResponse, freezeView, createPoller, hasApplicableRevocations};
});
