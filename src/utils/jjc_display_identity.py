"""Pure identity matching for raw match evidence; names alone never identify a role."""
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple


def text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def teams(detail: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    result = []
    for key in ("team1", "team2"):
        team = detail.get(key)
        players = team.get("players_info") if isinstance(team, dict) else None
        if isinstance(players, list):
            result.append([p for p in players if isinstance(p, dict)])
    return result


def matches_role(player: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    for key in ("server", "zone"):
        actual = text(player.get(key))
        if actual and text(identity.get(key)) and actual != text(identity[key]):
            return False
    role_id = text(player.get("role_id") or player.get("roleId"))
    expected_role = text(identity.get("game_role_id") or identity.get("role_id"))
    if role_id and expected_role and role_id != expected_role:
        return False
    global_id = text(player.get("global_id"))
    expected_global = text(identity.get("global_id"))
    sk01 = text(player.get("global_role_id"))
    expected_sk01 = text(identity.get("global_role_id"))
    if global_id and expected_global and global_id != expected_global:
        return False
    if sk01.startswith("SK01-") and expected_sk01.startswith("SK01-") and sk01 != expected_sk01:
        return False
    return bool((role_id and role_id == expected_role)
                or (global_id and global_id == expected_global)
                or (sk01.startswith("SK01-") and sk01 == expected_sk01))


def player_ids(player: Dict[str, Any]) -> set:
    ids = set()
    role = text(player.get("role_id") or player.get("roleId"))
    if role:
        zone, server = text(player.get("zone")), text(player.get("server"))
        if zone or server:
            ids.add(("role", "zone" if zone else "server", zone or server, role))
    global_id = text(player.get("global_id"))
    if global_id.isdigit():
        ids.add(("global", global_id))
    sk01 = text(player.get("global_role_id"))
    if sk01.startswith("SK01-"):
        ids.add(("sk01", sk01))
    return ids


def duplicate_player(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if player_ids(left).intersection(player_ids(right)):
        return True
    left_id = text(left.get("role_id") or left.get("roleId"))
    right_id = text(right.get("role_id") or right.get("roleId"))
    if not left_id or left_id != right_id:
        return False
    # A partially declared scope must not bypass duplicate protection. Explicitly
    # different zones disambiguate local IDs; otherwise use a shared server.
    for key in ("zone", "server"):
        if text(left.get(key)) and text(right.get(key)):
            return text(left[key]) == text(right[key])
    return True


def indicator_identity(identity: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich this attempt from already validated indicator evidence, without mutation."""
    result = dict(identity)
    info = data.get("role_info")
    if isinstance(info, dict):
        sk01 = text(info.get("global_role_id") or info.get("globalRoleId"))
        if not text(result.get("global_role_id")) and sk01.startswith("SK01-"):
            result["global_role_id"] = sk01
    return result


def _positive_number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    raw = text(value)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 32:
        return None
    number = int(raw)
    return number if number > 0 else None


def _name_server(player: Dict[str, Any], *, require_server: bool = False) -> Optional[Tuple[str, str]]:
    name, server = player.get("role_name"), player.get("server")
    if not isinstance(name, str) or not name.strip():
        return None
    if server is not None and not isinstance(server, str):
        return None
    name, server = name.strip(), text(server)
    if require_server and not server:
        return None
    if "·" in name:
        name, suffix = name.rsplit("·", 1)
        if not suffix or (server and suffix != server):
            return None
        server = suffix
    return (name, server) if name and server else None


def _replay_identity(player: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    named = _name_server(player)
    if named is None:
        return None
    normalized = dict(player)
    normalized["role_name"], normalized["server"] = named
    explicit, alias = text(player.get("global_id")), text(player.get("global_role_id"))
    numeric_alias = alias if _positive_number(alias) is not None else ""
    if explicit and numeric_alias and explicit != numeric_alias:
        return None
    global_id = explicit or numeric_alias
    if _positive_number(global_id) is None or str(_positive_number(global_id)) != global_id:
        return None
    if alias and not numeric_alias and not alias.startswith("SK01-"):
        return None
    normalized["global_id"] = global_id
    local, local_alias = text(player.get("role_id")), text(player.get("roleId"))
    if local and local_alias and local != local_alias:
        return None
    if local or local_alias:
        role_id = local or local_alias
        if _positive_number(role_id) is None or str(_positive_number(role_id)) != role_id:
            return None
        normalized["role_id"] = role_id
    return normalized


def _bridge_detail(detail: Dict[str, Any], identity: Dict[str, Any],
                   replay: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Join complete same-match rosters after authenticating a replay identity.

    A name never selects the target. Six unique compound keys connect a replay
    that already proves the target by stable IDs to the detail equipment source.
    Failure leaves independently trustworthy detail evidence available.
    """
    match_id = _positive_number(detail.get("match_id"))
    if match_id is None or match_id != _positive_number(replay.get("match_id")):
        return None
    players = replay.get("players")
    if not isinstance(players, list) or len(players) != 6 or any(not isinstance(p, dict) for p in players):
        return None
    replay_players = [_replay_identity(player) for player in players]
    if any(player is None for player in replay_players):
        return None
    if sum(matches_role(player, identity) for player in replay_players) != 1:
        return None
    replay_by_key, globals_seen = {}, set()
    for player in replay_players:
        team, kungfu = player.get("team"), _positive_number(player.get("kungfu_id"))
        if not isinstance(team, str) or not team.strip() or kungfu is None:
            return None
        key = (player["role_name"], player["server"], kungfu, team.strip())
        global_id = player["global_id"]
        if key in replay_by_key or global_id in globals_seen:
            return None
        replay_by_key[key] = player
        globals_seen.add(global_id)
    result, teams_seen, keys_seen, hydrated = deepcopy(detail), set(), set(), []
    for side in ("team1", "team2"):
        team = result.get(side)
        if not isinstance(team, dict):
            return None
        name, roster = team.get("team_name"), team.get("players_info")
        if (not isinstance(name, str) or not name.strip() or name.strip() in teams_seen
                or not isinstance(roster, list) or len(roster) != 3
                or any(not isinstance(player, dict) for player in roster)):
            return None
        name = name.strip()
        teams_seen.add(name)
        for player in roster:
            named, kungfu = _name_server(player, require_server=True), _positive_number(player.get("kungfu_id"))
            if named is None or kungfu is None:
                return None
            key = (*named, kungfu, name)
            other = replay_by_key.get(key)
            if other is None or key in keys_seen:
                return None
            keys_seen.add(key)
            # All declarations remain evidence; never overwrite a conflicting ID.
            local, local_alias = text(player.get("role_id")), text(player.get("roleId"))
            if local and local_alias and local != local_alias:
                return None
            for field in ("global_id", "zone"):
                if text(player.get(field)) and text(other.get(field)) and text(player[field]) != text(other[field]):
                    return None
            if (text(player.get("global_role_id")).startswith("SK01-")
                    and text(other.get("global_role_id")).startswith("SK01-")
                    and text(player["global_role_id"]) != text(other["global_role_id"])):
                return None
            numeric_global = _positive_number(player.get("global_role_id"))
            if numeric_global is not None and str(numeric_global) != other["global_id"]:
                return None
            other_local = text(other.get("role_id"))
            if (local or local_alias) and other_local and (local or local_alias) != other_local:
                return None
            role_id = local or local_alias or other_local
            if role_id:
                if not local and not local_alias:
                    player["role_id"] = role_id
            if not text(player.get("global_id")):
                player["global_id"] = other["global_id"]
            # Compare all declarations from both sources without adding aliases
            # or scope to the public detail projection.
            comparison = dict(player)
            if not text(comparison.get("zone")) and text(other.get("zone")):
                comparison["zone"] = other["zone"]
            if (not text(comparison.get("global_role_id")).startswith("SK01-")
                    and text(other.get("global_role_id")).startswith("SK01-")):
                comparison["global_role_id"] = other["global_role_id"]
            if any(duplicate_player(previous, comparison) for previous in hydrated):
                return None
            hydrated.append(comparison)
    return result if keys_seen == set(replay_by_key) else None


def needs_replay(detail: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    target, team = target_team(detail, identity)
    if target is None:
        return True
    for player in team:
        local = text(player.get("role_id") or player.get("roleId"))
        if not ((local and (text(player.get("zone")) or text(player.get("server"))))
                or _positive_number(player.get("global_id")) is not None
                or text(player.get("global_role_id")).startswith("SK01-")):
            return True
    return False

def target_team(detail: Dict[str, Any], identity: Dict[str, Any],
                replay: Optional[Dict[str, Any]] = None
                ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if isinstance(replay, dict):
        detail = _bridge_detail(detail, identity, replay) or detail
    linked_identity = dict(identity)
    if replay:
        players = replay.get("players")
        if isinstance(players, list):
            candidates = []
            for player in players:
                if not isinstance(player, dict):
                    continue
                # Legacy enrichment must validate the same aliases as the bridge.
                player = _replay_identity(player)
                if player is not None and matches_role(player, identity):
                    numeric_id = player["global_id"]
                    known = text(identity.get("global_id"))
                    if not known or known == numeric_id:
                        candidates.append(numeric_id)
            if len(candidates) == 1:
                linked_identity["global_id"] = candidates[0]
    found = [(player, team) for team in teams(detail) for player in team
             if matches_role(player, linked_identity)]
    return found[0] if len(found) == 1 else (None, [])
