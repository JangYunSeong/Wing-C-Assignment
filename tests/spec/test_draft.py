"""공통 과제 서술 (draft_task) — 자동 검증·고쳐 쓰기·재요청·실패 처리. 가상 온톨로지 값만."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.llm.backend import TransientLLMError
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DraftTaskOutput, NcsCriterion, TargetCompetency, TaskItem, TechEnv
from task_spec.spec.draft import (
    FLAG_DRAFT_FAILED,
    FLAG_DRAFT_INVALID,
    FLAG_NOTE_DROPPED,
    FLAG_NOTE_TOO_LONG,
    DraftContext,
    apply,
    build_input,
    check,
    draft_task,
    repair,
    safe_reviewer_note,
)

_REGISTRY = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


class _Pinned:
    """재요청·고쳐 쓰기 경로는 버전과 무관 — 자료 목록이 없는 v2 출력 형식으로 고정해 검사한다 (v3 는 test_artifacts)."""

    def __init__(self, version: int) -> None:
        self.version = version

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        return _REGISTRY.get(task, version or self.version)


PROMPTS = _Pinned(2)


async def _no_sleep(_: float) -> None:
    return None


def _ctx(languages: list[str] | None = None) -> DraftContext:
    crit = [
        NcsCriterion(
            criterion_id=f"E{u}.{e}#{n}",
            unit_cl_cd=f"U{u}",
            unit_name=f"가상 단위 {u}",
            element_name=f"가상 요소 {e}",
            text=f"가상 수행준거 {u}-{e}-{n}",
        )
        for u in (1, 2)
        for e in (1,)
        for n in (1, 2)
    ]
    return DraftContext(
        sub_label="sw.backend: 백엔드",
        industry_label="가상 업종",
        targets=[
            TargetCompetency(
                competency_id="comp:a",
                label="역량 A",
                level="수행",
                ncs_unit_ids=["U1"],
                ncs_criterion_ids=["E1.1#1", "E1.1#2"],
            ),
            TargetCompetency(
                competency_id="comp:b",
                label="역량 B",
                level="활용",
                ncs_unit_ids=["U2"],
                ncs_criterion_ids=["E2.1#1", "E2.1#2"],
            ),
        ],
        criteria=crit,
        ksa=[],
        env=TechEnv(
            languages=languages if languages is not None else ["Python", "C++"], basis="공고 언어"
        ),
        difficulty="high",
        time_limit_min=60,
        ontology_version="onto-v2026.11.02",
        language_labels=["C", "C++", "Go", "Python", "R", "Rust", "Shell/Bash"],
    )


def _draft(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "title": "가상 주문 처리 지연 분석",
        "scenario": "가상의 회사에서 Python 배치가 느려졌다. R&D 팀이 C++ 모듈을 붙였다.",
        "deliverables": ["원인 분석 메모", "수정 코드"],
        "invariant": "증상이 아니라 잠금 경합이 원인",
        "variable": ["domain_surface", "numbers"],
        "traps": [
            {
                "trap_id": "t1",
                "description": "인덱스가 아님",
                "why_ai_fails": "AI 는 인덱스 추가를 권함",
            }
        ],
        "observables": [
            {"kind": "first_mention_of", "value": "잠금 경합"},
            {"kind": "catches_trap", "value": "t1"},
        ],
        "behavioral_anchors": {
            "level_1": {"behavior": "AI 답을 그대로 적용", "criterion_ids": ["E1.1#1"]},
            "level_3": {"behavior": "원인을 확인하고 적용", "criterion_ids": ["E2.1#1"]},
            "level_5": {"behavior": "원인 구조를 먼저 정의", "criterion_ids": ["E1.1#2", "E2.1#2"]},
        },
        "tech_used": ["Python"],
    }
    d.update(over)
    return d


def _out(**over: Any) -> DraftTaskOutput:
    return DraftTaskOutput.model_validate(_draft(**over))


def _client(fake: FakeBackend) -> LLMClient:
    return LLMClient({"opencode": fake, "openai": fake}, sleep=_no_sleep)


def test_check_passes_and_ignores_lookalike_language_tokens() -> None:
    # 'C++' 안의 C, 'R&D' 의 R 은 공고 밖 언어로 잡지 않는다
    assert check(_out(), _ctx()) == []


def test_check_finds_each_problem() -> None:
    anchors = _draft()["behavioral_anchors"]
    anchors["level_3"] = {"behavior": "x", "criterion_ids": ["E9.9#9"]}
    anchors["level_5"] = {"behavior": "y", "criterion_ids": ["E1.1#2"]}
    out = _out(
        behavioral_anchors=anchors,
        observables=[{"kind": "catches_trap", "value": "t9"}],
        tech_used=["Rust"],
        scenario="가상의 회사에서 Go 로 만든 서비스가 느리다.",
    )
    problems = check(out, _ctx())
    text = "\n".join(problems)
    assert "E9.9#9" in text
    assert "comp:b" in text  # 역량 B 의 수행준거를 하나도 인용하지 않음
    assert "t9" in text
    assert "Rust" in text
    assert "Go" in text
    assert len(problems) == 5


def test_check_without_posting_language_flags_any_language() -> None:
    assert any("Python" in p for p in check(_out(tech_used=[]), _ctx(languages=[])))


def test_repair_drops_bad_ids_and_reports_empty_anchor() -> None:
    anchors = _draft()["behavioral_anchors"]
    anchors["level_1"] = {"behavior": "x", "criterion_ids": ["E1.1#1", "E9.9#9"]}
    fixed, ok = repair(_out(behavioral_anchors=anchors, tech_used=["Python", "Rust"]), _ctx())
    assert ok and fixed.behavioral_anchors.level_1.criterion_ids == ["E1.1#1"]
    assert fixed.tech_used == ["Python"]
    anchors["level_3"] = {"behavior": "y", "criterion_ids": ["E9.9#9"]}
    _, ok = repair(_out(behavioral_anchors=anchors), _ctx())
    assert not ok


def test_input_has_no_applicant_fields_and_lists_criteria() -> None:
    inp = build_input(_ctx())
    assert set(type(inp).model_fields) == {
        "sub",
        "industry",
        "difficulty",
        "time_limit_min",
        "tech_env",
        "competencies",
        "ncs_criteria",
        "ksa",
        "revision_note",
    }
    assert "[E1.1#1]" in inp.ncs_criteria and inp.revision_note == "(없음)"
    assert inp.tech_env == "Python, C++"


async def test_draft_first_answer_ok() -> None:
    fake = FakeBackend().push(Completion(json.dumps(_draft(), ensure_ascii=False)))
    res = await draft_task(_ctx(), llm=_client(fake), prompts=PROMPTS)  # type: ignore[arg-type]
    assert res.output is not None and res.flags == [] and res.errors == []
    assert len(fake.calls) == 1 and res.provenance_id == res.provenance[0].provenance_id


async def test_draft_reasks_once_then_ok() -> None:
    bad = _draft(tech_used=["Rust"])
    fake = FakeBackend().push(
        Completion(json.dumps(bad, ensure_ascii=False)),
        Completion(json.dumps(_draft(), ensure_ascii=False)),
    )
    res = await draft_task(_ctx(), llm=_client(fake), prompts=PROMPTS)  # type: ignore[arg-type]
    assert len(fake.calls) == 2
    assert [e.code for e in res.errors] == ["draft_invalid"]
    assert res.flags == [] and res.output is not None and res.output.tech_used == ["Python"]
    assert res.provenance_id == res.provenance[1].provenance_id


async def test_draft_invalid_twice_repairs_and_flags() -> None:
    bad = json.dumps(_draft(tech_used=["Rust"]), ensure_ascii=False)
    fake = FakeBackend().push(Completion(bad), Completion(bad))
    res = await draft_task(_ctx(), llm=_client(fake), prompts=PROMPTS)  # type: ignore[arg-type]
    assert len(fake.calls) == 2
    assert res.output is not None and res.output.tech_used == []
    assert len(res.flags) == 1 and res.flags[0].startswith(FLAG_DRAFT_INVALID)


async def test_draft_llm_failure_keeps_skeleton() -> None:
    fake = FakeBackend().push(*[TransientLLMError("InternalServerError")] * 8)
    res = await draft_task(_ctx(), llm=_client(fake), prompts=PROMPTS)  # type: ignore[arg-type]
    assert res.output is None and res.flags == [FLAG_DRAFT_FAILED]
    task = TaskItem(
        task_id="t",
        track="common",
        target_competencies=_ctx().targets,
        tech_env=_ctx().env,
        difficulty_target="high",
        time_limit_min=60,
    )
    applied = apply(task, res)
    assert applied.title is None and FLAG_DRAFT_FAILED in applied.review_flags


def test_reviewer_note_with_contact_pattern_is_not_sent() -> None:
    assert safe_reviewer_note("함정을 하나 더 넣어 주세요") == ("함정을 하나 더 넣어 주세요", [])
    assert safe_reviewer_note("  ") == (None, [])
    assert safe_reviewer_note("fake.user@example.com 로 연락") == (None, [FLAG_NOTE_DROPPED])
    assert safe_reviewer_note("홍길동 지원자 경험처럼 바꿔 주세요") == (None, [FLAG_NOTE_DROPPED])
    assert safe_reviewer_note("제한 시간 안에 풀 수 있게 자료를 줄여 주세요")[1] == []
    assert safe_reviewer_note("가" * 1001) == (None, [FLAG_NOTE_TOO_LONG])


def test_tools_in_tech_used_and_log_ids_are_not_languages() -> None:
    # 도구·개념은 언어 목록에 없으니 통과, 로그의 'R-12'·'로그 C:' 는 언어가 아님, 'C 언어' 는 잡는다
    ok = _out(tech_used=["Python", "Maven", "FIR 필터"], scenario="robot=R-12 기록. 로그 C: 재시도")
    assert check(ok, _ctx()) == []
    bad = _out(scenario="모듈은 C 언어로 작성되어 있다.")
    assert check(bad, _ctx()) == ["본문에 기술 환경 밖 언어가 나옴: C"]


def test_check_flags_root_cause_revealed_in_visible_text() -> None:
    # first_mention_of = 지원자가 먼저 말해야 할 원인 — 제목·상황에 나오면 정답 노출
    leaked = _out(title="잠금 경합으로 인한 배치 지연 분석")
    assert any("핵심 원인 개념" in p for p in check(leaked, _ctx()))
    spaced = _out(scenario="가상의 회사에서 잠금  경합 이 의심된다.")  # 띄어쓰기만 다른 경우도
    assert any("핵심 원인 개념" in p for p in check(spaced, _ctx()))
    assert check(_out(), _ctx()) == []  # invariant 에만 있는 것은 괜찮다 (지원자에게 안 보임)


def test_long_artifact_is_trimmed_by_lines_and_flagged() -> None:
    # 2026-11-10 갱신 손실 재료: 자료를 3000자 넘게 써 스키마 위반 4번 — 코드가 줄 단위로 줄이고 사람 확인 (사용자 결정)
    from task_spec.schemas.domain import ARTIFACT_MAX, ARTIFACT_TRIMMED, Artifact
    from task_spec.spec.draft import trimmed_artifacts

    long = "\n".join(f"2026-11-01 10:00:{i:02d} item=7 qty=-1 status=200" for i in range(200))
    a = Artifact(artifact_id="a1", title="요청 기록 발췌", content=long, role="key")
    assert len(a.content) <= ARTIFACT_MAX and a.content.endswith(ARTIFACT_TRIMMED)
    kept = a.content.removesuffix(ARTIFACT_TRIMMED).rstrip().splitlines()
    assert all(line in long.splitlines() for line in kept)  # 줄 중간에서 자르지 않는다
    short = Artifact(artifact_id="a2", title="설정", content="x=1", role="support")
    assert short.content == "x=1"

    class Out:
        artifacts = [a, short]
        debug_stage = None

    assert trimmed_artifacts(Out()) == ["a1"]


def test_cross_family_targets_only_for_other_job_families() -> None:
    # 2026-11-10 갱신 손실 명세: 백엔드 공고의 'React·Vue 프론트엔드' 역량 수행준거를 인용하지 못해 실패 — 기능형에서만 표시로
    from dataclasses import replace

    from task_spec.spec.draft import cross_family_targets

    ctx = _ctx()
    fe = TargetCompetency(
        competency_id="comp:fe.framework", label="React·Vue", ncs_criterion_ids=["E9.1#1"]
    )
    sql = TargetCompetency(competency_id="comp:data.sql", label="SQL", ncs_criterion_ids=["E8.1#1"])
    infra = TargetCompetency(competency_id="comp:infra.server_ops", label="서버 인프라")
    backend = replace(ctx, targets=[*ctx.targets, fe, sql, infra], sub="sw.backend")
    assert [t.competency_id for t in cross_family_targets(backend)] == [
        "comp:fe.framework",
        "comp:infra.server_ops",
    ]
    assert cross_family_targets(replace(backend, sub="sw.frontend")) == [
        infra
    ]  # 프론트 공고의 fe.* 는 그대로
    assert cross_family_targets(replace(backend, sub=None)) == []  # 직군을 모르면 예전처럼
    # 검사: 빼라고 한 역량만 인용 검사에서 빠진다 (이슈형은 skip_targets 를 주지 않아 예전 그대로)
    out = _out()
    full = check(out, backend)
    assert any("comp:fe.framework" in p for p in full)
    skipped = check(
        out, backend, skip_targets=frozenset({"comp:fe.framework", "comp:infra.server_ops"})
    )
    assert not any("comp:fe.framework" in p for p in skipped)
    assert any("comp:data.sql" in p for p in skipped)
