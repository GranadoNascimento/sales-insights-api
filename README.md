# Sales Insights API

API em FastAPI que responde perguntas sobre uma base de vendas em PostgreSQL usando linguagem natural. O LLM (Llama 3.1 via Ollama por padrão, ou OpenAI) gera o SQL, a API valida e executa a consulta, e o LLM responde usando só as linhas que voltaram do banco. Toda resposta traz o SQL e as linhas usadas, para dar para conferir.

Além dos endpoints pedidos, o projeto tem validação do SQL com sqlglot, usuário somente leitura no banco, 94 testes (76 unitários e 18 contra um Postgres real), CI no GitHub Actions e uma avaliação do LLM com perguntas de referência. Arquitetura, segurança, decisões técnicas e limitações estão em [docs/DETALHES.md](docs/DETALHES.md).

## Início rápido

Pré-requisitos: Docker (com Docker Compose), ~8 GB de RAM livres e ~6 GB de disco para o modelo.

```bash
docker compose up -d --build
```

Isso sobe o PostgreSQL (já com os dados), o Ollama, baixa o Llama 3.1 automaticamente (~4,7 GB, só na primeira vez) e a API em http://localhost:8000 (Swagger em /docs).

- `/top-products` funciona assim que a API sobe; `/sales-insights` precisa do modelo baixado.
- Acompanhe o download com `docker compose logs -f ollama-pull`. Enquanto ele não termina, `/health` mostra `"llm": "model_not_found"`; ao final, `"status": "ok"`.
- Depois do download, a API carrega o modelo na memória sozinha. Em CPU, cada pergunta nova leva de 20 a 60 segundos; perguntas repetidas voltam do cache na hora.

```bash
curl -G "http://localhost:8000/sales-insights" --data-urlencode "question=Qual foi o produto mais vendido na última semana?"
```

Atalhos: `make help` (Linux/macOS) ou `testar-docker.ps1` (Windows). Sem Docker ou com OpenAI, veja [Como rodar](#como-rodar).

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| GET | `/sales-insights?question=...` | Responde perguntas sobre vendas usando dados recuperados do banco |
| GET | `/top-products?days=30` | Top 5 produtos mais vendidos (por quantidade) no último mês: os 30 dias até a data de referência |
| GET | `/health` | Verifica o banco e o LLM (Ollama acessível e modelo baixado) |
| GET | `/docs` | Documentação interativa (Swagger) |

Sobre "último mês" e a data de referência: os dados do script são de janeiro a março de 2025, então a API usa como "hoje" a data da última venda (2025-03-02 10:00). "Último mês" são os 30 dias até essa data (31/01 a 02/03) e "última semana" os 7 dias até ela, tanto no `/top-products` quanto no `/sales-insights`. A janela muda com `?days=` (ex.: `/top-products?days=7`) e a data com a variável `REFERENCE_DATE`.

Exemplo de resposta de `/sales-insights?question=Qual foi o produto mais vendido na última semana?`:

```json
{
  "question": "Qual foi o produto mais vendido na última semana?",
  "answer": "O produto mais vendido na última semana foi o Product E, com um total de 9 unidades vendidas.",
  "sql": "SELECT p.name, SUM(s.quantity) AS total_quantity FROM sales s JOIN products p ON s.product_id = p.id WHERE s.sale_date > TIMESTAMP '2025-02-23 10:00:00' AND s.sale_date <= TIMESTAMP '2025-03-02 10:00:00' GROUP BY p.name ORDER BY total_quantity DESC LIMIT 1",
  "rows": [{"name": "Product E", "total_quantity": 9}],
  "reference_date": "2025-03-02 10:00:00",
  "cached": false
}
```

Códigos de resposta do `/sales-insights`:

| Código | Quando |
|---|---|
| 200 | Resposta gerada a partir dos dados (ou resposta fixa "não encontrei dados" quando a consulta volta vazia) |
| 400 | Pergunta fora do escopo, tentativa de prompt injection, pedido para alterar a base ou SQL gerado inseguro |
| 422 | Parâmetro inválido (pergunta vazia ou longa demais) |
| 502 | Falha do LLM ou SQL que não pôde ser corrigido; mensagem genérica, detalhe só no log |

## Como funciona

1. A pergunta passa por um filtro: fora do escopo de vendas, tentativa de prompt injection ou pedido para alterar a base voltam 400 sem chamar o LLM.
2. O LLM recebe o esquema das tabelas (com linhas de exemplo) e gera um SELECT. "Última semana" e "último mês" chegam a ele já convertidos em datas.
3. O SQL é validado com sqlglot (só leitura, só as tabelas `sales`, `products` e `customers`, com LIMIT) e executado com um usuário somente leitura, em transação `READ ONLY` e com timeout. Se der erro, o LLM recebe o erro e corrige (até 2 vezes).
4. Sem linhas no resultado, a resposta é fixa ("não encontrei dados") e o LLM não é chamado.
5. Com linhas, o LLM escreve a resposta usando só essas linhas.

## Como o uso de RAG é forçado

O modelo nunca responde só com o próprio conhecimento:

- Escopo: perguntas fora do domínio de vendas e pedidos para alterar a base ("apague", "atualize", "insira"...) são bloqueados antes de chamar o LLM (HTTP 400). O próprio LLM também pode marcar a pergunta como `FORA_DO_ESCOPO`.
- Contexto obrigatório: a resposta só é gerada a partir das linhas retornadas pela consulta. Se a consulta não retorna dados, a API devolve uma resposta fixa, sem chamar o LLM.
- Prompt restritivo: o prompt de resposta proíbe conhecimento externo, valores inventados, contas feitas pelo modelo (somas e médias que não vieram do banco) e símbolos de moeda que a base não informa.

## Como rodar

### Opção 1 – Docker Compose (recomendado)

```bash
docker compose up -d --build
```

Serviços:

- `db`: PostgreSQL 16, já com `db/01_init.sql` (esquema e dados) e `db/02_readonly_user.sql` (usuário somente leitura) carregados;
- `ollama`: servidor do modelo;
- `ollama-pull`: baixa o modelo uma vez e termina (nas próximas subidas é instantâneo);
- `api`: a aplicação, rodando como usuário sem privilégios.

> Se o container do banco já existia de antes, os scripts de `db/` não rodam de novo. Crie o usuário somente leitura com:
> `docker compose exec -T db psql -U sales -d sales -f /docker-entrypoint-initdb.d/02_readonly_user.sql`

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs

No Windows, `testar-docker.ps1` faz tudo isso, testa os endpoints e grava o resultado em `resultado-docker.txt`:

```powershell
powershell -ExecutionPolicy Bypass -File .\testar-docker.ps1
```

No Linux/macOS, o `Makefile` tem os mesmos atalhos (`make help` lista todos):

```bash
make up              # sobe tudo
make smoke           # chamadas de exemplo
make test            # testes unitários
make test-postgres   # testes de integração com o Postgres do compose
make eval            # avaliação do LLM com as perguntas de referência
```

Para usar OpenAI em vez do Ollama (sem baixar o modelo local):

```bash
LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=sk-... docker compose up -d --build db api
```

### Opção 2 – Local

Pré-requisitos: Python 3.11+, PostgreSQL e [Ollama](https://ollama.com).

```bash
# 1. Banco
createdb sales
psql -d sales -f db/01_init.sql
psql -d sales -f db/02_readonly_user.sql   # usuário somente leitura (recomendado)

# 2. LLM
ollama pull llama3.1

# 3. Aplicação
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env          # ajuste DATABASE_URL se necessário
uvicorn app.main:app --reload
```

### Exemplos

```bash
curl "http://localhost:8000/health"
curl "http://localhost:8000/top-products"
curl -G "http://localhost:8000/sales-insights" --data-urlencode "question=Qual foi o produto mais vendido na última semana?"
curl -G "http://localhost:8000/sales-insights" --data-urlencode "question=Qual cliente mais gastou em fevereiro?"
curl -G "http://localhost:8000/sales-insights" --data-urlencode "question=Qual é a capital da França?"   # bloqueada (400)
```

## Testes

```bash
pip install -r requirements-dev.txt
pytest -q                                # 76 testes unitários (SQLite em memória + LLM falso)
ruff check . && ruff format --check .    # lint e formatação (pip install ruff)
```

Os 18 testes de integração (`tests/test_postgres.py`) rodam contra um PostgreSQL carregado com os dois scripts de `db/` (com o compose no ar, `make test-postgres`):

```bash
TEST_POSTGRES_URL=postgresql+psycopg2://sales_reader:sales_reader@localhost:5432/sales \
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg2://sales:sales@localhost:5432/sales \
pytest -q -m postgres
```

O CI (`.github/workflows/ci.yml`) roda lint, testes unitários, testes de integração com PostgreSQL 16 e o build da imagem Docker a cada push. O que cada teste cobre está em [docs/DETALHES.md](docs/DETALHES.md#testes).

## Avaliação do LLM

Os testes usam um LLM falso. Para medir o LLM de verdade, `eval/perguntas.json` tem 34 perguntas com os valores esperados calculados no próprio script do banco: 14 usadas para calibrar o prompt e 20 inéditas, rodadas primeiro sem nenhum ajuste.

| Llama 3.1 8B (Ollama, CPU) | Ajuste (14) | Inéditas (20) | Total |
|---|---|---|---|
| Primeira rodada com as inéditas | 14/14 | 18/20 | 32/34 |
| Rodada final | 13/14 | 20/20 | 33/34 |

```bash
python scripts/avaliar.py --url http://localhost:8000   # ou: make eval / testar-consistencia.ps1
```

Os erros, o que foi ajustado e os tempos medidos estão em [docs/DETALHES.md](docs/DETALHES.md#avaliação-do-llm).

## Estrutura

```
app/
  main.py         # endpoints FastAPI, middleware de X-Request-ID e aquecimento do LLM
  config.py       # configurações via variáveis de ambiente
  database.py     # engine e sessão SQLAlchemy
  models.py       # modelos ORM (products, customers, sales)
  repository.py   # consultas determinísticas (top produtos, data de referência)
  insights.py     # pipeline: gera o SQL, executa e monta a resposta (+ caches e períodos)
  guardrails.py   # validação da pergunta e do SQL (sqlglot)
  llm.py          # fábrica do LLM (Ollama ou OpenAI), verificação para o /health e aquecimento
  cache.py        # cache em memória com TTL
db/
  01_init.sql             # script do banco fornecido no teste
  02_readonly_user.sql    # usuário somente leitura usado pela API
tests/
  test_api.py             # testes unitários (SQLite + LLM falso)
  test_postgres.py        # testes de integração com PostgreSQL
eval/perguntas.json       # perguntas de referência para avaliar o LLM
scripts/avaliar.py        # avaliação automática do LLM (qualquer sistema)
.github/workflows/ci.yml  # lint, testes unitários, integração com Postgres e build Docker
requirements.txt          # dependências de produção
requirements-dev.txt      # + pytest e httpx
pyproject.toml            # configuração do pytest e do ruff
docs/DETALHES.md          # arquitetura, segurança, decisões, avaliação e configuração
testar-docker.ps1         # teste ponta a ponta no Docker (Windows)
testar-consistencia.ps1   # avaliação do LLM no Docker com placar (Windows)
Makefile                  # atalhos para Linux/macOS (make help)
```
