"""Criação do modelo (Ollama ou OpenAI), checagem para o /health e pré-carregamento."""

import json
import logging
import os
import threading
import time
import urllib.request
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache
def get_llm() -> BaseChatModel:
    """Modelo configurado, com temperatura 0 e timeout por chamada."""
    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.llm_model,
            temperature=0,
            request_timeout=settings.llm_timeout,
            max_retries=1,
        )

    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=0,
        keep_alive=settings.ollama_keep_alive,
        client_kwargs={"timeout": settings.llm_timeout},
    )


def _ollama_url(path: str) -> str:
    return f"{settings.ollama_base_url.rstrip('/')}{path}"


def check_llm() -> str:
    """'ok', 'unreachable', 'model_not_found' ou 'missing_api_key'."""
    if settings.llm_provider == "openai":
        # não faz uma chamada paga só para o health check
        return "ok" if os.getenv("OPENAI_API_KEY") else "missing_api_key"

    url = _ollama_url("/api/tags")
    try:
        with urllib.request.urlopen(url, timeout=settings.llm_health_timeout) as resp:
            models = json.load(resp).get("models", [])
    except Exception:
        logger.warning("Ollama não respondeu em %s", url)
        return "unreachable"

    names = {m.get("name", "") for m in models}
    wanted = settings.llm_model
    if any(n == wanted or n.startswith(f"{wanted}:") for n in names):
        return "ok"
    return "model_not_found"


def _wait_for_model(max_wait: float, interval: float) -> bool:
    deadline = time.monotonic() + max_wait
    while True:
        status = check_llm()
        if status == "ok":
            return True
        if time.monotonic() >= deadline:
            logger.warning("Modelo %s não ficou disponível (último status: %s)", settings.llm_model, status)
            return False
        time.sleep(interval)


def _warm_up_ollama(max_wait: float = 3600, interval: float = 10) -> None:
    if not _wait_for_model(max_wait, interval):
        return
    # prompt vazio só carrega o modelo, não gera texto
    payload = json.dumps({"model": settings.llm_model, "keep_alive": settings.ollama_keep_alive}).encode()
    request = urllib.request.Request(
        _ollama_url("/api/generate"), data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout) as resp:
            resp.read()
        logger.info("Modelo %s carregado no Ollama", settings.llm_model)
    except Exception as exc:
        logger.warning("Não foi possível pré-carregar o modelo no Ollama: %s", exc)


def start_warm_up() -> threading.Thread | None:
    # Sem isso a primeira pergunta espera o modelo carregar (minutos em CPU).
    # Roda em thread para não segurar a subida da API; espera o download, se ainda estiver rodando.
    if not settings.llm_warmup or settings.llm_provider != "ollama":
        return None
    thread = threading.Thread(target=_warm_up_ollama, name="llm-warmup", daemon=True)
    thread.start()
    return thread
