from __future__ import annotations

from datetime import datetime
from string import Template
from typing import Any

from app.config.models import AppConfig
from app.schemas.record import Record


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
            mapped["raw_fields"] = doc
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
            value = doc.get(rule.get("source", ""))
            if not value:
                return None
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
        if mode == "boolean_cast":
            return bool(doc.get(rule.get("source", "")))
        if mode == "url_passthrough":
            value = doc.get(rule.get("source", ""))
            return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None
        return None


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
