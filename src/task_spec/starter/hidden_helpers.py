"""숨은 테스트 도우미를 계약에서 만든다 (템플릿 manifest `hidden_helper`) — 숨은 테스트가 어긋날 수 없게 구성으로 막는다.

2026-11-07~08 알파 평가: 숨은 테스트가 계약·시드와 어긋나는 실패가 반복됐다 — 배정받지 않은 id 를 쓰고, 응답 본문을 계약과 다른 모양으로
파싱하고, `Duration.ofSeconds(REQUEST_TIMEOUT)` 처럼 하네스 상수를 잘못 감싸 컴파일이 안 됐다(재평가 1 의 컴파일 오류 대부분).
검사로 잡는 대신, 이 과제의 계약에서 코드가 Java 추상 클래스를 만들어 숨은 케이스가 상속하게 한다:
- 케이스별 데이터 범위 상수 (`allocate_rows` 가 나눈 그대로 — 시드에 반드시 있는 행)
- 계약 테이블 이름 상수 (오류 주입 정규식용)
- 계약 API 호출 함수 (경로·메서드 그대로) + 200 응답 본문을 계약 `returns` 모양(목록/객체)으로 읽는 함수
- `peak(expectedStatus, call)` — 조절값(동시 요청 수·버스트 수)과 시간 한도를 넣은 동시 요청
- `num(v)` — JSON 숫자 → long
조절값 파일처럼 숨은 테스트 생성기는 이 파일을 쓰지 않는다 (gen_hidden 이 코드 렌더본으로 바꾼다).
"""

from __future__ import annotations

import re
from typing import Literal

from task_spec.schemas.build import Endpoint, GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.seed_rows import allocate_rows, public_free
from task_spec.starter.state_rule import next_states, sentence
from task_spec.starter.tables import contract_tables, spring_snake

_SEG = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")


def _camel(parts: list[str]) -> str:
    words = [w for p in parts for w in re.split(r"[^A-Za-z0-9]+", p) if w]
    return "".join(w[:1].upper() + w[1:] for w in words)


def method_name(e: Endpoint) -> str:
    """POST /api/transactions/{id}/status-change → postApiTransactionsIdStatusChange."""
    segs = [s for s in e.path.strip("/").split("/") if s]
    return (e.method or "call").lower() + _camel([_SEG.sub(r"\1", s) for s in segs])


def _path_expr(path: str) -> tuple[str, list[str]]:
    """'/orders/{id}/status' → ('"/orders/" + id + "/status"', ['id'])."""
    out: list[str] = []
    names: list[str] = []
    pos = 0
    for m in _SEG.finditer(path):
        if m.start() > pos:
            out.append('"' + path[pos : m.start()] + '"')
        out.append(m.group(1))
        names.append(m.group(1))
        pos = m.end()
    if pos < len(path):
        out.append('"' + path[pos:] + '"')
    return " + ".join(out) or '"/"', names


def _const(name: str) -> str:
    return spring_snake(name).upper()


def _body_kind(e: Endpoint) -> str | None:
    ok = next((r for r in e.responses if r.status == 200), None)
    if ok is None:
        return None
    if ok.returns:
        return "list" if ok.returns.endswith("[]") else "object"
    return "object" if ok.body else None


def public_rows(contract: TaskContract, plan: HiddenPlan) -> dict[str, list[int]]:
    """공개 테스트 몫 행 — 상수 이름(PUBLIC_<엔티티>_<처음 값>) → id 들 (seed_rows.public_free — fit_seed 가 몫을 채운다)."""
    names = {m.entity_id: m.name for m in contract.entities}
    out: dict[str, list[int]] = {}
    for eid, values, ids in public_free(contract, plan):
        if eid not in names or not ids:
            continue
        vals = "_".join(
            re.sub(r"[^A-Za-z0-9]+", "_", str(v)).strip("_").upper() for v in values.values()
        )
        out[f"PUBLIC_{_const(names[eid])}" + (f"_{vals}" if vals else "")] = ids
    return out


def _value_const(v: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(v)).strip("_").upper()


def _state_lines(contract: TaskContract) -> list[str]:
    """처음 값별 다음 상태 — 계약 state 칸에서 (state_rule.next_states). 테스트가 전이를 짐작하지 않게."""
    steps = list(next_states(contract))
    if not steps:
        return []
    lines = [
        "",
        "    // ── 상태 전이 (계약 state 칸): 처음 값별로 허용되는 다음 값(NEXT_OK_)과 거절되는 다음 값(NEXT_BAD_) ──",
    ]
    for m in contract.entities:
        if m.state is not None:
            lines.append(f"    // {m.name}.{m.state.field} — {sentence(m.state)}")
    for name, v, ok, bad in steps:
        key = f"{_const(name)}_{_value_const(v)}"
        if ok is not None:
            lines.append(f'    protected static final String NEXT_OK_{key} = "{ok}";')
        if bad is not None:
            lines.append(f'    protected static final String NEXT_BAD_{key} = "{bad}";')
    return lines


def render_contract_case(
    contract: TaskContract,
    plan: HiddenPlan,
    manifest: TemplateManifest,
    audience: Literal["hidden", "public"] = "hidden",
) -> GeneratedFile | None:
    """audience=hidden: 숨은 케이스 도우미(ContractCase — 케이스 범위·테이블·peak). public: 공개 테스트 도우미(PublicCase —
    공개 테스트 몫 행, 동시 요청·오류 주입 없음 — 공개 테스트는 숨은 요구를 시험하지 않는다)."""
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    names = {m.entity_id: m.name for m in contract.entities}
    lines = [
        f"package {rule.package};",
        "",
        "import com.example.service.grading.Burst;",
        "import com.example.service.grading.GraderParams;",
        "import com.example.service.grading.GraderSupport;",
        "import com.fasterxml.jackson.core.type.TypeReference;",
        "import java.net.http.HttpResponse;",
        "import java.util.List;",
        "import java.util.Map;",
        "import java.util.function.IntUnaryOperator;",
        "",
        "/**",
        f" * 이 과제의 공개 계약에서 빌드 파이프라인이 만든 도우미 — 손으로 고치지 않는다. {'숨은 케이스' if audience == 'hidden' else '공개 테스트'}는 이 클래스를 상속한다.",
        (
            " * id·경로·응답 파싱·동시 요청 시간 한도를 직접 쓰지 말고 아래 상수·함수를 쓴다."
            if audience == "hidden"
            else " * id·경로·응답 파싱을 직접 쓰지 말고 아래 상수·함수를 쓴다."
        ),
        " */",
        f"abstract class {rule.class_name} extends GraderSupport {{",
        "",
    ]
    if audience == "public":
        lines += [
            "    // ── 공개 테스트 몫 행: 계약 시드에 반드시 있고 처음 값이 이름대로, 숨은 케이스·지원자 테스트와 겹치지 않는다 ──",
            "    // 테스트 메서드마다 서로 다른 id 를 쓴다 (테스트끼리 같은 DB 를 함께 쓰고 순서는 정해지지 않음)",
        ]
        lines += [
            f"    protected static final long[] {k} = {{{', '.join(f'{i}L' for i in ids)}}};"
            for k, ids in public_rows(contract, plan).items()
        ]
    else:
        lines += [
            "    // ── 케이스별 데이터 범위: 계약 시드에 반드시 있는 행, 다른 케이스·지원자 테스트(작은 id)와 겹치지 않는다 ──"
        ]
    ranges = allocate_rows(contract, plan).ranges if audience == "hidden" else []
    per_case: dict[str, list] = {}
    for r in ranges:
        per_case.setdefault(r.case_id, []).append(r)
    for case_id, rs in per_case.items():
        for r in rs:
            prefix = (
                case_id.upper()
                if len(rs) == 1
                else f"{case_id.upper()}_{_const(names.get(r.entity_id, r.entity_id))}"
            )
            init = ", ".join(f"{k}={v}" for k, v in r.initial.items())
            lines += [
                f"    /** {case_id.upper()} {names.get(r.entity_id, r.entity_id)} id {r.first}~{r.last} ({r.rows}행"
                + (f", 처음 {init}" if init else "")
                + (", 버스트 — 요청마다 FIRST + index" if r.burst else "")
                + ") */",
                f"    protected static final long {prefix}_FIRST = {r.first}L;",
                f"    protected static final long {prefix}_LAST = {r.last}L;",
                f"    protected static final int {prefix}_ROWS = {r.rows};",
            ]
    lines += _state_lines(contract)
    tables = contract_tables(contract, manifest.naming.table) if audience == "hidden" else {}
    if tables:
        lines += ["", "    // ── 계약 테이블 이름 (오류 주입 정규식에는 이 이름을 그대로) ──"]
        lines += [
            f'    protected static final String TABLE_{_const(names[eid])} = "{t}";'
            for eid, t in tables.items()
        ]
    lines += ["", "    // ── 계약 API — 경로·메서드는 계약 그대로 ──"]
    seen: set[str] = set()
    for e in contract.endpoints:
        if not e.method:
            continue
        name = method_name(e)
        if name in seen:
            name += e.endpoint_id.upper()
        seen.add(name)
        expr, vars_ = _path_expr(e.path)
        params = [f"Object {v}" for v in vars_]
        has_body = e.method.upper() not in ("GET", "DELETE")
        call: str
        if e.headers or has_body:
            params.append("Map<String, String> headers")
            if has_body:
                params.append("String jsonBody")
            call = (
                f'send("{e.method.upper()}", {expr}, headers, {"jsonBody" if has_body else "null"})'
            )
        else:
            call = f"get({expr})"
        lines += [
            f"    /** {e.endpoint_id} {e.method.upper()} {e.path} — {e.summary} */",
            f"    protected HttpResponse<String> {name}({', '.join(params)}) throws Exception {{",
            f"        return {call};",
            "    }",
        ]
        kind = _body_kind(e)
        if kind == "list":
            lines += [
                f"    /** {e.endpoint_id} 의 200 본문 — 계약상 목록 */",
                f"    protected List<Map<String, Object>> {name}Body(HttpResponse<String> response) throws Exception {{",
                "        return json(response.body(), new TypeReference<List<Map<String, Object>>>() {});",
                "    }",
            ]
        elif kind == "object":
            lines += [
                f"    /** {e.endpoint_id} 의 200 본문 — 계약상 객체 하나 */",
                f"    protected Map<String, Object> {name}Body(HttpResponse<String> response) throws Exception {{",
                "        return jsonObject(response.body());",
                "    }",
            ]
    burst = manifest.burst_rows if audience == "hidden" else []
    if burst:
        c_name, r_name = burst[0], (burst[1] if len(burst) > 1 else None)
        c_raw = plan.harness.get(c_name, 1)
        c_default = c_raw if isinstance(c_raw, int) else 1
        r_default = plan.harness.get(r_name, 1) if r_name else 1
        rounds = (
            f'GraderParams.intValue("{r_name}", {int(r_default) if isinstance(r_default, int) else 1})'
            if r_name
            else "1"
        )
        lines += [
            "",
            "    /** 운영 피크 재현 — 동시 요청 수만큼씩 버스트 수만큼 차례로, 시간 한도 REQUEST_TIMEOUT 까지. index 는 0 ~ 전체-1. */",
            "    protected List<Burst.Outcome> peak(IntUnaryOperator expectedStatus, Burst.Call call) throws Exception {",
            f'        return Burst.rounds(GraderParams.intValue("{c_name}", {c_default}), {rounds}, expectedStatus, call, REQUEST_TIMEOUT);',
            "    }",
        ]
    lines += [
        "",
        "    /** JSON 숫자 → long (크기에 따라 Integer·Long 으로 읽혀 그대로 비교하면 틀린다) */",
        "    protected static long num(Object value) {",
        "        return ((Number) value).longValue();",
        "    }",
        "}",
        "",
    ]
    return GeneratedFile(path=rule.path, content="\n".join(lines))


def render_persistence_check(
    contract: TaskContract, manifest: TemplateManifest
) -> GeneratedFile | None:
    """새로 만드는 엔티티를 앱이 저장할 수 있는가 — 공개 테스트 자리에 코드가 쓰는 저장 확인 (LLM 없음).

    2026-11-08 알파 4케이스: 시작 코드의 새 감사 기록 엔티티에 id 생성 전략이 없어(`@Id` 만) 참조 구현 둘 다 기록을 저장하는 순간
    'must be manually assigned before persist()' 로 실패했다. 시작 코드는 기록을 쓰지 않으니 자기 테스트로는 드러나지 않는다.
    JPA 메타모델에서 계약 엔티티 이름으로 클래스를 찾아(클래스 이름을 미리 몰라도 됨) 필수 필드를 채워 저장해 본다.
    대상: 새로 만드는 엔티티(existing 아님) 중 처음 0행 — 표시가 하나도 없으면(옛 계약) 처음 0행인 엔티티.
    """
    rule = manifest.public_helper
    if rule is None:
        return None
    rows = {s.entity_id: s.rows for s in contract.seed}
    flagged = any(m.existing for m in contract.entities)
    targets = [
        m.name
        for m in contract.entities
        if rows.get(m.entity_id, 0) == 0 and (not m.existing if flagged else True)
    ]
    if not targets:
        return None
    path = rule.path.rsplit("/", 1)[0] + "/PersistenceCheckTest.java"
    names = ", ".join(f'"{t}"' for t in targets)
    src = f"""package {rule.package};

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import jakarta.persistence.metamodel.EntityType;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/** 빌드 파이프라인이 계약에서 만든 저장 확인 — 새로 만드는 엔티티는 앱이 저장할 수 있어야 한다 (손으로 고치지 않는다). */
@DisplayName("저장 확인 — 새로 만드는 엔티티")
class PersistenceCheckTest extends {rule.class_name} {{

    @PersistenceContext
    EntityManager em;

    @Autowired
    PlatformTransactionManager tx;

    @Test
    void 새로_만드는_엔티티를_저장하면_id_가_생긴다() throws Exception {{
        for (String name : new String[] {{{names}}}) {{
            EntityType<?> type = em.getMetamodel().getEntities().stream()
                    .filter(e -> e.getName().equals(name) || e.getJavaType().getSimpleName().equals(name))
                    .findFirst()
                    .orElse(null);
            assertThat(type).as("계약 엔티티 %s 의 JPA 엔티티 클래스 (클래스 이름은 계약 엔티티 이름)", name).isNotNull();
            Object row = newRow(type.getJavaType());
            new TransactionTemplate(tx).executeWithoutResult(status -> em.persist(row));
            Object id = em.getEntityManagerFactory().getPersistenceUnitUtil().getIdentifier(row);
            assertThat(id).as("%s 를 저장하면 id 가 생긴다 — 앱이 행을 만드는 엔티티라 id 생성 전략이 필요", name).isNotNull();
        }}
    }}

    private static Object newRow(Class<?> type) throws Exception {{
        var ctor = type.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object row = ctor.newInstance();
        for (Class<?> c = type; c != null && c != Object.class; c = c.getSuperclass()) {{
            for (Field f : c.getDeclaredFields()) {{
                if (Modifier.isStatic(f.getModifiers()) || f.isAnnotationPresent(jakarta.persistence.Id.class)) {{
                    continue;
                }}
                Object value = sample(f.getType());
                if (value != null) {{
                    f.setAccessible(true);
                    f.set(row, value);
                }}
            }}
        }}
        return row;
    }}

    private static Object sample(Class<?> t) {{
        if (t == String.class) return "x";
        if (t == Long.class || t == long.class) return 1L;
        if (t == Integer.class || t == int.class) return 1;
        if (t == Short.class || t == short.class) return (short) 1;
        if (t == Boolean.class || t == boolean.class) return false;
        if (t == Double.class || t == double.class) return 1.0;
        if (t == BigDecimal.class) return BigDecimal.ONE;
        if (t == LocalDateTime.class) return LocalDateTime.now();
        if (t == Instant.class) return Instant.now();
        if (t == OffsetDateTime.class) return OffsetDateTime.now();
        if (t == LocalDate.class) return LocalDate.now();
        if (t.isEnum() && t.getEnumConstants().length > 0) return t.getEnumConstants()[0];
        return null;
    }}
}}
"""
    return GeneratedFile(path=path, content=src)
