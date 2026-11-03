# Detalhes técnicos

Complemento do [README](../README.md): arquitetura, segurança, decisões técnicas, avaliação do LLM e configuração.

- [Arquitetura](#arquitetura)
- [Segurança do banco](#segurança-do-banco-defesa-em-camadas)
- [Datas relativas](#datas-relativas)
- [Desempenho e resiliência](#desempenho-e-resiliência)
- [Testes](#testes)
- [Avaliação do LLM](#avaliação-do-llm)
- [Decisões técnicas](#decisões-técnicas)
- [Limitações conhecidas](#limitações-conhecidas)
- [Configuração](#configuração)
- [Melhorias possíveis em produção](#melhorias-possíveis-em-produção)

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

## Segurança do banco (defesa em camadas)

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

## Datas relativas

Os dados de exemplo vão de janeiro a março de 2025. Usar a data de hoje faria "última semana" e "último mês" retornarem vazio. Por isso, a data de referência é, por padrão, a data da venda mais recente da base (2025-03-02). Para usar outra data, configure `REFERENCE_DATE` (ex.: `REFERENCE_DATE=2025-02-28`).

- "Última semana" são os 7 dias e "último mês" os 30 dias anteriores à data de referência, a mesma regra do `/top-products`.
- Esses intervalos são calculados em Python. Quando a pergunta cita um desses períodos, a expressão é trocada pelo intervalo exato na pergunta enviada ao LLM (a palavra "mês" some), para que ele não confunda "último mês" com o mês de calendário anterior.
- Quando a pergunta cita um mês sem ano ("fevereiro"), o SQL filtra também pelo ano da data de referência.

`/top-products` ordena por quantidade vendida e, em caso de empate, por faturamento (`?days=` ajusta a janela).

## Desempenho e resiliência

- Cache de respostas em memória por `ANSWER_CACHE_TTL` segundos (padrão 1 hora): a mesma pergunta, ignorando caixa, acentos e pontuação, volta em milissegundos.
- Cache do esquema: DDL e linhas de exemplo são montados uma vez e reutilizados.
- Timeout por chamada ao LLM (`LLM_TIMEOUT`): uma requisição nunca fica pendurada se o provedor travar.
- Aquecimento do modelo: ao subir, a API pede ao Ollama para carregar o modelo em segundo plano e mantê-lo na memória (`OLLAMA_KEEP_ALIVE`), evitando que a primeira pergunta espere o carregamento.
- Correção de SQL: se o SQL gerado falhar, o erro volta ao LLM para corrigir, até `SQL_MAX_RETRIES` vezes.

`/health` responde `200` com `status: ok`, `200` com `status: degraded` quando o LLM está fora (o `/top-products` continua funcionando) e `503` quando o banco está fora.

Toda resposta traz os cabeçalhos `X-Request-ID` (enviado pelo cliente ou gerado) e `X-Process-Time`, também registrados no log.

## Testes

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

## Avaliação do LLM

Os testes automatizados usam um LLM falso. Para medir o LLM de verdade há um conjunto de 34 perguntas de referência em `eval/perguntas.json`, com os valores esperados calculados direto no `01_init.sql`, em dois grupos:

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

## Melhorias possíveis em produção

- Autenticação e rate limit por cliente.
- Cache compartilhado (Redis) quando houver mais de uma instância da API.
- Senhas do banco em secrets, em vez de valores fixos no `docker-compose.yml`.
- Observabilidade das chamadas ao LLM (Langfuse ou LangSmith) e a avaliação de `eval/` rodando periodicamente.
- GPU para o Ollama ou um modelo maior, para reduzir o tempo de resposta.
