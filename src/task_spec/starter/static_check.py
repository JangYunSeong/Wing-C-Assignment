"""시작 코드 오버레이 정적 검사 `static_check` (docs/design/spec-to-code.md §6) — 결정적, LLM·빌드 없음.

생성기가 쓴 오버레이 파일(시작 코드·숨은 테스트·참조 구현)과 렌더한 README 를 빌드 전에 본다.
실패는 원인 생성기에 배정한다 (§7 수리): 재생성할 생성기(`gen_starter` 등)에 붙는 `problems` 와,
재생성으로 고칠 수 없어 승인자가 봐야 하는 `review_flags`(README = 승인된 명세 문장에서 나온 것·템플릿 기본값)를 나눈다.

검사
1. 경로 — kind 별 `fill_slots` 안, `locked` 밖, hidden 이 아니면 `reserved` 밖. 잠긴 하네스 자리를 고치면 따로 알린다. 파일 수·크기 상한.
2. 숨은 요구·운영 조건 낱말(재료 leak·trigger·원인 개념·먼저 말할 개념)·숨은 테스트 이름이 공개 파일(시작 코드·README·주는 공개 테스트)에
   — 코드 식별자는 낱말로 풀어서 본다 (connectionPool → connection pool). 미끼 코드(해법·문제 지점을 가리키는 주석·설정)도 이 낱말로 잡는다.
3. 운영 값이 시작 코드 설정에 박힘 — 템플릿 `harness_params[].config_keys` 의 값만 대조 (아무 숫자나 비교하면 8·32 같은 값이 오탐).
4. 비밀 값 · 외부 네트워크 주소 · 실존 회사·제품 이름(원 사례 고유어). 비밀 값은 실제 형식(AWS 키·개인 키·sk-/ghp-/xox- 토큰)만
   재생성 사유이고, 이름 = 값 추정(password·access_token 뒤 12자 이상)은 review_flags — 계약 fixture 의 시험용 토큰을 생성기가 못 바꾼다 (2026-11-18).
5. 숨은 테스트: 지원자 구현 내부를 import (게이트 ⑥ 사전 확인), 실패 사유 코드가 템플릿 목록 밖, 케이스 id 가 HiddenPlan 과 1:1 이 아님,
   조절값 파일을 손으로 씀(코드가 HiddenPlan 에서 쓴다 — `render.render_params`).
   공개 계약에 없는 API 를 부름 (manifest `hidden_rules.http_calls` 로 찾은 호출의 경로를 `{}` 자리로 정규화해 대조),
   한 파일에 같은 사유·문구의 단언이 둘 이상은 막지 않고 review_flags (`hidden_rules.reason_labels` — 사유가 잘못 붙었을 수 있음).
   경로 문자열에 쿼리(`?…`)를 손으로 붙임 — 도우미의 쿼리 매개변수 오버로드로 (숨은·공개 테스트), 시간을 재는 요청(Burst.fire·rounds·peak)
   안에서 요청을 둘 이상 보냄 (분기로 하나만 보내는 것은 둔다) — 2026-11-13.
6. 테이블 이름 (manifest `naming.table`, starter/tables.py): 시작 코드가 선언한 테이블이 계약 테이블 밖이면 gen_starter, 숨은 테스트의 오류 주입 정규식이 계약 테이블 밖을 가리키거나 SQL 종류 없이 넓으면 gen_hidden.
"""

from __future__ import annotations

import ast
import difflib
import functools
import posixpath
import re
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from task_spec.schemas import FeatureSeed, GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.build import Endpoint, FileKind
from task_spec.schemas.template import SlotKind, TemplateManifest
from task_spec.spec.feature import find_terms, title_allowed_text
from task_spec.starter.auth_rule import credentials, issued_rule, locked_existing
from task_spec.starter.contract import SpecView, contract_fields, existing_endpoints, hidden_terms
from task_spec.starter.field_types import non_null
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.header_values import PER_REQUEST, header_values
from task_spec.starter.hidden_helpers import (
    api_functions,
    code_written_paths,
    key_fields,
    render_contract_case,
    render_persistence_check,
    row_use_consts,
    setting_const,
)
from task_spec.starter.py_helpers import command_name, py_ident
from task_spec.starter.render import README, render_params
from task_spec.starter.seed_rows import peak_reasons
from task_spec.starter.seed_sql import schema_problems, seed_column_values, time_kind
from task_spec.starter.skeleton import (
    APP,
    ENDPOINTS,
    JAVA_CONTROLLER,
    JAVA_SERVICE,
    PAGES,
    endpoint_fns,
    java_fns,
    page_names,
)
from task_spec.starter.skeleton import routes as skeleton_routes
from task_spec.starter.skip_apis import NOTE as SKIP_NOTE
from task_spec.starter.skip_apis import skip_hits
from task_spec.starter.tables import (
    contract_tables,
    declared_tables,
    referenced_tables,
    same_table,
    spring_snake,
    tables_with_fields,
)
from task_spec.starter.templates import (
    Template,
    blocked_reason,
    glob_match,
    is_java,
    is_python,
)
from task_spec.starter.ts_helpers import (
    TESTING_LIBRARY,
    app_type_names,
    fault_fn,
    render_app_types,
)

KIND_SLOT: dict[str, SlotKind] = {
    "starter": "starter",
    "public_test": "public_test",
    "hidden": "hidden",
    "ref_naive": "ref",
    "ref_accepted": "ref",
}
KIND_TARGET = {
    "starter": "gen_starter",
    "public_test": "gen_public",
    "hidden": "gen_hidden",
    "ref_naive": "gen_naive",
    "ref_accepted": "gen_accepted",
    "ref_variant": "gen_variant",
}

# 실제 비밀의 정해진 형식 — 걸리면 생성기에 다시 쓰게 한다
_SECRETS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|gho|xox[abp])[-_][A-Za-z0-9]{20,}"),
]
# 이름과 길이만 보는 추정 — 승인자 표시로만. 2026-11-18 ct-10eb910705dd-b1: 계약 fixture 의 시험용 토큰
# (access_token: 'test-token-001')을 공개 테스트가 그대로 써야 하는데 이 규칙이 막아, 재요청으로 못 고치고 빌드가 실패했다
_SECRET_HINT = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?(?=[^\s'\"]*\d)(?=[^\s'\"]*[A-Za-z])[^\s'\"]{12,}"
)
_URL = re.compile(r"https?://([A-Za-z0-9.\-]+)")
_LOCAL_HOSTS = re.compile(
    r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|(?:[\w-]+\.)*example\.(?:com|org|net)|[\w-]+\.test|[\w-]+\.invalid)$"
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")  # 케밥 표기도 한 이름 (#241)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CONFIG_SUFFIX = (".yml", ".yaml", ".properties")


@dataclass(frozen=True)
class Overlay:
    """생성기 산출물 한 벌 — 참조 구현은 변형마다 한 벌 (label 로 구분, 예: "naive-t1")."""

    kind: FileKind
    files: tuple[GeneratedFile, ...]
    label: str = ""

    @property
    def target(self) -> str:
        return KIND_TARGET[self.kind]

    @property
    def name(self) -> str:
        return f"{self.kind}{f'({self.label})' if self.label else ''}"


@dataclass(frozen=True)
class StaticProblem:
    target: str  # 재생성할 생성기 — gen_starter · gen_hidden · gen_naive · gen_accepted
    message: str


@dataclass
class StaticResult:
    problems: list[StaticProblem] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)  # 재생성으로 못 고침 — 승인 화면에

    @property
    def ok(self) -> bool:
        return not self.problems

    def for_target(self, target: str) -> list[str]:
        return [p.message for p in self.problems if p.target == target]

    @property
    def targets(self) -> list[str]:
        return list(dict.fromkeys(p.target for p in self.problems))


def identifier_words(text: str) -> str:
    """코드 식별자를 낱말로 푼 글 — connectionPoolSize · max_pool · retry-queue → "connection pool size" · "max pool" · "retry queue"."""
    words = []
    for ident in set(_IDENT.findall(text)):
        spaced = _CAMEL.sub(" ", ident).replace("_", " ").replace("-", " ").strip().lower()
        if " " in spaced:
            words.append(spaced)
    return "\n".join(sorted(words))


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        out[prefix] = node
    return out


def config_values(path: str, content: str) -> dict[str, str]:
    """설정 파일(yml·yaml·properties)의 점 표기 키 → 값 문자열. 읽지 못하면 빈 dict."""
    if path.endswith(".properties"):
        out: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            kv = re.match(r"^([^=:\s]+)\s*[=:]\s*(.*)$", line)
            if kv:
                out[kv.group(1)] = kv.group(2).strip()
        return out
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return {}
    flat: dict[str, str] = {}
    for doc in docs:
        flat |= {k: str(v) for k, v in _flatten(doc).items()}
    return flat


def _same_value(a: str, b: Any) -> bool:
    try:
        return float(str(a).replace(",", "").replace("_", "")) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _paths(o: Overlay, t: Template) -> list[StaticProblem]:
    m = t.manifest
    out: list[StaticProblem] = []
    locked_harness = (
        [
            f"{m.hidden_layout.harness}/*",
            f"{m.hidden_layout.selftest}/**",
        ]
        if m.hidden_layout
        else []
    )
    slot = KIND_SLOT[o.kind]
    slots = m.fill_slots.get(slot, [])
    for f in o.files:
        why = blocked_reason(m, slot, f.path)
        if why is None:
            continue
        if locked_harness and glob_match(f.path, locked_harness):
            msg = f"{o.name} 파일 {f.path} 는 잠긴 채점 하네스 자리 — 하네스는 고치지 않고 케이스 자리({m.hidden_layout.cases if m.hidden_layout else ''})에만 쓴다"
        elif why == "locked":
            msg = (
                f"{o.name} 파일 {f.path} 는 잠긴 파일(템플릿 그대로 두는 파일) — 고치거나 다시 쓰지 않는다. 바꿀 것은 쓸 수 있는 "
                "자리의 파일에서: " + ", ".join(slots)
            )
        elif why == "reserved":
            msg = (
                f"{o.name} 파일 {f.path} 는 채점이 쓰는 자리 — 이 산출물은 두지 않는다. 쓸 수 있는 경로: "
                + ", ".join(slots)
            )
        else:
            msg = (
                f"{o.name} 파일 {f.path} 는 이 산출물이 쓸 수 있는 자리(fill_slots) 밖 — 쓸 수 있는 경로: "
                + ", ".join(slots)
            )
        if why != "locked":
            # 막힌 자리에 데이터를 두는 실수 — 2026-11-10 retrydupx #030: 시작 코드가 테스트 데이터를 src/test/resources/seed-test.sql 에
            # 두 번 다 써 시작 코드 생성이 실패했다 (그 경로는 fill_slots 밖). 자리마다가 아니라 막힌 경로 모두에 같은 안내 (#178)
            msg += ". 테스트용 데이터·설정 파일을 따로 두지 않는다 — 시드의 테스트 몫 행(작은 id)을 쓰거나 테스트 코드 안에서 저장소·API 로 만든다"
        out.append(StaticProblem(o.target, msg))
    lim = m.limits
    if lim is not None:
        # 코드가 계약에서 결정적으로 쓰는 파일은 크기 상한에서 뺀다 — 상한은 LLM 이 쓴 파일을 막는 장치이고, 시드 행 수는
        # 계약이 막는다 (SeedRule.rows ≤ 5000). 2026-11-11 N+1 백엔드 알파: 숨은 케이스 댓글 1050행(코드가 2100행으로 나눔) 시드로
        # data.sql 이 100000B 를 넘어 시작 코드 생성 실패 — 생성기가 고칠 수 없는 파일이다.
        coded = code_written_paths(m)  # 코드가 쓰는 파일 전부 (시드·조절값·도우미·저장 확인 — #178)
        sized = [f for f in o.files if f.path not in coded]
        if len(o.files) > lim.max_files:
            out.append(
                StaticProblem(
                    o.target, f"{o.name} 파일 수 {len(o.files)} 가 상한 {lim.max_files} 를 넘음"
                )
            )
        big = [f.path for f in sized if len(f.content.encode()) > lim.max_file_bytes]
        if big:
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} 파일이 크기 상한 {lim.max_file_bytes}B 를 넘음: {', '.join(big)}",
                )
            )
        total = sum(len(f.content.encode()) for f in sized)
        if total > lim.max_total_bytes:
            out.append(
                StaticProblem(
                    o.target, f"{o.name} 전체 크기 {total}B 가 상한 {lim.max_total_bytes}B 를 넘음"
                )
            )
    return out


def _hygiene(
    o: Overlay | None, files: Iterable[GeneratedFile], avoid_terms: Sequence[str]
) -> list[str]:
    """비밀 값·외부 주소·원 사례 고유어 — 메시지 목록 (배정은 호출하는 쪽)."""
    out: list[str] = []
    for f in files:
        where = f"{o.name} 파일 {f.path}" if o else f.path
        if any(rx.search(f.content) for rx in _SECRETS):
            out.append(f"{where} 에 비밀 값처럼 보이는 문자열이 있음")
        hosts = sorted({h for h in _URL.findall(f.content) if not _LOCAL_HOSTS.match(h)})
        if hosts:
            out.append(
                f"{where} 에 외부 네트워크 주소: {', '.join(hosts)} — 검증·채점은 네트워크 없이 돈다"
            )
        named = find_terms(avoid_terms, f.content)
        if named:
            out.append(f"{where} 에 원 사례의 실존 이름·고유어: {', '.join(named)}")
    return out


def _secret_hints(o: Overlay | None, files: Iterable[GeneratedFile]) -> list[str]:
    """비밀 값처럼 보이는 이름 = 값 — 시험용 값(계약 fixture 의 토큰 등)일 수 있어 승인자가 본다."""
    out: list[str] = []
    for f in files:
        if m := _SECRET_HINT.search(f.content):
            where = f"{o.name} 파일 {f.path}" if o else f.path
            out.append(
                f"{where} 에 비밀 값처럼 보이는 문자열 ({m.group(0)[:60]}) — 시험용 값인지 확인"
            )
    return out


def _leaks(files: Iterable[GeneratedFile], terms: Sequence[str]) -> list[tuple[str, list[str]]]:
    hits = []
    for f in files:
        text = f"{f.path}\n{f.content}\n{identifier_words(f.content)}\n{identifier_words(f.path)}"
        shown = find_terms(terms, text)
        if shown:
            hits.append((f.path, shown))
    return hits


def _operating_values(
    o: Overlay, t: Template, plan: HiddenPlan
) -> tuple[list[StaticProblem], list[str]]:
    problems: list[StaticProblem] = []
    flags: list[str] = []
    keyed = [
        (p, k) for p in t.manifest.harness_params if p.name in plan.harness for k in p.config_keys
    ]
    if not keyed:
        return problems, flags
    base = {
        k: v
        for rel in t.files("project")
        if rel.endswith(_CONFIG_SUFFIX)
        for k, v in config_values(
            rel, (t.project / rel).read_text("utf-8", errors="ignore")
        ).items()
    }
    for f in o.files:
        if not f.path.endswith(_CONFIG_SUFFIX):
            continue
        cfg = config_values(f.path, f.content)
        for p, key in keyed:
            if key in cfg and _same_value(cfg[key], plan.harness[p.name]):
                msg = (
                    f"{o.name} 설정 {f.path} 의 {key}={cfg[key]} 가 채점 운영 값({p.name})과 같음 — "
                    "운영 값은 채점 때 하네스가 넣는다, 시작 코드에는 개발용 값만"
                )
                if key in base and _same_value(base[key], plan.harness[p.name]):
                    flags.append(
                        f"템플릿 기본 설정 {key} 가 운영 값과 같음 — 템플릿 기본값을 변경할 신호"
                    )
                else:
                    problems.append(StaticProblem(o.target, msg))
    return problems, flags


HARNESS_OVERRIDE = "채점 때 하네스가 덮어쓰는 설정"


def _config_map(files: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        k: v
        for path, content in files
        if path.endswith(_CONFIG_SUFFIX)
        for k, v in config_values(path, content).items()
    }


def _harness_overrides(
    o: Overlay, starter: Overlay | None, t: Template, plan: HiddenPlan
) -> list[StaticProblem]:
    """허용 구현이 하네스 조절값의 설정 키를 바꿈 — 채점 때 운영 값으로 덮어써지므로 그 설정에 기댄 설계는 채점에서 무효
    (알파 테스트 2026-11-07: 허용 설계 '풀 크기 산정'을 하네스가 pool_size 로 덮어써 어떤 구현도 통과할 수 없었다)."""
    keys = {k for p in t.manifest.harness_params if p.name in plan.harness for k in p.config_keys}
    if not keys:
        return []
    base = _config_map(
        (rel, (t.project / rel).read_text("utf-8", errors="ignore"))
        for rel in t.files("project")
        if rel.endswith(_CONFIG_SUFFIX)
    )
    if starter is not None:
        base |= _config_map((f.path, f.content) for f in starter.files)
    out: list[StaticProblem] = []
    for f in o.files:
        cfg = _config_map([(f.path, f.content)])
        for key in sorted(keys & cfg.keys()):
            if key in base and str(base[key]).strip() == str(cfg[key]).strip():
                continue  # 시작 코드 값 그대로 — 바꾸지 않음
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} 설정 {f.path} 의 {key}={cfg[key]} 는 {HARNESS_OVERRIDE}다 — "
                    "이 값을 바꿔 숨은 요구를 피하는 설계는 채점에서 효과가 없다. 운영 값은 코드로도 바꾸지 않는다 — "
                    "숨은 요구는 연결을 쓰는 방식(트랜잭션 경계·기록 시점)으로 푼다",
                )
            )
    return out


def _config_pitfalls(o: Overlay, t: Template) -> list[StaticProblem]:
    """산출물 설정이 템플릿이 정하지 않은 값을 매니페스트 `config_pitfalls` 의 키에 씀 — 키와 이유는 템플릿이 선언한다 (#176).
    Java 판: 2026-11-10 v19-be 시작 코드가 spring.datasource.url 을 jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1 로 고정해 하네스 자체 시험
    (다른 테스트 컨텍스트)이 같은 DB 에 schema.sql·data.sql 을 다시 돌려 Table already exists·PK 위반으로 기동 실패 (2026-11-09 d3 도 같은 설정)."""
    slot = KIND_SLOT[o.kind]
    rules = [r for r in t.manifest.config_pitfalls if slot in r.overlays]
    configs = [f for f in o.files if f.path.endswith(_CONFIG_SUFFIX)]
    if not rules or not configs:
        return []
    base = _config_map(
        (rel, (t.project / rel).read_text("utf-8", errors="ignore"))
        for rel in t.files("project")
        if rel.endswith(_CONFIG_SUFFIX)
    )
    out: list[StaticProblem] = []
    for f in configs:
        got = _config_map([(f.path, f.content)])
        for r in rules:
            keys = sorted(k for k in got if k in r.keys and k not in base)
            if keys:
                out.append(
                    StaticProblem(
                        o.target,
                        f"{o.name} 설정 {f.path} 에 템플릿에 없는 설정 {', '.join(keys)} — {r.fix}",
                    )
                )
    return out


_SQL_STRING = re.compile(r"'(?:''|[^'])*'")
_QUOTED_NAME = re.compile(r'"([A-Za-z_]\w*)"')


def _java_test_pitfalls(o: Overlay, t: Template, seed_path: str | None) -> list[StaticProblem]:
    """schema.sql 의 큰따옴표 이름("rank"): H2 에서 대소문자를 구분하는 이름이 되어 따옴표 없는 엔티티 매핑으로는 'Column … not found'
    — 2026-11-10 stab3·v19-be 시작 코드의 기존 기능 테스트가 그대로 실패해 재생성 예산을 다 썼다. 파일 간 조건(엔티티 표기·
    globally_quoted_identifiers·시드 파일)이라 매니페스트 code_pitfalls(파일 하나)로 옮기지 않았다. PATCH 함정은 매니페스트로 옮김 (#176)."""
    if not is_java(t.manifest):
        return []
    out: list[StaticProblem] = []
    # 엔티티가 같은 따옴표 표기(@Column(name = "\"rank\"")·백틱)로 매핑한 이름은 맞다 — 시드 SQL 도 스키마 표기를 따른다
    # (seed_sql.declared_name, 2026-11-10 stab1 은 이렇게 맞춰 통과)
    escaped = {
        n
        for f in o.files
        if f.path.endswith(".java")
        for n in re.findall(r'\\"(\w+)\\"|`(\w+)`', f.content)
        for n in n
        if n
    }
    config = " ".join(f.content for f in o.files if f.path.endswith(_CONFIG_SUFFIX))
    if "globally_quoted_identifiers" in config:
        escaped |= {"*"}
    for f in o.files:
        if f.path.endswith(".sql") and f.path != seed_path:
            names = sorted(
                n
                for n in set(_QUOTED_NAME.findall(_SQL_STRING.sub("''", f.content)))
                if n not in escaped and "*" not in escaped
            )
            if names:
                out.append(
                    StaticProblem(
                        o.target,
                        f"{o.name} {f.path} 의 큰따옴표 이름 {', '.join(names)} 을 엔티티가 따옴표 없이 매핑함 — H2 에서 따옴표 이름은 대소문자를 "
                        '구분해 \'Column … not found\' 가 난다. 엔티티도 같은 표기로 매핑하거나(@Column(name = "\\"rank\\"")) 스키마에서 따옴표를 뺀다',
                    )
                )
    return out


def _call_arg(text: str, start: int) -> str:
    """start 부터 괄호 깊이 0 의 `,` 또는 `)` 앞까지 — 호출 인자 식 하나."""
    depth, i, quote = 0, start, False
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i]


def _split_plus(expr: str) -> list[str]:
    parts, depth, quote, cur = [], 0, False, ""
    for i, ch in enumerate(expr):
        if quote:
            quote = not (ch == '"' and expr[i - 1] != "\\")
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return [*parts, cur.strip()]


_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')


def path_template(expr: str) -> str | None:
    """`"/orders/" + id + "/status"` → `/orders/{}/status`. 첫 조각이 "/…" 문자열이 아니면 None (판단 안 함). 쿼리는 뺀다."""
    out: list[str] = []
    for n, part in enumerate(_split_plus(expr)):
        lit = _LITERAL.match(part)
        if n == 0 and not (lit and lit.group(1).startswith("/")):
            return None
        out.append(lit.group(1) if lit else "{}")
    return "".join(out).split("?", 1)[0]


def http_calls(content: str, patterns: Sequence[str]) -> list[tuple[str, str]]:
    """(메서드, 경로 틀) — 경로를 판단할 수 없는 호출은 뺀다."""
    found: list[tuple[str, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, content):
            path = path_template(_call_arg(content, m.end()))
            if path is not None:
                method = (m.groupdict().get("method") or "GET").upper()
                found.append((method, path))
    return found


def _contract_api(contract: TaskContract) -> list[tuple[str, re.Pattern[str]]]:
    return _endpoint_rx(contract.endpoints)


def _endpoint_rx(endpoints: Iterable[Endpoint]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (
            e.method.upper(),
            re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(e.path)) + "/?$"),
        )
        for e in endpoints
        if e.method
    ]


def _unavailable_imports(o: Overlay, t: Template) -> list[StaticProblem]:
    """템플릿 클래스패스에 없는 패키지 import — 컴파일이 반드시 실패하므로 Gradle 전에 그 생성기에."""
    rx = [re.compile(p, re.M) for p in t.manifest.unavailable_imports]
    out: list[StaticProblem] = []
    for f in o.files:
        bad = sorted({m.group(0).strip() for r in rx for m in r.finditer(f.content)})
        if bad:
            out.append(
                StaticProblem(
                    o.target,
                    f"{f.path} 가 템플릿에 없는 라이브러리를 씀: {'; '.join(bad)} — 템플릿 의존성만 쓴다 (입력 검증은 직접 확인하고 계약의 오류 응답으로)",
                )
            )
    return out


_REL_IMPORT = re.compile(
    r"""^\s*(?:import|export)\b[^;]*?\bfrom\s+['"](\.{1,2}/[^'"]+)['"]""", re.M
)
_TS_EXTS = ("", ".ts", ".tsx", "/index.ts", "/index.tsx")


def _test_folder_imports(o: Overlay, t: Template) -> list[StaticProblem]:
    """구현(시작 코드·참조 구현)이 채점 자리(reserved)나 공개 테스트 자리의 파일을 import — 그 파일은 대상마다 있거나 없어서
    (시작 코드 + 숨은 테스트 실행에는 공개 테스트가 없다) 컴파일이 깨지고, 지원자 이미지에도 없다. TypeScript 상대 import 만 (Java 는 패키지).
    2026-11-09 React 알파: 시작 코드·참조 구현 셋이 공개 도우미(src/__public__/contract.ts)의 응답 타입을 import 했다."""
    m = t.manifest
    if is_java(m):
        return []
    off_limits = [*m.reserved, *m.fill_slots.get("public_test", [])]
    out: list[StaticProblem] = []
    for f in o.files:
        bad = []
        for spec in _REL_IMPORT.findall(f.content):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(f.path), spec))
            if any(glob_match(target + ext, off_limits) for ext in _TS_EXTS):
                bad.append(spec)
        if bad:
            out.append(
                StaticProblem(
                    o.target,
                    f"{f.path} 가 채점·공개 테스트 폴더의 파일을 import 함: {', '.join(sorted(set(bad)))} — 구현은 테스트 도우미를 쓰지 않는다. "
                    "응답·데이터 타입은 계약에서 코드가 만든 src/api/contract-types.ts 를 import 한다 (없는 템플릿이면 계약의 데이터 모양대로 src/ 안에 정의)",
                )
            )
    return out


_OPTIONS_IMPORT = re.compile(
    r"""import\s*(?:type\s*)?\{[^}]*\bOPTIONS\b[^}]*\}\s*from\s+['"][./\w-]*contract-types['"]"""
)


def _starter_options(o: Overlay, contract: TaskContract | None) -> list[StaticProblem]:
    """시작 코드가 선택형 요소의 계약 값(options)을 그리는가 — 값을 그대로 쓰거나 contract-types 의 OPTIONS 로.
    다르면 테스트(OPTIONS 로 고름)와 화면(다른 값)이 어긋난다."""
    if contract is None:
        return []
    text = "\n".join(f.content for f in o.files if f.path.endswith((".ts", ".tsx")))
    uses_const = bool(_OPTIONS_IMPORT.search(text))
    out: list[StaticProblem] = []
    for sc in contract.screens:
        for el in sc.elements:
            if not el.options or uses_const:
                continue
            absent = [
                v
                for v in el.options
                if not re.search(rf"""['"`>]\s*{re.escape(v)}\s*['"`<]""", text)
            ]
            if absent:
                out.append(
                    StaticProblem(
                        o.target,
                        f"선택형 요소 {el.test_id} 의 계약 값이 화면에 없음: {', '.join(absent[:5])} — "
                        "contract-types 의 OPTIONS 로 그리거나 계약 값을 그대로 쓴다 (안내 문구는 따로, 값은 비워 둠)",
                    )
                )
    return out


_JSON_INLINE = re.compile(
    r":\s*\{[^;=]*\}\s*(?:\[\])?\s*=\s*await\s+[\w.$]+\.json\(\)"  # const data: { items: X[] } = await res.json()
    r"|\.json\(\)\s*\)?\s*as\s+\{"  # (await res.json()) as { … }
)
_MSW_IMPORT = re.compile(r"""from\s+['"]msw(?:/[\w-]+)?['"]""")


@functools.lru_cache(maxsize=8)
def mock_dirs(project: Path) -> tuple[str, ...]:
    """템플릿 프로젝트에서 개발용 가짜 서버(msw)를 두는 폴더 — msw 를 import 하는 파일들의 폴더 (React 템플릿 src/mocks/)."""
    dirs = {
        p.parent.relative_to(project).as_posix() + "/"
        for p in project.rglob("*.ts*")
        if "node_modules" not in p.parts
        and _MSW_IMPORT.search(p.read_text("utf-8", errors="ignore"))
    }
    return tuple(sorted(dirs))


def is_mock_server(f: GeneratedFile, t: Template) -> bool:
    """개발용 가짜 서버 코드 — msw 를 import 하거나 템플릿의 가짜 서버 폴더 안 (응답을 만드는 쪽이라 요청 본문 request.json() 을 읽는다).
    #177: 경로 글자(src/mocks/)와 변수 이름(request·req) 두 겹으로 면제해, 핸들러가 그 폴더 밖이고 변수가 r·input 이면 빗나갔다."""
    return bool(_MSW_IMPORT.search(f.content)) or f.path.startswith(mock_dirs(t.project))


_TYPES_IMPORT = re.compile(r"""from\s+['"](?:\.{1,2}/)+(?:[\w-]+/)*contract-types['"]""")


def _split_params(params: str) -> list[str]:
    """매개변수 목록 → 이름들 — 타입 안의 쉼표({ a: string; b: number }·Record<a, b>)는 나누지 않는다."""
    names, depth, cur = [], 0, ""
    for ch in params + ",":
        if ch in "{<([":
            depth += 1
        elif ch in "}>)]":
            depth -= 1
        if ch == "," and depth == 0:
            name = re.split(r"[?:=]", cur.strip(), maxsplit=1)[0].strip()
            if name:
                names.append(name)
            cur = ""
        else:
            cur += ch
    return names


_JAVA_PARAMS = r"\(((?:[^()]|\([^()]*\))*)\)"


def _java_param_names(params: str) -> list[str]:
    """Java 매개변수 목록 → 이름 (어노테이션·제네릭 안의 쉼표는 나누지 않는다)."""
    names, depth, cur = [], 0, ""
    for ch in params + ",":
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        if ch == "," and depth == 0:
            words = re.findall(r"\w+", re.sub(r"@\w+(\([^)]*\))?", "", cur))
            if words:
                names.append(words[-1])
            cur = ""
        else:
            cur += ch
    return names


def _java_skeleton_kept(written: dict[str, str], contract: TaskContract) -> list[str]:
    """Java 뼈대 — ContractController·ContractService 의 메서드 이름·매개변수, 컨트롤러 매핑. 다른 파일이 계약 경로를 다시 매핑하면 기동 실패."""
    msgs: list[str] = []
    fns = java_fns(contract)
    rule = " — 코드 뼈대: 메서드 이름·매개변수·매핑은 그대로 두고 몸통만 채운다 (클래스·메서드는 더해도 된다)"
    for path in (JAVA_CONTROLLER, JAVA_SERVICE):
        if path not in written:
            continue
        for fn in fns:
            m = re.search(
                rf"public\s+[\w<>?,\s]+?\s{re.escape(fn.name)}\s*{_JAVA_PARAMS}", written[path]
            )
            if m is None:
                msgs.append(f"{path} 에 계약 메서드 {fn.name} 가 없음{rule}")
            elif tuple(_java_param_names(m.group(1))) != fn.params:
                msgs.append(
                    f"{path} 의 {fn.name} 매개변수가 뼈대와 다름: ({', '.join(_java_param_names(m.group(1)))}) — 뼈대는 ({', '.join(fn.params)}){rule}"
                )
    if JAVA_CONTROLLER in written:
        for e in contract.endpoints:
            if (
                e.method
                and f'{e.method.capitalize()}Mapping("{e.path}")' not in written[JAVA_CONTROLLER]
            ):
                msgs.append(f"{JAVA_CONTROLLER} 에 {e.method} {e.path} 매핑이 없음{rule}")
    paths = {e.path for e in contract.endpoints if e.method}
    for path, text in written.items():
        if path == JAVA_CONTROLLER or not path.endswith(".java") or "/src/test/" in f"/{path}":
            continue
        if not re.search(r"@(?:Rest)?Controller\b", text):
            continue
        for p in paths:
            if f'"{p}"' in text:
                msgs.append(
                    f"{path} 가 계약 경로 {p} 를 다시 매핑함 — 계약 API 매핑은 {JAVA_CONTROLLER} 한곳 (같은 경로 두 매핑이면 기동 실패)"
                )
    return msgs


def _skeleton_kept(
    o: Overlay, template: Template, contract: TaskContract | None
) -> list[StaticProblem]:
    """코드 뼈대(docs/design/code-skeleton.md)를 유지했는가 — 이 오버레이가 쓴 뼈대 파일만 본다 (쓰지 않은 파일은 시작 코드·뼈대 판).
    API 호출 함수의 이름·매개변수, 화면 컴포넌트 이름, App.tsx 의 화면 경로 → 컴포넌트 라우트."""
    if contract is None or not template.manifest.skeleton:
        return []
    written = {f.path: f.content for f in o.files}
    msgs: list[str] = []
    if template.manifest.stack is not None and template.manifest.stack.language == "java":
        return [StaticProblem(o.target, m) for m in _java_skeleton_kept(written, contract)]
    if ENDPOINTS in written:
        text = written[ENDPOINTS]
        for fn in endpoint_fns(contract):
            m = re.search(
                rf"export\s+(?:async\s+)?function\s+{re.escape(fn.name)}\s*\(([^)]*(?:\{{[^}}]*\}}[^)]*)*)\)",
                text,
            )
            if m is None:
                msgs.append(
                    f"{ENDPOINTS} 에 계약 API 호출 함수 {fn.name} 가 없음 (이름을 바꾸지 않는다)"
                )
            elif tuple(_split_params(m.group(1))) != fn.params:
                msgs.append(
                    f"{ENDPOINTS} 의 {fn.name} 매개변수가 뼈대와 다름: ({', '.join(_split_params(m.group(1)))}) — 뼈대는 ({', '.join(fn.params)})"
                )
    names = page_names(contract)
    for sc in contract.screens:
        path = f"{PAGES}/{names[sc.screen_id]}.tsx"
        if path in written and not re.search(
            rf"export\s+default\s+function\s+{names[sc.screen_id]}\b", written[path]
        ):
            msgs.append(
                f"{path} 가 화면 컴포넌트 {names[sc.screen_id]} 를 export default 하지 않음 (이름을 바꾸지 않는다)"
            )
    if APP in written:
        for route, comp in skeleton_routes(contract):
            if f'path="{route}"' not in written[APP] or f"<{comp}" not in written[APP]:
                msgs.append(
                    f"{APP} 에 뼈대 라우트 {route} → {comp} 가 없음 (화면 경로·컴포넌트를 유지한다)"
                )
    rule = " — 코드 뼈대: 함수·컴포넌트 이름과 매개변수, 화면 라우트는 그대로 두고 몸통만 채운다 (도우미 함수·파일은 더해도 된다)"
    return [StaticProblem(o.target, m + rule) for m in msgs]


def _contract_types_usage(
    o: Overlay, t: Template, contract: TaskContract | None
) -> list[StaticProblem]:
    """앱 코드가 서버 응답을 계약에서 코드가 만든 타입(src/api/contract-types.ts)으로 읽는가 — TypeScript 템플릿만.
    2026-11-10 알파 분류: 따로 생성된 조각이 같은 응답을 다르게 읽은 실패가 가장 컸다 (fefix e99cbba0 시작 코드 api 모듈이
    배열 응답을 `{ items }` 로 읽음). 같은 타입을 쓰게 하면 tsc 가 모양 차이를 잡는다."""
    if contract is None:
        return []
    types = render_app_types(contract, t.manifest)
    if types is None:
        return []
    names = app_type_names(contract)
    decl = re.compile(rf"\bexport\s+(?:interface|type)\s+({'|'.join(map(re.escape, names))})\b")
    out: list[StaticProblem] = []
    for f in o.files:
        if (
            f.path == types.path
            or not f.path.endswith((".ts", ".tsx"))
            or ".test." in f.path
            or is_mock_server(f, t)
        ):
            continue
        redefined = sorted(set(decl.findall(f.content)))
        inline = bool(_JSON_INLINE.search(f.content))
        reads = "fetch(" in f.content and ".json()" in f.content
        missing = reads and not _TYPES_IMPORT.search(f.content)
        if redefined or inline or missing:
            why = []
            if redefined:
                why.append(f"계약 타입을 다시 정의함({', '.join(redefined)})")
            if inline:
                why.append("응답 본문에 모양을 직접 적음")
            if missing:
                why.append("응답을 읽는데 계약 타입을 import 하지 않음")
            out.append(
                StaticProblem(
                    o.target,
                    f"{f.path}: {'; '.join(why)} — 서버 응답은 {types.path}(잠김, 계약에서 코드가 만든 타입)의 "
                    "`<Api>Ok` 로 읽는다: `import type { … } from '…/contract-types'`, `const data: <Api>Ok = await res.json()`. "
                    "응답이 배열이면 배열 그대로 (감싼 객체로 읽지 않는다)",
                )
            )
    return out


def _starter_schema(o: Overlay, t: Template, contract: TaskContract | None) -> list[StaticProblem]:
    """시작 코드 스키마가 코드가 쓸 시드와 맞는가 (starter/seed_sql.schema_problems) — 맞지 않으면 기동이 깨진다."""
    rule = t.manifest.seed_sql
    if contract is None or rule is None:
        return []
    schema = next((f.content for f in o.files if f.path == rule.schema_path), None)
    return [StaticProblem("gen_starter", p) for p in schema_problems(schema, contract, t.manifest)]


def _starter_new_api_tests(
    o: Overlay, t: Template, contract: TaskContract | None, spec: SpecView
) -> list[StaticProblem]:
    """시작 코드의 기존 기능 테스트가 새로 만드는 API 를 부르면 새 기능을 구현한 것 — 새 API 는 지원자 몫이라 시작 코드에서 자리 표시다.
    이미 있는 API 가 하나도 없으면 판단하지 않는다 (무엇이 기존인지 모름)."""
    patterns = t.manifest.starter_test_calls
    if contract is None or not patterns:
        return []
    existing = existing_endpoints(contract, spec)
    new = [e for e in contract.endpoints if e.method and e not in existing]
    if not existing or not new:
        return []
    new_rx = _endpoint_rx(new)
    out: list[StaticProblem] = []
    for f in o.files:
        if "/test/" not in f"/{f.path}":
            continue
        calls = sorted(
            {
                f"{method} {path}"
                for method, path in http_calls(f.content, patterns)
                if any(method == m and rx.match(path) for m, rx in new_rx)
            }
        )
        if calls:
            out.append(
                StaticProblem(
                    "gen_starter",
                    f"시작 코드의 기존 기능 테스트 {f.path} 가 새로 만드는 API 를 부름: {', '.join(calls)} — 새 API 는 지원자 몫이라 "
                    "시작 코드에서는 자리 표시(TODO)다. 기존 기능 테스트는 이미 있는 API("
                    + ", ".join(f"{e.method} {e.path}" for e in existing)
                    + ")의 공개 요구 전 동작만 확인한다",
                )
            )
    return out


_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def _starter_issued_calls(
    o: Overlay, contract: TaskContract | None, spec: SpecView
) -> list[StaticProblem]:
    """시작 코드의 기존 기능 테스트가, 새로 만드는 발급 API(로그인)가 주는 값이 필요한 기존 API 를 부름 — 시작 코드에는 발급 API 가
    없어 통과하는 값을 만들 수 없고, 새 기능을 구현하면 그 요청은 거절된다 (기존 기능 테스트가 구현마다 깨진다). 새 API 를 부르는
    것은 _starter_new_api_tests 가 본다 — 여기는 계약에서 도출한 '시작 코드가 부를 수 없는 기존 API'.
    2026-11-24 세션 알파: 기존 API /api/items·/api/orders 를 X-Session-Id: dummy-session 으로 불러 200 을 단언 — 시작 코드는 통과,
    참조 구현 3개는 모두 401 이라 '계약 모호 의심'으로 빌드가 멈췄다. 부르는지는 계약이 선언한 그 헤더 이름 글자와 그 엔드포인트
    경로 글자로 본다 (manifest 의 MockMvc 모양 밖 HttpClient·URI 로 부르는 것도)."""
    if contract is None:
        return []
    locked = [
        (x.endpoint, x.rule, x.issuer)
        for x in locked_existing(contract, existing_endpoints(contract, spec))
    ]
    if not locked:
        return []
    out: list[StaticProblem] = []
    for f in o.files:
        if "/test/" not in f"/{f.path}":
            continue
        literals = [m.group(1).split("?", 1)[0] for m in _STRING.finditer(f.content)]
        hits = sorted(
            {
                f"{e.method} {e.path}"
                for e, rule, _ in locked
                if rule.header in literals
                or any(
                    re.search(re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(e.path)) + "/?$", lit)
                    or ("{" in e.path and lit.endswith(e.path.split("{", 1)[0]))
                    for lit in literals
                )
            }
        )
        if hits:
            _, rule, src = next(x for x in locked if f"{x[0].method} {x[0].path}" in hits)
            out.append(
                StaticProblem(
                    "gen_starter",
                    f"시작 코드의 기존 기능 테스트 {f.path} 가 {', '.join(hits)} 를 부름 — 이 API 는 새로 만드는 {src.method} {src.path} 가 "
                    f"발급하는 {rule.header} 가 필요하다. 시작 코드에는 발급 API 가 없어 통과하는 값을 만들 수 없고, 새 기능을 구현하면 "
                    f"이 요청은 {rule.denied_status} 이 된다 — 기존 기능 테스트는 이 API 를 부르지 않는다 (시드·저장소 수준으로 확인)",
                )
            )
    return out


# 저장소 조회에 넘긴 글자 값 — findByUsername("user1")·existsByEmail("…")·findAllByStatus("READY") (그룹 1 = 칸, 2 = 값)
_FINDER_LITERAL = re.compile(
    r"\.(?:find|exists|count|delete)(?:All|First|Top\d*)?By([A-Z][A-Za-z0-9]*?)(?:IgnoreCase)?"
    r"\(\s*\"((?:[^\"\\]|\\.)*)\"\s*\)"
)


def _natural(value: str) -> list[object]:
    """'username-2' 가 'username-10' 앞에 오게 — 실제 값 예를 행 순서대로 보이려고."""
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", value)]


@dataclass(frozen=True)
class SeedLiteralMiss:
    """저장소 조회에 넘긴 글자 값이 렌더된 시드에 없음 — path 의 call 이 field 를 literal 로 찾는다 (sample = 실제 값 예)."""

    path: str
    call: str
    field: str
    literal: str
    sample: str


def seed_literal_misses(
    files: Iterable[GeneratedFile], contract: TaskContract | None
) -> list[SeedLiteralMiss]:
    """테스트 파일이 저장소 조회에 넘긴 글자 값 중 렌더된 시드에 없는 것 (seed_sql.seed_column_values — render_seed_sql 과 같은 규칙).
    정적 검사로 막지 않는다 — '없는 값을 찾으면 비어 있다' 는 정상 테스트도 같은 꼴이라 의도를 알 수 없다 (2026-11-25 운영 b5:
    findBySessionId("nonexistent-session-xyz") 를 막아 시작 코드 생성이 실패). 기존 테스트가 실제로 떨어졌을 때 ② 수리 안내에만 쓴다.
    2026-11-25 prod-b4: findByUsername("user1") — 시드의 username 은 'username-<id>' 였는데 다시 만들어도 같은 테스트를 써 예산을 다 썼다."""
    if contract is None or not contract.seed:
        return []
    fields = {f.name.lower(): f.name for m in contract.entities for f in m.fields}
    values: dict[str, set[str]] | None = None
    out: list[SeedLiteralMiss] = []
    for f in files:
        if "/test/" not in f"/{f.path}" or not f.path.endswith(".java"):
            continue
        for m in _FINDER_LITERAL.finditer(f.content):
            name = fields.get(m.group(1).lower())
            if name is None:
                continue  # 칸 이름이 아닌 조회 (중첩 경로·여러 칸 조건)
            if values is None:
                values = seed_column_values(contract)
            literal = m.group(2)
            if name not in values:
                continue
            if "IgnoreCase(" in m.group(0):
                found = literal.lower() in {v.lower() for v in values[name]}
            else:
                found = literal in values[name]
            if not found:
                sample = ", ".join(f"'{v}'" for v in sorted(values[name], key=_natural)[:3])
                miss = SeedLiteralMiss(f.path, m.group(0).lstrip("."), name, literal, sample)
                if miss not in out:
                    out.append(miss)
    return out


def _unknown_calls(
    files: Iterable[GeneratedFile], contract: TaskContract, patterns: Sequence[str]
) -> list[str]:
    api = _contract_api(contract)
    out: list[str] = []
    for f in files:
        bad = sorted(
            {
                f"{method} {path}"
                for method, path in http_calls(f.content, patterns)
                if not any(method == m and rx.match(path) for m, rx in api)
            }
        )
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 가 공개 계약에 없는 API 를 부름: {', '.join(bad)} "
                f"— 계약 API({', '.join(e.method + ' ' + e.path for e in contract.endpoints if e.method)})만 쓴다"
            )
    return out


def _query_overloads(contract: TaskContract) -> str:
    """재요청 문구용 — 쿼리 매개변수 오버로드의 모양 (예: getOrders(after, size))."""
    shapes: list[str] = []
    for e, name in api_functions(contract):
        if e.query:
            args = [*_PATH_VAR.findall(e.path), *(f.name for f in e.query)]
            shapes.append(f"{name}({', '.join(args)})")
    return ", ".join(shapes)


_PATH_VAR = re.compile(r"\{(\w+)\}")


def _hand_built_query(
    files: Iterable[GeneratedFile], contract: TaskContract, patterns: Sequence[str], who: str
) -> list[str]:
    """테스트가 경로 문자열에 쿼리(`?…`)를 손으로 붙임 — 도우미의 쿼리 매개변수 오버로드를 쓰게 한다 (값 인코딩·빠진 칸을 도우미가 맡음).
    2026-11-12 행마다 조회 알파: `send("GET", "/orders?size=100", …)` 를 기본 크기 호출 `getOrders()` 와 섞어 허용 구현까지 떨어짐."""
    shapes = _query_overloads(contract)
    out: list[str] = []
    for f in files:
        bad = sorted(
            {
                _call_arg(f.content, m.end()).strip()[:80]
                for pat in patterns
                for m in re.finditer(pat, f.content)
                if any(
                    (lit := _LITERAL.match(p)) is not None and "?" in lit.group(1)
                    for p in _split_plus(_call_arg(f.content, m.end()))
                )
            }
        )
        if bad:
            how = (
                f"계약 API 함수의 쿼리 매개변수 오버로드({shapes})를 쓴다 — 계약 순서, 보내지 않을 칸은 null"
                if shapes
                else "계약에 쿼리 매개변수가 없는 API 다 — 계약에 없는 칸을 보내지 않는다"
            )
            out.append(f"{who} {f.path} 가 쿼리 문자열을 손으로 씀: {', '.join(bad[:3])} — {how}")
    return out


_TIMED_CALL = re.compile(r"\bBurst\.(?:fire|rounds)\s*\(|(?<![\w.])peak\s*\(")
_BRANCH = re.compile(r"\bif\s*\(|\bswitch\s*\(|\s\?\s")


def _paren_body(text: str, open_idx: int) -> str:
    """text[open_idx] 의 `(` 와 짝인 `)` 사이 — 문자열 안 괄호는 건너뛴다."""
    depth, i, quote = 0, open_idx, False
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
        i += 1
    return text[open_idx + 1 :]


def _timed_requests(
    files: Iterable[GeneratedFile], contract: TaskContract, patterns: Sequence[str]
) -> list[str]:
    """시간을 재는 요청(Burst.fire·Burst.rounds·peak) 안에서 요청을 둘 이상 보냄 — 무엇을 쟀는지 알 수 없다 (분기로 하나만 보내는 것은 둔다).
    세는 것: 템플릿 http_calls(send·get) + 도우미의 계약 API 함수 (2026-11-12 행마다 조회 알파: 한 lambda 안에서 getOrders() 와
    send("GET", "/orders?size=100") 를 함께 불러 시간 한도 안에 목록을 두 번 받아야 했다)."""
    calls = [re.compile(p) for p in patterns] + [
        re.compile(rf"(?<![\w.])(?:this\.)?{re.escape(name)}\s*\(")
        for _, name in api_functions(contract)
    ]
    out: list[str] = []
    for f in files:
        for m in _TIMED_CALL.finditer(f.content):
            inner = _paren_body(f.content, m.end() - 1)
            if _BRANCH.search(inner):
                continue
            hits = [h.group(0).split("(")[0].strip() for rx in calls for h in rx.finditer(inner)]
            if len(hits) > 1:
                timed = m.group(0).rstrip("( ").strip()
                out.append(
                    f"숨은 테스트 {f.path}: 시간을 재는 {timed}(…) 안에서 요청을 {len(hits)}번 보냄 ({', '.join(hits)}) — "
                    "재는 요청은 하나만. 데우기·결과 확인 요청은 Burst 밖에서 따로 보낸다"
                )
                break
    return out


def _starter_tables(o: Overlay, t: Template, contract: TaskContract | None) -> list[StaticProblem]:
    """시작 코드가 선언한 테이블(CREATE TABLE·@Table 등)은 계약이 정한 이름이어야 한다 — 숨은 테스트가 그 이름으로 오류를 주입한다."""
    naming = t.manifest.naming.table
    if contract is None or naming is None or not naming.declare_patterns:
        return []
    expected = contract_tables(contract, naming)
    if not expected:
        return []
    names = {m.entity_id: m.name for m in contract.entities}
    # 발급 값(로그인이 주는 sessionId 등)을 담는 테이블은 계약 밖이 맞다 — 저장소는 지원자 설계 몫이라 계약에서 뺐고
    # (auth_rule.fit_issued_store), 테스트는 HTTP 로만 판정해 이 이름으로 오류를 주입하지 않는다 (이 검사의 목적 밖). 시작 코드(기존 시스템)도
    # 자기 저장소를 둘 수 있다. 2026-11-25 #322 알파: 시작 코드가 기존 API 의 세션 확인에 쓸 session 테이블을 만들어 두 번 막혀 생성 실패.
    issued = {
        e.auth.issued_field
        for e in contract.endpoints
        if e.auth is not None and e.auth.kind == "issued" and e.auth.issued_field
    }
    stores = tables_with_fields(o.files, issued)
    stray = [
        d
        for d in declared_tables(o.files, naming.declare_patterns)
        if not any(same_table(d, e, naming) for e in expected.values())
        and not any(same_table(d, s, naming) for s in stores)
    ]
    if not stray:
        return []
    want = ", ".join(f"{names[i]} → {n}" for i, n in expected.items())
    return [
        StaticProblem(
            "gen_starter",
            f"시작 코드의 테이블 {', '.join(stray)} 이 계약 테이블 밖 — 계약이 정한 이름만 쓴다 ({want})",
        )
    ]


_RELATION_ANNOTATIONS = {"manytoone", "onetoone"}


def _starter_foreign_keys(
    o: Overlay, t: Template, contract: TaskContract | None
) -> list[StaticProblem]:
    """시작 코드는 계약에 없는 외래 키를 만들지 않는다 (계약 엔티티 foreign_keys 만).
    2026-11-07 루프: 계약에 관계가 없는데 시작 코드가 감사 기록 → 배송 건 외래 키를 걸어, 계약이 말하는 '없는 배송 건 요청도 기록'이 DB 에서 거부됐다."""
    naming = t.manifest.naming.table
    if contract is None or naming is None or not naming.fk_patterns:
        return []
    allowed = {fk_field.lower() for m in contract.entities for fk_field, _ in m.fk_pairs()}
    bad: list[str] = []
    for f in o.files:
        for pat in naming.fk_patterns:
            for mt in re.finditer(pat, f.content, re.I):
                name = mt.group(1)
                ok = (
                    bool(allowed)
                    if name.lower() in _RELATION_ANNOTATIONS
                    else name.replace("_", "").lower() in allowed
                )
                if not ok:
                    bad.append(f"{f.path}: {name}")
    if not bad:
        return []
    declared = (
        ", ".join(f"{m.name}.{a} → {b}" for m in contract.entities for a, b in m.fk_pairs())
        or "없음"
    )
    return [
        StaticProblem(
            "gen_starter",
            f"시작 코드가 계약에 없는 외래 키를 둠: {'; '.join(sorted(set(bad)))} — 계약의 외래 키({declared})만 둔다 "
            "(없는 대상의 요청도 기록하는 테이블에 외래 키를 걸면 그 기록이 거부된다)",
        )
    ]


_CONST_JOIN = re.compile(r'"\s*\+\s*(TABLE_[A-Z0-9_]+)\s*\+\s*"')
_CONST_TAIL = re.compile(r'"\s*\+\s*(TABLE_[A-Z0-9_]+)\b')
_CONST_HEAD = re.compile(r'\b(TABLE_[A-Z0-9_]+)\s*\+\s*"')


def inline_table_constants(content: str, consts: dict[str, str]) -> str:
    """`"^\\s*update\\s+" + TABLE_ORDER + "\\b"` → `"^\\s*update\\s+orders\\b"` — 계약 도우미의 테이블 상수를 문자열에 끼워 넣어
    문자열 상수만 보는 검사(오류 주입 정규식의 테이블·넓은 주입)가 이어 붙인 정규식도 읽게 한다.
    2026-11-08: ContractCase 가 TABLE_ 상수를 주자 숨은 테스트가 그것을 이어 붙였고, 첫 조각만 본 검사가 '넓은 주입'으로 오탐해 생성 실패."""
    if not consts:
        return content

    def val(m: re.Match[str]) -> str:
        return consts.get(m.group(1), m.group(1))

    content = _CONST_JOIN.sub(val, content)
    content = _CONST_TAIL.sub(lambda m: val(m) + '"', content)
    return _CONST_HEAD.sub(lambda m: '"' + val(m), content)


def _hidden_tables(
    files: Iterable[GeneratedFile], t: Template, contract: TaskContract
) -> list[str]:
    naming, rules = t.manifest.naming.table, t.manifest.hidden_rules
    if naming is None or not rules.table_refs:
        return []
    tables = contract_tables(contract, naming)
    names = {m.entity_id: m.name for m in contract.entities}
    consts = {f"TABLE_{spring_snake(names[e]).upper()}": v for e, v in tables.items()}
    expected = list(tables.values())
    files = [
        GeneratedFile(path=f.path, content=inline_table_constants(f.content, consts)) for f in files
    ]
    out: list[str] = []
    for f in files:
        bad = [
            r
            for r in referenced_tables(f.content, rules.table_refs, rules.string_literal)
            if not any(same_table(r, e, naming) for e in expected)
        ]
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 의 오류 주입 정규식이 계약에 없는 테이블을 가리킴: {', '.join(bad)} "
                f"— 계약의 테이블 이름({', '.join(expected) or '없음'})을 그대로 쓴다 (경로에서 짐작하지 않는다)"
            )
        broad = _broad_injections(f.content, rules.table_refs, rules.string_literal, expected)
        if broad:
            out.append(
                f"숨은 테스트 {f.path} 의 오류 주입 정규식이 SQL 종류 없이 넓음: {'; '.join(broad)} — "
                "`^\\s*update\\s+<테이블>\\b` 처럼 SQL 종류 + 계약 테이블로 시작하고, 숨은 요구가 말하는 그 단계만 주입한다 "
                "(테이블 이름만 쓰면 실패 기록 쓰기까지 막혀 어떤 설계도 통과 못 한다)"
            )
    return out


# 그대로 두는 단언: null·형식 확인. 기대값만 Integer 로 맞춘 비교(`.intValue()`)는 면제하지 않는다 — 큰 값(Long)에서 같은 오류이고 같은 문장의
# 다른 필드 단언까지 면제됐다 (#178, 2026-11-07 배송 감사 '작은 id 라 동작'). 실제값을 캐스트한 꼴은 매니페스트 패턴에 맞지 않아 여기 오지 않는다
_NULL_CHECK = re.compile(r"\.is(?:Not)?Null\(\)|\.isInstanceOf\(")


def _raw_number_asserts(
    files: Iterable[GeneratedFile], patterns: Sequence[str], contract: TaskContract
) -> list[str]:
    """계약에서 정수 형식인 필드를 JSON Map 값 그대로 단언 — 크기에 따라 Integer·Long 이라 long 과 비교하면 모든 구현이 실패.
    null 확인(isNull·isNotNull)·형식 확인(isInstanceOf)·기대값을 Integer 로 맞춘 비교(intValue)는 그대로 둔다."""
    # 숫자 칸(정수·number) — JSON 의 정수 꼴 값은 선언 형식과 무관하게 Integer·Long 으로 읽힌다. 목록(long[])은 Map 값이 List 라 아니다.
    # 형식 판정은 계약 형식 해석 한 곳 (#172 — smallint·int64 가 빠지고 long[] 이 들어갔다)
    numeric = {
        f.name
        for _, f in contract_fields(contract)
        if non_null(parse_field(f.type)).kind in ("integer", "number")
    }
    if not numeric:
        return []
    out: list[str] = []
    for f in files:
        hits: list[tuple[str, str]] = []  # (단언 앞부분, 필드 — 매니페스트 패턴의 그룹 1)
        for pat in patterns:
            for m in re.finditer(pat, f.content):
                end = f.content.find(";", m.end())
                tail = f.content[m.end() : end if end >= 0 else None]
                if m.group(1) in numeric and not _NULL_CHECK.search(tail):
                    hits.append((m.group(0), m.group(1)))
        if hits:
            shown = "; ".join(dict.fromkeys(h for h, _ in hits))
            name = hits[0][1]
            out.append(
                f"숨은 테스트 {f.path} 가 숫자 필드를 JSON Map 값 그대로 단언함: {shown} — JSON 숫자는 크기에 따라 "
                f'Integer·Long 으로 읽혀 long 과 비교하면 모든 구현이 실패한다. ((Number) x.get("{name}")).longValue() 로 바꿔 비교한다'
            )
    return out


_SQL_WORDS = re.compile(r"\b(?:update|insert|delete|select|into|from)\b", re.I)


def _broad_injections(
    content: str, table_refs: Sequence[str], literal: str, tables: Sequence[str]
) -> list[str]:
    """SQL 을 가리키는 정규식(계약 테이블 이름이나 SQL 단어가 든 것)인데 'SQL 종류 + 테이블' 꼴로 시작하지 않는 것.
    2026-11-07 루프: 다시 만든 숨은 테스트가 `failCommitAfter = "delivery|delivery_audit_log"` 로 실패 기록 INSERT 의 커밋까지 막았다."""
    out: list[str] = []
    for lit in re.finditer(literal, content):
        text = lit.group(1)
        sqlish = _SQL_WORDS.search(text) or any(t.lower() in text.lower() for t in tables)
        if (
            sqlish
            and not any(re.search(pat, text, re.I) for pat in table_refs)
            and f'"{text}"' not in out
        ):
            out.append(f'"{text}"')
    return out


_PEAK_CALL = re.compile(r"(?<![\w.])peak\s*\(")


def case_applied_tunes(m: TemplateManifest) -> dict[str, list[str]]:
    """사유 → 숨은 케이스가 직접 읽어 켜야 하는 보정 값 (#301·#308). 사유 tunes 중 운영 설정으로 자동으로 들어가는 값(config_keys)과
    채점이 밖에서 재는 시간 한도(hidden_rules.observable_params)는 하네스가 넣는다. 그 밖의 값(예: SQL 지연 query_delay_ms)은 케이스가
    읽어 적용해야 빌드 보정이 바꾼 값이 실제 부하가 된다 — 읽지 않으면 보정이 창을 못 찾고 엉뚱하게 흔한 구현을 다시 만든다."""
    observable = set(m.hidden_rules.observable_params)
    out: dict[str, list[str]] = {}
    for r in m.failure_reasons:
        names = [
            n
            for n in r.tunes
            if (p := m.param(n)) is not None and not p.config_keys and n not in observable
        ]
        if names:
            out[r.code] = names
    return out


def _reason_rules(
    cases: Iterable[GeneratedFile],
    m: TemplateManifest,
    plan: HiddenPlan,
    case_rx: re.Pattern[str],
    reason_rx: list[re.Pattern[str]],
) -> list[str]:
    """의도한 사유가 제대로 나오게 — (1) 사유를 붙이면 안 되는 단언(모든 실패를 세는 것)에 붙임, (2) 사유가 요구하는 것(오류 주입·운영 피크)을
    케이스가 쓰지 않음. 2026-11-08 숨은 테스트 검토: 사유를 Burst.wrong 에 붙임 14/14, 저장 실패 사유인데 주입 없음 4/5."""
    intended = {c.case_id: c.intended_reason for c in plan.cases}
    requires = {r.code: r.requires for r in m.failure_reasons if r.requires}
    reasons = {r.code: r for r in m.failure_reasons}
    applied = case_applied_tunes(m)
    peak = peak_reasons(m)  # 동시 요청으로만 재현되는 사유 — 버스트 없는 케이스 안내를 사유별로
    # 버스트 행이 없는 케이스 — 운영 피크(peak)를 쓰면 요청 하나의 조건이 아니라 동시 부하를 잰다
    single = {c.case_id for c in plan.cases if not any(d.burst for d in c.data)}
    # 사유를 붙여도 되는 단언 — 사유마다 선언(assert_on·assert_not_on) + 모든 사유 공통(hidden_rules.reason_not_on). 한 검사로 (#176 B)
    common_not = [re.compile(p) for p in m.hidden_rules.reason_not_on]
    out: list[str] = []
    for f in cases:
        for stmt in f.content.split(";"):
            codes = {mt.group(1) for rx in reason_rx for mt in rx.finditer(stmt) if mt.groups()}
            for code in sorted(codes):
                r = reasons.get(code)
                not_on = common_not + [re.compile(p) for p in (r.assert_not_on if r else [])]
                on = [re.compile(p) for p in (r.assert_on if r else [])]
                shown = " ".join(stmt.split())[:120]
                if any(rx.search(stmt) for rx in not_on):
                    out.append(
                        f"숨은 테스트 {f.path} 가 사유 {code} 를 붙이면 안 되는 단언에 붙임: {shown} — "
                        + (
                            m.hidden_rules.reason_not_on_note
                            or "그 단언은 이 사유가 아닌 실패도 센다"
                        )
                        + (f". {code}: {r.how}" if r and r.how else "")
                    )
                elif on and not any(rx.search(stmt) for rx in on):
                    out.append(
                        f"숨은 테스트 {f.path} 가 사유 {code} 를 그 사유를 보는 단언이 아닌 곳에 붙임: {shown}"
                        + (f" — {r.how}" if r and r.how else "")
                    )
        for cid in sorted(set(case_rx.findall(f.content))):
            code = intended.get(cid)
            need = requires.get(code or "")
            if need and not any(re.search(p, f.content) for p in need):
                # 일으키는 법은 사유의 how 한 곳 (#176: 하네스 클래스 이름으로 안내 둘 중 하나를 고르다 lost_update 에 엉뚱한 안내)
                how = reasons[code].how if code in reasons else None
                out.append(
                    f"숨은 케이스 {cid} (의도한 사유 {code}) 가 그 사유를 일으키는 것을 쓰지 않음 — 필요한 것: {' 또는 '.join(need)}"
                    + (f". {how}" if how else "")
                )
            for name in applied.get(code or "", []):
                if f'"{name}"' not in f.content:
                    # 2026-11-25 #308 팩트체크: 케이스가 지연을 켜지 않으면 보정이 지연을 바꿔도 부하가 그대로라 '보정 불가 → 흔한 구현 재생성'
                    out.append(
                        f"숨은 케이스 {cid} (의도한 사유 {code}) 가 조절값 {name} 을 읽지 않음 — 빌드가 이 값을 바꿔 흔한 구현과 "
                        f'허용 구현을 가르므로, 하네스 문서대로 "{name}" 를 읽어 적용하는 줄을 이 케이스에 둔다'
                    )
            if cid in single and _PEAK_CALL.search(f.content) and code in peak:
                # 2026-11-25 #308 팩트체크 3차: 아래 안내(Burst.fire(1, …))가 사유를 보지 않아, 동시 요청으로만 재현되는 사유에도 요청 한 건을
                # 권했다 — 그 사유의 how('peak(…) 로 재현')와 정반대. 계획에 버스트 행이 없는 것이 원인이라 숨은 테스트로는 고칠 수 없다
                # (계약 단계 seed_rows.case_rows_problems 가 거르므로 보통 여기까지 오지 않는다)
                out.append(
                    f"숨은 케이스 {cid} (의도한 사유 {code}) 는 동시 요청으로만 재현되는데 계약 케이스 data 에 버스트 행이 없어 peak(...) 를 "
                    "쓸 수 없음 — 숨은 테스트로 고칠 수 없는 계약 문제다 (케이스 data 에 버스트 행 필요). Burst.fire(1, …) 로 바꾸지 않는다 — "
                    "요청 한 건으로는 이 사유가 나오지 않는다"
                )
            elif cid in single and _PEAK_CALL.search(f.content):
                # 2026-11-10 nplus1-g: '목록 한 번 조회가 운영 지연에서 1초 안' 케이스를 peak(…) 로 동시 버스트해,
                # 조인 조회 허용 구현까지 연결·스레드 대기로 시간 초과 — 흔한 구현과 가르지 못했다
                out.append(
                    f"숨은 케이스 {cid} 는 버스트 행이 없는 케이스인데 peak(...) 로 동시 부하를 보냄 — 요청 하나의 조건(지연 주입·시간 한도)을 "
                    "잰다면 지연 없이 같은 요청을 한 번 보내 데운 뒤, 지연을 주입하고 Burst.fire(1, …) 로 한 번 보내 Burst.failures(…) 에 사유를 "
                    "붙인다 (동시 요청은 계약 케이스 data 가 버스트일 때만)"
                )
    return out


# 상태를 바꾸는 요청(계약 API 함수 patch…·post…·put…·delete…)의 응답 — 그 응답을 담은 변수, 또는 그 호출 그대로
_MUTATING_VAR = re.compile(r"\b(\w+)\s*=\s*(?:patch|post|put|delete)[A-Z]\w*\s*\(")
_MUTATING_INLINE = re.compile(r"assertThat\(\s*(?:patch|post|put|delete)[A-Z]\w*\s*\(")
_RESET = re.compile(r"GraderJdbcHarness\.reset\(\)")
# 응답 코드 단언의 매처 — 계약이 선언한 5xx 와 같다(isEqualTo·isIn·isOneOf)만 둔다. 그 밖의 매처는 모두 단정 (#178: 위로 막힘·'성공이
# 아님' 매처를 목록 둘로 따로 세던 것을 '허용 매처 밖이면 단정' 하나로)
_STATUS_IS = re.compile(r"\.(?:isEqualTo|isIn|isOneOf)\(([^()]*)\)")
_MATCHER = re.compile(r"\.(is[A-Z]\w*|has[A-Z]\w*|matches|satisfies|isTrue|isFalse)\(")
_DECLARED_MATCHERS = frozenset({"isEqualTo", "isIn", "isOneOf"})


def _injected_status_asserts(
    cases: Iterable[GeneratedFile], m: TemplateManifest, contract: TaskContract
) -> list[str]:
    """저장 실패를 주입한 케이스가 응답 코드를 특정 값·범위로 단정함 — 계약은 저장 실패 때의 응답을 정하지 않는다.
    2026-11-09 알파 (2df170e2): hc2 가 커밋 실패 주입 뒤 `statusCode() < 500` 을 rollback_left_success 사유로 단언해,
    실패를 그대로 알린(500) 흔한 구현·허용 구현 모두 같은 줄에서 떨어졌다 (두 회차). 계약이 선언한 5xx 와 같다고 보는 것은 둔다.
    2026-11-12 코드 리뷰(PR #121): '성공이 아님'(isNotEqualTo(200)·isGreaterThanOrEqualTo(400))도 단정이다 — 응답을 먼저 주고 뒤에
    저장·기록하는 허용 설계는 주입해도 2xx 라 떨어진다. gen_hidden v6 과 같이 응답 코드는 아예 보지 않게 한다."""
    inject = [
        re.compile(p)
        for r in m.failure_reasons
        if "GraderJdbcHarness" in "".join(r.requires)
        for p in r.requires
    ]
    if not inject:
        return []
    declared = {r.status for e in contract.endpoints for r in e.responses if r.status >= 500}
    out: list[str] = []
    for f in cases:
        start = min((mt.start() for rx in inject if (mt := rx.search(f.content))), default=None)
        if start is None:
            continue
        # 주입부터 reset 까지 보낸 상태 변경 요청만 — 그 뒤 기록을 읽는 GET 의 200 단언은 맞다
        # (2026-11-09 알파 재실행: 처음 검사가 주입 뒤 감사 조회 `auditResp.statusCode()).isEqualTo(200)` 까지 잡아 숨은 테스트 생성 실패)
        end = r.start() if (r := _RESET.search(f.content, start)) else len(f.content)
        window = f.content[start:end]
        injected = {mt.group(1) for mt in _MUTATING_VAR.finditer(window)}
        for stmt in window.split(";"):
            if "statusCode()" not in stmt or not (
                _MUTATING_INLINE.search(stmt)
                or any(
                    # 응답 코드로 시작하는 단언 전부 — `assertThat(r.statusCode() < 500).isTrue()` 도 (#178: 예전 사례 꼴이 빠져 있었다)
                    re.search(rf"assertThat\(\s*{re.escape(v)}\.statusCode\(\)", stmt)
                    for v in injected
                )
            ):
                continue
            # 계약이 선언한 5xx 와 같다고만 보는 단언은 둔다 — 그 밖의 값·범위·'성공이 아님'은 모두 단정
            codes = [
                {int(v) for v in re.findall(r"\b\d{3}\b", mt.group(1))}
                for mt in _STATUS_IS.finditer(stmt)
            ]
            matchers = set(_MATCHER.findall(stmt))
            declared_only = (
                bool(codes)
                and matchers <= _DECLARED_MATCHERS
                and all(c and c <= declared for c in codes)
            )
            if not declared_only:
                out.append(
                    f"숨은 테스트 {f.path} 가 저장 실패를 주입한 요청의 응답 코드를 단정함: {' '.join(stmt.split())[:120]} — "
                    "계약은 저장 실패 때의 응답을 정하지 않는다. 실패를 그대로 알린(5xx) 구현도, 응답을 먼저 주고 뒤에 저장하는(2xx) "
                    "허용 설계도 있다. 주입한 요청의 응답 코드는 보지 않고(isNotEqualTo(200)·isGreaterThanOrEqualTo(400) 도 쓰지 않음), "
                    "판정과 사유는 그 행의 상태·성공 기록을 조회해 보는 단언에 붙인다"
                )
                break
    return out


def _call_args(text: str, start: int) -> list[str]:
    """`f(` 다음 위치에서 닫는 괄호까지 맨 바깥 쉼표로 나눈 인자들 (글자·괄호 안 쉼표는 나누지 않는다)."""
    args, depth, cur, quote = [], 0, "", ""
    for ch in text[start:]:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return [*args, cur.strip()] if cur.strip() or args else []


def _hand_built_login(
    cases: Sequence[GeneratedFile], contract: TaskContract, label: str
) -> list[str]:
    """발급 API(로그인) 요청 본문을 손으로 만듦 — 자격 증명은 시드 값이라 짐작하면 모든 구현에서 거절된다. 도우미가 loginBody(row) 를 준다.
    2026-11-24 세션 알파: 공개 4개·숨은 1개 테스트가 모두 "user-"+id·"pass-"+id 로 지어 401. 계약이 선언한 발급 API 요청 칸 이름이
    모두 글자로 나오면 손으로 만든 본문으로 본다 — 낱말 목록이 아니라 계약의 칸 이름, 응답 단언에 나오는 칸 하나는 잡지 않게 '모두'.
    loginBody(…) 를 부르는 줄은 보지 않는다 (틀린 자격 증명 시험의 loginBody(row, "password", "wrong"))."""
    rule = issued_rule(contract)
    src = next(
        (e for e in contract.endpoints if rule is not None and e.endpoint_id == rule.issued_by),
        None,
    )
    if rule is None or src is None or not src.request or not credentials(contract, rule):
        return []
    names = [f.name for f in src.request]
    rxs = [re.compile(r'\\?"' + re.escape(n) + r'\\?"') for n in names]
    out: list[str] = []
    for f in cases:
        text = "\n".join(line for line in f.content.splitlines() if "loginBody(" not in line)
        if all(rx.search(text) for rx in rxs):
            out.append(
                f"{label} {f.path} 가 {src.method} {src.path} 요청 본문({', '.join(names)})을 손으로 만듦 — 자격 증명은 시드 값이라 "
                "짐작하면 모든 구현에서 거절된다. 도우미의 loginBody(row)(그 행의 자격 증명)·newSession(row)(그 행으로 로그인한 세션)을 "
                f'쓰고, 틀린 자격 증명은 loginBody(row, "{names[-1]}", "wrong") 로'
            )
    return out


def _hand_built_keys(
    cases: Sequence[GeneratedFile], contract: TaskContract, label: str
) -> list[str]:
    """도우미가 경로 키 값 함수(<필드>Of)를 주는데 계약 API 호출의 그 경로 변수 인자를 글자로 직접 만듦 — 시드 값 형식을 짐작한다.
    2026-11-09 v16-be2: 시드 'memberId-191', 테스트 "member-" + HC2_FIRST → 모든 구현에서 대상 없음.
    #178: 예전에는 `"member-" +` 꼴만 잡아 `"member" + n`·`String.format("member-%d", n)` 은 지나갔다 — 모양 대신 자리로 본다
    (키 필드 경로 변수 자리의 인자가 `<필드>Of(…)` 가 아니고 글자를 쓰면, 변수면 그 변수에 넣은 식을 한 번 따라가 본다)."""
    keys = {name for name, _, numeric in key_fields(contract) if not numeric}
    if not keys:
        return []
    calls: list[tuple[str, list[tuple[int, str]]]] = []
    for e, fn in api_functions(contract):
        vars_ = re.findall(r"\{([A-Za-z_]\w*)\}", e.path)
        slots = [(i, v) for i, v in enumerate(vars_) if v in keys]
        if slots:
            calls.append((fn, slots))
    out: list[str] = []
    for f in cases:
        bad: list[str] = []
        for fn, slots in calls:
            for m in re.finditer(rf"(?<![\w.]){re.escape(fn)}\s*\(", f.content):
                args = _call_args(f.content, m.end())
                for i, v in slots:
                    if i >= len(args):
                        continue
                    arg = args[i]
                    if re.fullmatch(r"[A-Za-z_]\w*", arg):
                        given = re.findall(rf"\b{re.escape(arg)}\s*=(?!=)\s*([^;]+);", f.content)
                        arg = given[-1] if given else arg
                    if f"{v}Of(" not in arg and ('"' in arg or "String.format" in arg):
                        bad.append(f"{fn} 의 {v} 자리 `{' '.join(args[i].split())[:40]}`")
        if bad:
            out.append(
                f"{label} {f.path} 가 경로 키 값을 직접 만듦 ({'; '.join(dict.fromkeys(bad))}) — "
                f"도우미의 {', '.join(sorted(k + 'Of(행 번호)' for k in keys))} 를 쓴다 (시드 값 형식을 짐작하지 않는다)"
            )
    return out


_OK_HEADERS_ARG = re.compile(r"\bok_?[Hh]eaders\(\s*([^()]*?)\s*\)")
_QUOTED = re.compile(r"^[\"']([^\"']*)[\"']$")


def _header_rejected(contract: TaskContract, name: str) -> bool:
    """계약이 그 헤더의 형식 오류·빈 값을 400 으로 정했나 — 그 거절 시험 값은 글자로 써도 둔다."""
    return any(
        r.status == 400 and name.lower() in (r.description or "").lower()
        for e in contract.endpoints
        if any(h.name == name for h in e.headers)
        for r in e.responses
    )


def _literal_value(arg: str, text: str) -> str | None:
    """헤더 값 자리의 글자 — 글자 그대로("req-001")이거나, 글자를 담은 변수(`String key = "req-001";` 뒤의 key).
    2026-11-12 실패 사례는 변수 꼴이었다 (`String reqId = "req-seq-001"; … okHeaders(reqId)`)."""
    if (q := _QUOTED.match(arg)) is not None:
        return q.group(1)
    if re.fullmatch(r"[A-Za-z_]\w*", arg):
        m = re.search(r"\b" + re.escape(arg) + r"\s*=\s*[\"']([^\"']*)[\"']\s*[;\n]", text)
        return m.group(1) if m else None
    return None


def _literal_request_keys(
    cases: Sequence[GeneratedFile], contract: TaskContract, name: str, label: str
) -> list[str]:
    """요청마다 새 값인 헤더(요청 번호·멱등 키)에 글자 값을 씀 — `okHeaders("req-001")`·`"X-Request-Id", "…"`·`"X-Request-Id": "…"`.
    케이스마다 따로 만든 테스트가 같은 글자를 쓰면 서로의 기록을 돌려받는다 (2026-11-12 백엔드 중복 처리: hc1·hc2 가 모두
    'req-seq-001' — 허용 구현이 hc2 의 기록을 hc1 에 돌려줘 '기대 15000, 실제 13000'). 계획에 적힌 글자도 예외로 두지 않는다
    (그 경우가 바로 계획에서 온 값). 계약이 그 헤더의 형식 오류를 400 으로 정했으면 거절 시험 값이라 둔다."""
    if _header_rejected(contract, name):
        return []
    named = re.compile(r'"' + re.escape(name) + r'"\s*[,:]\s*([^,)}\n]+)', re.I)
    out: list[str] = []
    for f in cases:
        args = [m.group(1).strip() for m in _OK_HEADERS_ARG.finditer(f.content)]
        args += [m.group(1).strip() for m in named.finditer(f.content)]
        vals = sorted({v for a in args if (v := _literal_value(a, f.content)) is not None})
        if vals:
            shown = ", ".join(f'"{v}"' for v in vals[:3])
            out.append(
                f"{label} {f.path} 가 요청마다 새 값인 헤더 {name} 에 글자 값을 씀 ({shown}) — `String key = newRequestId();` 처럼 "
                "도우미로 만든 변수를 okHeaders(key) 에 넘긴다 (같은 요청의 재시도는 그 변수를 다시 넘긴다). 계획에 적힌 요청 번호 "
                "글자도 쓰지 않는다 — 다른 케이스·공개 테스트와 같은 값이 되면 서로의 기록을 돌려받는다"
            )
    return out


def _literal_headers(
    cases: Sequence[GeneratedFile], contract: TaskContract, label: str
) -> list[str]:
    """도우미에 헤더 값 상수가 있는데 테스트가 계약에 없는 값을 직접 씀 (`"X-Requester-Id", "grader"`) — 통과하는 값도
    거절되는 값도 아니면 그 요청의 결과를 계약이 정하지 않는다 (2026-11-08 알파: 지어낸 값으로 모든 요청이 403).
    거절 값 상수가 없으면 통과 값이 아닌 값은 거절 시험일 수 있어 둔다. 계약 값 그대로면 글자로 써도 막지 않는다."""
    out: list[str] = []
    for h in header_values(contract):
        if h.source == PER_REQUEST:
            out += _literal_request_keys(cases, contract, h.name, label)
            continue
        if h.ok is None or h.denied is None:
            continue
        key = re.sub(r"[^A-Za-z0-9]+", "_", h.name).strip("_").upper()
        rx = re.compile(r'"' + re.escape(h.name) + r'"\s*,\s*"([^"]*)"', re.I)
        for f in cases:
            for mt in rx.finditer(f.content):
                v = mt.group(1)
                if v in (h.ok, h.denied):
                    continue
                out.append(
                    f'{label} {f.path} 가 헤더 {h.name} 값을 직접 씀 ("{v}") — 도우미의 HEADER_{key}_OK'
                    + f"·HEADER_{key}_DENIED 상수나 okHeaders() 를 쓴다 (계약이 정한 값)"
                )
    return out


_DIRECT_FAULT = re.compile(r"\.(?:fail|networkError)\(|\bapi\.once\(")


def _fault_calls(
    f: GeneratedFile, ids: Collection[str], plan: HiddenPlan, only: Collection[str] | None
) -> list[str]:
    """실패 지정이 있는 숨은 케이스 파일은 도우미 failHcN() 을 부르고, 실패를 따로 걸지 않는다 (TypeScript 도우미).
    2026-11-11 실패 지정 알파: hc2 가 지정을 waitForCall → respond/networkError 로 써 한꺼번에 보내는 구현에서만 예외."""
    out: list[str] = []
    for c in plan.cases:
        if not c.faults or c.case_id not in ids or (only is not None and c.case_id not in only):
            continue
        fn = fault_fn(c.case_id)
        if f"{fn}(" not in f.content:
            out.append(
                f"숨은 케이스 {c.case_id} 파일 {f.path} 이 실패 지정 도우미 {fn}() 을 부르지 않음 — 케이스 맨 앞(요청이 나가기 전)에 "
                f"`{fn}();` 을 한 번 부른다 (그 항목의 요청만 실패한다)"
            )
        direct = _DIRECT_FAULT.search(f.content)
        if direct:
            out.append(
                f"숨은 케이스 {c.case_id} 파일 {f.path} 이 실패를 직접 건다('{direct.group(0)}') — 실패는 {fn}() 이 건다. "
                "같은 경로 요청을 붙잡아 fail·networkError 로 답하면 한꺼번에 보내는 구현에서 붙잡힌 요청 모두에 같은 답이 간다"
            )
    return out


_CODE_NAME = re.compile(r"[A-Za-z_]\w*")  # 코드의 이름 (케밥 표기 없음 — 도우미 상수·함수 이름)


def _case_names(helper: str, ids: Collection[str]) -> set[str]:
    """계약 도우미에서 그 케이스들의 이름 — 케이스 id 를 대소문자와 상관없이 품은 이름(HC2_FIRST·failHc2 …).
    케이스 id 패턴(`\\bhc\\d{1,2}\\b`)은 소문자·단어 경계라 도우미 상수를 못 본다. 2026-11-19 운영 ct-d367bb7b2d4c-b2: hc1 파일이
    HC2_FIRST 의 행에 실패를 주입하고 정상 요청까지 보내, 뒤에 도는 hc2 가 남은 SUCCESS 기록으로 모든 구현에서 떨어졌다."""
    rx = [re.compile(rf"(?i)(?<![0-9]){re.escape(c)}(?![0-9])") for c in ids]
    return {n for n in _CODE_NAME.findall(helper) if any(r.search(n) for r in rx)}


def _hidden_cases(
    o: Overlay,
    t: Template,
    plan: HiddenPlan,
    contract: TaskContract | None = None,
    only: Collection[str] | None = None,
) -> list[StaticProblem]:
    """only: 케이스별 생성(generate.gen_hidden)에서 그 케이스만 — 계획의 다른 케이스가 없다고 하지 않는다."""
    m = t.manifest
    rules = m.hidden_rules
    out: list[str] = []
    imports = [re.compile(p, re.M) for p in rules.forbidden_imports]
    reason_rx = [re.compile(p) for p in rules.reason_patterns]
    case_rx = re.compile(rules.case_id_pattern)
    params_path = m.hidden_layout.params if m.hidden_layout else None
    helper = render_contract_case(contract, plan, m) if contract is not None else None
    helper_path = m.hidden_helper.path if m.hidden_helper else None
    plan_ids = {c.case_id for c in plan.cases}
    foreign = (
        _case_names(helper.content, plan_ids - set(only))
        if helper is not None and only is not None
        else set()
    )
    seen_ids: Counter[str] = Counter()
    used_codes: set[str] = set()
    for f in o.files:
        if f.path == params_path:
            if f.content != render_params(plan, m).content:
                out.append(
                    f"조절값 파일 {f.path} 를 손으로 씀 — 이 파일은 숨은 테스트에 넣지 않는다 (HiddenPlan 에서 코드가 쓴다)"
                )
            continue
        if f.path == helper_path:
            if helper is not None and f.content != helper.content:
                out.append(
                    f"계약 도우미 {f.path} 를 손으로 씀 — 이 파일은 숨은 테스트에 넣지 않는다 (계약에서 코드가 쓴다)"
                )
            continue
        if m.hidden_helper is not None and re.search(r"\bextends\s+GraderSupport\b", f.content):
            out.append(
                f"숨은 테스트 {f.path} 가 GraderSupport 를 직접 상속함 — {m.hidden_helper.class_name} 를 상속하고 그 범위 상수·계약 API 함수·"
                "peak() 를 쓴다 (id·경로·응답 파싱·시간 한도를 직접 쓰지 않는다)"
            )
        lines = f.content.splitlines()
        bad = [line.strip() for line in lines if any(rx.search(line) for rx in imports)]
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 가 지원자 구현 내부를 직접 씀: {'; '.join(bad[:3])} — 공개 계약(HTTP)으로만 판정한다"
            )
        for rx in reason_rx:
            used_codes |= {mt.group(1) for mt in rx.finditer(f.content)}
        ids = set(case_rx.findall(f.content))
        seen_ids.update(ids)
        if helper is not None and helper.path.endswith(".ts") and f.path != helper_path:
            out += _fault_calls(f, ids, plan, only)
        if only is not None and (others := sorted(ids & plan_ids - set(only))):
            out.append(
                f"숨은 테스트 {f.path} 에 다른 케이스 id 가 나옴: {', '.join(others)} — 이 파일은 {', '.join(sorted(only))} 만 다룬다"
                " (다른 케이스 id 는 주석·이름에도 쓰지 않는다 — 합치면 한 케이스가 여러 파일에 나온다)"
            )
        if used := sorted(foreign & set(_CODE_NAME.findall(f.content))):
            out.append(
                f"숨은 테스트 {f.path} 가 다른 케이스의 도우미 이름을 씀: {', '.join(used)} — 이 파일은 {', '.join(sorted(only or ()))} 의 "
                "행·실패 지정만 쓴다 (다른 케이스의 행을 바꾸면 그 케이스가 모든 구현에서 떨어진다)"
            )
    cases = [f for f in o.files if f.path not in (params_path, helper_path)]
    if helper is not None:
        out += unknown_helper_names(cases, helper, "숨은 테스트")
    out += unknown_harness_members(cases, t.hidden / "harness", "숨은 테스트")
    out += unknown_py_harness_members(cases, t.hidden / "harness", "숨은 테스트")
    out += unknown_py_keywords(cases, t.hidden / "harness", helper, "숨은 테스트")
    out += unrun_input_days(cases, contract, "숨은 테스트")
    out += future_input_days(cases, contract, "숨은 테스트")
    out += unreadable_as_pass(cases, "숨은 테스트")
    out += element_guesses(cases, contract, "숨은 테스트")
    out += type_escapes(cases, "숨은 테스트")
    out += array_substring_contains(cases, "숨은 테스트")
    out += misplaced_test_imports(cases, "숨은 테스트")
    out += option_guesses(cases, contract, "숨은 테스트")
    out += _reason_rules(cases, m, plan, case_rx, reason_rx)
    if contract is not None and rules.http_calls:
        out += _unknown_calls(cases, contract, rules.http_calls)
        out += _hand_built_query(cases, contract, rules.http_calls, "숨은 테스트")
        out += _timed_requests(cases, contract, rules.http_calls)
    if contract is not None:
        out += _hidden_tables(cases, t, contract)
        out += _literal_headers(cases, contract, "숨은 테스트")
        out += _hand_built_keys(cases, contract, "숨은 테스트")
        out += _hand_built_login(cases, contract, "숨은 테스트")
        out += _injected_status_asserts(cases, m, contract)
    if contract is not None and rules.raw_number_asserts:
        out += _raw_number_asserts(cases, rules.raw_number_asserts, contract)
    bad_codes = sorted(used_codes - m.reason_codes)
    if bad_codes:
        # 빌드의 템플릿은 재료가 적은 사유로 좁혀져 있다 (BuildInputs — templates.restrict_reasons)
        out.append(
            f"숨은 테스트의 실패 사유 코드가 쓸 수 있는 목록 밖: {', '.join(bad_codes)} "
            f"(쓸 수 있는 것: {', '.join(sorted(m.reason_codes)) or '없음'} — 이 숨은 요구의 사유. "
            "목록 밖의 실패(예: 이 요구와 무관한 5xx·시간 초과)를 확인하는 단언에는 사유를 붙이지 않는다)"
        )
    planned = {i for i in plan_ids if only is None or i in only}
    missing = sorted(planned - set(seen_ids))
    extra = sorted(set(seen_ids) - plan_ids)
    twice = sorted(i for i, n in seen_ids.items() if n > 1 and i in planned)
    if missing:
        out.append(f"HiddenPlan 케이스가 숨은 테스트에 없음: {', '.join(missing)}")
    if extra:
        out.append(f"HiddenPlan 에 없는 케이스 id 가 숨은 테스트에 있음: {', '.join(extra)}")
    if twice:
        out.append(
            f"케이스 id 가 여러 파일에 나옴 (케이스 하나 = 테스트 파일 하나): {', '.join(twice)}"
        )
    return [StaticProblem("gen_hidden", msg) for msg in out]


# 도우미 상수 이름의 가족 — 도우미가 실제로 낸 상수의 머리(HC·PUBLIC·TABLE·NEXT·HEADER·SETTING …)에서 뽑는다 (#178: 머리 넷을 손으로
# 적어 둬 도우미가 새로 낸 HEADER_…·SETTING_… 짐작은 javac 까지 갔다)
_HELPER_CONST_NAME = re.compile(r"^([A-Z]+)\d*_[A-Z0-9_]+$")


def _const_families(known: set[str]) -> re.Pattern[str] | None:
    heads = sorted({m.group(1) for k in known if (m := _HELPER_CONST_NAME.match(k))})
    if not heads:
        return None
    return re.compile(rf"\b((?:{'|'.join(heads)})\d*_[A-Z0-9_]+)\b")


_VERB_CALL = re.compile(r"(?<![\w.])((?:get|post|put|patch|delete)[A-Z]\w*)\s*\(")
_OWN_METHOD = re.compile(r"[\w>\]]\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+)?\s*\{")
_OWN_FIELD = re.compile(r"\b([A-Z][A-Z0-9_]+)\s*=")
_JDK_VERB = {"getClass"}


def helper_names(helper: GeneratedFile) -> set[str]:
    """코드가 쓴 도우미(ContractCase·PublicCase)의 함수·상수 이름."""
    methods = re.findall(r"protected (?:static )?[\w<>, \[\]]+? (\w+)\(", helper.content)
    consts = re.findall(r"protected static final [\w\[\]]+ (\w+) =", helper.content)
    return set(methods) | set(consts)


_TYPE_ESCAPE = re.compile(
    r"\bas\s+unknown\s+as\b|\bas\s+any\b|:\s*any\b|@ts-(?:ignore|expect-error|nocheck)"
)


def type_escapes(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """TypeScript 테스트가 타입 검사를 우회 (`as unknown as`·`as any`·`: any`·`@ts-ignore`) — 계약·하네스 타입이 잡아 주는 잘못(없는 필드·
    다른 응답 모양)이 실행 때 '요청이 오지 않음'·빈 화면으로 숨는다. 2026-11-09 React 알파(낙관적 갱신): 컴파일 루프가 'Call 에 json 이 없다'고
    알리자 as unknown as 로 우회해, 없는 json 필드를 보는 조건 때문에 모든 구현에서 요청을 못 찾았다 (실제 필드는 body)."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith((".ts", ".tsx")):
            continue
        hits = sorted({m.group(0) for m in _TYPE_ESCAPE.finditer(f.content)})
        if hits:
            out.append(
                f"{label} {f.path} 가 타입 검사를 우회함 ({', '.join(hits)}) — 하네스·계약 도우미의 타입대로 쓴다 "
                "(요청 기록은 call.query·call.params·call.body, 응답 본문은 <Api>Ok 타입)"
            )
    return out


# 항목 글 배열을 만드는 꼴 — textsByRole(…) 결과, 요소 목록의 .map(… textContent …)
_ARRAY_OF_TEXTS = re.compile(
    r"\b(?:const|let)\s+(\w+)\s*(?::\s*string\[\]\s*)?=\s*(?:textsByRole\(|[^;]*?\.map\(\s*\(?\s*\w+\s*\)?\s*=>[^;]*?textContent)"
)
# 화면 도구 이름 — testing-library 내보내기 표(ts_helpers.TESTING_LIBRARY, 템플릿이 고정한 판) + userEvent (#177: 이름 9개를 따로 적었다)
_TL_NAMES = frozenset({*TESTING_LIBRARY, "userEvent"})
_VITEST_IMPORT = re.compile(r"import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['\"]vitest['\"]")


def array_substring_contains(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """항목 글 배열에 `toContain('글 일부')` — 배열의 toContain 은 항목이 통째로 같아야 맞다 ('입출금 (10,000원)' ≠ '입출금').
    2026-11-10 알파 fe7c·fe7d-token: 허용 구현이 이 단언에서만 떨어지고, 흔한 구현의 '의도한 사유' 실패도 실은 이 단언이었다."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith((".ts", ".tsx")):
            continue
        names = set(_ARRAY_OF_TEXTS.findall(f.content))
        # 배열 그대로 (texts[0] 처럼 항목 하나를 꺼낸 문자열은 toContain 이 글 일부를 본다 — 걸지 않음)
        pats = [
            rf"expect\(\s*{re.escape(n)}\s*(?:,[^;]*?)?\)\s*\.(?:not\.)?toContain\(\s*['\"`]"
            for n in names
        ]
        pats.append(r"expect\(\s*textsByRole\([^;]*?\)\s*\.(?:not\.)?toContain\(\s*['\"`]")
        hits = [m.group(0) for pat in pats for m in re.finditer(pat, f.content)]
        if hits:
            out.append(
                f"{label} {f.path} 가 항목 글 배열에 toContain('글자') 를 씀: {' '.join(hits[0].split())[:90]} — 배열의 toContain 은 항목 글 전체가 같아야 맞는다. "
                "글 일부를 찾을 때는 `expect(texts.some((t) => t.includes('이름')), reason(…)).toBe(true)`"
            )
    return out


_OPTION_INDEX = re.compile(r"\.options\s*\[\s*\d+\s*\]")
_SELECT_CALL = re.compile(r"selectOptions\s*\(\s*[^,]+,\s*(['\"`])([^'\"`]+)\1")


def option_guesses(
    cases: Iterable[GeneratedFile], contract: TaskContract | None, label: str
) -> list[str]:
    """선택 값을 짐작 — `select.options[0]`(첫 항목은 안내 문구·빈 값일 수 있다)이나 계약 options 에 없는 글로 고름.
    2026-11-10 프론트 알파(fefix 008d6314): 숨은 테스트가 options[0](안내 문구)을 골라 필수 입력을 검사하는 허용 구현만 떨어짐.
    계약이 값을 정했으면 도우미 OPTIONS 로 고른다."""
    values = {
        v for sc in (contract.screens if contract else []) for el in sc.elements for v in el.options
    }
    out: list[str] = []
    for f in cases:
        index = bool(_OPTION_INDEX.search(f.content))
        unknown = sorted(
            {m.group(2) for m in _SELECT_CALL.finditer(f.content)} - values if values else set()
        )
        if index or unknown:
            what = (
                "선택 목록의 n 번째 항목(options[n])으로 고름"
                if index
                else f"계약 options 에 없는 값으로 고름: {', '.join(unknown[:5])}"
            )
            out.append(
                f"{label} {f.path}: {what} — 첫 항목은 안내 문구(빈 값)일 수 있다. 도우미 OPTIONS 의 값으로 고른다 "
                "(`await user.selectOptions(el, OPTIONS.CATEGORY_SELECT[0])`)"
            )
    return out


def misplaced_test_imports(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """화면 도구(waitFor·screen·render·within …)를 vitest 에서 import — Testing Library 의 것이다. 2026-11-10 알파 fe-v3-opt-r2·fe7d-offline:
    `import { waitFor } from 'vitest'` 로 컴파일 실패(TS2459), 재요청 2번에도 남음."""
    out: list[str] = []
    for f in cases:
        bad = sorted(
            {
                n.strip().split(" as ")[0]
                for m in _VITEST_IMPORT.finditer(f.content)
                for n in m.group(1).split(",")
            }
            & _TL_NAMES
        )
        if bad:
            out.append(
                f"{label} {f.path} 가 {', '.join(bad)} 를 vitest 에서 import 함 — 화면 도구는 '@testing-library/react'(userEvent 는 '@testing-library/user-event')에서, "
                "vitest 에서는 describe·it·expect·beforeEach·vi 만"
            )
    return out


# 계약 test id 밖에서 요소를 찾는 꼴 — Testing Library 의 이름·글·레이블 쿼리와 DOM 선택자 (#177: 사례마다 ByRole 이름·querySelector 를
# 따로 잡아 getByText·getByLabelText·closest()·getElementById 는 지나갔다)
_TL_QUERY = re.compile(
    r"\b(?:get|query|find)(?:All)?By(Role|Text|LabelText|PlaceholderText|DisplayValue|Title|AltText)\(\s*([^)\n]{0,60})"
)
_DOM_SELECT = re.compile(
    r"\.(querySelector(?:All)?|closest|getElementsBy\w+|getElementById)\(\s*([^)\n]{0,40})"
)
# 하네스 문서·생성기 프롬프트가 권하는 역할만 쿼리 — 앱 오류 표시(alert)가 없음을 보는 `screen.queryByRole('alert')`
DOC_ROLE_QUERIES = frozenset({"alert"})


def element_guesses(
    cases: Iterable[GeneratedFile], contract: TaskContract | None, label: str
) -> list[str]:
    """화면 계약이 있는 과제에서 테스트가 계약 test id 밖으로 요소를 찾음 — 접근 가능한 이름·보이는 글·레이블·자리표시자·역할이나 DOM 선택자는
    계약에 없는 꼴이라 짐작이고, 올바른 구현도 못 찾는다. 요소는 test id 로만 (`findByTestId`·`byTestId`, 목록 글은 `textsByRole`).
    2026-11-09 React 알파: 하네스 문서 예시의 '상품 검색'(ByRole 이름)을 따라 써 모든 구현에서 예외. 2026-11-10 fe7d-user: 계약 로그인 폼에
    입력란 test id 가 없어 name 속성(querySelector)으로 찾다 모든 구현에서 예외. 예외: 문서가 권하는 역할만 쿼리(DOC_ROLE_QUERIES)."""
    if contract is None or not contract.screens:
        return []
    out: list[str] = []
    for f in cases:
        hits: list[str] = []
        for m in _TL_QUERY.finditer(f.content):
            kind, args = m.group(1), m.group(2).strip()
            only_role = re.fullmatch(r"""['"]([a-z]+)['"]""", args)
            if kind == "Role" and only_role and only_role.group(1) in DOC_ROLE_QUERIES:
                continue
            hits.append(f"By{kind}({args[:30]})")
        hits += [
            f".{m.group(1)}({m.group(2).strip()[:30]})" for m in _DOM_SELECT.finditer(f.content)
        ]
        if hits:
            out.append(
                f"{label} {f.path} 가 계약 test id 밖으로 요소를 찾음 ({'; '.join(dict.fromkeys(hits))}"[
                    :260
                ]
                + ") — 요소는 계약의 test id 로만 찾는다 (`await screen.findByTestId(TID.X)`·`byTestId(범위, TID.X)`, 목록 글은 "
                "`textsByRole(역할, TID.X)`). 이름·글·레이블·선택자는 계약에 없어 짐작이 된다 — 계약에 그 요소가 없으면 화면 결과로 확인한다"
            )
    return out


_PY_CLASS = re.compile(r"^class (\w+)\b[^\n]*:\n((?:(?:[ \t]+[^\n]*|)\n)*)", re.M)


@functools.lru_cache(maxsize=8)
def py_harness_members(grader: Path) -> dict[str, frozenset[str]]:
    """Python 하네스(grader.py)의 클래스 → 쓸 수 있는 이름 (메서드·속성·필드). Workspace·JobResult 를 테스트가 쓴다."""
    if not grader.is_file():
        return {}
    src = grader.read_text("utf-8")
    out: dict[str, frozenset[str]] = {}
    for m in _PY_CLASS.finditer(src):
        body = m.group(2)
        names = set(re.findall(r"^\s{4}def (\w+)\(", body, re.M))
        names |= set(re.findall(r"^\s{4}(\w+)\s*:", body, re.M))  # 데이터클래스 필드
        names |= set(re.findall(r"self\.(\w+)\s*=", body))
        out[m.group(1)] = frozenset(n for n in names if not n.startswith("_"))  # 내부용 이름은 빼고
    return out


_PY_MAKERS = {"Workspace": r"workspace\(\)", "JobResult": r"run(?:_\w+)?\("}


def unknown_py_harness_members(
    cases: Iterable[GeneratedFile], harness_dir: Path, label: str
) -> list[str]:
    """Python 테스트가 하네스 객체(workspace() 의 Workspace, run…() 의 JobResult)에 없는 속성을 씀 — 실행해야 드러나는 AttributeError.
    2026-11-09 데이터 알파: 공개 테스트가 ws.input·ws.output·ws.extend 를 지어내 모든 구현에서 예외."""
    members = py_harness_members(harness_dir / "grader.py")
    if not members:
        return []
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        bad: set[tuple[str, str, str]] = set()
        for cls, maker in _PY_MAKERS.items():
            allowed = members.get(cls)
            if not allowed:
                continue
            for var in set(re.findall(rf"^\s*(\w+)\s*=\s*{maker}", f.content, re.M)):
                for attr in re.findall(rf"(?<![\w.]){re.escape(var)}\.(\w+)", f.content):
                    if attr not in allowed:
                        bad.add((var, cls, attr))
        if bad:
            hints = []
            for var, cls, attr in sorted(bad):
                near = difflib.get_close_matches(attr, sorted(members[cls]), n=1, cutoff=0.5)
                hints.append(
                    f"{var}.{attr} ({cls}" + (f" — 가까운 이름: {near[0]})" if near else ")")
                )
            out.append(
                f"{label} {f.path} 가 하네스에 없는 이름을 씀: {', '.join(hints)} — 하네스 문서의 이름만 쓴다 "
                f"(Workspace: {', '.join(sorted(members.get('Workspace', ())))})"
            )
    return out


@dataclass(frozen=True)
class PySignature:
    """Python 함수·메서드 시그니처 — 테스트가 하네스·계약 도우미를 맞게 부르는지 (키워드 이름·필수 인자) 대조용."""

    positional: tuple[str, ...]  # 위치로 받을 수 있는 이름 (순서대로, self 제외)
    keywords: frozenset[str]  # 키워드로 받을 수 있는 이름
    required: frozenset[str]  # 기본값 없는 이름
    varargs: bool  # *args
    varkw: bool  # **kwargs
    returns: str  # 돌려주는 클래스 이름 (모르면 "")
    # 문자열 기본값 (이름, 값) — 계약 입출력 도우미의 폴더 인자(raw="raw") 를 작업 폴더 이름으로 알아보려고 (#256 2단계)
    str_defaults: tuple[tuple[str, str], ...] = ()


def _signature_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> PySignature:
    a = fn.args
    pos = [x.arg for x in [*a.posonlyargs, *a.args] if x.arg not in ("self", "cls")]
    kw_pos = [x.arg for x in a.args if x.arg not in ("self", "cls")]
    n_def = len(a.defaults)
    pos_required = pos[: len(pos) - n_def] if n_def else pos
    kwonly_required = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None]
    ret = ""
    if isinstance(fn.returns, ast.Name):
        ret = fn.returns.id
    elif isinstance(fn.returns, ast.Constant) and isinstance(fn.returns.value, str):
        ret = fn.returns.value  # 문자열 표기
    paired = [
        *zip([x.arg for x in a.args][len(a.args) - n_def :], a.defaults, strict=True),
        *((x.arg, d) for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None),
    ]
    return PySignature(
        positional=tuple(pos),
        keywords=frozenset([*kw_pos, *(x.arg for x in a.kwonlyargs)]),
        required=frozenset([*pos_required, *kwonly_required]),
        varargs=a.vararg is not None,
        varkw=a.kwarg is not None,
        returns=ret,
        str_defaults=tuple(
            (name, d.value)
            for name, d in paired
            if isinstance(d, ast.Constant) and isinstance(d.value, str)
        ),
    )


def _py_signatures(src: str) -> tuple[dict[str, PySignature], dict[str, str]]:
    """Python 모듈 → 함수·메서드 시그니처('f' · 'Class.m')와 모듈 수준 인스턴스(`params = _Params()` → 클래스)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}, {}
    sigs: dict[str, PySignature] = {}
    instances: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs[node.name] = _signature_of(node)
        elif isinstance(node, ast.ClassDef):
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sigs[f"{node.name}.{b.name}"] = _signature_of(b)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    instances[tgt.id] = node.value.func.id
    return sigs, instances


@functools.lru_cache(maxsize=8)
def _harness_signatures(grader: Path) -> tuple[dict[str, PySignature], dict[str, str]]:
    return _py_signatures(grader.read_text("utf-8")) if grader.is_file() else ({}, {})


def _call_problems(node: ast.Call, name: str, sig: PySignature) -> list[str]:
    """없는 키워드 이름·빠진 필수 인자 (*args·**kwargs 를 받거나 넘기면 보지 않는다)."""
    if any(isinstance(a, ast.Starred) for a in node.args) or any(
        k.arg is None for k in node.keywords
    ):
        return []
    out: list[str] = []
    accepted = ", ".join(sig.positional + tuple(sorted(sig.keywords - set(sig.positional))))
    if not sig.varkw:
        out += [
            f"{name}({k.arg}=…) — 받는 이름: {accepted or '없음'}"
            for k in node.keywords
            if k.arg is not None and k.arg not in sig.keywords
        ]
    if not sig.varargs:
        given = set(sig.positional[: len(node.args)]) | {k.arg for k in node.keywords if k.arg}
        missing = sorted(sig.required - given)
        if missing:
            out.append(
                f"{name}(…) 에 필수 인자 {', '.join(missing)} 가 빠짐 — 받는 이름: {accepted}"
            )
    return out


def unknown_py_keywords(
    cases: Iterable[GeneratedFile], harness_dir: Path, helper: GeneratedFile | None, label: str
) -> list[str]:
    """Python 테스트가 하네스 함수·메서드(workspace().write_csv 등)나 계약 도우미 함수(run_…)를 시그니처와 다르게 부름 — 실행해야 드러나는
    TypeError. 없는 키워드 인자·빠진 필수 인자·문맥 관리자가 아닌 하네스 객체에 with·작업 폴더 경로를 문자열 그대로 명령에(5회차).
    2026-11-09 데이터 알파 2~3회차: 공개 테스트가 ws.write_csv(..., header=...)·ws.partitions(폴더)(key 빠짐)·`with workspace() as ws`
    를 지어내 모든 구현에서 예외 (다시 만들어도 반복)."""
    h_sigs, h_inst = _harness_signatures(harness_dir / "grader.py")
    c_sigs, _ = _py_signatures(helper.content) if helper is not None else ({}, {})
    if not h_sigs and not c_sigs:
        return []
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue  # 문법 오류는 컴파일 확인이 알린다
        funcs: dict[str, PySignature] = {}  # 이 파일이 import 한 하네스·도우미 함수 (별칭 포함)
        commands: set[str] = set()  # 그중 계약 도우미의 명령 함수 (run_…)
        io_helpers: set[str] = (
            set()
        )  # 그중 계약 도우미의 입력 쓰기·출력 읽기 함수 (write_…·read_… — #256 2단계)
        objs: dict[str, str] = {}  # 이름 → 하네스 클래스 (params 같은 인스턴스, workspace() 결과)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                last = node.module.rsplit(".", 1)[-1]
                pool = (
                    h_sigs
                    if node.module == "harness.grader"
                    else c_sigs
                    if last == "contract"
                    else None
                )
                if pool is None:
                    continue
                for al in node.names:
                    local = al.asname or al.name
                    if al.name in pool:
                        funcs[local] = pool[al.name]
                        if pool is c_sigs and al.name.startswith("run_"):
                            commands.add(local)
                        elif pool is c_sigs and al.name.startswith(("write_", "read_")):
                            io_helpers.add(local)
                    elif pool is h_sigs and al.name in h_inst:
                        objs[local] = h_inst[al.name]

        def made_by(value: ast.expr | None, funcs: dict[str, PySignature] = funcs) -> str:
            """하네스·도우미 함수 호출 결과의 클래스 (모르면 "")."""
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in funcs
            ):
                return funcs[value.func.id].returns
            return ""

        bad: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and (cls := made_by(node.value)):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        objs[tgt.id] = cls
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    expr = item.context_expr
                    cls = made_by(expr) or (
                        objs.get(expr.id, "") if isinstance(expr, ast.Name) else ""
                    )
                    if cls and f"{cls}.__enter__" not in h_sigs:
                        bad.append(
                            f"with {ast.unparse(expr)} — {cls} 는 with 로 쓰지 않는다 (그냥 대입: x = {ast.unparse(expr)})"
                        )
                        if isinstance(item.optional_vars, ast.Name):
                            objs[item.optional_vars.id] = cls
        # 작업 폴더에 쓰고 읽는 경로의 첫 마디 (ws.write_csv("raw/…") → raw) — 명령에 그 이름을 문자열 그대로 넘기면 저장소 루트 기준이 된다
        ws_roots: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in objs
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                first = node.args[0].value.strip("/").removeprefix("./").split("/", 1)[0]
                if first:
                    ws_roots.add(first)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in io_helpers
            ):
                # 도우미의 폴더 인자(파일 이름 name 빼고)도 작업 폴더 이름 — 넘긴 값이 없으면 기본값 (raw="raw")
                dirs = {k: v for k, v in funcs[node.func.id].str_defaults if k != "name"}
                given = {
                    kw.arg: kw.value.value
                    for kw in node.keywords
                    if kw.arg in dirs
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                }
                for v in {**dirs, **given}.values():
                    first = v.strip("/").removeprefix("./").split("/", 1)[0]
                    if first:
                        ws_roots.add(first)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in funcs:
                bad += _call_problems(node, node.func.id, funcs[node.func.id])
                if node.func.id in commands:
                    for kw in node.keywords:
                        v = kw.value
                        if (
                            kw.arg
                            and isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                            and v.value.removeprefix("./").split("/", 1)[0] in ws_roots
                        ):
                            bad.append(
                                f"{node.func.id}({kw.arg}={v.value!r}) — 작업 폴더 경로를 문자열 그대로 넘김. 명령은 저장소 루트에서 돌므로 "
                                f"ws.path({v.value!r}) 로 넘긴다"
                            )
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in objs
            ):
                cls = objs[node.func.value.id]
                sig = h_sigs.get(f"{cls}.{node.func.attr}")
                if sig is not None:
                    bad += _call_problems(node, f"{node.func.value.id}.{node.func.attr}", sig)
        if bad:
            out.append(
                f"{label} {f.path} 가 하네스·계약 도우미를 잘못 부름: {'; '.join(sorted(set(bad))[:6])} — "
                "하네스 문서·계약 도우미의 시그니처대로, 경로는 ws.path(…) 로 넘긴다"
            )
    return out


_DAY = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_WRITERS = frozenset({"write_jsonl", "write_csv", "write_parquet", "write_text", "write_json"})


def date_options(contract: TaskContract | None) -> dict[str, set[str]]:
    """명령 도우미 함수(run_…) → 날짜·날짜-시각 옵션 키워드 이름. 계약 형식·제약으로 정한다 (#178: 옵션 이름에 'date' 가 든 것만 봐
    `day=`·`as_of=`·`run_at=` 은 실행 날짜로 보지 못했다). 계약이 없으면 비어 있다 — 실행 날짜를 알 수 없어 판정하지 않는다."""
    out: dict[str, set[str]] = {}
    for e in contract.endpoints if contract is not None else []:
        if e.method:
            continue
        opts = {
            py_ident(f.name) for f in [*e.request, *e.query] if time_kind(f) in ("date", "datetime")
        }
        if opts:
            out[command_name(e.path)] = opts
    return out


@dataclass(frozen=True)
class DayFlow:
    """Python 테스트 함수 하나가 날짜 폴더에 입력을 쓰고(write) 그 날짜로 명령을 실행(run)한 순서 — 줄 번호 순."""

    test: str
    events: tuple[tuple[int, str, str], ...]  # (줄, write|run, 날짜)
    unknown: bool  # 날짜·경로를 변수로 넘김 — 글자 그대로가 아니라 판정하지 않는다
    looped: bool  # 반복문 안 — 줄 순서가 실행 순서가 아니다

    def days(self, kind: str) -> set[str]:
        return {day for _, k, day in self.events if k == kind}


def _imported_commands(tree: ast.Module, prefix: str = "run_") -> dict[str, str]:
    """`from …contract import run_x as y` → {y: run_x}. prefix="write_" 면 계약 도우미의 입력 쓰기 함수 (#256 2단계)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").rsplit(".", 1)[-1] == "contract"
        ):
            out |= {
                al.asname or al.name: al.name for al in node.names if al.name.startswith(prefix)
            }
    return out


def day_flows(f: GeneratedFile, options: dict[str, set[str]]) -> list[DayFlow]:
    """파일의 테스트 함수마다 날짜 폴더 쓰기·실행 흐름. 쓰기는 하네스 쓰기 함수의 경로 글자에 든 `YYYY-MM-DD/`, 실행은 계약의 날짜 옵션에
    넘긴 글자 날짜 (날짜-시각이면 앞 10자리)."""
    if not f.path.endswith(".py"):
        return []
    try:
        tree = ast.parse(f.content)
    except SyntaxError:
        return []
    commands = {alias: name for alias, name in _imported_commands(tree).items() if name in options}
    if not commands:
        return []
    # 계약 도우미의 입력 쓰기(write_…(ws, rows, date=…)) — 날짜 인자는 명령의 날짜 옵션과 같은 이름 (위치 패턴의 마디 = 옵션)
    writers = set(_imported_commands(tree, "write_"))
    dated_names = set().union(*options.values())
    flows: list[DayFlow] = []
    for fn in tree.body:
        if not (
            isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test")
        ):
            continue
        events: list[tuple[int, str, str]] = []
        unknown = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in _WRITERS and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    events += [
                        (node.lineno, "write", m.group(1))
                        for m in _DAY.finditer(a0.value)
                        if f"{m.group(1)}/" in a0.value
                    ]
                else:
                    unknown = True
            elif isinstance(node.func, ast.Name) and node.func.id in writers:
                for kw in node.keywords:
                    if kw.arg is None:
                        unknown = True
                    elif kw.arg in dated_names:
                        v = kw.value
                        day = (
                            _DAY.match(v.value)
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                            else None
                        )
                        if day is None:
                            unknown = True
                        else:
                            events.append((node.lineno, "write", day.group(1)))
            elif isinstance(node.func, ast.Name) and node.func.id in commands:
                dated = options[commands[node.func.id]]
                for kw in node.keywords:
                    if kw.arg is None:  # **옵션 — 무엇을 넘겼는지 모른다
                        unknown = True
                    elif kw.arg in dated:
                        v = kw.value
                        day = (
                            _DAY.match(v.value)
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                            else None
                        )
                        if day is None:
                            unknown = True
                        else:
                            events.append((node.lineno, "run", day.group(1)))
        looped = any(isinstance(n, (ast.For, ast.AsyncFor, ast.While)) for n in ast.walk(fn))
        flows.append(DayFlow(fn.name, tuple(sorted(events)), unknown, looped))
    return flows


def unrun_input_days(
    cases: Iterable[GeneratedFile], contract: TaskContract | None, label: str
) -> list[str]:
    """Python 테스트 함수가 날짜 폴더(`raw/2026-09-01/…`)에 입력을 쓰고 그 날짜로는 명령을 실행하지 않음 — 마지막 날짜로 한 번만 실행해 앞 날짜
    출력까지 기대하는 꼴. 받은 날짜마다 실행하는 정상 운영에서만 맞는 허용 설계('이번 폴더에 나온 날짜만 다시 계산')가 떨어진다.
    2026-11-09 데이터 알파 2~8회차 재검증: 손으로 쓴 허용 구현 두 가지로 돌려 보니 이 꼴의 케이스(hc3·hc4)가 7회 중 6회 나왔다.
    날짜·경로가 변수로 만들어지는 함수는 보지 않는다 (글자 그대로인 것만). 반복문 안이어도 날짜 집합은 같으므로 본다."""
    options = date_options(contract)
    out: list[str] = []
    for f in cases:
        for flow in day_flows(f, options):
            ran, missing = flow.days("run"), sorted(flow.days("write") - flow.days("run"))
            if ran and missing and not flow.unknown:
                out.append(
                    f"{label} {f.path} 의 {flow.test} 가 날짜 폴더 {', '.join(missing)} 에 입력을 쓰고 그 날짜로 실행하지 않음 (실행한 날짜: "
                    f"{', '.join(sorted(ran))}) — 받은 날짜 순서대로 하루씩 실행한다. 마지막 날짜로 한 번만 실행해 앞 날짜 출력까지 기대하면 "
                    "'이번 폴더에 나온 날짜만 다시 계산'하는 허용 설계가 떨어진다"
                )
    return out


def future_input_days(
    cases: Iterable[GeneratedFile], contract: TaskContract | None, label: str
) -> list[str]:
    """Python 테스트 함수가 실행 날짜보다 **뒤** 날짜 폴더를 그 실행 전에 미리 씀 — 실제로는 아직 받지 않은 입력이라, 공개 계약에 없는 요구
    ('실행 날짜보다 뒤 폴더는 읽지 않는다')를 시험하게 된다. 2026-11-10 데이터 알파 10회차: hc1·hc2 가 9/2 폴더를 9/1 실행 전에 써 두어,
    받은 폴더를 모두 읽는 허용 구현(손 구현으로 재현)이 떨어졌다. 줄 순서로 본다 — 날짜·경로가 변수이거나 반복문이 있는 함수는 보지 않는다."""
    options = date_options(contract)
    out: list[str] = []
    for f in cases:
        for flow in day_flows(f, options):
            if flow.unknown or flow.looped:
                continue
            written: set[str] = set()
            early: set[tuple[str, str]] = set()
            for _, kind, day in flow.events:
                if kind == "write":
                    written.add(day)
                else:
                    early |= {(w, day) for w in written if w > day}
            if early:
                pairs = ", ".join(f"{w}(실행 {r} 전)" for w, r in sorted(early)[:4])
                out.append(
                    f"{label} {f.path} 의 {flow.test} 가 실행 날짜보다 뒤 날짜 폴더를 그 실행 전에 씀: {pairs} — 입력은 받은 순서대로, "
                    "날짜 폴더는 그 날짜를 실행하기 직전에 쓴다. 미리 두면 공개 계약에 없는 '뒤 폴더는 읽지 않는다'를 시험하게 되어 "
                    "받은 폴더를 모두 읽는 허용 구현이 떨어진다"
                )
    return out


def _compares(expr: ast.expr, values: set[str], op: type[ast.cmpop]) -> bool:
    """`x == 표시` (op=Eq) · `x != 표시` (op=NotEq). 표시가 튜플·목록·집합 안이면 `x in (표시, …)` 도 Eq, `x not in (…)` 도 NotEq 로 본다
    (2026-11-10 알파 14회차: `assert v in ("<unreadable>", {})` 를 놓쳤다)."""
    if not (isinstance(expr, ast.Compare) and len(expr.ops) == 1):
        return False
    sides = (expr.left, *expr.comparators)
    if isinstance(expr.ops[0], op) and any(
        isinstance(x, ast.Constant) and x.value in values for x in sides
    ):
        return True
    member = {ast.Eq: ast.In, ast.NotEq: ast.NotIn}.get(op)
    return (
        member is not None
        and isinstance(expr.ops[0], member)
        and any(
            isinstance(c, (ast.Tuple, ast.List, ast.Set))
            and any(isinstance(e, ast.Constant) and e.value in values for e in c.elts)
            for c in expr.comparators
        )
    )


def unreadable_as_pass(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """크래시 케이스가 `except` 에서 바꾼 '읽을 수 없음' 표시 값을 통과로 둠 — `if v != 'unreadable': assert …`(그 값이면 단언을 건너뜀)이나
    `assert v == 'UNREADABLE' or …`. 깨진 파일이 보이는 것이 바로 숨은 요구 위반인데 최종 위치에 바로 쓰다 죽는 흔한 구현이 통과한다.
    2026-11-10 데이터 알파 8·9회차: 두 번 다 이 꼴 (프롬프트의 '읽을 수 없음을 값으로 바꿔 단언'이 무엇이 실패인지 말하지 않았다)."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue
        marks: set[str] = set()
        for h in ast.walk(tree):
            if isinstance(h, ast.ExceptHandler):
                for n in ast.walk(h):
                    v = n.value if isinstance(n, (ast.Return, ast.Assign)) else None
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        marks.add(v.value)
        if not marks:
            continue
        hits: list[int] = []
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.If)
                and _compares(n.test, marks, ast.NotEq)
                and any(isinstance(x, ast.Assert) for b in n.body for x in ast.walk(b))
            ):
                hits.append(n.lineno)
            elif isinstance(n, ast.Assert) and (
                (
                    isinstance(n.test, ast.BoolOp)
                    and isinstance(n.test.op, ast.Or)
                    and any(_compares(v, marks, ast.Eq) for v in n.test.values)
                )
                or (
                    isinstance(n.test, ast.Compare)
                    and isinstance(n.test.ops[0], ast.In)
                    and _compares(n.test, marks, ast.Eq)
                )
            ):
                hits.append(n.lineno)
        if hits:
            out.append(
                f"{label} {f.path} 가 크래시 뒤 '읽을 수 없음'({', '.join(sorted(repr(m) for m in marks))})을 통과로 둠 (줄 {', '.join(map(str, sorted(hits)[:4]))}) — "
                "읽을 수 없는 출력이 보이면 그것이 실패다. 그 값을 건너뛰거나 허용하지 말고 직전 성공 값(또는 성공했을 때의 값)과만 같게 단언한다"
            )
    return out


def unknown_helper_names(
    cases: Iterable[GeneratedFile], helper: GeneratedFile, label: str
) -> list[str]:
    """케이스가 도우미에 없는 범위·공개 행·테이블 상수나 계약 API 함수를 쓰면 가까운 실제 이름과 함께.
    2026-11-08 상류 평가: 도우미 소스를 보여 줬는데도 getApiOrdersOrderIdStatus(실제 …StatusAudit)·PUBLIC_ORDER_READY(없는 처음 값)를
    짐작해 컴파일 실패 — 이름은 코드가 만들어 정확히 알므로 Gradle 전에 대조한다."""
    if not helper.path.endswith(
        ".java"
    ):  # 이름 꼴이 Java 기준 — TypeScript 도우미(contract.ts)의 없는 이름은 tsc 가 잡는다
        return []
    known = helper_names(helper)
    families = _const_families(known)
    out: list[str] = []
    for f in cases:
        own = set(_OWN_METHOD.findall(f.content)) | set(_OWN_FIELD.findall(f.content))
        consts = set(families.findall(f.content)) if families is not None else set()
        used = consts | set(_VERB_CALL.findall(f.content))
        bad = sorted(u for u in used if u not in known and u not in own and u not in _JDK_VERB)
        if not bad:
            continue
        hints = []
        for b in bad:
            near = difflib.get_close_matches(b, sorted(known), n=1, cutoff=0.6)
            family = sorted(k for k in known if k.split("_")[0] == b.split("_")[0] and k.isupper())
            if near:
                hints.append(f"{b} (가까운 이름: {near[0]})")
            elif family and b.isupper():
                hints.append(f"{b} (있는 이름: {', '.join(family[:6])})")
            else:
                hints.append(b)
        out.append(
            f"{label} {f.path} 가 도우미에 없는 이름을 씀: {', '.join(hints)} — 도우미 소스에 있는 상수·함수 이름 그대로 쓴다 "
            "(없는 이름을 짐작하지 않는다)"
        )
    return out


_STATIC_MEMBER = re.compile(
    r"(?:public|protected)\s+static\s+(?:(?:final|volatile|synchronized)\s+)*[\w<>\[\],.? ]+?\s+(\w+)\s*[(=;]"
)
_NESTED_TYPE = re.compile(
    r"(?:public|protected)\s+(?:(?:static|final|abstract|sealed)\s+)*(?:record|interface|class|enum)\s+(\w+)"
)


_JAVA_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _strip_comments(src: str) -> str:
    return _JAVA_COMMENT.sub(" ", src)


@functools.lru_cache(maxsize=8)
def harness_members(harness_dir: Path) -> dict[str, frozenset[str]]:
    """잠긴 하네스 클래스 → 클래스 이름으로 부를 수 있는 것 (static 메서드·필드, 중첩 타입). 템플릿 소스에서 읽는다."""
    out: dict[str, frozenset[str]] = {}
    for f in sorted(harness_dir.glob("*.java")) if harness_dir.is_dir() else []:
        src = f.read_text(encoding="utf-8")
        nested = {n for n in _NESTED_TYPE.findall(src) if n != f.stem}
        out[f.stem] = frozenset(set(_STATIC_MEMBER.findall(src)) | nested | {"class"})
    return out


def unknown_harness_members(
    cases: Iterable[GeneratedFile], harness_dir: Path, label: str
) -> list[str]:
    """케이스가 하네스 클래스에 없는 멤버를 부르면 (`Burst.IntUnaryOperator` 등) 가까운 실제 이름과 함께.
    2026-11-08 상류 평가: 하네스 문서에 `IntUnaryOperator expectedStatus` 라 적혀 있는데 `Burst.IntUnaryOperator` 로 써 컴파일 실패."""
    members = harness_members(harness_dir)
    if not members:
        return []
    rx = re.compile(rf"(?<![\w.])({'|'.join(map(re.escape, members))})\.([A-Za-z_]\w*)")
    out: list[str] = []
    for f in cases:
        bad = sorted(
            {(c, n) for c, n in rx.findall(_strip_comments(f.content)) if n not in members[c]}
        )
        if not bad:
            continue
        hints = []
        for c, n in bad:
            near = difflib.get_close_matches(n, sorted(members[c] - {"class"}), n=1, cutoff=0.6)
            if near:
                hints.append(f"{c}.{n} (가까운 이름: {c}.{near[0]})")
            elif n[:1].isupper():
                hints.append(f"{c}.{n} ({c} 안의 타입이 아님 — JDK 타입이면 import 해서 그냥 {n})")
            else:
                hints.append(f"{c}.{n}")
        out.append(
            f"{label} {f.path} 가 하네스에 없는 멤버를 씀: {', '.join(hints)} — 하네스 문서·소스에 있는 이름 그대로 쓴다"
        )
    return out


_HIDDEN_ONLY = re.compile(r"\b(?:Burst|GraderJdbcHarness|GraderParams)\.|\bpeak\(")


def _starter_settings(o: Overlay, contract: TaskContract | None) -> list[StaticProblem]:
    """계약 settings 의 키가 시작 코드 설정 파일에 기본값으로 있는가 — 구현이 '설정에서 읽는다'의 출발점 (2026-11-13 #161)."""
    if contract is None or not contract.settings:
        return []
    config = _config_map((f.path, f.content) for f in o.files)
    out: list[StaticProblem] = []
    for st in contract.settings:
        if st.key not in config:
            out.append(
                StaticProblem(
                    o.target,
                    f"계약 설정 {st.key} 가 시작 코드 설정 파일(application.yml 등)에 없음 — 기본값 {st.default} 로 둔다",
                )
            )
        elif not _same_value(config[st.key], st.default):
            out.append(
                StaticProblem(
                    o.target,
                    f"시작 코드 설정 {st.key}={config[st.key]} 이 계약 기본값 {st.default} 와 다름",
                )
            )
    return out


def _public_row_uses(
    o: Overlay, contract: TaskContract | None, plan: HiddenPlan | None = None
) -> list[StaticProblem]:
    """계약이 공개 테스트(pt)마다 정한 쓸 행(rows)을 그 pt 의 테스트가 쓰는가 — 도우미의 공개 몫 상수(PUBLIC_…)로.
    #164: 경계 행·걸러질 행을 계약에 두어도 테스트가 다른 행(남은 20·요청 30 만)을 쓰면 규칙을 가르지 못한다.
    pt 의 테스트는 @DisplayName 이 pt id 로 시작하는 파일 (gen_public 규칙 1). 그 파일이 없으면 다른 검사(_public_cases)가 잡는다."""
    if contract is None:
        return []
    out: list[StaticProblem] = []
    for t in contract.public_tests:
        if not t.rows:
            continue
        mark = re.compile(rf'@DisplayName\(\s*"{re.escape(t.test_id)}\b')
        files = [f for f in o.files if mark.search(f.content)]
        if not files:
            continue
        text = "\n".join(f.content for f in files)
        for u in t.rows:
            consts = row_use_consts(contract, u, plan)
            if consts and not any(re.search(rf"\b{re.escape(c)}\b(?!_)", text) for c in consts):
                vals = ", ".join(f"{k}={v}" for k, v in u.values.items())
                out.append(
                    StaticProblem(
                        o.target,
                        f"공개 테스트 {t.test_id} 가 계약이 정한 행({vals})을 쓰지 않음 — 도우미 상수 {consts[0]} 의 행으로 "
                        "확인한다 (규칙을 지킨 구현과 어긴 구현이 다른 결과를 내는 행)",
                    )
                )
    return out


def _public_settings(o: Overlay, contract: TaskContract | None) -> list[StaticProblem]:
    """계약 settings 마다 그 키를 덮어 보는 공개 테스트가 있는가 — 도우미 SETTING_…_KEY 를 @DynamicPropertySource 에서 SETTING_…_PROBE 로
    덮고 PROBE 를 단언해야 값을 코드에 박은 구현·제한 없는 구현을 가른다 (기본값만 보면 둘 다 통과, 2026-11-13 #161)."""
    if contract is None or not contract.settings:
        return []
    out: list[StaticProblem] = []
    for st in contract.settings:
        c = setting_const(st.key)
        if not any(
            "@DynamicPropertySource" in f.content
            and f"SETTING_{c}_KEY" in f.content
            and f"SETTING_{c}_PROBE" in f.content
            for f in o.files
        ):
            out.append(
                StaticProblem(
                    o.target,
                    f"설정 {st.key} 를 따르는지 보는 공개 테스트가 없음 — 그 요구({', '.join(st.refs)})의 테스트 클래스에 "
                    f"@DynamicPropertySource 로 SETTING_{c}_KEY 를 SETTING_{c}_PROBE 로 덮고 결과가 SETTING_{c}_PROBE 를 따르는지 단언한다 "
                    f"(기본값 {st.default} 를 단언하지 않는다)",
                )
            )
    return out


def _public_cases(
    o: Overlay, t: Template, plan: HiddenPlan, contract: TaskContract | None
) -> list[StaticProblem]:
    """공개 테스트(gen_public) — 코드가 쓴 도우미·저장 확인은 그대로, 케이스는 PublicCase 상속·계약 API 만·pt 마다 하나·숨은 요구 영역 금지."""
    m = t.manifest
    rule = m.public_helper
    if rule is None or contract is None:
        return []
    written = {
        f.path: f
        for f in (
            render_contract_case(contract, plan, m, audience="public"),
            render_persistence_check(contract, m),
        )
        if f is not None
    }
    out: list[str] = []
    cases: list[GeneratedFile] = []
    for f in o.files:
        if f.path in written:
            if f.content != written[f.path].content:
                out.append(
                    f"{f.path} 를 손으로 씀 — 이 파일은 계약에서 코드가 쓴다 (공개 테스트에 넣지 않는다)"
                )
            continue
        cases.append(f)
        # 상속·숨은 전용 이름 검사는 Java 하네스 기준 — TypeScript 는 tsc 컴파일 루프와 import 규칙으로
        if not is_java(m):
            continue
        if not re.search(rf"\bextends\s+{re.escape(rule.class_name)}\b", f.content):
            out.append(
                f"공개 테스트 {f.path} 가 {rule.class_name} 를 상속하지 않음 — {rule.class_name} 의 공개 몫 행 상수·계약 API 함수·Body 함수를 쓴다"
            )
        if _HIDDEN_ONLY.search(f.content):
            out.append(
                f"공개 테스트 {f.path} 가 동시 요청·오류 주입·조절값을 씀 — 숨은 요구 영역이다. 공개 수용 기준(응답·기록)만 확인한다"
            )
    out += _literal_headers(cases, contract, "공개 테스트")
    out += _hand_built_keys(cases, contract, "공개 테스트")
    out += _hand_built_login(cases, contract, "공개 테스트")
    imports = [re.compile(p, re.M) for p in m.hidden_rules.forbidden_imports]
    for f in cases:
        bad = [
            line.strip()
            for line in f.content.splitlines()
            if any(rx.search(line) for rx in imports)
        ]
        if bad:
            out.append(
                f"공개 테스트 {f.path} 가 지원자 구현 내부를 직접 씀: {'; '.join(bad[:3])} — 공개 계약(HTTP)으로만 판정한다"
            )
    if m.hidden_rules.http_calls:
        out += [
            msg.replace("숨은 테스트", "공개 테스트")
            for msg in _unknown_calls(cases, contract, m.hidden_rules.http_calls)
        ]
        out += _hand_built_query(cases, contract, m.hidden_rules.http_calls, "공개 테스트")
    helper = written.get(rule.path)
    if helper is not None:
        out += unknown_helper_names(cases, helper, "공개 테스트")
    out += unknown_harness_members(cases, t.hidden / "harness", "공개 테스트")
    out += unknown_py_harness_members(cases, t.hidden / "harness", "공개 테스트")
    out += unknown_py_keywords(cases, t.hidden / "harness", helper, "공개 테스트")
    out += unrun_input_days(cases, contract, "공개 테스트")
    out += future_input_days(cases, contract, "공개 테스트")
    out += unreadable_as_pass(cases, "공개 테스트")
    out += element_guesses(cases, contract, "공개 테스트")
    out += type_escapes(cases, "공개 테스트")
    out += array_substring_contains(cases, "공개 테스트")
    out += misplaced_test_imports(cases, "공개 테스트")
    out += option_guesses(cases, contract, "공개 테스트")
    text = "\n".join(f.content for f in cases)
    missing = [
        pt.test_id for pt in contract.public_tests if not re.search(rf"\b{pt.test_id}\b", text)
    ]
    if missing:
        out.append(
            f"계약의 공개 테스트 {', '.join(missing)} 가 공개 테스트에 없음 — pt 마다 클래스 하나, @DisplayName 에 pt id"
        )
    return [StaticProblem("gen_public", msg) for msg in out]


def _duplicate_labels(o: Overlay, t: Template) -> list[str]:
    """같은 사유·문구의 단언이 한 케이스 파일에 둘 이상 — 사유가 잘못 붙었을 수 있어 승인자에게 표시 (막지 않음).
    서로 다른 요청을 같은 문구로 확인하는 것은 정상일 수 있다 (2026-11-07: 재요청 문제로 두었더니 생성 자체가 실패)."""
    rules = t.manifest.hidden_rules
    flags: list[str] = []
    for f in o.files:
        labels: Counter[tuple[str, str]] = Counter()
        for pat in rules.reason_labels:
            labels.update((m.group(1), m.group(2)) for m in re.finditer(pat, f.content))
        dup = sorted(f'{code} "{msg}"' for (code, msg), n in labels.items() if n > 1)
        if dup:
            flags.append(
                f"숨은 테스트 {f.path} 에 같은 사유·문구의 단언이 둘 이상: {'; '.join(dup)} — 사유가 단언과 일치하는지 확인 "
                "(흔한 구현이 '의도한 사유'로 실패한 것으로 오인할 수 있음)"
            )
    return flags


def static_check(
    overlays: Sequence[Overlay],
    *,
    template: Template,
    plan: HiddenPlan,
    spec: SpecView,
    contract: TaskContract | None = None,
    seed: FeatureSeed | None = None,
    readme: GeneratedFile | None = None,
    avoid_terms: Sequence[str] = (),
    public_tests_visible: bool = False,
    hidden_cases_only: Collection[str] | None = None,
) -> StaticResult:
    """오버레이 정적 검사 (§6). problems 는 생성기별 재생성 사유, review_flags 는 승인자에게."""
    res = StaticResult()
    hidden_files = [f for o in overlays if o.kind == "hidden" for f in o.files]
    # 코드가 쓴 파일(조절값·계약 도우미)의 이름은 빼고 — 2026-11-09 React 알파: 숨은 도우미 contract.ts 의 이름이 공개 테스트의
    # `import … from './contract'`(공개 도우미)를 누설로 막아 공개 테스트 생성이 재생성 예산을 다 썼다
    m_ = template.manifest
    written = {m_.hidden_layout.params if m_.hidden_layout else None}
    written |= {m_.hidden_helper.path if m_.hidden_helper else None}
    hidden_names = [PurePosixPath(f.path).stem for f in hidden_files if f.path not in written]
    terms = list(
        dict.fromkeys([*hidden_terms(seed, spec), *(n for n in hidden_names if len(n) >= 4)])
    )

    for o in overlays:
        res.problems += _paths(o, template)
        res.problems += [StaticProblem(o.target, msg) for msg in _hygiene(o, o.files, avoid_terms)]
        res.review_flags += _secret_hints(o, o.files)
        # 공개 테스트는 지원자에게 주지 않아도 숨은 요구 낱말이 있으면 안 된다 (게이트 ⑤ 누설 사전 확인)
        public = o.kind in ("starter", "public_test")
        if public:
            for path, shown in _leaks(o.files, terms):
                res.problems.append(
                    StaticProblem(
                        o.target,
                        f"공개 파일 {path} 에 숨은 요구·운영 조건·숨은 테스트를 드러내는 말: {', '.join(shown)} "
                        "— 코드·주석·이름에 쓰지 않는다 (미끼 코드 포함)",
                    )
                )
        res.problems += _code_pitfalls(o, template)  # 항목마다 overlays 로 거른다 (#176·#177)
        res.problems += _config_pitfalls(o, template)  # 매니페스트 config_pitfalls (#176)
        if o.kind in ("starter", "ref_naive", "ref_accepted"):
            res.problems += _unavailable_imports(o, template)
            res.problems += _test_folder_imports(o, template)
            res.problems += _script_imports(o, template, contract)
            res.problems += _contract_types_usage(o, template, contract)
            res.problems += _skeleton_kept(o, template, contract)
            res.problems += _java_test_pitfalls(
                o, template, template.manifest.seed_sql.path if template.manifest.seed_sql else None
            )
        if o.kind == "starter":
            problems, flags = _operating_values(o, template, plan)
            res.problems += problems
            res.problems += _starter_tables(o, template, contract)
            res.problems += _starter_foreign_keys(o, template, contract)
            res.problems += _starter_new_api_tests(o, template, contract, spec)
            res.problems += _starter_issued_calls(o, contract, spec)
            res.problems += _starter_stub_tests(o, template, contract, spec)
            res.problems += _starter_schema(o, template, contract)
            if not is_java(template.manifest):
                res.problems += _starter_options(o, contract)
            else:
                res.problems += _starter_settings(o, contract)
            res.review_flags += flags
        if o.kind in ("hidden", "public_test"):
            res.problems += _skipped_tests(o, template)
        if o.kind == "public_test":
            res.problems += _public_cases(o, template, plan, contract)
            res.problems += _public_row_uses(o, contract, plan)
            if is_java(template.manifest):
                res.problems += _public_settings(o, contract)
        if o.kind == "hidden":
            res.problems += _hidden_cases(o, template, plan, contract, hidden_cases_only)
            res.review_flags += _duplicate_labels(o, template)
        if o.kind == "ref_accepted":
            starter = next((x for x in overlays if x.kind == "starter"), None)
            res.problems += _harness_overrides(o, starter, template, plan)

    if readme is not None:
        res.review_flags += readme_flags(readme, spec=spec, seed=seed, avoid_terms=avoid_terms)
    res.review_flags = list(dict.fromkeys(res.review_flags))
    return res


def readme_flags(
    readme: GeneratedFile,
    *,
    spec: SpecView,
    seed: FeatureSeed | None = None,
    avoid_terms: Sequence[str] = (),
) -> list[str]:
    """지원자 README 검사 — 승인자 표시(review_flags)만. README 는 승인된 명세 문장으로 결정적으로 만든다 — 걸리면 명세 문제
    (재생성으로 못 고침). 표시는 '{README} …' 로 시작한다 (승인 화면 README 경고 칸이 이 머리로 고른다 — api/routers/builds.py).
    #232: 운영 빌드(builder/steps.py)는 README 를 렌더만 하고 검사하지 않아 그 칸이 늘 비어 있었다 — 평가 러너의
    static_check(…, readme=…)와 같은 문장을 이 함수 하나가 만든다.
    '진행 방식' 의 자료 목록 줄(명세의 1단계 자료 제목 그대로 — spec.artifact_titles)은 명세 검사와 같은 허용으로 본다
    — 운영 조건 낱말(trigger_terms)이 재료의 업무 요구·자료 종류 이름에 나왔으면 괜찮다 (spec/feature.title_allowed_text).
    숨은 요구·원인 낱말·먼저 말할 개념은 명세 검사처럼 자료 제목에서도 표시한다. 2026-11-18: 이 허용을 몰라 명세가 통과시킨
    자료 제목을 '명세 수정 필요'로 표시했다 — 알파 README 49개 중 33개, 걸린 44줄이 모두 자료 목록 줄."""
    terms = hidden_terms(seed, spec)
    titles = set(spec.artifact_titles)
    listed: list[str] = []
    rest: list[str] = []
    for line in readme.content.splitlines():
        (listed if line.strip().removeprefix("-").strip() in titles else rest).append(line)
    # 명세 검사와 같은 허용 — 운영 조건 낱말만. 숨은 요구·원인 낱말·먼저 말할 개념은 자료 제목에서도 안 된다
    exempt: set[str] = set()
    if seed is not None:
        strict = {*spec.concepts, *seed.leak_terms, *seed.stage2_cause_terms}
        allowed = title_allowed_text(seed)
        exempt = {t for t in seed.trigger_terms if t not in strict and find_terms([t], allowed)}
    shown = [
        *(
            s
            for _, hit in _leaks([GeneratedFile(path=readme.path, content="\n".join(rest))], terms)
            for s in hit
        ),
        *(
            s
            for _, hit in _leaks(
                [GeneratedFile(path=readme.path, content="\n".join(listed))],
                [t for t in terms if t not in exempt],
            )
            for s in hit
        ),
    ]
    out = (
        [
            f"{README} 에 숨은 요구·운영 조건을 노출하는 말: {', '.join(dict.fromkeys(shown))} — 승인한 명세 문장에서 유래함, 명세 수정 필요"
        ]
        if shown
        else []
    )
    out += _hygiene(None, [readme], avoid_terms)
    out += _secret_hints(None, [readme])
    return out


def _imports_package(stmt: ast.stmt, pkg: str) -> bool:
    if isinstance(stmt, ast.ImportFrom):
        return (stmt.module or "").split(".")[0] == pkg and stmt.level == 0
    return isinstance(stmt, ast.Import) and any(a.name.split(".")[0] == pkg for a in stmt.names)


def _touches_sys_path(stmt: ast.stmt) -> bool:
    for n in ast.walk(stmt):
        t = (
            n.func.value
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            else (
                n.targets[0].value
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript)
                else None
            )
        )
        if (
            isinstance(t, ast.Attribute)
            and t.attr == "path"
            and isinstance(t.value, ast.Name)
            and t.value.id == "sys"
        ):
            return True
    return False


def _script_imports(o: Overlay, t: Template, contract: TaskContract | None) -> list[StaticProblem]:
    """Python 명령 스크립트가 저장소 루트를 sys.path 에 넣기 전에 자기 패키지(pipeline)를 import — `python pipeline/x.py` 로 실행하면
    sys.path[0] 이 pipeline/ 라 ModuleNotFoundError. 2026-11-10 데이터 알파 14회차: 허용 구현이 입력 도우미를 맨 위에서 import 해 두 번 다 실패."""
    if contract is None or not is_python(t.manifest):
        return []
    commands = {e.path for e in contract.endpoints if e.method is None}
    out: list[StaticProblem] = []
    for f in o.files:
        if f.path not in commands or not f.path.endswith(".py"):
            continue
        pkg = PurePosixPath(f.path).parts[0]
        try:
            body = ast.parse(f.content).body
        except SyntaxError:
            continue
        fixed = False
        for stmt in body:
            if _touches_sys_path(stmt):
                fixed = True
            elif _imports_package(stmt, pkg) and not fixed:
                out.append(
                    StaticProblem(
                        o.target,
                        f"{o.name} 의 명령 스크립트 {f.path}:{stmt.lineno} 가 저장소 루트를 sys.path 에 넣기 전에 {pkg} 를 import — "
                        f"`python {f.path}` 로 실행하면 ModuleNotFoundError. 템플릿 뼈대처럼 그 import 앞에 "
                        "`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`",
                    )
                )
                break
    return out


_HELP_FLAGS = frozenset({"--help", "-h"})
_CLI_OPTION = re.compile(r"^--?[A-Za-z]")
_EXIT_ATTRS = frozenset({"returncode", "code", "exit_code"})
_STUB_ERROR = "NotImplementedError"
_PyFunc = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class _TestModule:
    """시작 코드 테스트 파일 하나에서 새 명령을 가리키는 것들 (_starter_stub_tests)."""

    commands: tuple[str, ...]  # 새 명령 스크립트 경로 (pipeline/build_revenue.py)
    values: dict[str, ast.expr]  # 모듈 수준 이름 = 값 — 명령 경로·인자 목록 상수를 따라간다
    funcs: dict[str, _PyFunc]  # 모듈 수준 함수
    # 새 명령 모듈에서 가져온 이름 (from pipeline.x import main · import pipeline.x as m)
    imported: frozenset[str]

    def is_command(self, value: str) -> bool:
        return any(
            value in (c, PurePosixPath(c).name, c.removesuffix(".py").replace("/", "."))
            or value.endswith("/" + c)
            for c in self.commands
        )

    def strings(self, node: ast.AST, seen: frozenset[str] = frozenset()) -> list[str]:
        """노드 안 문자열 상수 — 모듈 수준 이름은 그 값까지."""
        out: list[str] = []
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
            elif isinstance(n, ast.Name) and n.id in self.values and n.id not in seen:
                out += self.strings(self.values[n.id], seen | {n.id})
        return out

    def runs_command(self, node: ast.AST) -> bool:
        return any(self.is_command(s) for s in self.strings(node))


def _test_module(tree: ast.Module, commands: Sequence[str]) -> _TestModule:
    modules = {c.removesuffix(".py").replace("/", ".") for c in commands}
    values: dict[str, ast.expr] = {}
    funcs: dict[str, _PyFunc] = {}
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            values.update({t.id: node.value for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            values[node.target.id] = node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
        elif isinstance(node, ast.ImportFrom) and node.module in modules and not node.level:
            imported |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.asname or a.name for a in node.names if a.name in modules}
    return _TestModule(tuple(commands), values, funcs, frozenset(imported))


def _py_tests(tree: ast.Module) -> list[_PyFunc]:
    """pytest 가 모으는 테스트 함수 — 모듈의 test… 와 Test… 클래스 안의 test…."""
    out: list[_PyFunc] = []
    for node in tree.body:
        body = (
            node.body if isinstance(node, ast.ClassDef) and node.name.startswith("Test") else [node]
        )
        out += [
            n
            for n in body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        ]
    return out


def _dotted(node: ast.expr) -> str:
    """호출 대상의 점 이름 — main · m.main · pipeline.build_revenue.main (이름이 아니면 빈 문자열)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


def _option_args(strings: Sequence[str]) -> bool:
    """실행 인자에 옵션이 있고 도움말 요청이 아님."""
    return not _HELP_FLAGS.intersection(strings) and any(_CLI_OPTION.match(s) for s in strings)


def _runs_with_options(fn: _PyFunc, m: _TestModule, runners: Collection[str]) -> bool:
    """새 명령을 옵션과 함께 실행 — 인자 목록 그대로([sys.executable, "pipeline/x.py", "--date", …]), 그 명령을 실행하는 도우미 함수에
    옵션을 넘김, 새 명령 모듈의 main([...]) 호출."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.List, ast.Tuple)) and m.runs_command(n):
            if _option_args(m.strings(n)):
                return True
        elif isinstance(n, ast.Call):
            name = _dotted(n.func)
            main = name.rpartition(".")[2] == "main" and name.split(".")[0] in m.imported
            if name in runners or main:
                args = [*n.args, *(k.value for k in n.keywords)]
                if _option_args([s for a in args for s in m.strings(a)]):
                    return True
    return False


def _exit2_only(fn: _PyFunc, m: _TestModule) -> bool:
    """실행 결과를 종료 코드 2(인자 오류 — argparse)로만 확인 — 뼈대의 인자 검사라 구현 뒤에도 그대로다."""
    codes: list[tuple[type[ast.cmpop], object]] = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Compare):
            continue
        sides = [n.left, *n.comparators]
        if not any(
            (isinstance(s, ast.Attribute) and s.attr in _EXIT_ATTRS)
            or (isinstance(s, ast.Call) and _dotted(s.func).split(".")[0] in m.imported)
            for s in sides
        ):
            continue
        for op, a, b in zip(n.ops, sides, sides[1:], strict=False):
            const = b if isinstance(b, ast.Constant) else a if isinstance(a, ast.Constant) else None
            codes.append((type(op), const.value if const is not None else None))
    return bool(codes) and all(c == (ast.Eq, 2) for c in codes)


def _asserts_stub(fn: _PyFunc, m: _TestModule, seen: frozenset[str] = frozenset()) -> bool:
    """자리 표시의 예외를 확인 — pytest.raises(NotImplementedError)·`"NotImplementedError" in p.stderr` (부르는 모듈 함수 안까지).
    설명 글(docstring)의 낱말은 보지 않는다."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            if n.id == _STUB_ERROR:
                return True
            if (
                n.id in m.funcs
                and n.id not in seen
                and _asserts_stub(m.funcs[n.id], m, seen | {n.id})
            ):
                return True
        elif isinstance(n, (ast.Compare, ast.keyword)) and any(
            isinstance(c, ast.Constant) and isinstance(c.value, str) and _STUB_ERROR in c.value
            for c in ast.walk(n)
        ):
            return True
    return False


def _starter_stub_tests(
    o: Overlay, t: Template, contract: TaskContract | None, spec: SpecView
) -> list[StaticProblem]:
    """Python 시작 코드의 기존 테스트가 새 명령의 자리 표시를 단언 — 새 명령을 옵션과 함께 실행해 실패 종료를 보거나 NotImplementedError 를
    확인하면 기능을 채운 구현(참조 구현·지원자)에서 반드시 깨진다. 새 명령은 `--help`(계약 옵션이 보이는지)와 인자 오류(종료 코드 2)까지만.
    2026-11-15 g187c-de-kb002 · 2026-11-24 #256 재료 C 알파 2회: '옵션을 주면 종료 코드 1 · stderr 에 NotImplementedError' 를 단언해 참조 구현이
    모두 깼다 — 저장된 데이터 시작 코드 32개 중 3개, 셋 다 빌드 실패. Java 의 새 API 호출 검사(_starter_new_api_tests)는 HTTP 만 본다."""
    if contract is None or not is_python(t.manifest):
        return []
    existing = existing_endpoints(contract, spec)
    commands = [e.path for e in contract.endpoints if e.method is None and e not in existing]
    if not commands:
        return []
    hits: list[str] = []
    for f in o.files:
        name = PurePosixPath(f.path).name
        if not (f.path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue
        m = _test_module(tree, commands)
        runners = {
            k
            for k, fn in m.funcs.items()
            if not k.startswith("test")
            and any(
                isinstance(n, (ast.List, ast.Tuple)) and m.runs_command(n) for n in ast.walk(fn)
            )
        }
        for fn in _py_tests(tree):
            touches = m.runs_command(fn) or any(
                isinstance(n, ast.Name) and (n.id in m.imported or n.id in runners)
                for n in ast.walk(fn)
            )
            if touches and (
                _asserts_stub(fn, m)
                or (_runs_with_options(fn, m, runners) and not _exit2_only(fn, m))
            ):
                hits.append(f"{f.path}::{fn.name}")
    if not hits:
        return []
    return [
        StaticProblem(
            o.target,
            f"시작 코드의 기존 테스트가 새 명령({', '.join(commands)})의 자리 표시를 단언함: {', '.join(hits[:4])} — "
            "새 명령을 옵션과 함께 실행해 실패 종료를 보거나 NotImplementedError 를 확인하면 기능을 채운 구현에서 반드시 깨진다. "
            "새 기능 테스트는 쓰지 않는다 (지원자 몫) — 새 명령은 `--help` 가 계약 옵션을 보여 주는지까지만 확인한다",
        )
    ]


def _skipped_tests(o: Overlay, t: Template) -> list[StaticProblem]:
    """숨은·공개 테스트의 건너뛰기 API (#173) — 조건부 건너뛰기는 구현에 따라 판정이 갈린다."""
    hits = [h for f in o.files for h in skip_hits(t.manifest, f.path, f.content)]
    if not hits:
        return []
    return [
        StaticProblem(o.target, f"{o.name} 이 테스트를 건너뜀: {'; '.join(hits[:4])} — {SKIP_NOTE}")
    ]


def _keep_lines(src: str) -> str:
    """C 계열 주석을 빼되 줄 수는 그대로 (찾은 곳의 줄 번호가 맞게)."""
    return _JAVA_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n") or " ", src)


def _code_pitfalls(o: Overlay, t: Template) -> list[StaticProblem]:
    """manifest code_pitfalls — 이 스택에서 생성 모델이 되풀이하는 API 실수·채점 우회를 실행 전에 (파일·줄과 바른 쓰는 법).
    항목이 어느 산출물(overlays)·경로(paths)·조건(when — 이 꼴이 있는 파일만, unless — 이 꼴이 있으면 뺌)에서 거는지 정한다.
    #176: Java 전용 규칙(TestRestTemplate PATCH·코드로 풀·스레드 바꾸기)이 공용 검사기에 박혀 있던 것을 매니페스트 항목으로."""
    slot = KIND_SLOT[o.kind]
    out: list[StaticProblem] = []
    for pit in t.manifest.code_pitfalls:
        if slot not in pit.overlays:
            continue
        rx = re.compile(pit.pattern)
        ok = re.compile(pit.unless) if pit.unless else None
        when = re.compile(pit.when) if pit.when else None
        hits: list[str] = []
        for f in o.files:
            if pit.paths and not glob_match(f.path, pit.paths):
                continue
            body = _keep_lines(f.content) if pit.strip_comments else f.content
            if (ok is not None and ok.search(body)) or (when is not None and not when.search(body)):
                continue
            hits += [
                f"{f.path}:{body.count(chr(10), 0, m.start()) + 1} `{' '.join(m.group(0).split())[:80]}`"
                for m in rx.finditer(body)
            ]
        if hits:
            lead = (
                f"{o.name} 가 {HARNESS_OVERRIDE}(연결 풀·대기 시간·요청 스레드)를 코드로 우회함"
                if pit.harness_bypass
                else f"{o.name} 에 이 스택에서 되풀이되는 실수"
            )
            out.append(StaticProblem(o.target, f"{lead}: {'; '.join(hits[:4])} — {pit.fix}"))
    return out


def overlay_from_dir(kind: FileKind, root: Path, label: str = "") -> Overlay:
    """폴더 안 파일 → Overlay (평가·테스트용). 경로는 root 기준."""
    files = tuple(
        GeneratedFile(path=p.relative_to(root).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
    return Overlay(kind=kind, files=files, label=label)
