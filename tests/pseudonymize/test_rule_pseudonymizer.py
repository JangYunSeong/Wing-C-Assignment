"""RulePseudonymizer: 파일 입출력·형식 거부·누락 실패·로그 노출."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from task_spec.nodes.pseudonymize import RulePseudonymizer, load_pseudonymized
from task_spec.nodes.pseudonymize.engine import RunMapping
from task_spec.schemas import ExtractedRef
from tests.pseudonymize.fixtures import COVER_LETTER, RESUME, extracted_ref


def _doc(
    tmp_path: Path, name: str, body: bytes, kind: str = "resume", run_id: str = "run1"
) -> ExtractedRef:
    return extracted_ref(tmp_path, run_id, name, body, kind)


async def test_txt_pseudonymized_and_written_privately(tmp_path: Path) -> None:
    p = RulePseudonymizer(tmp_path)
    ref = await p("run1", _doc(tmp_path, "resume-1.txt", RESUME.encode()))
    assert ref.check.passed and ref.path
    assert stat.S_IMODE(os.stat(ref.path).st_mode) == 0o600
    text = load_pseudonymized(ref)
    assert "김가상" not in text and "[PERSON_1]" in text
    assert ref.check.counts["PERSON"] >= 1
    mapping = tmp_path / "pseudonymized" / "run1" / "mapping.json"
    assert stat.S_IMODE(os.stat(mapping).st_mode) == 0o600


async def test_documents_in_same_run_share_tokens(tmp_path: Path) -> None:
    p = RulePseudonymizer(tmp_path)
    r = await p("run1", _doc(tmp_path, "resume-1.txt", RESUME.encode()))
    c = await p("run1", _doc(tmp_path, "cover_letter-1.txt", COVER_LETTER.encode(), "cover_letter"))
    m = RunMapping.load(tmp_path / "pseudonymized" / "run1" / "mapping.json")
    org = next(t for t, vs in m.tokens.items() if "삼성전자" in vs)
    assert org in load_pseudonymized(r) and org in load_pseudonymized(c)


async def test_runs_do_not_share_mapping(tmp_path: Path) -> None:
    p = RulePseudonymizer(tmp_path)
    await p("run1", _doc(tmp_path, "a.txt", "성명: 김가상".encode()))
    ref = await p("run2", _doc(tmp_path, "b.txt", "성명: 이가상".encode(), run_id="run2"))
    assert "[PERSON_1]" in load_pseudonymized(ref)  # run2 는 1번부터


@pytest.mark.parametrize("reason", ["unsupported_format", "ocr_error", "empty_text"])
async def test_failed_extraction_is_not_pseudonymized(tmp_path: Path, reason: str) -> None:
    failed = ExtractedRef(doc_id="resume-1", kind="resume", ok=False, reason=reason)  # type: ignore[arg-type]
    ref = await RulePseudonymizer(tmp_path)("run1", failed)
    assert not ref.check.passed and ref.check.reason == reason and ref.path is None


async def test_unreadable_text_is_read_error(tmp_path: Path) -> None:
    ref = await RulePseudonymizer(tmp_path)("run1", _doc(tmp_path, "x.txt", b"\xff\xfe\xfa"))
    assert ref.check.reason == "read_error"


async def test_residual_fails_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_spec.nodes.pseudonymize import rule_pseudonymizer as mod

    monkeypatch.setattr(mod, "pseudonymize_documents", _leaky)
    ref = await RulePseudonymizer(tmp_path)("run1", _doc(tmp_path, "r.txt", RESUME.encode()))
    assert (
        not ref.check.passed and ref.check.reason == "residual_pii" and ref.check.residual_count > 0
    )
    assert not (tmp_path / "pseudonymized" / "run1" / "r.txt").exists()


def _leaky(texts, mapping, detectors):  # type: ignore[no-untyped-def]
    from task_spec.nodes.pseudonymize.engine import PseudoResult

    return [PseudoResult(text=t, counts={}, residual=3, detectors=[]) for t in texts]


async def test_logs_have_counts_not_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await RulePseudonymizer(tmp_path)("run1", _doc(tmp_path, "r.txt", RESUME.encode()))
    logs = caplog.text
    for value in ("김가상", "kim.gasang@example.com", "010-0000-1234", "삼성전자"):
        assert value not in logs
    assert "PERSON=" in logs


async def test_batch_processes_documents_together(tmp_path: Path) -> None:
    """한 run 의 문서를 함께 처리한다 — 앞 문서의 좁은 범위 값이 뒤 문서에서 넓어져도 새지 않는다 (code review M5)."""
    p = RulePseudonymizer(tmp_path)
    a = _doc(
        tmp_path,
        "resume-1.txt",
        "중앙 | 백엔드 개발자 | 2020.01 ~ 2021.01\n중앙 서버 구조를 바꿨다.".encode(),
    )
    b = _doc(tmp_path, "cover_letter-1.txt", "회사: 중앙\n지원 동기".encode(), "cover_letter")
    ra, rb = await p.batch("run1", [a, b])
    assert ra.check.passed and rb.check.passed
    assert "중앙" not in load_pseudonymized(ra) and "중앙" not in load_pseudonymized(rb)


async def test_batch_failure_writes_no_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """batch 의 한 문서라도 누락 검사에 걸리면 통과한 문서도 쓰지 않는다 (마지막 점검 L2)."""
    from task_spec.nodes.pseudonymize import rule_pseudonymizer as mod
    from task_spec.nodes.pseudonymize.engine import PseudoResult

    real = mod.pseudonymize_documents

    def second_fails(texts, mapping, detectors):  # type: ignore[no-untyped-def]
        out = real(texts, mapping, detectors)
        return [out[0], PseudoResult(text=out[1].text, counts={}, residual=2, detectors=[])]

    monkeypatch.setattr(mod, "pseudonymize_documents", second_fails)
    p = RulePseudonymizer(tmp_path)
    a = _doc(tmp_path, "resume-1.txt", "성명: 김가상".encode())
    b = _doc(tmp_path, "cover_letter-1.txt", "성명: 이가상".encode(), "cover_letter")
    ra, rb = await p.batch("run1", [a, b])
    assert not ra.check.passed and ra.check.reason == "batch_failed" and ra.path is None
    assert not rb.check.passed and rb.check.reason == "residual_pii"
    out_dir = tmp_path / "pseudonymized" / "run1"
    assert not (out_dir / "resume-1.txt").exists() and not (out_dir / "mapping.json").exists()
