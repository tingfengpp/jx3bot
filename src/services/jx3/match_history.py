from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

try:
    from nonebot import logger  # type: ignore
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


from src.utils.jjc_query_observability import foreground_safe_logger

logger = foreground_safe_logger(logger, component="inspect")

@dataclass(frozen=True)
class MatchHistoryClient:
    """
    推栏：分页获取用户 3c 战局历史

    接口: POST https://m.pvp.xoyo.com/3c/mine/match/history
    body: {"global_role_id": "...", "size": 20, "cursor": 0, "ts": "..."}  (ts 由 tuilan_request 自动补充)
    """

    match_history_url: str
    tuilan_request: Callable[[str, dict[str, Any]], Any]

    def get_mine_match_history(
        self,
        *,
        global_role_id: str,
        size: int = 20,
        cursor: int = 0,
    ) -> dict[str, Any]:
        url = self.match_history_url
        params = {"global_role_id": global_role_id, "size": int(size), "cursor": int(cursor)}

        logger.info(f"推栏战局历史请求: url={url} params={json.dumps(params, ensure_ascii=False)}")

        try:
            result = self.tuilan_request(url, params)

            if result is None:
                logger.warning("推栏战局历史请求失败: 返回None")
                return {"error": "请求返回None"}

            if isinstance(result, dict) and "error" in result:
                logger.warning("推栏战局历史请求失败: {}", result.get("error"))
                return result

            logger.info("推栏战局历史请求成功")
            return result
        except Exception as exc:
            logger.exception("推栏战局历史请求异常: {}", exc)
            return {"error": f"请求异常: {exc}"}

    def iter_mine_match_history(
        self,
        *,
        global_role_id: str,
        size: int = 20,
        cursor: int = 0,
        max_pages: int = 10,
    ) -> Iterable[dict[str, Any]]:
        """
        迭代拉取分页数据（cursor 默认按 offset 递增）。

        注意：推栏侧 cursor 的语义以实际返回为准；如接口采用不同 cursor 规则，可调用 get_mine_match_history 手工控制。
        """
        if max_pages <= 0:
            return

        current_cursor = int(cursor)
        page_size = int(size)
        for _ in range(int(max_pages)):
            payload = self.get_mine_match_history(
                global_role_id=global_role_id,
                size=page_size,
                cursor=current_cursor,
            )
            yield payload

            if not isinstance(payload, dict):
                break

            data = payload.get("data")
            if not isinstance(data, list) or not data:
                break

            if len(data) < page_size:
                break

            current_cursor += page_size


@dataclass(frozen=True)
class PersonMatchHistoryClient:
    """
    推栏：按 person_id 分页获取个人战局历史。

    接口: POST https://m.pvp.xoyo.com/mine/match/person-history
    body: {"person_id": "...", "size": 20, "cursor": 0, "ts": "..."}  (ts 由 tuilan_request 自动补充)
    """

    person_match_history_url: str
    tuilan_request: Callable[[str, dict[str, Any]], Any]

    def get_person_match_history(
        self,
        *,
        person_id: str,
        size: int = 20,
        cursor: int = 0,
    ) -> dict[str, Any]:
        url = self.person_match_history_url
        params = {"person_id": person_id, "size": int(size), "cursor": int(cursor)}

        logger.info(f"推栏个人战局历史请求: url={url} params={json.dumps(params, ensure_ascii=False)}")

        try:
            result = self.tuilan_request(url, params)

            if result is None:
                logger.warning("推栏个人战局历史请求失败: 返回None")
                return {"error": "请求返回None"}

            if isinstance(result, dict) and "error" in result:
                logger.warning("推栏个人战局历史请求失败: {}", result.get("error"))
                return result

            logger.info("推栏个人战局历史请求成功")
            return result
        except Exception as exc:
            logger.exception("推栏个人战局历史请求异常: {}", exc)
            return {"error": f"请求异常: {exc}"}
