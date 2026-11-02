"""의존성 (Annotated 별칭)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.errors import RunNotFound
from task_spec.api.launcher import RunLauncher
from task_spec.api.run_view import load_run_view
from task_spec.api.schemas import RunView
from task_spec.config import Settings
from task_spec.ontology.repository import OntologyRepository


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
