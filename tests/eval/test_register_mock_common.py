"""목 공통 과제 키 — 실제 지원자 실행의 재사용 조회에 걸리지 않는다. 가상 데이터만."""

from __future__ import annotations

import pytest

from eval.starter.register_mock_common import MOCK_PREFIX, mock_key
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from tests.spec.test_store_pg import _task


def test_mock_key_from_eval_task_id() -> None:
    k = mock_key("eval-autoever_1_both#025", "onto-test")
    assert k.posting_id == "autoever_1_both#025" and k.prompt_ref.startswith(MOCK_PREFIX)
    with pytest.raises(ValueError):
        mock_key("ct-0123456789ab", "onto-test")


async def test_mock_row_is_not_reused_by_real_key() -> None:
    store = InMemoryCommonStore()
    mk = mock_key("eval-autoever_1_both#025", "onto-test")
    await store.save_draft(mk, _task(), [])
    real = CommonKey(
        posting_id=mk.posting_id, ontology_version="onto-test", prompt_ref="draft_feature_task@6"
    )
    assert await store.reusable(real) is None
    assert (
        await store.approved_kind(mk.posting_id, "onto-test") is None
    )  # draft 는 종류 고정에도 안 쓰인다
