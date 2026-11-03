import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.guardrails import GuardrailError
from app.insights import SalesInsightsService
from app.llm import check_llm, get_llm, start_warm_up
from app.repository import get_top_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]
LLM = Annotated[BaseChatModel, Depends(get_llm)]
LLMStatus = Annotated[str, Depends(check_llm)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_warm_up()
    yield


app = FastAPI(
    title="Sales Insights API",
    description="Insights de vendas com RAG sobre banco relacional (LangChain + SQLAlchemy).",
    version="1.2.0",
    lifespan=lifespan,
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.middleware("http")
async def request_context(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID", "")
    request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{elapsed:.3f}"
    logger.info(
        "%s %s -> %s em %.2fs [%s]", request.method, request.url.path, response.status_code, elapsed, request_id
    )
    return response


class InsightResponse(BaseModel):
    question: str
    answer: str
    sql: str
    rows: list[dict]
    reference_date: str
    cached: bool = False


class TopProduct(BaseModel):
    rank: int
    product_id: int
    sku: str
    name: str
    category: str | None
    total_quantity: int
    total_revenue: float


class TopProductsResponse(BaseModel):
    period_start: str
    period_end: str
    products: list[TopProduct]


class HealthResponse(BaseModel):
    status: str  # ok | degraded | error
    database: str  # ok | error
    llm: str  # ok | unreachable | model_not_found | missing_api_key
    llm_provider: str
    llm_model: str


@app.get("/health", response_model=HealthResponse)
def health(response: Response, db: DbSession, llm_status: LLMStatus):
    """Banco fora: 503. LLM fora: 200 com status 'degraded' (o /top-products continua funcionando)."""
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        logger.exception("Falha ao conectar no banco")
        database = "error"

    if database != "ok":
        status = "error"
        response.status_code = 503
    elif llm_status != "ok":
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "database": database,
        "llm": llm_status,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
    }


@app.get("/sales-insights", response_model=InsightResponse)
def sales_insights(
    question: Annotated[
        str,
        Query(
            min_length=3,
            max_length=settings.max_question_length,
            examples=["Qual foi o produto mais vendido na última semana?"],
        ),
    ],
    db: DbSession,
    llm: LLM,
):
    """Responde perguntas sobre vendas usando somente dados recuperados do banco."""
    try:
        return SalesInsightsService(llm, db).answer(question)
    except GuardrailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # detalhe só no log, para não expor erro interno
        logger.exception("Falha ao processar a pergunta: %s", question)
        raise HTTPException(
            status_code=502,
            detail="Não foi possível processar a pergunta no momento. Tente novamente mais tarde.",
        ) from exc


@app.get("/top-products", response_model=TopProductsResponse)
def top_products(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365, description="Janela em dias (padrão: último mês).")] = 30,
):
    """Top 5 produtos por quantidade vendida no último mês (30 dias até a data de referência)."""
    return get_top_products(db, days=days, limit=5)
