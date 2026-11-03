"""실행 관리자 로그 — 노드 이름·시간만 남고 State 값은 남지 않는다."""

from __future__ import annotations

import asyncio
import logging

from task_spec.api.launcher import RunLauncher
from tests.conftest import fake_doc, run_input


async def test_launcher_logs_node_progress_without_state(memory_graph, caplog) -> None:  # type: ignore[no-untyped-def]
    launcher = RunLauncher(memory_graph)
    inp = run_input(docs=[fake_doc("d1", "resume")])
    with caplog.at_level(logging.INFO, logger="task_spec.api.launcher"):
        launcher.launch(inp["run_id"], inp)
        for _ in range(200):
            if not launcher.is_active(inp["run_id"]):
                break
            await asyncio.sleep(0.01)
    text = caplog.text
    assert f"run start run_id={inp['run_id']}" in text
    assert "node done" in text and "node=draft_spec" in text
    assert "run waiting approval" in text  # 승인 interrupt 에서 멈춤
    assert "posting-fake-001" not in text  # State 값은 로그에 없음
    await launcher.shutdown()
