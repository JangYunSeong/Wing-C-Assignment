"""함정 틀 금지 저장 엔티티 대조 (starter/forbidden_storage, 설계 검사 #332 T-F2·B-F7) — 가상 이름만."""

from __future__ import annotations

import pytest

from task_spec.schemas import ForbiddenStorage
from task_spec.starter.forbidden_storage import (
    forbidden_for,
    names_entity,
    storage_hits,
    words,
)

SESSION = ForbiddenStorage(entity="Session", why="가상 이유")


def test_words_split_camel_snake_and_acronyms() -> None:
    assert words("UserSession") == ["user", "session"]
    assert words("user_session") == ["user", "session"]
    assert words("HTTPSession") == ["http", "session"]
    assert words("Session 엔티티") == ["session"]


@pytest.mark.parametrize(
    "name",
    [
        "Session",
        "session",
        "sessions",
        "UserSession",
        "user_session",
        "LoginSessions",
        "user_sessions",
    ],
)
def test_names_that_point_at_the_forbidden_entity(name: str) -> None:
    assert names_entity(name, "Session")


@pytest.mark.parametrize("name", ["SessionToken", "session_token", "Order", "sessionId", "Ses"])
def test_names_that_do_not(name: str) -> None:
    """뒤에 낱말이 붙으면 다른 엔티티일 수 있다 — OrderItem 은 Order 가 아니다."""
    assert not names_entity(name, "Session")


def test_plural_forms_and_multi_word_entities() -> None:
    assert names_entity("request_usages", "RequestUsage")
    assert names_entity("DailyRequestUsage", "RequestUsage")
    assert not names_entity("usage", "RequestUsage")
    assert names_entity("categories", "Category")
    assert names_entity("statuses", "Status")
    assert not names_entity("OrderItem", "Order")


def test_a_frame_entity_written_with_korean_still_matches_its_identifier() -> None:
    """틀이 'Session 엔티티'처럼 써도 영문 식별자로 본다 — 예전 검사 4 는 한글을 남겨 조용히 꺼졌다 (B-F7)."""
    assert names_entity("sessions", "Session 엔티티")
    assert names_entity("세션", "세션") and not names_entity("세션들", "세션")


def test_forbidden_for_and_storage_hits() -> None:
    assert forbidden_for("user_session", [SESSION]) == SESSION
    assert forbidden_for("users", [SESSION]) is None
    sql = "CREATE TABLE user_session (\n  token VARCHAR(64) PRIMARY KEY,\n  user_id BIGINT\n);"
    java = "public interface UserSessionRepository extends JpaRepository<UserSession, String> {}"
    assert [e for e, _ in storage_hits(sql, [SESSION])] == ["Session"]
    assert [e for e, _ in storage_hits(java, [SESSION])] == ["Session"]
    # 공개 계약 칸(sessionId)·발급 값 도우미(newSession)는 저장 꼴이 아니다
    assert storage_hits("record LoginResponse(String sessionId) {}", [SESSION]) == []
    assert storage_hits("protected String newSession(long row) {", [SESSION]) == []
