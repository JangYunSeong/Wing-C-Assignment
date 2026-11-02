"""가명화 평가 회귀 — dev 세트 기준치 (2026-11-02 측정값에서 여유를 조금 둔 하한).

test 세트는 여기서 돌리지 않는다 (숨겨 둔 세트는 사람이 정한 시점에만 `run_eval --split test`).
"""

from __future__ import annotations

from eval.pseudonymize.run_eval import evaluate, load

# 범주별 하한. PRODUCT 는 따옴표·라벨 없는 서비스명이 구조적 한계라 낮다.
FLOORS = {
    "CONTACT": 1.0,
    "LINK": 1.0,
    "REMOVE": 1.0,
    "SCHOOL": 1.0,
    "COURSE": 1.0,
    "PERSON": 0.99,
    "ORG": 0.95,
    "PRODUCT": 0.55,
}


def test_dev_recall_floors_and_no_over_replacement() -> None:
    res = evaluate(load("dev"))
    low = {c: f"{h}/{n}" for c, (h, n) in res["recall"].items() if n and h / n < FLOORS.get(c, 1.0)}
    assert low == {}, f"재현율 하한 미달: {low}"
    assert res["over_replacement"][0] == 0, res["keep_lost"][:5]
    assert res["stopped_by_residual"] == 0
