from __future__ import annotations

from task_spec.ontology.load_pg import _convert, schema_name


def test_schema_name_slug() -> None:
    assert schema_name("v2026-10-30e@b69d00f+dirty") == "onto_v2026_10_30e_b69d00f_dirty"
    assert len(schema_name("v" + "x" * 200)) <= 55


def test_convert_types() -> None:
    assert _convert(1, "bool") is True and _convert(0, "bool") is False
    assert _convert('["03", "06"]', "_text") == ["03", "06"]
    assert _convert('{"수행": 1}', "jsonb").obj == {"수행": 1}
    assert _convert(None, "jsonb") is None
    assert _convert("x", "text") == "x"
