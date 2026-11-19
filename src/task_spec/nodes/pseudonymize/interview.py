"""채용담당자 인터뷰 글 가명화 (#262, 결정 4 [확정: 2026-11-19 사용자]).

담당자가 쓴 글(대화·양식·편집한 사례)과 인터뷰 맥락에 넣는 공고 줄은 LLM 에 보내기 전에 여기서 가명화한다.
포크(Wing-C-Interview)는 정규식으로 연락처·비밀 값만 거절하고 회사명·'담당자:' 줄 이름만 바꿨다 — 문장 속 이름·고객사는
그대로 외부 LLM 으로 갔다. 이 모듈은 지원서와 같은 탐지기(Kiwi 포함)를 쓴다.

- 인터뷰마다 대응표(RunMapping) 하나: 같은 이름은 턴이 달라도 같은 토큰. 대응표는 `data/interviews/<id>/mapping.json`
  (디렉터리 700·파일 600) 에만 두고 과제 승인·인터뷰 폐기(방치 포함) 때 지운다. 원 값은 이 모듈과 그 파일 밖으로 나가지 않는다.
- DB 에는 가명화한 글만 저장한다. 다시 LLM 입력으로 쓸 때는 `stored*` 로 패턴 탐지기·위조 토큰을 다시 검사한다.
- LLM 이 가명화 입력으로 쓴 글(질문·사례 초안·기준 답안)은 `derived` — `derive_pseudonymized` 와 같은 규칙
  (패턴 탐지기에 걸리면 거부, 대응표에 없는 토큰 거부). 이름 탐지기(Kiwi)는 LLM 글에 쓰지 않는다 — 오탐으로 막힌다.
- 담당자가 화면에 보인 가명화 글을 고쳐 보내면 대응표에 있는 토큰은 살리고, 없는 토큰 모양만 무력화한다.
  한계: 진짜 토큰 바로 뒤에 붙여 쓴 라벨 값(예: '담당자: [PERSON_1] 실명')은 라벨 탐지기가 건너뛸 수 있다 — 담당자는
  신뢰하는 사용자이고 실수로 넣은 개인정보를 막는 것이 목표다.

이 패키지는 LLM·임베딩·외부 API 를 import 하지 않는다 (불변식 2).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from task_spec.nodes.pseudonymize.detectors import TOKEN_RE, detect_patterns
from task_spec.nodes.pseudonymize.engine import (
    Detectors,
    RunMapping,
    ensure_private_dir,
    pseudonymize_text,
)
from task_spec.schemas.pseudo_text import _MINT, PseudonymizedText

MAX_CHARS = 60_000
# 비밀 값은 가명화하지 않고 거절한다 — 붙여 넣은 설정·헤더·로그의 자격 증명 모양만 (privacy-auditor M2).
# 평범한 장애 설명('비밀번호는 해시로 저장')은 막지 않는다: 라벨 뒤에 ':'·'=' 가 붙은 경우만 본다. 오탐이면 담당자가 고쳐 다시 보낸다.
_SECRET = re.compile(
    "|".join(
        (
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"(?i:\b(?:api[_ -]?key|access[_ -]?key|secret(?:[_ -]?key)?|client[_ -]?secret|"
            r"(?:access[_ -]?)?token|password|passwd|pwd|passphrase)\b\s*[\"']?\s*[:=]\s*\S+)",
            r"\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)\s*=\s*\S+",  # 환경 변수 (대문자만)
            r"(?:비밀\s*번호|비번|암호|패스워드|시크릿|토큰|인증\s*키|(?i:api)\s*키|접속\s*키|액세스\s*키)\s*[:=]\s*\S+",
            r"(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{12,})",
            r"\bsk-[A-Za-z0-9_-]{16,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}",
            r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
            r"://[^/\s:@]+:[^/\s@]+@",  # URL 안 계정:비밀번호
        )
    )
)
_INTERVIEW_ID = re.compile(r"^int-[0-9a-f]{32}$")
# 시스템이 만든 id 는 패턴 탐지 전에 가린다 — 무작위 16진수가 전화번호 모양이 되기도 한다 (uuid4 의 약 0.7% 가 유선·휴대
# 전화 패턴에 걸림, 2026-11-19 측정). 사례 항목 id(case_id)가 걸리면 같은 id 로 매번 거절돼 그 인터뷰가 기준 답안 단계에서 멈췄다.
_SYSTEM_ID = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])(?:pub|co):[a-z_.]+:[a-z0-9-]+"  # 사례 id (schemas/incident.py)
)
_CORP_MARK = re.compile(r"\(\s*주\s*\)|㈜|주식회사")
# 엔진을 거치는 동안 살릴 토큰의 자리표시 (유니코드 사용 영역 — 입력에서 미리 지운다)
_KEEP_OPEN, _KEEP_CLOSE = "", ""
_KEPT = re.compile(f"{_KEEP_OPEN}(\\d+){_KEEP_CLOSE}")


class InterviewTextRejected(ValueError):
    """담당자 글을 LLM 에 보낼 수 없다 — 비밀 값·길이 초과·가명화 누락. 메시지는 화면에 그대로 보여도 되는 말만."""


class InterviewMappingGone(RuntimeError):
    """대응표가 지워졌다 (승인·폐기) — 그 인터뷰는 더 이상 LLM 을 부르지 않는다."""


class InterviewPseudonymizer:
    def __init__(self, detectors: Detectors, data_dir: Path) -> None:
        self._detectors = detectors
        self._root = Path(data_dir) / "interviews"

    # ------------------------------------------------------------------ 대응표
    def _dir(self, interview_id: str) -> Path:
        if not _INTERVIEW_ID.fullmatch(interview_id):
            raise ValueError("인터뷰 id 형식이 아닙니다")
        return self._root / interview_id

    def new_mapping(self, interview_id: str, company: str) -> RunMapping:
        """새 인터뷰의 대응표. 회사 이름을 먼저 ORG 토큰으로 넣어 공고·대화 어디서든 같은 토큰이 되게 한다."""
        if self._dir(interview_id).exists():
            raise ValueError("이미 대응표가 있는 인터뷰입니다")
        mapping = RunMapping()
        # 엔진은 대응표의 원 값 그대로만 전파한다 — '(주)가상테크' 만 넣으면 문장 속 '가상테크' 는 남는다. 표기 변형을 같은 토큰에.
        for variant in dict.fromkeys(
            v for v in (company.strip(), _CORP_MARK.sub("", company).strip()) if len(v) >= 2
        ):
            mapping.token_for("ORG", variant, None)
        return mapping

    def load(self, interview_id: str) -> RunMapping:
        path = self._dir(interview_id) / "mapping.json"
        if not path.is_file():
            raise InterviewMappingGone(interview_id)
        return RunMapping.load(path)

    def save(self, interview_id: str, mapping: RunMapping) -> None:
        d = self._dir(interview_id)
        ensure_private_dir(self._root)
        ensure_private_dir(d)
        mapping.save(d / "mapping.json")

    def delete(self, interview_id: str) -> bool:
        d = self._dir(interview_id)
        if not d.exists():
            return False
        shutil.rmtree(d)
        return True

    def mapped_ids(self) -> list[str]:
        """대응표가 남아 있는 인터뷰 id (보관 기간 정리용)."""
        if not self._root.is_dir():
            return []
        return sorted(p.name for p in self._root.iterdir() if _INTERVIEW_ID.fullmatch(p.name))

    # ------------------------------------------------------------------ 담당자 글
    def pseudonymize(self, raw: str, mapping: RunMapping) -> PseudonymizedText:
        """담당자가 쓴 글 → 가명화 글. 대응표를 갱신한다 (부르는 쪽이 save)."""
        if len(raw) > MAX_CHARS:
            raise InterviewTextRejected("입력 길이가 너무 깁니다")
        if _SECRET.search(raw):
            raise InterviewTextRejected(
                "비밀 값(키·비밀번호·토큰)으로 보이는 내용은 보낼 수 없습니다"
            )
        # 대응표에 있는 토큰은 화면에 보인 가명화 글을 고쳐 보낸 것 — 살린다. 엔진은 입력의 토큰 모양을 전부 무력화하므로
        # (neutralize_token_lookalikes) 엔진을 거치는 동안 사용 영역 문자 자리표시로 감쌌다가 되돌린다. 원 값으로 되돌려
        # 다시 가명화하지 않는다 — 고용 문맥에서만 잡히는 회사명은 다른 문장에서 다시 안 잡혀 원문이 남는다.
        # 대응표에 없는 토큰 모양은 엔진이 위조로 보고 무력화한다.
        kept: list[str] = []

        def protect(m: re.Match[str]) -> str:
            if m.group(0) not in mapping.tokens:
                return m.group(0)
            kept.append(m.group(0))
            return f"{_KEEP_OPEN}{len(kept) - 1}{_KEEP_CLOSE}"

        text = TOKEN_RE.sub(protect, raw.replace(_KEEP_OPEN, "").replace(_KEEP_CLOSE, ""))
        result = pseudonymize_text(text, mapping, self._detectors)
        if result.residual:
            raise InterviewTextRejected(
                "이름·연락처 같은 개인정보를 다 가리지 못했습니다. 해당 부분을 빼고 다시 보내 주세요"
            )
        restored = _KEPT.sub(lambda m: kept[int(m.group(1))], result.text)
        if _KEEP_OPEN in restored or _KEEP_CLOSE in restored:
            raise InterviewTextRejected("입력을 처리하지 못했습니다. 다시 보내 주세요")
        return PseudonymizedText(restored.strip(), _token=_MINT)

    # ------------------------------------------------------------------ 저장된 가명화 글·LLM 글
    def derived(self, text: str, mapping: RunMapping) -> str:
        """LLM 이 가명화 입력으로 쓴 글을 저장·표시 전에 검사한다. 통과하면 그대로 돌려준다.
        시스템 id(uuid·사례 id)는 가린 뒤 패턴을 본다 — id 는 개인정보가 아니고, 우연히 전화번호 모양이 될 뿐이다."""
        if detect_patterns(_SYSTEM_ID.sub("ID", text)):
            raise InterviewTextRejected("생성한 글에 연락처·링크 같은 개인정보 형식이 있습니다")
        if any(m.group(0) not in mapping.tokens for m in TOKEN_RE.finditer(text)):
            raise InterviewTextRejected("생성한 글에 인터뷰에 없는 가명화 토큰이 있습니다")
        return text

    def stored(self, text: str, mapping: RunMapping) -> PseudonymizedText:
        """DB 에 저장된 가명화 글을 LLM 입력으로 다시 쓸 때."""
        return PseudonymizedText(self.derived(text, mapping), _token=_MINT)

    def stored_json(
        self, value: Any, mapping: RunMapping, *, compact: bool = False
    ) -> PseudonymizedText:
        """저장된 가명화 글들로 만든 구조(dict·list)를 JSON 입력으로. 글 잎과 dict 키마다 `derived` 검사.
        compact=True 는 공백 없는 구분자 (포크가 model_dump_json() 으로 넘기던 입력과 같은 모양)."""

        def walk(v: Any) -> None:
            if isinstance(v, str):
                self.derived(v, mapping)
            elif isinstance(v, dict):
                for key, item in v.items():
                    self.derived(str(key), mapping)  # 키도 글이다 (privacy-auditor H1)
                    walk(item)
            elif isinstance(v, list | tuple):
                for item in v:
                    walk(item)
            elif v is not None and not isinstance(v, bool | int | float):
                raise TypeError(f"JSON 으로 묶을 수 없는 값: {type(v).__name__}")

        walk(value)
        separators = (",", ":") if compact else None
        return PseudonymizedText(
            json.dumps(value, ensure_ascii=False, separators=separators), _token=_MINT
        )
