"""top-down 겹침 평가(eval/retrieve/run_topdown.py)의 순수 함수 — 가상 데이터만.

프롬프트 조립·답 검증은 운영 노드 테스트(tests/graph/test_verify_overlap.py)에서 본다.
"""

from __future__ import annotations

from eval.retrieve.run_topdown import (
    gold_from_table,
    ordered,
    pick_controls,
    roles,
    score,
    stability,
    state_for,
    verdicts_from,
    yes_bias,
)
from task_spec.schemas import OverlapCheck, OverlapCheckResult

CLAIMS = [
    {
        "claim_id": "d-c1",
        "action": "입고 검수 화면 구현",
        "quote": "입고 검수 화면을 만들고",
        "field": "과정",
    },
    {
        "claim_id": "d-c2",
        "action": "재고 실사 시간 단축",
        "quote": "재고 실사 시간을 하루에서 두 시간으로 단축",
        "field": "기여한 점",
    },
]
PSEUDO_DOC = {
    "doc_id": "resume-1",
    "kind": "resume",
    "path": "data/pseudonymized/eval/resume-1.txt",
    "sha256": "0" * 64,
    "check": {"passed": True, "residual_count": 0},
}


def test_state_for_puts_asked_competencies_in_profile_order() -> None:
    row = {"posting": "p-fake", "pseudo_doc": PSEUDO_DOC, "claims": CLAIMS}
    st = state_for(row, ["comp:b", "comp:a"], {"comp:a": "가상 A"}, "v-test")
    assert st.posting_profile is not None
    assert [(c.competency_id, c.label) for c in st.posting_profile.competencies] == [
        ("comp:b", "comp:b"),
        ("comp:a", "가상 A"),
    ]
    assert [(c.claim_id, c.doc_id, c.field) for c in st.claims] == [
        ("d-c1", "resume-1", "과정"),
        ("d-c2", "resume-1", "기여한 점"),
    ]


def test_verdicts_from_marks_missing_as_none() -> None:
    res = OverlapCheckResult(
        status="partial",
        items=[OverlapCheck(competency_id="comp:a", label="action", claim_ids=["d-c2"])],
    )
    v = verdicts_from(res, ["comp:a", "comp:b"])
    assert v["comp:a"] == {"label": "action", "claims": ["d-c2"]}
    assert v["comp:b"] == {"label": "none", "claims": [], "missing": True}


def test_controls_and_order_are_fixed_and_exclude_own() -> None:
    own, others, judged = (
        {"comp:a", "comp:b"},
        {"comp:a", "comp:c", "comp:d", "comp:e", "comp:f"},
        {
            "comp:c",
            "comp:d",
            "comp:e",
            "comp:a",
        },
    )
    c1 = pick_controls("s1", "p1", own, others, judged)
    assert c1 == pick_controls("s1", "p1", own, others, judged)
    assert set(c1) == {"comp:c", "comp:d", "comp:e"}  # f 는 정답 없음, a 는 공고 안
    ids = {"comp:a", "comp:b", *c1}
    assert (
        ordered(ids, "s1", "p1") == ordered(ids, "s1", "p1")
        and set(ordered(ids, "s1", "p1")) == ids
    )


def _row(
    resume: str, posting: str, table: list[tuple[str, bool, str]], controls: dict[str, str]
) -> dict:
    verdicts = {
        cid: {"label": td, "claims": ["d-c2"] if td != "none" else []} for cid, _, td in table
    }
    verdicts |= {cid: {"label": lab, "claims": []} for cid, lab in controls.items()}
    return {
        "resume": resume,
        "posting": posting,
        "claims": CLAIMS[:2],
        "controls": list(controls),
        "verdicts": verdicts,
        "table": [
            {
                "competency_id": cid,
                "label": cid,
                "L0": l0,
                "td": td,
                "td_claims": verdicts[cid]["claims"],
            }
            for cid, l0, td in table
        ],
    }


GOLD = {
    "resumes": {
        "s1": {
            "comp:a": {"label": "action", "evidence": ["재고 실사 시간을 하루에서"]},
            "comp:b": {"label": "action", "evidence": ["원문에 없는 근거"]},
            "comp:c": {"label": "none", "evidence": []},
            "comp:d": {"label": "related", "evidence": ["입고 검수 화면"]},
            "comp:e": {"label": "none", "evidence": []},
        }
    }
}


def test_score_layers_causes_and_resume_level() -> None:
    rows = [
        _row(
            "s1",
            "p1",
            [("comp:a", True, "action"), ("comp:b", False, "none"), ("comp:c", False, "action")],
            {"comp:e": "none"},
        ),
        _row(
            "s1",
            "p2",
            [("comp:a", False, "related"), ("comp:d", False, "related")],
            {"comp:c": "none"},
        ),
    ]
    s = score(rows, GOLD)
    act = s["row_level"]["action"]
    assert act["L0"] == {"predicted": 1, "gold": 3, "precision": 1.0, "recall": 0.333}
    assert act["TD"] == {"predicted": 2, "gold": 3, "precision": 0.5, "recall": 0.333}
    assert act["L0∪TD"]["predicted"] == 2 and act["L0∩TD"]["predicted"] == 1
    assert s["row_level"]["action+related"]["TD+rel"]["recall"] == 0.75
    assert s["td_miss_causes"] == {"주장 없음": 1, "TD related": 1}
    res = s["resume_level"]["action"]
    assert res["TD"] == {"predicted": 2, "gold": 2, "precision": 0.5, "recall": 0.5}
    bias = yes_bias(rows, GOLD)
    assert bias["posting"] == {"gold_none": 1, "action_rate": 1.0, "action_or_related_rate": 1.0}
    assert bias["control"]["gold_none"] == 2 and bias["control"]["action_rate"] == 0.0
    assert roles(rows) == {"lead": 2}  # 근거 주장 d-c2 는 기여한 점 칸
    assert stability(rows, rows) == {"n": 7, "label_agree": 1.0, "action_agree": 1.0}


def test_gold_from_table_is_provisional() -> None:
    g = gold_from_table(
        [
            {
                "resume": "s9",
                "table": [{"competency_id": "comp:a", "gt": "action", "evidence": "가상 근거"}],
            }
        ]
    )
    assert g["provisional"] and g["resumes"]["s9"]["comp:a"] == {
        "label": "action",
        "evidence": ["가상 근거"],
    }
