# {{title}}

{{scenario}}

## 요구 사항

{{requirements}}

## 공개 수용 기준

{{acceptance_public}}

## 화면·API 계약

{{contract}}

## 제출물

{{deliverables}}

## 코드 구조

- TypeScript 5.8 · React 18 · Vite 6 · React Router 6 · TanStack Query 5 · Zustand 5 · Vitest 3 + Testing Library · MSW 2
- 앱 코드는 `src/` 아래 (뿌리 `src/App.tsx`). 의존성(`package.json`)·빌드 설정·`src/main.tsx` 는 바꾸지 않는다.
- 서버 API 는 계약의 경로·형식 그대로 `fetch` 로 부른다. 개발 서버에서는 `src/mocks/handlers.ts` 의 가짜 응답이 서버를 대신한다.

## 실행

```bash
npm install
npm run dev        # 개발 서버 (가짜 API 응답으로)
npm test           # 테스트
npm run typecheck  # 타입 검사
```

## 채점 방법

채점 때 이 계약을 기준으로 한 화면 테스트가 추가로 실행된다 (서버 응답은 채점 쪽이 계약 형식으로 준다). 화면 요소의 역할·이름·test id·문구와 API 형식은 바꾸지 않는다.

## 진행 방식

{{process}}
