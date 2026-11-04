"""탐지기 단위 테스트 (Kiwi 없이 빠르게)."""

from __future__ import annotations

import pytest

from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors, RunMapping, pseudonymize_text

D = Detectors(load_dictionaries(), use_kiwi=False)


def run(text: str) -> str:
    return pseudonymize_text(text, RunMapping(), D).text


@pytest.mark.parametrize(
    ("text", "gone", "token"),
    [
        ("메일 a.b+c@example.co.kr 로", "a.b+c@example.co.kr", "[CONTACT_1]"),
        ("휴대폰 010-0000-1234", "010-0000-1234", "[CONTACT_1]"),
        ("휴대폰 01000001234", "01000001234", "[CONTACT_1]"),
        ("+82 10-0000-1234 로 연락", "10-0000-1234", "[CONTACT_1]"),
        ("사무실 02-000-1234", "02-000-1234", "[CONTACT_1]"),
        ("포트폴리오 https://example.com/me?x=1 참고", "example.com/me", "[LINK_1]"),
        ("github.com/someone/repo 참고", "github.com/someone", "[LINK_1]"),
        ("someone.github.io 블로그", "someone.github.io", "[LINK_1]"),
        ("번호 000000-3000000", "000000-3000000", "[CONTACT_1]"),
        ("성명: 홍가상", "홍가상", "[PERSON_1]"),
        ("이름 | 오가람", "오가람", "[PERSON_1]"),
        ("남궁가상 선배님과", "남궁가상", "[PERSON_1]"),
        ("김가온 님과 회의", "김가온", "[PERSON_1]"),
        ("멘토 이가람과 함께", "이가람", "[PERSON_1]"),
        ("가상대학교 졸업", "가상대학교", "[SCHOOL_1]"),
        ("연세대에서", "연세대", "[SCHOOL_1]"),
        ("주식회사 가상랩스 인턴", "가상랩스", "[ORG_1]"),
        ("가상테크(주) 근무", "가상테크", "[ORG_1]"),
        ("카카오뱅크에서 일했다", "카카오뱅크", "[ORG_1]"),
        ("우테코 6기", "우테코", "[COURSE_1]"),
        ("서비스명: 가상페이", "가상페이", "[PRODUCT_1]"),
        ("사내 앱 '가상노트' 출시", "가상노트", "[PRODUCT_1]"),
        ("서울시 강남구 가상로 123 거주", "가상로 123", "[CONTACT_1]"),
        ("성남시 분당구 정자동 12-3", "정자동 12-3", "[CONTACT_1]"),
    ],
)
def test_replaced(text: str, gone: str, token: str) -> None:
    out = run(text)
    assert gone not in out
    assert token in out


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("만 27세 지원자", "27세"),
        ("(여, 25세) 지원", "25세"),
        ("98년생", "98년생"),
        ("군필", "군필"),
        ("공군 병장 만기 전역", "전역"),
        ("생년월일: 1999.12.31", "1999.12.31"),
        ("성별: 여성", "여성"),
        ("(학점 4.0/4.5)", "4.0/4.5"),
        ("(전공평점 4.21/4.5)", "4.21/4.5"),
        ("1997년 3월 2일생", "1997년"),
        ("1997.03.02 생", "1997.03.02"),
        ("지원자 / 여 / 서울", "여"),
    ],
)
def test_demographics_removed_without_token(text: str, gone: str) -> None:
    out = run(text)
    assert gone not in out
    assert "[" not in out  # 토큰 없이 삭제


@pytest.mark.parametrize(
    "text",
    [
        "Python, Spark, Kubernetes 로 파이프라인을 만들었다.",
        "Oracle DB 튜닝과 구글 클라우드 운영",  # 맥락 없는 '!' 사전 항목은 그대로
        "카카오톡 알림 기능 개발",  # 카카오 ≠ 카카오톡
        "이대로 두면 장애가 난다",  # 짧은 별칭 오탐 없음
        "백엔드 팀장, 마케팅 매니저, 기획팀 팀장과 협업",
        "2023.01 ~ 2024.12, 처리량 3,000 TPS, 정확도 0.92/1.0",
        "서비스 매니저와 정기 회의",
        "20세기 소설",
        "추천 연구 파이프라인으로 3배 빨라졌다",  # '~구 ~로 숫자' 주소 오탐 없음
        "도구 자동화로 2시간 단축",
        "모델 연구 도구 개발로 3건 특허",
    ],
)
def test_no_over_replacement(text: str) -> None:
    assert run(text) == text


def test_context_required_entry_replaced_with_employment_context() -> None:
    out = run("Google (2021.03 ~ 2023.02) 소프트웨어 엔지니어")
    assert "Google" not in out and "[ORG_1]" in out


def test_newlines_preserved_around_removed_lines() -> None:
    out = run("학점: 3.9/4.5\nGPA: 4.1/4.5\n다음 줄")
    assert out.endswith("\n다음 줄")


# ---- 2026-11-04 탐지 보강 (가상 데이터) ----


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("배포: gruswap.vercel.app", "gruswap.vercel.app"),
        ("데모는 gru-eats.netlify.app 에서", "gru-eats.netlify.app"),
        ("API 서버 grumap-api.herokuapp.com 운영", "grumap-api.herokuapp.com"),
        ("문서 사이트 gruboard.pages.dev", "gruboard.pages.dev"),
        ("GitHub: gru-eats", "gru-eats"),
        ("깃허브: gruswap-dev 참고", "gruswap-dev"),
        ("GitLab: @hrmn-lab / 블로그", "hrmn-lab"),
        ("GitHub: gru-eats.", "gru-eats"),  # 문장 끝 마침표 (privacy-auditor M3)
        ("api.gruswap.vercel.app 배포", "api.gruswap.vercel.app"),  # 다단계 서브도메인
        ("데모 gruswap.vercel.app.", "gruswap.vercel.app"),
    ],
)
def test_schemeless_deploy_links_and_labeled_handles(text: str, gone: str) -> None:
    out = run(text)
    assert gone not in out and "[LINK_1]" in out


@pytest.mark.parametrize(
    "text",
    [
        "트랜잭션은 @Transactional 로 묶었다",  # 맨 @ 는 코드 애노테이션 — 라벨 뒤만 핸들
        "GitHub Actions 로 CI 를 구성했다",
        "GitHub: Actions로 배포를 자동화했다",  # 핸들 뒤에 한글이 붙으면 문장
        "Apache 하이브 쿼리를 튜닝했다",  # 하이브(회사)는 재직 맥락에서만
        "카카오톡 챗봇과 KakaoTalk 알림, 카카오생활 앱",  # 붙은 표기 허용 목록 밖
        "self.web.application.run",  # 도메인 뒤 우측 경계 (privacy-auditor M3)
        "config.web.app_name = 1",
        "x.fly.developer",
        "GitHub: Python 3.11",  # 라벨 뒤라도 버전·파일명·흔한 단어는 계정이 아님 (2차 W4)
        "GitHub: main.py",
        "GitHub: v1.2.3",
        "GitHub: Issues",
        "Github: README 참고",
    ],
)
def test_no_over_replacement_after_detector_additions(text: str) -> None:
    assert run(text) == text


@pytest.mark.parametrize(
    ("text", "gone", "kept"),
    [
        ("서울대생 200명 설문", "서울대", "생 200명"),
        ("2022.01 ~ 2023.06 삼성전자DS 부문 인턴", "삼성전자", "DS 부문"),
        ("개인 프로젝트 TossClone 으로", "Toss", "Clone"),
        ("네이버클라우드 인턴", "네이버", "클라우드"),
    ],
)
def test_dictionary_entity_with_attached_suffix(text: str, gone: str, kept: str) -> None:
    out = run(text)
    assert gone not in out and kept in out


def test_hive_company_needs_employment_context() -> None:
    assert "하이브" not in run("하이브 (2021.03 ~ 2022.02) 데이터 엔지니어")


@pytest.mark.parametrize(
    ("text", "gone"),
    [("윤도겸 팀원이 로그 수집을 맡았다", "윤도겸"), ("표서진 동료와 페어 프로그래밍", "표서진")],
)
def test_teammate_titles(text: str, gone: str) -> None:
    assert gone not in run(text)


@pytest.mark.parametrize(
    "text",
    [
        "학생 정보를 관리하는 시스템을 만들었다",  # 이름 앞 호칭으로는 넣지 않음
        "팀원 성과를 공유했다",
        "장학금 학생 선발 기준을 정리했다",  # '학생' 은 이름 뒤 호칭으로도 넣지 않음
        "주니어 팀원 교육 자료를 만들었다",
        "함께한 동료가 많다",  # 관형형 + 팀원·동료 (2차 W3)
        "성실한 팀원과 일했다",
        "새로 합류한 팀원이 늘었다",
        "배정된 팀원이 바뀌었다",
    ],
)
def test_teammate_titles_do_not_catch_nouns(text: str) -> None:
    assert run(text) == text


@pytest.mark.parametrize("name", ["김지한", "정우한", "최가난", "윤도된", "오지던"])
@pytest.mark.parametrize("pattern", ["{n} 팀원이 서버를 맡았다.", "{n} 동료와 함께 배포했다."])
def test_peer_titles_keep_names_ending_like_modifiers(name: str, pattern: str) -> None:
    """관형형은 끝음절이 아니라 단어 목록으로 거른다 — 실제 이름 끝음절(한·난·된·던)과 겹치지 않게 (2차 W3 후속)."""
    assert name not in run(pattern.format(n=name))


# ---- 2026-11-04 2차: 가상 이력서에서 찾은 빈틈 (가상 데이터 — 실존 인물·회사 아님) ----


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("가상 데이터\n변가온 — 데이터 엔지니어\n", "변가온"),
        ("가상 데이터\n도가람 - Backend Developer\n", "도가람"),
        ("가상 데이터\n모가상 – ML 엔지니어 (3년차)\n", "모가상"),
        ("가상 데이터\n추가온\t백엔드 개발자\n", "추가온"),
        ("가상 데이터\n여가람    프론트엔드 개발자\n", "여가람"),
        ("가상 데이터\n남궁 가온 — 클라우드 엔지니어\n", "남궁 가온"),
    ],
)
def test_header_name_followed_by_title(text: str, gone: str) -> None:
    assert gone not in run(text)


def test_header_label_word_is_not_a_name() -> None:
    """머리줄 구분자에 '-'·탭을 넣은 뒤 "연락처 - 010…" 의 '연락처' 를 이름으로 잡지 않는다."""
    out = run("가상 데이터\n연락처 - 010-0000-1111\n연락처\t010-0000-2222\n")
    assert out.count("연락처") == 2 and "[PERSON" not in out


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        (
            "이름\t탁가상\n연락처\t010-0000-1618\n학교\t가람대학교 산업공학과",
            ["탁가상", "010-0000-1618", "가람대학교"],
        ),
        (
            "성명\t생년월일\t연락처\n표가온\t1999.04.12\t010-0000-1414",
            ["표가온", "1999.04.12", "010-0000-1414"],
        ),
        ("| 성명 | 생년월일 |\n|---|---|\n| 길가람 | 2000.02.02 |", ["길가람", "2000.02.02"]),
        (
            "이름: 반가상\t생년월일: 1998.07.07\t연락처: 010-0000-7070",
            ["반가상", "1998.07.07", "010-0000-7070"],
        ),
        ("이름    연가온\n학교    다솜대학교", ["연가온", "다솜대학교"]),
        ("Name\tKang Gaon\nEmail\tgaon.k@example.com", ["Kang Gaon", "gaon.k@example.com"]),
        ("회사\t가상정보기술\n직무\t데이터 엔지니어", ["가상정보기술"]),
    ],
)
def test_table_label_cells(text: str, gone: list[str]) -> None:
    out = run(text)
    assert [g for g in gone if g in out] == []


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (
            "| 기간 | 학교 | 전공 | 학위 |\n| 2020.03 ~ 2024.02 | 가람대학교 | 컴퓨터공학 | 학사 |",
            ["전공", "2020.03 ~ 2024.02", "컴퓨터공학"],
        ),
        ("학력\t2020.03 ~ 2024.02\t가람대학교 졸업", ["2020.03 ~ 2024.02"]),
        ("회사\t가상정보기술\n기간\t2021.03 ~ 2023.08", ["2021.03 ~ 2023.08"]),
        ("이메일\t발송 서버 개발\t2023.01 ~ 2023.03", ["발송 서버 개발", "2023.01 ~ 2023.03"]),
    ],
)
def test_table_cells_keep_headers_periods_and_non_contacts(text: str, kept: list[str]) -> None:
    """열 이름·기간·연락처 모양이 아닌 값은 표 칸 규칙으로 치환하지 않는다 (값은 run 전체로 전파된다)."""
    out = run(text)
    assert [k for k in kept if k not in out] == []


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("프로젝트에서 선우 가온 팀원과 결제 API를 만들었다.", "선우 가온"),
        ("제갈 람 책임의 리뷰를 받았다.", "제갈 람"),
        ("독고 온 님과 함께 대시보드를 만들었다.", "독고 온"),
        ("사공 람가 데이터 정제를, 남궁 별이 시각화를 맡았다.", "남궁 별"),
        ("사공 람이 데이터 정제를 맡았다.", "사공 람"),
        ("프로젝트 리드는 황보가온이었다.", "황보가온"),
    ],
)
def test_compound_surnames_spaced_or_attached(text: str, gone: str) -> None:
    assert gone not in run(text)


@pytest.mark.parametrize(
    "text", ["보고서 서문 작성과 목차 정리를 맡았다.", "뱃사공이 많으면 배가 산으로 간다."]
)
def test_compound_surname_rule_keeps_common_words(text: str) -> None:
    assert run(text) == text


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("가상 데이터\nByun Gaon (변가온) — Data Engineer\n", ["Byun Gaon", "변가온"]),
        ("가상 데이터\nGARAM SEOK\nSoftware Engineer\n", ["GARAM SEOK"]),
        ("가상 데이터\nPark Gaon | Product Designer\n", ["Park Gaon"]),
        ("가상 데이터\n\n\n\n\n\n\n협업: Gaon Ahn, gaon.ahn@example.com\n", ["Gaon Ahn"]),
        ("영문 이름: Gaon Yeom\n", ["Gaon Yeom"]),
        ("가상 데이터\n도가람 (Do Garam)\n", ["도가람", "Do Garam"]),
    ],
)
def test_english_names_in_context(text: str, gone: list[str]) -> None:
    out = run(text)
    assert [g for g in gone if g in out] == []


@pytest.mark.parametrize(
    "text",
    [
        "가상 데이터\nData Scientist / 6년\nMachine Learning Engineer\n",
        "가상 데이터\nGo Developer | 2022.03 ~ 현재\n",
        "가상 데이터\nSUMMARY\nRESEARCH ENGINEER\n",
        "Spring Boot, Apache Kafka 기반 Song Recommendation 모델 개선",
        "원가 (Cost Table) 산정 기준 정리",
    ],
)
def test_english_name_rules_keep_job_and_tech_words(text: str) -> None:
    assert run(text) == text


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("누리저축은행 앱", "누리저축은행"),  # 강한 접미어 — Kiwi 없이도
        ("졸업 후 누리게임즈에서 3년간 근무", "누리게임즈"),
        ("누리랩스에서 인턴으로 근무", "누리랩스"),
        ("가람네트웍스 / 인프라팀", "가람네트웍스"),
        ("누리별정보기술 인프라운영팀 (2023.03 ~ 현재)", "누리별정보기술"),
        ("가온케어랩 | 결제개발팀 백엔드 개발자    2021.04 ~ 현재", "가온케어랩"),
        ("2020.03 ~ 2021.02  다온헬스 / 인프라팀 / 시스템 엔지니어", "다온헬스"),
        ("Gaon Labs · Platform Team · Backend Engineer    2021.01 ~ 2022.12", "Gaon Labs"),
        ("가상AI랩 | 음성모델팀 | 2021.05 ~ 현재", "가상AI랩"),
    ],
)
def test_org_without_dictionary(text: str, gone: str) -> None:
    assert gone not in run(text)


@pytest.mark.parametrize(
    "text",
    [
        "인터넷은행 앱의 카드 결제 화면과 신용카드 등록 흐름을 개선했다.",
        "오픈마켓 셀러 데이터와 이커머스 주문 로그, 소셜커머스 쿠폰 데이터를 통합했다.",
        "핀테크·에듀테크·헬스케어 도메인의 보안솔루션 시장을 조사했다.",
        "대형 은행과 증권사, 카드사 고객 데이터를 다뤘다.",
        "결제 시스템 개편 | 백엔드 리드 | 2023.01 ~ 2023.06",
        "추천엔진 | 개인 프로젝트 | 2022.07 ~ 2022.09",
        "데이터플랫폼팀 | 백엔드 개발자 | 2021.01 ~ 2022.12",
        "스킬 | 언어: Go, Java | 도구: Docker",
        "2) 스터디 공간 예약 웹 (2024.03 ~ 2024.06, 3인 팀)",
        "미래모빌리티 연구와 4대보험 처리",
    ],
)
def test_org_rules_keep_generic_compounds_and_project_lines(text: str) -> None:
    assert run(text) == text


def test_org_rule_prefers_dictionary_entity() -> None:
    """사전 실체가 들어 있는 후보는 사전 규칙이 같은 토큰으로 묶는다 (재직 문맥 규칙이 통째로 잡지 않는다)."""
    out = run("네이버클라우드 인턴 (2023.07 ~ 2023.08)")
    assert out.startswith("[ORG_1]클라우드")


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        (
            "교육\n- 가람 데이터 분석가 양성 과정 수료 (2022.01 ~ 2022.06)",
            "가람 데이터 분석가 양성 과정",
        ),
        ("누리 클라우드 부트캠프 3기 수료", "누리 클라우드 부트캠프"),
        ("2021.07 ~ 2021.12 가온 AI 아카데미 이수", "가온 AI 아카데미"),
        ('사내 디자인 시스템 "Gaonmark" 구축·운영', "Gaonmark"),
        ("사내 메신저 '가상톡' 알림 서버를 개발했다.", "가상톡"),
        ("자체 개발한 챗봇 「가람봇」의 응답 로직을 개선했다.", "가람봇"),
        ("소담과학기술원 전산학 석사", "소담과학기술원"),
    ],
)
def test_courses_products_and_institute_schools(text: str, gone: str) -> None:
    assert gone not in run(text)


@pytest.mark.parametrize(
    "text",
    [
        "인재 양성 과정을 설계하는 HR 데이터 분석을 지원했다.",
        "부트캠프에서 배운 내용을 정리했다.",
        "'로그인' 기능과 \"좋아요\" 버튼을 구현했다.",
        "구체적인 교육훈련 과정을 수료 기준에 맞췄다.",
    ],
)
def test_course_and_product_rules_keep_generic_phrases(text: str) -> None:
    assert run(text) == text


def test_course_rule_prefers_dictionary_course() -> None:
    out = run("항해99 부트캠프 수료 (2022.01 ~ 2022.03)")
    assert out.startswith("[COURSE_1] 부트캠프")


_TECH_PRODUCTS = [
    "Oracle Data Guard",
    "Data Guard",
    "Spring Cloud Gateway",
    "Apache Kafka Streams",
    "Power BI",
    "Visual Studio Code",
    "Looker Studio",
    "React Native",
    "Go Lang",
    "Go Kit",
    "Jin Air",
    "Moon Landing",
]


@pytest.mark.parametrize("product", _TECH_PRODUCTS)
@pytest.mark.parametrize(
    "frame",
    [
        "가상 데이터\n{p}\n백엔드 개발\n",  # 머리줄 첫머리
        "가상 데이터\n{p} | 운영 자동화\n",
        "가상 데이터\n\n\n\n\n\n\n- {p} 기반 배포 파이프라인 구축\n",
        "가상 데이터\n\n\n\n\n\n\n배포 도구 ({p}) 문서 정리\n",  # 한글 일반어 + 괄호
        "가상 데이터\n\n\n\n\n\n\n문의 data.guard@example.com — {p} 운영\n",  # 이메일 앞부분과 겹침
    ],
)
def test_english_name_rules_keep_tech_product_names(product: str, frame: str) -> None:
    """영문 기술·제품명 여러 단어를 사람 이름으로 치환하지 않는다 (coordinator 요청, 2차)."""
    assert product in run(frame.format(p=product))


# ---- 2026-11-04 2차 privacy-auditor 후속 (가상 데이터 — 실존 인물·회사 아님) ----


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("김온 (Kim On) 담당", ["김온", "Kim On"]),  # 두 음절 한글 이름 + 로마자 표기
        ("가상 데이터\nPark On\n이름: 박온\n", ["Park On", "박온"]),
        ("가상 데이터\nGa On Kim\n백엔드\n", ["Ga On Kim"]),
        ("가상 데이터\nGaon Han Data Engineer\n", ["Gaon Han"]),
        (
            "Seo Gaon (서가온) — Backend\n\n\n\n\n\n\n프로젝트에서 Gaon 이 일했다.\n",
            ["Seo Gaon", "서가온", "Gaon"],
        ),
    ],
)
def test_english_name_leaks_from_audit(text: str, gone: list[str]) -> None:
    out = run(text)
    assert [g for g in gone if g in out] == []


def test_romanized_variants_of_korean_name_replaced_with_same_token() -> None:
    m = RunMapping()
    out = pseudonymize_text(
        "이름: 서가온\n\n\n\n\n\n\n작성: Seo Gaon / GAON SEO / Ga-on Seo / Suh, Gaon", m, D
    ).text
    assert "Gaon" not in out and "GAON" not in out and "Ga-on" not in out
    assert list(m.tokens) == ["[PERSON_1]"]


def test_given_only_alias_skips_common_english_words() -> None:
    """이름만 로마자 변형이 흔한 영어 단어와 같으면 따로 바꾸지 않는다 (성+이름 전체 표기는 바꾼다)."""
    out = run("이름: 김다타\n\n\n\n\n\n\ndata pipeline 구축, Kim Data 작성")
    assert "data pipeline" in out and "Kim Data" not in out


@pytest.mark.parametrize(
    "text",
    ["Seo Gaon (서가온)\n서가온은 백엔드", "서가온 (Daniel Seo) 담당. Daniel 이 리뷰."],
)
def test_korean_and_english_forms_share_one_token(text: str) -> None:
    m = RunMapping()
    out = pseudonymize_text(text, m, D).text
    assert list(m.tokens) == ["[PERSON_1]"] and "서가온" not in out


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("가람국민카드 인턴 근무", "가람국민카드"),
        ("누리투자저축은행에서 3년간 근무했다.", "누리투자저축은행"),
        ("다온기업은행 인턴 근무", "다온기업은행"),
        ("소담모바일게임즈에서 2년간 근무했다.", "소담모바일게임즈"),
        ("미르금융투자 인턴 근무", "미르금융투자"),
        ("가상랩 | 플랫폼팀 | 2022.04 ~ 현재", "가상랩"),
    ],
)
def test_org_stem_list_does_not_hide_company_like_names(text: str, gone: str) -> None:
    """일반어 어간 목록은 어간 전체가 일반어일 때만 뺀다 (끝말 비교 안 함, privacy-auditor 2차 H2)."""
    assert gone not in run(text)


@pytest.mark.parametrize(
    "text",
    [
        "2022.01 ~ 2023.03 | Python개발자 | 팀 리드",
        "ML엔지니어 | 2022.01 ~ 2023.01 | 리드",
        "Spring Boot | 2022.01 ~ 2023.01 | 개발자",
    ],
)
def test_org_header_skips_job_titles_and_tech(text: str) -> None:
    assert run(text) == text


def test_compound_surname_with_particle_is_not_a_name() -> None:
    assert run("사공 이 있다") == "사공 이 있다"


def test_compound_surname_spaced_and_attached_share_token() -> None:
    m = RunMapping()
    pseudonymize_text("남궁 휘와 일했다. 남궁휘는 리뷰를 맡았다.", m, D)
    assert list(m.tokens) == ["[PERSON_1]"]


def test_name_equal_to_table_header_word_next_to_name_label() -> None:
    """이름 라벨 옆이면 열 이름과 같은 값도 그 칸만 치환하고(전파 없음), 다른 곳의 같은 말은 남긴다."""
    out = run("이름\t성과\n연락처\t010-0000-0000\n성과: 응답 속도 개선")
    assert out.startswith("이름\t[PERSON_1]") and "성과: 응답 속도 개선" in out


def test_name_in_org_header_slot_without_kiwi() -> None:
    """Kiwi 없이도 경력 머리말 첫 칸의 이름 모양 값은 그 칸에서 지운다."""
    assert "윤가온" not in run("\n" * 7 + "윤가온 | 백엔드 개발자 | 2022.01 ~ 현재")


@pytest.mark.parametrize(
    "line",
    [
        "이름: a" + " " * 20000 + "b",
        " " * 20000 + "x",
        "\t" * 20000 + "x",
        "." * 20000,
        "-" * 20000,
        "a@" * 10000,
    ],
    ids=["label_spaces", "spaces", "tabs", "dots", "dashes", "at"],
)
def test_detector_regexes_are_linear_on_long_lines(line: str) -> None:
    """긴 공백·점·하이픈 줄에서 정규식이 제곱 시간이 걸리지 않는다 (code review M7). Kiwi 를 빼고 정규식만 본다."""
    import time

    t0 = time.perf_counter()
    run(line)
    assert time.perf_counter() - t0 < 1.0
