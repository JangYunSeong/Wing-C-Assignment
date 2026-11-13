"""진행자 — 작업 폴더의 `./ask` 가 부르는 작은 서버 (docs/design/analysis-stage.md §4.6, §9.1 진행자).

답은 starter/answer_rule.py 그대로 (판 1·4: §4.6 `answer`, 판 3: 지금 규칙 모드 `answer_legacy` + 한도).
풀이(서브에이전트)가 사실 파일·답 규칙 코드를 읽을 수 없게 진행자는 따로 떠 있는 프로세스이고, 작업 폴더에는 주소와 토큰만 둔다.
모든 질문·자료 요청은 시각과 함께 `<RUNS>/facilitator/logs/<label>.jsonl` 에 남는다 — 상태(항목 열림)와 한도는 이 기록에서 다시 계산한다.

  uv run python -m eval.pilot.facilitator serve [--port 8765]
  ./ask question "질문"   ./ask material   ./ask material 번호        (작업 폴더에서)
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from eval.pilot.model import RUNS, Mode
from task_spec.schemas.base import Schema
from task_spec.starter.answer_rule import Reply, State, answer, answer_legacy
from task_spec.starter.facilitator import Answer, Material

PORT = 8765
FAC = RUNS / "facilitator"
SESSIONS = FAC / "sessions"
LOGS = FAC / "logs"

QUESTION_LIMIT = "질문 한도({n}번)를 다 썼습니다. 더 물을 수 없습니다."
MATERIAL_LIMIT = "자료 요청 한도({n}개)를 다 썼습니다."
MATERIAL_BAD = (
    "자료 번호로 요청해 주세요 (./ask material 로 목록 확인). 횟수는 차감하지 않았습니다."
)
EMPTY_QUESTION = '질문을 함께 적어 주세요: ./ask question "질문"'

Kind = Literal["question", "material"]


class Session(Schema):
    """풀이 하나의 진행자 설정 — prepare 가 만든다."""

    label: str
    token: str = Field(min_length=16)
    mode: Mode
    items: list[Answer]
    materials: list[Material]
    max_questions: int | None = None  # None = 한도 없음 (§4.4)
    max_requests: int | None = None


class Entry(Schema):
    """진행자 기록 한 줄."""

    t: str  # 시각 (초까지)
    kind: Literal["question", "material_list", "material", "refused"]
    arg: str = ""
    reply: Reply | None = None  # 질문이면 답 규칙의 판단 그대로
    served: str | None = None  # 자료면 준 자료 id
    text: str = ""  # 돌려준 글 (자료 본문은 넣지 않고 id 만)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def states(entries: list[Entry]) -> dict[str, State]:
    """지금 항목 상태 — 마지막 질문 답의 states (없으면 전부 '없음')."""
    for e in reversed(entries):
        if e.kind == "question" and e.reply is not None and e.reply.states:
            return dict(e.reply.states)
    return {}


def handle(
    s: Session, entries: list[Entry], kind: Kind, arg: str, now: str | None = None
) -> tuple[str, Entry]:
    """요청 하나 → (돌려줄 글, 남길 기록). 기록을 읽고 쓰는 것은 서버 몫 — now 를 주면 시각도 고정 (테스트)."""
    arg = arg.strip()
    t = now or _now()
    if kind == "material":
        return _material(s, entries, arg, t)
    if not arg:
        return EMPTY_QUESTION, Entry(t=t, kind="refused", arg=arg, text=EMPTY_QUESTION)
    asked = sum(e.kind == "question" for e in entries)
    if (
        s.max_questions is not None and asked >= s.max_questions
    ):  # 지금 규칙 모드 — 모든 질문이 한도에 든다
        msg = QUESTION_LIMIT.format(n=s.max_questions)
        return msg, Entry(t=t, kind="refused", arg=arg, text=msg)
    reply = (
        answer_legacy(arg, s.items) if s.mode == "legacy" else answer(arg, s.items, states(entries))
    )
    return reply.text, Entry(t=t, kind="question", arg=arg, reply=reply, text=reply.text)


def _material(s: Session, entries: list[Entry], arg: str, t: str) -> tuple[str, Entry]:
    served = list(dict.fromkeys(e.served for e in entries if e.kind == "material" and e.served))
    left = None if s.max_requests is None else s.max_requests - len(served)
    if not arg:
        head = "요청할 수 있는 자료" + (f" (남은 요청 {left}개)" if left is not None else "") + ":"
        text = "\n".join([head, *(f"  {i}. {m.title}" for i, m in enumerate(s.materials, 1))])
        return text, Entry(t=t, kind="material_list", text=head)
    if not arg.isdigit() or not 1 <= int(arg) <= len(s.materials):
        return MATERIAL_BAD, Entry(t=t, kind="refused", arg=arg, text=MATERIAL_BAD)
    m = s.materials[int(arg) - 1]
    if m.id not in served and left is not None and left <= 0:
        msg = MATERIAL_LIMIT.format(n=s.max_requests)
        return msg, Entry(t=t, kind="refused", arg=arg, text=msg)
    return f"[{m.title}]\n{m.content}", Entry(t=t, kind="material", arg=arg, served=m.id)


def read_log(label: str, logs: Path = LOGS) -> list[Entry]:
    p = logs / f"{label}.jsonl"
    if not p.exists():
        return []
    return [Entry.model_validate_json(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def load_session(token: str, sessions: Path = SESSIONS) -> Session | None:
    p = sessions / f"{token}.json"
    if not token.isalnum() or not p.exists():
        return None
    return Session.model_validate_json(p.read_text("utf-8"))


_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — http.server 이름
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            )
        except json.JSONDecodeError:
            return self._send(400, "요청을 읽지 못했습니다.")
        s = load_session(str(body.get("token", "")))
        kind = body.get("kind")
        if s is None or kind not in ("question", "material"):
            return self._send(403, "진행자에게 연결할 수 없습니다.")
        with _LOCK:  # 같은 풀이의 요청이 겹쳐도 상태·한도가 어긋나지 않게
            entries = read_log(s.label)
            text, entry = handle(s, entries, kind, str(body.get("arg", "")))
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / f"{s.label}.jsonl").open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
        self._send(200, text)

    def _send(self, code: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — 접속 기록은 남기지 않는다 (질문은 jsonl 에)
        return


ASK_CLIENT = """#!/usr/bin/env python3
# 진행자에게 질문·자료 요청 — ./ask question "질문" / ./ask material / ./ask material 번호
import json, sys, urllib.request
TOKEN, URL = "{token}", "http://127.0.0.1:{port}/ask"
if len(sys.argv) < 2 or sys.argv[1] not in ("question", "material"):
    print('사용법: ./ask question "질문"  /  ./ask material  /  ./ask material 번호'); sys.exit(2)
req = urllib.request.Request(URL, data=json.dumps({{"token": TOKEN, "kind": sys.argv[1], "arg": " ".join(sys.argv[2:])}}).encode(),
                             headers={{"Content-Type": "application/json"}})
try:
    print(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
except Exception:
    print("진행자에게 연결할 수 없습니다. 잠시 뒤 다시 해 주세요."); sys.exit(1)
"""


def client_script(token: str, port: int = PORT) -> str:
    return ASK_CLIENT.format(token=token, port=port)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    if a.cmd == "serve":
        LOGS.mkdir(parents=True, exist_ok=True)
        server = ThreadingHTTPServer(("127.0.0.1", a.port), _Handler)
        print(f"진행자: http://127.0.0.1:{a.port}/ask (세션 {SESSIONS}, 기록 {LOGS})", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
