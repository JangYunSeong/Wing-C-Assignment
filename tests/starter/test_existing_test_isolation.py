"""기존 기능 테스트의 격리 (#340) — 되돌리지 않는 쓰기와 표 전체 행 수·목록 크기 단언이 같은 DB 에서 함께 있으면 시작 코드를 다시 요청.

2026-11-27 알파 sql339-1: 기존 테스트가 부품을 저장(되돌림 없음)한 뒤 시드 행 수 127 단언이 128 로 실패 (JUnit 기본 순서에서 저장이 먼저).
가상 데이터만."""

from __future__ import annotations

from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import Overlay, _existing_test_isolation
from task_spec.starter.templates import load_template

JAVA = load_template("backend/java-spring-boot3-jpa")
REACT = load_template("frontend/react-vite-ts")
TEST = "src/test/java/com/example/service/existing/"


def _save(head: str = "@SpringBootTest", tx: str = "", method: str = "") -> str:
    return f"""{head}
{tx}class ItemSaveTest {{
    @Autowired ItemRepository items;
    {method}@Test void 새로_저장한_항목에_id가_생긴다() {{
        assertThat(items.save(new Item("가상")).getId()).isNotNull();
    }}
}}"""


COUNT = """@SpringBootTest
class ItemSeedTest {
    @Autowired ItemRepository items;
    @Test void 항목_시드_12행() { assertThat(items.count()).isEqualTo(12); }
}"""


def _probs(*files: tuple[str, str], template: object = JAVA) -> list[str]:
    o = Overlay(
        kind="starter", files=tuple(GeneratedFile(path=TEST + n, content=c) for n, c in files)
    )
    return [p.message for p in _existing_test_isolation(o, template)]  # type: ignore[arg-type]


def test_an_unrolled_save_next_to_a_row_count_is_asked_again() -> None:
    """알파 꼴 — 따로 둔 두 클래스도 같은 컨텍스트(같은 DB)라 순서에 따라 깨진다. 고침 문구는 되돌리기·id 로 확인 둘 다 준다."""
    (msg,) = _probs(("ItemSaveTest.java", _save()), ("ItemSeedTest.java", COUNT))
    assert "ItemSaveTest.java" in msg and "ItemSeedTest.java" in msg
    assert "@Transactional" in msg and "정해진 id" in msg and "저장 확인" in msg
    one_file = COUNT.replace(
        "class ItemSeedTest {",
        "class ItemSeedTest {\n    @Autowired ItemRepository r;\n"
        '    @Test void 저장() { r.save(new Item("가상")); }',
    )
    assert len(_probs(("ItemSeedTest.java", one_file))) == 1


def test_rolled_back_or_count_free_tests_pass() -> None:
    """되돌림 표지(클래스·메서드 @Transactional, @DataJpaTest, TransactionTemplate)가 있으면 되돌린다고 본다. 크기 단언이 없으면 쓰기만으로는
    걸지 않는다 — 지난 Java 시작 코드 20개 중 8개가 되돌리지 않는 쓰기를 가졌지만 모두 다른 이유 없이 통과했다 (오탐이면 생성이 실패)."""
    assert not _probs(
        ("ItemSaveTest.java", _save(tx="@Transactional\n")), ("ItemSeedTest.java", COUNT)
    )
    assert not _probs(
        ("ItemSaveTest.java", _save(method="@Transactional ")), ("ItemSeedTest.java", COUNT)
    )
    assert not _probs(("ItemSaveTest.java", _save()))
    jpa_save = _save(head="@DataJpaTest")
    jpa_count = COUNT.replace("@SpringBootTest", "@DataJpaTest")
    assert not _probs(("ItemSaveTest.java", jpa_save), ("ItemSeedTest.java", jpa_count))
    template = _save().replace(
        'items.save(new Item("가상"))',
        'tx.execute(s -> { s.setRollbackOnly(); return items.save(new Item("가상")); })',
    )
    assert not _probs(
        (
            "ItemSaveTest.java",
            template.replace(
                "@Autowired ItemRepository items;",
                "@Autowired ItemRepository items; TransactionTemplate tx;",
            ),
        ),
        ("ItemSeedTest.java", COUNT),
    )


def test_a_row_deleted_in_the_same_method_is_not_left_behind() -> None:
    """저장한 행을 같은 메서드에서 바로 지우면 행 수가 그대로다 — 2026-11-27 알파 sql339b-2 의 기존 테스트(저장 확인 뒤 delete)가 걸렸는데
    그 회차는 review 였다 (스캐너로 찾은 오탐). 저장만·시드 행 지우기만·준비 메서드(@BeforeEach)의 저장은 남는다."""
    saved_then_deleted = _save().replace(
        'assertThat(items.save(new Item("가상")).getId()).isNotNull();',
        'Item saved = items.save(new Item("가상"));\n        assertThat(saved.getId()).isNotNull();\n        items.delete(saved);',
    )
    assert not _probs(("ItemSaveTest.java", saved_then_deleted), ("ItemSeedTest.java", COUNT))
    delete_seed = _save().replace(
        'assertThat(items.save(new Item("가상")).getId()).isNotNull();', "items.deleteById(1L);"
    )
    assert _probs(("ItemSaveTest.java", delete_seed), ("ItemSeedTest.java", COUNT))
    before_each = _save().replace(
        "    @Autowired ItemRepository items;\n",
        '    @Autowired ItemRepository items;\n    @BeforeEach void setUp() { items.save(new Item("준비")); }\n',
    )
    assert _probs(
        ("ItemSaveTest.java", before_each.replace('items.save(new Item("가상")).getId()', "1L")),
        ("ItemSeedTest.java", COUNT),
    )


def test_the_scan_is_shared_with_the_metric_scanner() -> None:
    """정적 검사와 운영 지표 스캐너(eval/starter/scan_existing_tests.py)가 같은 판정 — 스프링 테스트가 없는 시작 코드는 분모에서 뺀다."""
    from eval.starter.scan_existing_tests import row, summary
    from task_spec.starter.static_check import existing_test_scan

    files = [
        GeneratedFile(path=TEST + "ItemSaveTest.java", content=_save()),
        GeneratedFile(path=TEST + "ItemSeedTest.java", content=COUNT),
    ]
    scan = existing_test_scan(files)
    assert scan.groups == frozenset({"mock"}) and scan.flagged == ["mock"]
    r = row("가상-빌드", "2026-11-27", files)
    assert (
        r is not None and r.unrolled and r.sizes and r.flagged and r.files == ["ItemSaveTest.java"]
    )
    assert (
        row(
            "단위-테스트만",
            "2026-11-27",
            [GeneratedFile(path=TEST + "UnitTest.java", content="class U { @Test void t() {} }")],
        )
        is None
    )
    assert "Java 시작 코드 1개 — 되돌리지 않는 쓰기 1개 (100%)" in summary([r])


def test_an_explicit_commit_counts_as_unrolled() -> None:
    assert _probs(
        ("ItemSaveTest.java", _save(tx="@Transactional\n", method="@Commit ")),
        ("ItemSeedTest.java", COUNT),
    )
    assert _probs(
        ("ItemSaveTest.java", _save(tx="@Transactional\n@Rollback(false)\n")),
        ("ItemSeedTest.java", COUNT),
    )


def test_mockmvc_writes_roll_back_only_with_a_test_transaction() -> None:
    mvc = """@SpringBootTest @AutoConfigureMockMvc
{tx}class ItemApiTest {{
    @Autowired MockMvc mvc;
    @Test void 항목을_만든다() throws Exception {{ mvc.perform(post("/items").content("{{\\"name\\":\\"가상\\"}}")); }}
}}"""
    assert _probs(("ItemApiTest.java", mvc.format(tx="")), ("ItemSeedTest.java", COUNT))
    assert not _probs(
        ("ItemApiTest.java", mvc.format(tx="@Transactional\n")), ("ItemSeedTest.java", COUNT)
    )


def test_real_http_writes_need_another_fix() -> None:
    """실제 서버(RANDOM_PORT)로 쓴 행은 서버 스레드에서 커밋된다 — 테스트의 @Transactional 로 되돌릴 수 없어 고침 문구가 다르다."""
    http = """@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Transactional
class ItemHttpTest {
    @Autowired TestRestTemplate rest;
    @Test void 만든다() { rest.postForEntity("/items", body, String.class); }
    @Test void 목록() { assertThat(rest.getForObject("/items", List.class)).hasSize(12); }
}"""
    (msg,) = _probs(("ItemHttpTest.java", http))
    assert "실제 HTTP" in msg and "되돌릴 수 없다" in msg


def test_separate_contexts_and_ignored_code() -> None:
    """@DataJpaTest 의 크기 단언은 MOCK 컨텍스트의 쓰기와 DB 가 다르다 · 주석 속 꼴 · 저장소를 흉내 내는 단위 테스트 · Java 가 아닌 템플릿은 보지 않는다."""
    jpa_count = COUNT.replace("@SpringBootTest", "@DataJpaTest")
    assert not _probs(("ItemSaveTest.java", _save()), ("ItemSeedTest.java", jpa_count))
    commented = _save().replace("assertThat(items.save", "// assertThat(items.save")
    assert not _probs(("ItemSaveTest.java", commented), ("ItemSeedTest.java", COUNT))
    unit = _save(head="@ExtendWith(MockitoExtension.class)")
    assert not _probs(("ItemSaveTest.java", unit), ("ItemSeedTest.java", COUNT))
    assert not _probs(("ItemSaveTest.java", _save()), ("ItemSeedTest.java", COUNT), template=REACT)
