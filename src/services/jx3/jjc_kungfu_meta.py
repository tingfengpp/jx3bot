"""Server-side catalog for kungfus displayed by the JJC matchup page."""

from typing import Any, Dict, List, Tuple

from config import KUNGFU_META


# Keep the legacy ranking page's display order while the old page continues to
# use its JavaScript map.  The mapped icon ID is also the JJC kungfu ID.
_JJC_KUNGFU_CATALOG_SOURCE: Tuple[Tuple[str, int, str], ...] = (
    ("易筋经", 10003, "dps"),
    ("紫霞功", 10014, "dps"),
    ("太虚剑意", 10015, "dps"),
    ("花间游", 10021, "dps"),
    ("离经易道", 10028, "healer"),
    ("傲血战意", 10026, "dps"),
    ("铁牢律", 10062, "tank"),
    ("冰心诀", 10081, "dps"),
    ("云裳心经", 10080, "healer"),
    ("毒经", 10175, "dps"),
    ("补天诀", 10176, "healer"),
    ("山居剑意", 10145, "dps"),
    ("问水诀", 10144, "dps"),
    ("天罗诡道", 10225, "dps"),
    ("惊羽诀", 10224, "dps"),
    ("分山劲", 10390, "dps"),
    ("铁骨衣", 10389, "tank"),
    ("笑尘诀", 10268, "dps"),
    ("焚影圣诀", 10242, "dps"),
    ("明尊琉璃体", 10243, "tank"),
    ("莫问", 10447, "dps"),
    ("相知", 10448, "healer"),
    ("北傲诀", 10464, "dps"),
    ("凌海诀", 10533, "dps"),
    ("隐龙诀", 10585, "dps"),
    ("无方", 10627, "dps"),
    ("灵素", 10626, "healer"),
    ("孤锋诀", 10698, "dps"),
    ("山海心诀", 10756, "dps"),
    ("周天功", 10786, "dps"),
    ("太玄经", 10615, "dps"),
    ("幽罗引", 10821, "dps"),
)

_ALLOWED_CATEGORIES = frozenset({"dps", "healer", "tank"})

_CONFIG_META_BY_NAME: Dict[str, Dict[str, str]] = {}
for _meta in KUNGFU_META.values():
    if not isinstance(_meta, dict):
        continue
    _name = _meta.get("name")
    _category = _meta.get("category")
    if not isinstance(_name, str) or not _name.strip():
        continue
    _normalized_name = _name.strip()
    _config_meta = {"name": _normalized_name}
    if isinstance(_category, str) and _category.strip() in _ALLOWED_CATEGORIES:
        _config_meta["category"] = _category.strip()
    _CONFIG_META_BY_NAME[_normalized_name] = _config_meta


def get_jjc_kungfu_catalog() -> List[Dict[str, Any]]:
    """Return a fresh, display-ordered JJC kungfu catalog for API consumers."""
    catalog: List[Dict[str, Any]] = []
    for display_order, (legacy_name, kungfu_id, fallback_category) in enumerate(
        _JJC_KUNGFU_CATALOG_SOURCE,
        start=1,
    ):
        config_meta = _CONFIG_META_BY_NAME.get(legacy_name, {})
        catalog.append({
            "id": kungfu_id,
            "name": config_meta.get("name") or legacy_name,
            "category": config_meta.get("category") or fallback_category,
            "icon_id": kungfu_id,
            "display_order": display_order,
        })
    return catalog
