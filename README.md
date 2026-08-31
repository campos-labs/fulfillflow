# FulfillFlow

Incremento executável do FulfillFlow v1.0.0 com FastAPI, PostgreSQL 18
assíncrono, Alembic e os módulos de negócio Orders, Shipments, Carriers,
Tracking e Notifications. Estão incluídas as máquinas de estados, persistência
modular, APIs JSON, paginação, filtros, request ID e erros
`application/problem+json`.

Este incremento inclui os Carriers simulados Alpha e Beta, allowlist estática de
adapters, autenticação HMAC sobre os bytes originais, inbox auditável, timeline
canônica e registro persistente de Notifications simuladas para transições
aplicadas durante o processamento síncrono. Ainda não inclui UI, seed de
demonstração, benchmark ou processamento assíncrono.

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

Os testes unitários, arquiteturais, de health e de problem details não dependem
de banco. Testes de repository/service e APIs persistentes usam somente um
PostgreSQL 18 dedicado informado por `TEST_DATABASE_URL`; sem essa variável,
eles são explicitamente ignorados.

```powershell
uv run pytest tests/unit tests/api -q
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
