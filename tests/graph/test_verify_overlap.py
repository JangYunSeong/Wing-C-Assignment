"""④ 공고 역량 → 주장 확인 노드 (verify_overlap) — FakeBackend, 가상 데이터만."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes.verify_overlap import NOTE, make_verify_overlap
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    Claim,
    PostingCompetency,
    PostingProfile,
    PseudoCheck,
    PseudoDocRef,
    RunState,
)

# 호출 하나가 EmptyResponse 로 끝나는 경우 — 다시 부르지 않는 빈 응답(내용 필터). 그냥 빈 응답은 클라이언트가 다시 부른다 (2026-11-15)
FAILED_CALL = Completion("", finish_reason="content_filter")

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
PSEUDO = (
    "가상 물류 창고 개편 2031.01~2031.06\n"
    "⟦과정⟧ [ORG_1] 입고 검수 화면을 만들고 바코드 스캐너를 연동\n"
    "⟦기여한 점⟧ 재고 실사 시간을 하루에서 두 시간으로 단축\n"
)


def _state(tmp_path: Path, *, claims: bool = True, comps: bool = True) -> RunState:
    path = tmp_path / "pseudonymized" / "r1" / "d1.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PSEUDO, encoding="utf-8")
    ref = PseudoDocRef(
        doc_id="d1",
        kind="resume",
        path=str(path),
        sha256=hashlib.sha256(PSEUDO.encode()).hexdigest(),
        check=PseudoCheck(passed=True, residual_count=0),
    )
    profile = PostingProfile(
        posting_id="p-fake",
        ontology_version="v-test",
        competencies=[
            PostingCompetency(competency_id="comp:a", label="가상 화면 개발", primary=True),
            PostingCompetency(competency_id="comp:b", label="가상 장비 연동", primary=True),
            PostingCompetency(competency_id="comp:c", label="가상 재고 관리", primary=False),
        ]
        if comps
        else [],
    )
    cs = [
        Claim(
            claim_id="d1-c1",
            doc_id="d1",
            action="입고 검수 화면 구현",
            quote="입고 검수 화면을 만들고",
            field="과정",
        ),
        Claim(
            claim_id="d1-c2",
            doc_id="d1",
            action="재고 실사 시간 단축",
            quote="재고 실사 시간을 하루에서 두 시간으로 단축",
            field="기여한 점",
        ),
        Claim(claim_id="d1-c3", doc_id="d1", action="지어낸 주장", quote="원문에 없는 문장"),
    ]
    return RunState(
        run_id="r1",
        ontology_version="v-test",
        posting_id="p-fake",
        posting_profile=profile,
        pseudo_docs=[ref],
        claims=cs if claims else [],
    )


def _answer(*items: tuple[str, str, list[str]]) -> Completion:
    body = {
        "items": [
            {"competency_id": c, "evidence_claims": refs, "reason": "가상", "label": lab}
            for c, lab, refs in items
        ]
    }
    return Completion(json.dumps(body, ensure_ascii=False))


async def _run(state: RunState, *answers: Completion) -> tuple[dict[str, Any], FakeBackend]:
    fake = FakeBackend().push(*answers)
    node = make_verify_overlap(LLMClient({"opencode": fake}), PROMPTS)
    return await node(state), fake


def _notes(out: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(e.message, e.ref_id) for e in out["errors"] if e.code == NOTE]


async def test_missing_ids_are_asked_again_once(tmp_path: Path) -> None:
    out, fake = await _run(
        _state(tmp_path),
        _answer(("comp:a", "action", ["c1"]), ("comp:c", "none", [])),
        # 두 번째 호출은 빠진 comp:b 만 묻는다 — 다시 답한 comp:a 는 목록 밖이라 버린다
        _answer(("comp:b", "related", ["c1"]), ("comp:a", "none", [])),
    )
    res = out["overlap_check"]
    assert res.status == "ok" and len(fake.calls) == 2
    assert [(c.competency_id, c.label, c.claim_ids) for c in res.items] == [
        ("comp:a", "action", ["d1-c1"]),  # 첫 답 유지
        ("comp:b", "related", ["d1-c1"]),
        ("comp:c", "none", []),
    ]
    assert ("빠진 역량을 다시 물음", "comp:b") in _notes(out)
    assert ("목록 밖 역량 id 를 버림", None) in _notes(out)
    assert [p.prompt for p in out["provenance"]] == ["verify_overlap@1", "verify_overlap@1"]
    p = out["provenance"][0]
    assert p.ontology_version == "v-test" and p.model == "opencode:qwen3.8-flash"
    assert len(p.prompt_sha256) == 64 and res.items[0].provenance_id == p.provenance_id


async def test_still_missing_after_retry_is_partial(tmp_path: Path) -> None:
    out, _ = await _run(
        _state(tmp_path),
        _answer(("comp:a", "action", ["c2"])),
        _answer(("comp:b", "mention", ["c1"])),
    )
    res = out["overlap_check"]
    assert res.status == "partial"
    assert [c.competency_id for c in res.items] == ["comp:a", "comp:b"]
    assert [(e.code, e.ref_id) for e in out["errors"] if e.code != NOTE] == [
        ("claim_not_pseudonymized", "d1-c3"),
        ("verify_labels_only", None),  # 카드 없이 라벨만 (retriever 미연결)
        ("verify_missing_after_retry", "comp:c"),
    ]


async def test_invalid_ids_and_no_evidence_are_corrected(tmp_path: Path) -> None:
    out, fake = await _run(
        _state(tmp_path),
        _answer(
            ("comp:zzz", "action", ["c1"]),  # 목록 밖 → 버림
            ("comp:a", "action", ["c9"]),  # 없는 주장 표시 → 근거 없음 → none
            ("comp:b", "related", []),  # 근거 없음 → none
            ("comp:c", "mention", ["c2", "c7"]),  # 아는 표시만 남김
            ("comp:c", "action", ["c1"]),  # 중복 → 앞의 답
        ),
    )
    res = out["overlap_check"]
    assert res.status == "ok" and len(fake.calls) == 1  # 빠진 것 없음 → 다시 묻지 않음
    assert [(c.competency_id, c.label, c.claim_ids) for c in res.items] == [
        ("comp:a", "none", []),
        ("comp:b", "none", []),
        ("comp:c", "mention", ["d1-c2"]),
    ]
    notes = _notes(out)
    assert ("목록 밖 역량 id 를 버림", None) in notes
    assert ("근거 주장 없는 판정을 none 으로", "comp:a") in notes
    assert ("근거 주장 없는 판정을 none 으로", "comp:b") in notes
    assert ("없는 주장 표시를 버림", "comp:c") in notes
    assert ("같은 역량 두 번 — 앞의 답을 씀", "comp:c") in notes
    assert all(
        "zzz" not in (e.ref_id or "") for e in out["errors"]
    )  # LLM 이 만든 id 는 남기지 않음


async def test_call_failure_falls_back_to_select_path(tmp_path: Path) -> None:
    # 첫 묶음·반씩 다시 묻기 둘 다 스키마 위반 (호출마다 1회 재요청 포함) → 모든 호출 실패 → failed
    out, fake = await _run(_state(tmp_path), *[Completion("not json")] * 6)
    assert out["overlap_check"].status == "failed" and out["overlap_check"].items == []
    codes = [e.code for e in out["errors"]]
    assert codes.count("SchemaViolation") == 3 and codes[-1] == "verify_failed"
    assert len(fake.calls) == 6  # 호출 3번 × 스키마 재요청 1번
    # 실패한 호출도 쓴 토큰·비용 기록은 남는다 (본문 없음)
    assert [p.prompt for p in out["provenance"]] == ["verify_overlap@1"] * 3
    assert all(p.retries == 1 for p in out["provenance"])


async def test_failed_batch_is_split_and_asked_again(tmp_path: Path) -> None:
    # G안: 첫 묶음이 빈 응답(추론 토큰 소진 등)으로 통째로 실패 → 반씩(2개 + 1개) 다시 → ok
    out, fake = await _run(
        _state(tmp_path),
        FAILED_CALL,
        _answer(("comp:a", "action", ["c1"]), ("comp:b", "none", [])),
        _answer(("comp:c", "related", ["c2"])),
    )
    res = out["overlap_check"]
    assert res.status == "ok" and len(fake.calls) == 3  # 첫 호출 + 다시 묻기 2번 (상한)
    assert [(c.competency_id, c.label) for c in res.items] == [
        ("comp:a", "action"),
        ("comp:b", "none"),
        ("comp:c", "related"),
    ]
    assert ("EmptyResponse", "공고 역량 확인 실패") in [(e.code, e.message) for e in out["errors"]]
    assert [ref for msg, ref in _notes(out) if msg == "빠진 역량을 다시 물음"] == [
        "comp:a",
        "comp:b",
        "comp:c",
    ]
    assert len(out["provenance"]) == 3  # 실패한 첫 호출도 비용 기록


async def test_split_retry_half_fails_is_partial(tmp_path: Path) -> None:
    out, fake = await _run(
        _state(tmp_path),
        FAILED_CALL,
        _answer(("comp:a", "action", ["c1"]), ("comp:b", "related", ["c2"])),
        FAILED_CALL,
    )
    res = out["overlap_check"]
    assert res.status == "partial" and len(fake.calls) == 3
    assert [c.competency_id for c in res.items] == ["comp:a", "comp:b"]
    errs = [(e.code, e.message, e.ref_id) for e in out["errors"] if e.code != NOTE]
    assert ("EmptyResponse", "빠진 역량 다시 묻기 실패", None) in errs
    assert (
        "verify_missing_after_retry",
        "다시 물어도 답이 없는 공고 역량 — 확인 안 됨",
        "comp:c",
    ) in errs


async def test_all_calls_fail_is_failed_with_bounded_calls(tmp_path: Path) -> None:
    out, fake = await _run(_state(tmp_path), FAILED_CALL, FAILED_CALL, FAILED_CALL)
    assert out["overlap_check"].status == "failed" and len(fake.calls) == 3
    assert [e.code for e in out["errors"] if e.code == "EmptyResponse"] == ["EmptyResponse"] * 3
    assert len(out["provenance"]) == 3


async def test_any_exception_is_contained(tmp_path: Path) -> None:
    # 백엔드의 예상 밖 예외 (4xx 등) — 실행을 죽이지 않고 failed, 메시지에 예외 내용은 남기지 않는다
    out, fake = await _run(_state(tmp_path), *[RuntimeError("400 bad request: 가상 본문")] * 3)
    assert out["overlap_check"].status == "failed" and len(fake.calls) == 3
    errs = [
        (e.code, e.message) for e in out["errors"] if e.code not in (NOTE, "verify_labels_only")
    ]
    assert errs[1:] == [
        ("RuntimeError", "공고 역량 확인 실패"),
        ("RuntimeError", "빠진 역량 다시 묻기 실패"),
        ("RuntimeError", "빠진 역량 다시 묻기 실패"),
        ("verify_failed", "공고 역량 확인 실패 — 선택 경로만"),
    ]
    assert all("가상 본문" not in e.message for e in out["errors"])
    # 설정되지 않은 provider (ValueError) 도 같다
    node = make_verify_overlap(LLMClient({}), PROMPTS)
    out2 = await node(_state(tmp_path))
    assert out2["overlap_check"].status == "failed" and "ValueError" in [
        e.code for e in out2["errors"]
    ]


async def test_retry_failure_keeps_first_answers(tmp_path: Path) -> None:
    out, _ = await _run(
        _state(tmp_path),
        _answer(("comp:a", "action", ["c1"]), ("comp:b", "related", ["c2"])),
        RuntimeError("timeout"),
    )
    res = out["overlap_check"]
    assert res.status == "partial"
    assert [(c.competency_id, c.label) for c in res.items] == [
        ("comp:a", "action"),
        ("comp:b", "related"),
    ]
    codes = [(e.code, e.message, e.ref_id) for e in out["errors"]]
    assert ("RuntimeError", "빠진 역량 다시 묻기 실패", None) in codes
    assert (
        "verify_missing_after_retry",
        "다시 물어도 답이 없는 공고 역량 — 확인 안 됨",
        "comp:c",
    ) in codes
    assert len(out["provenance"]) == 1  # 성공한 첫 호출


async def test_loose_answers_are_trimmed_not_failed(tmp_path: Path) -> None:
    # 출력 한도를 넘는 답(이유 500자·근거 표시 8개·모르는 label)도 호출 전체를 실패시키지 않는다
    body = {
        "items": [
            {
                "competency_id": "comp:a",
                "evidence_claims": ["c1", "c2", "c1", "c3", "c4", "c5", "c6", "c7"],
                "reason": "가" * 500,
                "label": "action",
            },
            {"competency_id": "comp:b", "evidence_claims": "c2", "label": "maybe"},
            {"competency_id": "comp:c", "label": "none", "extra": 1},
            "쓰레기",
        ]
    }
    out, fake = await _run(
        _state(tmp_path),
        Completion(json.dumps(body, ensure_ascii=False)),
        _answer(("comp:b", "related", ["c2"])),
    )
    res = out["overlap_check"]
    assert res.status == "ok" and len(fake.calls) == 2  # 모르는 label 인 comp:b 만 다시 물음
    got = {c.competency_id: (c.label, c.claim_ids) for c in res.items}
    assert got == {
        "comp:a": ("action", ["d1-c1", "d1-c2"]),
        "comp:b": ("related", ["d1-c2"]),
        "comp:c": ("none", []),
    }
    assert ("알 수 없는 판정 값 — 그 역량을 버림", "comp:b") in _notes(out)
    assert "가" * 50 not in json.dumps(res.model_dump(), ensure_ascii=False)  # 이유는 남지 않는다


def test_normalize_caps_evidence() -> None:
    from task_spec.nodes.verify_overlap import MAX_EVIDENCE, normalize
    from task_spec.schemas import VerifyParsedVerdict

    refmap = {f"c{i}": f"d1-c{i}" for i in range(1, 10)}
    item = VerifyParsedVerdict(
        competency_id="comp:a", evidence_claims=[f"c{i}" for i in range(1, 10)], label="action"
    )
    out = normalize([item], ["comp:a"], refmap, None)
    assert out.checks["comp:a"].claim_ids == [f"d1-c{i}" for i in range(1, MAX_EVIDENCE + 1)]
    assert (f"근거 주장을 {MAX_EVIDENCE}개로 줄임", "comp:a") in out.notes


class _Capture(FakeBackend):
    """메시지 본문을 기록하는 테스트 전용 백엔드 (FakeBackend 는 본문을 남기지 않는다)."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def complete(self, model, messages, params):  # type: ignore[no-untyped-def]
        self.texts.append("\n".join(m.content for m in messages))
        return await super().complete(model, messages, params)


def _many(tmp_path: Path, n: int) -> RunState:
    base = _state(tmp_path)
    comps = [
        PostingCompetency(competency_id=f"comp:v{i:02d}", label=f"가상 역량 {i}", primary=True)
        for i in range(n)
    ]
    assert base.posting_profile is not None
    return base.model_copy(
        update={"posting_profile": base.posting_profile.model_copy(update={"competencies": comps})}
    )


async def test_many_competencies_are_asked_in_batches(tmp_path: Path) -> None:
    st = _many(tmp_path, 65)
    ids = [c.competency_id for c in st.posting_profile.competencies]  # type: ignore[union-attr]
    fake = _Capture().push(
        _answer(*[(c, "none", []) for c in ids[:40]]),
        _answer(*[(c, "none", []) for c in ids[40:]]),
    )
    out = await make_verify_overlap(LLMClient({"opencode": fake}), PROMPTS)(st)
    res = out["overlap_check"]
    assert res.status == "ok" and len(res.items) == 65 and len(fake.texts) == 2
    assert [sum(f"- {c} " in t for c in ids) for t in fake.texts] == [40, 25]


async def test_llm_input_has_only_claims_and_ontology_text(tmp_path: Path) -> None:
    st = _state(tmp_path)
    # 원문·가명화 매핑이 디스크에 있어도 LLM 입력에는 가지 않는다 (가상 값)
    raw = tmp_path / "raw" / "r1" / "d1.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("홍가상 010-0000-0000 가상전자", encoding="utf-8")
    (tmp_path / "pseudonymized" / "r1" / "d1.mapping.json").write_text(
        json.dumps({"[ORG_1]": "가상전자"}, ensure_ascii=False), encoding="utf-8"
    )
    fake = _Capture().push(
        _answer(("comp:a", "none", []), ("comp:b", "none", []), ("comp:c", "none", []))
    )
    await make_verify_overlap(LLMClient({"opencode": fake}), PROMPTS)(st)
    sent = "\n".join(fake.texts)
    for secret in ("홍가상", "010-0000-0000", "가상전자"):
        assert secret not in sent
    # 이력서 전문이 아니라 주장만 (불변식 3): 주장이 아닌 머리 줄·지어낸 주장(인용 불일치)은 가지 않는다
    assert "가상 물류 창고 개편" not in sent and "원문에 없는 문장" not in sent
    assert "입고 검수 화면을 만들고" in sent and "바코드 스캐너를 연동" not in sent  # 인용 밖 문장


async def test_skips_without_llm_claims_or_posting_competencies(tmp_path: Path) -> None:
    assert await make_verify_overlap(None, PROMPTS)(_state(tmp_path)) == {"overlap_check": None}
    out, fake = await _run(_state(tmp_path, claims=False))
    assert out == {"overlap_check": None} and fake.calls == []
    out, fake = await _run(_state(tmp_path, comps=False))
    assert out == {"overlap_check": None} and fake.calls == []


async def test_failed_batch_among_many_is_split(tmp_path: Path) -> None:
    st = _many(tmp_path, 65)
    ids = [c.competency_id for c in st.posting_profile.competencies]  # type: ignore[union-attr]
    fake = _Capture().push(
        FAILED_CALL,  # 첫 묶음(40개) 실패
        _answer(*[(c, "none", []) for c in ids[40:]]),  # 둘째 묶음(25개) 답
        _answer(*[(c, "none", []) for c in ids[:20]]),  # 실패 묶음 앞 반
        _answer(*[(c, "none", []) for c in ids[20:40]]),  # 뒤 반
    )
    out = await make_verify_overlap(LLMClient({"opencode": fake}), PROMPTS)(st)
    res = out["overlap_check"]
    assert res.status == "ok" and len(res.items) == 65
    assert [sum(f"- {c} " in t for c in ids) for t in fake.texts] == [40, 25, 20, 20]
