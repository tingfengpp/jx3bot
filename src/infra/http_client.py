from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

import httpx
import email.utils
import datetime
from nonebot import logger
from src.utils.jjc_query_observability import (foreground_safe_logger, query_stage,
    query_outbound, query_failure)

logger = foreground_safe_logger(logger, component="http")


def _retry_after_seconds(value):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            return max(1, int((parsed - now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        verify: bool = True,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.verify = verify
        self.default_headers = dict(default_headers or {})

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
        verify: bool | None = None,
    ) -> dict[str, Any]:
        merged_headers = dict(self.default_headers)
        if headers:
            merged_headers.update(headers)

        last_error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(
                    timeout=timeout or self.timeout,
                    verify=self.verify if verify is None else verify,
                    headers=merged_headers,
                ) as client:
                    query_outbound()
                    with query_stage("http"):
                        response = client.request(
                            method.upper(),
                            url,
                            params=params,
                            json=json_body,
                            content=content,
                        )
                if response.status_code >= 400:
                    query_failure("upstream_rejected")
                    error_result = {
                        "error": f"http_status_{response.status_code}",
                        "status_code": response.status_code,
                        "text": response.text,
                    }
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        error_result["retry_after_seconds"] = _retry_after_seconds(retry_after)
                    return error_result
                try:
                    return response.json()
                except Exception:
                    return {"error": "invalid_json", "status_code": response.status_code, "text": response.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                query_failure("timeout" if isinstance(exc, httpx.TimeoutException) else "transport", exc)
                last_error = str(exc)
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
                logger.warning(f"http request failed: method={method} url={url} error={exc}")
                return {"error": "request_failed", "message": last_error}
            except Exception as exc:
                logger.warning(f"http request unexpected error: method={method} url={url} error={exc}")
                return {"error": "request_failed", "message": str(exc)}

        return {"error": "request_failed", "message": last_error or "unknown"}


    async def arequest_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
        verify: bool | None = None,
    ) -> dict[str, Any]:
        merged_headers = dict(self.default_headers)
        if headers:
            merged_headers.update(headers)

        last_error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout or self.timeout,
                    verify=self.verify if verify is None else verify,
                    headers=merged_headers,
                ) as client:
                    query_outbound()
                    with query_stage("http"):
                        response = await client.request(
                            method.upper(),
                            url,
                            params=params,
                            json=json_body,
                            content=content,
                        )
                if response.status_code >= 400:
                    query_failure("upstream_rejected")
                    error_result = {
                        "error": f"http_status_{response.status_code}",
                        "status_code": response.status_code,
                        "text": response.text,
                    }
                    if response.status_code == 429:
                        error_result["retry_after"] = response.headers.get("Retry-After")
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        error_result["retry_after_seconds"] = _retry_after_seconds(retry_after)
                    return error_result
                try:
                    return response.json()
                except Exception:
                    return {"error": "invalid_json", "status_code": response.status_code, "text": response.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                query_failure("timeout" if isinstance(exc, httpx.TimeoutException) else "transport", exc)
                last_error = str(exc)
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff_seconds * (2**attempt))
                    continue
                logger.warning(f"http request failed: method={method} url={url} error={exc}")
                return {"error": "request_failed", "message": last_error}
            except Exception as exc:
                logger.warning(f"http request unexpected error: method={method} url={url} error={exc}")
                return {"error": "request_failed", "message": str(exc)}

        return {"error": "request_failed", "message": last_error or "unknown"}
