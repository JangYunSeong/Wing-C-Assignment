"""채용담당자 사례 인터뷰 테스트 픽스처 (#262). 가상 데이터만 (불변식 1) — 포크(9ef2f50) 테스트의 사실·답안을 옮겼다."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from task_spec.api.app import create_app
from task_spec.interview.service import InterviewService
from task_spec.interview.store import MemoryInterviewStore
from task_spec.llm import LLMClient
from task_spec.llm.backend import Completion, TextCallback
from task_spec.llm.fake import FakeBackend
from task_spec.nodes.pseudonymize import InterviewPseudonymizer
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.prompting import Message, PromptRegistry
from task_spec.schemas import PostingProfile, PublicIncident
from task_spec.schemas.interview_answer import InterviewAnswerCheckOutput, InterviewAnswerOutput
from task_spec.schemas.interview_incident import IncidentInterviewOutput
from tests.api.conftest import make_settings

REPO = Path(__file__).resolve().parents[2]
PROMPTS = ("interview_incident_extended", "interview_answer", "interview_answer_check")
POSTING_ID = "prov-0123456789ab"
TEAM_LINES = [
    ("팀 맥락", "90일 기대 성과: 상태 기록 API 의 중복 저장 0건"),
    ("팀 맥락", "판단 상황과 기준: 재시도 정책을 바꿀 때 복구 가능성을 먼저 본다"),
]
JD_LINES = [
    ("회사 소개", "가상테크 물류팀은 배송 상태를 기록하는 서비스를 운영한다"),
    ("담당업무", "가상 배송 서비스의 상태 기록 API 개발"),
    ("자격요건", "REST API 설계 경험"),
]

FACTS = {
    "symptom": "재시도 때 중복 저장",
    "root_cause": "요청 식별자 검증 누락을 코드에서 확인",
    "accepted_fixes": "요청 식별자를 검증해 중복 저장이 사라짐",
    "naive_fix": "재시도 금지",
    "why_naive_fails": "통신 실패 복구를 막음",
    "conditions": "응답 유실 뒤 재시도 시",
}
PROJECT = "[개요]\n가상 배송 서비스\n[기능 명세]\n상태 기록 API\n[알려진 문제]\n" + "\n".join(
    FACTS.values()
)
SUCCESS = {
    "reproduction": "응답 유실 뒤 같은 요청을 재시도한다",
    "expected_result": "상태가 중복 저장되지 않아야 한다",
    "invariants": "통신 실패 뒤에도 정상 복구할 수 있어야 한다",
    "verification": "같은 요청을 재시도하고 저장 결과를 대조한다",
    "alternatives": "중복 저장을 막고 복구를 유지하는 방법을 인정한다",
}


def case() -> PublicIncident:
    """모델이 돌려준 사례 — 출처·조직·검증 상태는 서버가 덮어쓴다."""
    return PublicIncident(
        incident_id="pub:sw.backend:interview-draft",
        sub="sw.backend",
        title="중복 저장 사례",
        org="잘못된 조직",
        url="https://invalid.example",
        published="2020",
        source_lang="ko",
        verified=True,
        symptom=FACTS["symptom"],
        root_cause=FACTS["root_cause"],
        naive_fix=FACTS["naive_fix"],
        why_naive_fails=FACTS["why_naive_fails"],
        key_insight="요청 식별자의 검증 여부",
        deep_structure="동일 요청을 다시 처리하지 않는다",
        accepted_fixes=[FACTS["accepted_fixes"]],
        tech=[],
    )


def complete_output(**updates: Any) -> IncidentInterviewOutput:
    return IncidentInterviewOutput(
        **{
            "incident": case(),
            "conditions": FACTS["conditions"],
            "evidence": {k: [v] for k, v in FACTS.items()},
            **updates,
        }
    )


def answer_output(**updates: Any) -> InterviewAnswerOutput:
    return InterviewAnswerOutput(
        **{
            "answer_text": "[재현 조건] 응답 유실 뒤 재시도\n[성공 기준] 상태가 중복 저장되지 않아야 한다\n"
            "[지켜야 할 조건] 통신 실패 뒤에도 정상 복구할 수 있어야 한다\n"
            "[확인 방법] 같은 요청을 재시도하고 저장 결과를 대조한다\n"
            "[인정할 해법과 조건] 중복 저장을 막고 복구를 유지하는 방법을 인정한다",
            "trap_answer_text": "[실패한 판단] 재시도 금지\n[실패를 가르는 근거] 통신 실패 복구를 막음\n"
            "[올바른 판단 기준] 중복 저장을 막으면서 복구를 유지한다",
            "evidence": [SUCCESS["expected_result"], FACTS["root_cause"]],
            **updates,
        }
    )


def check_output(
    *, answer: InterviewAnswerOutput | None = None, trap: bool = True, updates: dict | None = None
) -> InterviewAnswerCheckOutput:
    answer = answer or answer_output()
    texts = [(False, answer.answer_text), *([(True, answer.trap_answer_text)] if trap else [])]
    return InterviewAnswerCheckOutput.model_validate(
        {
            "claims": [
                {
                    "text": line,
                    "is_trap": is_trap,
                    "status": "pass",
                    "evidence": [SUCCESS["expected_result"]],
                    "reason": "담당자 입력과 조건을 대조함",
                }
                for is_trap, text in texts
                for line in text.splitlines()
                if line.strip()
                and not (line.strip().startswith("[") and line.strip().endswith("]"))
            ],
            "checks": [
                {
                    "topic": topic,
                    "status": "pass",
                    "reason": "원문 요구와 답안의 판정 조건이 일치함",
                }
                for topic in (
                    "grounding",
                    "causality",
                    "correct_solution",
                    "alternatives",
                    "trap_discrimination",
                    "verification",
                )
            ],
            "probes": [
                {"probe_id": "p1", "verdict": "accept", "reason": "성공 조건을 만족함"},
                *(
                    [
                        {
                            "probe_id": "p2",
                            "verdict": "reject",
                            "reason": "실패 관찰과 복구 요구 위반",
                        }
                    ]
                    if trap
                    else []
                ),
            ],
            "questions": [],
            **(updates or {}),
        }
    )


def prompts_with_current(tmp: Path) -> PromptRegistry:
    """인터뷰 프롬프트 판 파일 + CURRENT (레포에는 평가 전이라 CURRENT 가 없다)."""
    root = tmp / "prompts"
    for task in PROMPTS:
        src, dst = REPO / "prompts" / task, root / task
        dst.mkdir(parents=True)
        versions = sorted(src.glob("v*.md"))
        for f in versions:
            shutil.copy(f, dst / f.name)
        (dst / "CURRENT").write_text(f"v{max(int(f.stem[1:]) for f in versions)}\n")
    return PromptRegistry(root)


class CapturingBackend(FakeBackend):
    """가짜 LLM + 받은 본문 기록 — 가명화된 글만 가는지 확인용 (가상 데이터)."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def complete(
        self,
        model: str,
        messages: list[Message],
        params: dict[str, Any],
        *,
        on_delta: TextCallback | None = None,
    ) -> Completion:
        self.prompts.append("\n".join(m.content for m in messages))
        return await super().complete(model, messages, params, on_delta=on_delta)


@dataclass
class FakePostings:
    company_name: str | None = "(주)가상테크"
    posting_lines: list[tuple[str, str]] = field(default_factory=lambda: [*JD_LINES, *TEAM_LINES])

    async def profile(self, posting_id: str) -> PostingProfile | None:
        if posting_id != POSTING_ID:
            return None
        return PostingProfile(
            posting_id=posting_id,
            ontology_version="onto-test",
            source="provisional",
            company="가상테크",
            position_name="백엔드 개발자",
            sub="sw.backend",
        )

    async def lines(self, posting_id: str) -> list[tuple[str, str]]:
        return list(self.posting_lines)

    async def company(self, posting_id: str) -> str | None:
        return self.company_name


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


@dataclass
class Session:
    client: httpx.AsyncClient
    backend: CapturingBackend
    service: InterviewService
    store: MemoryInterviewStore
    postings: FakePostings
    data_dir: Path
    clock: Clock
    interview_id: str = ""

    @property
    def revision(self) -> int:
        return self.store.rows[self.interview_id].revision

    def mapping_path(self) -> Path:
        return self.data_dir / "interviews" / self.interview_id / "mapping.json"

    def push(self, *outputs: BaseModel) -> None:
        for output in outputs:
            self.backend.push(Completion(output.model_dump_json()))

    async def start(self) -> dict[str, Any]:
        response = await self.client.post("/interviews", json={"posting_id": POSTING_ID})
        assert response.status_code == 201, response.text
        self.interview_id = response.json()["interview_id"]
        return response.json()

    async def post(self, suffix: str, **data: Any) -> httpx.Response:
        return await self.client.post(
            f"/interviews/{self.interview_id}/{suffix}", json={"revision": self.revision, **data}
        )

    async def success_step(self, *, trap: bool = True) -> dict[str, Any]:
        self.push(answer_output(), check_output(trap=trap))
        response = await self.post("success", **SUCCESS)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture(scope="session")
def detectors() -> Detectors:
    # 이름 탐지기(Kiwi)는 tests/pseudonymize 의 인터뷰 가명화 테스트에서 따로 — 여기서는 빠른 규칙 탐지기
    return Detectors(load_dictionaries(), use_kiwi=False)


def make_service(
    tmp: Path, detectors: Detectors, *, prompts: PromptRegistry | None = None
) -> tuple[InterviewService, CapturingBackend, MemoryInterviewStore, FakePostings, Clock]:
    clock = Clock()
    backend = CapturingBackend()
    store = MemoryInterviewStore(now=clock)
    postings = FakePostings()
    service = InterviewService(
        store=store,
        pseudo=InterviewPseudonymizer(detectors, tmp),
        postings=postings,
        llm=LLMClient({"opencode": backend}),
        prompts=prompts or prompts_with_current(tmp),
        ontology_version="onto-test",
        now=clock,
    )
    return service, backend, store, postings, clock


@pytest.fixture
async def session(tmp_path: Path, detectors: Detectors) -> AsyncIterator[Session]:
    service, backend, store, postings, clock = make_service(tmp_path, detectors)
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Session(client, backend, service, store, postings, tmp_path, clock)
