"""한글 이름 → 로마자 표기 변형 정규식 (2026-11-04 2차, privacy-auditor H1·M2). 외부 호출 없음.

이력서에는 같은 사람이 "서가온"·"Seo Gaon"·"Gaon Seo"·"GAON SEO"·"Ga-on Seo" 로 함께 나온다.
한글 이름을 잡으면 그 로마자 변형도 문서 어디서든 같은 토큰으로 바꿔야 영문 표기가 새지 않는다.

- 음절마다 국어의 로마자 표기법(2000) + 이름에 흔한 변형(ㅓ→u, ㅜ→oo, ㅣ→ee, ㄱ→k …)을 모은다.
- 성씨는 관용 표기(이→Lee/Yi/Rhee, 박→Park, 최→Choi …)를 더한다.
- 전체 이름: 성-이름 / 이름-성 순서, 이름 음절 사이 붙임·하이픈·공백, 성과 이름 사이 공백·쉼표. 대소문자 무시.
- 이름만(성 없이)은 두 음절 이상이고 흔한 영어·기술·공개 어휘 단어와 겹치지 않을 때만 (`aliases`).

privacy-auditor 가 참고로 든 ko-pii(MIT) `context/romanization.py` 와 같은 방식(음절 분해·변형 표)이다. 그 코드는 보지 않고 새로 썼다.
"""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import product

_INITIALS: tuple[tuple[str, ...], ...] = (
    ("g", "k"),  # ㄱ
    ("kk", "gg"),  # ㄲ
    ("n",),  # ㄴ
    ("d", "t"),  # ㄷ
    ("tt", "dd"),  # ㄸ
    ("r", "l"),  # ㄹ
    ("m",),  # ㅁ
    ("b", "p"),  # ㅂ
    ("pp", "bb"),  # ㅃ
    ("s",),  # ㅅ
    ("ss", "s"),  # ㅆ
    ("",),  # ㅇ
    ("j", "ch"),  # ㅈ
    ("jj",),  # ㅉ
    ("ch",),  # ㅊ
    ("k",),  # ㅋ
    ("t",),  # ㅌ
    ("p",),  # ㅍ
    ("h",),  # ㅎ
)
_VOWELS: tuple[tuple[str, ...], ...] = (
    ("a",),  # ㅏ
    ("ae",),  # ㅐ
    ("ya",),  # ㅑ
    ("yae",),  # ㅒ
    ("eo", "u", "o"),  # ㅓ
    ("e",),  # ㅔ
    ("yeo", "yu", "yo"),  # ㅕ
    ("ye",),  # ㅖ
    ("o", "oh"),  # ㅗ
    ("wa",),  # ㅘ
    ("wae",),  # ㅙ
    ("oe", "oi", "we"),  # ㅚ
    ("yo",),  # ㅛ
    ("u", "oo"),  # ㅜ
    ("wo", "weo"),  # ㅝ
    ("we",),  # ㅞ
    ("wi", "wee"),  # ㅟ
    ("yu", "yoo"),  # ㅠ
    ("eu", "u"),  # ㅡ
    ("ui", "ee", "eui"),  # ㅢ
    ("i", "ee"),  # ㅣ
)
_FINALS: tuple[tuple[str, ...], ...] = (
    ("",),
    ("k", "g"),  # ㄱ
    ("k",),  # ㄲ
    ("k",),  # ㄳ
    ("n",),  # ㄴ
    ("n",),  # ㄵ
    ("n",),  # ㄶ
    ("t", "d"),  # ㄷ
    ("l", "r"),  # ㄹ
    ("k",),  # ㄺ
    ("m",),  # ㄻ
    ("l",),  # ㄼ
    ("l",),  # ㄽ
    ("l",),  # ㄾ
    ("p",),  # ㄿ
    ("l",),  # ㅀ
    ("m",),  # ㅁ
    ("p", "b"),  # ㅂ
    ("p",),  # ㅄ
    ("t", "s"),  # ㅅ
    ("t",),  # ㅆ
    ("ng",),  # ㅇ
    ("t",),  # ㅈ
    ("t",),  # ㅊ
    ("k",),  # ㅋ
    ("t",),  # ㅌ
    ("p",),  # ㅍ
    ("t",),  # ㅎ
)
# 성씨 관용 표기 (기계 변형에 더한다)
_SURNAME_EXTRA: dict[str, tuple[str, ...]] = {
    "이": ("lee", "yi", "rhee", "rhie", "ri", "i"),
    "박": ("park", "pak", "bak"),
    "최": ("choi", "choe", "chey"),
    "정": ("jung", "jeong", "chung", "jeoung"),
    "김": ("kim", "gim"),
    "노": ("noh", "roh", "ro"),
    "류": ("ryu", "yoo", "yu", "you", "ryoo"),
    "유": ("yoo", "you", "ryu"),
    "임": ("lim", "im", "yim", "rim"),
    "신": ("shin", "sin"),
    "안": ("ahn", "an"),
    "손": ("sohn", "son"),
    "백": ("baek", "paik", "back"),
    "허": ("heo", "huh", "hur"),
    "엄": ("eom", "um", "uhm"),
    "오": ("oh", "o"),
    "조": ("cho", "jo", "joe"),
    "윤": ("yoon", "yun"),
    "서": ("seo", "suh"),
    "우": ("woo", "wu"),
    "구": ("koo", "ku", "gu"),
    "고": ("ko", "go", "koh"),
    "주": ("joo", "ju", "chu"),
    "성": ("sung", "seong"),
    "곽": ("kwak", "gwak"),
    "권": ("kwon", "gwon"),
    "문": ("moon", "mun"),
    "명": ("myung", "myeong"),
    "경": ("kyung", "gyeong"),
    "현": ("hyun", "hyeon"),
    "변": ("byun", "byeon"),
    "편": ("pyun", "pyeon"),
    "육": ("yook", "yuk"),
    "국": ("kook", "guk"),
    "남궁": ("namgung", "namkung"),
    "제갈": ("jegal", "chegal"),
    "선우": ("seonwoo", "sunwoo"),
    "황보": ("hwangbo",),
    "독고": ("dokgo", "dokko"),
    "사공": ("sagong",),
    "서문": ("seomun",),
}
COMPOUND_SURNAMES = ("남궁", "제갈", "선우", "황보", "독고", "사공", "서문")


@lru_cache(maxsize=4096)
def syllable_variants(ch: str) -> tuple[str, ...]:
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return ()
    ini, vow, fin = code // 588, (code % 588) // 28, code % 28
    out = {i + v + f for i, v, f in product(_INITIALS[ini], _VOWELS[vow], _FINALS[fin])}
    if ini == 9 and vow == 20:  # 시·신 → shi·shin
        out |= {"shi" + f for f in _FINALS[fin]}
    if ini == 11 and vow == 13:  # 우 → woo
        out |= {"woo" + f for f in _FINALS[fin]} | {"wu" + f for f in _FINALS[fin]}
    return tuple(sorted(out, key=len, reverse=True))


def _alt(options: set[str] | tuple[str, ...]) -> str:
    return "(?:" + "|".join(re.escape(o) for o in sorted(options, key=len, reverse=True)) + ")"


def split_name(name: str, surnames: frozenset[str]) -> tuple[str, str] | None:
    """'서가온' → ('서', '가온'), '남궁 휘' → ('남궁', '휘'). 성씨 목록 밖이면 None."""
    compact = name.replace(" ", "")
    if not re.fullmatch(r"[가-힣]{2,5}", compact):
        return None
    if len(compact) >= 3 and compact[:2] in COMPOUND_SURNAMES:
        return compact[:2], compact[2:]
    if compact[0] in surnames and len(compact) <= 4:
        return compact[0], compact[1:]
    return None


@lru_cache(maxsize=64)
def _surname_variants(surname: str) -> frozenset[str]:
    opts: set[str] = set(_SURNAME_EXTRA.get(surname, ()))
    if len(surname) == 1:
        opts |= set(syllable_variants(surname))
    else:
        opts |= {"".join(p) for p in product(*(syllable_variants(c) for c in surname))}
    return frozenset(opts)


def surname_variants(surname: str) -> set[str]:
    return set(_surname_variants(surname))


def surname_pattern(surname: str) -> str:
    return _alt(_surname_variants(surname))


# 이름(성 없이) 정규식·검사는 지원자 이름이 키가 되므로 프로세스 전역 캐시에 두지 않는다 (code review M4 와 같은 이유).
# 엔진이 run 단위로 컴파일해 둔다 (`aliases.AliasSet`).


def given_pattern(given: str) -> str:
    """이름 음절 사이 붙임·하이픈·밑줄·공백 ("Gaon", "Ga-on", "Ga_on", "Ga On")."""
    return r"[-_ ]?".join(_alt(syllable_variants(c)) for c in given)


def canonical_given(given: str) -> str:
    """국어의 로마자 표기법 기본형 ("가온" → "gaon"). 이름이 겹치는 사람 찾기에 쓴다."""
    out = []
    for ch in given:
        code = ord(ch) - 0xAC00
        if not 0 <= code < 11172:
            return ""
        out.append(
            _INITIALS[code // 588][0] + _VOWELS[(code % 588) // 28][0] + _FINALS[code % 28][0]
        )
    return "".join(out)


# 성과 이름 사이: 공백·쉼표·하이픈·밑줄·점 ("gaon.seo" — 이메일·계정 앞부분 모양, 마지막 점검 M1)
_FULL_SEP = r"(?:[ \t]*,?[ \t]+|[ \t]*,[ \t]*|[-_.])"


def full_name_pattern(surname: str, given: str) -> str:
    """성+이름 로마자 전체 표기: 성-이름·이름-성, 공백·쉼표·하이픈·밑줄 (재점검 M-B), 이름이 두 음절 이상이거나 복성이면
    붙여 쓰기도("seogaon", "SeoGaon"). 한 음절 이름을 붙여 쓰면 영어 단어와 겹친다("이온" → "ion")."""
    sur, giv = surname_pattern(surname), given_pattern(given)
    sep = _FULL_SEP + ("?" if len(given) >= 2 or len(surname) >= 2 else "")
    return rf"(?<![A-Za-z])(?:{sur}{sep}{giv}|{giv}{sep}{sur})(?![A-Za-z])"


def full_name_regex(surname: str, given: str) -> re.Pattern[str]:
    return re.compile(full_name_pattern(surname, given), re.IGNORECASE)


def initials_pattern(surname: str, given: str) -> str:
    """이니셜 + 성 ("G. Seo", "G.O. Seo", "Seo G.", "Seo, G. O."). 성이 확정된 이름에서만 만든다 (재점검 M-B)."""
    inits = [_alt({v[0] for v in syllable_variants(c) if v}) for c in given]
    init = inits[0] + r"\."
    if len(inits) >= 2:
        init += rf"(?:[ \t]?{inits[1]}\.)?"
    sur = surname_pattern(surname)
    return rf"(?<![A-Za-z])(?:{init}[ \t]*{sur}(?![A-Za-z])|{sur}[ \t]*,?[ \t]+{init})"


# 이름 뒤 조사 (한글 이름만 표기의 문맥)
KO_PARTICLES = (
    r"(?:은|는|이|가|을|를|의|와|과|도|로|으로|에게|에게서|한테|에서|께|께서|랑|이랑|하고|이가|이는|이도|이의|이를|이와|"
    r"이에게|이랑|이나|이고|이며|이란|이라는|아|야|이야)"
)
# 이름 뒤·앞 호칭·직함 (한 칸 띄어 쓰거나 붙여 씀)
TITLES_AFTER = (
    r"(?:님|씨|팀원|매니저|선배|후배|책임|선임|수석|대리|과장|차장|부장|팀장|파트장|실장|본부장|리드|멘토|멘티|"
    r"연구원|인턴|주임|사원|동료|담당|담당자|교수|박사|강사|대표|이사)"
)
_TITLES_BEFORE = (
    "팀원",
    "매니저",
    "팀장",
    "선배",
    "후배",
    "멘토",
    "멘티",
    "동료",
    "리드",
    "책임",
    "선임",
    "수석",
    "대리",
    "과장",
    "교수",
    "강사",
    "대표",
    "PM",
    "PO",
)
_KO_NAME_END = rf"(?={KO_PARTICLES}?(?![가-힣]))"


def korean_spaced_pattern(surname: str, given: str) -> str:
    """띄어 쓴 한글 성+이름 ("서 가온", "서 가온은") — 붙여 쓴 표기는 원 값 그대로 전파된다."""
    return rf"(?<![가-힣]){re.escape(surname)}[ \t]+{re.escape(given)}{_KO_NAME_END}"


def korean_given_patterns(given: str, level: str) -> list[str]:
    """한글 이름만 표기 ("가온이 말했다", "가온님", "팀원 가온") — 성+이름이 잡힌 사람에게만 (마지막 점검 H1).

    level:
    - "bare": 조사 없이 단어로만 와도 ("가온 소개"), 조사, 호칭·직함 앞뒤 — 이름처럼 쓰이는 드문 말
    - "particle": 조사·호칭·직함이 붙을 때만 — Kiwi 가 동사·여러 조각으로 나누는 이름("해온" = 하+어+오+ㄴ, "해온 일")
    - "title": 호칭·직함이 앞뒤에 올 때만 — 흔한 일반 명사·공개 어휘인 이름("하늘이 맑다"는 남긴다)
    """
    g = re.escape(given)
    before = "".join(
        f"(?<={re.escape(t)} )" if i == 0 else f"|(?<={re.escape(t)} )"
        for i, t in enumerate(_TITLES_BEFORE)
    )
    pats = [
        rf"(?<![가-힣]){g}(?=[ \t]?{TITLES_AFTER}{KO_PARTICLES}?(?![가-힣]))",
        rf"(?:{before}){g}{_KO_NAME_END}",
    ]
    if level in ("bare", "particle"):
        pats.append(rf"(?<![가-힣]){g}(?={KO_PARTICLES}(?![가-힣]))")
    if level == "bare":
        pats.append(rf"(?<![가-힣]){g}(?![가-힣])")
    return pats


def given_only_pattern(given: str) -> str:
    return rf"(?<![A-Za-z]){given_pattern(given)}(?![A-Za-z])"
