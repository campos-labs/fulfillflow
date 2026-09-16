# FulfillFlow

Checkout de desenvolvimento **v1.2.0.dev0**, na linha de Tracking assíncrono.
O [DESIGN](DESIGN.md) define os contratos e o [RELEASE_PLAN](RELEASE_PLAN.md)
registra o aceite dos incrementos. As referências `v1.0.0` e `v1.1.0-rc.1`
e suas evidências permanecem congeladas; campanhas de carga estão suspensas.

Core mantém Orders, Shipments, Notifications e Carriers. Tracking mantém HMAC,
adapters Alpha/Beta, inbox e timeline. Cada serviço possui banco, role e worker
próprios. Core continua sendo a entrada pública e serve a interface.

Eventos novos recebem **202 após admissão durável**, com inbox, comando e outbox
Tracking gravados juntos. O worker publica `tracking.apply.v1` pelo RabbitMQ.
Core grava recibo, efeitos e outbox `tracking.result.v1` na mesma transação local;
Tracking finaliza a timeline e o inbox ao processar esse resultado. Publicação e
ACK ocorrem fora da transação SQL. Não há atomicidade global nem exactly-once.
O endpoint e o cliente internos de apply HTTP foram removidos; consultas e
encaminhamento autenticados continuam HTTP.

## Subida local com Docker Compose

O fluxo padrão funciona a partir do checkout sem publicar o PostgreSQL no host:

```powershell
docker compose up --build --wait
```

O projeto padrão `fulfillflow-v12` cria volumes PostgreSQL e RabbitMQ novos.
PostgreSQL inicializa `fulfillflow_core` e `fulfillflow_tracking`, com roles sem
CONNECT ao banco alheio. Os serviços `migrate-core` e `migrate-tracking` aplicam
os heads antes dos processos. Somente Core publica `127.0.0.1:8000`; Tracking,
workers, PostgreSQL e RabbitMQ ficam na rede interna. O broker usa um vhost e
dois usuários com permissões separadas para os fluxos de comando e resultado.

Cada API usa 0,5 CPU, 384 MiB e pool 2/0; cada worker, 0,5 CPU, 384 MiB e pool 3/0.
PostgreSQL usa 2 CPUs/2560 MiB e RabbitMQ, 0,5 CPU/512 MiB. São parâmetros
funcionais, sem alegação de equivalência de recursos com a v1.1. Workers usam
prefetch 8, lote 20, polling 500 ms, lease 30 s e timeout de confirm 5 s.

Health/readiness das APIs não comprovam conclusão do trabalho. Healthcheck,
lifecycle operacional completo e rearme auditável dos workers pertencem ao
incremento III; não se deve considerar esta etapa uma validação operacional final.
Não aponte os serviços a bancos ou volumes históricos.

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
Invoke-WebRequest http://127.0.0.1:8000/
```

Além dos health checks, os contratos atuais sob `/api/v1` são:

- `GET /health/live` retorna `200 {"status":"ok"}` sem acessar o banco;
- `GET /health/ready` retorna `200 {"status":"ok"}` quando o schema está no
  head do Alembic e `SELECT 1` responde;
- readiness retorna `503 {"status":"unavailable"}` se o banco ficar
  indisponível depois do startup;
- startup falha se o banco estiver inacessível ou o schema não estiver no head.
- Orders: criação, listagem, detalhe com resumo de remessas, confirmação e
  cancelamento;
- Shipments: criação, listagem, detalhe e cancelamento manual;
- Tracking: ingestão autenticada em
  `POST /api/v1/carriers/{carrier_code}/events`, timeline paginada em
  `GET /api/v1/shipments/{shipment_id}/tracking` e consulta sanitizada do inbox
  em `GET /api/v1/carrier-events` e
  `GET /api/v1/carrier-events/{inbox_event_id}`;
- Notifications: consulta operacional paginada em `GET /api/v1/notifications`,
  com filtros por `status`, `shipment_id`, `created_from` e `created_to`, e
  detalhe em `GET /api/v1/notifications/{notification_id}`;
- erros públicos, inclusive validação, 404 e 405, usam problem details e todas
  as respostas propagam um `X-Request-ID` válido.

Os webhooks usam `Content-Type: application/json` e os headers
`X-FulfillFlow-Event-Id`, `X-FulfillFlow-Timestamp` e
`X-FulfillFlow-Signature`. A assinatura é `sha256=<hex>` para HMAC-SHA256 de
`timestamp + "." + event_id + "." + raw_body`; Alpha e Beta usam secrets
distintos. O corpo autenticado é preservado byte a byte e só depois é parseado e
normalizado. Cada header autenticado deve ocorrer exatamente uma vez, e o event
ID assinado aceita somente ASCII visível, sem whitespace lateral, com até 128
caracteres.

### Aceite, consulta e duplicatas

A resposta 202 contém `inbox_event_id`, `external_event_id`, `status=RECEIVED`,
`received_at` e `request_id`. `Location` indica
`/api/v1/carrier-events/{inbox_event_id}` e `Retry-After: 1` orienta polling.
O GET expõe `result`, `tracking_event_id`, `completed_at` e progresso local:
`QUEUED`, `AWAITING_RESULT`, `COMPLETED` ou `BLOCKED_LOCAL`.

Mesmo ID/bytes pendente retorna 202; já processado retorna 200 `DUPLICATE` com
resultado original; rejeitado preserva o erro permanente. Bytes divergentes
retornam 409. Erros de normalização autenticada persistem rejeição sem comando
nem outbox. Erros de domínio decididos depois do aceite são consultados no GET.
Broker indisponível não impede admissão se Core/Tracking e PostgreSQL necessários
estiverem disponíveis. 202 e fila vazia **não significam conclusão**.

O ACK confirma a inbox técnica durável. O processador local retoma trabalho após
reinício; falhas transitórias por item têm cinco tentativas por geração, com
esperas de 1/5/15/60 s. Conflitos e esgotamento ficam `BLOCKED`, sem retomada
automática. O rearme auditável será entregue no III; não altere esses estados
manualmente para simular recuperação. Legado sem transporte tem progresso nulo;
as migrations não criam comandos nem inventam resultados históricos.

Os defaults de secrets no `compose.yaml` são exclusivos do ambiente local
isolado. Para sobrescrevê-los, copie `.env.example` para `.env` e substitua todos
os placeholders. Mantenha `CORE_DB_PASSWORD`/`CORE_DATABASE_URL` e
`TRACKING_DB_PASSWORD`/`TRACKING_DATABASE_URL` coerentes. `POSTGRES_PASSWORD`
pertence apenas à administração inicial. Alterar o `.env` não altera senhas de
roles já criadas. `INTERNAL_API_SECRET` autentica as chamadas internas; HMAC fica
somente no Tracking e `SESSION_SECRET` somente no Core. Ao trocar credenciais AMQP, provisione os usuários correspondentes no broker;
`CORE_AMQP_URL`/`TRACKING_AMQP_URL` não alteram usuários já existentes.
Timeouts HTTP padrão:
10 s para chamadas ao Core e 30 s para encaminhamento ao Tracking.

## Interface operacional

A UI usa Jinja2 com HTML server-rendered, Bootstrap e HTMX locais, sem SPA,
toolchain Node ou dependência de CDN em runtime. A navegação segue Dashboard →
Orders → Shipments → Inbox → Notifications → Simulator. Requisições
HTMX usam as mesmas rotas e casos de uso das páginas completas.

As rotas HTML são:

- `GET /`: dashboard com contagens operacionais e eventos recentes do inbox;
- `GET /orders`, `GET /orders/new`, `POST /orders` e
  `GET /orders/{order_id}`: lista, criação e detalhe composto com Shipments;
- `POST /orders/{order_id}/confirm` e `POST /orders/{order_id}/cancel`:
  confirmação e cancelamento de Order;
- `GET /shipments`, `GET /shipments/new`, `POST /shipments` e
  `GET /shipments/{shipment_id}`: lista com filtros, criação e detalhe;
- `POST /shipments/{shipment_id}/cancel` e
  `GET /shipments/{shipment_id}/tracking`: cancelamento e timeline sanitizada;
- `GET /carrier-events` e `GET /carrier-events/{inbox_event_id}`: lista, filtros
  e detalhe sanitizado do inbox;
- `GET /notifications` e `GET /notifications/{notification_id}`: lista, filtros
  e detalhe somente leitura das simulações de Notification;
- `GET /simulator`: painel estritamente instrucional para o cliente externo;
- `GET /static/...`: assets versionados empacotados com a aplicação.

`/shipments/new?order_id=<uuid>` permite pré-preencher o Order. Formulários
mutáveis usam sessão assinada, CSRF e Post/Redirect/Get; APIs JSON e webhooks
continuam stateless, sem CSRF e sem criação de cookie de sessão.

Os assets oficiais incluídos no pacote são:

- Bootstrap 5.3.8 (`bootstrap-5.3.8.min.css` e
  `bootstrap-5.3.8.bundle.min.js`), do pacote oficial `bootstrap@5.3.8`
  [distribuído pelo jsDelivr](https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/),
  sob licença MIT registrada em `BOOTSTRAP-LICENSE.txt`;
- HTMX 2.0.10 (`htmx-2.0.10.min.js`), do pacote oficial `htmx.org@2.0.10`
  [distribuído pelo jsDelivr](https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/),
  sob licença 0BSD registrada em `HTMX-LICENSE.txt`.

Os arquivos ficam em `src/fulfillflow/web/static/vendor`, são referenciados por
versão e integridade e são servidos somente pela própria aplicação.

## Simulador externo de Carriers

O modo padrão do simulador é assíncrono: imprime admissão separada da conclusão,
consulta o Location no mesmo origin e aguarda até 30 s por evento
(`--completion-timeout-seconds`). `RESULT_NOT_OBSERVED` significa timeout de
observação; não prova perda nem rejeição. `--mode synchronous` permite usar a
referência congelada v1.1. O modo assíncrono não adapta o loadgen histórico.

O painel `/simulator` apenas documenta o uso. A execução real ocorre em processo
externo por `scripts/simulate_carrier_events.py`, que usa somente a biblioteca
padrão para chamar o webhook HTTP público. Antes da execução, crie normalmente
um Order confirmado e uma Shipment e use o `tracking_code` dessa Shipment.

O secret nunca é aceito como argumento. O Carrier selecionado lê exclusivamente
`CARRIER_ALPHA_WEBHOOK_SECRET` ou `CARRIER_BETA_WEBHOOK_SECRET`; o valor deve ser
o mesmo configurado na aplicação que receberá o evento. Exemplo seguro para o
Compose local:

```powershell
$env:CARRIER_ALPHA_WEBHOOK_SECRET = "<mesmo-secret-local-da-aplicacao>"
uv run python scripts/simulate_carrier_events.py `
  --base-url http://127.0.0.1:8000 `
  --carrier carrier-alpha `
  --tracking-code ALPHA000001 `
  --scenario valid
```

Os cinco cenários existem para `carrier-alpha` e `carrier-beta`:

- `valid`: envia POSTED → IN_TRANSIT → OUT_FOR_DELIVERY → DELIVERED e
  exige `APPLIED` em cada resposta;
- `duplicate`: repete integralmente o primeiro artefato assinado e exige
  `DUPLICATE` na segunda resposta;
- `out-of-order`: envia um evento mais recente seguido de um anterior e exige
  `IGNORED_STALE` no segundo;
- `unknown-status`: exige HTTP 422 e o problem code de status externo
  desconhecido;
- `invalid-signature`: exige HTTP 401 e o problem code de assinatura inválida.

O exit code é `0` somente quando todas as respostas coincidem exatamente com o
contrato do cenário, inclusive os erros 422 e 401 intencionais. Divergência de
status/result/problem code, resposta inválida, timeout ou erro de rede retorna
exit code diferente de zero. `--seed`, `--event-id-prefix` e `--start-at` tornam
determinísticos os IDs e a sequência de payloads; o timestamp HMAC continua usando
o relógio atual para respeitar a janela anti-replay. `--timeout-seconds` limita a
espera; sem prefixo explícito e sem seed, IDs únicos são gerados para o uso manual.

Para encerrar e remover o volume local deste projeto:

```powershell
docker compose down --volumes
```

## Preparação funcional e execução por processos

Com o Compose saudável, este comando cria um Order confirmado e duas Shipments
pendentes, Alpha `V11ALPHA0001` e Beta `V11BETA0001`, pela API pública:

```powershell
uv sync --frozen
uv run python scripts/prepare_demo_v11.py --base-url http://127.0.0.1:8000
```

Referências e dados sintéticos são fixos; UUIDs e timestamps são atribuídos pela
aplicação. Repetir preserva os mesmos registros, inclusive após eventos, e uma
divergência nos dados esperados falha sem sobrescrevê-los. Uma preparação parcial
pode ser retomada; ela não é uma transação entre bancos nem o seed do benchmark.
Use o simulador acima com cada tracking code para preencher timeline e concluir
o Order após entregar ambas as Shipments.

Para executar aplicações no host, provisione os dois bancos PostgreSQL 18 com as
roles segregadas do script `infrastructure/init-databases.sh`. Em dois terminais
PowerShell, configure os secrets próprios, o token interno compartilhado e as URLs:

```powershell
# Terminal Core; DATABASE_URL aponta exclusivamente ao banco Core no host.
$env:SERVICE_ROLE = "core"
$env:DATABASE_URL = "postgresql+psycopg://<core-role>:<password>@127.0.0.1:5432/fulfillflow_core"
$env:APP_PORT = "8000"
$env:TRACKING_BASE_URL = "http://127.0.0.1:8001"
uv run alembic -c alembic_core.ini upgrade head
uv run alembic -c alembic_core.ini current --check-heads
uv run alembic -c alembic_core.ini check
uv run python -m fulfillflow
```

```powershell
# Terminal Tracking; HMAC configurado aqui, além do mesmo INTERNAL_API_SECRET.
$env:SERVICE_ROLE = "tracking"
$env:DATABASE_URL = "postgresql+psycopg://<tracking-role>:<password>@127.0.0.1:5432/fulfillflow_tracking"
$env:APP_PORT = "8001"
$env:CORE_BASE_URL = "http://127.0.0.1:8000"
uv run alembic -c alembic_tracking.ini upgrade head
uv run alembic -c alembic_tracking.ini current --check-heads
uv run alembic -c alembic_tracking.ini check
uv run python -m fulfillflow.tracking
```

Os workers são processos adicionais, com a mesma configuração do serviço
proprietário e `AMQP_URL` apontando ao vhost provisionado. Em terminais próprios:

```powershell
# Ambiente Core, DATABASE_URL Core e AMQP_URL do usuário Core.
uv run python -m fulfillflow.core.worker
# Ambiente Tracking, DATABASE_URL Tracking e AMQP_URL do usuário Tracking.
uv run python -m fulfillflow.tracking.worker
```

## Testes e qualidade

Os testes unitários do simulador, arquiteturais, de health e de problem details
não dependem de banco. Testes de repository/service, APIs persistentes, UI e E2E
usam PostgreSQL 18 real. Configure os três bancos isolados abaixo; fixtures
ausentes são explicitamente ignoradas e não constituem validação completa.
O E2E sobe Core e Tracking em portas TCP distintas e chama o webhook público
por processo externo. Os testes limpam apenas os bancos dedicados informados.

```powershell
docker compose -p fulfillflow-v12-tests -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL = "postgresql+psycopg://fulfillflow_core:v11-isolated-core-test@127.0.0.1:18541/fulfillflow_core"
$env:TEST_TRACKING_DATABASE_URL = "postgresql+psycopg://fulfillflow_tracking:v11-isolated-tracking-test@127.0.0.1:18541/fulfillflow_tracking"
$env:TEST_AMQP_URL = 'amqp://v12_test:v12-isolated-broker-test@127.0.0.1:18542/fulfillflow-v12-test'
$env:TEST_V11_POSTGRES_CONTAINER = 'fulfillflow-v12-tests-db-1'
$env:TEST_LEGACY_DATABASE_URL = "postgresql+psycopg://fulfillflow_legacy:v11-isolated-legacy-test@127.0.0.1:18541/fulfillflow_legacy"
```

O teste estrutural histórico cria e remove seu próprio container Docker, sem
Locust ou campanha. Após os gates, remova somente a infraestrutura dedicada:
`docker compose -p fulfillflow-v12-tests -f compose.test.yaml down --volumes`.

```powershell
uv run pytest tests/unit tests/api -q
uv run pytest tests/unit/test_carrier_simulator.py -q
uv run pytest tests/ui -q
uv run pytest tests/e2e -q
uv run pytest
uv run pytest --cov=fulfillflow --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
docker compose config --quiet
```

A v1.2 usa `1201_core` e `1202_tracking`, com metadados e graphs separados.
Ambos recebem outbox/inbox técnica/quarentena; Tracking adiciona resultado e
conclusão opcionais. As bases v1.1 `1101_core`/`1101_tracking` são preservadas.
Os testes verificam upgrade/check/downgrade/upgrade de ambos, ausência de FKs
entre proprietários e rejeição de conexão com a credencial do outro serviço.

## Documentação

- [Demonstração funcional](docs/DEMO.md): preparação e jornada pela API/UI.
- [DESIGN](DESIGN.md): arquitetura alvo v1.2 e contratos preservados da base.
- [RELEASE_PLAN](RELEASE_PLAN.md): incrementos, aceite e estado da v1.2.
- [Ferramenta de benchmark](benchmarks/README.md): datasets, validação e artefatos.
- [Revisão da v1.1](benchmarks/V11_REVIEW.md): síntese e índice das evidências.
- [Baseline v1.0 publicada](benchmarks/baselines/v1.0/README.md): referência histórica.

## Transporte v1.2 — incremento I

O runtime assíncrono usa transporte durável. Cada banco possui suas próprias
`message_outbox`, `message_inbox` e `message_quarantine`. ACK confirma persistência
técnica; não representa conclusão de negócio. Itens `BLOCKED` não retomam sozinhos;
a operação de rearme auditável pertence ao incremento III.

Os testes de transporte exigem RabbitMQ real, além dos bancos já documentados:

```powershell
docker compose -p fulfillflow-v12-tests -f compose.test.yaml up -d --wait
$env:TEST_AMQP_URL = 'amqp://v12_test:v12-isolated-broker-test@127.0.0.1:18542/fulfillflow-v12-test'
uv run pytest tests/integration/test_message_transport.py -q
```

Configure também `TEST_DATABASE_URL` e `TEST_TRACKING_DATABASE_URL` conforme a
seção de testes. Use projeto e volumes novos; não reutilize recursos históricos.
A imagem de teste é RabbitMQ 4.2.4 Alpine, fixada por digest no Compose; o cliente
é `aio-pika==9.5.8`. Os testes verificam confirmação, retorno, commit/ACK incerto,
duplicação, lease, esgotamento e recuperação local com a fila vazia.
