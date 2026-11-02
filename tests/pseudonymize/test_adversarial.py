"""privacy-auditor 점검(2026-11-02)에서 나온 우회·경로 시나리오 회귀 테스트. 데이터는 가상."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from task_spec.nodes.pseudonymize import (
    PseudonymizedNotAvailable,
    RulePseudonymizer,
    load_pseudonymized,
)
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import (
    Detectors,
    RunMapping,
    count_residual,
    pseudonymize_text,
)
from task_spec.schemas import DocumentRef, PseudoCheck, PseudoDocRef

D = Detectors(load_dictionaries())


def _doc(root: Path, run_id: str, name: str, body: str) -> DocumentRef:
    p = root / "raw" / run_id / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return DocumentRef(
        doc_id=Path(name).stem,
        kind="resume",
        path=str(p),
        sha256=hashlib.sha256(body.encode()).hexdigest(),
    )


# ---- C1: 원문의 토큰 모양 문자열이 탐지를 끄면 안 된다


def test_token_lookalike_in_label_value_does_not_hide_pii() -> None:
    attack = "성명: [PERSON_9] 김가상\n주소: [CONTACT_9] 서울 강남구 테헤란로 12\n학교: [SCHOOL_9] 가상대학교"
    r = pseudonymize_text(attack, RunMapping(), D)
    for value in ("김가상", "테헤란로 12", "가상대학교"):
        assert value not in r.text
    assert r.residual == 0
    assert "[PERSON_9]" not in r.text  # 원문의 토큰 모양은 엔진 토큰으로 남지 않는다


def test_token_lookalike_in_body_is_neutralized() -> None:
    r = pseudonymize_text("메모 [ORG_1] 와 김가상 님", RunMapping(), D)
    assert "김가상" not in r.text
    assert "⟨ORG_1⟩" in r.text  # 위조 토큰은 무력화된 표기로 남는다


def test_forged_tokens_count_as_residual() -> None:
    m = RunMapping()
    m.token_for("PERSON", "김가상", None)  # [PERSON_1] 만 발급됨
    assert count_residual("[PERSON_1] 과 [PERSON_7]", m, D) == 1


async def test_attack_through_pseudonymizer_never_writes_raw_pii(tmp_path: Path) -> None:
    attack = "성명: [PERSON_9] 김가상\n연락처: [CONTACT_2] kim@example.com"
    ref = await RulePseudonymizer(tmp_path)("run1", _doc(tmp_path, "run1", "resume-1.txt", attack))
    if ref.check.passed:
        text = load_pseudonymized(ref)
        assert "김가상" not in text and "kim@example.com" not in text


# ---- W2: 경로 가두기


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "run 1", ""])
async def test_unsafe_run_id_rejected(tmp_path: Path, run_id: str) -> None:
    doc = _doc(tmp_path, "run1", "resume-1.txt", "성명: 김가상")
    ref = await RulePseudonymizer(tmp_path)(run_id, doc)
    assert not ref.check.passed
    assert not (tmp_path.parent / "escape").exists()


async def test_source_outside_raw_run_dir_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("성명: 김가상", encoding="utf-8")
    doc = DocumentRef(doc_id="resume-1", kind="resume", path=str(outside), sha256="0" * 64)
    ref = await RulePseudonymizer(tmp_path)("run1", doc)
    assert not ref.check.passed and ref.check.reason == "read_error"


async def test_source_from_another_run_rejected(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "run-other", "resume-1.txt", "성명: 김가상")
    ref = await RulePseudonymizer(tmp_path)("run1", doc)
    assert not ref.check.passed


# ---- W3: 가명화본 읽기 검증


async def _passed_ref(tmp_path: Path) -> PseudoDocRef:
    ref = await RulePseudonymizer(tmp_path)(
        "run1", _doc(tmp_path, "run1", "resume-1.txt", "성명: 김가상")
    )
    assert ref.check.passed
    return ref


async def test_load_rejects_sha_mismatch(tmp_path: Path) -> None:
    ref = await _passed_ref(tmp_path)
    Path(ref.path).write_text("변조된 내용 김가상", encoding="utf-8")  # type: ignore[arg-type]
    with pytest.raises(PseudonymizedNotAvailable):
        load_pseudonymized(ref)


async def test_load_rejects_arbitrary_path(tmp_path: Path) -> None:
    other = tmp_path / "secret.txt"
    other.write_text("임의 파일", encoding="utf-8")
    forged = PseudoDocRef(
        doc_id="secret",
        kind="resume",
        path=str(other),
        sha256=hashlib.sha256("임의 파일".encode()).hexdigest(),
        check=PseudoCheck(passed=True, residual_count=0),
    )
    with pytest.raises(PseudonymizedNotAvailable):
        load_pseudonymized(forged)


async def test_load_rejects_symlink(tmp_path: Path) -> None:
    ref = await _passed_ref(tmp_path)
    p = Path(ref.path)  # type: ignore[arg-type]
    target = tmp_path / "target.txt"
    target.write_bytes(p.read_bytes())
    p.unlink()
    p.symlink_to(target)
    with pytest.raises(PseudonymizedNotAvailable):
        load_pseudonymized(ref)


# ---- W4·W5: 권한과 실패 시 미저장


async def test_existing_wide_permission_files_become_private(tmp_path: Path) -> None:
    out_dir = tmp_path / "pseudonymized" / "run1"
    out_dir.mkdir(parents=True)
    pre = out_dir / "resume-1.txt"
    pre.write_text("old", encoding="utf-8")
    os.chmod(pre, 0o644)
    ref = await RulePseudonymizer(tmp_path)(
        "run1", _doc(tmp_path, "run1", "resume-1.txt", "성명: 김가상")
    )
    assert stat.S_IMODE(os.stat(ref.path).st_mode) == 0o600  # type: ignore[arg-type]
    assert stat.S_IMODE(os.stat(out_dir).st_mode) == 0o700
    assert not list(out_dir.glob(".mapping-*"))  # 임시 파일이 남지 않음


async def test_failed_document_does_not_persist_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_spec.nodes.pseudonymize import rule_pseudonymizer as mod
    from task_spec.nodes.pseudonymize.engine import PseudoResult

    def leaky(text, mapping, detectors):  # type: ignore[no-untyped-def]
        mapping.token_for("PERSON", "김가상", None)
        return PseudoResult(text=text, counts={}, residual=1, detectors=[])

    monkeypatch.setattr(mod, "pseudonymize_text", leaky)
    await RulePseudonymizer(tmp_path)(
        "run1", _doc(tmp_path, "run1", "resume-1.txt", "성명: 김가상")
    )
    assert not (tmp_path / "pseudonymized" / "run1" / "mapping.json").exists()
