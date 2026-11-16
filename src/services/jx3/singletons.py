from __future__ import annotations

from typing import Any, Optional
import time

import config as cfg
from jinja2 import Environment, FileSystemLoader

from src.infra.jx3api_get import get
from src.infra.tuilan_foreground_client import TuilanForegroundClient
from src.storage.mongo_repos.group_config_repo import GroupConfigRepo
from src.services.jx3.jjc_ranking_inspect import JjcRankingInspectService
from src.services.jx3.jjc_ranking import JjcRankingService
from src.services.jx3.jjc_matchup import JjcMatchupService
from src.services.jx3.jjc_match_data_sync import JjcMatchDataSyncService, parse_season_start_timestamp
from src.storage.mongo_repos.jjc_sync_repo import JjcSyncRepo
from src.services.jx3.jjc_cache_repo import JjcCacheRepo
from src.services.jx3.kungfu import get_role_indicator
from src.services.jx3.match_history import MatchHistoryClient, PersonMatchHistoryClient
from src.services.jx3.match_detail import MatchDetailClient
from src.services.jx3.match_detail_identity_projection import MatchDetailIdentityProjectionService
from src.services.jx3.match_detail_fact_projection import MatchDetailFactProjectionService
from src.services.jx3.match_replay import MatchReplayClient
from src.services.jx3.role_indicator import RoleIndicatorClient
from src.storage.mongo_repos.jjc_inspect_repo import JjcInspectRepo
from src.storage.mongo_repos.jjc_match_participant_repo import JjcMatchParticipantRepo
from src.storage.mongo_repos.jjc_match_fact_repo import JjcMatchFactRepo
from src.storage.mongo_repos.jjc_match_snapshot_repo import JjcMatchSnapshotRepo
from src.storage.mongo_repos.role_identity_repo import RoleIdentityRepo
from src.services.jx3.match_detail_participant_projection import MatchDetailParticipantProjectionService
from src.services.jx3.match_detail_persistence import MatchDetailPersistenceService
from src.utils.tuilan_request import tuilan_request

env = Environment(loader=FileSystemLoader("templates"))
group_config_repo = GroupConfigRepo()

KUNGFU_PINYIN_TO_CHINESE = {key: value["name"] for key, value in cfg.KUNGFU_META.items()}
KUNGFU_HEALER_LIST = [
    value["name"] for value in cfg.KUNGFU_META.values() if value.get("category") == "healer"
]
KUNGFU_DPS_LIST = [value["name"] for value in cfg.KUNGFU_META.values() if value.get("category") == "dps"]

JJC_RANKING_CACHE_DURATION = 7200  # 缓存时间2小时（秒）
KUNGFU_CACHE_DURATION = 7 * 24 * 60 * 60  # 心法缓存有效期一周（秒）

jjc_sync_repo = JjcSyncRepo()
role_identity_repo = RoleIdentityRepo()
match_participant_repo: Any = JjcMatchParticipantRepo()
jjc_match_fact_repo: Any = JjcMatchFactRepo()
jjc_matchup_service = JjcMatchupService(
    matchup_repo=jjc_match_fact_repo,
    season_start_time=parse_season_start_timestamp(cfg.CURRENT_SEASON_START),
)

match_detail_participant_projection_service: Any = MatchDetailParticipantProjectionService(
    participant_repo=match_participant_repo,
    sync_repo=jjc_sync_repo,
    current_season=cfg.CURRENT_SEASON,
    season_start_time=parse_season_start_timestamp(cfg.CURRENT_SEASON_START),
)


def _new_jjc_inspect_repo(snapshot_repo: Optional[JjcMatchSnapshotRepo] = None) -> JjcInspectRepo:
    kwargs: dict[str, Any] = {}
    if snapshot_repo is not None:
        kwargs["snapshot_repo"] = snapshot_repo
    kwargs["participant_repo"] = match_participant_repo
    return JjcInspectRepo(**kwargs)


match_detail_identity_projection_service = MatchDetailIdentityProjectionService(
    identity_repo=role_identity_repo,
    sync_repo=jjc_sync_repo,
    kungfu_pinyin_to_chinese=KUNGFU_PINYIN_TO_CHINESE,
)
match_detail_fact_projection_service: Any = MatchDetailFactProjectionService(
    fact_repo=jjc_match_fact_repo,
)

jjc_inspect_repo = _new_jjc_inspect_repo(snapshot_repo=JjcMatchSnapshotRepo())
match_detail_persistence_service = MatchDetailPersistenceService(
    inspect_repo=jjc_inspect_repo,
    identity_projector=match_detail_identity_projection_service,
    participant_projector=match_detail_participant_projection_service,
    fact_projector=match_detail_fact_projection_service,
)

from src.infra.tuilan_kungfu_client import TuilanKungfuClient
from src.services.jx3.jjc_kungfu_policy import policy_from_config
from src.services.jx3.jjc_kungfu_refresh import JjcKungfuRefreshService
from src.storage.mongo_repos.jjc_kungfu_refresh_repo import JjcKungfuRefreshRepo
from src.storage.mongo_repos.jjc_ranking_stats_repo import JjcRankingStatsRepo
from src.services.jx3.jjc_ranking_display import JjcRankingDisplayService

jjc_kungfu_refresh_repo = JjcKungfuRefreshRepo()
jjc_kungfu_refresh_service = JjcKungfuRefreshService(
    identity_repo=role_identity_repo,
    queue_repo=jjc_kungfu_refresh_repo,
    client=TuilanKungfuClient(display_enabled=getattr(cfg, "JJC_RANKING_DISPLAY_QUERY_ENABLED", 0) == 1),
    policy=policy_from_config,
    mapping=KUNGFU_PINYIN_TO_CHINESE,
)

jjc_ranking_display_service = JjcRankingDisplayService(
    stats_repo=JjcRankingStatsRepo(suppress_errors=False),
    identity_repo=role_identity_repo,
    policy=policy_from_config,
    enabled=lambda: getattr(cfg, "JJC_RANKING_DISPLAY_OVERLAY_ENABLED", 0) == 1,
    clock=time.time,
)

# Equipment reads own a lazy bounded pool; frozen compatibility reads use the
# ordinary stats repository so disabling new aggregation never hides schema2.
from src.infra.jjc_equipment_read_runtime import EquipmentReadRuntime
from src.services.jx3.jjc_ranking_equipment_view import JjcRankingEquipmentViewService

jjc_equipment_read_runtime = EquipmentReadRuntime(
    enabled=lambda: getattr(cfg, "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", 0) == 1,
    uri=cfg.MONGO_URI,
    host_override=getattr(cfg, "JJC_EQUIPMENT_MONGO_HOST", ""),
)


class _EquipmentReadDatabase:
    def __getattr__(self, name):
        return getattr(jjc_equipment_read_runtime.get_db(), name)

    def __getitem__(self, name):
        return self.__getattr__(name)


_equipment_db = _EquipmentReadDatabase()
jjc_ranking_equipment_service = JjcRankingEquipmentViewService(
    stats_repo=JjcRankingStatsRepo(suppress_errors=False),
    dynamic_stats_repo=JjcRankingStatsRepo(db=_equipment_db, suppress_errors=False),
    identity_repo=RoleIdentityRepo(db=_equipment_db),
    inspect_repo=JjcInspectRepo(db=_equipment_db, snapshot_repo=JjcMatchSnapshotRepo(db=_equipment_db), participant_repo=None),
    snapshot_repo=JjcMatchSnapshotRepo(db=_equipment_db),
    scope=policy_from_config,
    enabled=lambda: getattr(cfg, "JJC_RANKING_LOCAL_WIN_EQUIPMENT_ENABLED", 0) == 1,
    overlay_enabled=lambda: getattr(cfg, "JJC_RANKING_DISPLAY_OVERLAY_ENABLED", 0) == 1,
)

jjc_ranking_service = JjcRankingService(
    equipment_service=jjc_ranking_equipment_service,
    token=cfg.TOKEN,
    ticket=cfg.TICKET,
    jjc_query_url=cfg.API_URLS["竞技查询"],
    arena_time_tag_url=cfg.API_URLS["竞技场时间查询"],
    arena_ranking_url=cfg.API_URLS["竞技场排行榜查询"],
    match_detail_url=cfg.API_URLS["竞技场战局详情"],
    jjc_ranking_cache_duration=JJC_RANKING_CACHE_DURATION,
    kungfu_cache_duration=KUNGFU_CACHE_DURATION,
    current_season=cfg.CURRENT_SEASON,
    current_season_start=cfg.CURRENT_SEASON_START,
    kungfu_healer_list=KUNGFU_HEALER_LIST,
    kungfu_dps_list=KUNGFU_DPS_LIST,
    kungfu_pinyin_to_chinese=KUNGFU_PINYIN_TO_CHINESE,
    tuilan_request=tuilan_request,
    defget_get=get,
    match_replay_url=cfg.API_URLS["竞技场战局回放"],
    match_detail_projection_service=match_detail_identity_projection_service,
    match_detail_participant_projection_service=match_detail_participant_projection_service,
    match_detail_persistence_service=match_detail_persistence_service,
    inspect_repo=jjc_inspect_repo,
    identity_repo=role_identity_repo,
    kungfu_refresh_repo=jjc_kungfu_refresh_repo,
    kungfu_query_service=jjc_kungfu_refresh_service,
    kungfu_policy=policy_from_config,
)

match_detail_client = MatchDetailClient(
    match_detail_url=cfg.API_URLS["竞技场战局详情"],
    tuilan_request=tuilan_request,
)

match_history_client = MatchHistoryClient(
    match_history_url=cfg.API_URLS["竞技场战局历史"],
    tuilan_request=tuilan_request,
)

person_match_history_client = PersonMatchHistoryClient(
    person_match_history_url=cfg.API_URLS["竞技场个人战局历史"],
    tuilan_request=tuilan_request,
)

match_replay_client = MatchReplayClient(
    match_replay_url=cfg.API_URLS["竞技场战局回放"],
    tuilan_request=tuilan_request,
)

role_indicator_client = RoleIndicatorClient(
    role_indicator_url=cfg.API_URLS["推栏角色指标"],
    tuilan_request=tuilan_request,
)


# Assembly only: clients remain loop-local and lazy until application lifecycle.
jjc_foreground_client = TuilanForegroundClient(endpoint_urls={
    'role_indicator': cfg.API_URLS['推栏角色指标'],
    'match_history': cfg.API_URLS['竞技场战局历史'],
    'arena_time_tag': cfg.API_URLS['竞技场时间查询'],
    'arena_ranking': cfg.API_URLS['竞技场排行榜查询'],
    'match_detail': cfg.API_URLS['竞技场战局详情'],
    'match_replay': cfg.API_URLS['竞技场战局回放'],
})

jjc_ranking_inspect_service = JjcRankingInspectService(
    foreground_client=jjc_foreground_client,
    ranking_service=jjc_ranking_service,
    kungfu_cache_repo=JjcCacheRepo(
        jjc_ranking_cache_duration=JJC_RANKING_CACHE_DURATION,
        kungfu_cache_duration=KUNGFU_CACHE_DURATION,
    ),
    match_history_client=match_history_client,
    match_detail_client=match_detail_client,
    match_replay_client=match_replay_client,
    cache_repo=jjc_inspect_repo,
    tuilan_request=tuilan_request,
    role_indicator_fetcher=get_role_indicator,
    kungfu_pinyin_to_chinese=KUNGFU_PINYIN_TO_CHINESE,
    match_detail_projection_service=match_detail_identity_projection_service,
    match_detail_participant_projection_service=match_detail_participant_projection_service,
    match_detail_persistence_service=match_detail_persistence_service,
    current_season=cfg.CURRENT_SEASON,
    season_start_time=parse_season_start_timestamp(cfg.CURRENT_SEASON_START),
    role_recent_ttl_seconds=86400,
)

jjc_match_data_sync_service = JjcMatchDataSyncService(
    repo=jjc_sync_repo,
    current_season=cfg.CURRENT_SEASON,
    current_season_start=cfg.CURRENT_SEASON_START,
    match_history_client=match_history_client,
    person_match_history_client=person_match_history_client,
    match_replay_client=match_replay_client,
    role_indicator_client=role_indicator_client,
    inspect_service=jjc_ranking_inspect_service,
    identity_repo=role_identity_repo,
    match_detail_projection_service=match_detail_identity_projection_service,
    match_detail_participant_projection_service=match_detail_participant_projection_service,
    match_detail_persistence_service=match_detail_persistence_service,
    dispatcher_idle_sleep=getattr(cfg, "JJC_SYNC_DISPATCHER_IDLE_SLEEP", 10),
    dispatcher_batch_size=getattr(cfg, "JJC_SYNC_DISPATCHER_BATCH_SIZE", 20),
    dispatcher_target_per_worker=getattr(cfg, "JJC_SYNC_DISPATCHER_TARGET_PER_WORKER", 3),
)

from src.services.jx3.jjc_live_refresh import JjcLiveRefreshService
from src.storage.mongo_repos.jjc_live_refresh_repo import JjcLiveRefreshRepo

jjc_live_refresh_service = JjcLiveRefreshService(
    identity_repo=role_identity_repo,
    history_client=match_history_client,
    indicator_client=role_indicator_client,
    season=cfg.CURRENT_SEASON,
    season_start=parse_season_start_timestamp(cfg.CURRENT_SEASON_START),
    names=KUNGFU_PINYIN_TO_CHINESE,
    enabled=lambda: bool(getattr(cfg, "JJC_LIVE_REFRESH_ENABLED", 0)),
    repo=JjcLiveRefreshRepo(capacity=getattr(cfg, "JJC_LIVE_REFRESH_CAPACITY", 100),
                           cooldown=getattr(cfg, "JJC_LIVE_REFRESH_COOLDOWN", 120)),
)
