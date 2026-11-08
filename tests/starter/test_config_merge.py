"""설정 파일 합치기 (starter/config_merge.py) — 가상 설정만.

2026-11-08 알파: 참조 구현이 application.yml 을 통째로 다시 써 템플릿의 ddl-auto·sql.init 을 지우거나 DB 주소를 고정했다.
"""

from __future__ import annotations

import yaml

from task_spec.schemas import GeneratedFile
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.config_merge import merge_config

YML = "src/main/resources/application.yml"
TEMPLATE_YML = """spring:
  application:
    name: service
  jpa:
    hibernate:
      ddl-auto: none   # 스키마는 schema.sql 로 관리한다
  sql:
    init:
      mode: always
"""
MANIFEST = TemplateManifest.model_validate(
    {
        "template_id": "backend/x",
        "version": 1,
        "interfaces": ["http"],
        "config_merge": {
            "paths": [YML],
            "locked_keys": [
                "spring.datasource.url",
                "spring.jpa.hibernate.ddl-auto",
                "spring.sql.init.mode",
            ],
        },
    }
)


def _merge(text: str, path: str = YML) -> tuple[dict, list[str], str]:  # type: ignore[type-arg]
    files, notes = merge_config(
        [GeneratedFile(path=path, content=text)], MANIFEST, {YML: TEMPLATE_YML}
    )
    return (yaml.safe_load(files[0].content) or {}), notes, files[0].content


def test_rewritten_file_keeps_base_keys() -> None:
    """d2·d3: open-in-view 만 써 ddl-auto·sql.init 이 사라짐 → 바탕 위에 얹는다."""
    doc, notes, _ = _merge("spring:\n  jpa:\n    open-in-view: false\n")
    assert doc["spring"]["jpa"] == {"hibernate": {"ddl-auto": "none"}, "open-in-view": False}
    assert doc["spring"]["sql"]["init"]["mode"] == "always"
    assert doc["spring"]["application"]["name"] == "service"
    assert len(notes) == 1 and "spring.jpa.hibernate.ddl-auto" in notes[0] and "되살림" in notes[0]
    assert "되돌림" not in notes[0]  # 잠긴 키를 바꾼 것이 아니라 빠뜨린 것


def test_locked_keys_return_to_base_and_missing_ones_are_removed() -> None:
    """d1·흔한 구현: DB 주소 고정(바탕에 없음 → 지움), ddl-auto 를 바꿈 → 바탕 값."""
    doc, notes, _ = _merge(
        "spring:\n  datasource:\n    url: jdbc:h2:mem:testdb\n    driver-class-name: org.h2.Driver\n"
        "  jpa:\n    hibernate:\n      ddl-auto: create-drop\n"
    )
    assert "url" not in doc["spring"]["datasource"]
    assert (
        doc["spring"]["datasource"]["driver-class-name"] == "org.h2.Driver"
    )  # 잠기지 않은 키는 생성 코드 값
    assert doc["spring"]["jpa"]["hibernate"]["ddl-auto"] == "none"
    assert "spring.datasource.url" in notes[0] and "spring.jpa.hibernate.ddl-auto" in notes[0]
    # 잠긴 키만 있던 묶음은 비어서 사라진다
    doc, _, _ = _merge("spring:\n  datasource:\n    url: jdbc:h2:mem:testdb\n")
    assert "datasource" not in doc["spring"]


def test_dotted_keys_merge_with_nested_ones() -> None:
    doc, _, _ = _merge("spring.jpa.open-in-view: false\nspring.sql.init.mode: never\n")
    assert doc["spring"]["jpa"]["open-in-view"] is False
    assert doc["spring"]["sql"]["init"]["mode"] == "always"


def test_unchanged_or_unreadable_files_are_left_alone() -> None:
    _, notes, text = _merge(TEMPLATE_YML)
    assert not notes and text == TEMPLATE_YML  # 바탕과 같으면 주석까지 그대로
    for odd in ("spring: [\n", "a: 1\n---\nb: 2\n"):  # 읽지 못함 · 여러 문서
        files, notes = merge_config(
            [GeneratedFile(path=YML, content=odd)], MANIFEST, {YML: TEMPLATE_YML}
        )
        assert not notes and files[0].content == odd
    other = "src/main/resources/other.yml"
    files, notes = merge_config(
        [GeneratedFile(path=other, content="x: 1\n")], MANIFEST, {YML: TEMPLATE_YML}
    )
    assert not notes and files[0].content == "x: 1\n"
