"""Dependency-neutral validation of indicator cache wire fields (no I/O)."""
import math
from typing import Any, Dict, Optional


def indicator_cache_age(cached_at: Any, now: float) -> Optional[float]:
    if type(cached_at) not in (int, float):
        return None
    try:
        timestamp = float(cached_at)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > now:
        return None
    return now - timestamp


def normalize_indicator_scope(scope: Any) -> Optional[Dict[str, Any]]:
    """Validate structure only; the service decides the current season."""
    if not isinstance(scope, dict):
        return None
    season = scope.get("season_id")
    start = scope.get("season_start")
    if (not isinstance(season, str) or not season.strip()
            or type(start) is not int or start <= 0
            or type(scope.get("pvp_type")) is not int or scope["pvp_type"] != 3):
        return None
    return {"season_id": season, "season_start": start, "pvp_type": 3}
