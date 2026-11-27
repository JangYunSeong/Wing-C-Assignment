"""그래프 조립.

START → posting_profile ─┬─ (문서 있음) ingest → pseudonymize ─┬─ (통과) extract_claims → assign_roles
                         │                                      └─ (실패) END
                         └─ (문서 없음) evidence
assign_roles → [retrieve_t1 ∥ retrieve_t2] → evidence → verify_overlap → compare → draft_spec → approval
approval ─ approved → finalize → END / revise → draft_spec / rejected → END
"""

from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from task_spec.llm import LLMClient
from task_spec.nodes.claims import make_extract_claims
from task_spec.nodes.compare import make_compare
from task_spec.nodes.ingest import Ingestor, make_ingest, stub_ingestor
from task_spec.nodes.posting_profile import make_posting_profile
from task_spec.nodes.pseudonymize import (
    Pseudonymizer,
    make_pseudonymize,
    pseudonymization_passed,
    stub_pseudonymizer,
)
from task_spec.nodes.retrieve import make_evidence, make_retrieve_t1, retrieve_t2
from task_spec.nodes.roles import assign_roles
from task_spec.nodes.spec import approval, make_draft_spec, make_finalize
from task_spec.nodes.verify_overlap import make_verify_overlap
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import PostingStore
from task_spec.postings.suggest import OntologyLists
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import PublicIncident, RunInput, RunState
from task_spec.spec.case_frame import CaseFramer
from task_spec.spec.company_incidents import CompanyIncidentRepo, CompanyResolver
from task_spec.spec.store import CommonTaskRepo


def _route_after_profile(state: RunState) -> Literal["ingest", "evidence", "__end__"]:
    # 공고를 찾지 못하면 멈춘다. 지원자 문서가 없으면 ②③ retrieve 를 건너뛰고 공고 역량의 평가 근거 → 공통 과제만.
    if state.posting_profile is None:
        return END
    return "ingest" if state.has_documents() else "evidence"


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
    ontology: OntologyRepository | None = None,
    postings: PostingStore | None = None,
    ontology_lists: OntologyLists | None = None,
    retriever: T1Retriever | None = None,
    relatedness: Relatedness | None = None,
    common_store: CommonTaskRepo | None = None,
    incidents: dict[str, PublicIncident] | None = None,
    use_cover_letters: bool = False,
    feature_task_default: bool = False,
    company_incidents: CompanyIncidentRepo | None = None,
    company_of: CompanyResolver | None = None,
    # 공유 회사 사례의 함정 틀 (설계 검사 #332 S-F8) — 인터뷰 서비스가 만든다 (같은 가명화 길). 없으면 틀 없이
    case_framer: CaseFramer | None = None,
) -> CompiledStateGraph:
    g = StateGraph(RunState, input_schema=RunInput)

    g.add_node(
        "posting_profile", make_posting_profile(ontology, postings, ontology_lists, common_store)
    )
    g.add_node("ingest", make_ingest(ingestor, use_cover_letters=use_cover_letters))
    g.add_node("pseudonymize", make_pseudonymize(pseudonymizer))
    g.add_node("assign_roles", assign_roles)
    g.add_node("extract_claims", make_extract_claims(llm, prompts))
    g.add_node("retrieve_t1", make_retrieve_t1(retriever, llm, prompts))
    g.add_node("retrieve_t2", retrieve_t2)
    g.add_node("evidence", make_evidence(ontology))
    # ④ 공고 역량 → 주장 확인 (top-down). 주장·공고 역량이 없으면 아무것도 하지 않는다
    g.add_node(
        "verify_overlap",
        make_verify_overlap(llm, prompts, retriever.cards if retriever is not None else None),
    )
    g.add_node("compare", make_compare(relatedness))
    g.add_node(
        "draft_spec",
        make_draft_spec(
            ontology,
            relatedness,
            llm,
            prompts,
            common_store,
            incidents,
            feature_default=feature_task_default,
            company_incidents=company_incidents,
            company_of=company_of,
            case_framer=case_framer,
        ),
    )
    g.add_node("approval", approval, destinations=("finalize", "draft_spec", END))
    g.add_node("finalize", make_finalize(common_store))

    g.add_edge(START, "posting_profile")
    g.add_conditional_edges("posting_profile", _route_after_profile, ["ingest", "evidence", END])
    g.add_edge("ingest", "pseudonymize")
    g.add_conditional_edges("pseudonymize", _route_after_pseudonymize, ["extract_claims", END])
    g.add_edge("extract_claims", "assign_roles")
    g.add_edge("assign_roles", "retrieve_t1")
    g.add_edge("assign_roles", "retrieve_t2")
    g.add_edge(["retrieve_t1", "retrieve_t2"], "evidence")  # 둘 다 끝나야 합류
    g.add_edge("evidence", "verify_overlap")
    g.add_edge("verify_overlap", "compare")
    g.add_edge("compare", "draft_spec")
    g.add_edge("draft_spec", "approval")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
