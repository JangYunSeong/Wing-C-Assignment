"""엔진: 문서 간 일관 토큰, 매핑 저장, 누락 검사, 재현율·과잉 치환 (Kiwi 포함)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

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


# ---- 2026-11-04 2차: 회사 문맥 규칙 (Kiwi 포함, 가상 데이터) ----


def test_weak_suffix_org_in_free_text() -> None:
    """약한 업종 접미어(카드·전자·증권 …)는 Kiwi 가 일반 명사로만 나누지 않을 때 회사로 본다."""
    for text, name in [
        ("고객사인 미르카드의 정산 배치를 이관했다.", "미르카드"),
        ("소담전자 생산기술팀에서 설비 데이터 수집을 맡았다.", "소담전자"),
        ("졸업 후 다솜증권에서 트레이딩 시스템을 운영했다.", "다솜증권"),
    ]:
        assert name not in pseudonymize_text(text, RunMapping(), D).text, text


def test_weak_suffix_generic_compounds_kept() -> None:
    text = "추심은행과 수탁은행, 개설은행에 통지했다. 자동화솔루션을 도입하고 신상카드를 작성했다."
    assert pseudonymize_text(text, RunMapping(), D).text == text


def test_loanwords_in_team_and_employment_context_are_not_orgs() -> None:
    """사전 밖 기술 외래어는 회사와 구분되지 않아, 팀 이름 앞·재직 문맥에서는 접미어 없는 후보를 받지 않는다."""
    text = "임베디드 개발팀 (2021.03 ~ 2022.02)\n임베디드 인턴으로 근무했다.\n온프레미스에서 3년간 운영했다."
    assert pseudonymize_text(text, RunMapping(), D).text == text


def test_org_header_first_cell_without_suffix() -> None:
    out = pseudonymize_text(
        "다온 | 결제플랫폼팀 백엔드 개발자 | 2022.04 ~ 현재", RunMapping(), D
    ).text
    assert out.startswith("[ORG_1] |")


def test_org_span_never_swallows_particles() -> None:
    """후보에 조사가 붙으면 다른 표기("다온헬스에서")가 전파되지 않고 새므로, 경계는 Kiwi 형태소로 맞춘다."""
    m = RunMapping()
    r = pseudonymize_text("다온헬스에서 2년간 근무하며 다온헬스의 앱을 운영했다.", m, D)
    assert "다온헬스" not in r.text and r.residual == 0
    r2 = pseudonymize_text("[가상] 누리랩스에서 인턴으로 근무", RunMapping(), D)
    assert r2.residual == 0 and "인턴으로" in r2.text


def test_name_in_org_header_slot_is_still_replaced() -> None:
    """회사 머리말 규칙이 이름 모양 후보를 회사로 바꾸지 않는다 — 이름 탐지기(Kiwi)가 잡는다."""
    text = "가상 데이터\n\n\n\n\n\n\n경력\n윤하람 | 백엔드 개발자 | 2022.01 ~ 현재\n"
    assert "윤하람" not in pseudonymize_text(text, RunMapping(), D).text


def test_org_header_loanword_is_replaced_only_in_place() -> None:
    """접미어 없는 경력 머리말 첫 칸은 그 자리만 치환하고 전파하지 않는다 — 기술 외래어가 주장 재료에서
    지워지지 않게 (privacy-auditor 2차 M1)."""
    m = RunMapping()
    r = pseudonymize_text(
        "온프레미스 | 인프라 엔지니어 | 2021.01 ~ 2022.01\n온프레미스 서버를 클라우드로 옮겼다.",
        m,
        D,
    )
    assert r.text.startswith("[ORG_1] |") and "온프레미스 서버" in r.text and r.residual == 0
    assert m.context == ["온프레미스"]  # 공개 어휘 — 고용 문맥 언급에만 전파


def test_mapping_spread_lists_roundtrip(tmp_path: Path) -> None:
    m = RunMapping()
    pseudonymize_text(
        "온프레미스 | 인프라 엔지니어 | 2021.01 ~ 2022.01\n이름\t성과\n연락처\t010-0000-0000", m, D
    )
    m.save(tmp_path / "mapping.json")
    loaded = RunMapping.load(tmp_path / "mapping.json")
    assert loaded.context == ["온프레미스"] and loaded.local == ["성과"]


@pytest.mark.parametrize(
    "text",
    [
        "개발자 양성 과정 운영을 지원하며 수강생 데이터를 분석했다.",
        "AI 아카데미 강사로 참여 (2023.03)",
        "여름 방학 코딩 캠프 운영 보조 (2021.07)",
        "온라인 강의 플랫폼의 부트캠프 상품 결제 기능 개발 (2022.01 ~ 2022.06)",
    ],
)
def test_course_rule_skips_operator_context(text: str) -> None:
    assert pseudonymize_text(text, RunMapping(), D).text == text


def test_generic_course_name_replaced_only_in_place() -> None:
    m = RunMapping()
    out = pseudonymize_text(
        "신입 개발자 교육 과정 수료 (2020.03)\n신입 개발자 교육 과정 자료 정리", m, D
    ).text
    assert out.startswith("[COURSE_1] 수료") and "신입 개발자 교육 과정 자료" in out


def test_suffixless_header_company_propagates_to_prose() -> None:
    """공개 어휘가 아닌 첫 칸 값은 지원자 회사로 보고 전파한다 — 본문 언급도 같은 토큰, 누락 검사도 센다 (2차 후속)."""
    m = RunMapping()
    r = pseudonymize_text(
        "다온 | 결제플랫폼팀 백엔드 개발자 | 2022.04 ~ 현재\n다온에서 결제 서버를 맡았다.", m, D
    )
    assert "다온" not in r.text and r.residual == 0 and m.local == []


def test_course_name_outside_public_vocab_propagates() -> None:
    m = RunMapping()
    out = pseudonymize_text(
        "윤슬 클라우드 부트캠프 3기 수료\n윤슬 클라우드 부트캠프 과제를 정리했다.", m, D
    ).text
    assert "윤슬 클라우드" not in out and m.local == []


def test_public_vocab_has_loanwords_and_no_known_companies_or_contacts() -> None:
    import re

    d = load_dictionaries()
    assert {"온프레미스", "임베디드", "성과"} <= d.public_vocab
    assert "가온" not in d.public_vocab  # 드문 이름 모양 두 음절 (code review M3)
    assert not [w for w in d.public_vocab if w.startswith("www") or w.endswith(".com")]
    companies = {a.casefold() for e in d.entries if e.category == "ORG" for a in e.aliases}
    assert not (companies & {w.casefold() for w in d.public_vocab})
    assert not [w for w in d.public_vocab if "@" in w or re.search(r"\d{4,}", w)]


# ---- 재점검(H-A·M-B·M-C·M-A4)·code review(H1·M5·M7) 후속 (가상 데이터 — 실존 인물·회사 아님) ----


@pytest.mark.parametrize("company", ["중앙", "가상결제", "바람", "이음"])
def test_public_word_company_cell_always_replaced_and_context_spread(company: str) -> None:
    """공개 어휘로 된 회사 이름: 머리 줄 첫 칸은 항상, 본문은 고용 문맥이 붙은 언급만 바꾼다 (재점검 H-A)."""
    m = RunMapping()
    text = (
        f"{company} | 백엔드 개발자 | 2020.01 ~ 2021.01\n"
        f"{company}에서 3년간 결제 서버를 맡았다. {company}에 입사해 정산을 개편했다.\n"
        f"(전) {company} 플랫폼팀\n"
        f"생산 {company} 데이터와 {company}색 화면을 정리했다.\n"
    )
    r = pseudonymize_text(text, m, D)
    lines = r.text.split("\n")
    assert lines[0].startswith("[ORG_1] |")
    assert company not in lines[1] and company not in lines[2]
    assert f"생산 {company} 데이터" in lines[3] and f"{company}색" in lines[3]
    assert r.residual == 0


def test_context_spread_counted_by_residual_check() -> None:
    """고용 문맥 전파 값이 남으면 누락 검사가 센다."""
    from task_spec.nodes.pseudonymize.engine import count_residual

    m = RunMapping()
    pseudonymize_text("중앙 | 백엔드 개발자 | 2020.01 ~ 2021.01", m, D)
    assert count_residual("중앙에서 3년간 근무했다.", m, D) >= 1
    assert count_residual("생산 중앙 데이터", m, D) == 0


@pytest.mark.parametrize(
    "variant",
    [
        "G. Seo",
        "Seo G.",
        "G.O. Seo",
        "Seo-Gaon",
        "seogaon",
        "SeoGaon",
        "gaon_seo",
        "Gaon-seo",
        "서 가온",
    ],
)
def test_romanized_and_spaced_variants_of_known_name(variant: str) -> None:
    """이미 잡은 "서가온"의 이니셜·붙여쓰기·밑줄·띄어 쓴 표기도 바꾼다 (재점검 M-B)."""
    m = RunMapping()
    r = pseudonymize_text(f"이름: 서가온\n\n\n\n\n\n\n리뷰어: {variant} (코드 리뷰)", m, D)
    assert variant not in r.text and r.residual == 0
    assert list(m.tokens) == ["[PERSON_1]"]


def test_korean_given_name_alone_replaced_with_particle() -> None:
    from task_spec.nodes.pseudonymize.aliases import build_alias_set

    aliases = build_alias_set([("[PERSON_1]", "서윤슬")], D.dicts)
    assert aliases.replace("윤슬은 팀원과 배포를 맡았다.") == "[PERSON_1]은 팀원과 배포를 맡았다."
    m = RunMapping()
    r = pseudonymize_text("이름: 서윤슬\n\n\n\n\n\n\n윤슬은 팀원과 배포를 맡았다.", m, D)
    assert "윤슬" not in r.text and r.residual == 0


def test_korean_given_name_alone_skips_common_nouns() -> None:
    """이름만 한글 표기는 Kiwi 가 일반 명사로 보거나 공개 어휘면 만들지 않는다 ("하늘색", "하늘은")."""
    out = pseudonymize_text(
        "이름: 도하늘\n\n\n\n\n\n\n하늘은 맑고 하늘색 버튼을 만들었다.", RunMapping(), D
    ).text
    assert "하늘은 맑고" in out and "하늘색" in out


@pytest.mark.parametrize("word", ["lora", "roi", "boa", "ami", "ansi"])
def test_given_only_alias_skips_public_english_words(word: str) -> None:
    """이름만 영문 표기가 기술 약어와 겹치면 만들지 않는다 (재점검 M-C)."""
    from task_spec.nodes.pseudonymize.engine import _propagation

    m = RunMapping()
    pseudonymize_text("이름: 서가온\nSeo Gaon", m, D)
    prop = _propagation(m, D.dicts)
    assert not any(re.fullmatch(x, word, re.IGNORECASE) for x in prop.aliases.given_patterns)


def test_two_people_with_same_given_name() -> None:
    """이름이 같은 두 사람: 전체 표기는 각자 토큰, 이름만 표기는 끈다 (code review H1)."""
    m = RunMapping()
    r = pseudonymize_text(
        "이름: 김가온\n\n\n\n\n\n\n서가온님과 함께 일했다. Seo Gaon 이 리뷰하고 Kim Gaon 이 배포했다. Gaon 이 정리.",
        m,
        D,
    )
    kim = next(t for t, vs in m.tokens.items() if "김가온" in vs)
    seo = next(t for t, vs in m.tokens.items() if "서가온" in vs)
    assert kim != seo
    assert f"{seo} 이 리뷰" in r.text and f"{kim} 이 배포" in r.text
    assert "Seo" not in r.text and "Kim" not in r.text
    assert "Gaon 이 정리" in r.text  # 누구인지 모르는 이름만 표기는 바꾸지 않는다


def test_promoted_value_replaced_in_earlier_document() -> None:
    """문서 A 에서 범위가 좁던 값이 문서 B 에서 전파 대상이 되면 A 의 다른 언급도 바꾼다 (code review M5)."""
    from task_spec.nodes.pseudonymize.engine import pseudonymize_documents

    m = RunMapping()
    a = "중앙 | 백엔드 개발자 | 2020.01 ~ 2021.01\n중앙 서버 구조를 바꿨다."
    b = "회사: 중앙\n지원 동기"
    ra, rb = pseudonymize_documents([a, b], m, D)
    assert "중앙" not in ra.text and "중앙" not in rb.text
    assert ra.residual == 0 and rb.residual == 0


def test_long_whitespace_line_is_fast() -> None:
    """라벨 값 뒤 긴 공백 — 정규식 제곱 시간이 없어야 한다 (code review M7)."""
    import time

    text = "이름: 가상" + " " * 20000 + "b"
    t0 = time.perf_counter()
    r = pseudonymize_text(text, RunMapping(), D)
    assert time.perf_counter() - t0 < 1.0
    assert "가상" not in r.text


def test_header_name_public_word_replaced_only_in_place_without_kiwi() -> None:
    """Kiwi 없이 머리줄의 이름 모양 공개어("경영진", "조직도")는 그 자리만 바꾼다 (재점검 M-A4)."""
    dn = Detectors(load_dictionaries(), use_kiwi=False)
    out = pseudonymize_text(
        "가상 데이터\n경영진 | 백엔드 개발자 | 2022.01 ~ 현재\n경영진 보고 자료를 만들었다.\n"
        + "\n" * 6
        + "조직도 | 백엔드 개발자 | 2022.01 ~ 현재\n조직도를 정리했다.",
        RunMapping(),
        dn,
    ).text
    assert "경영진 보고" in out and "조직도를 정리" in out


@pytest.mark.parametrize(
    "text",
    [
        '장애 대응 시스템 "장애 대응" 절차를 정리했다.',
        "로봇 '집기 동작' 시험을 반복했다.",
        "현실 | 백엔드 개발자 | 2020.01 ~ 2021.01",
    ],
)
def test_quoted_tool_and_role_mark_generic(text: str) -> None:
    """따옴표 안 일반 구절·'로봇'의 '봇'·'현실'의 '실'은 제품·팀이 아니다 (code review L1·L2)."""
    out = pseudonymize_text(text, RunMapping(), D).text
    assert "[PRODUCT" not in out
    if text.startswith("현실"):
        assert "현실" in out


# ---- 마지막 개인정보 점검 후속 (점검자 탐침을 지어낸 이름으로 옮김 — 실존 인물 아님) ----


@pytest.mark.parametrize(
    "tail",
    [
        "하람이 말했다.",
        "하람은 팀원이다.",
        "하람님과 협업했다.",
        "하람 씨가 리뷰했다.",
        "팀원 하람 소개.",
        "하람이는 성실하다.",
    ],
)
def test_given_name_alone_after_full_name_known(tail: str) -> None:
    """성+이름을 잡은 사람의 성 뺀 한글 이름도 같은 토큰으로 바꾼다 (마지막 점검 H1)."""
    m = RunMapping()
    r = pseudonymize_text(f"인적사항\n이름: 탁하람\n{tail}", m, D)
    assert "하람" not in r.text and r.residual == 0 and list(m.tokens) == ["[PERSON_1]"]


def test_given_name_alone_across_documents_shares_token() -> None:
    """문서를 묶어 처리하면 앞 문서의 이름만 표기도 같은 토큰 (점검 Low L1)."""
    from task_spec.nodes.pseudonymize.engine import pseudonymize_documents

    m = RunMapping()
    a, b = pseudonymize_documents(
        ["협업 소감: 하람이 말했다. 하람님과 함께", "인적사항\n이름: 탁하람\n"], m, D
    )
    assert "하람" not in a.text and "하람" not in b.text
    assert list(m.tokens) == ["[PERSON_1]"] and a.residual == 0 and b.residual == 0


def test_given_name_that_is_common_noun_only_with_title() -> None:
    """흔한 일반어인 이름("하늘")은 호칭·직함이 붙을 때만 바꾼다 — "하늘이 맑다"는 남긴다."""
    out = pseudonymize_text(
        "인적사항\n이름: 도하늘\n하늘이 맑은 날 배포했다. 하늘님과 협업했다. 팀원 하늘 소개.",
        RunMapping(),
        D,
    ).text
    assert "하늘이 맑은" in out and "하늘님" not in out and "팀원 하늘" not in out


def test_given_name_alone_off_when_shared() -> None:
    """이름이 같은 사람이 둘이면 성 뺀 이름만으로는 바꾸지 않는다 (누구인지 모른다)."""
    out = pseudonymize_text("이름: 탁하람\n동료: 국하람 팀원\n하람이 말했다.", RunMapping(), D).text
    assert "하람이 말했다" in out


@pytest.mark.parametrize("name", ["편소율", "변다래", "국하람", "편나린", "봉예솔", "길도율"])
@pytest.mark.parametrize(
    "frame", ["{n}은 팀원이다.", "팀원 {n}와 협업했다.", "{n} 담당 모듈을 인수했다."]
)
def test_name_shape_without_context(name: str, frame: str) -> None:
    """Kiwi 가 이름으로 보지 못하는 성+이름도 조사·호칭·담당 앞이면 바꾼다 (마지막 점검 H2)."""
    assert name not in pseudonymize_text(frame.format(n=name), RunMapping(), D).text


@pytest.mark.parametrize(
    "text",
    [
        "이용자는 늘었다.",
        "조직도를 정리했다.",
        "한국어로 작성했다.",
        "전문가와 협업했다.",
        "지원하는 기능을 넣었다.",
        "안정성을 높였다.",
        "어울리는 화면을 골랐다.",
        "이를 위해서는 캐시가 필요했다.",
        "사용성을 개선했다.",
        "기준서를 갱신했다.",
    ],
)
def test_name_shape_rule_keeps_common_words(text: str) -> None:
    assert pseudonymize_text(text, RunMapping(), D).text == text


@pytest.mark.parametrize("variant", ["gaon.seo", "seo.gaon", "seo_gaon_dev", "gaon-seo"])
def test_dot_and_underscore_forms_leave_no_surname(variant: str) -> None:
    """점·밑줄로 이은 로마자 표기도 성까지 바꾼다 (마지막 점검 M1)."""
    r = pseudonymize_text(f"인적사항\n이름: 서가온\n작성: {variant}\n", RunMapping(), D)
    assert "seo" not in r.text.casefold() and r.residual == 0


def test_surname_left_next_to_token_counted_as_residual() -> None:
    from task_spec.nodes.pseudonymize.engine import count_residual

    m = RunMapping()
    pseudonymize_text("이름: 서가온", m, D)
    assert count_residual("작성: [PERSON_1].seo", m, D) >= 1
    assert count_residual("작성: seo_[PERSON_1]", m, D) >= 1
    assert count_residual("작성: [PERSON_1]_dev", m, D) == 0


def test_header_value_propagation_counted_and_warned(caplog: pytest.LogCaptureFixture) -> None:
    """머리 줄 값이 본문에서 여러 번 바뀌면 counts 에 남기고, 5번 이상이면 경고한다 (토큰 id·건수만, 마지막 점검 M2)."""
    import logging

    body = "\n".join(f"- 다온에서 {i}번째 기능을 맡았다." for i in range(6))
    with caplog.at_level(logging.WARNING, logger="task_spec.nodes.pseudonymize.engine"):
        r = pseudonymize_text(
            f"다온 | 결제플랫폼팀 백엔드 개발자 | 2022.04 ~ 현재\n{body}", RunMapping(), D
        )
    assert r.counts.get("header_propagated") == 6
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("[ORG_1]" in s and "count=6" in s for s in msgs)
    assert not any("다온" in s for s in msgs)
