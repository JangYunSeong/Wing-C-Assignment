"""PublicIncident.known_incident_cases (#262) — 비면 직렬화에서 빠져 기존 사례 지문·공통 과제 키·회사 사례 해시가 그대로다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from task_spec.schemas import KnownIncidentCase, PublicIncident

PUBLIC = sorted((Path(__file__).resolve().parents[2] / "incidents" / "public_v1").glob("*.json"))
CASE_ID = "00000000-0000-4000-8000-00000000000{}"


@pytest.mark.parametrize("path", PUBLIC, ids=lambda p: p.stem)
def test_public_incident_serialization_has_no_empty_case_field(path: Path) -> None:
    """다른 칸은 그대로이므로 빈 칸이 빠지면 칸이 생기기 전과 바이트가 같다 → 사례 지문·공통 과제 키·회사 사례 해시 불변.
    (#262: 기존 프롬프트 173판의 출력 스키마 글과 공개 사례 24건의 직렬화 해시를 바꾸기 전·후로 대조해 같음을 확인)"""
    inc = PublicIncident.model_validate_json(path.read_text("utf-8"))
    assert "known_incident_cases" not in inc.model_dump(mode="json")
    assert "known_incident_cases" not in inc.model_dump_json()
    assert "known_incident_cases" not in json.loads(inc.model_dump_json())


def test_cases_are_serialized_when_present_and_validated() -> None:
    inc = PublicIncident.model_validate_json(PUBLIC[0].read_text("utf-8"))
    cases = [
        KnownIncidentCase(
            case_id=CASE_ID.format(1), interview_text="확인한 사건", answer_text="답", is_trap=False
        ),
        KnownIncidentCase(case_id=CASE_ID.format(2), interview_text="실패한 첫 시도", is_trap=True),
    ]
    with_cases = inc.model_copy(update={"known_incident_cases": cases})
    dumped = with_cases.model_dump(mode="json")
    assert [c["is_trap"] for c in dumped["known_incident_cases"]] == [False, True]
    assert PublicIncident.model_validate(dumped) == with_cases
    assert (
        hashlib.sha256(with_cases.model_dump_json().encode()).hexdigest()
        != hashlib.sha256(inc.model_dump_json().encode()).hexdigest()
    )
    with pytest.raises(ValueError, match="한 사건"):
        PublicIncident.model_validate(
            {**dumped, "known_incident_cases": [c.model_dump() for c in cases[1:]]}
        )
    with pytest.raises(ValueError, match="UUID"):
        PublicIncident.model_validate(
            {
                **dumped,
                "known_incident_cases": [
                    cases[0].model_dump(),
                    {**cases[1].model_dump(), "case_id": CASE_ID.format(1)},
                ],
            }
        )
