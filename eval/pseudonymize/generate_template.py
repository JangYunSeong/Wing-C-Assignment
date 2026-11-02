"""가상 지원서 템플릿 생성기 — 심은 개인정보(planted)를 정답으로 함께 기록한다.

전부 가상 데이터: 이름은 성씨 × 이름 음절 조합, 전화는 010-0000-XXXX, 이메일은 example 도메인,
주소는 '가상로'. 회사·학교는 공개 사전 항목(사전 재현율)과 지어낸 이름(사전 밖 재현율)을 섞는다.

사용: uv run python -m eval.pseudonymize.generate_template --n 100 --seed 1 --split dev
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

OUT_DIR = Path(__file__).parent / "samples"

SURNAMES = [
    "김",
    "이",
    "박",
    "최",
    "정",
    "강",
    "조",
    "윤",
    "장",
    "임",
    "한",
    "오",
    "서",
    "신",
    "권",
    "황",
    "안",
    "송",
    "남궁",
    "선우",
]
GIVEN = [
    "가온",
    "하람",
    "다온",
    "라온",
    "시우",
    "서윤",
    "도윤",
    "하준",
    "지안",
    "예린",
    "수아",
    "민재",
    "태오",
    "이안",
    "채원",
    "가람",
    "나래",
    "보람",
    "새봄",
    "한결",
]
DICT_COMPANIES = [
    "삼성전자",
    "카카오뱅크",
    "네이버",
    "토스",
    "LG CNS",
    "SK하이닉스",
    "현대오토에버",
    "우아한형제들",
    "쿠팡",
    "KB국민은행",
]
INVENTED_COMPANIES = [
    ("주식회사 가상데이터", "가상데이터"),
    ("㈜누리가상", "누리가상"),
    ("가상솔루션(주)", "가상솔루션"),
]
BARE_INVENTED_COMPANIES = [
    "누리데이터랩",
    "한빛소프트웨어",
    "다온테크",
]  # 접미어·사전 없음 — 원래 못 잡는 경우
DICT_SCHOOLS = ["서울대학교", "연세대", "KAIST", "부산대학교", "한양대학교", "성균관대"]
INVENTED_SCHOOLS = ["한빛가상대학교", "가람대학교", "누리과학기술대학교"]
COURSES = ["SSAFY", "우아한테크코스", "부스트캠프", "42서울", "항해99"]
PRODUCTS = ["가상페이", "누리노트", "다온톡", "한결맵"]
MAJORS = ["컴퓨터공학과", "산업공학과", "통계학과", "경영학과", "전자공학과"]
TITLES = ["팀장", "책임", "선임", "매니저", "대리", "교수님", "멘토님"]
ROLES = ["데이터 엔지니어", "백엔드 개발자", "ML 엔지니어", "데이터 분석가", "마케팅 매니저"]
CITIES = [("서울특별시", "가상구"), ("경기도 성남시", "분당구"), ("부산광역시", "누리구")]
TECH = ["Python", "Spark", "Airflow", "Kubernetes", "PyTorch", "SQL", "Tableau", "Kafka"]
ACTIONS = [
    "{tech} 기반 배치 파이프라인을 설계했다",
    "{tech} 로 대시보드를 구축해 보고 시간을 {a}시간에서 {b}분으로 줄였다",
    "추천 모델을 개발해 클릭률을 {a}% 올렸다",
    "{tech} 클러스터 비용을 {a}% 절감했다",
]


@dataclass
class Sample:
    id: str
    split: str
    source: str
    layout: str
    text: str
    planted: list[dict[str, str]] = field(default_factory=list)  # {category, value}
    keep: list[str] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)  # 설계상 못 잡는 것으로 예상하는 값 (별도 집계)


def _person(r: random.Random) -> str:
    return r.choice(SURNAMES) + r.choice(GIVEN)


def _phone(r: random.Random, sep: str) -> str:
    return sep.join(["010", "0000", f"{r.randint(0, 9999):04d}"])


def _period(r: random.Random) -> str:
    y = r.randint(2018, 2023)
    return f"{y}.{r.randint(1, 12):02d} ~ {y + r.randint(1, 2)}.{r.randint(1, 12):02d}"


def build(r: random.Random, idx: int, split: str) -> Sample:
    name, colleague, prof = _person(r), _person(r), _person(r)
    while colleague == name:
        colleague = _person(r)
    phone = _phone(r, r.choice(["-", " ", ""]))
    email = f"user{idx}.{r.randint(10, 99)}@example.{r.choice(['com', 'org', 'net'])}"
    link = r.choice(
        [f"https://github.com/user{idx}", f"user{idx}.tistory.com", f"linkedin.com/in/user-{idx}"]
    )
    city, gu = r.choice(CITIES)
    road = f"가상로 {r.randint(1, 200)}"
    address = f"{city} {gu} {road}"
    school = r.choice(DICT_SCHOOLS + INVENTED_SCHOOLS)
    major = r.choice(MAJORS)
    comp_kind = r.random()
    if comp_kind < 0.55:
        company_text = company_value = r.choice(DICT_COMPANIES)
    elif comp_kind < 0.85:
        company_text, company_value = r.choice(INVENTED_COMPANIES)
    else:
        company_text = company_value = r.choice(BARE_INVENTED_COMPANIES)
    course = r.choice(COURSES)
    product = r.choice(PRODUCTS)
    title = r.choice(TITLES)
    role = r.choice(ROLES)
    age = r.randint(24, 34)
    birth = f"{2025 - age}.{r.randint(1, 12):02d}.{r.randint(1, 28):02d}"
    gpa = f"{r.choice(['3.5', '3.8', '4.1', '4.25'])}/4.5"
    tech = r.sample(TECH, 3)
    period = _period(r)
    acts = [
        a.format(tech=r.choice(tech), a=r.randint(2, 30), b=r.randint(10, 50))
        for a in r.sample(ACTIONS, 2)
    ]
    gender = r.choice(["남", "여"])

    layout = r.choice(["labeled", "header_slash", "english", "cover_prose"])
    planted: list[tuple[str, str]] = [("PERSON", name), ("PERSON", colleague)]
    hard: list[str] = []
    if layout == "labeled":
        text = (
            f"성명: {name} ({gender}, {age}세)\n생년월일: {birth}\n연락처: {phone}\n이메일: {email}\n"
            f"주소: {address}\n포트폴리오: {link}\n학력: {school} {major} 졸업 (학점 {gpa})\n\n"
            f"경력\n{company_text} ({period}) {role}\n"
            f"- {colleague} {title}과 함께 {acts[0]}.\n- {acts[1]}.\n"
            f"- 사내 서비스 '{product}' 개선에 참여했다.\n교육: {course} 수료\n기술: {', '.join(tech)}\n"
        )
        planted += [
            ("CONTACT", phone),
            ("CONTACT", email),
            ("CONTACT", road),
            ("LINK", link.split("//")[-1]),
            ("REMOVE", f"{age}세"),
            ("REMOVE", birth),
            ("REMOVE", gpa),
            ("PRODUCT", product),
        ]
    elif layout == "header_slash":
        text = (
            f"{name} / {birth[:4]}년 {int(birth[5:7])}월 {int(birth[8:10])}일생 / {gender}\n"
            f"H.P {phone}   E-mail {email}\n{link}\n"
            f"{period}  {school} {major}\n{period}  {company_text} {role}\n"
            f" · {colleague} {title}과 {acts[0]}.\n · {acts[1]}.\n"
            f" · {course} 수료. {city} {gu} {road} 거주\n"
        )
        planted += [
            ("CONTACT", phone),
            ("CONTACT", email),
            ("CONTACT", road),
            ("LINK", link.split("//")[-1]),
            ("REMOVE", f"{birth[:4]}년 {int(birth[5:7])}월"),
        ]
    elif layout == "english":
        text = (
            f"Name: {name}\nPhone: {phone}\nEmail: {email}\nGitHub: {link}\n"
            f"Education: {school} {major}\nExperience: {company_text} ({period}) — {role}\n"
            f"- Worked with {colleague} {title} on {tech[0]} pipelines.\n- {acts[1]}.\n"
        )
        planted += [("CONTACT", phone), ("CONTACT", email), ("LINK", link.split("//")[-1])]
    else:  # cover_prose — 라벨 없는 산문
        text = (
            f"안녕하십니까, {role} 직무에 지원한 {name}입니다. {school}에서 {major}를 전공하며 "
            f"{prof} 교수님 연구실에서 학부연구생으로 일했습니다. 이후 {company_text}에서 "
            f"{colleague} {title}과 함께 {acts[0]}. 특히 {acts[1]}. "
            f"{course}에서 배운 협업 방식을 실무에 적용했습니다. 연락은 {email} 로 부탁드립니다.\n"
        )
        planted += [("PERSON", prof), ("CONTACT", email)]
    planted += [("SCHOOL", school), ("ORG", company_value), ("COURSE", course)]
    if company_value in BARE_INVENTED_COMPANIES:
        hard.append(company_value)

    keep = [*tech, major, role] + ([period] if layout != "cover_prose" else [])
    return Sample(
        id=f"tpl-{split}-{idx:04d}",
        split=split,
        source="template",
        layout=layout,
        text="가상 데이터 — 실존 인물 아님\n" + text,
        planted=[{"category": c, "value": v} for c, v in dict.fromkeys(planted)],
        keep=keep,
        hard=hard,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    args = ap.parse_args()
    r = random.Random(args.seed)
    out = OUT_DIR / f"template_{args.split}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for i in range(args.n):
            f.write(json.dumps(asdict(build(r, i, args.split)), ensure_ascii=False) + "\n")
    print(f"{out}: {args.n} samples")


if __name__ == "__main__":
    main()
