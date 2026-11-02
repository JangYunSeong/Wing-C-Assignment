"""주장 추출 평가용 가상 지원 문서 — 가명화가 끝난 형태([ORG_1] 토큰, ⟦칸⟧ 표시)로 바로 만든다.

문장마다 정답을 안다:
- actions: 이 문장에 든 행동 수 (2 이면 '…하고 …했다' — 주장 2건으로 나눠야 함, 프롬프트 규칙 1)
- field / role: 칸과 규칙 판정 역할 (pipeline-graph "역할 규칙")
- actions=0: 뽑으면 안 되는 문장 (문제 정의 맥락, 지원 동기·다짐·성격 묘사)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# (문장, 행동 수, 설계 표현 여부)
ACTION_ONE = [
    ("Airflow DAG 를 재구성했다.", False),
    ("Spark 집계 잡을 파티션 단위로 나눴다.", False),
    ("추천 모델의 피처 저장소를 도입했다.", False),
    ("배치 장애 대응 매뉴얼을 작성했다.", False),
    ("데이터 품질 검증 규칙 40개를 만들었다.", False),
    ("신규 정산 파이프라인 아키텍처를 설계했다.", True),
    ("로그 수집 구조를 정의했다.", True),
    ("A/B 테스트 대시보드를 구축했다.", False),
]
ACTION_TWO = [
    "Kafka 토픽을 재설계하고 소비자 지연 알림을 추가했다.",
    "학습 파이프라인을 컨테이너화하고 배포 스크립트를 자동화했다.",
    "쿼리 실행 계획을 분석하고 인덱스를 다시 만들었다.",
    "라벨링 가이드를 정리하고 검수 절차를 도입했다.",
]
RESULTS = [
    "집계 시간을 {a}시간에서 {b}분으로 줄였다.",
    "월 클라우드 비용을 {a}% 절감했다.",
    "추천 클릭률을 {a}% 올렸다.",
]
CONTEXT = [  # 문제 정의 — 행동이 아님
    "주문 로그 집계가 하루 {a}시간 걸려 리포트가 늦었다.",
    "배치 실패가 주 {a}회 발생했다.",
]
NON_ACTION = [  # 자기소개서의 행동이 아닌 문장
    "저는 문제를 끝까지 파고드는 사람입니다.",
    "귀사에서 데이터로 고객 경험을 바꾸고 싶습니다.",
    "입사 후에는 팀의 데이터 문화를 함께 만들어 가겠습니다.",
    "협업에서 가장 중요한 것은 신뢰라고 생각합니다.",
]
PROSE_ACTIONS = [
    "[ORG_2] 인턴 때 고객 문의 데이터를 분류하는 스크립트를 만들었습니다.",
    "졸업 프로젝트에서 [PERSON_2] 와 함께 추천 서비스 프로토타입을 개발했습니다.",
    "동아리에서 매주 데이터 분석 스터디를 운영했습니다.",
]
ROLE = {"과정": "participate", "담당업무": "participate", "기여한 점": "lead"}


@dataclass
class GoldSentence:
    sid: str
    text: str
    actions: int
    field: str | None
    role: str | None


@dataclass
class Doc:
    id: str
    split: str
    kind: str
    text: str
    sentences: list[GoldSentence] = field(default_factory=list)


def _resume(r: random.Random, did: str, split: str) -> Doc:
    lines = ["가상 데이터 — 실존 인물 아님", "경력"]
    sents: list[GoldSentence] = []
    # 한 문서 안에서 같은 문장을 두 번 쓰지 않는다 (채점이 어느 문장인지 구분할 수 있게)
    one_pool = list(ACTION_ONE)
    two_pool = list(ACTION_TWO)
    r.shuffle(one_pool)
    r.shuffle(two_pool)

    def add(label: str | None, text: str, actions: int, design: bool = False) -> None:
        sid = f"{did}-s{len(sents) + 1}"
        role = None
        if label in ROLE and actions:
            role = "design" if design and label in ("기여한 점", "담당업무") else ROLE[label]
        sents.append(GoldSentence(sid, text, actions, label if actions or label else None, role))

    for p in range(r.randint(1, 2)):
        y = r.randint(2017, 2022)
        lines.append(
            f"[ORG_{p + 1}] 데이터 엔지니어 {y}.0{r.randint(1, 9)} ~ {y + 2}.0{r.randint(1, 9)}"
        )
        ctx = CONTEXT[p % len(CONTEXT)].format(a=r.randint(2, 9))
        lines.append(f"⟦문제 정의⟧ {ctx}")
        add("문제 정의", ctx, 0)
        # 과정: 한 행동 1~2 줄 + 두 행동 0~1 줄
        first = True
        for _ in range(r.randint(1, 2)):
            text, design = one_pool.pop()
            lines.append(f"⟦과정⟧ {text}" if first else text)
            first = False
            add("과정", text, 1, design)
        if r.random() < 0.6:
            two = two_pool.pop()
            lines.append(two)
            add("과정", two, 2)
        # 기여한 점: 결과 문장 + 가끔 설계 문장
        res = RESULTS[(p + r.randint(0, 2)) % len(RESULTS)].format(
            a=r.randint(5, 40) + 50 * p, b=r.randint(10, 50)
        )
        lines.append(f"⟦기여한 점⟧ {res}")
        add("기여한 점", res, 1)
        designs = [a for a in one_pool if a[1]]
        if designs and r.random() < 0.4:
            text, design = designs[0]
            one_pool.remove(designs[0])
            lines.append(text)
            add("기여한 점", text, 1, design)
        lines.append("")
    return Doc(did, split, "resume", "\n".join(lines) + "\n", sents)


def _cover(r: random.Random, did: str, split: str) -> Doc:
    parts: list[tuple[str, int]] = [(s, 0) for s in r.sample(NON_ACTION, 2)]
    parts += [(s, 1) for s in r.sample(PROSE_ACTIONS, 2)]
    r.shuffle(parts)
    sents = [GoldSentence(f"{did}-s{i + 1}", s, n, None, None) for i, (s, n) in enumerate(parts)]
    text = "가상 데이터 — 실존 인물 아님\n자기소개\n" + " ".join(s for s, _ in parts) + "\n"
    return Doc(did, split, "cover_letter", text, sents)


def docs(split: str, n: int) -> list[Doc]:
    r = random.Random({"dev": 301, "test": 302}[split])
    out = []
    for i in range(n):
        did = f"clm-{split}-{i:03d}"
        out.append(_resume(r, did, split) if i % 4 != 3 else _cover(r, did, split))
    return out
