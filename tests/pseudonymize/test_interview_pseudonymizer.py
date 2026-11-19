"""채용담당자 인터뷰 가명화 (#262, 결정 4) — 이름 탐지기(Kiwi) 포함. 가상 이름·연락처만."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from task_spec.nodes.pseudonymize import (
    InterviewMappingGone,
    InterviewPseudonymizer,
    InterviewTextRejected,
)
from task_spec.nodes.pseudonymize.detectors import detect_patterns
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.schemas.pseudo_text import PseudonymizedText

IID = "int-" + "a" * 32


@pytest.fixture(scope="module")
def kiwi_detectors() -> Detectors:
    return Detectors(load_dictionaries(), use_kiwi=True)


@pytest.fixture
def pseudo(tmp_path: Path, kiwi_detectors: Detectors) -> InterviewPseudonymizer:
    return InterviewPseudonymizer(kiwi_detectors, tmp_path)


def test_same_name_gets_the_same_token_across_turns(pseudo: InterviewPseudonymizer) -> None:
    mapping = pseudo.new_mapping(IID, "(주)가상테크")
    first = pseudo.pseudonymize(
        "가상테크 결제팀 김가상 대리가 010-0000-0000 으로 장애를 알렸다", mapping
    )
    second = pseudo.pseudonymize("김가상 대리가 다시 재현했고 Redis 설정을 바꿨다", mapping)
    assert isinstance(first, PseudonymizedText)
    assert "김가상" not in first + second and "010-0000-0000" not in first
    assert first.startswith("[ORG_1] 결제팀 [PERSON_1]") and "[CONTACT_1]" in first
    assert second.startswith("[PERSON_1]") and "Redis" in second  # 기술 이름은 그대로


def test_tokens_shown_on_screen_are_kept_and_unknown_tokens_neutralized(
    pseudo: InterviewPseudonymizer,
) -> None:
    mapping = pseudo.new_mapping(IID, "가상테크")
    pseudo.pseudonymize("김가상 대리가 확인했다", mapping)
    edited = pseudo.pseudonymize(
        "[PERSON_1] 님과 [ORG_1] 에서 재현했고 [PERSON_9] 는 모른다", mapping
    )
    assert edited.startswith("[PERSON_1] 님과 [ORG_1] 에서")
    assert "⟨PERSON_9⟩" in edited and "[PERSON_9]" not in edited
    assert pseudo.pseudonymize("0 자리표시 문자", mapping) == "0 자리표시 문자"


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=sk-abcdefghijklmnop1234",
        "password: hunter2",
        "-----BEGIN RSA PRIVATE KEY-----",
        "비밀번호: hunter2",
        "SECRET_KEY=abc123",
        "Authorization: Bearer abcdefghijklmnopqrstu",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "postgres://user:pw@host/db",
        '"password": "abc"',
        "토큰: abcdef",
    ],
)
def test_secrets_are_rejected_not_pseudonymized(
    pseudo: InterviewPseudonymizer, secret: str
) -> None:
    with pytest.raises(InterviewTextRejected, match="비밀 값"):
        pseudo.pseudonymize(f"설정 값 {secret}", pseudo.new_mapping(IID, "가상테크"))


def test_model_text_checks_contacts_and_forged_tokens(pseudo: InterviewPseudonymizer) -> None:
    mapping = pseudo.new_mapping(IID, "가상테크")
    assert (
        pseudo.derived("[ORG_1] 의 원인은 무엇이었나요?", mapping)
        == "[ORG_1] 의 원인은 무엇이었나요?"
    )
    with pytest.raises(InterviewTextRejected):
        pseudo.derived("ops@example.com 으로 물어보세요", mapping)
    with pytest.raises(InterviewTextRejected):
        pseudo.derived("[PERSON_7] 이 찾았다", mapping)
    composed = pseudo.stored_json(
        {"messages": [{"role": "user", "text": "[ORG_1] 에서 재현"}]}, mapping
    )
    assert isinstance(composed, PseudonymizedText)
    assert json.loads(composed) == {"messages": [{"role": "user", "text": "[ORG_1] 에서 재현"}]}
    assert pseudo.stored_json({"a": "b"}, mapping, compact=True) == '{"a":"b"}'
    with pytest.raises(InterviewTextRejected):
        pseudo.stored_json(["010-1234-5678"], mapping)


def test_mapping_file_is_private_and_deletable(
    pseudo: InterviewPseudonymizer, tmp_path: Path
) -> None:
    mapping = pseudo.new_mapping(IID, "가상테크")
    pseudo.pseudonymize("김가상 대리", mapping)
    pseudo.save(IID, mapping)
    path = tmp_path / "interviews" / IID / "mapping.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert pseudo.load(IID).tokens.keys() == mapping.tokens.keys()
    assert pseudo.mapped_ids() == [IID]
    assert pseudo.delete(IID) and not path.parent.exists()
    with pytest.raises(InterviewMappingGone):
        pseudo.load(IID)
    with pytest.raises(ValueError):
        pseudo.load("../escape")


@pytest.mark.parametrize(
    "text",
    [
        "비밀번호는 bcrypt 해시로 저장했다",
        "토큰 갱신이 실패했다",
        "cache_key=product:{id} 로 캐시했다",
        "JWT 서명 검증 실패",
    ],
)
def test_ordinary_incident_wording_is_not_mistaken_for_a_secret(
    pseudo: InterviewPseudonymizer, text: str
) -> None:
    assert pseudo.pseudonymize(text, pseudo.new_mapping(IID, "가상테크"))


def test_dict_keys_are_checked_too(pseudo: InterviewPseudonymizer) -> None:
    mapping = pseudo.new_mapping(IID, "가상테크")
    with pytest.raises(InterviewTextRejected):
        pseudo.stored_json({"ops@example.com": "값"}, mapping)


def test_system_ids_that_look_like_phone_numbers_are_not_rejected(
    pseudo: InterviewPseudonymizer,
) -> None:
    """uuid4 의 약 0.7% 가 전화번호 패턴에 걸린다 — 사례 항목 id 가 걸리면 그 인터뷰는 매번 같은 id 로 거절돼 멈췄다."""
    mapping = pseudo.new_mapping(IID, "가상테크")
    case_id = "66b54d53-5915-4071-92cd-50ac4fcf7f9b"  # '53-5915-4071' 이 유선 전화 모양
    incident_id = "co:sw.backend:1783903502ab"  # '1783903502' 가 휴대 전화 모양
    assert detect_patterns(case_id) and detect_patterns(
        incident_id
    )  # 탐지기는 그대로 — 가리는 쪽이 막는다
    cases = {"incident_id": incident_id, "known_incident_cases": [{"case_id": case_id}]}
    assert json.loads(pseudo.stored_json(cases, mapping)) == cases
    assert pseudo.derived(f"사례 {incident_id} 의 항목 {case_id}", mapping)
    with pytest.raises(InterviewTextRejected):  # id 옆의 진짜 연락처는 그대로 막는다
        pseudo.derived(f"{case_id} 담당 010-1234-5678", mapping)
