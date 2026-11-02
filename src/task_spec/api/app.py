"""앱 팩토리. 테스트는 `create_app(settings=..., graph=...)` 로 설정·그래프를 바꿔 끼운다.

ASGITransport 는 lifespan 을 돌리지 않으므로, graph 가 주입되면 상태를 즉시 구성한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.errors import install_error_handlers
from task_spec.api.launcher import RunLauncher
from task_spec.api.routers import health, runs
from task_spec.config import Settings, get_settings
from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.ingest import FileIngestor
from task_spec.nodes.pseudonymize import RulePseudonymizer
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry


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
        # 온톨로지 버전 고정 — 적재되지 않은 버전이면 기동 실패 (불변식 4)
        app.state.ontology = await OntologyRepository.open(
            app.state.settings.database_url.get_secret_value(), app.state.settings.ontology_version
        )
        prompts = PromptRegistry(app.state.settings.prompts_dir)
        prompts.validate_all()  # 프롬프트 머리말·변수·스키마 오류면 기동 실패
        llm = build_llm_client(app.state.settings)
        pseudonymizer = RulePseudonymizer(app.state.settings.data_dir)
        await pseudonymizer.warm_up()  # Kiwi 모델 로드(수 초)를 첫 요청이 아니라 기동 때
        async with make_checkpointer(app.state.settings) as cp:
            app.state.graph = build_graph(
                cp,
                ingestor=FileIngestor(app.state.settings.data_dir),
                pseudonymizer=pseudonymizer,
                llm=llm,
                prompts=prompts,
                ontology=app.state.ontology,
            )
            app.state.launcher = RunLauncher(app.state.graph)
            try:
                yield
            finally:
                await app.state.launcher.shutdown()
                await app.state.ontology.close()

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
    app.include_router(health.router)
    return app


app = create_app()
