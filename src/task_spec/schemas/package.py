"""과제 판 전달 묶음 — 받는 곳별 파일 형식 (이슈 #85, docs/design/starter-templates.md §6.1) [제안].

빌드 1건(`build_id` = `<common_id>-b<n>`)마다 `data/images/<build_id>/` 아래에 역할별 폴더를 둔다.

    data/images/<build_id>/
      candidate/  <build_id>-candidate.tar.zst · candidate.json · SHA256SUMS   → 응시 환경만
      grader/     <build_id>-grader.tar.zst    · grader.json    · SHA256SUMS   → 채점 서버만
      eval_kit/   kit.json                     · SHA256SUMS                    → 평가 쪽만 (채점과 같은 권한)

- 이미지는 역할마다 완전한 이미지(`podman save --format docker-archive` → zstd)를 하나씩 담는다.
  식별은 `image_id`(설정 해시 — save/load 뒤에도 같다)와 전송 파일 sha256. 레지스트리 다이제스트는 쓰지 않는다
  (load 하면 바뀐다, 2026-11-10 실측).
- 응시용(candidate.json)에는 케이스·함정·사유 정보를 하나도 넣지 않는다.
- 채점용(grader.json)에는 케이스 id 와 시작 코드 기대값(개수)만. 함정 id·의도한 사유는 이미지 안 config 와 `result.json` 에만 있다.
- 평가 키트(kit.json)는 승인된 판에서 코드로 만든다. 지원자 문서에서 나온 칸(`slots`·`claims_to_verify`)은 넣지 않는다 (불변식 1·3).
  결과 평가(케이스·숨은 요구) 말고 과정 평가 기준도 단계별로 담는다(`stage1`·`stage2` — 관찰 항목·자료 역할·질문이 단서가 되는 함정,
  2026-11-12 사용자 결정 A: 2단계는 같은 함정의 진단 단계). 자료 본문·답 문장은 진행자 쪽에만 있다.
- 2026-11-10 목 묶음(`ct-0683f8a1b8d6-r3`)을 기준으로 했다. 목과 다른 점: `build_id`·`grader_sha256` 추가,
  `acceptance_check`(글) → `starter_expected`(값), 결과 케이스 필드에 `message`, 키트 케이스에 `competency_ids`·승인자 판단.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import (
    AcceptedDesign,
    BehavioralAnchors,
    Observable,
    TargetCompetency,
)

COMMON_ID = r"^ct-[0-9a-f]{12}$"
BUILD_ID = (
    r"^ct-[0-9a-f]{12}-b[1-9][0-9]{0,3}$"  # <common_id>-b<n> — 경로에 그대로 쓰므로 패턴을 좁게
)
SHA256 = r"^[0-9a-f]{64}$"
IMAGE_ID = r"^sha256:[0-9a-f]{64}$"
TEMPLATE_REF = r"^[a-z0-9_-]+/[a-z0-9_.-]+@v[1-9][0-9]*$"  # <직군>/<스택>@vN
FILE_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"  # 역할 폴더 안 파일 이름 — 경로 구분자 없음

ArtifactRole = Literal["candidate", "grader", "eval_kit"]
Platform = Literal["linux/amd64", "linux/arm64"]
ReviewVerdict = Literal[
    "confirmed", "fixed", "accepted_risk"
]  # 확인(문제 없음) · 고침 · 알고 받아들임
# 겹침·무력화 재료 판정을 받아들인 빌드의 검토 표시 머리 (#341, 2026-11-27 사용자 — 숨은 케이스는 채점에 남긴다). 이 표시는 판단이
# '알고 받아들임'(accepted_risk)이어야 승인된다 (starter/store.check_approval) — 과제가 AI 첫 답과 구별되지 않음을 승인자가 받아들인다.
# 저장 모델에 칸을 더하지 않고 문장 머리로 가른다 — 모든 경계 모델이 모르는 칸을 거부해 배포를 되돌리면 옛 코드가 새 기록을 못 읽는다
NOT_A_TRAP = "함정 아님(채점 유지): "


class ArtifactFile(Schema):
    """역할 폴더 안 파일 1개 — SHA256SUMS 한 줄과 같다."""

    name: str = Field(pattern=FILE_NAME)
    sha256: str = Field(pattern=SHA256)
    size_bytes: int = Field(ge=0)


class ImageArchive(Schema):
    """압축한 이미지 파일. `podman load -i <file>` 뒤 `podman image inspect --format '{{.Id}}'` 가 `image_id` 와 같아야 한다."""

    file: str = Field(pattern=FILE_NAME)
    sha256: str = Field(pattern=SHA256)  # 압축 파일(.tar.zst) 기준
    size_bytes: int = Field(ge=1)
    image_id: str = Field(pattern=IMAGE_ID)
    name: str = Field(min_length=1)  # load 뒤 이름 (localhost/task/<build_id>-<role>)


class CaseResult(Schema):
    """케이스 하나의 채점 결과 — result.json `cases[]` 에서 (승인자가 문제를 쓸지 정할 때 본다, 2026-11-11 사용자).
    message 는 실패 단언 앞부분만 — 테스트 본문은 넣지 않는다."""

    id: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=20)  # hidden · public
    passed: bool
    reason: str | None = Field(default=None, max_length=80)
    message: str | None = Field(default=None, max_length=300)


class VerifySummary(Schema):
    """채점 결과 요약 — result.json `summary` 와 같은 모양 + 케이스별 결과(cases, 없으면 예전 기록)."""

    hidden_passed: int = Field(ge=0)
    hidden_total: int = Field(ge=0)
    public_passed: int = Field(ge=0)
    public_total: int = Field(ge=0)
    existing_failed: list[str] = Field(default_factory=list)
    selftest_failed: list[str] = Field(default_factory=list)
    cases: list[CaseResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_within_total(self) -> VerifySummary:
        if self.hidden_passed > self.hidden_total or self.public_passed > self.public_total:
            raise ValueError("통과 수가 전체보다 많다")
        return self


class PackageHeader(Schema):
    """세 파일 공통 머리."""

    mock: bool = False
    # approved = 시작 코드 승인 뒤(전달 가능) · pending = builder 가 review 에서 쓴 승인 전 묶음(전달 불가, 승인 때 approved 로 다시 씀 #130)
    # · none = 목 판(평가용 명세로 만든 것)
    approval: Literal["approved", "pending", "none"]
    note: str = ""
    common_id: str = Field(pattern=COMMON_ID)
    revision: int = Field(ge=1)
    build_id: str = Field(pattern=BUILD_ID)
    template_ref: str = Field(pattern=TEMPLATE_REF)
    template_sha256: str | None = Field(
        default=None, pattern=SHA256
    )  # 템플릿 vN 폴더 해시 (§6 `id@vN+sha`)
    ontology_version: str = Field(min_length=1)
    tree_sha256: str = Field(pattern=SHA256)  # 응시용 작업 트리 (시작 코드)
    platform: Platform = "linux/amd64"
    title: str | None = None

    @model_validator(mode="after")
    def _real_package_rules(self) -> PackageHeader:
        if not self.build_id.startswith(f"{self.common_id}-b"):
            raise ValueError(
                f"build_id {self.build_id} 가 common_id {self.common_id} 의 빌드가 아니다"
            )
        if self.mock:
            return self
        # 실제 묶음: 승인 전(pending)이거나 사람 승인(시작 코드 승인) 뒤(approved) — 승인 없이 전달되는 묶음은 없다.
        # pending 은 builder(#129)가 review 에서 등록하는 묶음 (승인이 묶음을 요구하므로 승인 전에 만든다). 전달은 approved 만.
        if self.approval == "none":
            raise ValueError(
                "mock 이 아닌 묶음은 approval=pending(승인 전) 또는 approved 여야 한다"
            )
        if self.approval == "pending":
            return self
        # 전달하는 묶음은 온톨로지 고정 버전만 (불변식 4)
        if self.ontology_version.endswith("+dirty"):
            raise ValueError("mock 이 아닌 묶음에 커밋 안 된 온톨로지(+dirty)를 쓸 수 없다")
        return self


class CandidateDescriptor(PackageHeader):
    """candidate.json — 응시 환경. 케이스·함정·사유 정보 없음."""

    role: Literal["candidate"] = "candidate"
    image: ImageArchive
    load: str = Field(min_length=1)
    workspace: str = Field(pattern=r"^/[^\s]*$")
    run: str = Field(min_length=1)  # 지원자 컨테이너 시작 명령
    commands: dict[str, str] = Field(default_factory=dict)  # build·test·run 등 — 모두 오프라인


class ResultContract(Schema):
    """채점 결과 파일 형식 — 채점 스크립트(starter/grade/image_grade*.py)가 쓰는 `result.json`."""

    path: str = "/out/result.json"
    fields: list[str] = Field(min_length=1)
    case_fields: list[str] = Field(min_length=1)
    note: str = (
        "result.json 에 trap_id·intended_reason·실패 message(숨은 테스트 단언 문구)가 들어 있다 "
        "— 채점·평가 쪽 권한으로만 다룬다"
    )


# 받는 쪽 안내 — grader.json 칸 설명·플랫폼 계약 문서에 같은 문장 (마운트 정책: starter/mounts.py, #174)
COPY_NOTE = (
    "<제출물>·<결과>는 채점할 때마다 로컬 디스크에 새로 만든 사본 폴더 (podman 을 돌리는 사용자 소유) — 원본 제출물을 붙이지 "
    "않는다. 전용 라벨(:Z)이 그 사본에만 붙고, 로컬이라 NFS 처럼 라벨을 못 바꾸는 저장소 문제도 피한다"
)
NO_RELABEL_NOTE = "SELinux 격리를 끈다 — 사본을 로컬 디스크에 둘 수 없을 때만"


class GraderDescriptor(PackageHeader):
    """grader.json — 채점 서버. 함정 id·의도한 사유는 넣지 않는다 (이미지 안 config·result.json 에만)."""

    role: Literal["grader"] = "grader"
    image: ImageArchive
    load: str = Field(min_length=1)
    grader_sha256: str = Field(
        pattern=SHA256
    )  # 채점용 내용(숨은 overlay + config) — tree_sha256 은 숨은 테스트를 가리지 못한다
    # 기본 — SELinux 격리를 켠 채 사본 폴더에 전용 라벨 `:Z` (starter/mounts.py)
    grade: str = Field(min_length=1, description=COPY_NOTE)
    # 라벨을 못 바꾸는 저장소(NFS 등)용
    grade_without_selinux_relabel: str = Field(min_length=1, description=NO_RELABEL_NOTE)
    network: Literal["none"] = "none"
    timeout_s: int = Field(ge=1)
    result: ResultContract
    case_ids: list[str] = Field(min_length=1)
    starter_expected: VerifySummary  # 시작 코드를 제출물로 채점했을 때 나와야 하는 값 (setup=ok 와 함께 — 받는 쪽 호환성 시험)

    @model_validator(mode="after")
    def _grade_offline(self) -> GraderDescriptor:
        for cmd in (self.grade, self.grade_without_selinux_relabel):
            if "--network none" not in cmd:
                raise ValueError("grade 명령에 --network none 이 없다")
        if "label=disable" not in self.grade_without_selinux_relabel:
            raise ValueError(
                "grade_without_selinux_relabel 에 --security-opt label=disable 이 없다"
            )
        return self


class KitCase(Schema):
    """케이스 → 의미. 실패 사유 코드를 사람이 읽을 수 있게."""

    id: str = Field(min_length=1)
    trap_id: str = Field(min_length=1)
    intended_reason: str = Field(min_length=1)
    reason_description: str = Field(min_length=1)  # 템플릿 manifest failure_reasons 설명
    hidden_requirement: str = Field(min_length=1)
    competency_ids: list[str] = Field(
        default_factory=list
    )  # 이 케이스가 근거가 되는 대상 역량 — 지금 명세에 연결이 없어 비면 과제 단위로 본다


class KitAcceptance(Schema):
    ac_id: str = Field(pattern=r"^h\d{1,2}$")
    trap_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


def flag_id(text: str) -> str:
    """검토 표시 문장의 짧은 id (sha256 앞 12자리). 같은 문장 = 같은 id — 화면·키트에서 표시를 가리키고, 다른 빌드의 같은 표시를 찾을 때 쓴다."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


class ReviewDecision(Schema):
    """검토 표시 1건에 대한 승인자 판단 — 과제를 믿을 근거. 표시는 `flag_id` 로 맞추고, 그때의 문장도 함께 남긴다."""

    flag_id: str = Field(pattern=r"^[0-9a-f]{12}$")
    flag: str = Field(min_length=1)  # 판단할 때 본 표시 문장 그대로
    verdict: ReviewVerdict
    note: str = ""
    reviewer: str = Field(min_length=1)

    @model_validator(mode="after")
    def _id_matches_text(self) -> ReviewDecision:
        if flag_id(self.flag) != self.flag_id:
            raise ValueError(f"flag_id {self.flag_id} 가 표시 문장과 맞지 않는다")
        return self

    @classmethod
    def of(cls, flag: str, verdict: ReviewVerdict, reviewer: str, note: str = "") -> ReviewDecision:
        return cls(flag_id=flag_id(flag), flag=flag, verdict=verdict, note=note, reviewer=reviewer)


class KitMaterial(Schema):
    """요청 시 공개하는 자료 1건의 역할 — 평가 쪽이 진행자 기록(어떤 자료를 요청했는지)과 맞춰 본다.
    본문은 진행자 쪽(진행자 보기)에 있어 넣지 않는다. 역할은 지원자에게 숨긴다."""

    artifact_id: str = Field(pattern=r"^a\d{1,2}$")
    title: str = Field(min_length=1)
    role: Literal["key", "support", "distractor"]


class KitClarification(Schema):
    """추가 질문 주제 1건과 그 답이 단서가 되는 함정 — 숨은 요구에 닿는 질문을 했는지 평가 쪽이 본다. 답 문장은 진행자 쪽에."""

    clarification_id: str = Field(pattern=r"^c\d{1,2}$")
    topic: str = Field(min_length=1)
    reveals: list[str] = Field(default_factory=list)  # trap_id


class KitRequirement(Schema):
    """공식 요구 1건 — 문제 분석 판에서는 지원자 README 에 없으므로, 평가 쪽이 요구별로 판단(§7.1)하려면 문장이 필요하다."""

    req_id: str = Field(pattern=r"^r\d{1,2}$")
    text: str = Field(min_length=1)


class KitFact(Schema):
    """요청자 지식 1건의 연결 — 진행자 기록에서 열린 사실을 요구로 맞춘다(§7.1 "물어서 앎"). 답 문장은 진행자 쪽에."""

    fact_id: str = Field(pattern=r"^f\d{1,2}$")
    covers: list[str] = Field(min_length=1)  # r#·p#
    topic: str = Field(min_length=1)


class KitPublicAcceptance(Schema):
    """공개 수용 기준 → 공식 요구 — 공개 테스트 결과를 요구로 모은다(§7.1 "만들었나")."""

    ac_id: str = Field(pattern=r"^p\d{1,2}$")
    req_ids: list[str] = Field(min_length=1)


class KitStage(Schema):
    """한 단계의 진단(과정) 평가 기준 — 진행자 기록(먼저 말한 것·요청한 자료·질문)과 맞춰 본다.
    2026-11-12 사용자 결정(A): 2단계는 1단계와 같은 함정의 증상을 주고 진단 과정을 보는 단계 — 새 숨은 요구·테스트는 없다."""

    trap_ids: list[str] = Field(
        default_factory=list
    )  # 이 단계가 겨냥하는 함정 (2단계 = 증상의 원인 함정)
    observables: list[Observable] = Field(default_factory=list)
    materials: list[KitMaterial] = Field(default_factory=list)
    max_requests: int | None = None
    clarifications: list[KitClarification] = Field(
        default_factory=list
    )  # 1단계만 (EvalKit 이 확인)
    max_clarifications: int | None = None  # 1단계만 — 진행자 보기의 max_questions 와 같은 값
    # 문제 분석 판(analysis-stage.md §6·§8) — 1단계만, 명세에 사실이 있을 때만 채운다 (예전 판은 비어 있음)
    request_mentions: list[str] = Field(
        default_factory=list
    )  # 요청 글이 이미 말한 r# → "보이는 것으로 앎"
    requirements: list[KitRequirement] = Field(default_factory=list)
    facts: list[KitFact] = Field(default_factory=list)
    acceptance_public: list[KitPublicAcceptance] = Field(default_factory=list)

    def analysis_fields(self) -> bool:
        return bool(
            self.request_mentions or self.requirements or self.facts or self.acceptance_public
        )


class KitImages(Schema):
    candidate: str = Field(pattern=IMAGE_ID)
    grader: str = Field(pattern=IMAGE_ID)


class EvalKit(PackageHeader):
    """kit.json — 평가 쪽. 숨은 요구가 문장으로 들어가므로 채점용 이미지와 같은 권한. 참조 구현은 넣지 않는다."""

    role: Literal["eval_kit"] = "eval_kit"
    images: KitImages
    grader_sha256: str = Field(pattern=SHA256)
    cases: list[KitCase] = Field(min_length=1)
    acceptance_hidden: list[KitAcceptance] = Field(min_length=1)
    accepted_designs: list[AcceptedDesign] = Field(min_length=1)
    behavioral_anchors: BehavioralAnchors | None = None
    follow_up_questions: list[str] = Field(default_factory=list)
    stage1: KitStage | None = (
        None  # 1단계(구현) 진단 기준 — 기능형 명세의 observables·artifacts·clarifications
    )
    stage2: KitStage | None = None  # 2단계(디버깅) 진단 기준 — 명세 debug_stage (없는 과제는 None)
    target_competencies: list[TargetCompetency] = Field(min_length=1)
    verification: dict[
        str, VerifySummary
    ]  # starter-only · naive-* · d* (참조 구현 결과만, 코드는 없음)
    verification_note: str = ""
    review_flags: list[str] = Field(default_factory=list)
    review_decisions: list[ReviewDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kit_consistency(self) -> EvalKit:
        traps = {c.trap_id for c in self.cases}
        loose = sorted({a.trap_id for a in self.acceptance_hidden} - traps)
        if loose:
            raise ValueError(f"acceptance_hidden 의 trap_id 가 케이스에 없다: {loose}")
        known = {t.competency_id for t in self.target_competencies}
        unknown = sorted({cid for c in self.cases for cid in c.competency_ids} - known)
        if unknown:
            raise ValueError(f"케이스 competency_ids 가 대상 역량에 없다: {unknown}")
        self._check_stages(traps)
        if "starter-only" not in self.verification:
            raise ValueError("verification 에 starter-only(시작 코드 그대로) 결과가 없다")
        if not self.mock:
            decided = {d.flag_id for d in self.review_decisions}
            missing = [f for f in self.review_flags if flag_id(f) not in decided]
            if missing:
                raise ValueError(f"승인자 판단이 없는 검토 표시: {missing}")
        return self

    def _check_stages(self, case_traps: set[str]) -> None:
        """과정 기준의 참조 — 명세 생성 검증(spec/feature.py)과 같은 단계별 규칙. 진행자 기록은 단계마다 따로 맞춰 보므로
        한 단계의 관찰 항목은 그 단계의 자료·질문만 가리킨다. 함정은 케이스의 것과 1단계 목록(명세의 traps 전부 —
        변별 못 해 채점에서 뺀 케이스의 함정도 남는다)에서 찾는다."""
        s1, s2 = self.stage1, self.stage2
        if s1 is not None and (missing := sorted(case_traps - set(s1.trap_ids))):
            raise ValueError(f"stage1 trap_ids 에 케이스의 함정이 없다: {missing}")
        known = case_traps | (set(s1.trap_ids) if s1 is not None else set())
        if s2 is not None and (s2.clarifications or s2.max_clarifications is not None):
            raise ValueError("stage2 에 추가 질문이 있다 — 질문은 1단계에만")
        if s2 is not None and s2.analysis_fields():
            raise ValueError("stage2 에 문제 분석 칸이 있다 — 요청 글·사실은 1단계에만")
        if s1 is not None and s1.analysis_fields():
            self._check_analysis(s1)
        for name, s in (("stage1", s1), ("stage2", s2)):
            if s is None:
                continue
            mats = {m.artifact_id for m in s.materials}
            asks = {c.clarification_id for c in s.clarifications}
            refs = [*s.trap_ids, *(t for c in s.clarifications for t in c.reveals)]
            for o in s.observables:
                if o.kind == "requests_artifact" and o.value not in mats:
                    raise ValueError(
                        f"{name} 관찰 항목이 이 단계에 없는 자료를 가리킨다: {o.value}"
                    )
                if o.kind == "asks_clarification" and o.value not in asks:
                    raise ValueError(
                        f"{name} 관찰 항목이 이 단계에 없는 질문을 가리킨다: {o.value}"
                    )
                if o.kind == "catches_trap":
                    refs.append(o.value)
            if bad := sorted(set(refs) - known):
                raise ValueError(f"{name} 이 키트에 없는 함정을 가리킨다: {bad}")

    @staticmethod
    def _check_analysis(s: KitStage) -> None:
        """문제 분석 칸의 키트 안 참조 — 평가 쪽이 진행자 기록·공개 테스트를 요구로 모을 수 있게 (명세 쪽 검사는 spec/analysis.py)."""
        reqs = {r.req_id for r in s.requirements}
        known = reqs | {a.ac_id for a in s.acceptance_public}
        if bad := sorted({c for f in s.facts for c in f.covers} - known):
            raise ValueError(f"stage1 facts 의 covers 가 키트에 없는 요구·기준을 가리킨다: {bad}")
        pointed = {*s.request_mentions, *(r for a in s.acceptance_public for r in a.req_ids)}
        if bad := sorted(pointed - reqs):
            raise ValueError(
                f"stage1 request_mentions·req_ids 가 키트에 없는 요구를 가리킨다: {bad}"
            )
