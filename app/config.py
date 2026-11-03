import os
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg2://sales:sales@localhost:5432/sales")

    # "ollama" ou "openai"
    llm_provider: str = os.getenv("LLM_PROVIDER", "ollama").lower()
    llm_model: str = os.getenv("LLM_MODEL", "llama3.1")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    # timeouts em segundos
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "300"))
    llm_health_timeout: float = float(os.getenv("LLM_HEALTH_TIMEOUT", "3"))
    llm_warmup: bool = os.getenv("LLM_WARMUP", "true").lower() in ("1", "true", "yes")
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

    # vazio = data da última venda (os dados são de 2025; com a data de hoje "última semana" viria vazia)
    reference_date: datetime | None = field(default_factory=lambda: _parse_date(os.getenv("REFERENCE_DATE")))

    max_question_length: int = int(os.getenv("MAX_QUESTION_LENGTH", "300"))
    max_rows_to_llm: int = int(os.getenv("MAX_ROWS_TO_LLM", "50"))
    sql_timeout_ms: int = int(os.getenv("SQL_TIMEOUT_MS", "5000"))
    sql_max_retries: int = int(os.getenv("SQL_MAX_RETRIES", "2"))

    # 0 desativa o cache de respostas
    answer_cache_ttl: int = int(os.getenv("ANSWER_CACHE_TTL", "3600"))
    answer_cache_size: int = int(os.getenv("ANSWER_CACHE_SIZE", "256"))


settings = Settings()
