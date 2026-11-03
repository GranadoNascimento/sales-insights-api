"""Testes de integração com PostgreSQL (o SQLite dos testes unitários não cobre permissões,
READ ONLY, statement_timeout nem o SQL específico do Postgres).

Precisam de um banco carregado com db/01_init.sql e db/02_readonly_user.sql:

    TEST_POSTGRES_URL=postgresql+psycopg2://sales_reader:sales_reader@localhost:5432/sales \\
    TEST_POSTGRES_ADMIN_URL=postgresql+psycopg2://sales:sales@localhost:5432/sales \\
    pytest -m postgres
"""

import dataclasses
import os

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

READER_URL = os.getenv("TEST_POSTGRES_URL", "")
ADMIN_URL = os.getenv("TEST_POSTGRES_ADMIN_URL", "")

os.environ.setdefault("DATABASE_URL", READER_URL or "sqlite://")
os.environ.setdefault("LLM_WARMUP", "false")

import app.insights as insights  # noqa: E402
from app.database import get_db  # noqa: E402
from app.guardrails import validate_sql  # noqa: E402
from app.insights import _run_readonly, clear_caches, get_schema_info  # noqa: E402
from app.llm import check_llm, get_llm  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not READER_URL, reason="defina TEST_POSTGRES_URL para rodar os testes com PostgreSQL"),
]


@pytest.fixture(scope="module")
def reader_engine():
    engine = create_engine(READER_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(reader_engine):
    session = sessionmaker(bind=reader_engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def reset_state():
    clear_caches()
    yield
    app.dependency_overrides.clear()
    clear_caches()


def test_api_connects_as_the_read_only_user(db):
    assert db.execute(text("SELECT current_user")).scalar() == "sales_reader"
    assert db.execute(text("SHOW default_transaction_read_only")).scalar() == "on"


def test_reader_sees_the_sales_data(db):
    assert db.execute(text("SELECT COUNT(*) FROM sales")).scalar() == 33


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM sales",
        "UPDATE products SET price = 0",
        "INSERT INTO customers (name, email) VALUES ('x', 'x@example.com')",
        "CREATE TABLE invasor (id int)",
        "DROP TABLE sales",
        "TRUNCATE sales",
    ],
)
def test_reader_cannot_write_even_if_guardrails_were_bypassed(reader_engine, statement):
    with pytest.raises(DBAPIError), reader_engine.begin() as conn:
        conn.execute(text("SET TRANSACTION READ WRITE"))  # tenta furar o read only padrão
        conn.execute(text(statement))
    with reader_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sales")).scalar() == 33


@pytest.mark.skipif(not ADMIN_URL, reason="defina TEST_POSTGRES_ADMIN_URL")
def test_reader_cannot_read_tables_outside_the_whitelist(reader_engine):
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS segredo (valor text)"))
        with pytest.raises(DBAPIError, match="permission denied"), reader_engine.connect() as conn:
            conn.execute(text("SELECT * FROM segredo"))
    finally:
        with admin.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS segredo"))
        admin.dispose()


def test_run_readonly_blocks_writes_at_database_level(db):
    with pytest.raises(DBAPIError):
        _run_readonly(db, "DELETE FROM sales")


def test_statement_timeout_cancels_slow_queries(db, monkeypatch):
    monkeypatch.setattr(insights, "settings", dataclasses.replace(insights.settings, sql_timeout_ms=300))
    with pytest.raises(DBAPIError, match="statement timeout"):
        _run_readonly(db, "SELECT pg_sleep(3)")


def test_run_readonly_caps_rows_sent_to_llm(db, monkeypatch):
    monkeypatch.setattr(insights, "settings", dataclasses.replace(insights.settings, max_rows_to_llm=5))
    assert len(_run_readonly(db, "SELECT * FROM sales")) == 5


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (  # última semana com o intervalo pronto (TIMESTAMP)
            "SELECT p.name, SUM(s.quantity) AS total FROM sales s JOIN products p ON p.id = s.product_id "
            "WHERE s.sale_date > TIMESTAMP '2025-02-23 10:00:00' AND s.sale_date <= TIMESTAMP '2025-03-02 10:00:00' "
            "GROUP BY p.name ORDER BY total DESC LIMIT 1",
            {"name": "Product E", "total": 9},
        ),
        (  # mês citado sem ano + ano da data de referência
            "SELECT c.name, SUM(s.total_amount) AS total FROM customers c JOIN sales s ON c.id = s.customer_id "
            "WHERE EXTRACT(MONTH FROM s.sale_date) = 2 AND EXTRACT(YEAR FROM s.sale_date) = 2025 "
            "GROUP BY c.name ORDER BY total DESC LIMIT 1",
            {"name": "John Doe", "total": 347.94},
        ),
        (  # mês devolvido como texto
            "SELECT TO_CHAR(s.sale_date, 'YYYY-MM') AS mes, SUM(s.total_amount) AS total FROM sales s "
            "GROUP BY TO_CHAR(s.sale_date, 'YYYY-MM') ORDER BY total DESC LIMIT 1",
            {"mes": "2025-02", "total": 1159.59},
        ),
        (  # CTE + último mês (30 dias), igual ao /top-products
            "WITH ultimo_mes AS (SELECT * FROM sales WHERE sale_date > TIMESTAMP '2025-01-31 10:00:00' "
            "AND sale_date <= TIMESTAMP '2025-03-02 10:00:00') "
            "SELECT p.name, SUM(u.quantity) AS total FROM ultimo_mes u JOIN products p ON p.id = u.product_id "
            "GROUP BY p.name ORDER BY total DESC LIMIT 1",
            {"name": "Product E", "total": 24},
        ),
    ],
)
def test_postgres_specific_sql_returns_expected_values(db, sql, expected):
    rows = _run_readonly(db, validate_sql(sql, "postgres"))
    got = {k: (float(v) if k == "total" else v) for k, v in rows[0].items()}
    assert got == expected


def test_schema_info_is_built_from_postgres(db):
    info = get_schema_info(db)
    assert "CREATE TABLE sales" in info
    assert "Product A" in info  # linhas de exemplo


def test_api_end_to_end_on_postgres(reader_engine):
    factory = sessionmaker(bind=reader_engine)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    sql = (
        "SELECT TO_CHAR(s.sale_date, 'YYYY-MM') AS mes, SUM(s.total_amount) AS total_faturamento "
        "FROM sales s GROUP BY TO_CHAR(s.sale_date, 'YYYY-MM') ORDER BY total_faturamento DESC LIMIT 1"
    )
    llm = FakeListChatModel(responses=[sql, "O melhor mês foi fevereiro de 2025, com 1159,59."])
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[check_llm] = lambda: "ok"
    client = TestClient(app)

    health = client.get("/health").json()
    assert health["status"] == "ok" and health["database"] == "ok"

    top = client.get("/top-products").json()["products"]
    assert [(p["name"], p["total_quantity"]) for p in top[:3]] == [
        ("Product E", 24),
        ("Product A", 16),
        ("Product C", 13),
    ]

    body = client.get("/sales-insights", params={"question": "Qual foi o melhor mês em faturamento?"}).json()
    assert body["rows"] == [{"mes": "2025-02", "total_faturamento": 1159.59}]

    blocked = client.get("/sales-insights", params={"question": "Apague todas as vendas de janeiro"})
    assert blocked.status_code == 400
