# retrieve T1 gold — 판정 규칙

`claims_gold_v1.jsonl`: 가상 지원자 주장 41건 (지원자 데이터 아님). dev = 홀수 id, test = 짝수 id.
필드: `id, sub, kind, action, quote, gold, accept, note`.

## 규칙 [확정: 2026-11-02 — 사용자 검토]

1. **gold 는 역량 정의(라벨·ESCO·NCS)를 따른다.** 공고 예문의 문체(도구명 위주)를 따르지 않는다.
   이 세트는 공고 문체와 다른 표현을 시험하는 것이 목적이다.
2. **`[]`(정답 없음)은 그 행동을 덮는 역량이 온톨로지에 없을 때만** 쓴다. 정의가 덮으면 `[]` 로 두지 않는다.
3. **accept(허용 답)** 의 뜻은 "반환해도 오답 아님, 안 반환해도 누락 아님"이다. 아래 세 조건을 모두 만족할 때만 넣는다.
   ① 문장 안에 그 역량을 가리키는 구절이 명시돼 있다.
   ② 역량 정의가 그 구절을 덮는다.
   ③ 지원 직군(sub) 범위 안이다.
   "관련 있어 보임"만으로는 넣지 않는다.
4. **gold 가 둘 이상이면(kind=multi) 둘 다 정답**이다. 재현율은 각각 세고, 완전 일치는 둘 다 골라야 한다.
5. kind
   - `normal`
   - `uncovered`: 정답 없음
   - `scope_probe`: 정답이 지원 직군 범위 밖이라 범위 처리를 시험하는 항목
   - `multi`: 정답 둘 이상

## 채점

- 검색: strict R@k(gold 만), lenient R@10(gold ∪ accept), nDCG@10(gold=2, accept=1).
- 선택: precision·lenient_P, recall, exact·lenient_exact, kind 별 통과율.
- 정답 없음 항목은 검색 재현율에서 빼고, 선택의 `uncovered_ok` 로 본다.

## 온톨로지 공백 메모 (gold 를 비틀지 않고 온톨로지 쪽에서 다룰 것)

- data.ml 지원자의 고전 ML(협업 필터링·수요 예측)이 갈 곳이 없다 (g09, g15).
  `ai.ml`(일반 ML 모델링) 역량을 신설하거나 `data.stat_ml` 범위에 data.ml 을 넣어야 한다. 반영되면 g09 gold 를 갱신한다.
- `sw.agile` 라벨이 "애자일 협업 (Jira·Confluence)"이라 도구 쪽으로 읽힌다.
  설명을 "애자일 개발 프로세스 운영·개선(스크럼 이벤트 포함)"으로 보강할 것을 제안한다 (g40).
