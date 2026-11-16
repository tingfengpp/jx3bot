"""Lazy ownership of the equipment-only bounded Motor read connection.

No database client is created at import or while this optional feature is off.
This adapter does not mutate the ordinary sync/foreground Mongo clients.
"""
import inspect
import ipaddress
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from typing import Any, Callable, Optional

from motor.motor_asyncio import AsyncIOMotorClient


class EquipmentReadRuntimeError(RuntimeError):
    """A bounded local read runtime could not provide a usable database."""


def _equipment_uri(uri, override):
    if override == "":
        return uri
    if not isinstance(override, str):
        raise ValueError("invalid_equipment_host")
    address = ipaddress.IPv4Address(override)
    if not any(address in ipaddress.IPv4Network(network) for network in
            ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("invalid_equipment_host")
    parsed = urlsplit(uri)
    if parsed.scheme != "mongodb" or not parsed.hostname or parsed.fragment:
        raise ValueError("invalid_equipment_uri")
    # Never weaken TLS or hostname validation to adapt an internal endpoint.
    if any(key.lower() in ("tls", "ssl") and value.lower() == "true"
           for key, value in parse_qsl(parsed.query)):
        raise ValueError("unsupported_equipment_tls_override")
    auth = parsed.netloc[:parsed.netloc.rfind("@") + 1]
    port = ":" + str(parsed.port) if parsed.port is not None else ""
    return urlunsplit(parsed._replace(netloc=auth + str(address) + port))


class EquipmentReadRuntime:
    def __init__(self, *, enabled: Any, uri: str,
                 client_factory: Optional[Callable[..., Any]] = None, host_override: Any = "") -> None:
        self.enabled = enabled
        self.uri = uri
        self.host_override = host_override
        self._client_factory = AsyncIOMotorClient if client_factory is None else client_factory
        self._client = None
        self._db = None
        self._closed = False

    def _enabled(self) -> bool:
        try:
            value = self.enabled() if callable(self.enabled) else self.enabled
        except Exception:
            return False
        return value is True or (type(value) is int and value == 1)

    def get_db(self) -> Any:
        if self._closed:
            raise EquipmentReadRuntimeError("equipment_read_closed")
        if not self._enabled():
            raise EquipmentReadRuntimeError("equipment_read_disabled")
        if self._db is not None:
            return self._db
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise EquipmentReadRuntimeError("equipment_read_unavailable")
        client = None
        try:
            client = self._client_factory(
                _equipment_uri(self.uri, self.host_override), maxPoolSize=4, minPoolSize=0,
                compressors=["zlib"], zlibCompressionLevel=1,
                serverSelectionTimeoutMS=1500, connectTimeoutMS=1500,
                socketTimeoutMS=2000, waitQueueTimeoutMS=500, connect=False,
            )
            db = client.get_default_database()
            if db is None:
                raise EquipmentReadRuntimeError("equipment_read_unavailable")
        except Exception:
            # A failed get_default_database can still leave an owned client.
            # Motor.close is synchronous, including before first connection.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raise EquipmentReadRuntimeError("equipment_read_unavailable") from None
        self._client, self._db = client, db
        return db

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        self._db = None
        if client is not None:
            result = client.close()
            if inspect.isawaitable(result):
                await result
