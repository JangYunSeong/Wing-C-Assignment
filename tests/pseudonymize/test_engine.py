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


# ---- 2026-11-04 Kiwi 이름 후보 오탐 (가상 데이터) ----


def test_kiwi_noun_like_rules() -> None:
    from task_spec.nodes.pseudonymize.detectors import _kiwi_noun_like

    assert _kiwi_noun_like("유튜브 채널", 0, 3)  # 이름에 안 쓰는 외래어 음절
    assert not _kiwi_noun_like("윤하람과 함께", 0, 3)
    # privacy-auditor 1차 H2: 이름에 있을 법한 음절(률·량·스·크·트·플·값 …)로는 빼지 않는다, 조사가 붙으면 면제
    assert not _kiwi_noun_like("김동률이 주도했다", 0, 3)
    assert not _kiwi_noun_like("강동량과 함께", 0, 3)
    # 2차 H-new1: 뒤에 기술어가 와도 문맥으로 빼지 않는다 (명사 오탐은 단어 목록으로)
    assert not _kiwi_noun_like("김민재 값 비교", 0, 3)
    assert not _kiwi_noun_like("박지훈 검색팀 소속으로 근무", 0, 3)


def test_names_before_tech_words_are_replaced() -> None:
    """privacy-auditor 2차 H-new1·W1 — 지난 수정은 이 문장들의 이름을 남겼다 (HEAD 는 치환)."""
    for text, name in [
        ("김민재 값 비교", "김민재"),
        ("한지우 지표 설계", "한지우"),
        ("박지훈 점수 산출", "박지훈"),
        ("오세훈 검색을 맡았다", "오세훈"),
        ("서지수 기반 서비스", "서지수"),
        ("김동률 값을 확인", "김동률"),
        ("박지훈 검색팀 소속으로 근무", "박지훈"),
    ]:
        assert name not in pseudonymize_text(text, RunMapping(), D).text, text


def test_tech_nouns_not_names_in_reproduced_context() -> None:
    text = "실패 로그의 임베딩 유사도 검색(HNSW)과 키워드 검색(BM25)을 결합했다.\n- 기본값 정리\n- 오프셋 분석"
    assert pseudonymize_text(text, RunMapping(), D).text == text


def test_names_with_loanword_like_syllables_still_detected() -> None:
    """privacy-auditor H2 회귀 — 지난 변경은 이 이름들을 Kiwi 경로에서 모두 뺐다."""
    for n in ("강동량", "이재플", "장민클"):
        assert n not in pseudonymize_text(f"프로젝트는 {n}이 주도했다.", RunMapping(), D).text


def test_kiwi_name_spreads_into_compounds_in_same_run() -> None:
    """privacy-auditor H1 회귀 — Kiwi 로 잡힌 이름도 같은 run 의 합성어 안까지 치환한다 (조용한 누출 없음)."""
    m = RunMapping()
    pseudonymize_text("프로젝트는 윤하람이 주도했다.", m, D)
    r = pseudonymize_text("깃 작성자: 개발자윤하람, 윤하람의 PR", m, D)
    assert "윤하람" not in r.text and r.residual == 0
