# FulfillFlow

Incremento I da v1.1: Core + Tracking, FastAPI e PostgreSQL 18 assíncrono,
com bancos, credenciais e migrações separados. Core mantém Orders, Shipments,
Notifications e cadastro de Carriers. Tracking autentica e normaliza os webhooks
Alpha/Beta e mantém inbox e timeline. A entrada pública e a UI continuam no Core.

O processamento continua síncrono. Um recibo idempotente no Core preserva o
resultado original e impede efeitos duplicados após perda de resposta. A
reentrega idêntica conclui o inbox pendente. Entre commits, o estado no Core pode
estar atualizado antes da timeline; não há atomicidade global nem recuperação
automática sem reentrega. Falhas de comunicação retornam 503, erros inesperados
500, preservando o inbox recebido para retomada.

A v1.0.0 publicada permanece na tag original. A comparação experimental da v1.1,
a adaptação do loadgen e a preparação física do dataset congelado permanecem no
incremento II de [RELEASE_PLAN.md](RELEASE_PLAN.md). Este checkout não está pronto
para campanhas v1.1; workload, protocolo e imagem congelada não foram substituídos.

O roteiro reproduzível está em [docs/DEMO.md](docs/DEMO.md).

## Subida local com Docker Compose

O fluxo padrão funciona a partir do checkout sem publicar o PostgreSQL no host:

```powershell
docker compose up --build --wait
```

O projeto padrão `fulfillflow-v11` cria um volume próprio, sem reutilizar o volume
do monólito. PostgreSQL inicializa `fulfillflow_core` e `fulfillflow_tracking`, com
roles distintas sem CONNECT ao banco do outro serviço. `migrate-core` e
`migrate-tracking` aplicam seus respectivos heads antes de `core` e `tracking`
iniciarem. Somente Core publica `127.0.0.1:8000`; Tracking e PostgreSQL ficam na
rede interna. Cada aplicação usa um worker, 1 CPU, 768 MiB e pool de 5 conexões
sem overflow. PostgreSQL recebe 2 CPUs e 2560 MiB.

As migrações v1.1 destinam-se a bancos novos: não migram dados da v1.0 em uso.
Os scripts Alembic históricos permanecem preservados. Não aponte os novos
serviços para volumes ou bancos históricos.

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
os placeholders. Mantenha `CORE_DB_PASSWORD`/`CORE_DATABASE_URL` e
`TRACKING_DB_PASSWORD`/`TRACKING_DATABASE_URL` coerentes. `POSTGRES_PASSWORD`
pertence apenas à administração inicial. Alterar o `.env` não altera senhas de
roles já criadas. `INTERNAL_API_SECRET` autentica as chamadas internas; HMAC fica
somente no Tracking e `SESSION_SECRET` somente no Core. Timeouts HTTP padrão:
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

## Seeds sintéticos e benchmark histórico v1.0

Os comandos desta seção pertencem à topologia monolítica da tag `v1.0.0`.
`seed_demo.py`, o loader físico e o harness não preparam os bancos v1.1.
Os testes de regressão continuam verificando os hashes lógicos e estruturais
congelados em um terceiro banco isolado. A relocação do import de normalização
no gerador não altera o dataset. Compatibilidade de manifest/topologia, identidades
da versão candidata e eventual necessidade de imagem do loadgen serão decididas
no incremento II; não executar a campanha a partir deste checkout.

Os seeds são fail-closed e aceitam somente PostgreSQL 18 via `postgresql+psycopg`.
Eles exigem `APP_ENV` explícito, schema no head do Alembic e confirmação literal do
nome do banco. Não limpam, substituem ou corrigem dados existentes: banco vazio é
carregado atomicamente, repetição do dataset exato é no-op e qualquer divergência
falha sem escrita.

```powershell
$env:APP_ENV = "local"
$env:DATABASE_URL = "postgresql+psycopg://<user>:<password>@127.0.0.1:5432/fulfillflow_demo"
uv run python scripts/seed_demo.py --confirm-database-name fulfillflow_demo
```

O documento lógico integral do benchmark e o SHA-256 dos mesmos bytes canônicos
ficam em `benchmarks/datasets`. O documento contém metadata, coortes e todas as
linhas usadas pelo seed; a campanha autentica o arquivo e o sidecar antes de
expor qualquer slot ao loadgen. A documentação do contrato, das duas fases
Locust, dos coletores externos e dos artefatos está em `benchmarks/README.md`.
O único manifest de campanha versionado é uma fixture sintética não oficial;
não existe manifest `v1-baseline` nem resultado oficial.

```powershell
uv sync --frozen --all-groups
uv run python -m benchmarks.dataset
uv run python -m benchmarks.run_campaign `
  --manifest benchmarks/fixtures/smoke-campaign.json `
  --validate-only
docker compose -f compose.benchmark.yaml config --quiet
docker build --target loadgen --tag fulfillflow-loadgen:smoke .
```

O target Docker `runtime` continua sem Locust. O target separado `loadgen` instala
o grupo `benchmark` e executa somente o cliente HTTP. Em uma execução aprovada,
o runner usa processos Locust fisicamente distintos para warm-up e measurement,
confere o ambiente Docker, o head Alembic e o digest estrutural específico da release,
e compara integralmente o conteúdo PostgreSQL observado com o artefato autenticado
antes do warm-up. Depois dele, valida cada evento e
relação lógica esperada, preserva individualmente os 15.000 eventos iniciais,
coleta recursos e
mantém os resultados completos visíveis ao Git. A CI valida contratos, hashes,
seeds, Compose e a imagem do loadgen, mas não executa warm-up de 60 segundos,
measurement de 300 segundos ou campanha completa.

## Testes e qualidade

Os testes unitários do simulador, arquiteturais, de health e de problem details
não dependem de banco. Testes de repository/service, APIs persistentes, UI e E2E
usam PostgreSQL 18 real. Configure os três bancos isolados abaixo; fixtures
ausentes são explicitamente ignoradas e não constituem validação completa.
O E2E sobe Core e Tracking em portas TCP distintas e chama o webhook público
por processo externo. Os testes limpam apenas os bancos dedicados informados.

```powershell
docker compose -p fulfillflow-v11-tests -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL = "postgresql+psycopg://fulfillflow_core:v11-isolated-core-test@127.0.0.1:18541/fulfillflow_core"
$env:TEST_TRACKING_DATABASE_URL = "postgresql+psycopg://fulfillflow_tracking:v11-isolated-tracking-test@127.0.0.1:18541/fulfillflow_tracking"
$env:TEST_LEGACY_DATABASE_URL = "postgresql+psycopg://fulfillflow_legacy:v11-isolated-legacy-test@127.0.0.1:18541/fulfillflow_legacy"
```

O teste estrutural histórico cria e remove seu próprio container Docker, sem
Locust ou campanha. Após os gates, remova somente a infraestrutura dedicada:
`docker compose -p fulfillflow-v11-tests -f compose.test.yaml down --volumes`.

```powershell
uv run pytest tests/unit tests/api -q
uv run pytest tests/unit/test_carrier_simulator.py -q
uv run pytest tests/ui -q
uv run pytest tests/e2e/test_external_simulator_journey.py -q
uv run pytest
uv run pytest --cov=fulfillflow --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
docker compose config --quiet
```

A v1.1 usa `1101_core` (cadastro, Orders, Shipments, Notifications e recibos)
e `1101_tracking` (inbox e timeline), com metadados e graphs Alembic separados.
Os testes verificam upgrade/check/downgrade/upgrade de ambos, ausência de FKs
entre proprietários e rejeição de conexão com a credencial do outro serviço.

No histórico v1.0, a revisão `0001_bootstrap` estabelece o baseline, `0002_orders_shipments` cria
Orders, Shipments e o registro de Carriers, e `0003_carriers_tracking` instala os
registros determinísticos Alpha/Beta e cria `carrier_event_inbox` e
`tracking_events`. A revisão `0004_notifications` cria o registro persistente das
notificações simuladas. O schema usa UUID/timestamptz nativos, JSONB/bytea para a
projeção e os bytes externos, checks textuais nomeados, FKs restritivas e os
índices do contrato.
