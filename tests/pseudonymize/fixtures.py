"""가상 지원서 픽스처 — 실존 인물·연락처가 아니다. 심어 둔 개인정보(PLANTED)로 재현율을 잰다.

이름은 '가상' 같은 단어를 넣어 실존 인물과 겹치지 않게 했다. 전화·이메일은 예약 대역/예시 도메인.
"""

from __future__ import annotations

RESUME = """가상 데이터 — 실존 인물 아님
성명: 김가상 (남, 27세)
생년월일: 1998.01.15
연락처: 010-0000-1234 / kim.gasang@example.com
주소: 서울특별시 가상구 가상로 12
GitHub: https://github.com/gasang-kim
블로그: gasang.tistory.com/42
학력: 가상대학교 컴퓨터공학과 졸업 (학점 3.9/4.5)
병역: 육군 병장 만기 전역

경력
삼성전자 DS부문 (2023.01 ~ 2024.12) 데이터 엔지니어
⟦담당업무⟧ 팀장 박가온님과 함께 Spark 기반 파이프라인을 설계했다.
⟦과정⟧ 정하늘 대리의 요청으로 Airflow DAG 를 재구성했다.
⟦기여한 점⟧ 집계 시간을 6시간에서 40분으로 줄였다.
백엔드 팀장, 마케팅 매니저와 협업했다.

교육
SSAFY 9기 수료 (2022.07 ~ 2022.12)
기술: Python, PyTorch, Kubernetes, Oracle DB, 파이썬, 노션
"""

COVER_LETTER = """가상 데이터 — 실존 인물 아님
삼성 전자에서 박가온 팀장님께 배운 것은 데이터 품질이었습니다.
서울대 대학원 진학도 고민했지만 현업을 택했습니다. 문의: 02-000-1234
"""

# (범주, 원래 값) — 결과에 원래 값이 남으면 놓친 것
PLANTED: list[tuple[str, str]] = [
    ("PERSON", "김가상"),
    ("PERSON", "박가온"),
    ("PERSON", "정하늘"),
    ("CONTACT", "010-0000-1234"),
    ("CONTACT", "kim.gasang@example.com"),
    ("CONTACT", "02-000-1234"),
    ("CONTACT", "가상로 12"),
    ("LINK", "github.com/gasang-kim"),
    ("LINK", "gasang.tistory.com"),
    ("SCHOOL", "가상대학교"),
    ("SCHOOL", "서울대"),
    ("ORG", "삼성전자"),
    ("ORG", "삼성 전자"),
    ("COURSE", "SSAFY"),
    ("REMOVE", "27세"),
    ("REMOVE", "1998.01.15"),
    ("REMOVE", "3.9/4.5"),
    ("REMOVE", "병장"),
]

# 바뀌면 안 되는 것 (과잉 치환 검사)
KEEP = [
    "Spark",
    "Airflow DAG",
    "Python",
    "PyTorch",
    "Kubernetes",
    "Oracle DB",
    "파이썬",
    "노션",
    "백엔드 팀장",
    "마케팅 매니저",
    "6시간에서 40분",
    "2023.01 ~ 2024.12",
    "2022.07 ~ 2022.12",
    "DS부문",
    "컴퓨터공학과",
    "⟦담당업무⟧",
]

# 두 번째 가상 지원서 — 처음에는 탐지기 개발 중 보지 않은 표본(held-out)으로 돌렸다.
# 그때 놓친 4건(본문 생년월일, 슬래시 성별, 라벨 없는 주소, 따옴표 서비스명)을 고친 뒤 회귀용으로 고정.
RESUME_B = """[가상 지원서 B — 실존 인물 아님]
지원자 최가윤 / 1997년 3월 2일생 / 여
E-mail : choi.gayun@example.org   H.P : 010 0000 5678
Portfolio: linkedin.com/in/gayun-choi
2019.03 – 2023.02  한빛가상대학교 산업공학과 (전공평점 4.21/4.5)
2023.03 – 현재     ㈜가상모빌리티 데이터 분석가
 · 윤가람 책임 연구원과 수요 예측 모델을 개발해 MAPE 를 18%에서 11%로 낮췄습니다.
 · 사내 서비스 '가상택시' 대시보드를 Tableau 로 구축했습니다.
 · 네이버 부스트캠프 AI Tech 5기 수료, 토스 인턴(2022.07~08)
자기소개: 저는 서강대 학생회장 출신으로, 김가온 교수님 연구실에서 학부연구생을 했습니다.
경기도 성남시 분당구 가상로 77 거주
"""

PLANTED_B: list[tuple[str, str]] = [
    ("PERSON", "최가윤"),
    ("PERSON", "윤가람"),
    ("PERSON", "김가온"),
    ("CONTACT", "choi.gayun@example.org"),
    ("CONTACT", "010 0000 5678"),
    ("CONTACT", "가상로 77"),
    ("LINK", "linkedin.com/in/gayun-choi"),
    ("SCHOOL", "한빛가상대학교"),
    ("SCHOOL", "서강대"),
    ("ORG", "가상모빌리티"),
    ("ORG", "토스"),
    ("PRODUCT", "가상택시"),
    ("COURSE", "부스트캠프"),
    ("REMOVE", "1997년 3월 2일생"),
    ("REMOVE", "/ 여"),
    ("REMOVE", "4.21/4.5"),
]
KEEP_B = [
    "MAPE 를 18%에서 11%로",
    "Tableau",
    "AI Tech 5기",
    "2019.03 – 2023.02",
    "산업공학과",
    "(2022.07~08)",
]
