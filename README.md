# Sales Insights API

API em FastAPI que responde perguntas sobre uma base de vendas em PostgreSQL usando linguagem natural. O LLM (Llama 3.1 via Ollama por padrão, ou OpenAI) gera o SQL, a API valida e executa a consulta, e o LLM responde usando só as linhas que voltaram do banco. Toda resposta traz o SQL e as linhas usadas, para dar para conferir.

Além dos endpoints pedidos, o projeto tem validação do SQL com sqlglot, usuário somente leitura no banco, 94 testes (76 unitários e 18 contra um Postgres real), CI no GitHub Actions e uma avaliação do LLM com perguntas de referência (resultados em [Avaliação do LLM](#avaliação-do-llm)).

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
| GET | `/top-products?days=30` | Top 5 produtos mais vendidos (por quantidade) no último mês |
| GET | `/health` | Verifica o banco e o LLM (Ollama acessível e modelo baixado) |
| GET | `/docs` | Documentação interativa (Swagger) |

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

`/health` responde `200` com `status: ok`, `200` com `status: degraded` quando o LLM está fora (o `/top-products` continua funcionando) e `503` quando o banco está fora.

Toda resposta traz os cabeçalhos `X-Request-ID` (enviado pelo cliente ou gerado) e `X-Process-Time`, também registrados no log.

## Arquitetura

```mermaid
flowchart TD
    Q[GET /sales-insights] --> G1{Guardrail da pergunta}
    G1 -- fora do escopo / injection / alteração --> E400[400]
    G1 --> C{Cache de respostas}
    C -- acerto --> R[Resposta]
    C --> P[Reescreve 'última semana' / 'último mês' como intervalo exato]
    P --> L1[LLM gera SQL a partir do esquema + linhas de exemplo]
    L1 --> G2{Guardrail do SQL - sqlglot}
    G2 -- inseguro --> E400
    G2 --> X[Execução: usuário somente leitura, READ ONLY, timeout]
    X -- erro --> F[Erro volta ao LLM para corrigir, até 2 vezes] --> G2
    X -- sem linhas --> N[Resposta fixa: não encontrei dados]
    X -- linhas --> L2[LLM responde só com as linhas]
    L2 --> R
```

### Como o uso de RAG é forçado

O modelo nunca responde só com o próprio conhecimento:

- Escopo: perguntas fora do domínio de vendas e pedidos para alterar a base ("apague", "atualize", "insira"...) são bloqueados antes de chamar o LLM (HTTP 400). O próprio LLM também pode marcar a pergunta como `FORA_DO_ESCOPO`.
- Contexto obrigatório: a resposta só é gerada a partir das linhas retornadas pela consulta. Se a consulta não retorna dados, a API devolve uma resposta fixa, sem chamar o LLM.
- Prompt restritivo: o prompt de resposta proíbe conhecimento externo, valores inventados, contas feitas pelo modelo (somas e médias que não vieram do banco) e símbolos de moeda que a base não informa.

### Segurança do banco (defesa em camadas)

1. Validação do SQL com sqlglot, que lê a árvore sintática da consulta (não usa regex):
   - só uma instrução, do tipo `SELECT`/`WITH`/`UNION`;
   - nenhum comando de escrita ou DDL em nenhum ponto da árvore, inclusive dentro de CTEs (`WITH x AS (DELETE ...)`), `SELECT INTO` e `FOR UPDATE`;
   - todas as tabelas, inclusive após vírgula, em JOINs e em subconsultas, precisam estar em `sales`, `products` ou `customers` (CTEs declaradas na própria consulta são aceitas);
   - sem acesso a `pg_catalog`, `information_schema` ou outros esquemas;
   - sem funções `pg_*` ou que expõem o servidor (`version`, `current_setting`, `dblink`, ...);
   - `LIMIT` obrigatório (adicionado se faltar).
2. Usuário somente leitura (`db/02_readonly_user.sql`): a API conecta como `sales_reader`, que só tem `SELECT` nas três tabelas e `default_transaction_read_only = on`. Os testes de integração provam que ele não consegue escrever nem ler outras tabelas, mesmo tentando `SET TRANSACTION READ WRITE`.
3. Transação `READ ONLY` com `statement_timeout`, e no máximo `MAX_ROWS_TO_LLM` linhas lidas.
4. Erros internos não vazam: falhas inesperadas retornam `502` com mensagem genérica; o detalhe fica só no log.
5. Container sem root: a imagem roda como `appuser`, com healthcheck.

### Datas relativas

Os dados de exemplo vão de janeiro a março de 2025. Usar a data de hoje faria "última semana" e "último mês" retornarem vazio. Por isso, a data de referência é, por padrão, a data da venda mais recente da base (2025-03-02). Para usar outra data, configure `REFERENCE_DATE` (ex.: `REFERENCE_DATE=2025-02-28`).

- "Última semana" são os 7 dias e "último mês" os 30 dias anteriores à data de referência, a mesma regra do `/top-products`.
- Esses intervalos são calculados em Python. Quando a pergunta cita um desses períodos, a expressão é trocada pelo intervalo exato na pergunta enviada ao LLM (a palavra "mês" some), para que ele não confunda "último mês" com o mês de calendário anterior.
- Quando a pergunta cita um mês sem ano ("fevereiro"), o SQL filtra também pelo ano da data de referência.

`/top-products` ordena por quantidade vendida e, em caso de empate, por faturamento (`?days=` ajusta a janela).

### Desempenho e resiliência

- Cache de respostas em memória por `ANSWER_CACHE_TTL` segundos (padrão 1 hora): a mesma pergunta, ignorando caixa, acentos e pontuação, volta em milissegundos.
- Cache do esquema: DDL e linhas de exemplo são montados uma vez e reutilizados.
- Timeout por chamada ao LLM (`LLM_TIMEOUT`): uma requisição nunca fica pendurada se o provedor travar.
- Aquecimento do modelo: ao subir, a API pede ao Ollama para carregar o modelo em segundo plano e mantê-lo na memória (`OLLAMA_KEEP_ALIVE`), evitando que a primeira pergunta espere o carregamento.
- Correção de SQL: se o SQL gerado falhar, o erro volta ao LLM para corrigir, até `SQL_MAX_RETRIES` vezes.

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
pytest -q                                # unitários (SQLite em memória + LLM falso), sem Postgres nem Ollama
ruff check . && ruff format --check .    # lint e formatação (pip install ruff)
```

Testes de integração com PostgreSQL (`tests/test_postgres.py`), contra um banco carregado com os dois scripts de `db/`:

```bash
TEST_POSTGRES_URL=postgresql+psycopg2://sales_reader:sales_reader@localhost:5432/sales \
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg2://sales:sales@localhost:5432/sales \
pytest -q -m postgres
```

São 94 testes ao todo:

- Unitários (76):
  - `/top-products`: ranking e validação de parâmetros;
  - `/sales-insights`: resposta baseada em dados, resposta fixa sem dados, correção do SQL gerado (até 2 vezes, depois 502), SQL inseguro vindo do LLM e erro genérico quando o LLM cai;
  - cache: pergunta repetida não chama o LLM, expiração, limite de itens e cache do esquema;
  - `/health`: ok, degradado (LLM fora) e 503 (banco fora); middleware de `X-Request-ID`; timeout e aquecimento do LLM;
  - guardrails da pergunta: prompt injection, pedidos de alteração, perguntas válidas aceitas e fora de escopo rejeitadas;
  - guardrails do SQL: escrita, DDL, várias instruções, tabelas não permitidas (inclusive após vírgula e em subconsultas), catálogos do sistema, funções perigosas, `SELECT INTO`, CTE com `DELETE`, `FOR UPDATE`, e consultas válidas aceitas;
  - períodos relativos e estrutura do conjunto de avaliação.
- Integração com PostgreSQL (18): permissões do usuário somente leitura (escrita e outras tabelas negadas), transação `READ ONLY`, `statement_timeout`, limite de linhas, SQL específico do Postgres (`TO_CHAR`, `TIMESTAMP`, `EXTRACT`, CTE) com os valores esperados e a API ponta a ponta.

CI (`.github/workflows/ci.yml`): a cada push roda lint, testes unitários, testes de integração com um serviço PostgreSQL 16 e o build da imagem Docker (conferindo que ela roda sem root).

## Avaliação do LLM

Os testes acima usam um LLM falso. Para medir o LLM de verdade há um conjunto de 34 perguntas de referência em `eval/perguntas.json`, com os valores esperados calculados direto no `01_init.sql`, em dois grupos:

- ajuste (14): perguntas usadas para calibrar o prompt durante o desenvolvimento;
- inéditas (20): escritas depois e rodadas primeiro sem nenhum ajuste de prompt, para medir como o modelo se sai com perguntas que nunca viu (16 de dados e 4 que devem ser recusadas).

```bash
python scripts/avaliar.py --url http://localhost:8000 --rodadas 3   # suba a API com ANSWER_CACHE_TTL=0
```

Também via `make eval-consistencia` (Linux/macOS) ou `testar-consistencia.ps1 -Rodadas 2` (Windows), que reconstroem a API com o cache desligado e mostram o placar por grupo.

Resultados com Llama 3.1 8B (Ollama, CPU, cache desligado):

| Rodada | Ajuste (14) | Inéditas (20) | Total |
|---|---|---|---|
| Primeira rodada com as inéditas, sem nenhum ajuste para elas | 14/14 | 18/20 | 32/34 |
| Depois de duas regras gerais no prompt e da remoção de moeda em código | 13/14 | 20/20 | 33/34 |

- Os dois erros da primeira rodada foram de interpretação: "fez mais compras" virou soma de unidades em vez de contagem de vendas, e "produto mais caro" veio sem o preço. Viraram regras gerais no prompt (contagem x unidades; incluir no SELECT a coluna usada para ordenar), não respostas específicas.
- O erro da rodada final mudou de pergunta: o top 3 do último mês agrupou por produto e data. Com um modelo de 8B, mudar o prompt para uma pergunta às vezes altera a interpretação de outra; por isso o que dava para garantir foi para código (períodos relativos, moeda, pedidos de alteração) e não regrediu mais.
- Respostas idênticas entre rodadas com o mesmo prompt (temperatura 0).
- Tempo por pergunta com LLM: mediana de ~25 s (de 10 a 75 s). A primeira pergunta logo após subir a API espera o modelo carregar (até alguns minutos em CPU).
- Pergunta repetida: ~0,1 s (cache).
- Pedidos para alterar a base foram recusados em todas as rodadas; o banco continuou com as 33 vendas.

## Decisões técnicas

- Text-to-SQL em vez de embeddings: os dados são tabulares e as perguntas são agregações (somas, rankings, períodos). Um banco vetorial recuperaria trechos, mas não calcularia totais; gerar SQL deixa o banco fazer as contas, com resultado exato e auditável.
- O LLM não faz contas: datas relativas são calculadas em Python e o prompt proíbe somas e médias que não vieram do banco. Os erros mais comuns de modelos pequenos são aritméticos.
- Validação por árvore sintática: regex não enxerga tabelas após vírgula, subconsultas ou CTEs com `DELETE`; o sqlglot lê a consulta como o banco leria.
- Defesa em profundidade: mesmo que a validação falhasse, o usuário `sales_reader` e a transação `READ ONLY` impedem qualquer escrita no próprio banco.
- Data de referência na última venda: com dados de 2025, usar a data de hoje deixaria "última semana" vazia; `REFERENCE_DATE` permite fixar outra data.
- Llama 3.1 via Ollama como padrão: gratuito e local (nenhum dado sai da máquina); trocar para OpenAI é só uma variável de ambiente.

## Limitações conhecidas

- Modelo pequeno: o Llama 3.1 8B acerta a grande maioria das perguntas (33/34 na avaliação), mas pode interpretar uma pergunta de forma inesperada, como agrupar por uma coluna a mais. A resposta sempre traz o SQL e as linhas para conferência; um modelo maior (ex.: `gpt-4o-mini`) interpreta com mais segurança.
- Heurísticas: o filtro de escopo usa uma lista de termos do domínio, e a troca de "último mês" por datas usa padrões de texto. O LLM é a segunda barreira (`FORA_DO_ESCOPO`), e a validação do SQL é a terceira.
- Latência em CPU: de 20 a 60 s por pergunta nova; GPU ou um provedor externo reduzem para poucos segundos.
- Cache local: fica na memória de cada instância da API.

## Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://sales:sales@localhost:5432/sales` | Conexão com o banco (no Docker: usuário `sales_reader`) |
| `LLM_PROVIDER` | `ollama` | `ollama` ou `openai` |
| `LLM_MODEL` | `llama3.1` | Modelo (ex.: `gpt-4o-mini` na OpenAI) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Endereço do Ollama |
| `OPENAI_API_KEY` | — | Chave da OpenAI |
| `LLM_TIMEOUT` | `300` | Tempo máximo (s) de cada chamada ao LLM |
| `LLM_WARMUP` | `true` | Pré-carrega o modelo do Ollama ao subir |
| `OLLAMA_KEEP_ALIVE` | `30m` | Tempo que o modelo fica carregado na memória |
| `LLM_HEALTH_TIMEOUT` | `3` | Tempo máximo (s) do `/health` para checar o LLM |
| `REFERENCE_DATE` | última venda | Data usada como "hoje" |
| `MAX_QUESTION_LENGTH` | `300` | Tamanho máximo da pergunta |
| `MAX_ROWS_TO_LLM` | `50` | Linhas máximas lidas e enviadas ao LLM |
| `SQL_TIMEOUT_MS` | `5000` | `statement_timeout` das consultas |
| `SQL_MAX_RETRIES` | `2` | Correções de SQL pelo LLM |
| `ANSWER_CACHE_TTL` | `3600` | Duração do cache de respostas (s); `0` desativa |
| `ANSWER_CACHE_SIZE` | `256` | Número máximo de respostas em cache |

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
testar-docker.ps1         # teste ponta a ponta no Docker (Windows)
testar-consistencia.ps1   # avaliação do LLM no Docker com placar (Windows)
Makefile                  # atalhos para Linux/macOS (make help)
```

## Melhorias possíveis em produção

- Autenticação e rate limit por cliente.
- Cache compartilhado (Redis) quando houver mais de uma instância da API.
- Senhas do banco em secrets, em vez de valores fixos no `docker-compose.yml`.
- Observabilidade das chamadas ao LLM (Langfuse ou LangSmith) e a avaliação de `eval/` rodando periodicamente.
- GPU para o Ollama ou um modelo maior, para reduzir o tempo de resposta.
