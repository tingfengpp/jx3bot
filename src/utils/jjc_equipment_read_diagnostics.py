"""Fixed-field failure diagnostics; never serialize input or exception payloads."""
import asyncio
import logging
import math

logger = logging.getLogger(__name__)
_STAGES = frozenset(("context", "latest", "qualify_before", "qualify_after",
    "binding_before", "binding_after", "binding_cached", "candidates", "snapshots",
    "construct", "read"))
_KINDS = frozenset(("ExecutionTimeout", "NetworkTimeout", "ServerSelectionTimeoutError",
    "WaitQueueTimeoutError", "ConnectionFailure", "AutoReconnect", "OperationFailure",
    "EquipmentSnapshotError", "EquipmentViewError", "TimeoutError", "ValueError", "RuntimeError"))

def report_equipment_read_failure(stage, exc, elapsed_seconds):
    if isinstance(exc, asyncio.CancelledError):
        return None
    kind = type(exc).__name__
    try:
        code = getattr(exc, "code", None)
    except BaseException:
        code = None
    if type(code) is not int or not 0 <= code <= 2147483647:
        code = None
    if type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        elapsed_seconds = 0.0
    event = {"stage": stage if isinstance(stage, str) and stage in _STAGES else "read",
        "exception_kind": kind if kind in _KINDS else "other", "code": code,
        "elapsed_seconds": elapsed_seconds}
    try:
        logger.warning("jjc_equipment_read_failure %s", event)
    except BaseException:
        # Diagnostics must never replace the original business exception.
        pass
    return event
