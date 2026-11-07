"""빌드 파이프라인 경계 모델 (schemas/build.py · schemas/template.py) — 가상 데이터만."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import (
    BuildState,
    ContractField,
    ContractResponse,
    FileRef,
    GeneratedFiles,
    HarnessParam,
    TaskContract,
    TemplateManifest,
)


def _param(**over: Any) -> dict[str, Any]:
    return {"name": "pool_size", "kind": "int", "description": "풀 크기", **over}


def test_visible_param_needs_value_pattern() -> None:
    with pytest.raises(ValidationError, match="표기 패턴"):
        HarnessParam.model_validate(_param(visible=True))
    with pytest.raises(ValidationError, match="값 그룹"):
        HarnessParam.model_validate(_param(visible=True, spec_patterns=[r"pool:\s*\d+"]))
    with pytest.raises(ValidationError, match="정규식"):
        HarnessParam.model_validate(_param(spec_patterns=["pool:(\\d+"]))
    with pytest.raises(ValidationError, match="minimum"):
        HarnessParam.model_validate(_param(minimum=10, maximum=1))
    ok = HarnessParam.model_validate(_param(visible=True, spec_patterns=[r"pool:\s*(\d+)"]))
    assert ok.required and ok.visible


def test_manifest_names_unique_and_ref() -> None:
    base: dict[str, Any] = {"template_id": "backend/x", "version": 2, "interfaces": ["http"]}
    with pytest.raises(ValidationError, match="겹침"):
        TemplateManifest.model_validate({**base, "harness_params": [_param(), _param()]})
    m = TemplateManifest.model_validate(
        {
            **base,
            "harness_params": [_param()],
            "failure_reasons": [
                {"code": "pool_acquire_timeout", "description": "d", "log_patterns": ["x"]}
            ],
        }
    )
    assert m.ref == "backend/x@v2" and m.param("pool_size") is not None
    assert m.reason_codes == {"pool_acquire_timeout"}


def test_field_refs_only_public_ids() -> None:
    with pytest.raises(ValidationError, match="r\\*"):
        ContractField.model_validate({"name": "a", "type": "string", "refs": ["h1"]})
    with pytest.raises(ValidationError):
        ContractField.model_validate({"name": "a", "type": "string", "refs": []})


def test_response_refs_are_dropped_other_extras_rejected() -> None:
    # plan_contract v7 "모든 요소에 refs" 를 응답 묶음에까지 붙인 답 (알파 테스트 2026-11-07)
    r = ContractResponse.model_validate({"status": 200, "description": "성공", "refs": ["r1"]})
    assert "refs" not in r.model_dump()
    with pytest.raises(ValidationError):
        ContractResponse.model_validate({"status": 200, "description": "성공", "note": "x"})


def test_contract_size_limits() -> None:
    ep = {
        "method": "GET",
        "path": "/a",
        "summary": "s",
        "responses": [{"status": 200, "description": "ok"}],
        "refs": ["r1"],
    }
    base = {
        "interface_kind": "http",
        "entities": [
            {
                "entity_id": "m1",
                "name": "A",
                "fields": [{"name": "id", "type": "integer", "refs": ["r1"]}],
            }
        ],
        "public_tests": [{"test_id": "pt1", "ac_id": "p1", "checks": "c"}],
        "covers": {"r1": ["e1"]},
    }
    TaskContract.model_validate({**base, "endpoints": [{**ep, "endpoint_id": "e1"}]})
    with pytest.raises(ValidationError):
        TaskContract.model_validate(
            {**base, "endpoints": [{**ep, "endpoint_id": f"e{i}"} for i in range(1, 8)]}
        )


@pytest.mark.parametrize("path", ["/abs/x.java", "a/../b", "a\\b", "a\tb", "a\nb"])
def test_generated_file_paths_are_relative_and_clean(path: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedFiles.model_validate({"files": [{"path": path, "content": "x"}]})


def test_generated_files_reject_duplicates() -> None:
    with pytest.raises(ValidationError, match="같은 경로"):
        GeneratedFiles.model_validate(
            {"files": [{"path": "a/B.java", "content": "1"}, {"path": "a/B.java", "content": "2"}]}
        )


def test_build_state_keeps_only_refs() -> None:
    sha = "a" * 64
    s = BuildState(build_id="b1", common_id="c1", revision=1, ontology_version="onto-v1")
    s2 = s.model_copy(
        update={"files": [FileRef(kind="hidden", path="hidden/cases/T1.java", sha256=sha, size=10)]}
    )
    assert s.status == "pending" and s2.files[0].kind == "hidden"
    assert "content" not in FileRef.model_fields  # 본문은 State 에 없다 (starter_file 테이블)
    with pytest.raises(ValidationError):
        FileRef(kind="hidden", path="x", sha256="nothex", size=1)
    with pytest.raises(ValidationError):
        BuildState.model_validate(
            {
                "build_id": "b",
                "common_id": "c",
                "revision": 1,
                "ontology_version": "v",
                "unknown": 1,
            }
        )
