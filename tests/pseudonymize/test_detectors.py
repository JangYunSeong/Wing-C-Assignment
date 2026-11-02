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
