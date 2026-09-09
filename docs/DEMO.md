# Demonstração funcional do FulfillFlow v1.1 — incremento I

Este roteiro apresenta a jornada principal do FulfillFlow em um ambiente local e
controlado: criação de um `Order`, criação de uma `Shipment`, recebimento de
eventos de uma transportadora simulada e consulta dos efeitos pela interface.

A interface é um painel operacional interno. A aplicação não possui cadastro ou
login de usuários e não deve ser exposta diretamente à internet.

## O que será demonstrado

- criação e confirmação de um `Order`;
- criação de uma `Shipment` vinculada ao Carrier Alpha;
- envio externo de webhooks autenticados com HMAC-SHA256;
- evolução da `Shipment` até `DELIVERED`;
- timeline de `TrackingEvent`, inbox auditável e `Notification` simulada;
- conclusão automática do `Order` quando todas as suas Shipments não canceladas
  estiverem entregues;
- idempotência com a repetição do mesmo evento.

Todos os dados e secrets abaixo são exclusivamente sintéticos. Nenhuma
transportadora ou conta de e-mail real é utilizada.

## Pré-requisitos

- checkout da branch v1.1 validada;
- Docker Engine com Docker Compose;
- Python 3.13 e `uv` para executar o simulador externo;
- PowerShell;
- porta `127.0.0.1:8000` disponível.

Execute todos os comandos a partir da raiz do repositório.

## 1. Configurar o ambiente local

Na sessão do PowerShell usada para iniciar o Compose e o simulador, defina:

```powershell
$env:APP_ENV = "local"
$env:APP_PORT = "8000"
$env:POSTGRES_PASSWORD = "fulfillflow-demo-admin-password-2026"
$env:CORE_DB_PASSWORD = "fulfillflow-demo-core-password-2026"
$env:TRACKING_DB_PASSWORD = "fulfillflow-demo-tracking-password-2026"
$env:CORE_DATABASE_URL = "postgresql+psycopg://fulfillflow_core:fulfillflow-demo-core-password-2026@db:5432/fulfillflow_core"
$env:TRACKING_DATABASE_URL = "postgresql+psycopg://fulfillflow_tracking:fulfillflow-demo-tracking-password-2026@db:5432/fulfillflow_tracking"
$env:INTERNAL_API_SECRET = "fulfillflow-demo-internal-2026-local-only-72be"
$env:SESSION_SECRET = "fulfillflow-demo-session-2026-local-only-7f91"
$env:CARRIER_ALPHA_WEBHOOK_SECRET = "fulfillflow-demo-alpha-2026-local-only-a84e"
$env:CARRIER_BETA_WEBHOOK_SECRET = "fulfillflow-demo-beta-2026-local-only-b73c"
```

Cada senha de role deve coincidir com seu DSN. Use um volume v1.1 novo; não
reutilize o banco da v1.0. Os secrets Alpha e Beta precisam ser distintos e ficam
no Tracking. Core recebe somente o secret de sessão e o token interno comum.
As telas e os webhooks continuam acessíveis pela porta pública do Core.

## 2. Iniciar a aplicação

Prepare o ambiente Python do simulador e inicie o stack:

```powershell
uv sync --frozen
docker compose up --build --wait
```

Confirme que a aplicação está pronta:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

As duas respostas devem retornar HTTP 200 e `{"status":"ok"}`.

| Tela | URL |
|---|---|
| Dashboard | `http://127.0.0.1:8000/` |
| Orders | `http://127.0.0.1:8000/orders` |
| Shipments | `http://127.0.0.1:8000/shipments` |
| Carrier event inbox | `http://127.0.0.1:8000/carrier-events` |
| Notifications | `http://127.0.0.1:8000/notifications` |
| Instruções do simulador | `http://127.0.0.1:8000/simulator` |

O painel `/simulator` apresenta os comandos disponíveis, mas não envia eventos.
Os webhooks são enviados pelo processo externo
`scripts/simulate_carrier_events.py`.

## 3. Criar e confirmar um Order

1. Abra **Orders** e selecione **Create Order**.
2. Preencha o formulário com estes dados sintéticos:

   - `External reference`: `DEMO-LIVE-0001`;
   - `Recipient name`: `Cliente Demonstração 001`;
   - `Synthetic email`: `cliente.demo.001@example.test`;
   - `Postal code`: `09700-000`;
   - `City`: `São Bernardo do Campo`;
   - `State`: `SP`.

3. Selecione **Create Order** e confirme o estado inicial `CREATED`.
4. Selecione **Confirm** e confirme a mudança para `CONFIRMED`.

Uma `Shipment` só pode ser criada para um `Order` confirmado.

## 4. Criar a Shipment

1. No detalhe do `Order`, selecione **Create Shipment**.
2. Confirme que o `Order ID` está preenchido.
3. Selecione `Carrier Alpha`.
4. Informe `ALPHA-LIVE-0001` em `Tracking code`.
5. Deixe `Estimated delivery date` vazio ou use uma data sintética.
6. Selecione **Create Shipment**.
7. Confirme no detalhe:

   - Carrier `carrier-alpha`;
   - tracking code `ALPHA-LIVE-0001`;
   - estado inicial `PENDING`.

Anote o UUID da `Shipment`, pois ele poderá ser usado para filtrar as
Notifications. A timeline ainda deverá informar que não existem eventos.

## 5. Enviar os eventos do Carrier Alpha

Na mesma sessão do PowerShell que contém o secret Alpha, execute:

```powershell
uv run python scripts/simulate_carrier_events.py `
  --base-url http://127.0.0.1:8000 `
  --carrier carrier-alpha `
  --tracking-code ALPHA-LIVE-0001 `
  --scenario valid
```

O simulador envia quatro eventos:

```text
POSTED → IN_TRANSIT → OUT_FOR_DELIVERY → DELIVERED
```

Cada resposta deve apresentar HTTP 200, `"result":"APPLIED"` e
`"success":true`. A última deve apresentar
`"current_status":"DELIVERED"`. Exit code zero indica que todas as respostas
corresponderam ao contrato esperado.

## 6. Conferir os efeitos na interface

1. Atualize o detalhe da `Shipment` e confirme o estado `DELIVERED`, além de
   `Shipped at`, `Delivered at` e `Status occurred at` preenchidos.
2. Abra **Tracking timeline** e confirme os quatro estados canônicos. Todos
   devem apresentar `Application result` igual a `APPLIED`.
3. Abra **Inbox**, filtre por um `event_id` exibido pelo simulador e confirme o
   estado `PROCESSED`. O detalhe apresenta a projeção sanitizada, sem assinatura,
   secret ou `raw_body`.
4. Abra **Notifications**, filtre pelo UUID da `Shipment` e confirme quatro
   registros `SIMULATED`. Nenhum e-mail é enviado.
5. Volte ao `Order`. Como ele possui somente essa Shipment não cancelada, seu
   estado deverá ser `FULFILLED`.
6. Volte ao Dashboard e confira as contagens e os eventos recentes.

## 7. Demonstrar idempotência — opcional

Crie outro `Order` confirmado e outra Shipment Alpha em `PENDING`, usando o
tracking code `ALPHA-DUP-0001`. Em seguida, execute:

```powershell
uv run python scripts/simulate_carrier_events.py `
  --base-url http://127.0.0.1:8000 `
  --carrier carrier-alpha `
  --tracking-code ALPHA-DUP-0001 `
  --scenario duplicate
```

A primeira tentativa deve retornar `APPLIED`; a repetição do mesmo evento deve
retornar `DUPLICATE` com `original_result` igual a `APPLIED`. A repetição não
cria outro inbox, `TrackingEvent` ou `Notification`.

O simulador também oferece `out-of-order`, `unknown-status` e
`invalid-signature`. No último caso, HTTP 401 com
`INVALID_WEBHOOK_SIGNATURE` é o resultado esperado e nenhum inbox é criado.

## Como o fluxo funciona

O simulador serializa o payload uma vez, assina os mesmos bytes enviados e chama
`POST /api/v1/carriers/{carrier_code}/events`. A aplicação autentica o webhook,
preserva o inbox, normaliza o evento, registra o `TrackingEvent`, atualiza a
`Shipment`, cria uma `Notification` quando aplicável e reavalia o `Order`.
Detalhes de transações, locks e idempotência permanecem documentados em
`DESIGN.md`.

## Capturas da demonstração

As imagens abaixo foram obtidas da aplicação real FulfillFlow v1.1 — incremento I em ambiente
local controlado. Todos os dados apresentados são sintéticos.

Não use geração de imagens e não exponha secrets, assinaturas, DSN ou variáveis
de ambiente.

![Dashboard operacional do FulfillFlow v1.1 — incremento I](assets/demo/dashboard.png)

Dashboard com os estados operacionais e os eventos recentes.

![Detalhe de Order concluído](assets/demo/order-detail.png)

Order em `FULFILLED`, com sua Shipment vinculada.

![Timeline de Tracking de uma Shipment](assets/demo/tracking-timeline.png)

Linha do tempo dos eventos da Shipment e seus resultados de aplicação.

![Painel instrucional do simulador de transportadoras](assets/demo/carrier-simulator.png)

Instruções para executar o simulador externo; o painel não envia eventos.

## Encerrar o ambiente

```powershell
docker compose down --volumes
```

Esse comando remove permanentemente o banco local do Compose. Use-o somente no
ambiente sintético e descartável desta demonstração.

## Limitações

- não há autenticação de usuários, Carrier real ou envio real de e-mail;
- o bind padrão é local, em `127.0.0.1`;
- `/simulator` é somente instrucional;
- o processamento do webhook é síncrono, sem fila ou worker;
- o roteiro não substitui as suítes automatizadas;
- o roteiro não executa Locust nem produz baseline ou resultado oficial de
   benchmark.
