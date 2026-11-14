"""Node verifier 판정 — tsc 출력·vitest JSON 보고서를 TargetRun 결과로 (노드 없이, 실제 vitest 3.2 실패 모양을 본뜬 가상 보고서)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import load_template
from task_spec.starter.verify.node import clean_message, tsc_errors, vitest_results

FE = load_template("frontend/react-vite-ts")
WORK = Path("/work/r1-naive")


def _file(
    rel: str, tests: list[dict[str, Any]], status: str = "", message: str = ""
) -> dict[str, Any]:
    bad = any(t["status"] == "failed" for t in tests)
    return {
        "name": str(WORK / rel),
        "status": status or ("failed" if bad else "passed"),
        "message": message,
        "assertionResults": tests,
    }


def _t(describe: str, title: str, failure: str = "") -> dict[str, Any]:
    return {
        "ancestorTitles": [describe],
        "title": title,
        "status": "failed" if failure else "passed",
        "failureMessages": [failure] if failure else [],
    }


def test_tsc_errors_keep_position_message_and_continuation() -> None:
    log = (
        "$ npx tsc --noEmit --pretty false\n"
        "src/__grading__/cases/hc1.test.tsx(12,5): error TS2339: Property 'nope' does not exist on type 'Api'.\n"
        "src/App.tsx(3,7): error TS2322: Type 'number' is not assignable to type 'string'.\n"
        "  Type 'number' is not assignable to type 'string'.\n"
    )
    errs = tsc_errors(log)
    assert [(e.path, e.line) for e in errs] == [
        ("src/__grading__/cases/hc1.test.tsx", 12),
        ("src/App.tsx", 3),
    ]
    assert errs[0].message.startswith("TS2339: Property 'nope'")
    assert errs[1].detail.startswith("Type 'number'")


def test_clean_message_drops_colors_dom_dump_and_stack() -> None:
    raw = (
        'Error: Unable to find role="dialog"\n\nIgnored nodes: comments, script, style\n'
        "\x1b[36m<body>\x1b[39m\n  <div />"
    )
    assert clean_message(raw) == 'Error: Unable to find role="dialog"'
    assert clean_message("AssertionError: x\n    at /work/a.ts:1:2") == "AssertionError: x"


def test_vitest_report_becomes_one_result_per_file_with_case_ids_and_reasons() -> None:
    hidden = Overlay(
        kind="hidden",
        files=(
            GeneratedFile(path="src/__grading__/cases/late.test.tsx", content="// hc3 늦은 실패\n"),
        ),
    )
    report = {
        "testResults": [
            _file(
                "src/__grading__/cases/hc1.test.tsx",
                [
                    _t("hc1 늦은 응답", "준비", ""),
                    _t(
                        "hc1 늦은 응답",
                        "최신 입력",
                        "AssertionError: [reason:stale_response_shown] 목록이 바뀜: expected 1 to be 2\n    at x",
                    ),
                ],
            ),
            _file(
                "src/__grading__/cases/hc2.test.tsx",
                [
                    _t(
                        "hc2 지움",
                        "다시 안 뜸",
                        'Error: Unable to find role="combobox"\n\nIgnored nodes: …',
                    )
                ],
            ),
            _file(
                "src/__grading__/cases/late.test.tsx",
                [
                    _t(
                        "늦은 실패",
                        "알림 없음",
                        "Error: \x1b[2mexpect(\x1b[22melement).not.toBeInTheDocument()",
                    )
                ],
            ),
            _file(
                "src/__grading__/cases/hc4.test.tsx",
                [],
                "failed",
                'Failed to resolve import "./nope"',
            ),
            _file(
                "src/__grading__/selftest/harness.selftest.test.tsx", [_t("하네스 자체 점검", "a")]
            ),
            _file("src/__public__/pt1.test.tsx", [_t("pt1 추천이 보인다", "순서대로")]),
            _file(
                "src/App.test.tsx",
                [_t("App", "그린다", "TypeError: Cannot read properties of undefined")],
            ),
        ]
    }
    got = {(r.group, r.name): r for r in vitest_results(report, WORK, FE, hidden)}
    assert got[("hidden", "hc1")].result == "ASSERT"
    assert got[("hidden", "hc1")].reason == "stale_response_shown"
    assert got[("hidden", "hc1")].message.startswith("최신 입력 — AssertionError:")
    # 요소를 못 찾음 = 단언이 아닌 예외 (구현끼리 비교해 탓을 정한다)
    assert got[("hidden", "hc2")].result == "HARNESS_ERR" and got[("hidden", "hc2")].reason is None
    # describe 제목에 id 가 없으면 케이스 파일 본문에서
    assert got[("hidden", "hc3")].result == "ASSERT" and got[("hidden", "hc3")].reason is None
    # 파일을 못 읽음 — 케이스 id 는 파일 이름에서 못 뽑으면 경로
    assert got[("hidden", "__grading__/cases/hc4.test.tsx")].result == "HARNESS_ERR"
    assert got[("selftest", "__grading__/selftest/harness.selftest.test.tsx")].result == "PASS"
    assert got[("public", "pt1")].result == "PASS"
    assert got[("existing", "App.test.tsx")].result == "HARNESS_ERR"


def test_failed_line_is_a_separate_field_only_for_build_verification() -> None:
    """실패한 줄(at)은 수리 안내용 칸 — 메시지에 붙이지 않는다. 채점(grade, locate 끔)에는 넣지 않는다 (숨은 테스트 파일·줄)."""
    from task_spec.starter.grade import image_grade_node as judge

    path = "src/__grading__/cases/hc2.test.tsx"
    stack = f"AssertionError: expected 2 to be greater than or equal to 3\n    at {WORK / path}:66:7\n    at file:///x/chunk-hooks.js:155:11"
    report = {"testResults": [_file(path, [_t("hc2 재연결", "중복 없음", stack)])]}
    got = vitest_results(report, WORK, FE, None)[0]
    assert got.at == f"{path}:66" and "66" not in got.message
    grade = judge.vitest_results(report, WORK, {"cases": "src/__grading__/cases"}, r"\bhc\d{1,2}\b")
    assert "at" not in grade[0]


def test_skipped_todo_and_conditional_skips_are_not_a_pass() -> None:
    """#173: status 가 failed 인 것만 실패로 봐 it.skip·skipIf·ctx.skip()·todo 가 통과였다 — 모두 건너뛴 파일도 파일 상태는 passed."""
    from task_spec.starter.grade.image_grade_node import SKIPPED

    skipped = {**_t("hc1 늦은 응답", "출력이 있으면"), "status": "skipped"}
    todo = {**_t("hc2 지움", "나중에"), "status": "todo"}
    report = {
        "testResults": [
            _file("src/__grading__/cases/hc1.test.tsx", [_t("hc1 늦은 응답", "준비"), skipped]),
            _file("src/__grading__/cases/hc2.test.tsx", [todo], status="passed"),
            _file("src/__public__/pt1.test.tsx", [_t("pt1 추천이 보인다", "순서대로")]),
        ]
    }
    got = {r.name: r for r in vitest_results(report, WORK, FE, None)}
    assert got["hc1"].result == "HARNESS_ERR" and SKIPPED in got["hc1"].message
    assert got["hc2"].result == "HARNESS_ERR"
    assert got["pt1"].result == "PASS"
