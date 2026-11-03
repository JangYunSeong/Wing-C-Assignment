"""extract_claims 노드를 FakeBackend 로 그래프 안에서 검증한다. 데이터는 가상."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DocumentRef, PseudoCheck, PseudoDocRef
from tests.conftest import config_for, fake_doc, run_input

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
PSEUDO = (
    "가상 데이터\n⟦담당업무⟧ [ORG_1] 에서 데이터 파이프라인을 설계했다.\n⟦성과⟧ 처리 시간 30% 단축."
)


def _writer(tmp_path: Path):
    async def pseudonymizer(run_id: str, doc: DocumentRef) -> PseudoDocRef:
        out = tmp_path / "pseudonymized" / run_id / f"{doc.doc_id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(PSEUDO, encoding="utf-8")
        return PseudoDocRef(
            doc_id=doc.doc_id,
            kind=doc.kind,
            path=str(out),
            sha256=hashlib.sha256(PSEUDO.encode()).hexdigest(),
            check=PseudoCheck(passed=True, residual_count=0, detectors=["fake"]),
        )

    return pseudonymizer


async def _run(tmp_path: Path, fake: FakeBackend | None, docs: list[DocumentRef]):
    async with make_checkpointer(kind="memory") as cp:
        llm = LLMClient({"opencode": fake}) if fake else None
        graph = build_graph(cp, pseudonymizer=_writer(tmp_path), llm=llm, prompts=PROMPTS)
        inp = run_input(docs=docs)
        return await graph.ainvoke(inp, config_for(inp))


async def test_claims_extracted_with_provenance_and_bad_quote_dropped(tmp_path: Path) -> None:
    body = {
        "claims": [
            {
                "action": "데이터 파이프라인 설계",
                "target": "데이터 파이프라인",
                "quote": "[ORG_1] 에서 데이터 파이프라인을 설계했다.",
            },
            {"action": "처리 시간 단축", "result": "30%", "quote": "처리 시간 30% 단축."},
            {"action": "지어낸 주장", "quote": "모델 정확도를 99% 로 올렸다."},
        ]
    }
    fake = FakeBackend().push(Completion(json.dumps(body, ensure_ascii=False)))
    result = await _run(tmp_path, fake, [fake_doc("d1", "resume")])

    claims = result["claims"]
    assert [c.claim_id for c in claims] == ["d1-c1", "d1-c2"]
    # 역할은 LLM 이 아니라 칸 규칙으로 (불변식 6): ⟦담당업무⟧ + '설계' 표현 → 설계, ⟦성과⟧ → 판정 안 함
    assert [(c.field, c.role, c.role_basis) for c in claims] == [
        ("담당업무", "design", "field:담당업무+design_term"),
        ("성과", None, "field:성과"),
    ]
    assert len(result["provenance"]) == 1
    prov = result["provenance"][0]
    assert all(c.provenance_id == prov.provenance_id for c in claims)
    assert prov.prompt == PROMPTS.get("extract_claims").ref  # CURRENT
    codes = [e.code for e in result["errors"]]
    assert codes == ["quote_mismatch"]
    assert "99%" not in result["errors"][0].message  # 에러에 원문·인용 없음
    assert result["__interrupt__"]  # 승인 단계까지 진행


async def test_one_document_failure_does_not_block_others(tmp_path: Path) -> None:
    good = json.dumps(
        {"claims": [{"action": "설계", "quote": "처리 시간 30% 단축."}]}, ensure_ascii=False
    )
    fake = FakeBackend().push(Completion("깨진 응답"), Completion("또 깨짐"), Completion(good))
    result = await _run(tmp_path, fake, [fake_doc("d1", "resume"), fake_doc("d2", "resume")])
    assert [c.doc_id for c in result["claims"]] == ["d2"]
    assert [(e.code, e.ref_id) for e in result["errors"]] == [("SchemaViolation", "d1")]


async def test_no_llm_configured_is_recorded(tmp_path: Path) -> None:
    result = await _run(tmp_path, None, [fake_doc("d1", "resume")])
    assert result["claims"] == []
    assert [e.code for e in result["errors"]] == ["llm_not_configured"]
