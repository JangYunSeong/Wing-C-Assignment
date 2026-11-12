"""의존성 (Annotated 별칭)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.errors import RunNotFound, ServiceUnavailable
from task_spec.api.launcher import RunLauncher
from task_spec.api.run_view import load_run_view
from task_spec.api.schemas import RunView
from task_spec.config import Settings
from task_spec.delivery.sender import DeliverySender
from task_spec.llm import LLMClient
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import PostingStore
from task_spec.postings.suggest import OntologyLists
from task_spec.prompting import PromptRegistry
from task_spec.spec.store import CommonTaskRepo
from task_spec.starter.store import BuildRepo


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_graph(request: Request) -> CompiledStateGraph:
    return request.app.state.graph  # type: ignore[no-any-return]


def get_launcher(request: Request) -> RunLauncher:
    return request.app.state.launcher  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
GraphDep = Annotated[CompiledStateGraph, Depends(get_graph)]
LauncherDep = Annotated[RunLauncher, Depends(get_launcher)]


def get_ontology(request: Request) -> OntologyRepository | None:
    """앱 lifespan 이 연 온톨로지 (테스트에서 그래프를 주입하면 없을 수 있다)."""
    return getattr(request.app.state, "ontology", None)


OntologyDep = Annotated[OntologyRepository | None, Depends(get_ontology)]


async def get_run_view(run_id: str, graph: GraphDep, launcher: LauncherDep) -> RunView:
    view = await load_run_view(graph, launcher, run_id)
    if view is None:
        raise RunNotFound(run_id)
    return view


RunViewDep = Annotated[RunView, Depends(get_run_view)]


@dataclass(frozen=True)
class PostingsContext:
    """새 공고(경로 2) 처리에 필요한 것 — 앱 lifespan 이 만든다."""

    store: PostingStore
    lists: OntologyLists
    llm: LLMClient | None
    prompts: PromptRegistry | None


def get_postings(request: Request) -> PostingsContext:
    ctx = getattr(request.app.state, "postings", None)
    if ctx is None:
        raise ServiceUnavailable("새 공고 처리가 준비되지 않았습니다 (온톨로지·저장소 미연결)")
    return ctx


PostingsDep = Annotated[PostingsContext, Depends(get_postings)]


def get_builds(request: Request) -> BuildRepo:
    """공통 과제 빌드 저장소 — lifespan 이 연다 (테스트는 InMemoryBuildStore 를 app.state.builds 에)."""
    store = getattr(request.app.state, "builds", None)
    if store is None:
        raise ServiceUnavailable("빌드 저장소가 준비되지 않았습니다 (DB 미연결)")
    return store  # type: ignore[no-any-return]


BuildsDep = Annotated[BuildRepo, Depends(get_builds)]


def get_delivery(request: Request) -> DeliverySender:
    """승인된 묶음 전달기 (#116) — lifespan 이 만든다 (테스트는 app.state.delivery 에)."""
    sender = getattr(request.app.state, "delivery", None)
    if sender is None:
        raise ServiceUnavailable("전달기가 준비되지 않았습니다")
    return sender  # type: ignore[no-any-return]


DeliveryDep = Annotated[DeliverySender, Depends(get_delivery)]


def get_common_store(request: Request) -> CommonTaskRepo | None:
    """공고 단위 공통 과제 저장소 — lifespan 이 연다. DB 없는 테스트 앱은 None (승인 전 확인을 건너뛴다)."""
    return getattr(request.app.state, "common_store", None)


CommonStoreDep = Annotated[CommonTaskRepo | None, Depends(get_common_store)]


def get_build_approval(request: Request) -> CompiledStateGraph:
    """빌드 승인 그래프 (#130) — lifespan 이 실행 그래프와 같은 체크포인터로 만든다 (테스트는 app.state.build_approval 에)."""
    graph = getattr(request.app.state, "build_approval", None)
    if graph is None:
        raise ServiceUnavailable("빌드 승인 그래프가 준비되지 않았습니다")
    return graph  # type: ignore[no-any-return]


BuildApprovalDep = Annotated[CompiledStateGraph, Depends(get_build_approval)]
