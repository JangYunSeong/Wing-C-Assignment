"""앱 팩토리. 테스트는 `create_app(settings=..., graph=...)` 로 설정·그래프를 바꿔 끼운다.

ASGITransport 는 lifespan 을 돌리지 않으므로, graph 가 주입되면 상태를 즉시 구성한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.deps import PostingsContext
from task_spec.api.errors import install_error_handlers
from task_spec.api.interview_generation import InterviewGenerationRunner
from task_spec.api.launcher import RunLauncher
from task_spec.api.routers import builds, health, incidents, interviews, postings, pull, runs
from task_spec.config import Settings, get_settings
from task_spec.delivery.sender import DeliverySender
from task_spec.graph import build_graph, make_checkpointer
from task_spec.graph.build_approval import build_approval_graph
from task_spec.graph.incident_share import incident_share_graph
from task_spec.graph.interview_approval import interview_approval_graph
from task_spec.graph.interview_generate import interview_generate_graph
from task_spec.interview.approval import PgApprovalCommitter
from task_spec.interview.postings import PostingSource
from task_spec.interview.service import InterviewService, sweep_forever
from task_spec.interview.store import PgInterviewStore
from task_spec.interview.task import OntologyTaskMaker
from task_spec.llm.factory import build_llm_client
from task_spec.logging_setup import configure_logging
from task_spec.nodes.ingest import FileIngestor
from task_spec.nodes.pseudonymize import InterviewPseudonymizer, RulePseudonymizer
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.company import posting_companies
from task_spec.postings.store import PostingStore
from task_spec.postings.suggest import load_lists
from task_spec.prompting import PromptRegistry
from task_spec.retrieve import build_retriever
from task_spec.spec.company_incidents import CompanyIncidentStore
from task_spec.spec.incidents import load_incidents
from task_spec.spec.store import CommonTaskStore
from task_spec.starter.store import PgBuildStore


def create_app(
    settings: Settings | None = None, graph: CompiledStateGraph | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "graph", None) is not None:
            yield  # 주입된 그래프 (체크포인터 수명은 주입한 쪽 책임)
            await app.state.launcher.shutdown()
            return
        app.state.settings = settings or get_settings()
        configure_logging(app.state.settings.log_level, app.state.settings.log_format)
        # 온톨로지 버전 고정 — 적재되지 않은 버전이면 기동 실패 (불변식 4)
        app.state.ontology = await OntologyRepository.open(
            app.state.settings.database_url.get_secret_value(), app.state.settings.ontology_version
        )
        prompts = PromptRegistry(app.state.settings.prompts_dir)
        prompts.validate_all()  # 프롬프트 머리말·변수·스키마 오류면 기동 실패
        llm = build_llm_client(app.state.settings)
        # 새 공고(경로 2): 온톨로지 목록·매칭 규칙(같은 버전) + 임시 공고 저장소
        lists = await load_lists(app.state.ontology)
        store = await PostingStore.open(app.state.settings.database_url.get_secret_value())
        # 공고 단위 공통 과제 저장소 (같은 공고 지원자는 같은 공통 과제)
        common_store = await CommonTaskStore.open(
            app.state.settings.database_url.get_secret_value()
        )
        # 공통 과제 빌드·전달 묶음 저장소 (#85) — 테이블은 여기서 만든다. 묶음 파일은 settings.images_root
        app.state.builds = await PgBuildStore.open(
            app.state.settings.database_url.get_secret_value()
        )
        # 승인된 묶음 SFTP 전달 (#116) — API 프로세스 안에서만 보낸다 (builder 는 만들기만)
        app.state.delivery = DeliverySender(
            app.state.builds,
            app.state.settings.images_root,
            app.state.settings.sftp_targets,
            app.state.settings.sftp_timeout_seconds,
        )
        await app.state.delivery.recover()
        app.state.common_store = common_store  # 명세 승인 전 공통 과제 확인 (runs.approve_run)
        # 회사 결정적 사건 (같은 회사 공고에서 공유 승인된 사례를 공개 사례보다 먼저) [확정: 2026-11-19 사용자]
        company_incidents = await CompanyIncidentStore.open(
            app.state.settings.database_url.get_secret_value()
        )
        app.state.company_incidents = company_incidents

        async def company_of(posting_id: str) -> str | None:
            found = await posting_companies([posting_id], ontology=app.state.ontology, store=store)
            c = found.get(posting_id)
            return c.company_id if c is not None else None

        app.state.postings = PostingsContext(store=store, lists=lists, llm=llm, prompts=prompts)
        pseudonymizer = RulePseudonymizer(app.state.settings.data_dir)
        await pseudonymizer.warm_up()  # Kiwi 모델 로드(수 초)를 첫 요청이 아니라 기동 때
        # 채용담당자 사례 인터뷰 (#262 D1) — 탐지기(Kiwi)는 지원서 가명화기와 같이 쓴다. 프롬프트 CURRENT 가 없으면 '준비 중'
        interview_store = await PgInterviewStore.open(
            app.state.settings.database_url.get_secret_value()
        )
        public_incidents = load_incidents(app.state.settings.incidents_dir)  # 공개 사례 (git 파일)
        app.state.interviews = InterviewService(
            store=interview_store,
            pseudo=InterviewPseudonymizer(
                pseudonymizer.detectors, app.state.settings.data_dir.resolve()
            ),
            postings=PostingSource(ontology=app.state.ontology, store=store, lists=lists),
            llm=llm,
            prompts=prompts,
            ontology_version=lists.version,
            tasks=OntologyTaskMaker(
                ontology=app.state.ontology,
                llm=llm,
                prompts=prompts,
                interfaces=public_incidents.interfaces,  # #256 재료 인터페이스
            ),
            common_store=common_store,
            # 승인 확정 — 인터뷰·회사 사례·공통 과제를 같은 연결로 한 트랜잭션에 (D2b)
            approvals=PgApprovalCommitter(interview_store.pool),
            # 사례 출처 (D2c) — 공개 사례와 같은 회사의 공유 승인된 사례에서 고르기
            public_incidents=public_incidents,
            company_incidents=company_incidents,
            builds=app.state.builds,
            mapping_ttl=timedelta(days=app.state.settings.interview_mapping_ttl_days),
            draft_ttl=timedelta(hours=app.state.settings.interview_draft_ttl_hours),
        )
        # 보관 기간 정리 (결정 4) — 원문 임시 저장 하루, 방치 인터뷰 대응표 30일
        sweeper = asyncio.create_task(
            sweep_forever(
                app.state.interviews,
                timedelta(minutes=app.state.settings.interview_sweep_minutes),
            )
        )
        retriever = await build_retriever(app.state.ontology, lists)  # T1 검색 색인 (수 초, 로컬)
        async with make_checkpointer(app.state.settings) as cp:
            app.state.graph = build_graph(
                cp,
                ingestor=FileIngestor(app.state.settings.data_dir),
                pseudonymizer=pseudonymizer,
                llm=llm,
                prompts=prompts,
                ontology=app.state.ontology,
                postings=store,
                ontology_lists=lists,
                retriever=retriever,
                relatedness=await load_relatedness(app.state.ontology),
                common_store=common_store,
                incidents=public_incidents,
                use_cover_letters=app.state.settings.use_cover_letters,
                feature_task_default=app.state.settings.feature_task_default,
                company_incidents=company_incidents,
                company_of=company_of,
            )
            app.state.launcher = RunLauncher(app.state.graph)
            # 빌드(시작 코드) 승인 — 실행 그래프와 같은 체크포인터, thread 는 빌드·결과마다 (#130)
            app.state.build_approval = build_approval_graph(app.state.builds, cp)
            # 회사 사례 공유 승인 — 같은 체크포인터, thread 는 (사례, 내용)마다
            app.state.incident_share = incident_share_graph(company_incidents, cp)
            # 인터뷰 과제 승인 — 같은 체크포인터, thread 는 (인터뷰, 판본)마다 (결정 2, #266 D2b)
            app.state.interview_approval = interview_approval_graph(app.state.interviews, cp)
            # 인터뷰 과제 만들기 — 같은 체크포인터, thread 는 생성마다 (#280, 명세 실행과 같은 방식으로 백그라운드)
            generation = InterviewGenerationRunner(
                interview_generate_graph(app.state.interviews, cp)
            )
            app.state.interviews.generation = generation
            try:
                yield
            finally:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
                await (
                    generation.shutdown()
                )  # 도는 생성은 끊긴다 — 다음 기동 때 '중단됨'(이어서 하기)
                await interview_store.close()
                await app.state.launcher.shutdown()
                await app.state.delivery.shutdown()
                await app.state.ontology.close()
                await store.close()
                await common_store.close()
                await company_incidents.close()
                await app.state.builds.close()

    app = FastAPI(title="task-spec", lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings
    if graph is not None:
        if settings is None:
            raise ValueError("graph 를 주입할 때는 settings 도 함께 줘야 합니다")
        app.state.graph = graph
        app.state.launcher = RunLauncher(graph)
    install_error_handlers(app)
    app.include_router(runs.router)
    app.include_router(postings.router)
    app.include_router(builds.router)
    app.include_router(incidents.router)
    app.include_router(interviews.router)
    app.include_router(pull.router)
    app.include_router(health.router)
    return app


app = create_app()
