"""주장 추출 채점기 자기 검증 — 정답을 그대로 넣으면 만점, 오답을 섞으면 감점 (LLM 호출 없음)."""

from __future__ import annotations

import pytest

from eval.claims.generate import docs
from eval.claims.run_eval import score
from task_spec.schemas import Claim


def _perfect(d):  # type: ignore[no-untyped-def]
    return [
        Claim(
            claim_id=f"x{i}-{k}", doc_id=d.id, action="a", quote=s.text, field=s.field, role=s.role
        )
        for i, s in enumerate(d.sentences)
        for k in range(s.actions)
    ]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_perfect_predictions_score_full(split: str) -> None:
    for d in docs(split, 20):
        assert len({s.text for s in d.sentences}) == len(d.sentences), (
            d.id
        )  # 문서 안 문장 중복 없음
        sc = score(d, _perfect(d))
        assert sc["sent_hit"] == sc["sent_total"] and sc["act_hit"] == sc["act_total"], d.id
        assert sc["good_claims"] == sc["claims"] == sc["field_ok"] == sc["role_ok"], d.id
        assert sc["non_action_claims"] == 0 and sc["unmatched"] == 0


def test_noise_is_penalized() -> None:
    d = next(x for x in docs("dev", 8) if x.kind == "resume")
    non_action = next(s for s in d.sentences if not s.actions)
    two = next((s for s in d.sentences if s.actions == 2), None)
    preds = _perfect(d)
    preds.append(Claim(claim_id="n1", doc_id=d.id, action="x", quote=non_action.text))
    preds.append(
        Claim(claim_id="n2", doc_id=d.id, action="x", quote="문서에 없는 지어낸 문장이다.")
    )
    sc = score(d, preds)
    assert sc["non_action_claims"] == 1 and sc["unmatched"] == 1
    if two is not None:  # 두 행동 문장을 1건으로만 뽑으면 split 실패
        merged = [c for c in _perfect(d) if c.quote != two.text] + [
            Claim(claim_id="m", doc_id=d.id, action="a", quote=two.text)
        ]
        assert score(d, merged)["split_hit"] == score(d, _perfect(d))["split_hit"] - 1
