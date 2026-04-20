"""Backend-agnostic document mapping.

:class:`SchemaMapper` turns a raw backend document (``doc``) into the public
:class:`~app.schemas.record.Record`. Each public field is produced by a small
mode (``direct``, ``split_list``, ``first_non_empty``, ``template``,
``nested_object``, ``date_parser``, ``boolean_cast``, ``url_passthrough``) so
the operator can compose a mapping from configuration alone without touching
Python code. Mode helpers are defensive by design: invalid dates return
``None`` rather than raising, URL passthrough validates scheme + host, and
``raw_fields`` (enabled per security profile) drops any backend-internal key
prefixed with ``_``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from string import Template
from typing import Any
from urllib.parse import urlparse

from app.config.models import AppConfig
from app.errors import AppError
from app.schemas.record import Record

logger = logging.getLogger(__name__)


def _filter_internal_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop backend-internal fields (``_id``, ``_score``, …) from raw_fields.

    Public consumers must not see internal bookkeeping that could leak index
    layout or scoring internals.
    """
    return {k: v for k, v in doc.items() if not (isinstance(k, str) and k.startswith("_"))}


# ---------------------------------------------------------------------------
# Mode handlers.
#
# Each handler takes ``(rule, doc)`` and returns the mapped value. Adding a
# new mode is a one-line dict entry below the handler — no touching
# ``map_record`` — and every rule in _MODE_HANDLERS corresponds one-to-one
# with a ``MappingMode`` token in ``app.config.models``.
# ---------------------------------------------------------------------------


def _apply_direct(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    return doc.get(rule.get("source", ""))


def _apply_constant(rule: dict[str, Any], _doc: dict[str, Any]) -> Any:
    return rule.get("constant")


def _apply_split_list(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    value = doc.get(rule.get("source", ""))
    if not value:
        return []
    separator = rule.get("separator", ";")
    return [x.strip() for x in str(value).split(separator) if x.strip()]


def _apply_first_non_empty(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    for source in rule.get("sources", []):
        value = doc.get(source)
        if value:
            return value
    return None


def _apply_template(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    return Template(rule.get("template") or "").safe_substitute(doc)


def _apply_nested_object(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    value = doc.get(rule.get("source", ""))
    return value if isinstance(value, dict) else {}


def _apply_date_parser(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    source = rule.get("source", "")
    return _parse_iso_date(doc.get(source), source)


def _apply_boolean_cast(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    return bool(doc.get(rule.get("source", "")))


def _apply_url_passthrough(rule: dict[str, Any], doc: dict[str, Any]) -> Any:
    return _safe_public_url(doc.get(rule.get("source", "")))


_ModeHandler = Callable[[dict[str, Any], dict[str, Any]], Any]
_MODE_HANDLERS: dict[str, _ModeHandler] = {
    "direct": _apply_direct,
    "constant": _apply_constant,
    "split_list": _apply_split_list,
    "first_non_empty": _apply_first_non_empty,
    "template": _apply_template,
    "nested_object": _apply_nested_object,
    "date_parser": _apply_date_parser,
    "boolean_cast": _apply_boolean_cast,
    "url_passthrough": _apply_url_passthrough,
}


class SchemaMapper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def map_record(self, doc: dict[str, Any]) -> Record:
        mapped: dict[str, Any] = {}
        for public_field, rule in self.config.mapping.items():
            mapped[public_field] = self._apply_mode(rule.mode, rule.model_dump(), doc)

        # `setdefault` does NOT overwrite an explicit None that a mapping rule
        # produced when the configured source was absent. Guard both structural
        # fields explicitly: missing id/type from the backend is an upstream
        # data issue, not an EGG config bug -> raise 502 instead of a Pydantic
        # 500 later.
        if not mapped.get("id"):
            mapped["id"] = str(doc.get("id") or doc.get("_id") or "")
        if not mapped.get("type"):
            mapped["type"] = str(doc.get("type") or "record")
        if not mapped["id"]:
            raise AppError(
                "bad_gateway",
                "Backend record is missing a usable identifier",
                {"hint": "id / _id fields were empty or absent"},
                status_code=502,
            )
        if self.config.profiles[self.config.security_profile].allow_raw_fields:
            mapped["raw_fields"] = _filter_internal_fields(doc)
        return Record.model_validate(mapped)

    @staticmethod
    def _apply_mode(mode: str, rule: dict[str, Any], doc: dict[str, Any]) -> Any:
        handler = _MODE_HANDLERS.get(mode)
        if handler is None:
            # Pydantic's MappingMode Literal prevents this at config-load
            # time; the fallback covers rules constructed programmatically.
            return None
        return handler(rule, doc)


def _parse_iso_date(value: Any, source_name: str | None = None) -> str | None:
    """Parse an ISO date (or datetime) into YYYY-MM-DD; return None on failure."""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        logger.warning("date_parser: invalid value in source=%s reason=%s", source_name, exc)
        return None
    return parsed.date().isoformat()


def _safe_public_url(value: Any) -> str | None:
    """Return ``value`` only if it is a well-formed absolute http(s) URL."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return value


class MappingHealthService:
    def classify(self, mapping: dict[str, Any], doc: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for field, rule_obj in mapping.items():
            rule = rule_obj.model_dump() if hasattr(rule_obj, "model_dump") else rule_obj
            criticality = rule.get("criticality", "optional")
            source = rule.get("source")
            if source and source not in doc:
                out[field] = "missing" if criticality in {"required", "recommended"} else "degraded"
            elif source and doc.get(source) in (None, "", [], {}):
                out[field] = "empty_source"
            else:
                out[field] = "ok"
        return out
