# 공개 테스트 작성용 (frontend/react-vite-ts v1)

P1 내부 문서다 (`gen_public` 입력에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

공개 테스트는 **공개 수용 기준(p\*)이 지켜지는지**를 화면으로 확인하는 기능 테스트다. 시작 코드·참조 구현이 이 테스트를 보고 맞춘다(테스트 먼저).
숨은 요구(응답 순서 뒤바뀜·늦은 실패·중복 요청 같은 것)는 시험하지 않는다 — 그건 숨은 테스트 몫이다.

## 케이스 파일

- 위치 `src/__public__/<이름>.test.tsx`. **계약의 공개 테스트(pt\*) 하나 = 파일 하나**, 맨 바깥 `describe` 제목에 pt id(`pt1` …)와 확인할 내용.
- 하네스(`'../__grading__/harness/grader'`)의 `api`·`renderApp`·`textsByRole`·`byTestId`·`flush` 를 쓴다 (숨은 테스트와 같은 하네스).
- **이 과제의 계약 도우미(`'./contract'`)** — 빌드 파이프라인이 공개 계약에서 만든다 (이 문서 끝에 붙은 `contract.ts`, 쓰지 않는다).
  `TID.X`(test id) · `SCREEN.S1`(화면 경로, `renderApp(SCREEN.S1)`) · `API.…` · `routeContract()`(계약 API 를 모두 가로챔) · `waitFor<Api>(match?)`.
  test id·경로를 손으로 쓰지 않는다. `waitFor<Api>()` 가 돌려준 요청의 `respond`·`fail` 은 계약의 응답 타입(`<Api>Ok`·`<Api>Error`)만 받는다 —
  **응답 본문은 이 과제 계약의 모양 그대로** (예시의 본문 모양은 예시 계약의 것).
- 지원자 구현(`src/...`)을 import 하지 않는다 — 공개 계약(화면의 역할·이름·test id·문구, 서버 API 형식)으로만 판정한다.

## 판정 규칙

- **기대 결과는 계약에서만**: 화면 요소·문구는 계약 그대로, 서버 응답 모양은 계약의 API 형식 그대로.
- 응답은 요청 하나에 하나씩, 보낸 순서대로 준다 (`waitForCall` → `respond`). **응답 순서를 뒤바꾸거나, 늦게 실패시키거나, 같은 조작을 빠르게
  반복하지 않는다** — 숨은 요구 영역이다. 판정과 상관없는 API 는 `api.always`.
- 요소는 **계약의 test id 로** 찾는다 (`screen.findByTestId(TID.X)`) — 계약에 없는 접근 가능한 이름을 짐작하지 않는다.
- 기다리는 요청은 계약·공개 요구가 정한 것만. 확인 방법이 구현마다 다를 수 있는 요청(빈 입력에 다시 요청하는지 등)을 말하면
  요청을 기다리지 말고 **화면 결과로만** 확인한다 — 올바른 구현이 "요청이 오지 않음"으로 떨어진다.
- 테스트끼리 서로 독립 — 순서에 기대지 않는다.

## 예시 (모양만)

```tsx
import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { renderApp, textsByRole } from '../__grading__/harness/grader';
import { routeContract, SCREEN, TID, waitForGetApiProductsSuggest } from './contract';

describe('pt1 검색어를 입력하면 추천 목록이 보인다', () => {
  beforeEach(() => routeContract());

  it('응답의 상품명이 순서대로 보인다', async () => {
    const { user } = renderApp(SCREEN.S1);
    await user.type(await screen.findByTestId(TID.SEARCH_INPUT), '노트');
    const call = await waitForGetApiProductsSuggest((c) => (c.query.get('q') ?? '').trim() === '노트');
    await call.respond({ query: '노트', items: [{ id: '1', name: '노트 패드' }], total: 1 });
    await waitFor(() => expect(textsByRole('option')).toEqual(['노트 패드']));
  });
});
```
