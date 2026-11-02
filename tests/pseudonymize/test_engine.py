"""엔진: 문서 간 일관 토큰, 매핑 저장, 누락 검사, 재현율·과잉 치환 (Kiwi 포함)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import (
    Detectors,
    RunMapping,
    count_residual,
    pseudonymize_text,
)
from tests.pseudonymize.fixtures import (
    COVER_LETTER,
    KEEP,
    KEEP_B,
    PLANTED,
    PLANTED_B,
    RESUME,
    RESUME_B,
)

D = Detectors(load_dictionaries())


def test_planted_pii_all_removed_and_report_recall() -> None:
    m = RunMapping()
    out = pseudonymize_text(RESUME, m, D).text + "\n" + pseudonymize_text(COVER_LETTER, m, D).text
    missed = [(cat, v) for cat, v in PLANTED if v in out]
    by_cat: dict[str, list[int]] = {}
    for cat, v in PLANTED:
        by_cat.setdefault(cat, [0, 0])
        by_cat[cat][1] += 1
        by_cat[cat][0] += v not in out
    print("\nrecall " + " ".join(f"{c}={h}/{n}" for c, (h, n) in sorted(by_cat.items())))
    assert missed == []


def test_keep_terms_untouched() -> None:
    out = pseudonymize_text(RESUME, RunMapping(), D).text
    assert [k for k in KEEP if k not in out] == []


def test_same_entity_same_token_across_documents() -> None:
    m = RunMapping()
    r = pseudonymize_text(RESUME, m, D).text
    c = pseudonymize_text(COVER_LETTER, m, D).text
    org = next(t for t, vs in m.tokens.items() if "삼성전자" in vs)
    person = next(t for t, vs in m.tokens.items() if "박가온" in vs)
    assert org in r and org in c  # "삼성전자" 와 "삼성 전자" 가 같은 토큰
    assert person in r and person in c


def test_known_value_replaced_everywhere() -> None:
    m = RunMapping()
    out = pseudonymize_text("성명: 김가상\n김가상은 리더였다. 김가상의 역할", m, D).text
    assert "김가상" not in out
    assert out.count("[PERSON_1]") == 3


def test_residual_detects_leftover_values() -> None:
    m = RunMapping()
    m.token_for("PERSON", "김가상", None)
    assert count_residual("요약: 김가상 이 했다", m, D) >= 1
    assert count_residual("요약: [PERSON_1] 이 했다", m, D) == 0


def test_mapping_roundtrip_and_private_permissions(tmp_path: Path) -> None:
    m = RunMapping()
    pseudonymize_text(RESUME, m, D)
    path = tmp_path / "run" / "mapping.json"
    m.save(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = RunMapping.load(path)
    assert again.tokens == m.tokens and again.counters == m.counters


def test_resume_b_regression() -> None:
    out = pseudonymize_text(RESUME_B, RunMapping(), D).text
    assert [(c, v) for c, v in PLANTED_B if v in out] == []
    assert [k for k in KEEP_B if k not in out] == []
    assert (
        "경기도" not in out or "[PERSON" not in out.split("경기도")[0][-12:]
    )  # 지명을 이름으로 보지 않음
