"""공개 어휘 사전 만들기 — `src/task_spec/resources/dicts/public_vocab.txt` (2026-11-04 2차, privacy-auditor 후속).

가명화가 '그 자리만 치환할지, run 전체로 전파할지'를 정할 때 쓴다. 경력 머리말 첫 칸 같은 약한 문맥에서 잡힌 값이
공개 문서에 흔히 나오는 말(기술 외래어 "온프레미스", 일반어 "성과")이면 전파하지 않고, 그렇지 않으면(지원자마다 다른
회사 이름 "다온") 전파한다.

입력은 온톨로지 산출물(sqlite)의 **공개 데이터만** — 채용 공고 문장(`posting_line`)과 NCS 수행준거(`ncs_criterion`).
지원자 데이터는 읽지 않는다. 로컬 형태소 분석기(Kiwi)만 쓰고 외부 호출은 없다.

- 한글 명사(NNG·NNP, 2자 이상)와 영문 단어(2자 이상, 소문자)를 모은다.
- 뺀다 (공개 어휘로 분류되면 그 값의 전파가 좁아지므로, 회사·사람 이름이 들어가지 않게):
  - 회사 사전(`companies.txt`)의 모든 표기, 온톨로지 `company` 표의 회사 이름.
  - Kiwi 사전에 있는 고유명사(NNP — 브랜드·지명·인명) 중 10번 미만 나온 것. 자주 나오는 것은 Kiwi 가 고유명사로 붙인
    기술어("클라우드", "트래픽")라 둔다. 사전 밖 고유명사(기술 외래어 "리팩토링" 등)는 둔다 (code review M3).
  - 공고 문장의 사람 이름 모양(성씨 + 두 음절, 복성 + 한두 음절)이면서 고유명사이거나 Kiwi 사전 밖인 단어 — 담당자 이름 등.
    NCS 수행준거는 표준 문서라 이 거르기를 하지 않는다 (이름 모양 기술어 "기준서"·"성형기"를 공개 어휘로 남기려고).
  - 두 음절이 모두 흔한 이름 음절이고 말뭉치에 7번 미만 나온 두 음절 단어 ("가온") — 이름으로 쓰이는 드문 말 (code review M3).
  - 도메인·주소 모양 영문 (www, onday.com — 재점검 Low).

사용: uv run python -m eval.pseudonymize.build_public_vocab --sqlite <ontology.sqlite>
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter
from pathlib import Path

from task_spec.nodes.pseudonymize.dictionaries import GIVEN_SYLLABLES, load_dictionaries
from task_spec.nodes.pseudonymize.kiwi_tools import kiwi_version, oov_ids

OUT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "task_spec"
    / "resources"
    / "dicts"
    / "public_vocab.txt"
)
_HANGUL = re.compile(r"[가-힣]{2,12}")
_DOMAIN = re.compile(
    r"^(?:www|https?|ftp)$|@|\.(?:com|net|org|io|co|kr|ai|dev|app|me|xyz|biz|info|jp|us|cn)(?:\.|$)"
)
_PROPER_MIN = 10
_RARE = 7  # 기술 외래어 실험(공고 사전 밖 명사 251개)의 본문 과치환이 늘지 않는 가장 큰 값 — 10 이면 "제진"(7)·"현가"(9)가 빠진다
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,24}")
_COMPOUND_SURNAMES = ("남궁", "제갈", "선우", "황보", "독고", "사공", "서문")


def _name_shaped(word: str, surnames: frozenset[str]) -> bool:
    if len(word) in (3, 4) and word[:2] in _COMPOUND_SURNAMES:
        return True
    return len(word) == 3 and word[0] in surnames


def build(sqlite_path: Path) -> tuple[list[str], dict[str, int]]:
    from kiwipiepy import Kiwi  # 로컬 형태소 분석기 — 외부 호출 없음

    dicts = load_dictionaries()
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    postings = [r[0] for r in con.execute("select text from posting_line") if r[0]]
    ncs = [r[0] for r in con.execute("select text from ncs_criterion") if r[0]]
    company_names = {r[0].casefold() for r in con.execute("select name from company") if r[0]}
    version = con.execute("select * from ontology_version").fetchone()
    con.close()

    known_companies = {
        a.casefold() for e in dicts.entries if e.category == "ORG" for a in e.aliases
    }
    known_companies |= company_names
    kiwi = Kiwi()
    oov = oov_ids(kiwi)  # 런타임 탐지기와 같은 판정 (code review L3)
    counts: Counter[str] = Counter()
    proper: dict[str, int] = {}  # Kiwi 사전 고유명사 → 나온 수 (흔하면 기술어로 보고 둔다)
    vocab: set[str] = set()
    dropped: Counter[str] = Counter()
    for source, texts in (("posting", postings), ("ncs", ncs)):
        for tokens in kiwi.tokenize(texts):
            for t in tokens:
                form = t.form
                if t.tag in ("NNG", "NNP") and _HANGUL.fullmatch(form):
                    counts[form] += 1
                    if form.casefold() in known_companies:
                        dropped["company"] += 1
                        continue
                    # 이름 모양이면서 고유명사이거나 Kiwi 사전 밖 단어면 뺀다 — 공고 문장만. NCS 수행준거는 표준 문서라
                    # 사람 이름이 없고, 이름 모양 기술어("기준서", "성형기")가 공개 어휘에 있어야 이름 모양 보조 규칙이
                    # 그 말을 이름으로 잡지 않는다 (마지막 점검 H2)
                    if (
                        source == "posting"
                        and _name_shaped(form, dicts.surnames)
                        and (t.tag == "NNP" or t.id in oov)
                    ):
                        dropped["name_shaped"] += 1
                        continue
                    if t.tag == "NNP" and t.id not in oov:
                        proper[form] = proper.get(form, 0) + 1
                        continue
                    vocab.add(form)
                elif t.tag == "SL" and _LATIN.fullmatch(form):
                    w = form.casefold()
                    if w in known_companies:
                        dropped["company"] += 1
                        continue
                    if _DOMAIN.search(w):
                        dropped["domain"] += 1
                        continue
                    vocab.add(w)
    # Kiwi 사전 고유명사는 드물면 뺀다 (브랜드·지명·인명, code review M3). 10번 이상 나오면 Kiwi 가 고유명사로 잘못 붙인
    # 기술어로 보고 둔다 ("클라우드", "트래픽", "펌웨어", "사용성").
    frequent = {w for w, n in proper.items() if n >= _PROPER_MIN}
    dropped["rare_proper"] = len(set(proper) - frequent - vocab)
    vocab |= frequent
    texts = postings + ncs
    rare_given = {
        w
        for w in vocab
        if len(w) == 2 and all(ch in GIVEN_SYLLABLES for ch in w) and counts[w] < _RARE
    }
    vocab -= rare_given
    dropped["rare_given_like"] = len(rare_given)
    stats = {"texts": len(texts), "words": len(vocab), **dropped}
    stats["ontology_version"] = version[0] if version else "unknown"  # type: ignore[assignment]
    stats["kiwi"] = kiwi_version()  # type: ignore[assignment]
    return sorted(vocab), stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sqlite", type=Path, required=True, help="온톨로지 산출물 ontology.sqlite (읽기 전용)"
    )
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    words, stats = build(a.sqlite)
    header = (
        "# 공개 어휘 — 공개 채용 공고 문장·NCS 수행준거에 나오는 한글 명사·영문 단어 (지원자 데이터 아님).\n"
        "# 약한 문맥(경력 머리말 첫 칸·이름 라벨 옆 열 이름·과정 이름·따옴표 도구 이름)에서 잡힌 값의 전파 범위를 정한다.\n"
        "# 회사 이름, 사전에 있는 고유명사, 사람 이름 모양 단어, 드문 이름 모양 두 음절 단어, 도메인 모양 영문은 뺐다.\n"
        f"# 생성: eval/pseudonymize/build_public_vocab.py — 온톨로지 {stats['ontology_version']}, Kiwi {stats['kiwi']}, "
        f"문장 {stats['texts']}건, 단어 {stats['words']}개\n"
        f"# 제외: 회사 {stats.get('company', 0)}회, 드문 사전 고유명사 {stats.get('rare_proper', 0)}개, "
        f"공고의 이름 모양 {stats.get('name_shaped', 0)}회, 드문 이름 모양 두 음절 {stats.get('rare_given_like', 0)}개, "
        f"도메인 {stats.get('domain', 0)}회\n"
    )
    lines = [" ".join(words[i : i + 20]) for i in range(0, len(words), 20)]
    a.out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    print(stats)


if __name__ == "__main__":
    main()
