-- builder 전용 DB 역할 (#129) — 공통 과제는 읽기만, 빌드 표는 쓰기. 체크포인트(지원자 파생물)·공고·온톨로지 표에는 권한이 없다 (불변식 3).
-- 표 주인(task)으로 실행: podman exec -i task_db psql -U task -d task_db < docker/builder/db-role.sql
-- 비밀번호는 셸 기록에 남지 않게 따로: podman exec -it task_db psql -U task -d task_db -c '\password task_builder'
-- 표·칸은 API(표 주인)가 기동할 때 만든다 — 이 스크립트는 API 를 새 버전으로 한 번 띄운 뒤에 돌린다. 다시 돌려도 된다.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'task_builder') THEN
    CREATE ROLE task_builder LOGIN;
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO task_builder', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO task_builder;
GRANT SELECT ON public.common_task TO task_builder;                                   -- 명세·판·상태 (superseded 확인)
GRANT SELECT, INSERT, UPDATE ON public.common_task_build TO task_builder;             -- 큐·상태·결과 (claim 은 FOR UPDATE)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.starter_file TO task_builder;          -- 오버레이·README (죽은 작업 복구 때 지움)
GRANT SELECT, INSERT ON public.build_artifact TO task_builder;                        -- 응시용·채점용 묶음 등록
