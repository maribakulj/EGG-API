from __future__ import annotations

from app.config.models import FieldMapping
from app.dependencies import container
from app.mappers.schema_mapper import MappingHealthService


def test_mapping_logic_split_list() -> None:
    rec = container.mapper.map_record({"id": "1", "type": "book", "creator_csv": "x;y"})
    assert rec.id == "1"
    assert rec.creators == ["x", "y"]


def test_mapping_drift_classification() -> None:
    svc = MappingHealthService()
    mapping = {
        "id": FieldMapping(source="id", criticality="required"),
        "title": FieldMapping(source="title", criticality="recommended"),
        "subtitle": FieldMapping(source="subtitle", criticality="optional"),
    }
    status = svc.classify(mapping, {"id": "1", "title": ""})
    assert status["id"] == "ok"
    assert status["title"] == "empty_source"
    assert status["subtitle"] in {"degraded", "missing"}
