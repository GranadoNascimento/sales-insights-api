import re
import unicodedata

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.config import settings

ALLOWED_TABLES = {"sales", "products", "customers"}

# Filtro rápido de escopo antes de gastar uma chamada ao LLM.
# Se algo passar, o LLM ainda pode responder FORA_DO_ESCOPO.
DOMAIN_TERMS = (
    # entidades e métricas
    "venda",
    "vend",
    "produto",
    "cliente",
    "compra",
    "compr",
    "gast",
    "receita",
    "faturamento",
    "fatur",
    "quantidade",
    "unidade",
    "categoria",
    "sku",
    "preco",
    "valor",
    "ticket",
    "pedido",
    "total",
    "item",
    "itens",
    "consumidor",
    # rankings e agregações
    "mais vendido",
    "melhor",
    "pior",
    "maior",
    "menor",
    "ranking",
    "top",
    "media",
    "soma",
    # períodos
    "dia",
    "semana",
    "mes",
    "trimestre",
    "ano",
    "periodo",
    "hoje",
    "ontem",
    "janeiro",
    "fevereiro",
    "marco",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
    # inglês
    "sales",
    "sale",
    "product",
    "customer",
    "revenue",
    "order",
)

INJECTION_PATTERNS = (
    r"ignore (as|todas|the|all|previous|anteriores)",
    r"esque[cç]a (as|suas|tudo)",
    r"system prompt",
    r"prompt do sistema",
    r"voc[eê] agora [eé]",
    r"act as",
    r"jailbreak",
)

# palavras inteiras, para não barrar "excluindo"
WRITE_INTENT = re.compile(
    r"\b(apague|apagar|apaga|delete|deletar|deleta|exclua|excluir|exclui|remova|remover|remove|"
    r"altere|alterar|altera|atualize|atualizar|atualiza|insira|inserir|insere|modifique|modificar|"
    r"cadastre|cadastrar|zere|zerar|drop|truncate|update|insert)\b"
)

_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "AlterTable",
    "TruncateTable",
    "Command",
    "Into",
    "Lock",
    "Grant",
    "Revoke",
    "Copy",
    "Set",
    "Use",
    "Pragma",
    "Transaction",
    "Commit",
    "Rollback",
)
FORBIDDEN_NODES = tuple(node for node in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES) if node)

FORBIDDEN_FUNCTIONS = {
    "dblink",
    "dblink_exec",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "set_config",
    "current_setting",
    "query_to_xml",
    "version",
    "current_version",
    "current_user",
    "session_user",
    "current_database",
    "current_schema",
    "inet_server_addr",
    "inet_server_port",
    "inet_client_addr",
    "txid_current",
}

SQLGLOT_DIALECTS = {"postgresql": "postgres", "sqlite": "sqlite"}


class GuardrailError(ValueError):
    pass


class InvalidSQLError(ValueError):
    """SQL que não parseia; diferente de GuardrailError, vale pedir correção ao LLM."""


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def validate_question(question: str) -> str:
    question = (question or "").strip()
    if len(question) < 3:
        raise GuardrailError("A pergunta está vazia ou é curta demais.")
    if len(question) > settings.max_question_length:
        raise GuardrailError(f"A pergunta excede o limite de {settings.max_question_length} caracteres.")

    normalized = normalize_text(question)
    if any(re.search(p, normalized) for p in INJECTION_PATTERNS):
        raise GuardrailError("A pergunta contém instruções não permitidas.")
    if WRITE_INTENT.search(normalized):
        raise GuardrailError("Esta API só consulta os dados; não é possível alterar a base.")
    if not any(term in normalized for term in DOMAIN_TERMS):
        raise GuardrailError("Só respondo perguntas sobre os dados de vendas, produtos e clientes desta base.")
    return question


def clean_sql(raw: str) -> str:
    sql = re.sub(r"```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()
    match = re.search(r"\b(with|select)\b", sql, re.IGNORECASE)
    if match:
        sql = sql[match.start() :]
    return sql.strip().rstrip(";").strip()


def _function_name(func: exp.Func) -> str:
    if isinstance(func, exp.Anonymous):
        return str(func.name).lower()
    return func.sql_name().lower()


def validate_sql(sql: str, dialect: str | None = "postgres") -> str:
    """Retorna o SQL com LIMIT. GuardrailError se inseguro, InvalidSQLError se não parseia."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise InvalidSQLError("A consulta está vazia.")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:
        raise InvalidSQLError(f"SQL inválido: {exc}") from exc

    if len(statements) != 1:
        raise GuardrailError("Apenas uma instrução SQL é permitida.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise GuardrailError("Apenas consultas SELECT são permitidas.")

    forbidden = next(iter(tree.find_all(*FORBIDDEN_NODES)), None)
    if forbidden is not None:
        raise GuardrailError("A consulta contém comandos não permitidos.")

    # find_all pega tabelas em JOIN, depois de vírgula e em subconsultas (regex não pegava)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = str(table.name or "").lower()
        schema = str(table.db or "").lower()
        if table.catalog or (schema and schema != "public"):
            raise GuardrailError(f"Esquema não permitido na consulta: {schema or table.catalog}.")
        if not name or (name not in ALLOWED_TABLES and name not in cte_names):
            raise GuardrailError(f"Tabelas não permitidas na consulta: {name or table.sql()}.")

    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name.startswith("pg_") or name in FORBIDDEN_FUNCTIONS:
            raise GuardrailError(f"Função não permitida na consulta: {name}.")

    if not tree.args.get("limit"):
        sql = f"{sql}\nLIMIT {settings.max_rows_to_llm}"
    return sql
