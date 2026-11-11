import type { HttpHandler } from 'msw';

/** 개발 서버·자체 테스트에서 서버 API 를 흉내 낸다 (시작 코드가 계약의 API 를 채운다). 채점은 이 파일을 쓰지 않는다. */
// MSW 2 문법 (1 의 rest·res(ctx…) 는 없다):
//   import { http, HttpResponse } from 'msw';
//   http.get('/api/items', () => HttpResponse.json([{ id: '1' }]))
//   http.patch('/api/items/:id', async ({ params, request }) => HttpResponse.json({ id: params.id, ...(await request.json() as object) }))
//   오류 응답: HttpResponse.json({ code: 'X', message: '…' }, { status: 500 })
//   테스트에서 응답 바꾸기: server.use(http.get('/api/items', () => HttpResponse.json([])))  (src/mocks/server.ts)
export const handlers: HttpHandler[] = [];
