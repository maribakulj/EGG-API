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
from datetime import datetime
from string import Template
from typing import Any
from urllib.parse import urlparse

from app.config.models import AppConfig
from app.schemas.record import Record

logger = logging.getLogger(__name__)


def _filter_internal_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop backend-internal fields (``_id``, ``_score``, …) from raw_fields.

    Public consumers must not see internal bookkeeping that could leak index
    layout or scoring internals.
    """
    return {k: v for k, v in doc.items() if not (isinstance(k, str) and k.startswith("_"))}


class SchemaMapper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def map_record(self, doc: dict[str, Any]) -> Record:
        mapped: dict[str, Any] = {}
        for public_field, rule in self.config.mapping.items():
            mapped[public_field] = self._apply_mode(rule.mode, rule.model_dump(), doc)

        mapped.setdefault("id", str(doc.get("id") or doc.get("_id") or ""))
        mapped.setdefault("type", str(doc.get("type") or "record"))
        if self.config.profiles[self.config.security_profile].allow_raw_fields:
            mapped["raw_fields"] = _filter_internal_fields(doc)
        return Record.model_validate(mapped)

    def _apply_mode(self, mode: str, rule: dict[str, Any], doc: dict[str, Any]) -> Any:
        if mode == "direct":
            return doc.get(rule.get("source", ""))
        if mode == "constant":
            return rule.get("constant")
        if mode == "split_list":
            value = doc.get(rule.get("source", ""))
            if not value:
                return []
            return [x.strip() for x in str(value).split(rule.get("separator", ";")) if x.strip()]
        if mode == "first_non_empty":
            for source in rule.get("sources", []):
                value = doc.get(source)
                if value:
                    return value
            return None
        if mode == "template":
            return Template(rule.get("template") or "").safe_substitute(doc)
        if mode == "nested_object":
            source = rule.get("source", "")
            return doc.get(source) if isinstance(doc.get(source), dict) else {}
        if mode == "date_parser":
            return _parse_iso_date(doc.get(rule.get("source", "")), rule.get("source"))
        if mode == "boolean_cast":
            return bool(doc.get(rule.get("source", "")))
        if mode == "url_passthrough":
            return _safe_public_url(doc.get(rule.get("source", "")))
        return None


def _parse_iso_date(value: Any, source_name: str | None = None) -> str | None:
    """Parse an ISO date (or datetime) into YYYY-MM-DD; return None on failure."""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "date_parser: invalid value in source=%s reason=%s", source_name, exc
        )
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
