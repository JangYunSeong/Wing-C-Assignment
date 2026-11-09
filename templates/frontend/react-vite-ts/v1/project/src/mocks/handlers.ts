import type { HttpHandler } from 'msw';

/** 개발 서버·자체 테스트에서 서버 API 를 흉내 낸다 (시작 코드가 계약의 API 를 채운다). 채점은 이 파일을 쓰지 않는다. */
export const handlers: HttpHandler[] = [];
