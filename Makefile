# Atalhos para Linux/macOS (no Windows use testar-docker.ps1 e testar-consistencia.ps1).
API ?= http://localhost:8000
PG_READER ?= postgresql+psycopg2://sales_reader:sales_reader@localhost:5432/sales
PG_ADMIN ?= postgresql+psycopg2://sales:sales@localhost:5432/sales

.PHONY: help up down logs health smoke install test test-postgres lint eval eval-consistencia

help:  ## Lista os comandos
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-18s %s\n", $$1, $$2}'

up:  ## Sobe banco, Ollama (baixa o modelo na 1a vez) e API
	docker compose up -d --build
	@echo "API em $(API) (Swagger em $(API)/docs). Download do modelo: docker compose logs -f ollama-pull"

down:  ## Para os containers (mantém o modelo baixado)
	docker compose down

logs:  ## Acompanha os logs da API
	docker compose logs -f api

health:  ## Mostra o status da API, do banco e do LLM
	@curl -s $(API)/health; echo

smoke:  ## Faz algumas chamadas de exemplo
	@curl -s $(API)/health; echo
	@curl -s $(API)/top-products; echo
	@curl -s -G $(API)/sales-insights --data-urlencode "question=Qual foi o produto mais vendido na última semana?"; echo
	@curl -s -G $(API)/sales-insights --data-urlencode "question=Qual é a capital da França?"; echo

install:  ## Instala as dependências de desenvolvimento
	pip install -r requirements-dev.txt ruff

test:  ## Testes unitários (sem Postgres e sem Ollama)
	pytest -q -m "not postgres"

test-postgres:  ## Testes de integração com o Postgres do docker compose (rode 'make up' antes)
	TEST_POSTGRES_URL="$(PG_READER)" TEST_POSTGRES_ADMIN_URL="$(PG_ADMIN)" pytest -q -m postgres

lint:  ## Lint e formatação
	ruff check . && ruff format --check .

eval:  ## Avalia o LLM com as perguntas de eval/perguntas.json (1 rodada)
	python scripts/avaliar.py --url $(API)

eval-consistencia:  ## Reinicia a API sem cache e avalia 2 rodadas (consistência do LLM)
	ANSWER_CACHE_TTL=0 docker compose up -d --build api
	@until curl -sf $(API)/health > /dev/null; do sleep 2; done
	python scripts/avaliar.py --url $(API) --rodadas 2; status=$$?; docker compose up -d api; exit $$status
