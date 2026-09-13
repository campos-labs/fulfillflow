# FulfillFlow — Design Architecture: v1.0.0 e alvo v1.1

| Campo | Valor |
|---|---|
| Status | v1.0.0 publicada; incremento I funcional da v1.1 implementado localmente, sem release |
| Arquitetura vigente | Core + Tracking no checkout v1.1; monólito preservado na tag v1.0.0 |
| Escopo | Contrato histórico v1.0.0 e extração delimitada de Tracking na v1.1 (§30) |
| Runtime principal | Python 3.13 / FastAPI |
| Persistência | PostgreSQL 18 |
| Última revisão | 2026-09-09 |

As seções 1–29 preservam a descrição e as restrições da v1.0.0. A seção 30 registra
as substituições aprovadas e os invariantes da arquitetura v1.1, distinguindo o
incremento funcional implementado da preparação experimental pendente. Regras não
substituídas permanecem aplicáveis. A execução
por incrementos e seu estado pertencem a `RELEASE_PLAN.md`, não a este contrato.

## 1. Objetivo

FulfillFlow é uma aplicação web interna para operação e acompanhamento do fluxo entre pedido, remessa, eventos de transportadora e entrega.

A v1.0.0 deve oferecer uma jornada funcional completa e demonstrável:

1. cadastrar um pedido;
2. criar uma ou mais remessas associadas;
3. atribuir transportadora e código de rastreamento;
4. receber webhooks autenticados de duas transportadoras simuladas;
5. preservar o evento recebido em formato original;
6. normalizar o evento para o modelo canônico;
7. validar e aplicar a transição de estado;
8. manter a timeline imutável de tracking;
9. registrar uma notificação simulada;
10. disponibilizar o resultado por API e interface web.

O produto representa um MVP operacional executado em ambiente controlado. Não é um e-commerce, uma plataforma de gestão de armazém nem um sistema pronto para exposição pública.

## 2. Drivers arquiteturais

As decisões da v1.0.0 priorizam:

- consistência transacional no processamento dos eventos;
- fronteiras modulares verificáveis;
- rastreabilidade do payload externo até o estado resultante;
- idempotência e comportamento determinístico;
- contratos HTTP estáveis e versionados;
- execução integral por containers;
- observabilidade suficiente para diagnóstico e medição;
- testes contra PostgreSQL real;
- reprodutibilidade de dados e benchmarks;
- simplicidade operacional sem substituição dos fundamentos por implementações em memória.

## 3. Limites do sistema

### 3.1 Incluído

- pedidos;
- remessas;
- cadastro fixo de duas transportadoras simuladas;
- ingestão autenticada de eventos por webhook;
- adaptação de dois formatos externos distintos;
- normalização para status canônico;
- histórico e estado atual da remessa;
- tratamento de duplicidade, atraso e transição inválida;
- notificações simuladas e persistidas;
- dashboard e telas operacionais;
- API REST `/api/v1`;
- logs estruturados, métricas e tracing;
- seeds de demonstração e benchmark;
- testes funcionais, arquiteturais e de desempenho.

### 3.2 Excluído

- autenticação de usuários, SSO, OAuth ou RBAC;
- multitenancy;
- catálogo de produtos, estoque, picking, packing ou gestão de armazém;
- pagamentos, checkout ou faturamento;
- integração com ERP;
- transportadoras reais;
- envio real de e-mail, SMS, WhatsApp ou push;
- filas, brokers e workers independentes;
- RabbitMQ, Kafka, Redis, Celery ou Taskiq;
- API Gateway;
- Kubernetes, AKS ou qualquer cloud obrigatória;
- React, Vue, Angular ou pipeline Node.js;
- MinIO, Blob Storage ou armazenamento de arquivos;
- IA, LLM ou inferência probabilística;
- reprocessamento automático em background;
- rate limiting distribuído na aplicação.

## 4. Visão arquitetural

A v1.0.0 é um monólito modular: uma aplicação FastAPI, uma imagem implantável e um banco PostgreSQL. Banco e backends opcionais de observabilidade são infraestrutura, não unidades adicionais da aplicação.

```mermaid
flowchart TB
    O["Operador"] --> W["Web UI"]
    O --> A["REST API"]
    S["Simulador de transportadora"] --> H["Webhook autenticado"]
    W --> M["Monólito FulfillFlow"]
    A --> M
    H --> M
    M --> P[("PostgreSQL")]
    M --> T["Telemetria"]
```

### 4.1 Restrições estruturais

- Não há chamadas HTTP entre módulos internos.
- Não há banco por módulo.
- Não há publicação assíncrona de eventos de negócio.
- Uma única transação pode coordenar alterações em múltiplos módulos, desde que cada alteração seja executada pelo serviço público do módulo proprietário.
- Nenhum módulo acessa models ou repositories internos de outro módulo.
- A camada web e os routers não contêm regra de negócio.
- Models ORM não são contratos de API.
- O simulador interage exclusivamente pelos endpoints públicos.

## 5. Stack aprovada

### 5.1 Aplicação

- Python 3.13;
- FastAPI;
- Uvicorn;
- Pydantic v2;
- pydantic-settings;
- SQLAlchemy 2.x com `AsyncSession`;
- psycopg 3 em modo assíncrono;
- Alembic;
- Jinja2;
- HTMX;
- Bootstrap 5.

### 5.2 Qualidade e testes

- pytest;
- pytest-asyncio;
- httpx;
- coverage.py / pytest-cov;
- Ruff para lint e formatação;
- Mypy para análise estática;
- Import Linter para contratos arquiteturais;
- Locust para testes de carga.

### 5.3 Observabilidade

- logging estruturado em JSON;
- OpenTelemetry API, SDK e instrumentações de FastAPI e SQLAlchemy;
- exportação OTLP configurável;
- Prometheus Python Client;
- Jaeger opcional no perfil de observabilidade do Compose.

### 5.4 Empacotamento e execução

- `pyproject.toml`;
- `uv`;
- `uv.lock` versionado;
- Dockerfile multi-stage;
- Docker Compose;
- GitHub Actions.

Versões patch são determinadas pelo `uv.lock`; o DESIGN fixa produtos e linhas principais, não substitui o lockfile.

## 6. Organização do repositório

```text
fulfillflow/
├── AGENTS.md
├── DESIGN.md
├── README.md
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── compose.yaml
├── compose.benchmark.yaml
├── .env.example
├── alembic.ini
├── alembic/
│   └── versions/
├── src/
│   └── fulfillflow/
│       ├── main.py
│       ├── config.py
│       ├── db/
│       │   ├── base.py
│       │   ├── session.py
│       │   └── types.py
│       ├── errors/
│       ├── observability/
│       ├── shared/
│       │   ├── clock.py
│       │   ├── ids.py
│       │   └── pagination.py
│       ├── modules/
│       │   ├── orders/
│       │   ├── shipments/
│       │   ├── carriers/
│       │   ├── tracking/
│       │   └── notifications/
│       └── web/
│           ├── routers/
│           ├── templates/
│           └── static/
├── tests/
│   ├── unit/
│   ├── architecture/
│   ├── integration/
│   └── e2e/
├── benchmarks/
│   ├── README.md
│   ├── locustfile.py
│   ├── datasets/
│   └── results/
└── scripts/
    ├── seed_demo.py
    ├── seed_benchmark.py
    └── simulate_carrier_events.py
```

### 6.1 Estrutura mínima de cada módulo

```text
module/
├── public.py
├── router.py
├── schemas.py
├── models.py
├── repository.py
├── service.py
└── domain.py
```

Arquivos sem conteúdo real não devem ser criados apenas para satisfazer a estrutura. `domain.py` é reservado para enums, regras e objetos sem dependência de infraestrutura; `public.py` expõe apenas os contratos autorizados aos demais módulos.

## 7. Módulos e dependências

### 7.1 Orders

Responsabilidades:

- criar e consultar pedidos;
- validar unicidade da referência externa;
- controlar o estado do pedido;
- expor dados mínimos necessários à criação da remessa;
- expor a operação pública idempotente de conclusão de pedido, sem consultar tabelas de Shipment.

Não depende de módulos de negócio.

### 7.2 Carriers

Responsabilidades:

- consultar transportadoras cadastradas;
- localizar o adapter pelo código da transportadora;
- validar o schema específico do payload externo;
- converter o payload externo em `CanonicalCarrierEvent`;
- mapear status externo para status canônico.

Não inicia processamento de tracking e não grava tabelas de tracking.
`adapter_key` é resolvido por allowlist estática no código; nenhum nome vindo do banco é usado em import dinâmico.

### 7.3 Shipments

Responsabilidades:

- criar e consultar remessas;
- garantir unicidade de `(carrier_id, tracking_code)`;
- controlar a máquina de estados da remessa;
- aplicar status de tracking sob bloqueio transacional;
- manter datas derivadas como `shipped_at` e `delivered_at`;
- avaliar, a partir de suas próprias remessas, se um Order pode ser concluído e solicitar a transição pela interface pública de Orders.

Pode depender apenas das interfaces públicas de Orders e Carriers.

### 7.4 Notifications

Responsabilidades:

- produzir a mensagem correspondente a uma transição aplicada;
- registrar a simulação de notificação;
- expor consultas operacionais.

Não envia mensagens para provedores externos.
Falha esperada de renderização/simulação produz registro `FAILED` e não desfaz uma transição válida de Shipment; falha de persistência continua sendo falha da Transação B.

### 7.5 Tracking

Responsabilidades:

- expor o endpoint de webhook;
- autenticar a requisição antes da persistência;
- registrar o evento bruto autenticado;
- detectar duplicidade;
- solicitar normalização ao módulo Carriers;
- localizar a remessa pelo contrato público de Shipments;
- persistir o evento canônico;
- coordenar a aplicação da transição por Shipments;
- solicitar o registro da notificação por Notifications;
- finalizar o status de processamento do inbox;
- expor a timeline.

Pode depender das interfaces públicas de Carriers, Shipments e Notifications.

```mermaid
flowchart TB
    T["Tracking"] --> C["Carriers public"]
    T --> S["Shipments public"]
    T --> N["Notifications public"]
    S --> O["Orders public"]
    S --> C
```

### 7.6 Shared

Contém somente primitivas transversais estáveis: `Clock`, geração de UUID, paginação e tipos técnicos sem semântica de negócio. Todos os módulos podem depender de Shared; Shared não depende de módulo, FastAPI, ORM ou banco.

Tempo de domínio é obtido por uma interface `Clock` injetável. Chamadas diretas a `datetime.now()` ficam restritas à implementação concreta do clock, evitando relógio real em testes.

### 7.7 Contratos arquiteturais

O Import Linter deve verificar:

- ausência de ciclos entre módulos irmãos;
- proibição de importação de `models.py` e `repository.py` fora do módulo proprietário;
- acesso externo ao módulo apenas por `public.py`, `schemas.py` quando explicitamente público, ou routers registrados em `main.py`;
- ausência de dependência dos módulos de domínio sobre `web/`;
- ausência de dependência de `domain.py` sobre FastAPI, SQLAlchemy ou infraestrutura.

Testes de integração validam o comportamento; contratos arquiteturais validam imports. Um não substitui o outro.

## 8. Modelo de domínio

### 8.1 Entidades

#### Order

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK, gerado pela aplicação |
| `external_reference` | varchar(64) | obrigatório, único |
| `recipient_name` | varchar(160) | obrigatório |
| `recipient_email` | varchar(254) | obrigatório, sintético |
| `recipient_postal_code` | varchar(16) | obrigatório |
| `recipient_city` | varchar(120) | obrigatório |
| `recipient_state` | char(2) | obrigatório, maiúsculo |
| `status` | enum textual | `CREATED`, `CONFIRMED`, `FULFILLED`, `CANCELLED` |
| `created_at` | timestamptz | UTC, obrigatório |
| `updated_at` | timestamptz | UTC, obrigatório |

#### Carrier

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `code` | varchar(32) | obrigatório, único, imutável |
| `name` | varchar(120) | obrigatório |
| `adapter_key` | varchar(64) | obrigatório, único |
| `active` | boolean | default `true` |
| `created_at` | timestamptz | UTC |
| `updated_at` | timestamptz | UTC |

O secret HMAC não é persistido. O `code` resolve o adapter e a configuração segura correspondente.

#### Shipment

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `order_id` | UUID | FK `orders.id`, obrigatório |
| `carrier_id` | UUID | FK `carriers.id`, obrigatório |
| `tracking_code` | varchar(80) | obrigatório |
| `status` | enum textual | status canônico atual |
| `status_occurred_at` | timestamptz | instante do evento que determinou o status atual |
| `status_event_received_at` | timestamptz | recepção do evento determinante; `null` enquanto `PENDING` inicial |
| `status_external_event_id` | varchar(128) | desempate determinístico; `null` enquanto `PENDING` inicial |
| `estimated_delivery_date` | date | opcional |
| `shipped_at` | timestamptz | preenchido na primeira saída logística |
| `delivered_at` | timestamptz | preenchido em `DELIVERED` |
| `created_at` | timestamptz | UTC |
| `updated_at` | timestamptz | UTC |

Constraint única: `(carrier_id, tracking_code)`.

#### CarrierEventInbox

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `carrier_id` | UUID | FK, obrigatório |
| `external_event_id` | varchar(128) | obrigatório |
| `payload_sha256` | char(64) | hash dos bytes originais |
| `raw_body` | bytea | bytes autenticados exatamente como recebidos, limite 64 KiB |
| `parsed_payload` | jsonb | JSON parseado quando válido; `null` para JSON inválido |
| `received_at` | timestamptz | horário do servidor |
| `status` | enum textual | `RECEIVED`, `PROCESSED`, `REJECTED` |
| `error_code` | varchar(64) | opcional |
| `error_detail` | text | opcional, sem secrets |
| `processed_at` | timestamptz | opcional |
| `request_id` | UUID | correlação |

Constraint única: `(carrier_id, external_event_id)`.

Uma tentativa repetida com o mesmo hash não cria nova linha. Se o inbox original estiver `RECEIVED`, a requisição pode retomar o processamento; se estiver finalizado, devolve o resultado original. Reuso de `(carrier_id, external_event_id)` com outro hash é conflito e nunca substitui o evento original.

#### TrackingEvent

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `inbox_event_id` | UUID | FK única para inbox |
| `shipment_id` | UUID | FK, obrigatório |
| `carrier_id` | UUID | FK, obrigatório |
| `external_status` | varchar(120) | valor recebido |
| `canonical_status` | enum textual | status normalizado |
| `description` | varchar(500) | opcional |
| `location` | varchar(240) | opcional |
| `occurred_at` | timestamptz | horário do evento na origem |
| `received_at` | timestamptz | horário local de ingestão |
| `application_result` | enum textual | resultado da aplicação |
| `previous_shipment_status` | enum textual | opcional |
| `resulting_shipment_status` | enum textual | obrigatório |
| `created_at` | timestamptz | UTC |

`application_result`:

- `APPLIED`;
- `NO_STATE_CHANGE`;
- `IGNORED_STALE`;
- `IGNORED_INVALID_TRANSITION`.

O registro é append-only. Correções administrativas não fazem parte da v1.0.0.

#### Notification

| Campo | Tipo | Regras |
|---|---|---|
| `id` | UUID | PK |
| `shipment_id` | UUID | FK, obrigatório |
| `tracking_event_id` | UUID | FK, único |
| `channel` | enum textual | `EMAIL` na v1.0.0 |
| `recipient` | varchar(254) | endereço sintético do pedido |
| `template_key` | varchar(80) | template determinístico |
| `message` | text | conteúdo renderizado |
| `status` | enum textual | `SIMULATED` ou `FAILED` |
| `error_detail` | text | opcional |
| `created_at` | timestamptz | UTC |
| `simulated_at` | timestamptz | opcional |

### 8.2 Relacionamentos

```mermaid
erDiagram
    ORDER ||--o{ SHIPMENT : contains
    CARRIER ||--o{ SHIPMENT : transports
    CARRIER ||--o{ CARRIER_EVENT_INBOX : sends
    SHIPMENT ||--o{ TRACKING_EVENT : receives
    CARRIER_EVENT_INBOX ||--o| TRACKING_EVENT : normalizes_to
    TRACKING_EVENT ||--o| NOTIFICATION : produces
```

### 8.3 Índices obrigatórios

- `orders(external_reference)` unique;
- `orders(status, created_at desc)`;
- `shipments(carrier_id, tracking_code)` unique;
- `shipments(order_id)`;
- `shipments(status, updated_at desc)`;
- `carrier_event_inbox(carrier_id, external_event_id)` unique;
- `carrier_event_inbox(status, received_at)`;
- `tracking_events(shipment_id, occurred_at desc, created_at desc)`;
- `tracking_events(inbox_event_id)` unique;
- `notifications(status, created_at desc)`;
- `notifications(tracking_event_id)` unique.

### 8.4 Representação física e integridade

- UUIDs usam o tipo nativo `uuid` e são gerados como UUIDv4 pela aplicação;
- timestamps usam `timestamptz`, são persistidos em UTC e convertidos apenas na apresentação;
- enums de domínio são `varchar` com `CHECK` nomeado, não PostgreSQL native enum;
- FKs de registros de negócio usam `ON DELETE RESTRICT`; exclusão em cascata não é permitida;
- `updated_at` é mantido pela aplicação na mesma transação da alteração; não há triggers de auditoria;
- `tracking_code` é armazenado e pesquisado após `strip` e conversão para maiúsculas;
- `carrier.code` é slug ASCII minúsculo e imutável;
- `external_event_id` é case-sensitive, usa somente ASCII visível sem whitespace lateral e não é reescrito depois da validação do header;
- campos externos extras são tolerados pelo adapter, mas não entram no contrato canônico;
- valores vazios após normalização são inválidos;
- todos os comprimentos declarados são validados na borda e protegidos novamente no schema;
- `raw_body` é a fonte forense; `parsed_payload` é uma projeção de consulta e não substitui os bytes originais.

## 9. Máquinas de estados

### 9.1 Order

| Estado atual | Próximos estados permitidos |
|---|---|
| `CREATED` | `CONFIRMED`, `CANCELLED` |
| `CONFIRMED` | `FULFILLED` |
| `FULFILLED` | nenhum |
| `CANCELLED` | nenhum |

Regras:

- remessas só podem ser criadas para pedido `CONFIRMED`;
- pedido só pode ser cancelado enquanto estiver `CREATED` e, portanto, antes da criação de remessas;
- pedido torna-se `FULFILLED` quando existe ao menos uma remessa não cancelada e todas as remessas não canceladas estiverem `DELIVERED`;
- pedido sem remessa não pode ser `FULFILLED`.
- o predicado de conclusão é reavaliado quando uma Shipment resulta em `DELIVERED` ou
  `CANCELLED`, tanto por evento externo quanto por cancelamento manual, sempre sob locks na ordem
  Shipment → Order.

`confirm` em pedido já `CONFIRMED` e `cancel` em pedido já `CANCELLED` são sucessos idempotentes. Ação sobre qualquer outro estado não permitido retorna 409.

### 9.2 Shipment

| Estado atual | Próximos estados permitidos |
|---|---|
| `PENDING` | `POSTED`, `IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED`, `EXCEPTION`, `CANCELLED` |
| `POSTED` | `IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED`, `EXCEPTION`, `RETURNED` |
| `IN_TRANSIT` | `OUT_FOR_DELIVERY`, `DELIVERED`, `EXCEPTION`, `RETURNED` |
| `OUT_FOR_DELIVERY` | `IN_TRANSIT`, `DELIVERED`, `EXCEPTION`, `RETURNED` |
| `EXCEPTION` | `IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED`, `RETURNED` |
| `DELIVERED` | nenhum |
| `RETURNED` | nenhum |
| `CANCELLED` | nenhum |

Regras:

- criação inicia em `PENDING`;
- `shipped_at` recebe o `occurred_at` do primeiro evento aplicado em `POSTED`, `IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED` ou `RETURNED`; `EXCEPTION` isolado não comprova postagem;
- `delivered_at` recebe o `occurred_at` do evento aplicado `DELIVERED`;
- estado terminal não regride;
- a ordem total de eventos usa a chave `(occurred_at, received_at, external_event_id)` em ordem lexicográfica;
- enquanto a Shipment estiver no `PENDING` inicial, sem evento externo determinante, o primeiro evento válido não é considerado atrasado apenas por anteceder `created_at`;
- depois do primeiro evento externo, chave anterior à chave determinante atual é registrada como `IGNORED_STALE`;
- evento com o mesmo estado e chave posterior é registrado como `NO_STATE_CHANGE` e avança os três campos de ordenação, sem alterar o status;
- transição não listada é registrada como `IGNORED_INVALID_TRANSITION`;
- saltos para estados posteriores são aceitos porque transportadoras podem omitir eventos intermediários;
- eventos `IGNORED_*` não criam notificação e não alteram Order ou Shipment;
- `NO_STATE_CHANGE` não cria notificação nem reavalia o Order.

`cancel` em Shipment já `CANCELLED` é sucesso idempotente. Cancelamento manual em qualquer estado diferente de `PENDING` ou `CANCELLED` retorna 409.
Cancelamento manual não fabrica evento de transportadora: altera a Shipment, registra log de domínio e não cria `CarrierEventInbox`, `TrackingEvent` ou Notification.

## 10. Contrato canônico de evento

Adapters retornam exclusivamente:

```python
class CanonicalCarrierEvent(BaseModel):
    external_event_id: str
    tracking_code: str
    canonical_status: ShipmentStatus
    external_status: str
    occurred_at: datetime
    description: str | None
    location: str | None
```

Invariantes:

- `occurred_at` deve possuir timezone;
- strings são normalizadas sem perda do payload bruto;
- adapter não consulta banco;
- adapter não altera estado;
- status externo desconhecido rejeita o inbox com `UNKNOWN_EXTERNAL_STATUS`;
- campos extras do fornecedor permanecem apenas em `parsed_payload` e `raw_body`.

## 11. Transportadoras simuladas

### 11.1 Carrier Alpha

Código: `carrier-alpha`.

Payload:

```json
{
  "eventId": "alpha-evt-000001",
  "trackingCode": "ALPHA000001",
  "status": "MOVING",
  "eventDate": "2026-08-28T15:20:00Z",
  "city": "São Bernardo do Campo",
  "description": "Transferência entre unidades"
}
```

Mapeamento:

| Externo | Canônico |
|---|---|
| `CREATED` | `POSTED` |
| `MOVING` | `IN_TRANSIT` |
| `OUT_FOR_DELIVERY` | `OUT_FOR_DELIVERY` |
| `DELIVERED` | `DELIVERED` |
| `PROBLEM` | `EXCEPTION` |
| `RETURNED` | `RETURNED` |

### 11.2 Carrier Beta

Código: `carrier-beta`.

Payload:

```json
{
  "id": "beta-evt-000001",
  "tracking_number": "BETA000001",
  "event": {
    "type": "hub_scan",
    "occurred_at": "2026-08-28T15:20:00-03:00",
    "details": "Recebido no centro de distribuição"
  },
  "location": {
    "city": "São Paulo",
    "state": "SP"
  }
}
```

Mapeamento:

| Externo | Canônico |
|---|---|
| `label_created` | `POSTED` |
| `hub_scan` | `IN_TRANSIT` |
| `courier_route` | `OUT_FOR_DELIVERY` |
| `completed` | `DELIVERED` |
| `delivery_issue` | `EXCEPTION` |
| `returned_origin` | `RETURNED` |

## 12. Autenticação dos webhooks

### 12.1 Headers

```text
Content-Type: application/json
X-FulfillFlow-Event-Id: <external_event_id>
X-FulfillFlow-Timestamp: <unix_seconds>
X-FulfillFlow-Signature: sha256=<hex_digest>
```

### 12.2 Assinatura

```text
signed_payload = timestamp + "." + event_id + "." + raw_body
signature = HMAC-SHA256(carrier_secret, signed_payload)
```

Em bytes:

```python
signed_payload = (
    timestamp.encode("ascii")
    + b"."
    + event_id.encode("utf-8")
    + b"."
    + raw_body
)
```

Regras:

- secret distinto por transportadora;
- secrets fornecidos apenas por variáveis de ambiente;
- comparação por `hmac.compare_digest`;
- timestamp representado por inteiro decimal de segundos Unix, sem espaços;
- `event_id` não vazio, com no máximo 128 caracteres;
- cada header autenticado ocorre exatamente uma vez; ausência ou duplicidade é inválida;
- assinatura em 64 dígitos hexadecimais após o prefixo `sha256=`;
- tolerância de timestamp padrão de 300 segundos;
- corpo máximo padrão de 65.536 bytes;
- HMAC calculado sobre os bytes originais, antes do parse JSON;
- `event_id` do header deve coincidir com o identificador extraído pelo adapter;
- assinatura, secret e corpo completo não são gravados em logs;
- requisição não autenticada não é persistida;
- ausência de carrier, headers, assinatura válida ou timestamp válido retorna erro antes do acesso de negócio.

### 12.3 Simulador

O simulador externo fica em `scripts/simulate_carrier_events.py` e recebe base URL, carrier,
tracking code e cenário por argumento ou ambiente. O secret não é aceito por argumento nem por
variável genérica: `carrier-alpha` usa exclusivamente `CARRIER_ALPHA_WEBHOOK_SECRET` e
`carrier-beta` usa exclusivamente `CARRIER_BETA_WEBHOOK_SECRET`.

Os nomes e as semânticas normativas dos cenários são:

| Cenário | Semântica esperada |
|---|---|
| `valid` | envia, no schema externo do carrier escolhido, a sequência válida que normaliza para `POSTED -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED`; cada resposta retorna 200 e `APPLIED`, terminando em `DELIVERED` |
| `duplicate` | envia um evento válido e repete exatamente `raw_body`, event ID, timestamp do header e assinatura; a repetição retorna 200 e `DUPLICATE` |
| `out-of-order` | envia um evento válido e depois outro evento válido com chave de ordenação anterior; o segundo retorna 200 e `IGNORED_STALE` |
| `unknown-status` | envia payload autenticado no schema do carrier com status externo desconhecido; retorna 422 e `UNKNOWN_EXTERNAL_STATUS` |
| `invalid-signature` | envia payload sintaticamente válido com assinatura inválida; retorna 401 e `INVALID_WEBHOOK_SIGNATURE`, sem persistência |

Quando seed, IDs e relógio forem fornecidos, a sequência é determinística; na execução manual, os
defaults geram IDs únicos e timestamp dentro da janela aceita. O payload é serializado uma única vez
e exatamente os mesmos bytes assinados são enviados.

O processo termina com exit code zero somente quando todas as respostas observadas coincidem com a
semântica do cenário. Assim, 422 em `unknown-status` e 401 em `invalid-signature` são resultados
esperados e bem-sucedidos do simulador apenas quando acompanhados pelos respectivos códigos de
problema; resposta inesperada, timeout ou erro de rede resulta em exit code diferente de zero.

O simulador mostra apenas status HTTP e resultado operacional sanitizado, nunca secret ou
assinatura. Ele não importa código da aplicação, não acessa PostgreSQL e interage exclusivamente por
HTTP com `POST /api/v1/carriers/{carrier_code}/events`.

## 13. Processamento de webhook

```mermaid
sequenceDiagram
    participant C as Carrier
    participant T as Tracking
    participant DB as PostgreSQL
    participant S as Shipments
    participant N as Notifications

    C->>T: POST webhook assinado
    T->>T: Limite, timestamp e HMAC
    T->>DB: Inserir inbox RECEIVED
    alt Evento duplicado
        DB-->>T: Conflito de unicidade
        T-->>C: 200 DUPLICATE
    else Evento novo
        T->>T: Normalizar pelo adapter
        T->>S: Aplicar status com lock
        S->>DB: Atualizar Shipment
        T->>DB: Inserir TrackingEvent
        opt Transição aplicada
            T->>N: Registrar notificação
            N->>DB: Inserir Notification
        end
        T->>DB: Marcar inbox final
        T-->>C: 200 resultado
    end
```

### 13.1 Fronteiras transacionais

#### Transação A — recepção

1. validar autenticação fora do banco;
2. inserir `CarrierEventInbox(RECEIVED)` com `raw_body`, hash e metadados de recepção;
3. realizar commit;
4. em conflito único, carregar o evento original;
5. se o hash divergir, responder conflito sem processar o novo body;
6. se o hash coincidir, seguir para a Transação B usando o inbox original.

O commit independente garante preservação do evento autenticado mesmo se o processamento de negócio falhar.

#### Transação B — processamento de negócio

1. carregar e bloquear o inbox com `SELECT ... FOR UPDATE`;
2. se estiver `PROCESSED`, devolver `DUPLICATE` com o resultado original;
3. se estiver `REJECTED`, devolver novamente a rejeição registrada;
4. se estiver `RECEIVED`, decodificar e validar JSON, persistindo `parsed_payload` quando válido;
5. normalizar pelo adapter;
6. localizar Shipment por carrier e tracking code;
7. bloquear Shipment com `SELECT ... FOR UPDATE`;
8. determinar resultado usando a chave total do evento;
9. inserir TrackingEvent;
10. se `APPLIED`, atualizar estado e campos de ordenação da Shipment;
11. se `NO_STATE_CHANGE`, atualizar apenas os campos de ordenação da Shipment;
12. se `APPLIED`, registrar Notification pelo serviço proprietário;
13. se o estado resultante for `DELIVERED` ou `CANCELLED`, bloquear o Order pela interface pública de Orders;
14. somente nesses casos, Shipments avalia todas as remessas não canceladas do Order e, quando aplicável, solicita sua conclusão;
15. marcar inbox `PROCESSED`;
16. commit atômico.

#### Falha de processamento

Após erro permanente de schema ou negócio e rollback da Transação B, uma transação curta deve marcar o inbox como `REJECTED`, com `parsed_payload` quando disponível, `error_code`, detalhe sanitizado e `processed_at`.

JSON autenticado, porém sintaticamente inválido, permanece preservado em `raw_body` e é rejeitado com `INVALID_JSON`. Erros de autenticação não geram inbox.

Falha transitória de infraestrutura não converte o inbox em `REJECTED`: ele permanece `RECEIVED` e a resposta é 503. Nova entrega idêntica retoma a Transação B. Essa retomada síncrona é obrigatória porque não existe worker de reprocessamento na v1.0.0.

Falha inesperada da aplicação depois da Transação A retorna 500 e também preserva `RECEIVED`, para não classificar um defeito interno como rejeição definitiva do fornecedor.

### 13.2 Concorrência

- `AsyncSession` por requisição;
- isolamento PostgreSQL `READ COMMITTED`;
- lock pessimista da Shipment durante aplicação do status;
- unique constraint como autoridade final de idempotência;
- nenhum check-then-insert sem tratamento do conflito no banco;
- concorrência simultânea do mesmo `event_id` deve gerar um único inbox;
- concorrência de eventos distintos para a mesma Shipment deve serializar a transição;
- concorrência das últimas entregas de Shipments diferentes do mesmo Order deve serializar pelo lock do Order e produzir exatamente uma transição para `FULFILLED`;
- locks são adquiridos na ordem inbox, Shipment e Order; código algum pode inverter essa ordem;
- retries de erro transitório devem ser limitados e ocorrer apenas na camada de persistência da mesma requisição; não existe fila de retry.

## 14. API REST

### 14.1 Convenções

- prefixo `/api/v1`;
- JSON UTF-8;
- nomes de campos em `snake_case`;
- timestamps ISO 8601 com timezone;
- paginação por `page` e `page_size`;
- `page_size` default 25, máximo 100;
- ordenação estável com desempate por UUID;
- schemas distintos para create, update, read e list;
- resposta de erro em `application/problem+json`, compatível com RFC 9457;
- `X-Request-ID` devolvido em todas as respostas;
- rotas externas não expõem models ORM.

### 14.2 Erro padronizado

```json
{
  "type": "https://fulfillflow.local/problems/invalid-transition",
  "title": "Invalid shipment transition",
  "status": 409,
  "code": "INVALID_SHIPMENT_TRANSITION",
  "detail": "Transition DELIVERED -> IN_TRANSIT is not allowed.",
  "request_id": "9e443f7d-440f-4918-aa0f-fc9b753f2c70",
  "errors": []
}
```

Códigos estáveis:

- `VALIDATION_ERROR`;
- `RESOURCE_NOT_FOUND`;
- `RESOURCE_CONFLICT`;
- `INVALID_ORDER_TRANSITION`;
- `INVALID_SHIPMENT_TRANSITION`;
- `PAYLOAD_TOO_LARGE`;
- `INVALID_WEBHOOK_SIGNATURE`;
- `STALE_WEBHOOK_TIMESTAMP`;
- `INVALID_JSON`;
- `EVENT_ID_MISMATCH`;
- `EVENT_ID_PAYLOAD_CONFLICT`;
- `UNKNOWN_EXTERNAL_STATUS`;
- `SHIPMENT_NOT_FOUND_FOR_TRACKING`;
- `DATABASE_UNAVAILABLE`;
- `UNSUPPORTED_MEDIA_TYPE`;
- `INTERNAL_ERROR`.

Handlers globais convertem inclusive validações do FastAPI/Pydantic, 404, 405 e falhas conhecidas para o mesmo formato problem detail; não deve escapar o schema de erro padrão do framework nas rotas públicas.

### 14.3 Semântica HTTP do webhook

| Condição | HTTP | Persistência | Resultado |
|---|---:|---|---|
| processado e aplicado | 200 | inbox, tracking, alteração e notificação | `APPLIED` |
| mesmo estado | 200 | inbox e tracking | `NO_STATE_CHANGE` |
| evento atrasado | 200 | inbox e tracking | `IGNORED_STALE` |
| transição inválida | 200 | inbox e tracking | `IGNORED_INVALID_TRANSITION` |
| `event_id` e hash duplicados, inbox processado | 200 | nenhuma linha adicional | `DUPLICATE` com resultado original |
| `event_id` e hash duplicados, inbox recebido | resultado da retomada | reutiliza o inbox original | processamento normal |
| `event_id` já usado com outro hash | 409 | nenhuma linha adicional | `EVENT_ID_PAYLOAD_CONFLICT` |
| carrier desconhecido ou inativo | 404 | nenhuma | problem detail |
| assinatura ausente ou inválida | 401 | nenhuma | problem detail |
| timestamp expirado ou futuro fora da janela | 401 | nenhuma | problem detail |
| body acima do limite | 413 | nenhuma | problem detail |
| media type diferente de `application/json` | 415 | nenhuma | problem detail |
| JSON inválido após autenticação | 422 | inbox `REJECTED` com raw bytes | problem detail |
| payload inválido, status desconhecido ou ID divergente | 422 | inbox `REJECTED` | problem detail |
| tracking code não localizado | 422 | inbox `REJECTED` | problem detail |
| indisponibilidade de banco antes da recepção | 503 | nenhuma confirmação | problem detail |

Respostas 200 confirmam que o evento não precisa ser reenviado, mesmo quando não alterou o estado. Respostas 401, 413, 415 e 422 não devem ser repetidas sem correção do remetente; se o body mudar, a correção usa novo `external_event_id`. Em 500 ou 503, o remetente pode repetir os mesmos bytes com o mesmo ID.

### 14.4 Rotas

#### Orders

| Método | Rota | Resultado |
|---|---|---|
| POST | `/api/v1/orders` | 201, cria Order em `CREATED` |
| GET | `/api/v1/orders` | 200, lista paginada e filtrável |
| GET | `/api/v1/orders/{order_id}` | 200, detalhe com resumo das remessas |
| POST | `/api/v1/orders/{order_id}/confirm` | 200, `CREATED -> CONFIRMED` |
| POST | `/api/v1/orders/{order_id}/cancel` | 200 ou 409 |

Request de criação:

```json
{
  "external_reference": "ORDER-2026-000001",
  "recipient": {
    "name": "Cliente Demonstração 001",
    "email": "cliente001@example.test",
    "postal_code": "09700-000",
    "city": "São Bernardo do Campo",
    "state": "SP"
  }
}
```

#### Shipments

| Método | Rota | Resultado |
|---|---|---|
| POST | `/api/v1/shipments` | 201, cria Shipment em `PENDING` |
| GET | `/api/v1/shipments` | 200, lista paginada e filtrável |
| GET | `/api/v1/shipments/{shipment_id}` | 200, detalhe operacional |
| POST | `/api/v1/shipments/{shipment_id}/cancel` | 200 ou 409 |
| GET | `/api/v1/shipments/{shipment_id}/tracking` | 200, timeline ordenada |

Request de criação:

```json
{
  "order_id": "0dfc9d7d-d338-417f-9dcc-b0e6cb11e47d",
  "carrier_code": "carrier-alpha",
  "tracking_code": "ALPHA000001",
  "estimated_delivery_date": "2026-09-03"
}
```

Filtros mínimos de listagem:

- `status`;
- `carrier_code`;
- `order_external_reference`;
- `tracking_code`;
- `created_from`;
- `created_to`.

Toda listagem JSON retorna:

```json
{
  "items": [],
  "page": 1,
  "page_size": 25,
  "total": 0
}
```

#### Carrier events

| Método | Rota | Resultado |
|---|---|---|
| POST | `/api/v1/carriers/{carrier_code}/events` | 200, resultado do processamento |
| GET | `/api/v1/carrier-events` | 200, consulta operacional do inbox |
| GET | `/api/v1/carrier-events/{inbox_event_id}` | 200, detalhe sanitizado pelo UUID interno do inbox |

Resposta aplicada:

```json
{
  "external_event_id": "alpha-evt-000001",
  "inbox_event_id": "74cbbb9f-3f15-43f0-8917-76cd843cf0f7",
  "tracking_event_id": "b6c22f99-8f17-44ca-92a4-c4c9db089c70",
  "result": "APPLIED",
  "shipment_id": "c622f547-27f2-465b-9036-cd181c4d3f58",
  "previous_status": "POSTED",
  "current_status": "IN_TRANSIT",
  "request_id": "9e443f7d-440f-4918-aa0f-fc9b753f2c70"
}
```

Resposta duplicada de inbox já processado retorna 200, `result: DUPLICATE`, o `original_result` e os IDs do processamento original. Repetição de inbox rejeitado devolve o mesmo problem detail registrado. Reuso do ID com conteúdo diferente retorna 409.

#### Notifications

| Método | Rota | Resultado |
|---|---|---|
| GET | `/api/v1/notifications` | 200, lista paginada e filtrável |
| GET | `/api/v1/notifications/{notification_id}` | 200, detalhe |

Carrier events aceitam filtros por `carrier_code`, `status`, `external_event_id`, `received_from` e `received_to`. Notifications aceitam `status`, `shipment_id`, `created_from` e `created_to`.

## 15. Interface web

### 15.1 Estratégia

- HTML renderizado pelo servidor;
- Jinja2 com autoescape;
- HTMX apenas para requisições relativas à própria aplicação;
- Bootstrap e HTMX mantidos em `web/static`;
- nenhuma dependência de CDN em runtime;
- formulários e tabelas usam os mesmos serviços de aplicação da API, sem chamadas HTTP internas;
- respostas HTMX retornam fragments; navegação convencional retorna páginas completas;
- responses que variam por `HX-Request` enviam `Vary: HX-Request`.

### 15.2 Telas obrigatórias

- dashboard com contagem por status e eventos recentes;
- lista de Orders;
- criação e detalhe de Order;
- confirmação e cancelamento de Order;
- lista de Shipments com filtros;
- criação e detalhe de Shipment;
- timeline de Tracking;
- lista e detalhe de CarrierEventInbox;
- lista e detalhe somente leitura de Notifications;
- painel instrucional do simulador externo para os dois carriers.

O dashboard apresenta contagens de Orders, Shipments, CarrierEventInbox e Notifications agrupadas
por seus respectivos status, além dos registros mais recentes do inbox em projeção sanitizada. O
limite de apresentação dos registros recentes é detalhe local de implementação.

O painel `/simulator` documenta como executar `scripts/simulate_carrier_events.py`; ele não dispara
eventos, não recebe nem renderiza secrets e não cria uma chamada HTTP da aplicação para ela mesma.

### 15.3 Rotas HTML

| Método | Rota | Finalidade |
|---|---|---|
| GET | `/` | dashboard operacional |
| GET | `/orders` | lista de Orders |
| GET | `/orders/new` | formulário de criação de Order |
| POST | `/orders` | criação de Order |
| GET | `/orders/{order_id}` | detalhe de Order e composição com suas Shipments |
| POST | `/orders/{order_id}/confirm` | confirmação de Order |
| POST | `/orders/{order_id}/cancel` | cancelamento de Order |
| GET | `/shipments` | lista filtrável de Shipments |
| GET | `/shipments/new` | formulário de criação de Shipment |
| POST | `/shipments` | criação de Shipment |
| GET | `/shipments/{shipment_id}` | detalhe operacional de Shipment |
| POST | `/shipments/{shipment_id}/cancel` | cancelamento de Shipment |
| GET | `/shipments/{shipment_id}/tracking` | timeline de Tracking da Shipment |
| GET | `/carrier-events` | lista operacional do inbox |
| GET | `/carrier-events/{inbox_event_id}` | detalhe sanitizado do inbox |
| GET | `/notifications` | lista de Notifications |
| GET | `/notifications/{notification_id}` | detalhe somente leitura de Notification |
| GET | `/simulator` | instruções do simulador externo |
| GET | `/static/{path}` | assets locais versionados |

Requisições HTMX usam essas mesmas rotas e podem receber fragments; não existem endpoints HTML
paralelos apenas para fragments. `/shipments/new` aceita `order_id` como query parameter para
pré-preencher o Order associado.

### 15.4 Restrições

- nenhuma regra de domínio em templates;
- ações mutáveis nunca usam GET;
- formulários mutáveis incluem token CSRF vinculado à sessão assinada;
- mensagens de erro apresentam `request_id`;
- dados de usuário são escapados;
- UI não expõe raw secrets, assinatura ou configuração interna;
- `parsed_payload` pode ser exibido formatado no detalhe operacional do ambiente controlado;
- `raw_body` inválido deve ser apresentado como texto escapado e truncado para a UI, nunca interpretado como HTML;
- headers sensíveis nunca são exibidos.

## 16. Configuração

Configuração central por `BaseSettings`, validada no startup.

| Variável | Obrigatória | Default local | Finalidade |
|---|---:|---|---|
| `APP_ENV` | não | `local` | `local`, `test`, `benchmark` |
| `APP_NAME` | não | `FulfillFlow` | identificação |
| `APP_HOST` | não | `0.0.0.0` | bind interno |
| `APP_PORT` | não | `8000` | porta interna |
| `APP_TIMEZONE` | não | `America/Sao_Paulo` | apresentação |
| `DATABASE_URL` | sim | — | DSN PostgreSQL assíncrona |
| `DB_POOL_SIZE` | não | `10` | conexões persistentes por processo |
| `DB_MAX_OVERFLOW` | não | `0` | conexões extras por processo |
| `DB_POOL_TIMEOUT_SECONDS` | não | `5` | espera por conexão |
| `DB_STATEMENT_TIMEOUT_MS` | não | `5000` | limite por statement |
| `LOG_LEVEL` | não | `INFO` | nível de log |
| `LOG_FORMAT` | não | `json` | `json` ou `console` |
| `SESSION_SECRET` | sim | — | assinatura da sessão e token CSRF da UI |
| `CARRIER_ALPHA_WEBHOOK_SECRET` | sim | — | HMAC Alpha |
| `CARRIER_BETA_WEBHOOK_SECRET` | sim | — | HMAC Beta |
| `WEBHOOK_SIGNATURE_TOLERANCE_SECONDS` | não | `300` | janela anti-replay |
| `MAX_WEBHOOK_BODY_BYTES` | não | `65536` | limite do body |
| `METRICS_ENABLED` | não | `true` | `/metrics` |
| `OTEL_ENABLED` | não | `false` | tracing |
| `OTEL_SERVICE_NAME` | não | `fulfillflow` | resource name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | condicional | — | destino OTLP |
| `OTEL_TRACES_SAMPLER` | não | `parentbased_traceidratio` | política de sampling |
| `OTEL_TRACES_SAMPLER_ARG` | não | `1.0` local | amostragem |
| `SEED_RANDOM_SEED` | não | `20260828` | dados determinísticos |

Regras:

- a aplicação falha no startup se configuração obrigatória estiver ausente;
- `DATABASE_URL` usa o dialect `postgresql+psycopg` e nunca aponta para fallback SQLite;
- secrets vazios ou triviais são rejeitados fora de `test`; os secrets dos carriers também devem ser distintos;
- `.env.example` contém apenas placeholders;
- testes substituem settings por fixture explícita;
- não há acesso disperso a `os.environ` fora de `config.py`.

## 17. Segurança

### 17.1 Modelo de exposição

- ambiente controlado;
- porta publicada em `127.0.0.1` por padrão no Compose;
- nenhuma garantia de segurança para exposição direta à internet;
- UI e APIs operacionais não possuem autenticação na v1.0.0;
- CORS desabilitado por padrão;
- dados exclusivamente sintéticos.

### 17.2 Controles incluídos

- HMAC-SHA256 por transportadora;
- proteção de replay por timestamp;
- idempotência por constraint;
- limite de payload antes do parse;
- validação Pydantic estrita;
- queries parametrizadas pelo ORM;
- autoescape Jinja2;
- proteção CSRF em formulários HTML mutáveis, com cookie `HttpOnly` e `SameSite=Lax`;
- CSRF não se aplica à API JSON nem aos webhooks, que não usam autenticação por cookie;
- headers `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` e política CSP compatível com os assets locais;
- container executado por usuário não-root;
- secrets apenas por ambiente;
- sanitização de logs e erros;
- dependências bloqueadas por lockfile;
- nenhuma credencial nos seeds.

### 17.3 Rate limiting

Não há limiter de negócio na aplicação porque:

- o ambiente não é público;
- limiter em memória não é consistente em múltiplos processos;
- limiter distribuído adicionaria infraestrutura fora do escopo;
- respostas 429 contaminariam o benchmark de throughput.

Proteção de exposição pública deve ocorrer externamente. O benchmark opera abaixo de qualquer limite técnico de concorrência configurado no servidor.

## 18. Persistência e migrations

- PostgreSQL 18 é a única persistência suportada;
- SQLite é proibido em desenvolvimento, teste e benchmark;
- migrations Alembic são a única forma de criar ou alterar schema;
- `Base.metadata.create_all()` não é usado em runtime nem teste de integração;
- toda migration gerada é revisada antes de execução;
- nomes de constraints e índices são determinísticos;
- downgrade é implementado quando a reversão for segura; dados de referência cuja procedência não possa ser provada são retidos para evitar perda, e migrations destrutivas devem documentar irreversibilidade;
- CI executa `alembic upgrade head` sobre banco vazio;
- CI verifica ausência de diferenças não migradas entre metadata e head;
- sessão por requisição e transações explícitas;
- nenhum repository executa commit autônomo dentro de uma transação coordenada;
- repositories podem `flush`; o service coordenador decide commit/rollback.

## 19. Observabilidade

### 19.1 Logging

Formato JSON em runtime e benchmark. Campos mínimos:

- `timestamp`;
- `level`;
- `event`;
- `service`;
- `environment`;
- `request_id`;
- `trace_id`;
- `span_id`;
- `route`;
- `method`;
- `status_code`;
- `duration_ms`.

Eventos de negócio estruturados:

- `carrier_event.auth_failed`;
- `carrier_event.received`;
- `carrier_event.duplicate`;
- `carrier_event.rejected`;
- `tracking_event.applied`;
- `tracking_event.no_state_change`;
- `tracking_event.ignored_stale`;
- `tracking_event.ignored_invalid_transition`;
- `notification.simulated`;
- `notification.failed`;
- `request.completed`;
- `request.failed`.

Não registrar:

- secrets;
- assinatura completa;
- recipient completo em eventos de infraestrutura;
- corpo integral do webhook em logs;
- stack trace em resposta HTTP.

### 19.2 Request ID

- aceitar `X-Request-ID` apenas se UUID válido;
- gerar UUID quando ausente ou inválido;
- devolver `X-Request-ID`;
- propagar para logs, inbox e spans;
- não usar request ID como label Prometheus.

### 19.3 Métricas

Métricas obrigatórias:

```text
fulfillflow_http_requests_total{method,route,status_class}
fulfillflow_http_request_duration_seconds{method,route}
fulfillflow_carrier_events_total{carrier,result}
fulfillflow_tracking_transitions_total{from_status,to_status,result}
fulfillflow_notifications_total{channel,status}
fulfillflow_database_errors_total{operation}
```

Regras:

- histogramas com buckets declarados no código;
- labels limitados a conjuntos finitos;
- proibido usar UUID, tracking code, event ID, request ID ou path bruto como label;
- `/metrics` pode ser desabilitado por ambiente;
- métricas não substituem logs de auditoria.

### 19.4 Tracing

- instrumentação de FastAPI e SQLAlchemy;
- spans explícitos para autenticação, inbox, normalização, lock/aplicação e notificação;
- atributos não contêm secrets ou payload completo;
- exportação OTLP ativada por ambiente;
- Jaeger é opcional no perfil local de observabilidade;
- sampling deve permanecer fixo durante uma campanha de benchmark.

### 19.5 Health checks

`GET /health/live`:

- não acessa banco;
- retorna 200 enquanto o processo e event loop estiverem responsivos.

`GET /health/ready`:

- executa consulta mínima ao PostgreSQL;
- depende de um gate de startup que tenha confirmado o head de migration compatível;
- usa flag interna `schema_ready` após essa validação, sem consultar `alembic_version` a cada probe;
- retorna 503 se a aplicação não puder atender tráfego.

## 20. Testes

### 20.1 Unitários

Cobrir sem banco:

- máquinas de estados;
- normalização Alpha e Beta;
- status desconhecido;
- composição e verificação HMAC;
- assinatura sobre raw bytes com whitespace e Unicode preservados;
- janela de timestamp;
- templates de notificação;
- validação de schemas;
- regras de Order e Shipment;
- ordenação determinística de eventos com timestamps iguais.

### 20.2 Arquiteturais

- contratos Import Linter;
- ausência de ciclos;
- imports proibidos;
- domínio sem dependência de framework;
- routers sem imports de repositories de outros módulos.

### 20.3 Integração

Executar contra PostgreSQL real e banco isolado:

- repositories;
- constraints e índices relevantes;
- migrations desde banco vazio;
- preservação byte a byte de `raw_body` e projeção `parsed_payload`;
- transações A e B;
- rollback de falha de negócio;
- persistência de inbox rejeitado;
- aplicação com `SELECT FOR UPDATE`;
- concorrência do mesmo event ID;
- conflito do mesmo event ID com outro payload;
- retomada de inbox `RECEIVED` após falha transitória;
- concorrência de eventos diferentes da mesma Shipment;
- concorrência das últimas entregas de Shipments diferentes do mesmo Order;
- conclusão automática do Order;
- geração única de Notification.

### 20.4 API

- criação, confirmação, consulta e cancelamento de Order;
- criação, consulta e cancelamento de Shipment;
- webhook Alpha válido;
- webhook Beta válido;
- assinatura inválida;
- timestamp expirado;
- payload excedido;
- media type inválido e JSON autenticado inválido;
- evento duplicado;
- conflito de event ID com body diferente;
- divergência entre event ID do header e do payload;
- status desconhecido;
- tracking code inexistente;
- evento stale;
- transição inválida;
- paginação, filtros e erros padronizados;
- propagação de request ID.

### 20.5 E2E

Cenário obrigatório:

1. criar Order;
2. confirmar Order;
3. criar Shipment;
4. enviar sequência `POSTED -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED`;
5. consultar timeline;
6. validar Shipment entregue;
7. validar Order concluído;
8. validar notificações simuladas;
9. validar eventos, logs correlacionáveis e métricas incrementadas.

### 20.6 UI

- smoke tests das páginas obrigatórias;
- formulários válidos e inválidos;
- rejeição de formulário mutável sem CSRF válido;
- fragments HTMX;
- `Vary: HX-Request`;
- autoescape de conteúdo fornecido pelo usuário.

### 20.7 Qualidade mínima

- cobertura global mínima de 80%;
- regras de transição, adapters, HMAC e idempotência integralmente cobertas;
- Ruff sem violações;
- formatação Ruff estável;
- Mypy sem erros no código da aplicação;
- nenhum teste depende de ordem de execução ou relógio real não controlado.

## 21. Seeds e dados sintéticos

### 21.1 Seed de demonstração

- 25 Orders;
- 50 Shipments;
- 2 Carriers;
- ao menos 5 Shipments em cada um dos oito estados operacionais;
- timelines coerentes;
- notificações sintéticas;
- seed fixa `20260828`.

### 21.2 Dataset de benchmark

- 1.000 Orders;
- 1.500 Shipments;
- 15.000 TrackingEvents iniciais;
- distribuição de carriers 50/50;
- distribuição de status documentada no manifest;
- referências e UUIDs determinísticos a partir da seed;
- arquivo de dados ou manifest com SHA-256;
- nenhuma informação pessoal real.

### 21.3 Invariantes

- execução repetida sobre banco limpo produz o mesmo dataset lógico;
- seed de benchmark não chama APIs externas;
- dataset lógico é independente da forma física de carga;
- qualquer mudança material no dataset gera nova versão e invalida resultados anteriores até repetição da baseline.

## 22. Benchmark

Detalhes operacionais completos ficam em `benchmarks/README.md`; este DESIGN fixa os invariantes.

### 22.1 Perfis

#### Leitura de timeline

- consulta de Shipment e Tracking;
- IDs selecionados determinísticamente do dataset;
- sem mutação.

#### Ingestão de eventos

- eventos HMAC válidos;
- external event IDs únicos por execução;
- sequência canônica controlada;
- mede processamento completo, incluindo persistência e notificação.

#### Operação mista

- 50% consulta de Shipment/timeline;
- 30% ingestão de evento;
- 20% listagem filtrada do dashboard.

### 22.2 Protocolo mínimo

- execução headless;
- aquecimento de 60 segundos não contabilizado;
- namespace determinístico de IDs distinto entre aquecimento e janela medida;
- reset das estatísticas do Locust no fim do aquecimento;
- janela medida de 5 minutos;
- pelo menos 5 repetições por perfil e carga;
- banco restaurado para o mesmo estado antes de cada repetição;
- preparação executada por argv sem shell, com projeto Compose e banco confirmados literalmente:
  remove apenas os recursos e o volume do projeto de benchmark, sobe os três serviços sem build,
  aplica migrations pelo Compose, carrega atomicamente o dataset autenticado em banco vazio e só
  retorna após repetir as verificações integrais de ambiente, schema e conteúdo inicial; falhas
  tentam cleanup do mesmo projeto e nunca exibem DSN, secrets ou saídas brutas;
- estabilização declarada por `stabilization_seconds` em toda repetição, após preparação e
  verificação do banco e antes do warm-up; positiva em campanha oficial, podendo ser zero em
  fixtures não oficiais; início, fim e duração monotônica observada registrados no metadata;
- dependências bloqueadas pelo mesmo lockfile da release;
- mesma configuração de workers, pool de conexões, recursos e sampling;
- nenhuma outra carga relevante no host;
- resultados exportados em CSV;
- amostras de CPU, memória e conexões do PostgreSQL coletadas pelo mesmo sampler nas duas
  fases, em `warmup/resources.csv` e `resources.csv`, sem misturar estatísticas;
- arquivos de recursos obrigatórios, com ciclos completos de app/PostgreSQL/loadgen e
  checksums; intervalo declarado de coleta de um segundo, com cadência real auditável;
- timeouts finitos dos processos estritamente maiores que `60 + drain_seconds` no warm-up
  e `300 + drain_seconds` na measurement, sem margem numérica adicional imposta;
- registro de hardware, SO, Docker, versões, commit e horário; identidade estável observada
  uma vez e estado dinâmico observado antes do warm-up de cada repetição.

O host oficial é Windows/WSL2. O manifest declara SO/versão/build, modelo da CPU, núcleos
físicos/lógicos, RAM física, versões Docker Engine/Compose, versão/kernel WSL2 e CPUs/RAM
efetivas da VM Docker. Declara também alimentação AC, GUID do plano de energia e quantidade
de containers concorrentes, excluindo os três containers verificados da campanha. Esses
campos são essenciais: ausência ou divergência impede campanha oficial. Expectativas
declaradas também são verificadas em execuções não oficiais.

`docker_memory_bytes` é a única exceção à igualdade exata: o manifest pode declarar uma tolerância
absoluta em bytes e a comparação inclusiva aceita `abs(observado - esperado) <= tolerância`. Expected,
observed e tolerance permanecem registrados em bytes exatos; tolerância ausente mantém igualdade
exata e valor acima do limite falha fechado. Nenhum outro campo aceita tolerância. O valor e a
tolerância oficiais serão congelados após medições entre reinicializações do WSL2 e serão idênticos
nas campanhas v1.0 e v1.1.

RAM disponível, commit/limite de commit, pagefile alocado/usado e swap WSL total/livre são
observações dinâmicas, sem thresholds automáticos ainda não calibrados. Indisponibilidade
não essencial é `not_confirmed`; metadata separa `expected`, `observed` e `matches` (nulo
quando não há comparação). Não registrar hostname, usuário, serial, IP/MAC, caminhos pessoais,
lista geral de processos, secrets ou sensores proprietários. O probe usa comandos somente
leitura e nunca altera configurações. CI valida esse contrato apenas com executores simulados.

Na comparação arquitetural principal, o orçamento agregado de CPU e memória da camada de
aplicação e o da camada de dados permanecem fixos separadamente. Serviços extraídos dividem
o orçamento da aplicação; bancos adicionais dividem o de dados, incluindo overhead de
distribuição. O loadgen tem orçamento separado e idêntico. O pool total de conexões não é
multiplicado por componente. A v1.0 mantém um worker Uvicorn. Ensaios suplementares com
recursos por componente devem ser identificados separadamente da comparação principal.

### 22.3 Métricas

- latência p50 e p95;
- throughput;
- taxa de erro;
- respostas por código HTTP;
- CPU e memória do app;
- CPU e memória do PostgreSQL;
- conexões ativas;
- quantidade de eventos aplicados, ignorados, duplicados e rejeitados.

### 22.4 Congelamento

Após o primeiro benchmark válido:

- contrato externo `/api/v1` utilizado pelo Locust fica congelado para a campanha comparativa;
- schemas e conteúdo lógico dos payloads ficam congelados;
- pesos, usuários, spawn rate, duração e warm-up ficam congelados;
- dataset e manifest ficam congelados;
- alteração corretiva material no harness exige repetir todas as baselines afetadas;
- arquivos podem receber correções que não mudem semântica, desde que documentadas;
- congelamento recai sobre protocolo e dados, não sobre o hash isolado do script.

### 22.5 Interferências proibidas

- rate limit produzindo 429 durante medição;
- tracing ou logging com configuração diferente entre repetições comparadas;
- cache aquecido em apenas parte das execuções;
- crescimento não controlado do banco entre repetições;
- payloads ou rotas diferentes sob o mesmo nome de cenário;
- mudança de quantidade de workers sem nova campanha;
- otimização aplicada apenas a uma execução da mesma release.

## 23. Docker e runtime

### 23.1 Imagem

- base `python:3.13-slim-trixie`, com digest fixado na release;
- build multi-stage;
- instalação por `uv sync --frozen`;
- somente dependências de produção na imagem final;
- usuário e grupo não-root;
- diretório de trabalho explícito;
- código copiado após dependências para aproveitar cache;
- sem compiladores e caches na imagem final;
- imagem `postgres:18-trixie` do Compose também fixada por digest na release;
- `PYTHONUNBUFFERED=1`;
- health check de liveness;
- um processo Uvicorn por container na baseline.

### 23.2 Compose padrão

Serviços:

- `app`;
- `migrate`, one-shot, usando a mesma imagem da aplicação;
- `db`.

Regras:

- porta do app publicada em loopback;
- banco não publicado fora da rede Compose por padrão;
- volume nomeado montado em `/var/lib/postgresql`, respeitando o layout da imagem PostgreSQL 18;
- `migrate` aguarda o health check do banco e executa `alembic upgrade head`;
- `app` inicia somente após `migrate` concluir com sucesso;
- o entrypoint de `app` inicia apenas o servidor e nunca disputa migrations com outra réplica;
- restart não substitui tratamento de erro de aplicação.

### 23.3 Perfil de observabilidade

Pode adicionar Jaeger e exportação OTLP sem alterar a lógica de negócio. O perfil é opcional para desenvolvimento, mas sua configuração deve ser fixa quando usado em benchmark.

### 23.4 Perfil de benchmark

`compose.benchmark.yaml` fixa:

- recursos de app e banco;
- serviço `loadgen` isolado, sem execução dentro do container da aplicação;
- importação obrigatória de Locust em etapa transitória da mesma imagem, concluída com sucesso
  antes de iniciar o loadgen; healthcheck periódico leve (`python -c "pass"`), sem substituir
  a supervisão de erros e timeouts do workload pelo runner; mesmo mecanismo na comparação v1.0 × v1.1;
- um worker do app;
- configuração de logging e tracing;
- banco e volumes exclusivos;
- portas exclusivas;
- ausência de auto-reload;
- nenhuma montagem do código-fonte.

## 24. Integração contínua

Pipeline mínimo:

1. `uv sync --frozen`;
2. Ruff check;
3. Ruff format check;
4. Mypy;
5. Import Linter;
6. testes unitários;
7. PostgreSQL de teste;
8. Alembic upgrade em banco vazio;
9. `alembic current --check-heads` e `alembic check`;
10. testes de integração e API;
11. cobertura;
12. build da imagem;
13. smoke test do container.

O pipeline não publica imagem nem executa benchmark completo automaticamente na v1.0.0.

## 25. Comandos esperados

Os comandos finais devem ser confirmados no AGENTS.md e README. Interface prevista:

```bash
uv sync --frozen
uv run alembic upgrade head
uv run alembic current --check-heads
uv run alembic check
uv run python scripts/seed_demo.py
uv run fastapi dev src/fulfillflow/main.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run lint-imports
docker compose up --build
```

No Windows, os comandos devem funcionar em PowerShell sem dependência de Make, Bash ou WSL para o fluxo normal.

## 26. Definition of Done da v1.0.0

A release só pode ser considerada v1.0.0 quando:

- Docker Compose inicia app e banco a partir de checkout limpo;
- migrations criam o schema em banco vazio;
- seed de demonstração é determinística;
- Orders podem ser criadas, confirmadas, consultadas e canceladas conforme regras;
- Shipments podem ser criadas e consultadas;
- webhooks Alpha e Beta são autenticados e normalizados;
- evento bruto autenticado é preservado;
- duplicidade é idempotente;
- eventos stale e transições inválidas são registrados sem regressão;
- concorrência do mesmo evento e da mesma Shipment é segura;
- timeline é append-only e ordenada;
- notificações simuladas são registradas uma única vez;
- Order conclui exatamente uma vez quando existe ao menos uma remessa ativa e todas as remessas ativas são entregues;
- API `/api/v1` e erros padronizados estão documentados no OpenAPI;
- telas obrigatórias estão funcionais;
- logs, request ID, métricas, health checks e tracing configurável estão presentes;
- testes obrigatórios passam contra PostgreSQL;
- cobertura e verificações estáticas atendem aos limites;
- imagem final executa como usuário não-root;
- benchmark pode ser executado e exportar CSV;
- README, DESIGN e AGENTS refletem o comportamento real;
- não existe componente listado como fora de escopo.

## 27. Liberdades de implementação

O implementador pode decidir sem alterar este DESIGN:

- nomes de funções e classes não públicas;
- divisão interna de arquivos dentro de um módulo;
- biblioteca de logging JSON, desde que cumpra o contrato;
- detalhes visuais do Bootstrap;
- forma exata dos fixtures;
- helpers e factories de teste;
- buckets dos histogramas, desde que declarados e congelados no benchmark;
- estratégia de cache de templates estáticos;
- mensagens textuais das notificações, desde que determinísticas.

Exigem revisão deste DESIGN antes da implementação:

- novo módulo de negócio;
- nova infraestrutura persistente;
- alteração de banco ou driver;
- processo, worker ou serviço adicional;
- mudança de máquina de estados;
- alteração de fronteira ou dependência entre módulos;
- mudança de transação ou idempotência;
- alteração material do contrato `/api/v1`;
- mudança do protocolo de benchmark;
- inclusão de item atualmente fora de escopo.

## 28. Matriz de rastreabilidade

| Capacidade | Módulo proprietário | Persistência | Interface principal | Verificação |
|---|---|---|---|---|
| Cadastro de pedido | Orders | `orders` | `/api/v1/orders` | unit + API + E2E |
| Cadastro de remessa | Shipments | `shipments` | `/api/v1/shipments` | unit + integration + API |
| Adapter externo | Carriers | nenhuma | contrato público | unit |
| Autenticação de webhook | Tracking | nenhuma | carrier event route | unit + API |
| Inbox auditável | Tracking | `carrier_event_inbox` | carrier events | integration + API |
| Timeline | Tracking | `tracking_events` | shipment tracking | integration + API + E2E |
| Estado atual | Shipments | `shipments` | shipment detail | unit + concurrency + E2E |
| Notificação simulada | Notifications | `notifications` | notifications | unit + integration + E2E |
| UI operacional | Web | nenhuma | HTML/HTMX | smoke + E2E |
| Telemetria | Observability | backend opcional | logs/metrics/OTLP | integration + benchmark |

## 29. Critério de conformidade

Uma implementação está em conformidade quando satisfaz simultaneamente:

- as fronteiras e dependências da seção 7;
- as entidades, constraints e índices da seção 8;
- as transições da seção 9;
- o contrato de evento das seções 10 a 13;
- os contratos HTTP da seção 14;
- os controles das seções 16 a 19;
- os testes e invariantes das seções 20 a 22;
- os requisitos de runtime das seções 23 a 25;
- o Definition of Done da seção 26.

Ambiguidade de implementação não autoriza ampliar o escopo. Quando duas alternativas atenderem igualmente ao documento, deve prevalecer a de menor complexidade operacional e menor acoplamento, preservando os contratos observáveis.

## 30. Arquitetura v1.1 — extração e preparação; comparação pendente

A v1.1 extrai deliberadamente somente Tracking, para avaliar autonomia, comunicação,
consistência e custos. A extração não depende de a baseline provar necessidade de
distribuição: os dados v1.0 não isolam Tracking como causa exclusiva de contenção e
não sustentam promessa de ganho de desempenho. O incremento I entrega a extração
funcional descrita em §30.1–30.4. O incremento II implementa a preparação experimental
de §30.5 para revisão. Piloto e controles foram executados; a matriz contemporânea
concluiu mixed/4 e permanece incompleta após o warm-up v1.1/12. A conclusão do
incremento III depende de revisão das evidências e de autorização específica.

### 30.1 Substituições delimitadas do contrato v1.0

| Contrato histórico | Substituição v1.1 |
| --- | --- |
| Aplicação/imagem única e ausência de HTTP interno (§4, §23) | Core e Tracking como serviços de aplicação; HTTP somente entre serviços, interfaces Python entre módulos do mesmo serviço |
| Adapters, schemas externos e normalização em Carriers (§7.2, §7.5, §28) | Interpretação dos eventos em Tracking; cadastro de transportadoras no Core |
| Banco único e FKs entre todos os módulos (§4.1, §8, §18) | Uma instância PostgreSQL 18, dois bancos segregados por proprietário, sem SQL ou FKs entre bancos |
| Transação B e ordem de locks globais (§13) | Transações locais, recibo idempotente no Core e recuperação por reentrega (§30.4) |
| Um worker e topologia app/banco/loadgen da baseline (§22–23) | Um worker por serviço de aplicação, orçamento agregado fixo e topologia a validar (§30.5) |

Essas substituições não alteram a release histórica. A tag `v1.0.0` aponta para o
commit documental `6235f6cb2a733e23ea76cf8264d2145f3759a871`; o commit medido foi
`ae15e0a2da465f4aec3d9c699655441ad1947265`. Manifests e resultados publicados
continuam identificando a v1.0, sem reinterpretação como execuções distribuídas.
Permanecem a stack Python/FastAPI/Compose, as máquinas de estados, os contratos
externos e as exclusões do §3.2: sem broker, worker de reprocessamento, Redis,
gateway adicional, Kubernetes/AKS ou novas capacidades de produto.

### 30.2 Fronteiras e propriedade dos dados

- **Core:** Orders, Shipments, Notifications, suas regras/transições e o cadastro
  de transportadoras (identidade, código, nome, situação ativa e associação às
  Shipments). Aplica eventos canônicos e possui o recibo idempotente dessa aplicação.
- **Tracking:** HMAC, schemas externos, adapters Alpha/Beta, normalização, inbox
  bruto, idempotência de recepção, coordenação do processamento, eventos canônicos
  persistidos e timeline. Cadastro não se confunde com interpretação de eventos;
  o Core não mantém uma segunda implementação dos adapters ou da normalização.

Cada serviço tem banco, credencial e migrations próprios na mesma instância
PostgreSQL 18. Nenhum serviço consulta tabelas, ORM ou repositories do outro.
Referências a Carrier/Shipment no Tracking e a TrackingEvent na Notification deixam
de ser FKs entre proprietários: identidade e existência necessárias são verificadas
pelos contratos, sem cópia das regras de domínio. Unicidades locais permanecem,
incluindo inbox por `(carrier_id, external_event_id)`, timeline por inbox e
notificação por identidade do evento. Não introduzir exclusão em cascata de histórico.
Compartilhar a instância não proporciona isolamento completo de infraestrutura.

### 30.3 Comunicação e contratos internos

A entrada pública permanece no Core. API, UI e simulador conservam os contratos
externos vigentes; operações de Tracking são encaminhadas por cliente HTTP
assíncrono, sem transformar a requisição em processamento em fila. O encaminhamento
preserva bytes brutos para HMAC, headers relevantes, X-Request-ID, códigos HTTP,
schemas, paginação e semântica de erros. HMAC continua obrigatório antes de parsing
ou persistência; seus secrets ficam no serviço verificador, não em respostas ou logs.

Contratos internos estreitos e autenticados, com timeouts finitos, cobrem:

- Core → Tracking: recepção do webhook e consultas de inbox/timeline;
- Tracking → Core: consultas cadastrais e de Shipment necessárias aos contratos
  externos, sem expor persistência ou transferir interpretação de payloads;
- Tracking → Core: aplicação do evento canônico com identidade estável vinculada
  à transportadora, hash de conteúdo imutável e dados de ordenação originais.
  Core devolve o resultado persistido suficiente para finalizar ou retomar no
  Tracking sem recalcular efeitos.

Nomes de endpoints e DTOs internos são detalhes de implementação; não são novas
rotas públicas nem autorização para compartilhar implementações de negócio.
Módulos dentro do Core continuam usando interfaces Python públicas e serviços
proprietários. Nenhuma chamada HTTP entre serviços mantém conexão, transação ou
lock SQL aberto, inclusive no caminho Core → Tracking → Core. Erros são sanitizados;
autenticação interna não substitui autenticação do webhook.

### 30.4 Consistência, recibo e recuperação

A Transação A independente de recepção é preservada; a Transação B global é
substituída por coordenação de commits locais:

1. Tracking autentica e persiste o inbox `RECEIVED`, raw bytes/hash e identidade
   estável recuperável do evento/comando. Preserva os casos sem persistência do
   §14.3 e o `received_at` original em toda retomada.
2. Core registra o recibo idempotente e aplica Shipment, Notification e Order na
   mesma transação local. A chave única do recibo identifica o evento e sua
   transportadora; o hash vincula o conteúdo imutável e o recibo guarda o resultado
   original. O conflito de unicidade é tratado no banco, não por check-then-insert.
3. Tracking persiste timeline/resultado e finaliza o inbox em transação local.
   HTTP 200 só é confirmado após todos os efeitos previstos para esse resultado.

Reentregas idênticas reutilizam as identidades, inclusive a correlação entre recibo,
Notification e TrackingEvent. Recibo existente devolve o resultado original, sem
reaplicar estado, notificar novamente ou consultar um estado posterior para
reconstruir o resultado. Reutilização da identidade com outro conteúdo é conflito.
A finalização concorrente também não duplica timeline nem sobrescreve inbox final.

Core preserva `READ COMMITTED`, constraints e locks locais de Shipment antes de
Order, assim como a ordenação `(occurred_at, received_at, external_event_id)` e
conclusão de Order. Locks de inbox pertencem às transações locais do Tracking;
não há ordem de locks global atravessando HTTP. Eventos distintos da mesma Shipment
continuam serializando sua aplicação no Core, sem regressão por eventos atrasados.

Retomada de `RECEIVED` é processamento normal, não `DUPLICATE` de inbox já
`PROCESSED`. Rejeições permanentes são reproduzíveis e finalizam `REJECTED`, sem
TrackingEvent. `NO_STATE_CHANGE` e `IGNORED_*` continuam HTTP 200 com timeline,
não rejeições permanentes. Falhas transitórias após recepção mantêm `RECEIVED`
e retornam 503; falhas inesperadas retornam 500 e também preservam a retomada.
Ausência de resposta não prova rollback do Core: se houve commit, a reentrega
obtém o recibo e conclui a finalização no Tracking. Não há retry em background
nem garantia de recuperação automática se o remetente não reentregar.

**Não existe atomicidade global na v1.1.** Entre os commits, Shipment/Order e
Notification podem estar atualizados enquanto a timeline ainda está pendente,
inclusive se um evento posterior já foi aplicado. Essa janela deve ser visível
nas limitações e testada; não se promete snapshot atômico entre os dois bancos.
Não adicionar transação distribuída, saga genérica ou compensação destrutiva.
Testes desde o incremento I cobrem contratos, HMAC/adapters, PostgreSQL real,
duplicatas concorrentes e resposta perdida após commit do Core; o incremento II
completa os casos adversos e comprova recuperação em cada fronteira de commit.
Import Linter e demais gates são adaptados à fronteira aprovada, nunca enfraquecidos.

### 30.5 Comparabilidade e pendência experimental

A campanha v1.1 terá identidade própria; nenhum diagnóstico ou resultado v1.0
conta como repetição v1.1. Preservar o protocolo publicado em `benchmarks/baselines/v1.0/`
e os manifests oficiais em `benchmarks/campaigns/`, incluindo workload, contratos externos, dataset lógico,
coortes/pesos, q=430, spawn rate 16 users/s e matriz 4/12 users × três profiles ×
cinco repetições válidas. Permanecem estabilização 300 s após preparação verificada,
warm-up 60 s, measurement 300 s, coleta 1 s, admissão/drain e exportação final,
timeouts, logging/tracing/sampling, energia e condições do host. Versões do ambiente,
nominal de memória Docker e tolerância absoluta de 1 MiB permanecem os aprovados.

Orçamento planejado da comparação principal: Core e Tracking terão cada um
1 CPU, 768 MiB, um worker e pool 5 com overflow 0; total da aplicação 2 CPUs,
1536 MiB e pool 10. PostgreSQL compartilhado mantém 2 CPUs/2560 MiB e o loadgen
separado mantém 2 CPUs/1536 MiB. Custos de HTTP, recibos e distribuição cabem nesses
orçamentos. Dois processos versus um e a partição de recursos são diferenças
declaradas, não equivalência impossível de topologias. Fixar a divisão antes da
campanha, sem ajustes oportunistas após observar resultados oficiais.

HEAD, imagens da aplicação, migrations e schemas v1.1 terão identidades próprias;
hash físico do schema não precisa coincidir com o monólito. Conteúdo lógico e
geração determinística do dataset permanecem congelados; a preparação física pode
distribuí-los pelos dois bancos, declarando recibos auxiliares sem alterar o estado
lógico inicial. Só há prontidão quando ambos os bancos foram verificados; falha
parcial bloqueia o ensaio, sem promessa de commit SQL global na preparação.

**Decisão autorizada no incremento II:** o parent congelado importa
`benchmarks/campaign.py`, que aceita somente v1.0 e app/postgres/loadgen. O workload
não depende desses papéis nem consulta SQL. Após apresentação do diff, foi autorizada
uma imagem derivada do parent exato, alterando somente `campaign.py` para aceitar
manifest v2 com release, recursos, imagens, pools e schemas explícitos Core/Tracking.
A imagem derivada tem identidade própria. O audit exige igualdade de todas as
distribuições instaladas e de todos os demais arquivos de benchmark, incluindo
locustfile/dataset. Não há mudança de dependências, protocolo HTTP ou parâmetros
de carga. O manifest v1 preserva a validação histórica. Essa diferença de imagem
e validação é declarada; não se atribui identidade v1.0 à candidata.
Os imports de normalização de `benchmarks/dataset.py` e `dataset_validation.py`
foram relocados para Tracking no incremento I; os testes dos hashes lógicos
congelados continuam passando. O loader v1.1 distribui as mesmas linhas por proprietário.
Inboxes históricos finalizados têm `command=NULL`; a preparação deixa zero recibos
Core. Reentrega desses históricos usa a timeline original. Comandos e recibos novos
pertencem às requisições v1.1, sem fabricação de efeitos ou mudança do estado inicial.
Preparação parcial bloqueia prontidão; ambos os bancos são relidos após os commits.

O Compose experimental fixa deadlines totais por chamada de 6 s Tracking→Core e
8 s Core→Tracking, dentro dos 10 s externos de request/drain. O limite total usa
cancelamento assíncrono além dos timeouts de I/O do HTTPX. Pool/SQL continuam
5 s/5000 ms. Defaults funcionais 10/30 s permanecem fora do benchmark. Timeout não
prova rollback nem cancelamento remoto; uma falha invalida a repetição e pode
requerer reentrega para recuperação. Não ampliar os deadlines externos.

Os probes leem cada banco com seu proprietário; a conciliação acontece no harness,
fora da janela medida. Recibo, comando, resultado, evento e notificação devem concordar.
Custos de recibos são contados separadamente do dataset inicial. Recursos e conexões
Core/Tracking são coletados por serviço e somados por ciclo; CPU/memória do PostgreSQL
compartilhado não são atribuíveis exclusivamente a um proprietário. A restauração
recria apenas um projeto isolado cuja propriedade foi registrada quando estava ausente.
O pacote e os comandos estão em `benchmarks/V11_REVIEW.md`; os manifests candidatos
são materializados com HEAD/imagens/schemas reais e conservam as expectativas do host.

Exceção inicialmente autorizada para o piloto não oficial: Windows build
`26200.9445`, divergente da baseline `26200.9278`, fixado na geração de um candidato
novo mixed/4, q=430, uma repetição. O comparador permanece exato; não há atualização
automática de expectativas. Demais parâmetros são preservados. Essa autorização
não estende a mudança à campanha oficial, cuja comparabilidade de ambiente exige
decisão própria conforme a proposta em `benchmarks/V11_REVIEW.md`.

Extensão delimitada autorizada após o piloto: dois controles exploratórios v1.0
mixed/4 no mesmo build (já executados manualmente) e, após sua análise, preparação
de quatro novos controles não oficiais na ordem v1.0, v1.1, v1.1, v1.0 (pares AB/BA).
Estes últimos conservam código e imagens medidos de cada versão, workload e parâmetros
acima; só usam novos nomes, destinos e projetos isolados, com Windows `26200.9445`
explícito. A preparação pode verificar os bancos sem carga; as quatro execuções são
manuais e não integram a matriz oficial. Os contrastes e sua leitura exploratória
ficam fixados antes dos resultados no pacote de revisão. Concordância entre dois
pares não prova equivalência estatística nem causalidade exclusiva. A baseline
publicada e a decisão de ambiente/referência da campanha oficial permanecem separadas.

A instrumentação deve observar Core/Tracking separados e agregados, reconciliar
Locust/HTTP/efeitos nos dois bancos e preservar completude e checksums. Mantém-se a
importação obrigatória de Locust antes da prontidão, healthcheck periódico leve e
supervisão de erros/timeouts pelo runner (§23.4). Relatar custos, dispersão e limites;
ganho de throughput não é critério de aceite. Cargas, congelamento e release exigem
autorização própria; os incrementos e seus aceites estão em `RELEASE_PLAN.md`.

Antes do encerramento experimental, aplicar a revisão autorizada da ferramenta de
medição: o runner de host pode receber `--application-source` para um checkout
congelado separado. Release/SHA, Compose e preparação continuam vinculados à
aplicação; SHA, lock e hashes dos módulos do runner são registrados separadamente.
O runner revisado é comum aos dois lados, sem modificar imagens ou checkouts
históricos, sem substituir módulos silenciosamente e sem reduzir validadores.
O modo sem a opção conserva a validação de fonte local. Não apresentar a revisão
do coletor como se fosse o executável da baseline histórica.

A coleta mantém cálculos, consultas, intervalo e deadlines congelados. A revisão
preserva diagnóstico sanitizado da falha original no coletor e no helper Docker;
a supervisão verifica falha/conclusão com esperas de até 250 ms, sem probes extras.
Falha obrigatória interrompe a tentativa, preservando erro primário, erros de
encerramento/exportação e artefatos incompletos. Não há repetição automática,
interpolação, tolerância nova ou promoção de evidência parcial a resultado válido.
Esse tratamento de falhas é uma mudança declarada da ferramenta de medição.

Decisão subsequente autorizada para a referência contemporânea: preparar no Windows
`26200.9445` trinta execuções novas por versão, preservando as imagens medidas e
usando o mesmo runner de host revisado. A baseline publicada continua histórica.
Primeiro, um novo ABBA mixed/4 substitui operacionalmente o bloco anterior inválido,
sem apagar suas evidências. A matriz posterior contém cinco repetições por versão
e célula, em blocos: mixed/4 A→B; mixed/12 B→A; timeline/4 A→B; timeline/12 B→A;
ingestion/4 A→B; ingestion/12 B→A (A=v1.0, B=v1.1). Essa ordem não é randomizada
nem exclui efeito de ordem. Pilotos, controles e tentativas inválidas não integram
a matriz. Execução manual e revisão dos resultados permanecem etapas separadas.
Preserva-se a política efetiva de logs das imagens; as lacunas de implementação
de logging estruturado, métricas e tracing de §19 permanecem explícitas.

Extensão diagnóstica autorizada após a interrupção da matriz em mixed/12 v1.1:
uma única tentativa manual não oficial, limitada ao warm-up, com 12 usuários.
Preservar imagens, dataset, preparação, q430, recursos, estabilização 300 s,
admissão 60 s, drain, supervisão, coleta e verificações de estado/recibos.
O runner de host recebe `--diagnostic-warmup-only`; não inicia measurement e
registra `valid=false`, `matrix_eligible=false`, além de conclusão e validade
específicas do diagnóstico. O manifest mantém o contrato original de duração;
a omissão explícita da medição pertence somente a esse modo diagnóstico.
Aplicação, runner e coordenador têm identidades separadas em pacote novo.
O diagnóstico separa processo Locust, coleta, encerramento e exportação; quota
derivada dos artefatos não é apresentada como razão textual interna do Locust.
Os dez resultados mixed/4 válidos e a tentativa inválida permanecem preservados.
Nova falha equivalente mantém as células de 12 usuários bloqueadas; sucesso exige
revisão da divergência e não prova estabilidade nem retoma a matriz automaticamente.
Não há nova margem de aprovação nem alteração da imagem congelada do loadgen.

#### Sensibilidade autorizada à política de warm-up

Após duas tentativas v1.1/12 com quota incompleta em 60 s, preparar uma campanha
separada, não oficial, identificada por `warmup-policy-sensitivity-v1`. Os 120 s
foram escolhidos após conhecer essas evidências; não constituem parâmetro prévio
da campanha histórica. A aplicação, imagens originais, dataset, q430 por usuário,
12 usuários, spawn rate 16/s, coortes, eventos, recursos, pools, logs, timeouts de
requisição e drain permanecem congelados. Estabilização de 300 s a cada tentativa.
Não executar measurement. A quota total de 5.160 distribui-se proporcionalmente
em 60 s (86 aplicações/s nominais) ou 120 s (43/s). Variam duração **e ritmo**;
não se identifica o efeito isolado do tempo. Após admissão, somente requisições
já iniciadas concluem no drain; não há emissão adicional para completar quota.

São oito tentativas máximas, duas por combinação, na ordem fixa:
v1.0/60, v1.1/120, v1.1/60, v1.0/120, v1.0/120, v1.1/60, v1.1/120, v1.0/60.
A ordem não é randomizada e não elimina variação temporal. Não substituir falhas,
ampliar durações, reduzir quota ou incorporar resultados anteriores nessas condições.

O loader histórico `CampaignManifest` mantém `Literal[60]` e rejeita o campo de
protocolo novo. `SensitivityManifest` tem entrada explícita distinta. Compartilham
os gates de identidade, dataset, recursos e topologia. O manifest de preparação
histórico de 60 s é uma referência para os probes/checkouts congelados; o manifest
executável separado declara o novo protocolo e a duração efetiva. O coordenador
confere sua correspondência campo a campo: somente protocolo, duração e prazo de
supervisão do warm-up diferem. O prazo de 120 s recebe mais 60 s, preservando a
margem original sobre admissão/drain. O runner recusa measurement neste modo.

Criar loadgens derivados dos parents preservados de cada versão, com os mesmos
`campaign.py`, `locustfile.py` e `warmup_sensitivity.py` revisados. Inventariar hashes,
parents, imagens e distribuições instaladas; nenhuma atualização de dependências.
O novo loadgen exporta, ao término, contadores por usuário e código de causa
controlado, sem corpos, segredos ou erro livre. Essa causa registrada pertence
somente às novas imagens; não recupera a razão textual das tentativas históricas.
Aplicação, runner, loadgen e coordenador/pacote conservam identidades separadas.
O override Compose do pacote altera somente a imagem do loadgen; fontes históricas
e tags originais não são modificadas.

Mantêm-se coleta de 1 s, consultas e cálculos existentes, supervisão de 250 ms e
interrupção sem repetição. Conferir os prefixos determinísticos por usuário, bytes,
efeitos, ponteiros de ordenação, coortes inativas e conteúdo inicial preservado;
na v1.1, conciliar também comandos, recibos e finalizações. Quota incompleta segue
com saída não zero e warm-up inválido; observação íntegra pode informar a análise
de sensibilidade, nunca a matriz oficial. Erros primários, secundários e evidências
parciais permanecem separados. Não adicionar consultas periódicas ou instrumentação.

Cada chamada manual executa uma tentativa em destino/projeto novo. A próxima
condição exige revisão explícita das evidências anteriores: somente sucesso ou
quota incompleta com integridade e conciliação permitem liberação. Preparação,
identidade, coleta, exportação, reinício/OOM, integridade ou conciliação falhos
bloqueiam a sequência. Após exportação, parar somente recursos de propriedade
comprovada; preservar contêineres, volumes e evidências. Nenhum retry automático.

Interpretação fixada antes dessas oito tentativas: v1.1 incompleta em 60 s e completa
em 120 s indica sensibilidade à política, compatível com demanda nominal maior que
o ritmo observado em 60 s, sem causalidade exclusiva. Ambas completas em 120 s
com conciliação tornam essa política candidata à preparação comum, sem aprovar
estabilidade ou a matriz. Divergências entre repetições são variabilidade a relatar;
erros, inconsistência ou perda de progresso exigem investigação. Duas observações
por condição são exploratórias, e amostras de 1 s não são repetições independentes.
Não declarar significância, equivalência, ausência de vazamentos ou deadlocks.
Uma nova comparação completa exige decisão posterior; não está preparada aqui.

#### Nova comparação simétrica autorizada após a sensibilidade

As oito observações foram encerradas e preservadas: v1.0 completou as quatro
quotas; v1.1 completou as duas de 120 s e ficou em 3.677 e 3.674/5.160 nas de 60 s.
A continuação manual bloqueante 4–8 foi posteriormente autorizada com revisão
programática entre condições. Seu código agregado 2 preservou a quota inválida.

A decisão atual prepara `symmetric-warmup120-comparison-v1`, campanha nova com
120 s para ambas as versões. Variam duração e ritmo nominal: 14,333… aplicações/s
em 4 usuários e 43/s em 12, preservando q430 por usuário. Não é correção da aplicação
nem efeito isolado do tempo. Nenhum piloto, controle, ABBA, diagnóstico, sensibilidade,
resultado inválido ou medição anterior integra a nova matriz. A campanha original
e suas dez medições válidas permanecem intactas e com classificações preservadas.

São cinco repetições por versão/célula, total 60. Ordem dos blocos: mixed/4 A→B,
mixed/12 B→A, timeline/4 A→B, timeline/12 B→A, ingestion/4 A→B, ingestion/12 B→A.
A=v1.0 e B=v1.1; ordem fixa, não randomizada, sem exclusão de efeito temporal.
Estabilização 300 s, measurement 300 s, coleta 1 s e preparação limpa por repetição.
Piso das fases: 43.200 s (12 horas), além de preparação, verificações, drain e exportação.

`ComparisonManifest` usa loader explícito separado: oficial, cinco repetições, uma
carga 4 ou 12, 120 s e q430. O loader histórico continua restrito a 60 s; o de
sensibilidade continua não oficial e sem measurement. Probes congelados usam um
manifest de preparação de 60 s; o executável difere somente em protocolo, warm-up
e seu prazo de processo (90→150 s). Mantém-se a margem de 20 s sobre admissão +
drain, request/drain 10 s, measurement process 330 s, preparation 120 s e phase
start 30 s. Demais parâmetros, recursos e configurações permanecem congelados.

Reutilizar os loadgens de sensibilidade como parents. A derivação adiciona
`comparison_protocol.py` e revisa somente `locustfile.py` para selecionar o contrato
completo e exportar os mesmos contadores finais de warm-up. Agendamento, barreira,
quota, drain e workload são reutilizados. Conferir inventário integral, parents,
digests, fontes e distribuições instaladas; nenhuma dependência muda. O runner só
passa a measurement após quota por usuário completa, drain e verificação integral
do banco e efeitos. Aplicação, runner, loadgen, coordenador e pacote têm identidades
separadas. Checkouts e imagens históricos não são modificados.

Uma entrada manual bloqueante coordena os doze blocos. Validadores programáticos
entre repetições e blocos dispensam interações rotineiras. Qualquer falha, inclusive
quota incompleta, medição, identidade, coleta, exportação ou conciliação, interrompe
sem retry, reposição ou reinício de blocos. Preservar medições válidas e diagnósticos;
retomada exige autorização posterior com estado verificado. Limpeza só atinge recursos
isolados de propriedade comprovada. Não executar carga na preparação. O pacote fica
bloqueado até revisão independente, liberação do usuário, commit final, sua CI e
conferência final da identidade; documentos e evidências publicados ficam intactos.

A continuação explicitamente autorizada usa o contrato operacional
`comparison120-explicit-continuation-v1`. Mantém os manifests de cinco repetições;
a única alteração neles é o projeto Compose isolado. O primeiro segmento novo
executa r02–r05 e referencia r01 no diretório original, vinculada aos checksums
aprovados, manifest original e runner `02fe942`. Nenhuma cópia ou alteração de
metadados antigos é permitida. Os onze blocos seguintes executam r01–r05, na
ordem já fixada: 59 medições novas, piso 42.480 s (11 h 48 min).

O runner só aceita o modo explícito de continuação com pacote liberado, manifest
e destino exatos. O resumo do primeiro bloco reúne quatro diretórios novos e a
referência externa autenticada, sem declarar que foram medidos pelo mesmo SHA
da ferramenta. Identidades da aplicação e loadgen permanecem congeladas; runner,
coordenador e pacote novo são registrados separadamente. Cada preparação revisa
a repetição anterior nova; a r01 original é verificada por sua identidade aprovada.
Falha em qualquer segmento encerra o novo journal, sem repetição ou retomada implícita.

A entrada manual nova pode remover somente o projeto anterior v1.0 de propriedade
comprovada por `owned.json` e pelo inventário de containers selado, depois de
exportar logs para o journal novo. Nenhum arquivo antigo é escrito. Na preparação
do pacote, essa remoção não ocorre; host e imagens são inspecionados sem carga.
Qualquer container divergente bloqueia a remoção. A admissão dinâmica do host é
repetida após a remoção e antes da carga, mantendo os requisitos originais.

Uma falha de exportação do diagnóstico da preparação preserva em memória o
relatório sanitizado completo: erro primário, encerramento, stderr do filho,
código de saída, duração e erro de exportação. O mesmo relatório é emitido no
stderr capturado pelo coordenador, sem dependência da gravação que falhou.
Prazos, coleta e cálculos não mudam. O pacote novo aguarda revisão antes do
commit final e sua CI; a entrada anterior permanece encerrada.

Duas quotas completas em 120 s por versão não demonstram estabilidade, equivalência,
superioridade, teto físico universal, causa exclusiva ou ausência geral de defeitos.
Os 60 s eram requisito do protocolo original, não um SLA. Nenhuma mudança funcional,
de observabilidade ou de serviços integra esta etapa. Matriz e release permanecem pendentes.

#### Diagnóstico delimitado com tela e sistema ativos

A continuação de 120 s encerrou com oito medições aprovadas pelos validadores e
r04 v1.1 mixed/4 inválida por 503 na medição. Preservam-se todas as classificações.
A auditoria administrativa encontrou quatro sessões Screen Off, sem segmentos
Sleep registrados; sua agregação não identifica a causa do 503. Os testes de
recuperação sustentam os cenários examinados, sem demonstrar estabilidade sob carga.

O protocolo separado `active-screen-mixed4-diagnostic-v1` é não oficial e inelegível
para a matriz, limitado a cinco repetições v1.1 mixed/4. Conserva preparação limpa,
300 s de estabilização, 120 s de admissão proporcional, q430 por usuário, 300 s de
medição, coleta de 1 s, aplicações, recursos e demais limites da comparação de 120 s.
Quota e conciliação completas continuam necessárias antes da medição. O loader
histórico de 60 s e o comparativo oficial não aceitam esse contrato. Uma projeção
somente em memória reaproveita integralmente as restrições do comparativo; nenhum
manifest oficial é reclassificado ou substituído. A imagem derivada altera somente
o contrato e sua seleção/exportação no loadgen, com inventário de arquivos e pacotes.

Uma solicitação nativa temporária `SetThreadExecutionState` mantém sistema e tela
ativos, no mesmo thread até a liberação em finally; não altera o plano persistente.
Observam-se alimentação AC, desktop interativo desbloqueado, notificações de tela
e sessão e heartbeat a cada 250 ms. Ausência de confirmação, tela diferente de On,
transição de sessão, suspensão ou lacuna de heartbeat superior a 3 s interrompem.
O usuário deve manter tampa aberta, tomada e sessão desbloqueada, sem suspensão
manual. O mecanismo não impede todas as ações deliberadas nem garante condições
durante Modern Standby ou sessão bloqueada; essas condições são recusadas.
Samsung Mode e máximo de CPU de 99% permanecem sem alteração. Esse percentual não
é interpretado como perda linear de capacidade ou comprovação de turbo desativado.

O launcher bloqueante mantém a solicitação entre repetições, limita o bloco a duas
horas operacionais e libera o mecanismo após sucesso ou falha. A espera final
do filho é limitada a 180 s; ausência de
encerramento confirmado gera saída 2, PID/início UTC e inspeção manual, sem kill
indiscriminado ou remoção de recursos. O rótulo de proveniência deste diagnóstico
explicita warm-up e medição, sem alterar seu protocolo. Prazos das fases e
requisições não mudam. O runner verifica o heartbeat com espera de até 250 ms durante
carga e estabilização; preparação em andamento conserva seu prazo de 120 s, e não
autoriza iniciar carga após falha ambiental. A verificação ociosa manual dura 960 s,
excede o timeout de tela de 15 minutos e deve anteceder a liberação do bloco.
Capturam-se saída e código de powercfg; saída vazia ou acesso negado é inconclusivo.
Repetições ociosas recebem número explícito e destino novo; essa seleção é recusada
no modo de carga e não altera o destino operacional único.

O custo adicional é a observação nativa e leitura/escrita de heartbeat no host a cada
250 ms, além de exportação entre repetições e após falha. Não há novas consultas SQL
periódicas nem instrumentação da aplicação. Logs com timestamps, stdout/stderr
sanitizados e amostras parciais preservam os sinais disponíveis; lacunas permanecem
nos horários reais, sem interpolação. A exceção interna de outro 503 pode continuar
indeterminada, sem correlação inequívoca entre HTTP público e efeitos persistidos.

Recursos novos têm propriedade verificada e preparação limpa por repetição. A
parada graciosa dos concorrentes históricos só poderá ocorrer manualmente após
exportação e nova conferência de identidade, sem remover containers ou volumes.
Ela perde estado volátil; parar o PostgreSQL de testes em tmpfs perde seus dados
sintéticos. Na preparação atual não se para nenhum desses recursos. A cópia
independente das evidências permanece pendente de confirmação.

Qualquer falha encerra sem retry, reposição ou sobrescrita. Cinco sucessos permitem
avaliar uma campanha futura, sem provar causa da falha anterior ou estabilidade
geral. Evidência ambiental insuficiente impede conclusão sobre a aplicação. Piso
das fases: uma hora; preparação, drain e exportação são adicionais. Nenhum resultado
desse bloco integra a matriz e nenhuma nova campanha completa está preparada aqui.

### 30.6 Estado funcional do incremento I

O Compose usa o projeto `fulfillflow-v11`, volume novo e bancos `fulfillflow_core`
e `fulfillflow_tracking`. As roles não possuem privilégios administrativos nem
CONNECT ao banco do outro proprietário. Os graphs `alembic_core.ini` e
`alembic_tracking.ini` têm heads `1101_core` e `1101_tracking`; não convertem um
banco v1.0 existente. O graph histórico permanece destinado à regressão v1.0.

As chamadas sob `/internal/v1` exigem exatamente um
`X-FulfillFlow-Internal-Token`, comparado em tempo constante antes de decodificar
o corpo. O token vem de `INTERNAL_API_SECRET`; não substitui HMAC. As rotas
internas ficam fora do OpenAPI público. Os clientes preservam correlação e headers
de trace e usam timeouts finitos de 10 s ao Core e 30 s ao Tracking por padrão.
Falhas de transporte ou resposta interna inválida geram problem detail 503
`SERVICE_UNAVAILABLE`; autenticação interna inválida gera 401
`INTERNAL_AUTHENTICATION_FAILED`, sem divulgar o token.

A identidade do comando é derivada de modo determinístico do UUID do inbox.
Após autenticação e commit do bruto, a normalização salva o comando imutável em
uma transação local antes de chamar o Core. Uma retomada usa esse comando e o
`received_at` original. O recibo Core vincula UUID, Carrier/event ID e SHA-256
de todos os campos canônicos, incluindo o hash dos bytes brutos e a ordenação.
O resultado inclui o instante original da decisão. A reserva de unicidade e
todos os efeitos são confirmados juntos; nenhuma reserva incompleta é confirmada.
Rejeição por Shipment ausente também tem recibo e não muda caso a Shipment seja
criada posteriormente. Timeline/finalização usam exclusivamente o resultado original.

`scripts/prepare_demo_v11.py` prepara pela API pública um Order confirmado e duas
Shipments pendentes com referências e dados sintéticos fixos. Repetição preserva
registros compatíveis; conflitos não são sobrescritos. UUIDs e horários pertencem
à aplicação. Esse comando permite demonstração funcional, sem substituir o seed
determinístico de benchmark nem prometer preparação atômica entre bancos.

Foram validados contratos/API/UI, dois processos TCP com simulador externo,
PostgreSQL por proprietário, duplicatas com contenção real, rollback dos efeitos
Core, perda de resposta seguida de evento posterior e retomada, e falha na
finalização Tracking. O teste de encaminhamento verifica que nenhum checkout SQL
atravessa HTTP. A matriz adversa restante e a prontidão experimental pertencem ao II.
