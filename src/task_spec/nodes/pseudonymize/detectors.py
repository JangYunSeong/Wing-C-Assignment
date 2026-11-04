"""탐지기. 각 탐지기는 원문 위치(Span) 목록을 돌려준다. 값은 Span 안에만 있고 로그로 나가지 않는다.

우선순위(priority): 겹치면 큰 쪽이 이긴다. 같으면 긴 쪽.
  3 패턴(이메일·전화·URL·주민번호)  /  2 사전·접미어·인구통계·호칭 이름  /  1 라벨 값·형태소(Kiwi) 이름
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

from task_spec.nodes.pseudonymize.dictionaries import (
    LEFT,
    RIGHT,
    SCHOOL_ONLY_SUFFIX,
    Dictionaries,
)

REMOVE = "REMOVE"
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

    def value(self, text: str) -> str:
        return text[self.start : self.end]


# ---------------------------------------------------------------- 패턴 (priority 3)

_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("email", "CONTACT", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "url",
        "LINK",
        re.compile(
            r"(?:https?://|www\.)[^\s<>()\[\]가-힣]+"
            r"|(?<![\w.])(?:github\.com|gitlab\.com|linkedin\.com|velog\.io|medium\.com|brunch\.co\.kr"
            r"|blog\.naver\.com|[a-z0-9-]+\.github\.io|[a-z0-9-]+\.tistory\.com|[a-z0-9-]+\.notion\.site"
            # 스킴 없는 배포 주소 — 개인 프로젝트 주소가 이름·학교를 드러낸다 (2026-11-04 분석). 다단계 서브도메인 허용
            r"|(?:[a-z0-9-]+\.)+(?:vercel\.app|netlify\.app|herokuapp\.com|pages\.dev|web\.app|firebaseapp\.com"
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
    ("photo", re.compile(r"(?m)^\s*\[?\s*사진\s*\]?\s*$")),
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
            r"\(?[ \t]*(?:전공[ \t]?)?(?:학점|평점|GPA)[ \t]*[:：]?[ \t]*\d(?:\.\d{1,2})?[ \t]*/[ \t]*\d(?:\.\d{1,2})?[ \t]*\)?",
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

_LABELS: dict[str, str] = {
    "성명": "PERSON",
    "이름": "PERSON",
    "name": "PERSON",
    "주소": "CONTACT",
    "거주지": "CONTACT",
    "address": "CONTACT",
    "연락처": "CONTACT",
    "휴대폰": "CONTACT",
    "전화번호": "CONTACT",
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
    "학교": "SCHOOL",
    "출신학교": "SCHOOL",
    "최종학력": "SCHOOL",
    "학력": "SCHOOL",
    "서비스명": "PRODUCT",
    "제품명": "PRODUCT",
}
_LABEL_RX = re.compile(
    r"(?im)^[ \t]*[-•*·]?[ \t]*(?P<label>"
    + "|".join(sorted((re.escape(k) for k in _LABELS), key=len, reverse=True))
    + r"|출신 학교|최종 학력)[ \t]*[:：|][ \t]*(?P<value>[^\n]+?)[ \t]*$"
)


def detect_labels(text: str) -> list[Span]:
    spans = []
    for m in _LABEL_RX.finditer(text):
        cat = _LABELS[m.group("label").casefold().replace(" ", "")]
        start, end = m.start("value"), m.end("value")
        if cat == "PERSON":  # "김가상 (남, 27세)" → 이름만
            cut = re.search(r"[(,/]", m.group("value"))
            if cut:
                end = start + cut.start()
            while end > start and text[end - 1] == " ":
                end -= 1
        if end > start:
            spans.append(Span(start, end, cat, 2 if cat == REMOVE else 1, f"label:{cat}"))
    return spans


# ---------------------------------------------------------------- 사전·접미어 (priority 2)

_CONTEXT_RX = re.compile(r"(?:19|20)\d{2}\s?[.\-/년]|재직|근무|인턴|입사|퇴사")
_SCHOOL_SUFFIX = re.compile(rf"{LEFT}[가-힣A-Za-z]{{2,15}}(?:대학교|대학원|고등학교|대학){RIGHT}")
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


def _line_of(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return text[line_start : line_end if line_end != -1 else len(text)]


def detect_dictionary(text: str, dicts: Dictionaries) -> list[Span]:
    spans = []
    for m in _QUOTED_PRODUCT.finditer(text):
        g = "name" if m.group("name") is not None else "name2"
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


# 문서 앞부분의 '이름 / 생년 / …' 머리줄 (라벨 없음)
_HEADER_NAME = re.compile(
    r"^[ \t]*(?:지원자[ \t]*[:：]?[ \t]*)?(?P<name>[가-힣]{2,5})[ \t]*(?=[/|·,(]|$)", re.M
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
        if _is_name(m.group("name"), dicts, allow_two=False):
            spans.append(Span(m.start("name"), m.end("name"), "PERSON", 2, "header"))
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


_kiwi: Any = None
_kiwi_lock = threading.Lock()


def _get_kiwi() -> Any:
    global _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            from kiwipiepy import Kiwi  # 로컬 형태소 분석기 — 외부 호출 없음

            _kiwi = Kiwi()
        return _kiwi


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
    tokens = [t for t in _get_kiwi().tokenize(text)]
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
