"""Fluxo do /sales-insights: pergunta -> SQL gerado pelo LLM -> consulta -> resposta com base nas linhas."""

import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Lock

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.cache import TTLCache
from app.config import settings
from app.guardrails import (
    ALLOWED_TABLES,
    SQLGLOT_DIALECTS,
    GuardrailError,
    clean_sql,
    normalize_text,
    validate_question,
    validate_sql,
)
from app.models import Base
from app.repository import get_reference_date

logger = logging.getLogger(__name__)

OUT_OF_SCOPE = "FORA_DO_ESCOPO"
NO_DATA_ANSWER = "Não encontrei dados na base para responder a essa pergunta."

SQL_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Você é um especialista em SQL ({dialect}). Gere UMA única consulta SELECT que "
            "responda à pergunta usando somente as tabelas abaixo.\n\n"
            "Esquema:\n{schema}\n\n"
            "Regras:\n"
            "- Retorne apenas o SQL, sem explicações e sem markdown.\n"
            "- Use somente SELECT. Nunca altere dados.\n"
            "- A data atual é TIMESTAMP '{reference_date}'. Use esse literal, sempre com a palavra "
            "TIMESTAMP antes (nunca NOW() nem CURRENT_DATE).\n"
            "- Só filtre por data se a pergunta citar um período (semana, mês, ano, data). "
            "Sem período na pergunta, use todas as vendas, sem WHERE de data.\n"
            "- Períodos relativos são os dias anteriores à data atual. Quando a pergunta citar um "
            "deles, use exatamente estes filtros:\n"
            "  'última semana' / 'últimos 7 dias': s.sale_date > TIMESTAMP '{week_start}' "
            "AND s.sale_date <= TIMESTAMP '{reference_date}'\n"
            "  'último mês' / 'últimos 30 dias': s.sale_date > TIMESTAMP '{month_start}' "
            "AND s.sale_date <= TIMESTAMP '{reference_date}'\n"
            "- O ano atual é {reference_year}. Quando a pergunta citar um mês sem dizer o ano, "
            "filtre também por esse ano usando o número, por exemplo "
            "EXTRACT(YEAR FROM s.sale_date) = {reference_year}; nunca filtre só pelo número do mês "
            "e nunca aplique EXTRACT sobre um texto entre aspas.\n"
            "- 'Mais vendido' e 'unidades' são a soma de quantity; faturamento é a soma de total_amount.\n"
            "- Número de vendas, compras ou pedidos é a contagem de linhas de sales (COUNT(*)), "
            "não a soma de quantity; em rankings desse tipo, ordene por COUNT(*) DESC.\n"
            "- Ao agrupar ou filtrar por mês, devolva o mês como texto no formato 'YYYY-MM' "
            "(no PostgreSQL: TO_CHAR(s.sale_date, 'YYYY-MM') AS mes), nunca só o número do mês.\n"
            "- Traga nomes de produtos e clientes (JOIN), não apenas ids.\n"
            "- Inclua no SELECT os valores calculados (ex.: SUM(s.quantity) AS total_quantity) e a coluna "
            "usada para ordenar (ex.: ORDER BY p.price DESC exige p.price no SELECT), não só os nomes.\n"
            "- Se a pergunta não puder ser respondida com essas tabelas, retorne exatamente "
            f"{OUT_OF_SCOPE}.",
        ),
        ("human", "{question}"),
    ]
)

SQL_RETRY_PROMPT = ChatPromptTemplate.from_messages(
    [
        *SQL_PROMPT.messages,
        (
            "human",
            "A consulta abaixo falhou.\nSQL: {failed_sql}\nErro: {error}\nCorrija e retorne apenas o SQL.",
        ),
    ]
)

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Você é um analista de vendas. Responda à pergunta em português, de forma direta, "
            "usando EXCLUSIVAMENTE os dados do resultado da consulta abaixo. "
            "Não use conhecimento externo e não invente valores. "
            "Sempre cite na resposta os números do resultado (quantidades, totais, valores), "
            "copiando-os exatamente como estão. "
            "Não faça contas: não some, não subtraia e não calcule médias ou totais que não "
            "estejam prontos no resultado. "
            "Escreva meses pelo nome em português, por exemplo 2025-02 = fevereiro de 2025. "
            "Escreva valores monetários só como número, por exemplo 347,94, sem símbolo de "
            "moeda, porque a base não informa a moeda. "
            "Só diga que a base não tem a informação se o resultado não tiver os dados pedidos.\n\n"
            "Data de referência: {reference_date}\n"
            "Consulta executada:\n{sql}\n\n"
            "Resultado (JSON):\n{rows}",
        ),
        ("human", "{question}"),
    ]
)

answer_cache = TTLCache(maxsize=settings.answer_cache_size, ttl=settings.answer_cache_ttl)

_schema_cache: dict[tuple[int, str], str] = {}
_schema_lock = Lock()


def clear_caches() -> None:
    answer_cache.clear()
    with _schema_lock:
        _schema_cache.clear()


_CURRENCY = re.compile(r"(?:R\$|US\$|\$)\s*(?=\d)")


def strip_currency(answer: str) -> str:
    # A base não informa moeda e o Llama às vezes inventa "R$" mesmo com a regra no prompt.
    return _CURRENCY.sub("", answer)


def relative_periods(reference_date: str) -> dict[str, str]:
    # O LLM erra conta com datas, então os intervalos vão prontos (mesma regra do /top-products).
    ref = datetime.fromisoformat(reference_date)
    return {
        "reference_date": ref.isoformat(sep=" "),
        "reference_year": str(ref.year),
        "week_start": (ref - timedelta(days=7)).isoformat(sep=" "),
        "month_start": (ref - timedelta(days=30)).isoformat(sep=" "),
    }


_PERIOD_EXPRESSIONS = (
    (
        re.compile(r"\b(?:(?:n|d)[ao]s?\s+|em\s+)?[uú]ltim[ao]s?\s+(semana|7\s+dias|sete\s+dias)\b", re.IGNORECASE),
        "week_start",
    ),
    (
        re.compile(r"\b(?:(?:n|d)[ao]s?\s+|em\s+)?[uú]ltim[ao]s?\s+(m[eê]s|30\s+dias|trinta\s+dias)\b", re.IGNORECASE),
        "month_start",
    ),
)


def rewrite_relative_periods(question: str, periods: dict[str, str]) -> str:
    # Com a palavra "mês" na pergunta o Llama filtrava o mês de calendário (fevereiro inteiro)
    # mesmo com a regra no prompt. Trocar a expressão pelo intervalo resolveu.
    end = periods["reference_date"]
    for pattern, key in _PERIOD_EXPRESSIONS:
        start = periods[key]
        interval = (
            f"no período de {start} até {end} "
            f"(filtro: s.sale_date > TIMESTAMP '{start}' AND s.sale_date <= TIMESTAMP '{end}')"
        )
        question = pattern.sub(interval, question)
    return question


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _run_readonly(db: Session, sql: str) -> list[dict]:
    """Executa em transação READ ONLY com timeout e lê no máximo MAX_ROWS_TO_LLM linhas."""
    conn = db.connection()
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = {int(settings.sql_timeout_ms)}"))
    try:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result.fetchmany(settings.max_rows_to_llm)]
    finally:
        db.rollback()


def _build_schema_info(db: Session, sample_rows: int) -> str:
    bind = db.get_bind()
    existing = set(inspect(bind).get_table_names())
    parts = []
    for name in sorted(ALLOWED_TABLES & existing):
        table = Base.metadata.tables[name]
        ddl = str(CreateTable(table).compile(dialect=bind.dialect)).strip()
        rows = db.execute(text(f"SELECT * FROM {name} LIMIT {int(sample_rows)}")).fetchall()
        sample = "\n".join(" | ".join(str(v) for v in row) for row in rows)
        cols = " | ".join(c.name for c in table.columns)
        parts.append(f"{ddl}\n/* {sample_rows} linhas de exemplo:\n{cols}\n{sample}\n*/")
    return "\n\n".join(parts)


def get_schema_info(db: Session, sample_rows: int = 2) -> str:
    """DDL das tabelas + linhas de exemplo para o prompt, montado uma vez por engine."""
    bind = db.get_bind()
    key = (id(bind), str(bind.url))
    with _schema_lock:
        cached = _schema_cache.get(key)
    if cached is not None:
        return cached
    schema = _build_schema_info(db, sample_rows)
    with _schema_lock:
        _schema_cache[key] = schema
    return schema


class SalesInsightsService:
    """Responde perguntas sobre vendas usando só os dados do banco."""

    def __init__(self, llm: BaseChatModel, db: Session):
        self.db = db
        to_sql = StrOutputParser() | RunnableLambda(clean_sql)
        self.sql_chain = SQL_PROMPT | llm | to_sql
        self.sql_retry_chain = SQL_RETRY_PROMPT | llm | to_sql
        self.answer_chain = ANSWER_PROMPT | llm | StrOutputParser()

    def answer(self, question: str) -> dict:
        """Valida a pergunta, devolve do cache se puder e, senão, gera a resposta."""
        question = validate_question(question)
        reference_date = get_reference_date(self.db).isoformat(sep=" ")

        cache_key = (
            normalize_text(question).rstrip("?!. "),
            reference_date,
            settings.llm_provider,
            settings.llm_model,
        )
        cached = answer_cache.get(cache_key)
        if cached is not None:
            return {**cached, "question": question, "cached": True}

        result = self._answer_uncached(question, reference_date)
        answer_cache.set(cache_key, result)
        return {**result, "cached": False}

    def _answer_uncached(self, question: str, reference_date: str) -> dict:
        """Gera o SQL, executa (com correções se falhar) e monta a resposta a partir das linhas."""
        dialect_name = self.db.get_bind().dialect.name
        sqlglot_dialect = SQLGLOT_DIALECTS.get(dialect_name)
        periods = relative_periods(reference_date)
        inputs = {
            "question": rewrite_relative_periods(question, periods),
            "schema": get_schema_info(self.db),
            "dialect": dialect_name,
            **periods,
        }

        sql = self.sql_chain.invoke(inputs)
        if OUT_OF_SCOPE in sql.upper():
            raise GuardrailError("A pergunta não pode ser respondida com os dados de vendas.")

        # erro de SQL volta para o LLM corrigir; SQL inseguro não tem segunda chance
        for attempt in range(settings.sql_max_retries + 1):
            try:
                sql = validate_sql(sql, sqlglot_dialect)
                rows = _run_readonly(self.db, sql)
                break
            except GuardrailError:
                raise
            except Exception as exc:
                if attempt == settings.sql_max_retries:
                    raise
                logger.info("SQL gerado falhou (tentativa %d), tentando de novo: %s", attempt + 1, exc)
                sql = self.sql_retry_chain.invoke({**inputs, "failed_sql": sql, "error": str(exc)[:500]})

        if not rows:
            return {
                "question": question,
                "answer": NO_DATA_ANSWER,
                "sql": sql,
                "rows": [],
                "reference_date": reference_date,
            }

        rows_json = json.dumps(rows, default=_json_default, ensure_ascii=False)
        answer = self.answer_chain.invoke(
            {"question": question, "sql": sql, "rows": rows_json, "reference_date": reference_date}
        )
        return {
            "question": question,
            "answer": strip_currency(answer).strip(),
            "sql": sql,
            "rows": json.loads(rows_json),
            "reference_date": reference_date,
        }
