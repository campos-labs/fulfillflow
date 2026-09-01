# FulfillFlow

Incremento executável do FulfillFlow v1.0.0 com FastAPI, PostgreSQL 18
assíncrono, Alembic e os módulos de negócio Orders, Shipments, Carriers,
Tracking e Notifications. Estão incluídas as máquinas de estados, persistência
modular, APIs JSON, paginação, filtros, request ID e erros
`application/problem+json`.

Este incremento inclui os Carriers simulados Alpha e Beta, allowlist estática de
adapters, autenticação HMAC sobre os bytes originais, inbox auditável, timeline
canônica e registro persistente de Notifications simuladas para transições
aplicadas durante o processamento síncrono. A interface operacional é renderizada
no servidor e permite demonstrar esse fluxo completo. Seed de demonstração,
benchmark e processamento assíncrono permanecem fora deste incremento.

## Subida local com Docker Compose

O fluxo padrão funciona a partir do checkout sem publicar o PostgreSQL no host:

```powershell
docker compose up --build --wait
```

O Compose aguarda o banco ficar saudável, executa `alembic upgrade head` no
serviço one-shot `migrate` e só então inicia `app`. A API é publicada apenas em
`127.0.0.1:8000` por padrão.

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

Os defaults de secrets no `compose.yaml` são exclusivos do ambiente local
isolado. Para sobrescrevê-los, copie `.env.example` para `.env` e substitua todos
os placeholders. Mantenha `POSTGRES_PASSWORD` e a senha codificada em
`DATABASE_URL` coerentes.

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

## Execução sem container da aplicação

É necessário Python 3.13 gerenciado pelo `uv`, um PostgreSQL 18 acessível e um
`.env` preenchido. Para um banco no host, altere o hostname de `DATABASE_URL` de
`db` para `127.0.0.1`.

```powershell
uv sync --frozen
uv run alembic upgrade head
uv run alembic current --check-heads
uv run alembic check
uv run fastapi dev src/fulfillflow/main.py
```

## Testes e qualidade

Os testes unitários do simulador, arquiteturais, de health e de problem details
não dependem de banco. Testes de repository/service, APIs persistentes, UI e E2E
usam somente um PostgreSQL 18 dedicado informado por `TEST_DATABASE_URL`; sem
essa variável, eles são explicitamente ignorados. O E2E sobe a aplicação em uma
porta TCP local e executa o script externo contra o webhook público.

```powershell
uv run pytest tests/unit tests/api -q
uv run pytest tests/unit/test_carrier_simulator.py -q
uv run pytest tests/ui -q
uv run pytest tests/e2e/test_external_simulator_journey.py -q
uv run pytest
uv run pytest --cov=fulfillflow --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run lint-imports
docker compose config --quiet
```

A revisão `0001_bootstrap` estabelece o baseline, `0002_orders_shipments` cria
Orders, Shipments e o registro de Carriers, e `0003_carriers_tracking` instala os
registros determinísticos Alpha/Beta e cria `carrier_event_inbox` e
`tracking_events`. A revisão `0004_notifications` cria o registro persistente das
notificações simuladas. O schema usa UUID/timestamptz nativos, JSONB/bytea para a
projeção e os bytes externos, checks textuais nomeados, FKs restritivas e os
índices do contrato.
