"""Persistent activity log + risk events, with secret redaction."""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.events import bus
from app.models import RiskEvent, SystemLog

log = logging.getLogger("trading")

_SENSITIVE_KEY = re.compile(r"(secret|api[_-]?key|signature|password|token|authorization)", re.I)
_SENSITIVE_VAL = re.compile(r"(signature=)[0-9a-fA-F]+|(X-MBX-APIKEY[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9]+")


def redact(value: Any) -> Any:
    """Recursively strip anything that looks like a credential."""
    if isinstance(value, dict):
        return {k: ("***" if _SENSITIVE_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SENSITIVE_VAL.sub(lambda m: (m.group(1) or m.group(2) or "") + "***", value)
    return value


def _jsonable(meta: dict | None) -> dict | None:
    if meta is None:
        return None
    from app.events import encode
    import json

    return json.loads(encode(redact(meta)))


def log_event(
    session: Session,
    component: str,
    message: str,
    level: str = "INFO",
    meta: dict | None = None,
    category: str | None = None,
) -> SystemLog:
    """Write an activity-log row and broadcast it. Caller owns the transaction."""
    message = redact(message)
    payload = _jsonable({**(meta or {}), **({"category": category} if category else {})})
    row = SystemLog(level=level, component=component, message=message, log_meta=payload)
    session.add(row)
    session.flush()
    getattr(log, level.lower(), log.info)("[%s] %s", component, message)
    bus.publish("dashboard", "log", {"id": row.id, "level": level, "component": component,
                                     "message": message, "metadata": payload,
                                     "created_at": row.created_at})
    return row


def risk_event(session: Session, event_type: str, severity: str, description: str, meta: dict | None = None) -> RiskEvent:
    payload = _jsonable(meta)
    row = RiskEvent(event_type=event_type, severity=severity, description=description, event_meta=payload)
    session.add(row)
    session.flush()
    log_event(session, "risk", description, level="WARNING" if severity != "INFO" else "INFO",
              meta={"event_type": event_type, **(payload or {})}, category="risk")
    bus.publish("dashboard", "risk_event", {"id": row.id, "event_type": event_type, "severity": severity,
                                            "description": description, "created_at": row.created_at})
    return row
