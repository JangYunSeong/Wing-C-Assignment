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
