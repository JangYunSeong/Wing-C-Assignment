"""탐지기. 각 탐지기는 원문 위치(Span) 목록을 돌려준다. 값은 Span 안에만 있고 로그로 나가지 않는다.

우선순위(priority): 겹치면 큰 쪽이 이긴다. 같으면 긴 쪽.
  3 패턴(이메일·전화·URL·주민번호)
  2 사전·접미어·인구통계·호칭 이름·복성 이름·영문 이름·회사 문맥(업종 접미어·경력 머리말·재직)·교육 과정
  1 라벨·표 칸 값·형태소(Kiwi) 이름

탐지한 값은 엔진이 같은 run 전체로 전파한다(`engine._replace_known`). 일반어를 잡으면 문서 곳곳이 지워지므로
문맥 규칙은 후보마다 일반어 검사를 거치고, 조사가 붙은 경계("가상랩에서")는 만들지 않는다 — 붙으면 다른 표기가 새는데
누락 검사도 못 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from task_spec.nodes.pseudonymize.dictionaries import (
    GIVEN_SYLLABLES,
    LEFT,
    RIGHT,
    SCHOOL_ONLY_SUFFIX,
    Dictionaries,
)
from task_spec.nodes.pseudonymize.kiwi_tools import Token, get_kiwi, line_tokens, oov_ids
from task_spec.nodes.pseudonymize.romanize import (
    KO_PARTICLES,
    TITLES_AFTER,
    full_name_regex,
    split_name,
)

REMOVE = "REMOVE"
Spread = Literal["all", "context", "none"]
CATEGORIES = ("PERSON", "CONTACT", "LINK", "SCHOOL", "ORG", "PRODUCT", "COURSE")
TOKEN_RE = re.compile(r"\[(?:PERSON|CONTACT|LINK|SCHOOL|ORG|PRODUCT|COURSE)_\d+\]")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    category: str  # CATEGORIES 또는 REMOVE
    priority: int
    detector: str
    canonical: str | None = None  # 사전 별칭이면 대표표기 (같은 토큰으로 묶기 위해)
    # 전파 범위 (엔진 `RunMapping`): "all" 같은 run 어디서든 / "context" 고용 문맥이 붙은 언급에만 ("X에서 3년간",
    # "X에 입사" — 공개 어휘로 된 회사 이름) / "none" 그 자리만 (근거가 약한 탐지)
    spread: Spread = "all"

    def value(self, text: str) -> str:
        return text[self.start : self.end]


# ---------------------------------------------------------------- 패턴 (priority 3)

_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # 앞부분은 64자까지 (RFC 5321) — 길이 제한이 없으면 점·문자만 긴 줄에서 제곱 시간이 걸린다 (code review M7)
    ("email", "CONTACT", re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "url",
        "LINK",
        re.compile(
            r"(?:https?://|www\.)[^\s<>()\[\]가-힣]+"
            r"|(?<![\w.])(?:github\.com|gitlab\.com|linkedin\.com|velog\.io|medium\.com|brunch\.co\.kr"
            r"|blog\.naver\.com|[a-z0-9-]{1,63}\.github\.io|[a-z0-9-]{1,63}\.tistory\.com|[a-z0-9-]{1,63}\.notion\.site"
            # 스킴 없는 배포 주소 — 개인 프로젝트 주소가 이름·학교를 드러낸다 (2026-11-04 분석). 다단계 서브도메인 허용.
            # 이름 조각은 63자·8단계까지 (DNS 한도) — 제한이 없으면 '-' 만 긴 줄에서 제곱 시간 (code review M7)
            r"|(?:[a-z0-9-]{1,63}\.){1,8}(?:vercel\.app|netlify\.app|herokuapp\.com|pages\.dev|web\.app|firebaseapp\.com"
            r"|streamlit\.app|onrender\.com|fly\.dev|gitlab\.io|glitch\.me|repl\.co))"
            # 도메인 뒤 우측 경계 — "self.web.application", "config.web.app_name", "x.fly.developer" 는 주소가 아님
            r"(?![A-Za-z0-9_-])(?:/[^\s<>()\[\]가-힣]*)?",
            re.IGNORECASE,
        ),
    ),
    (
        "mobile",
        "CONTACT",
        re.compile(r"(?<!\d)(?:\+82[-.\s]?)?0?1[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"),
    ),
    (
        "landline",
        "CONTACT",
        re.compile(r"(?<!\d)(?:\+82[-.\s]?)?0?(?:2|[3-6][1-5])[-.)\s]\s?\d{3,4}[-.\s]\d{4}(?!\d)"),
    ),
    ("rrn", "CONTACT", re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)")),
]


# 주소: 행정구역 단위(시·도·군·구)가 2개 이상 이어진 뒤 도로명/동 + 번지. 한 단위만이면 일반 문장 오탐이 많다
# ("연구 파이프라인으로 3배"). 단위처럼 끝나는 일반어는 제외.
_ADDRESS = re.compile(
    r"(?P<units>(?:[가-힣]{1,10}(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구)[ \t]+){2,3})"
    r"[가-힣0-9]{1,15}(?<!으)(?:로|길|동|읍|면|리)[ \t]?\d+(?:-\d+)?(?:번길[ \t]?\d+)?"
    r"(?:[ \t]?\d+동)?(?:[ \t]?\d+호)?"
)
_NOT_ADMIN_UNITS = frozenset(
    {
        "연구",
        "도구",
        "요구",
        "추구",
        "탐구",
        "기구",
        "지구",
        "가구",
        "출구",
        "입구",
        "용구",
        "시도",
        "의도",
        "정도",
        "속도",
        "제도",
    }
)


def detect_address(text: str) -> list[Span]:
    spans = []
    for m in _ADDRESS.finditer(text):
        if any(u in _NOT_ADMIN_UNITS for u in m.group("units").split()):
            continue
        spans.append(Span(m.start(), m.end(), "CONTACT", 3, "address"))
    return spans


# 라벨 붙은 계정 핸들: "GitHub: gru-eats", "깃허브: @gruswap". 맨 '@핸들' 은 코드 애노테이션(@Transactional)과
# 구분할 수 없어 라벨 뒤일 때만. 핸들 바로 뒤에 한글이 붙으면("Actions로") 계정이 아니라 문장으로 본다.
_HANDLE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:github|gitlab|깃허브|깃헙|깃랩)[ \t]*[:：][ \t]*@?"
    # 핸들 뒤 '.' 은 문장 끝 마침표일 때(뒤가 공백·줄끝)만 허용 — "GitHub: gru-eats."
    r"(?P<h>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9])?)(?![A-Za-z0-9_:/-]|[가-힣]|\.(?=[A-Za-z0-9_-]))"
)


# 라벨 뒤에 와도 계정이 아닌 것: 버전("v1.2.3", "3.11"), 파일명("main.py"), GitHub 기능·흔한 단어 (짧은 목록, 대소문자 그대로)
_NOT_HANDLE_VERSION = re.compile(r"v?\d+(?:\.\d+)+", re.IGNORECASE)
_NOT_HANDLE_FILE = re.compile(
    r".+\.(?:py|js|ts|tsx|jsx|java|kt|go|rs|rb|php|c|h|cpp|cs|md|txt|json|ya?ml|toml|cfg|ini|sh|html|css|sql|ipynb)",
    re.IGNORECASE,
)
_NOT_HANDLE_WORDS = frozenset(
    {
        "README",
        "Issues",
        "Issue",
        "Actions",
        "Pages",
        "Wiki",
        "Projects",
        "Releases",
        "Packages",
        "Discussions",
        "Copilot",
        "Python",
        "Java",
        "Docker",
        "Kubernetes",
        "main",
        "master",
        "develop",
    }
)


def detect_handles(text: str) -> list[Span]:
    spans = []
    for m in _HANDLE.finditer(text):
        h = m.group("h")
        if (
            h in _NOT_HANDLE_WORDS
            or _NOT_HANDLE_VERSION.fullmatch(h)
            or _NOT_HANDLE_FILE.fullmatch(h)
        ):
            continue
        spans.append(Span(m.start("h"), m.end("h"), "LINK", 3, "handle"))
    return spans


def detect_patterns(text: str) -> list[Span]:
    return (
        [
            Span(m.start(), m.end(), cat, 3, name)
            for name, cat, rx in _PATTERNS
            for m in rx.finditer(text)
        ]
        + detect_address(text)
        + detect_handles(text)
    )


# ---------------------------------------------------------------- 인구통계 삭제 (priority 2)

_REMOVALS: list[tuple[str, re.Pattern[str]]] = [
    (
        "age",
        re.compile(r"만\s?\d{1,2}\s?세|(?<![\d.])\d{2}\s?세(?![가-힣])|(?:19|20)?\d{2}\s?년생"),
    ),
    (
        "gender_paren",
        re.compile(r"\(\s*(?:남|여)(?:성|자)?\s*(?:[,/]\s*(?:만\s?)?\d{1,2}\s?세?)?\s*\)"),
    ),
    (
        "military",
        re.compile(
            r"(?:육군|해군|공군|해병대)\s?(?:병장|상병|일병|이병|하사|중사|소위|중위|대위)?\s?(?:만기\s?)?(?:전역|제대)"
            r"|군필|미필|병역\s?필|(?:병역|군)\s?면제"
        ),
    ),
    # 인접한 공백 반복을 두지 않는다 — 공백만 긴 줄에서 제곱 시간 (code review M7)
    ("photo", re.compile(r"(?m)^[ \t]*(?:\[[ \t]*)?사진[ \t]*(?:\][ \t]*)?$")),
    (
        "birthdate",
        re.compile(
            r"(?:19|20)\d{2}[ \t]?년[ \t]?\d{1,2}[ \t]?월[ \t]?\d{1,2}[ \t]?일[ \t]?생"
            r"|(?:19|20)\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}[ \t]?(?:생|출생)"
        ),
    ),
    ("gender_slash", re.compile(r"(?<=/)[ \t]*(?:남|여)(?:성|자)?[ \t]*(?=/|$|\n)", re.MULTILINE)),
    (
        "gpa",
        re.compile(
            # 여는 괄호 뒤 공백만 받는다 — 앞 공백 반복은 공백만 긴 줄에서 제곱 시간 (code review M7)
            r"(?:\([ \t]*)?(?:전공[ \t]?)?(?:학점|평점|GPA)[ \t]*[:：]?[ \t]*\d(?:\.\d{1,2})?[ \t]*/[ \t]*\d(?:\.\d{1,2})?[ \t]*\)?",
            re.IGNORECASE,
        ),
    ),
]


def detect_demographics(text: str) -> list[Span]:
    return [
        Span(m.start(), m.end(), REMOVE, 2, name)
        for name, rx in _REMOVALS
        for m in rx.finditer(text)
    ]


# ---------------------------------------------------------------- 라벨 (priority 1, 삭제 라벨은 2)

# 라벨 표기 → 범주. 키는 표시 표기 그대로 두고, 대조는 소문자·공백 제거(`_label_key`)로 한다.
_LABEL_FORMS: dict[str, str] = {
    "성명": "PERSON",
    "이름": "PERSON",
    "name": "PERSON",
    # 영문 이름 라벨 (2026-11-04 2차)
    "영문 이름": "PERSON",
    "영문 성명": "PERSON",
    "영문명": "PERSON",
    "english name": "PERSON",
    "주소": "CONTACT",
    "거주지": "CONTACT",
    "address": "CONTACT",
    "연락처": "CONTACT",
    "휴대폰": "CONTACT",
    "휴대전화": "CONTACT",
    "전화번호": "CONTACT",
    "phone": "CONTACT",
    "mobile": "CONTACT",
    "이메일": "CONTACT",
    "email": "CONTACT",
    "e-mail": "CONTACT",
    "생년월일": REMOVE,
    "생일": REMOVE,
    "나이": REMOVE,
    "연령": REMOVE,
    "성별": REMOVE,
    "병역": REMOVE,
    "학점": REMOVE,
    "gpa": REMOVE,
    "date of birth": REMOVE,
    "birth": REMOVE,
    "gender": REMOVE,
    "학교": "SCHOOL",
    "출신 학교": "SCHOOL",
    "최종 학력": "SCHOOL",
    "학력": "SCHOOL",
    "학교명": "SCHOOL",
    "school": "SCHOOL",
    "university": "SCHOOL",
    # 회사 라벨 (2차). '소속' 은 "소속: 백엔드 파트" 처럼 부서만 오는 일이 많아 넣지 않는다.
    "회사": "ORG",
    "회사명": "ORG",
    "직장": "ORG",
    "근무처": "ORG",
    "재직 회사": "ORG",
    "company": "ORG",
    "서비스명": "PRODUCT",
    "제품명": "PRODUCT",
}


def _label_key(label: str) -> str:
    return re.sub(r"\s+", "", label).casefold()


_LABELS: dict[str, str] = {_label_key(k): v for k, v in _LABEL_FORMS.items()}
_LABEL_ALT = "|".join(
    sorted(
        (r"[ \t]?".join(re.escape(w) for w in k.split(" ")) for k in _LABEL_FORMS),
        key=len,
        reverse=True,
    )
)
# 값은 탭에서 끊는다 ("이름: 가상\t생년월일: …" — 한 줄에 라벨 여러 개, 2차)
_LABEL_RX = re.compile(
    r"(?im)^[ \t]*(?:[-•*·][ \t]*)?(?P<label>"
    + _LABEL_ALT
    # 값은 탭·줄끝까지 탐욕으로 받고 뒤 공백은 코드에서 자른다 — 게으른 반복 + 공백 반복은 긴 공백 줄에서
    # 제곱 시간이 걸렸다 ("이름: a" + 공백 2만 자 + "b" 에 35초, code review M7)
    + r")[ \t]*[:：|][ \t]*(?P<value>[^\n\t]+)(?=\t|$)"
)


def _label_value_span(text: str, start: int, end: int, cat: str, detector: str) -> Span | None:
    if cat == "PERSON":  # "김가상 (남, 27세)" → 이름만
        cut = re.search(r"[(,/]", text[start:end])
        if cut:
            end = start + cut.start()
    while end > start and text[end - 1] in " \t":
        end -= 1
    if end <= start:
        return None
    return Span(start, end, cat, 2 if cat == REMOVE else 1, detector)


def detect_labels(text: str) -> list[Span]:
    spans = []
    for m in _LABEL_RX.finditer(text):
        cat = _LABELS[_label_key(m.group("label"))]
        sp = _label_value_span(text, m.start("value"), m.end("value"), cat, f"label:{cat}")
        if sp:
            spans.append(sp)
    return spans


# 표 칸 (2026-11-04 2차): 탭·'|'·두 칸 이상 공백으로 나뉜 줄. DOCX·HWPX 표를 뽑으면 "이름⇥값" 이나
# 머리 행("성명⇥생년월일⇥연락처") 아래 값 행이 된다. 라벨 칸 바로 다음 칸, 또는 머리 행의 같은 열을 값으로 본다.
_CELL_SEP = re.compile(r"\t+|[ \t]*\|[ \t]*|[ \t]{2,}")
_TABLE_RULE = re.compile(r"^[\s|:\-+=]+$")
# 기간·날짜만 있는 칸은 값으로 치환하지 않는다 (T2 기간 대조). 생년월일 같은 삭제 라벨은 예외.
_ONLY_PERIOD = re.compile(
    r"^[\s(]*(?:19|20)\d{2}(?:[.\-/년][ \t]?\d{1,2}월?)?(?:[.\-/][ \t]?\d{1,2}일?)?"
    r"(?:[ \t]*[~\-–—][ \t]*(?:(?:19|20)\d{2}(?:[.\-/년][ \t]?\d{1,2}월?)?|현재|재직[ \t]?중|present))?[\s)]*$",
    re.IGNORECASE,
)
# 표 머리 행에 흔한 열 이름 — 라벨 칸 옆에 있어도 값이 아니다 ("| 기간 | 학교 | 전공 | 학위 |" 의 '전공').
# 값으로 잡으면 같은 run 전체에서 그 단어가 지워진다 (엔진 전파).
_TABLE_HEADER_WORDS = frozenset(
    (
        "기간 전공 학과 구분 내용 비고 직위 직급 직책 부서 담당 담당업무 역할 성과 학위 졸업 상태 소재지 지역 기관 기관명 "
        "발급기관 취득일 자격 자격증 근무기간 재학기간 전공명 점수 등급 일자 날짜 년도 연도 활동 활동명 수상 수상명 "
        "프로젝트 프로젝트명 기술 사용기술 항목 언어 수준 시험 시험명 주요업무 업무 고용형태 비율 번호 순번 "
        "period major role title department degree status note"
    ).split()
)
_CELL_LABEL_VALUE = re.compile(
    r"(?i)^(?P<label>" + _LABEL_ALT + r")[ \t]*[:：][ \t]*(?P<value>.+)$"
)


def _cells(line: str, offset: int) -> list[tuple[int, int]]:
    """줄을 칸으로 나눠 (시작, 끝) 원문 위치 목록. 빈 칸(줄 앞뒤 '|')은 뺀다."""
    out, last = [], 0
    for m in _CELL_SEP.finditer(line):
        out.append((last, m.start()))
        last = m.end()
    out.append((last, len(line)))
    cells = []
    for a, b in out:
        while a < b and line[a] in " \t":
            a += 1
        while b > a and line[b - 1] in " \t":
            b -= 1
        if b > a:
            cells.append((offset + a, offset + b))
    return cells


def _cell_label(text: str, cell: tuple[int, int]) -> str | None:
    return _LABELS.get(_label_key(text[cell[0] : cell[1]].rstrip(":：")))


# 전화·이메일 라벨 (값에 숫자나 '@' 가 있어야 한다). 주소 라벨은 숫자 없는 값("서울시 가상구 가상동")도 받는다.
_PHONE_MAIL_LABELS = frozenset(
    _label_key(k)
    for k in (
        "연락처",
        "휴대폰",
        "휴대전화",
        "전화번호",
        "phone",
        "mobile",
        "이메일",
        "email",
        "e-mail",
    )
)


def _cell_value(
    text: str,
    cell: tuple[int, int],
    cat: str,
    detector: str,
    label: str,
    dicts: Dictionaries,
) -> Span | None:
    """표 칸 값. 라벨 줄(`_LABEL_RX`)보다 문맥이 약하므로 값 모양을 본다 — 값으로 잡으면 run 전체로 전파된다.

    전화·이메일 라벨 값은 숫자나 '@' 가 있어야 하고 ("이메일 | 발송 서버 개발" 은 아님), 이름은 짧고 숫자가 없어야 한다.
    열 이름과 같은 값("성과", "기간")은 이름 라벨 옆 한글 2~5자일 때만 치환한다 — 이름이면 새지 않게. 공개 어휘면
    그 칸만(문서 전체에서 "성과"가 지워지지 않게), 아니면 전파한다 (privacy-auditor 2차 확인 항목·후속).
    """
    value = text[cell[0] : cell[1]]
    if cat != REMOVE and _ONLY_PERIOD.match(value):
        return None
    if label in _PHONE_MAIL_LABELS and not re.search(r"[\d@]", value):
        return None
    if cat == "PERSON" and (len(value) > 30 or re.search(r"\d", value)):
        return None
    if cat in ("ORG", "SCHOOL", "PRODUCT") and len(value) > 40:
        return None
    if _label_key(value) in _TABLE_HEADER_WORDS:
        # 이름 라벨 바로 옆이면 일반어와 같아도 이름이다 ("이름⇥성과" — 이름 탐지기는 일반어 목록 때문에 못 잡는다)
        if cat != "PERSON" or not re.fullmatch(r"[가-힣]{2,5}", value):
            return None
        sp = _label_value_span(text, cell[0], cell[1], cat, detector)
        if sp is None:
            return None
        public = _public_value(text, sp.start, sp.end, dicts, kiwi=False)
        return Span(
            sp.start,
            sp.end,
            sp.category,
            sp.priority,
            sp.detector,
            spread="none" if public else "all",
        )
    return _label_value_span(text, cell[0], cell[1], cat, detector)


def _is_header_row(labels: list[str | None], keys: list[str], next_is_rule: bool) -> bool:
    """표 머리 행인가: 라벨 칸이 둘 이상이고 절반 이상, 또는 라벨 칸 하나 + 나머지가 열 이름(세 칸 이상)·다음 줄이 구분선.
    두 칸짜리 "이름⇥성과" 는 라벨-값 줄로 본다 (값이 열 이름과 같은 이름이어도 새지 않게)."""
    n_labels = sum(lb is not None for lb in labels)
    others_are_headers = all(
        lb is not None or k in _TABLE_HEADER_WORDS for lb, k in zip(labels, keys, strict=True)
    )
    return (n_labels >= 2 and n_labels * 2 >= len(labels)) or (
        n_labels >= 1 and (next_is_rule or (len(labels) >= 3 and others_are_headers))
    )


def detect_label_cells(text: str, dicts: Dictionaries) -> list[Span]:
    spans: list[Span] = []
    lines = text.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    header: list[str | None] | None = None
    header_keys: list[str] = []
    for i, line in enumerate(lines):
        if header is not None and _TABLE_RULE.match(line) and line.strip():
            continue  # 마크다운 표 구분 줄
        cells = _cells(line, offsets[i])
        if len(cells) < 2:
            header = None
            continue
        labels = [_cell_label(text, c) for c in cells]
        keys = [_label_key(text[c[0] : c[1]].rstrip(":：")) for c in cells]
        next_is_rule = (
            i + 1 < len(lines) and bool(_TABLE_RULE.match(lines[i + 1])) and "-" in lines[i + 1]
        )
        if _is_header_row(labels, keys, next_is_rule):
            header, header_keys = labels, keys  # 머리 행
            continue
        # 머리 행 아래 같은 칸 수의 값 행. 라벨 칸이 있으면 값 행이 아니라 라벨-값 줄이다.
        if header is not None and len(header) == len(cells) and all(lb is None for lb in labels):
            for cat, cell, key in zip(header, cells, header_keys, strict=True):
                if cat is not None:
                    sp = _cell_value(text, cell, cat, f"cell_col:{cat}", key, dicts)
                    if sp:
                        spans.append(sp)
            continue
        header = None
        for j, cell in enumerate(cells):
            m = _CELL_LABEL_VALUE.match(text[cell[0] : cell[1]])
            if m and j > 0:  # 첫 칸의 "라벨: 값" 은 _LABEL_RX 몫
                key = _label_key(m.group("label"))
                cat = _LABELS[key]
                sp = _cell_value(
                    text, (cell[0] + m.start("value"), cell[1]), cat, f"cell:{cat}", key, dicts
                )
                if sp:
                    spans.append(sp)
            cat = labels[j]
            if cat is not None and j + 1 < len(cells) and labels[j + 1] is None:
                sp = _cell_value(text, cells[j + 1], cat, f"cell:{cat}", keys[j], dicts)
                if sp:
                    spans.append(sp)
    return spans


# ---------------------------------------------------------------- 사전·접미어 (priority 2)

_CONTEXT_RX = re.compile(r"(?:19|20)\d{2}\s?[.\-/년]|재직|근무|인턴|입사|퇴사")
_SCHOOL_SUFFIX = re.compile(
    rf"{LEFT}[가-힣A-Za-z]{{2,15}}(?:대학교|대학원|고등학교|대학|과학기술원){RIGHT}"
)
# 이름 뒤: (조사) + 단어 경계. "누리가상에서" 의 '가' 를 조사로 보고 "누리" 에서 끊지 않게.
_NAME_END = r"(?=(?:에서의|에서는|에서|으로|에게|은|는|이|가|을|를|에|의|와|과|로|도)?(?:[^가-힣A-Za-z0-9]|$))"
_ORG_PREFIXED = re.compile(r"(?:주식회사|㈜|\(주\))[ \t]?[가-힣A-Za-z0-9&]{2,20}?" + _NAME_END)
# ㈜·(주) 는 이름에 붙어 있을 때만, '주식회사' 는 한 칸 띄어도 허용 ("이후 ㈜…" 를 회사명으로 보지 않게)
_ORG_SUFFIXED = re.compile(LEFT + r"[가-힣A-Za-z0-9&]{2,20}(?:㈜|\(주\)|[ \t]?주식회사)")


_PRODUCT_WORDS = r"(?:서비스|앱|어플|애플리케이션|제품|플랫폼|프로덕트|프로젝트|솔루션)"
_QUOTED_PRODUCT = re.compile(
    _PRODUCT_WORDS + r"[ \t]?['‘\"“「](?P<name>[^'’\"”」\n]{2,20})['’\"”」]"
    r"|['‘\"“「](?P<name2>[^'’\"”」\n]{2,20})['’\"”」][ \t]?(?:[가-힣]{1,6}[ \t])?" + _PRODUCT_WORDS
)
# 2차: 사내 도구·봇·메신저 이름 ("디자인 시스템 'X'", "챗봇 「X」", "사내 메신저 'X'"). 이 말들은 일반어 끝에도 붙고
# ("로봇", "시스템화") 따옴표 안이 일반 구절일 때가 많아("시스템 "장애 대응"") 단어 경계를 두고, 따옴표 안이 공개 어휘로만
# 되어 있으면 제품으로 보지 않는다 (code review L1).
_TOOL_WORDS = r"(?:시스템|챗봇|봇|메신저|라이브러리|브랜드|게임|커뮤니티|사이트|도구|툴)"
_QUOTED_TOOL = re.compile(
    rf"(?<![가-힣A-Za-z]){_TOOL_WORDS}[ \t]?['‘\"“「](?P<name>[^'’\"”」\n]{{2,20}})['’\"”」]"
    rf"|['‘\"“「](?P<name2>[^'’\"”」\n]{{2,20}})['’\"”」][ \t]?(?:[가-힣]{{1,6}}[ \t])?{_TOOL_WORDS}(?![가-힣])"
    # 사내·자체 + 꾸밈말 0~3개 + 따옴표 이름 ("사내 디자인 시스템 \"X\"", "자체 개발한 챗봇 「X」")
    r"|(?<![가-힣])(?:사내|자체|자사|인하우스)[ \t]?(?:[가-힣A-Za-z]{1,10}[ \t]?){0,3}?"
    r"['‘\"“「](?P<name3>[^'’\"”」\n]{2,20})['’\"”」]"
)


def _line_of(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return text[line_start : line_end if line_end != -1 else len(text)]


def detect_dictionary(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    for m in _QUOTED_PRODUCT.finditer(text):
        g = "name" if m.group("name") is not None else "name2"
        spans.append(Span(m.start(g), m.end(g), "PRODUCT", 2, "quoted"))
    for m in _QUOTED_TOOL.finditer(text):
        g = next(k for k in ("name", "name2", "name3") if m.group(k) is not None)
        if not _public_value(text, m.start(g), m.end(g), dicts, kiwi=False):
            spans.append(Span(m.start(g), m.end(g), "PRODUCT", 2, "quoted"))
    for name, rx in (("dict", dicts.pattern()), ("dict_attached", dicts.attached_pattern())):
        for m in rx.finditer(text):
            entry = dicts.entry_for(m.group(0))
            if entry is None:
                continue
            if (
                name == "dict_attached"
                and text.startswith(SCHOOL_ONLY_SUFFIX, m.end())
                and entry.category != "SCHOOL"
            ):
                continue  # '…생' 은 학교 뒤에서만 (회사+생 은 다른 단어일 수 있다)
            if entry.needs_context and not _CONTEXT_RX.search(_line_of(text, m.start(), m.end())):
                continue
            spans.append(Span(m.start(), m.end(), entry.category, 2, name, entry.canonical))
    for m in _SCHOOL_SUFFIX.finditer(text):
        spans.append(Span(m.start(), m.end(), "SCHOOL", 2, "suffix"))
    for rx in (_ORG_PREFIXED, _ORG_SUFFIXED):
        for m in rx.finditer(text):
            spans.append(Span(m.start(), m.end(), "ORG", 2, "suffix"))
    return spans


# ---------------------------------------------------------------- 회사·교육 과정 문맥 (2026-11-04 2차)
#
# 사전·'(주)' 표기가 없는 회사를 세 가지 문맥으로 잡는다. 회사 값은 같은 run 전체로 전파되므로(엔진 `_replace_known`)
# 일반어를 회사로 잡으면 문서 곳곳이 지워진다 — 후보마다 일반어 검사(`_org_candidate`)를 거친다.
#   1) 업종 접미어로 끝나는 한 단어 ("누리저축은행", "누리게임즈") — 어간이 일반어("인터넷은행")면 아님
#   2) 경력 머리말: 기간이 있는 줄의 첫 칸 + 구분자 뒤 팀·직무 ("가상랩 | 플랫폼팀 | 2022.04 ~ 현재"),
#      또는 첫 단어 뒤 팀 이름 ("가상정보 인프라운영팀 (2023.03 ~ 현재)")
#   3) 재직 문맥: "X에서 3년간", "X 인턴", "X에 입사"
# 일반 합성어가 너무 많은 접미어(시스템·데이터·플랫폼·통신·미디어·에너지·푸드·금융·투자·스튜디오)는 넣지 않는다.

# 강한 접미어: 회사 이름 끝에만 쓰이는 표기 ("게임즈", "랩스", "저축은행"). 어간 일반어 목록만 본다.
_ORG_STRONG_SUFFIXES = (
    "저축은행 손해보험 생명보험 자산운용 투자증권 금융투자 캐피탈 인베스트먼트 파트너스 홀딩스 금융지주 중공업 물산 텔레콤 "
    "정보통신 정보기술 네트웍스 네트워크스 엔터테인먼트 게임즈 게임스 랩스 테크놀로지 테크놀로지스 솔루션즈 시스템즈 "
    "모빌리티 로보틱스 코퍼레이션 컴퍼니 인터랙티브 웍스"
).split()
# 약한 접미어: 일반 합성어에도 붙는다 ("추심은행", "서명카드", "자동화솔루션"). 본문에서는 Kiwi 가 그 단어를
# 사전에 있는 일반 명사들로만 나누면 회사가 아니다 (`_generic_in_context`). 경력 머리말·재직 문맥에서는 이 검사를 하지 않는다.
_ORG_WEAK_SUFFIXES = (
    "은행 카드 보험 화재보험 해상보험 증권 생명 페이 뱅크 전자 화학 제약 바이오 건설 상사 테크 랩 솔루션 소프트 "
    "소프트웨어 커머스 마켓 헬스 헬스케어 애드"
).split()
_ORG_INDUSTRY = re.compile(
    LEFT
    + r"(?P<org>(?P<stem>[가-힣A-Za-z0-9&]{2,15}?)(?P<suf>"
    + "|".join(sorted(_ORG_STRONG_SUFFIXES + _ORG_WEAK_SUFFIXES, key=len, reverse=True))
    + r"))"
    + RIGHT
)

# 기간: "2022.04", "2022년 4월", "2019 ~ 2021", "2022.03 ~ present"
_PERIOD = re.compile(
    r"(?:19|20)\d{2}(?:[ \t]?[.\-/][ \t]?\d{1,2}|[ \t]?년|[ \t]*[~–—\-][ \t]*(?:(?:19|20)\d{2}|현재|present))",
    re.IGNORECASE,
)
_LEADING_PERIOD = re.compile(
    r"^(?:19|20)\d{2}(?:[ \t]?[.\-/][ \t]?\d{1,2}|[ \t]?년(?:[ \t]?\d{1,2}[ \t]?월)?)?"
    r"(?:[ \t]*[~–—\-][ \t]*(?:(?:19|20)\d{2}(?:[ \t]?[.\-/][ \t]?\d{1,2}|[ \t]?년(?:[ \t]?\d{1,2}[ \t]?월)?)?"
    r"|현재|재직[ \t]?중|present))?[ \t]*(?:[|/·,—–\-][ \t]*)?",
    re.IGNORECASE,
)
_LEADING_BULLET = re.compile(r"^[ \t]*(?:[-•*·▶▷ㆍ■□◆◇○●▪]|\d{1,2}[.)])?[ \t]*")
# '실' 은 부서 이름으로만 ("현실", "사실" 에 걸리지 않게, code review L2)
_TEAM_WORD = (
    r"(?:팀|파트|본부|사업부|센터|연구소|그룹|랩|셀|스쿼드|부문|Team|Lab|Squad"
    r"|(?:기획|전략|연구|개발|운영|비서|감사|경영|디자인|품질|보안|인사|재무|홍보|법무|기술|정보|전산|관제|상황|사업|지원|혁신)실)"
)
_ROLE_MARK = re.compile(
    rf"[가-힣A-Za-z&]{_TEAM_WORD}(?![가-힣A-Za-z])|(?<![가-힣A-Za-z]){_TEAM_WORD}(?![가-힣A-Za-z])"
    r"|개발자|엔지니어|연구원|매니저|마케터|디자이너|기획자|분석가|컨설턴트|인턴|사원|주임|대리|과장|차장|부장|팀장"
    r"|파트장|리드|책임|선임|수석|정규직|계약직|프리랜서"
    r"|(?i:engineer|developer|manager|designer|scientist|analyst|researcher|intern|marketer|consultant"
    r"|director|architect|specialist)"
)
_SEG_KO = r"[가-힣A-Za-z0-9&][가-힣A-Za-z0-9&.]{1,19}"
_SEG_EN = r"[A-Z][A-Za-z0-9&.]*(?: [A-Z][A-Za-z0-9&.]*){1,2}"
_HEADER_SEP = r"[ \t]*(?:[|·/]|—|–|-(?=[ \t])|\t)[ \t]*|[ \t]{2,}"
_HEADER_S1 = re.compile(rf"^(?P<org>{_SEG_EN}|{_SEG_KO})(?:{_HEADER_SEP})(?P<rest>.+)$")
_HEADER_S2 = re.compile(
    rf"^(?P<org>{_SEG_KO})[ \t]+(?:\S+[ \t]+){{0,2}}?[가-힣A-Za-z&]*{_TEAM_WORD}"
    r"(?=$|[^가-힣A-Za-z]|에서|에|의|과|와|으로|로|이|가|은|는)"
)
_EMPLOY_CONTEXT = re.compile(
    # 토큰 바로 뒤("[ORG_1]에서 인턴")는 조사라 후보가 아니다 — 누락 검사 재탐지에서 조사를 회사로 잡지 않게
    r"(?<![가-힣A-Za-z0-9\]\)⟩])(?P<org>[가-힣][가-힣A-Za-z0-9&]{1,14}?)"
    r"(?:(?:에서|에)[ \t]?(?:약[ \t]?)?\d{1,2}[ \t]?(?:년|개월)"
    r"|(?:에서[ \t]?|[ \t])(?:인턴|근무|재직|퇴사)"
    r"|에[ \t]?입사)"
)
# 절 제목·흔한 머리말 단어 (경력 머리말 첫 칸 오탐 방지)
_SECTION_WORDS = frozenset(
    "경력 학력 기술 프로젝트 자격 자격증 수상 교육 활동 소개 요약 경험 대외활동 스킬 사용기술 보유기술 기타 담당 역할".split()
)
# 영문 일반어 (머리말 첫 칸·영문 이름 후보에서 뺀다)
_EN_STOP = frozenset(
    (
        "developer engineer manager designer scientist analyst researcher intern lead senior junior head "
        "data product platform backend frontend server mobile cloud web app team summary experience education "
        "skills skill project projects profile contact resume cv portfolio landing page the and for with of name "
        "ai ml machine learning deep software hardware research engineering development design marketing sales "
        "growth business operations full stack devops security infra infrastructure system systems service "
        "services core present current now not air bank cloud pay talk hub box kit lang "
        "cost table note verbal sheet list report plan chart form rule process standard guide manual index "
        "schedule budget account balance statement order invoice contract letter request policy review check "
        "item unit level"
    ).split()
)
# 영문 회사 표기 끝말 — 영문 이름 후보에서는 빼고, 경력 머리말 첫 칸에서는 회사 근거로 본다
_EN_COMPANY_WORDS = frozenset(
    "inc corp co ltd lab labs studio studios technologies tech systems solutions networks games partners holdings "
    "health commerce company".split()
)

# 회사 이름 조각으로 볼 수 있는 품사: 명사·외국어·숫자, 그리고 지어낸 이름이 쪼개질 때 나오는 관형사·부사("새+솔", "늘+봄")
_ORG_TAGS = frozenset({"NNG", "NNP", "NR", "SL", "SH", "SN", "XPN", "XSN", "MM", "MAG"})


def _kiwi_span_tokens(text: str, start: int, end: int) -> list[Token]:
    """[start, end) 와 겹치는 Kiwi 형태소 (start, end, 품사, id) — 그 줄만 분석한다.
    문서 전체를 한 번에 넣으면 줄바꿈을 넘어 "데이터⏎가온누리" → "데이터+가(조사)+온+누리" 처럼 이어 붙인다.
    분석 결과는 문서 하나를 처리하는 동안만 캐시한다 (`kiwi_tools.document_cache`)."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    return [
        (a + line_start, b + line_start, tag, tid)
        for a, b, tag, tid in line_tokens(text[line_start:line_end])
        if a + line_start < end and b + line_start > start
    ]


def _proper_in_context(text: str, start: int, end: int) -> bool:
    """문장 안에서 Kiwi 가 본 [start, end) 가 고유명사 모양인가.

    - 형태소 경계가 후보 경계와 맞고, 조사·어미·용언이 섞이지 않았다 ("엔지니어로", "안녕하세요" 는 아님)
    - 고유명사(NNP)·사전 밖 명사·외국어·관형사/부사 조각 중 하나가 있다 — 사전에 있는 일반 명사만이면 일반어
      ("추천엔진", "현업", "바람").
    """
    toks = _kiwi_span_tokens(text, start, end)
    if not toks or toks[0][0] != start or toks[-1][1] != end:
        return False
    if any(tag not in _ORG_TAGS for _, _, tag, _ in toks):
        return False
    oov = oov_ids()
    return any(
        tag in ("NNP", "SL", "MM", "MAG") or (tag == "NNG" and tid in oov)
        for _, _, tag, tid in toks
    )


def _industry_org(word: str, dicts: Dictionaries) -> re.Match[str] | None:
    """업종 접미어로 끝나고 어간이 일반어가 아닌 단어면 그 매치 (강·약 구분은 호출한 쪽)."""
    m = _ORG_INDUSTRY.fullmatch(word)
    if not m:
        return None
    stem = m.group("stem")
    if (
        # 어간 전체가 일반어 하나일 때만 뺀다. 끝말로 빼면("…국민"+"카드", "…투자"+"저축은행") 실제 회사 모양이 숨는다
        # (privacy-auditor 2차 H2). 일반 합성어("대형시중은행")는 약한 접미어의 Kiwi 검사가 막는다.
        stem.casefold() in dicts.org_generic_stems
        or stem[0].isdigit()
        or word in dicts.tech_terms
        or dicts.pattern().match(word)  # 사전 실체로 시작하면 사전 규칙 몫 ("카카오페이"는 사전에)
    ):
        return None
    return m


def _generic_in_context(text: str, start: int, end: int) -> bool:
    """Kiwi 가 문장 안에서 [start, end) 를 사전에 있는 일반 명사·접사로만 나눴다 ("추심+은행", "자동+화+솔루션")."""
    oov = oov_ids()
    toks = _kiwi_span_tokens(text, start, end)
    return bool(toks) and all(
        (tag == "NNG" and tid not in oov) or tag in ("XPN", "XSN") for _, _, tag, tid in toks
    )


# 직무·직급으로 끝나는 칸은 회사가 아니다 ("Python개발자", "ML엔지니어" — privacy-auditor 2차 M1).
# '리드'·'책임' 같은 짧은 말은 회사 이름 끝("…그리드")과 겹쳐 넣지 않는다.
_JOB_TITLE_END = re.compile(
    r"(?:개발자|엔지니어|연구원|매니저|마케터|디자이너|기획자|분석가|컨설턴트|인턴|사원|팀장|파트장|프리랜서"
    r"|(?i:engineer|developer|manager|designer|scientist|analyst|researcher|intern|marketer|consultant"
    r"|director|architect|specialist))$"
)


def _public_word(word: str, dicts: Dictionaries) -> bool:
    w = word.casefold()
    return (
        word in dicts.public_vocab
        or w in dicts.public_vocab
        or word in dicts.tech_terms
        or w in dicts.tech_terms_casefold
        or w in dicts.en_words
    )


def _public_value(text: str, start: int, end: int, dicts: Dictionaries, *, kiwi: bool) -> bool:
    """[start, end) 가 공개 어휘(공고·NCS 의 말, 기술어, 영어 단어)로만 되어 있는가.

    약한 문맥에서 잡힌 값을 run 전체로 전파할지 정한다 — 공개 어휘("온프레미스", "성과", "사내 교육")면 그 자리만,
    아니면(지원자마다 다른 "다온") 전파한다 (privacy-auditor 2차 후속). Kiwi 를 쓰면 형태소마다, 아니면 띄어쓰기 단어마다 본다.
    관형사·부사 조각("새+솔")은 공개 어휘에 없으므로 지어낸 이름은 전파된다.
    """
    value = text[start:end]
    if _public_word(value, dicts):
        return True
    if not kiwi:
        words = value.split()
        return bool(words) and all(_public_word(w, dicts) for w in words)
    toks = _kiwi_span_tokens(text, start, end)
    return bool(toks) and all(
        tag in ("XSN", "XPN", "SN", "SP", "SW") or _public_word(text[a:b], dicts)
        for a, b, tag, _ in toks
    )


def _org_candidate(
    text: str, start: int, end: int, dicts: Dictionaries, *, kiwi: bool, loose: bool
) -> str | None:
    """경력 머리말·재직 문맥 후보 → 'strong'(전파) / 'context'(고용 문맥 언급에만 전파) / None(회사 아님).

    업종 접미어(약한 접미어 포함)·한글+영문 혼합·영문 회사 표기는 문맥만으로 받는다 (strong). 접미어 없는 한 단어는
    `loose`(구분자로 나뉜 경력 머리말 첫 칸)일 때만 받는다 — 공개 어휘가 아니면 strong, 공개 어휘(기술 외래어
    "온프레미스", 일반어 "중앙")면 context: 그 칸은 바꾸되 본문은 고용 문맥이 붙은 언급만 (재점검 H-A, 2차 M1).
    재직 문맥("X 인턴", "X에서 3년간")·팀 이름 앞("X 개발팀")에서는 접미어 없는 한 단어를 받지 않는다 — 기술 외래어가
    사전 밖 단어로 똑같이 나와 회사와 구분되지 않는다 (공고 문장의 사전 밖 명사 251개 중 70% 이상이 잡혔다).
    이름 모양(성씨+2음절)은 여기서 판단하지 않는다 — 이름 탐지기 몫.
    """
    cand = text[start:end]
    if (
        cand in dicts.tech_terms
        or cand in _SECTION_WORDS
        or cand.casefold() in _EN_STOP
        or cand.isdigit()
        or _JOB_TITLE_END.search(cand)
        or dicts.pattern().search(cand)
        or dicts.attached_pattern().search(cand)
    ):
        return None  # 사전 실체가 들어 있으면 사전 규칙이 같은 토큰으로 묶는다 ("네이버클라우드" → "[ORG_1]클라우드")
    if _industry_org(cand, dicts):
        return "strong"
    if _is_name(cand, dicts, allow_two=False) or cand.endswith(_ORG_UNIT_SUFFIX):
        return None
    if re.fullmatch(
        _SEG_EN, cand
    ):  # 영문 여러 단어: 일반어·기술어가 하나도 없을 때만 ("Gasang Labs")
        words = [w.casefold() for w in cand.split()]
        if any(w in _EN_STOP or w in dicts.tech_terms_casefold for w in words):
            return None
        return "strong"
    if not re.search(r"[가-힣]", cand):
        return None  # 영문 한 단어("Python")는 회사로 보지 않는다
    if re.search(r"[A-Za-z]", cand) and re.fullmatch(r"[가-힣A-Za-z0-9&]+", cand):
        return "strong"  # 한글+영문 섞인 이름 ("가상AI")
    if loose:
        # 구분자 경력 머리말 첫 칸(한 토큰 + 구분자 + 직무·팀 + 기간)은 그 칸을 항상 치환한다 (재점검 H-A).
        # 공개 어휘가 아니면("다온", "누리") 지원자 회사로 보고 문서 전체로 전파하고, 공개 어휘면("라인", "중앙",
        # 기술 외래어 "온프레미스") 고용 문맥이 붙은 언급에만 전파한다 — "생산 라인"은 남기고 "라인에서 3년간"은 바꾼다.
        return "context" if _public_value(text, start, end, dicts, kiwi=kiwi) else "strong"
    return None


def _org_span(
    text: str,
    start: int,
    end: int,
    dicts: Dictionaries,
    kiwi: bool,
    detector: str,
    *,
    loose: bool = False,
) -> Span | None:
    kind = _org_candidate(text, start, end, dicts, kiwi=kiwi, loose=loose)
    if kind is None:
        return None
    return Span(start, end, "ORG", 2, detector, spread="all" if kind == "strong" else "context")


# 경력 머리말은 짧은 줄이다 — 문장(…다./…요.)이거나 긴 줄은 보지 않는다 ("안녕하세요, … 1993년생으로 …")
_HEADER_MAX_LEN = 100
# 문장 끝: 마침표가 붙은 "…다./…요." 또는 줄 끝의 "…다/요/함/음/임" ("이음 | 백엔드" 의 '음'은 아니다)
_SENTENCE_END = re.compile(r"(?:다|요)[.!](?:\s|$)|(?:다|요|함|음|임)[.!]?[ \t]*$")


def detect_org_context(text: str, dicts: Dictionaries, *, kiwi: bool = True) -> list[Span]:
    spans: list[Span] = []
    for m in _ORG_INDUSTRY.finditer(text):
        hit = _industry_org(m.group("org"), dicts)
        if hit is None:
            continue
        if hit.group("suf") in _ORG_WEAK_SUFFIXES and (
            not kiwi or _generic_in_context(text, m.start("org"), m.end("org"))
        ):
            continue
        spans.append(Span(m.start("org"), m.end("org"), "ORG", 2, "org_industry"))
    pos = 0
    for line in text.split("\n"):
        offset, pos = pos, pos + len(line) + 1
        if len(line) > _HEADER_MAX_LEN or not _PERIOD.search(line) or _SENTENCE_END.search(line):
            continue
        b = _LEADING_BULLET.match(line)
        start = b.end() if b else 0
        lp = _LEADING_PERIOD.match(line, start)
        if lp and lp.end() > start and _PERIOD.match(line, start):
            start = lp.end()
        body = line[start:]
        base = offset + start
        sp = None
        m1 = _HEADER_S1.match(body)
        if m1 and _ROLE_MARK.search(m1.group("rest")):
            sp = _org_span(
                text,
                base + m1.start("org"),
                base + m1.end("org"),
                dicts,
                kiwi,
                "org_header",
                loose=True,
            )
            if sp is None and _is_name(m1.group("org"), dicts, allow_two=False):
                # 이름 모양 첫 칸("가상인 | 백엔드 개발자 | 2022.01 ~")은 이름 탐지기(Kiwi)가 전파까지 맡는다.
                # Kiwi 를 끈 경우에도 그 칸은 비우도록 가장 낮은 우선순위·그 칸만으로 둔다 (재점검 M-A4:
                # 이름 모양 공개어 "경영진"·"조직도"가 문서 전체에서 지워지지 않게).
                a, b = base + m1.start("org"), base + m1.end("org")
                spans.append(Span(a, b, "PERSON", 0, "header_cell_name", spread="none"))
        m2 = None if sp else _HEADER_S2.match(body)
        if m2:
            sp = _org_span(
                text, base + m2.start("org"), base + m2.end("org"), dicts, kiwi, "org_header"
            )
        if sp:
            spans.append(sp)
    for m in _EMPLOY_CONTEXT.finditer(text):
        sp = _org_span(text, m.start("org"), m.end("org"), dicts, kiwi, "org_employ")
        if sp:
            spans.append(sp)
    return spans


# 교육 과정: "가상 데이터 분석가 양성 과정 수료", "가상 클라우드 부트캠프 3기", "2021.07 ~ 2021.12 가상 AI 아카데미 이수".
# 과정 이름은 핵심어 앞 단어들(최대 5개)이다 — 구분 기호·날짜·조사로 끝나는 단어("지원으로")에서 멈춘다.
# 같은 줄에 수료·이수·교육·기수·기간 같은 문맥이 있어야 한다 ("인재 양성 과정을 설계" 는 아님).
_COURSE_KEY = re.compile(
    r"양성[ \t]?과정|(?:교육|훈련|전문가|개발자)[ \t]과정|부트캠프|아카데미|사관학교|캠프|스쿨"
)
# 과정 문맥: 들은 사람의 말(수료·이수·수강·기수), 또는 기간 — 기간만 있는 줄은 운영·개발한 사람의 말이 없을 때만
# ("AI 아카데미 강사로 참여 (2023.03)", "부트캠프 상품 결제 기능 개발" 은 과정 이름이 아니다)
_COURSE_ATTENDED = re.compile(r"수료|이수|수강(?!생)|\d{1,3}[ \t]?기(?![가-힣])")
_COURSE_DATE = re.compile(r"(?:19|20)\d{2}[ \t]?[.\-/년~]")
_COURSE_OPERATOR = re.compile(r"운영|강사|모집|개발|구축|설계|관리|기획|지원|담당|입학|멘토")
# 과정 이름에서 멈추는 단어: 기호·날짜·숫자·라벨, 문장 부호로 끝나는 단어, 흔한 부사("이후", "당시").
# 조사·어미로 끝나는 단어("지원으로", "구체적인", "연계하여")는 Kiwi 로 본다 — 끝 글자로 거르면
# "엔지니어"·"디자인" 같은 명사에서 멈춘다. Kiwi 를 쓰지 않을 때는 분명한 조사·어미만.
_COURSE_STOP_WORD = re.compile(
    r"^(?:[-•*·▶▷ㆍ■□◆◇○●▪|:：~–—()\[\]]+|(?:19|20)\d{2}\S*|\d+\S*|\S*[:：.,!?;)\]]"
    r"|이후|이전|당시|현재|최근|우선|먼저|다음|직접|함께|또한|그리고|특히|이어|다시)$"
)
_COURSE_STOP_ENDING = re.compile(
    r"(?:으로|에서|에게|하여|해서|하고|하며|하는|하던|했던|되는|적인|으로서|로서|이며|이고|를|을|및)$"
)


def _course_stop(text: str, k: int, j: int, kiwi: bool) -> bool:
    """과정 이름 왼쪽 확장을 멈출 단어인가. Kiwi 로는 조사(J*)·연결·관형 어미(EC·ETM — "맞춰", "구체적인")로
    끝나는 단어와 부사 한 단어만 멈춘다. 지어낸 이름이 쪼개진 조각("윤+슬(부사)", "늘+보+ㅁ")에서 멈추면
    이름의 앞부분("윤슬")이 남는다 (privacy-auditor 2차 후속에서 확인)."""
    word = text[k:j]
    if not word or _COURSE_STOP_WORD.match(word) or _COURSE_STOP_ENDING.search(word):
        return True
    if not kiwi or not re.search(r"[가-힣]", word):
        return False
    toks = [t for t in _kiwi_span_tokens(text, k, j) if t[0] >= k]
    if not toks:
        return False
    last = toks[-1][2]
    return (
        last.startswith("J") or last in ("EC", "ETM") or (len(toks) == 1 and last in ("MAG", "MAJ"))
    )


def detect_courses(text: str, dicts: Dictionaries, *, kiwi: bool = True) -> list[Span]:
    spans = []
    for m in _COURSE_KEY.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end == -1 else line_end
        rest = text[line_start : m.start()] + " " + text[m.end() : line_end]
        if not (
            _COURSE_ATTENDED.search(rest)
            or (_COURSE_DATE.search(rest) and not _COURSE_OPERATOR.search(rest))
        ):
            continue
        start = m.start()
        if "과정" in m.group(0):
            if start > line_start and not text[start - 1].isspace():
                continue  # "교육훈련 과정" 처럼 앞말에 붙은 '…과정' 은 일반어
        else:  # 핵심어에 붙은 앞말 ("가상부트캠프")
            while start > line_start and not text[start - 1].isspace():
                start -= 1
        words = 0
        while words < 5:
            j = start
            while j > line_start and text[j - 1] in " \t":
                j -= 1
            k = j
            while k > line_start and not text[k - 1].isspace():
                k -= 1
            if _course_stop(text, k, j, kiwi):
                break
            start, words = k, words + 1
        if start == m.start():  # 이름 없는 "부트캠프에서 배운" — 일반어
            continue
        if dicts.pattern().search(text, start, m.end()):
            continue  # 사전 과정("항해99", "부스트캠프")이 들어 있으면 사전 규칙이 같은 토큰으로
        # 이름 부분이 공개 어휘뿐이면("사내 교육 과정", "데이터 분석 아카데미") 그 자리만 치환한다 — 전파하면 같은 말이
        # 문서 전체에서 지워진다. 공개 어휘 밖 말("가상", "미르")이 있으면 기관·과정 이름으로 보고 전파한다.
        public = _public_value(text, start, m.start(), dicts, kiwi=kiwi)
        spans.append(
            Span(start, m.end(), "COURSE", 2, "course", spread="none" if public else "all")
        )
    return spans


# ---------------------------------------------------------------- 사람 이름 (호칭 2, 형태소 1)

_TITLES = (
    "선배님|후배님|멘토님|대표님|이사님|교수님|박사님|강사님|팀장님|매니저님|"
    "님|씨|선임|책임|수석|대리|과장|차장|부장|팀장|파트장|실장|본부장|매니저|교수|박사|"
    "선배|후배|멘토|대표|이사|주임|사원|연구원|조교|강사|"
    # 이름 뒤 협업 명사 ("{이름} 팀원이") — 2026-11-04. '학생'·'멘티' 는 "장학금 학생" 같은 오탐이 나서 넣지 않는다.
    # 이름 앞에 오는 쪽(_TITLE_BEFORE_NAME)에는 넣지 않는다 — "팀원 성과를", "학생 정보를" 이 이름으로 잡힌다.
    "팀원|동료"
)
_NAME_BEFORE_TITLE = re.compile(rf"(?<![가-힣])(?P<name>[가-힣]{{2,4}})[ \t]?(?P<title>{_TITLES})")
_TITLE_BEFORE_NAME = re.compile(
    r"(?:팀장|매니저|교수|선배|멘토|대표|리더|PM|PO)[ \t](?P<name>[가-힣]{3,4}?)"
    r"(?=님|씨|과|와|의|이|가|은|는|을|를|에게|께|[ \t,.)\n]|$)"
)
# 부서·직무를 이름으로 오인하지 않게
_ORG_UNIT_SUFFIX = ("팀", "부", "실", "소", "과", "센터", "본부", "그룹", "랩")


def _is_name(cand: str, dicts: Dictionaries, *, allow_two: bool) -> bool:
    if cand in dicts.tech_terms or cand in dicts.all_aliases or cand.endswith(_ORG_UNIT_SUFFIX):
        return False
    if len(cand) >= 4 and cand[:2] in dicts.surnames:  # 복성 + 이름
        return len(cand) <= 4
    if cand[0] not in dicts.surnames:
        return False
    return len(cand) == 3 or (allow_two and len(cand) == 2)


# 복성 (두 음절 성). '서문' 은 일반어("서문 작성")와 겹쳐 머리줄·호칭 문맥에서만 쓴다.
_COMPOUND_SURNAMES = r"(?:남궁|제갈|선우|황보|독고|사공)"
# 문서 앞부분의 '이름 / 생년 / …' 머리줄 (라벨 없음). 2차: 이름 뒤 "— 직함"·탭·두 칸 공백, 띄어 쓴 복성("남궁 휘")
_HEADER_NAME = re.compile(
    r"^[ \t]*(?:지원자[ \t]*[:：]?[ \t]*)?"
    rf"(?P<name>(?:{_COMPOUND_SURNAMES}|서문)[ \t][가-힣]{{1,2}}|[가-힣]{{2,5}})"
    r"(?=[ \t]*(?:[/|·,(—–\-]|$)|\t|[ \t]{2,})",
    re.M,
)
_HEADER_LINES = 6
# "지원한 OOO입니다", "저는 OOO이라고 합니다"
_SELF_INTRO = re.compile(
    r"(?:지원한|지원하는|지원자|저는|제 이름은|안녕하세요[,!.]?|안녕하십니까[,!.]?)[ \t]*"
    r"(?P<name>[가-힣]{2,5}?)(?=입니다|이라고|라고|이며|이고|이에요|예요)"
)


def detect_intro_names(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    head_end = 0
    for _ in range(_HEADER_LINES):
        nxt = text.find("\n", head_end)
        if nxt == -1:
            head_end = len(text)
            break
        head_end = nxt + 1
    for m in _HEADER_NAME.finditer(text, 0, head_end):
        name = m.group("name")
        if _label_key(name) in _LABELS:
            continue  # "연락처⇥010-…" 의 라벨
        if " " in name or "\t" in name or _is_name(name, dicts, allow_two=False):
            # 이름 모양 공개어("경영진 | …", "조직도 / …")는 그 자리만 (재점검 M-A4). 공개 어휘에는 이름 모양
            # 고유명사·사전 밖 단어가 없으므로 실제 이름은 그대로 전파된다.
            public = _public_word(name, dicts)
            spans.append(
                Span(
                    m.start("name"),
                    m.end("name"),
                    "PERSON",
                    2,
                    "header",
                    spread="none" if public else "all",
                )
            )
    for m in _SELF_INTRO.finditer(text):
        if _is_name(m.group("name"), dicts, allow_two=False):
            spans.append(Span(m.start("name"), m.end("name"), "PERSON", 2, "intro"))
    return spans


# '팀원·동료' 앞에는 꾸미는 말이 흔하다 ("함께한 동료", "새로 합류한 팀원"). 끝음절로 거르면 실제 이름(정우한·최가난)과
# 겹치므로, 흔한 관형형 **단어 목록**일 때만 이름으로 보지 않는다 (privacy-auditor 2차 W3 후속).
_PEER_TITLES = ("팀원", "동료")
_PEER_MODIFIERS = frozenset(
    {
        "함께한",
        "참여한",
        "협업한",
        "같이한",
        "근무한",
        "일한",
        "일했던",
        "함께했던",
        "소속된",
        "배정된",
        "합류한",
        "새로운",
        "기존",
        "성실한",
        "유능한",
        "우수한",
        "든든한",
        "소중한",
        "훌륭한",
        "좋은",
        "다른",
        "모든",
        "같은",
        "여러",
        "신규",
        "주니어",
        "시니어",
        "담당",
        "현재",
        "이전",
    }
)


def detect_titled_names(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    for m in _NAME_BEFORE_TITLE.finditer(text):
        name = m.group("name")
        if m.group("title") in _PEER_TITLES and name in _PEER_MODIFIERS:
            continue
        honorific = m.group("title") in ("님", "씨")
        if _is_name(name, dicts, allow_two=honorific):
            spans.append(Span(m.start("name"), m.end("name"), "PERSON", 2, "title"))
    for m in _TITLE_BEFORE_NAME.finditer(text):
        name = m.group("name")
        if _is_name(name, dicts, allow_two=False):
            spans.append(Span(m.start("name"), m.end("name"), "PERSON", 2, "title"))
    return spans


# 복성 이름 (2차): 띄어 쓴 "남궁 휘"·"선우 진아" 와 붙여 쓴 "황보윤". 복성은 일반어로 거의 쓰이지 않아 문맥 없이 잡는다.
# 이름 뒤는 조사·서술어 첫 글자이거나 단어 경계. 붙여 쓴 '사공'(뱃사공)은 뺀다.
_COMPOUND_NAME = re.compile(
    rf"(?<![가-힣])(?P<name>{_COMPOUND_SURNAMES}[ \t][가-힣]{{1,2}}?|(?:남궁|제갈|선우|황보|독고)[가-힣]{{1,2}}?)"
    r"(?=[^가-힣]|$|님|씨|이|가|은|는|을|를|의|와|과|도|로|에|께|였|랑)"
)


# 복성 뒤 한 글자가 조사·흔한 한 글자 말이면 이름이 아니다 ("사공 이 있다", "독고 와")
_NOT_GIVEN_1 = frozenset("이가은는을를의도와과에로만등및들께서나며고")


def detect_compound_names(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    for m in _COMPOUND_NAME.finditer(text):
        given = m.group("name")[2:].strip()
        if given in dicts.tech_terms or given in _NOT_GIVEN_1:
            continue
        spans.append(Span(m.start("name"), m.end("name"), "PERSON", 2, "compound"))
    return spans


# 이름 모양 보조 규칙 (마지막 점검 H2): Kiwi 가 이름으로 보지 못하는 "성씨 + 흔한 이름 음절 2개" 어절이 조사·호칭·직함
# 앞에 오면 이름 후보 ("김가온은 팀원이다", "팀원 오해온과", "송하준 담당 모듈"). 오탐은 공개 어휘·기술어·사전 별칭과
# Kiwi 가 흔한 일반어 한 단어로 보는 말로 막는다 ("이용자", "조직도", "한국어", "전문가").
@lru_cache(maxsize=4)
def _name_shape_rx(surnames: frozenset[str]) -> re.Pattern[str]:
    single = "".join(sorted(s for s in surnames if len(s) == 1))
    given = "".join(sorted(GIVEN_SYLLABLES))
    return re.compile(
        rf"(?<![가-힣])(?P<name>{_COMPOUND_SURNAMES}[{given}]{{1,2}}|[{single}][{given}]{{2}})"
        rf"(?P<after>{KO_PARTICLES}|[ \t]?{TITLES_AFTER}{KO_PARTICLES}?)(?![가-힣])"
    )


def _shape_is_common(name: str, after: str, dicts: Dictionaries) -> bool:
    """이름 모양이지만 일반어인가 (공고·NCS 줄에서 본 오탐 유형):
    - Kiwi 가 사전 일반 명사 한 단어로 본다 ("정기선")
    - 사전 일반 명사(두 글자 이상)로 시작한다 ("지원+하는", "안정+성", "제조+원", "차이+나")
    - 접미사가 붙은 말이다 ("유형+별", "구조+화")
    - 끝 글자 + 뒤 조사 글자가 공개 어휘다 ("사용빈+도" → 빈도, "기대효+과" → 효과)
    - 뒤 두 글자가 공개 어휘다 ("현+인원"), 사전 일반 명사 + 하 ("제안+하는")
    - 용언으로 시작한다 ("어울리는", "위해서는")
    이름 모양 기술어("기준서", "성형기", "사용성")는 공개 어휘에 있어 앞에서 빠진다 (`build_public_vocab`).
    """
    if after[:1] and (name[-1] + after[0]) in dicts.public_vocab:
        return True
    if len(name) == 3 and name[1:] in dicts.public_vocab:
        return True  # 관형사·접두사 + 공개 어휘 ("현+인원", "소+정비", "고+효율")
    kw = get_kiwi()
    oov = oov_ids()
    if name.endswith("하"):  # 동사 줄기 + 하 ("제안+하는", "구성+하는")
        stem = kw.tokenize(name[:-1])
        if len(stem) == 1 and stem[0].tag == "NNG" and stem[0].id not in oov:
            return True
    toks = kw.tokenize(name)
    if not toks:
        return False
    first = toks[0]
    if len(toks) == 1 and first.tag == "NNG" and first.id not in oov:
        return True
    if first.tag in ("VV", "VA"):  # 용언 ("어울리+는", "위하+어서")
        return True
    if first.tag == "NNG" and first.id not in oov and first.len >= 2:
        return True
    return any(t.tag in ("XSN", "XSV", "XSA") for t in toks)


def detect_name_shapes(text: str, dicts: Dictionaries, *, kiwi: bool = True) -> list[Span]:
    spans = []
    for m in _name_shape_rx(dicts.surnames).finditer(text):
        name = m.group("name")
        if (
            name in dicts.public_vocab
            or name in dicts.tech_terms
            or name in dicts.all_aliases
            or not _is_name(name, dicts, allow_two=False)
            or (kiwi and _shape_is_common(name, m.group("after").strip(), dicts))
        ):
            continue
        # 앞 두 글자가 공개 어휘면("기준+서", "성형+기", "연마+재" — NCS 수행준거의 기술 복합어 모양) 오탐일 때가 많아
        # 그 자리만 바꾼다. 실제 이름이면 조사·호칭이 붙은 다른 언급도 이 규칙이 다시 잡는다.
        spread: Spread = "none" if name[:2] in dicts.public_vocab else "all"
        spans.append(Span(m.start("name"), m.end("name"), "PERSON", 1, "name_shape", spread=spread))
    return spans


# 영문 이름 (2차). 기술 용어와 구분할 수 없어 문맥이 있을 때만:
#   머리줄(문서 앞 6줄)의 줄 첫머리, 한글 이름과 괄호로 짝지은 표기("서가상 (Seo Gasang)"),
#   같은 문서 이메일·계정의 앞부분과 겹치는 표기("Gasang Seo" ↔ gasang.seo@…). 라벨("Name:", "영문 이름:")은 라벨 규칙.
# 잡힌 이름의 다른 표기(어순·이름만·한글 이름의 로마자)는 엔진이 문서 전체에서 같은 토큰으로 바꾼다 (`aliases`).
_EN_WORD = r"[A-Z][a-z]+(?:-[A-Za-z][a-z]+)?"
_EN_CAPS = r"[A-Z]{2,}(?:-[A-Z]{2,})?"
_EN_NAME = rf"(?:{_EN_WORD}(?: {_EN_WORD}){{1,2}}|{_EN_CAPS}(?: {_EN_CAPS}){{1,2}})"
# 머리줄: 줄 첫머리 2~4 단어. 일반어(Data·Engineer …)가 나오면 그 앞까지만 후보 ("Seoyeon Han Data Engineer" → "Seoyeon Han")
_EN_HEADER = re.compile(
    rf"^[ \t]*(?P<en>{_EN_WORD}(?: {_EN_WORD}){{1,3}}|{_EN_CAPS}(?: {_EN_CAPS}){{1,3}})(?P<after>[^\n]*)",
    re.M,
)
_EN_HEADER_END = re.compile(r"[ \t]*(?:[(/|·,—–\-:：]|$)|\t|[ \t]{2,}")
_EN_ANY = re.compile(rf"(?<![A-Za-z])(?P<en>{_EN_NAME})(?![A-Za-z])")
_EN_PAIR = re.compile(
    rf"(?P<ko>[가-힣]{{2,4}})[ \t]*\([ \t]*(?P<en>{_EN_NAME})[ \t]*\)"
    rf"|(?P<en2>{_EN_NAME})[ \t]*\([ \t]*(?P<ko2>[가-힣]{{2,4}})[ \t]*\)"
)
_ROMAN_SYLLABLE = (
    r"(?:kk|tt|pp|ss|jj|ch|g|k|n|d|t|r|l|m|b|p|s|j|h)?"
    r"(?:yeo|yae|wae|wa|wo|we|wi|ya|ye|yo|yu|ae|eo|eu|ui|oe|oo|ee|ou|a|e|i|o|u)"
    r"(?:ng|n|m|l|k|p|t|r)?"
)
_ROMAN_WORD = re.compile(rf"(?:{_ROMAN_SYLLABLE})(?:-?{_ROMAN_SYLLABLE}){{0,3}}h?", re.IGNORECASE)
_ROMAN_GIVEN_1 = re.compile(rf"(?:{_ROMAN_SYLLABLE}){{1,3}}h?", re.IGNORECASE)
_ROMAN_GIVEN_2 = re.compile(rf"(?:{_ROMAN_SYLLABLE}){{2,3}}h?", re.IGNORECASE)
# 영어 단어이기도 한 성씨 로마자 — 이름 부분이 한 음절이면 받지 않는다 ("Go Lang", "Song Title", "Hong Kong").
# 그 밖의 성씨(Park·Kim·Lee …)는 한 음절 이름도 받는다 ("Park Jin", privacy-auditor H1).
_AMBIGUOUS_ROMAN_SURNAMES = frozenset(
    "song moon sun won ban bang gong kong wang gil man ham min han jin yang seo sin mun nam bong ok yong eun hong "
    "young ma".split()
)


def same_romanized(korean: str, latin: str, dicts: Dictionaries) -> bool:
    """영문 표기가 한글 이름의 로마자 전체 표기인가 ("김민" ↔ "Kim Min")."""
    parts = split_name(korean, dicts.surnames)
    return bool(parts) and bool(full_name_regex(*parts).fullmatch(latin))


def _en_stop_word(word: str, dicts: Dictionaries) -> bool:
    w = word.casefold()
    return w in _EN_STOP or w in _EN_COMPANY_WORDS or w in dicts.tech_terms_casefold


def _romanized_korean_name(en: str, dicts: Dictionaries) -> bool:
    """성씨 로마자 + 한국어 음절 로마자 이름 ("Seo Gaon", "Yu Na Kim", "HAJIN SEOK", "Park Jin")."""
    words = [w.casefold() for w in en.split()]
    if len(words) < 2 or any(_en_stop_word(w, dicts) for w in words):
        return False
    for i in (0, len(words) - 1):
        sur = words[i]
        if sur not in dicts.roman_surnames:
            continue
        rest = "".join(words[:i] + words[i + 1 :]).replace("-", "")
        ambiguous = len(sur) <= 2 or sur in _AMBIGUOUS_ROMAN_SURNAMES
        if (_ROMAN_GIVEN_2 if ambiguous else _ROMAN_GIVEN_1).fullmatch(rest):
            return True
    return False


_EMAIL_LOCAL = re.compile(r"(?P<local>[A-Za-z0-9._%+-]{1,64})@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _echoed_in_account(en: str, accounts: list[str]) -> bool:
    """영문 표기의 단어가 모두 같은 문서 이메일·계정 앞부분에 있다 ("Gaon Seo" ↔ gaon.seo@…).
    일반어·회사 표기가 섞이면 아니다 ("Data Guard" ↔ data.guard@…)."""
    words = [w.casefold() for w in re.split(r"[ \t-]", en) if len(w) >= 2]
    if any(w in _EN_STOP or w in _EN_COMPANY_WORDS for w in words):
        return False
    return len(words) >= 2 and any(all(w in acc for w in words) for acc in accounts)


def detect_english_names(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    head_end = 0
    for _ in range(_HEADER_LINES):
        nxt = text.find("\n", head_end)
        if nxt == -1:
            head_end = len(text)
            break
        head_end = nxt + 1
    for m in _EN_HEADER.finditer(text, 0, head_end):
        words = m.group("en").split(" ")
        keep = next((i for i, w in enumerate(words) if _en_stop_word(w, dicts)), len(words))
        if keep < 2:
            continue
        if keep == len(words) and not _EN_HEADER_END.match(m.group("after")):
            continue  # 머리줄 첫머리 뒤가 구분자·줄끝이 아니면 문장
        cand = " ".join(words[:keep])
        if _romanized_korean_name(cand, dicts):
            spans.append(Span(m.start("en"), m.start("en") + len(cand), "PERSON", 2, "en_header"))
    for m in _EN_PAIR.finditer(text):
        ko, en = ("ko", "en") if m.group("ko") else ("ko2", "en2")
        ko_v, en_v = m.group(ko), m.group(en)
        if any(_en_stop_word(w, dicts) for w in en_v.split()):
            continue
        # 영문 쪽이 한글 이름의 로마자 표기면 받는다 ("김민 (Kim Min)" — 두 음절 이름도). 한글 쪽이 이름 모양이면
        # 성씨 로마자 검사 없이 받되(privacy-auditor H1, "서가온 (Daniel Seo)"), 영문 단어가 한국어 음절 로마자로
        # 나뉠 때만 — 이름 모양 기술어 뒤 영문 용어("제어기 (Motor Controller)", "원가표 (Cost Table)")를 이름으로
        # 잡으면 그 단어가 run 전체에서 지워진다.
        if same_romanized(ko_v, en_v, dicts) or (
            _is_name(ko_v, dicts, allow_two=False)
            and all(_ROMAN_WORD.fullmatch(w) for w in en_v.split())
        ):
            spans.append(Span(m.start(ko), m.end(ko), "PERSON", 2, "en_pair"))
            # 같은 사람의 한글·영문 표기는 한 토큰 (privacy-auditor 2차 M2)
            spans.append(Span(m.start(en), m.end(en), "PERSON", 2, "en_pair", canonical=ko_v))
    accounts = [m.group("local").casefold() for m in _EMAIL_LOCAL.finditer(text)]
    accounts += [m.group("h").casefold() for m in _HANDLE.finditer(text)]
    accounts += [
        u.casefold()
        for u in re.findall(r"(?:github\.com|gitlab\.com|linkedin\.com/in)/([A-Za-z0-9_.-]+)", text)
    ]
    if accounts:
        for m in _EN_ANY.finditer(text):
            if _echoed_in_account(m.group("en"), accounts):
                spans.append(Span(m.start("en"), m.end("en"), "PERSON", 2, "en_account"))
    return spans


# Kiwi 이름 후보 오탐 줄이기 (2026-11-04 분석 — 성씨로 시작하는 3음절 명사 26% 가 어떤 문맥에선 이름으로 잡힘).
# 재현율(누출 방지)이 우선이다 — 문맥 규칙은 실제 이름을 놓치게 한다 ("김민재 값 비교" 처럼 뒤에 기술어가 오는 일상 문장,
# privacy-auditor 2차 H-new1). 그래서 오탐 명사는 **단어 목록(`tech_terms.txt`)으로** 막고, 규칙은 하나만 둔다:
# 이름에 쓰이지 않는 외래어 음절(튜·뷰·큐·셋 …)이 있으면 명사. 이름에 있을 법한 음절(량·률·스·크·트·플·값 …)은 넣지 않고,
# 바로 뒤에 조사가 붙으면 면제한다 (privacy-auditor 1차 H2·2차 W1). Kiwi 신뢰도·사전 밖 여부로 거르지 않는다.
_FOREIGN_SYLLABLES = frozenset("튜퓨뷰챗쿼큐셋웨엑텍젝렉펙픽")
_PARTICLE_AFTER = re.compile(r"(?:에게|에서|으로|이랑|이|가|과|와|은|는|을|를|의|도|로|랑|께)")


def _kiwi_noun_like(text: str, start: int, end: int) -> bool:
    if _PARTICLE_AFTER.match(text, end):
        return False
    return any(ch in _FOREIGN_SYLLABLES for ch in text[start + 1 : end])


def detect_kiwi_names(text: str, dicts: Dictionaries) -> list[Span]:
    """Kiwi 고유명사(NNP) 중 '성씨 + 이름' 모양만 사람 이름으로 본다. 붙어 있는 NNP 는 합친다 (박+지훈)."""
    tokens = [t for t in get_kiwi().tokenize(text)]
    spans = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.tag != "NNP":
            i += 1
            continue
        start, end = t.start, t.start + t.len
        j = i + 1
        while j < len(tokens) and tokens[j].tag == "NNP" and tokens[j].start == end:
            end = tokens[j].start + tokens[j].len
            j += 1
        cand = text[start:end]
        if (
            re.fullmatch(r"[가-힣]{3,4}", cand)
            and _is_name(cand, dicts, allow_two=False)
            and not _kiwi_noun_like(text, start, end)
        ):
            spans.append(Span(start, end, "PERSON", 1, "kiwi"))
        i = j
    return spans
