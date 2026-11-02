"""그래프 조립.

START → posting_profile ─┬─ (문서 있음) ingest → pseudonymize ─┬─ (통과) extract_claims → assign_roles
                         │                                      └─ (실패) END
                         └─ (문서 없음) compare
assign_roles → [retrieve_t1 ∥ retrieve_t2] → evidence → compare → draft_spec → approval
approval ─ approved → finalize → END / revise → draft_spec / rejected → END
"""

from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from task_spec.llm import LLMClient
from task_spec.nodes.claims import make_extract_claims
from task_spec.nodes.ingest import Ingestor, make_ingest, stub_ingestor
from task_spec.nodes.posting_profile import posting_profile
from task_spec.nodes.pseudonymize import (
    Pseudonymizer,
    make_pseudonymize,
    pseudonymization_passed,
    stub_pseudonymizer,
)
from task_spec.nodes.retrieve import evidence, retrieve_t1, retrieve_t2
from task_spec.nodes.roles import assign_roles
from task_spec.nodes.spec import approval, compare, draft_spec, finalize
from task_spec.prompting import PromptRegistry
from task_spec.schemas import RunInput, RunState


def _route_after_profile(state: RunState) -> Literal["ingest", "compare"]:
    # 지원자 문서가 없으면 ②③ 을 건너뛰고 공통 과제만 만든다.
    return "ingest" if state.has_documents() else "compare"


def _route_after_pseudonymize(state: RunState) -> Literal["extract_claims", "__end__"]:
    # 가명화 누락 검사 실패 → 즉시 END. 다음 단계로 절대 넘기지 않는다.
    return "extract_claims" if pseudonymization_passed(state) else END


def build_graph(
    checkpointer: BaseCheckpointSaver | None,
    *,
    ingestor: Ingestor = stub_ingestor,
    pseudonymizer: Pseudonymizer = stub_pseudonymizer,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
) -> CompiledStateGraph:
    g = StateGraph(RunState, input_schema=RunInput)

    g.add_node("posting_profile", posting_profile)
    g.add_node("ingest", make_ingest(ingestor))
    g.add_node("pseudonymize", make_pseudonymize(pseudonymizer))
    g.add_node("assign_roles", assign_roles)
    g.add_node("extract_claims", make_extract_claims(llm, prompts))
    g.add_node("retrieve_t1", retrieve_t1)
    g.add_node("retrieve_t2", retrieve_t2)
    g.add_node("evidence", evidence)
    g.add_node("compare", compare)
    g.add_node("draft_spec", draft_spec)
    g.add_node("approval", approval, destinations=("finalize", "draft_spec", END))
    g.add_node("finalize", finalize)

    g.add_edge(START, "posting_profile")
    g.add_conditional_edges("posting_profile", _route_after_profile, ["ingest", "compare"])
    g.add_edge("ingest", "pseudonymize")
    g.add_conditional_edges("pseudonymize", _route_after_pseudonymize, ["extract_claims", END])
    g.add_edge("extract_claims", "assign_roles")
    g.add_edge("assign_roles", "retrieve_t1")
    g.add_edge("assign_roles", "retrieve_t2")
    g.add_edge(["retrieve_t1", "retrieve_t2"], "evidence")  # 둘 다 끝나야 합류
    g.add_edge("evidence", "compare")
    g.add_edge("compare", "draft_spec")
    g.add_edge("draft_spec", "approval")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
