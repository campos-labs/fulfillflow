# FulfillFlow

**v1.3 em desenvolvimento — Notifications assíncrono (incrementos I–III)**,
na branch `feature/v1.3-notifications-async`, baseada em `v1.2.0-rc.1`
(`9b445f9b5466cd302c89f1deed7a9c051cb397ae`). O [DESIGN](DESIGN.md) define o
alvo e o [RELEASE_PLAN](RELEASE_PLAN.md) registra o plano e seu estado.
O código e os comandos abaixo executam `v1.3.0.dev0`, com Tracking e Notifications
assíncronos, com recuperação e operação do III. UI/DEMO e aceite funcional final
pertencem ao IV, ainda não iniciado; a execução para antes dele. As referências v1.0/v1.1/v1.2
e suas evidências permanecem congeladas; campanhas de carga estão suspensas.

Core mantém Orders, Shipments e Carriers. Tracking mantém HMAC,
adapters Alpha/Beta, inbox e timeline. Notifications possui os registros e a
simulação de entrega. Cada serviço possui banco, role, API e worker
próprios. Core continua sendo a entrada pública e serve a interface.

Eventos novos recebem **202 após admissão durável**, com inbox, comando e outbox
Tracking gravados juntos. O worker publica `tracking.apply.v1` pelo RabbitMQ.
Core grava recibo, efeitos e outbox `tracking.result.v1` na mesma transação local;
Tracking finaliza a timeline e o inbox ao processar esse resultado. Publicação e
ACK ocorrem fora da transação SQL. Não há atomicidade global nem exactly-once.
O endpoint e o cliente internos de apply HTTP foram removidos; consultas e
encaminhamento autenticados continuam HTTP.

Somente transições Tracking `APPLIED` geram `shipment.status_changed.v1`, com
destinatário congelado, na mesma transação Core do recibo, efeitos e resultado
Tracking. Notifications persiste a inbox antes do ACK e grava simulação + DONE
atomicamente. Não há escritor local ou fallback no Core. Backlog e falhas
controladas do novo publicador não impedem `tracking.result`; processo, banco,
broker e recursos compartilhados ainda têm falhas comuns. Simulação idempotente
no banco não demonstra exactly-once em um provedor externo.

## Subida local com Docker Compose

O fluxo padrão funciona a partir do checkout sem publicar o PostgreSQL no host:

```powershell
docker compose up --build --detach --wait
```

O projeto padrão `fulfillflow-v13` cria volumes PostgreSQL e RabbitMQ novos.
PostgreSQL inicializa `fulfillflow_core`, `fulfillflow_tracking` e
`fulfillflow_notifications`, com roles sem CONNECT ao banco alheio. Os jobs
`migrate-core`, `migrate-tracking` e `migrate-notifications` aplicam
os heads antes dos processos. Somente Core publica `127.0.0.1:8000`; Tracking,
workers, PostgreSQL e RabbitMQ ficam na rede interna. O broker usa um vhost e
três usuários com permissões separadas para comando, resultado e fato Notifications.
O hostname do broker é estável (`broker` por padrão), pois a identidade do nó
participa do caminho dos dados RabbitMQ. Antes de recriar um broker v1.2 existente
criado sem hostname fixo, siga o README congelado. Para inspecionar a identidade
de um broker do projeto atual, registre o valor em `.env`:

```powershell
$env:RABBITMQ_HOSTNAME = docker inspect (docker compose ps -a -q broker) --format '{{.Config.Hostname}}'
```

Use o mesmo projeto (`-p`, quando aplicável) nessa inspeção e na subida. Não troque
essa identidade ao reutilizar um volume. Os CLIs RabbitMQ usam `+S 1:1 +A 1`
para não dimensionar seus processos de diagnóstico pelos CPUs do host; quotas,
imagem e configuração do servidor permanecem as declaradas.

Cada API usa 0,5 CPU, 384 MiB e pool 2/0; cada worker, 0,5 CPU, 384 MiB e pool 3/0.
PostgreSQL usa 2 CPUs/2560 MiB e RabbitMQ, 0,5 CPU/512 MiB. São parâmetros
funcionais: APIs/workers somam 3 CPUs/2304 MiB; com banco/broker, 5,5 CPUs/5376 MiB,
sem jobs e sem alegação de equivalência com versões anteriores. Workers usam
prefetch 8, lote 20, polling 500 ms, lease 30 s e timeout de confirm 5 s.
Heartbeat atualiza a cada 500 ms: arquivo com mais de 5 s ou loop sem atividade
por 45 s invalida a saúde. Dependência indisponível também invalida a saúde.

Health/readiness das APIs não comprovam conclusão do trabalho. Workers têm
heartbeat local e healthcheck sem HTTP: Core exige quatro loops, Tracking três
e Notifications dois (consumo/processamento), além de
dependências disponíveis. Saúde não implica ausência de `BLOCKED`, nem fila
RabbitMQ vazia comprova conclusão. SIGTERM/SIGINT param admissão de trabalho e
aguardam até 15 s; interrupções deixam trabalho durável. Compose reserva 20 s,
incluindo margem de fechamento. Falha inesperada de loop encerra o processo
com erro. Não há alegação de estabilidade prolongada.
Não aponte os serviços a bancos ou volumes históricos.

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
Invoke-WebRequest http://127.0.0.1:8000/
```

## Operação local do transporte

Execute no projeto v1.3 descartável (acrescente `-p` se usar outro nome).
Cada CLI acessa somente o banco do próprio serviço e exige schema atual.
Não há endpoint administrativo público.

```powershell
docker compose exec -T core-worker python -m fulfillflow.core.operations healthcheck
docker compose exec -T tracking-worker python -m fulfillflow.tracking.operations healthcheck
docker compose exec -T core-worker python -m fulfillflow.core.operations diagnose
docker compose exec -T tracking-worker python -m fulfillflow.tracking.operations diagnose
docker compose exec -T notifications-worker python -m fulfillflow.notifications.operations healthcheck
docker compose exec -T notifications-worker python -m fulfillflow.notifications.operations diagnose
docker compose exec -T core-worker python -m fulfillflow.core.operations diagnose --flow shipment.status_changed.v1
docker compose exec -T broker rabbitmqctl list_queues -p fulfillflow-v13 name messages_ready messages_unacknowledged
```

Use `diagnose --id <UUID>` nos três serviços para correlacionar mensagem,
evento ou correlação; quarentena aceita seu ID local. O relatório apresenta
estado, hash, geração, tentativas, atividade, publicação, decisão e finalização
disponíveis, sem payloads. Contagens são por etapa: não some inbox/outbox/filas
como total de eventos. `accepted_nonterminal` do Tracking conta eventos ainda
pendentes, inclusive bloqueados; `legacy_pending` identifica registros sem outbox,
sem recuperação automática. Banco e broker não formam fotografia global atômica.

Falhas transitórias de item permitem cinco tentativas por geração, com esperas
1/5/15/60 s. Conflitos e erros inesperados bloqueiam; rejeição permanente de negócio
é terminal. Banco/broker indisponível pausa o componente, com espera limitada a
30 s, sem esgotar os demais itens. Confirmação incerta aguarda a lease de 30 s
e pode republicar a mesma identidade. Reiniciar worker não desbloqueia itens.

Corrija a causa e informe o hash técnico, banco e motivo do rearme:

```powershell
$messageId = '<UUID-da-mensagem>'
$expectedHash = '<body_sha256-do-diagnostico>'
docker compose exec -T core-worker python -m fulfillflow.core.operations rearm --stage inbox --id $messageId --expected-hash $expectedHash --expected-database fulfillflow_core --reason 'Causa corrigida e verificada'
```

Para Tracking use `tracking-worker`, `fulfillflow.tracking.operations` e
`fulfillflow_tracking`; para publicação use `--stage outbox`. Somente `BLOCKED`
pode ser rearmado: identidade/payload permanecem iguais, geração aumenta e
as tentativas reiniciam junto com a auditoria do motivo e estado anterior.
A CLI não reenfileira `DONE`/`SENT` ou quarentena. Bytes de quarentena ficam no
banco proprietário; comandos não os exibem. Não inclua segredos no motivo.

Notifications aceita somente `--stage inbox`; use seu proprietário e banco:

```powershell
docker compose exec -T notifications-worker python -m fulfillflow.notifications.operations diagnose --id $messageId
docker compose exec -T notifications-worker python -m fulfillflow.notifications.operations rearm --stage inbox --id $messageId --expected-hash $expectedHash --expected-database fulfillflow_notifications --reason 'Causa corrigida e verificada'
```

O resultado identifica proprietário, banco, ID, hash, motivo e nova geração.
`SIMULATED` e `FAILED` são terminais e nunca autorizam outra simulação, mesmo se
uma inconsistência externa marcar a inbox como `BLOCKED` (`TERMINAL_NOTIFICATION`).
`LEGACY_EVENT_SUPPRESSED` preserva o legado, a quarentena diagnóstica e inbox DONE;
não oferece replay/rearme. Duplicata ASYNC é verificada pelo envelope original;
LEGACY não recebe envelope ou hash histórico inventado.

Core permite `--flow shipment.status_changed.v1` em `diagnose` e `rearm`; filtro
incompatível impede a mutação. Quarentena é explicitamente do proprietário inteiro,
pois envelopes inválidos não permitem filtro seguro por fluxo. Use `--id` para
inspecionar auditorias; o conteúdo da notificação não aparece no diagnóstico.

Separe quatro observações: contagens/idade por etapa são backlog durável;
`worker.stages` com `dependency_unavailable` é pausa de dependência; `reason`,
`attempts` e `BLOCKED` descrevem falha de item; loop obrigatório inesperadamente
encerrado faz o processo sair com erro. O heartbeat pode ficar fresco por até 5 s
após morte abrupta: confira também o estado/exit code do processo. `stale`,
`stopping` ou `unobserved` não comprovam sucesso nem diagnosticam sozinhos a causa.
A observação é local ao arquivo do worker; rodar CLI em outro processo/container
sem esse arquivo não observa sua saúde. `SENT`, fila vazia e saúde positiva não
significam simulação concluída. Falhas comuns de processo, banco ou broker continuam
compartilhadas; somente backlog e falhas controladas específicas dos publishers
são independentes.

Workers emitem JSON INFO em stdout: serviço, etapa, IDs, tentativa, duração,
resultado e categoria controlada, sem bodies, assinaturas ou exceções brutas.
Essa instrumentação difere da v1.1; Prometheus/OTel mais amplos permanecem
pendentes. Não há alegação de paridade de benchmark.

## Contratos HTTP

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
- `GET /api/v1/notification-status/{tracking_event_id}` separa fato/publicação Core
  da observação Notifications. `SIMULATED`/`FAILED` são terminais; `NOT_RECEIVED`,
  `PENDING` e `BLOCKED` descrevem processamento. Ausência do recibo retorna 404;
  falha de consulta retorna 503, sem transformar indisponibilidade em zero ou FAILED;
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
A tela do inbox consulta a cada segundo por até 30 segundos por observação.
Falhas de consulta preservam a aceitação e exibem aviso; **Check again** inicia
outra observação sem reenviar o evento. Conclusão/rejeição encerram as consultas,
e o resultado aplicado oferece link para a timeline. Prazo esgotado não é rejeição.
O simulador emite fases `accepted`, `completed`, `rejected`, `observation_failed`
e `observation_expired`; falhas de admissão/cliente usam `failed` (503 não é
rejeição de negócio). Seu prazo é configurável por `--completion-timeout-seconds`.

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
automática. Use a CLI de rearme auditável descrita na operação local; não altere
esses estados manualmente para simular recuperação. Legado sem transporte tem progresso nulo;
as migrations não criam comandos nem inventam resultados históricos.

Os defaults de secrets no `compose.yaml` são exclusivos do ambiente local
isolado. Para sobrescrevê-los, copie `.env.example` para `.env` e substitua todos
os placeholders. Mantenha `CORE_DB_PASSWORD`/`CORE_DATABASE_URL` e
`TRACKING_DB_PASSWORD`/`TRACKING_DATABASE_URL` e
`NOTIFICATIONS_DB_PASSWORD`/`NOTIFICATIONS_DATABASE_URL` coerentes. `POSTGRES_PASSWORD`
pertence apenas à administração inicial. Alterar o `.env` não altera senhas de
roles já criadas. `INTERNAL_API_SECRET` autentica Core/Tracking;
`NOTIFICATIONS_API_SECRET` é exclusivo das consultas Notifications. HMAC fica
somente no Tracking e `SESSION_SECRET` somente no Core. Ao trocar credenciais AMQP, provisione os usuários correspondentes no broker;
`CORE_AMQP_URL`/`TRACKING_AMQP_URL`/`NOTIFICATIONS_AMQP_URL` não alteram usuários já existentes.
Timeouts HTTP padrão:
10 s para chamadas ao Core, 30 s para encaminhamento ao Tracking e 2 s para
Notifications, sem retry automático. Core obtém lista/detalhe/contagens por HTTP
autenticado, sem SQL entre serviços nem conexão SQL retida durante a chamada.

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
- `GET /notification-status/{tracking_event_id}`: observação manual do progresso
  independente, também acessível após APPLIED no inbox; polling refinado fica no IV;
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
pendentes, Alpha `V13ALPHA0001` e Beta `V13BETA0001`, pela API pública:

```powershell
uv sync --frozen
uv run python scripts/prepare_demo_v13.py --base-url http://127.0.0.1:8000
```

Referências e dados sintéticos são fixos; UUIDs e timestamps são atribuídos pela
aplicação. Repetir preserva os mesmos registros, inclusive após eventos, e uma
divergência nos dados esperados falha sem sobrescrevê-los. Uma preparação parcial
pode ser retomada; ela não é uma transação entre bancos nem o seed do benchmark.
Use o simulador acima com cada tracking code para preencher timeline e concluir
o Order após entregar ambas as Shipments. A conclusão Tracking/Order não espera
Notifications; consulte as simulações separadamente. O dashboard mantém contagens
locais e mostra indisponibilidade explícita se a consulta Notifications falhar.

Para executar aplicações no host, provisione os três bancos PostgreSQL 18 com as
roles segregadas dos scripts `infrastructure/init-databases.sh` e
`infrastructure/init-notifications-db.sh`. Em terminais separados,
configure os secrets próprios, os tokens internos e as URLs:

```powershell
# Terminal Core; DATABASE_URL aponta exclusivamente ao banco Core no host.
$env:SERVICE_ROLE = "core"
$env:DATABASE_URL = "postgresql+psycopg://<core-role>:<password>@127.0.0.1:5432/fulfillflow_core"
$env:APP_PORT = "8000"
$env:TRACKING_BASE_URL = "http://127.0.0.1:8001"
$env:NOTIFICATIONS_BASE_URL = "http://127.0.0.1:8002"
# Configure também NOTIFICATIONS_API_SECRET com o token independente de Notifications.
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

```powershell
# Terminal Notifications; use o token independente também configurado no Core.
$env:SERVICE_ROLE = "notifications"
$env:DATABASE_URL = "postgresql+psycopg://<notifications-role>:<password>@127.0.0.1:5432/fulfillflow_notifications"
$env:APP_PORT = "8002"
$env:INTERNAL_API_SECRET = $env:NOTIFICATIONS_API_SECRET
uv run alembic -c alembic_notifications.ini upgrade head
uv run alembic -c alembic_notifications.ini current --check-heads
uv run alembic -c alembic_notifications.ini check
uv run python -m fulfillflow.notifications
```

Os workers são processos adicionais, com a mesma configuração do serviço
proprietário e `AMQP_URL` apontando ao vhost provisionado. Em terminais próprios:

```powershell
# Ambiente Core, DATABASE_URL Core e AMQP_URL do usuário Core.
uv run python -m fulfillflow.core.worker
# Ambiente Tracking, DATABASE_URL Tracking e AMQP_URL do usuário Tracking.
uv run python -m fulfillflow.tracking.worker
# Ambiente Notifications, banco e usuário AMQP exclusivos de Notifications.
uv run python -m fulfillflow.notifications.worker
```

## Testes e qualidade

Os testes unitários do simulador, arquiteturais, de health e de problem details
não dependem de banco. Testes de repository/service, APIs persistentes, UI e E2E
usam PostgreSQL 18 real. Configure os três bancos proprietários e o legado isolados abaixo; fixtures
ausentes são explicitamente ignoradas e não constituem validação completa.
O E2E sobe as três APIs em portas TCP distintas e chama o webhook público
por processo externo. Os testes limpam apenas os bancos dedicados informados.

```powershell
docker compose -p fulfillflow-v13-tests -f compose.test.yaml up -d --wait
docker exec --user rabbitmq fulfillflow-v13-tests-broker-1 rabbitmqctl import_definitions /etc/rabbitmq/v13-definitions.json
$env:TEST_DATABASE_URL = "postgresql+psycopg://fulfillflow_core:v11-isolated-core-test@127.0.0.1:18541/fulfillflow_core"
$env:TEST_TRACKING_DATABASE_URL = "postgresql+psycopg://fulfillflow_tracking:v11-isolated-tracking-test@127.0.0.1:18541/fulfillflow_tracking"
$env:TEST_NOTIFICATIONS_DATABASE_URL = "postgresql+psycopg://fulfillflow_notifications:v13-isolated-notifications-test@127.0.0.1:18541/fulfillflow_notifications"
$env:TEST_AMQP_URL = 'amqp://v12_test:v12-isolated-broker-test@127.0.0.1:18542/fulfillflow-v12-test'
$env:TEST_V11_POSTGRES_CONTAINER = 'fulfillflow-v13-tests-db-1'
$env:TEST_V12_RABBITMQ_CONTAINER = 'fulfillflow-v13-tests-broker-1'
$env:TEST_LEGACY_DATABASE_URL = "postgresql+psycopg://fulfillflow_legacy:v11-isolated-legacy-test@127.0.0.1:18541/fulfillflow_legacy"
```

O teste estrutural histórico cria e remove seu próprio container Docker, sem
Locust ou campanha. Após os gates, remova somente a infraestrutura dedicada:
`docker compose -p fulfillflow-v13-tests -f compose.test.yaml down --volumes`.

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

A v1.3 usa `1301_core`, `1203_tracking` e `1301_notifications`, com metadados e
graphs separados. Notifications tem inbox/quarentena/auditoria e terminais;
não possui outbox. As migrations anteriores permanecem preservadas. Os testes
verificam upgrade/check/downgrade/upgrade, FKs somente locais e isolamento das roles.

### Corte offline em cópia descartável

O procedimento não é migração online. Trabalhe somente em cópias novas dos bancos
Core/Tracking congelados e um banco Notifications vazio, com roles próprias.
Não reutilize volumes históricos. Antes de exportar, pare admissão de webhooks e
APIs antigas, drene os workers antigos e confira pelo CLI de cada proprietário:
Tracking `accepted_nonterminal = 0`, `legacy_pending = 0`; inbox somente DONE e
outbox somente SENT em ambos. BLOCKED e quarentena não se resolvem por apagar
história. Resolva pendências antes do corte; em seguida pare todos os escritores.
Nenhum processo antigo pode continuar escrevendo ou lendo Notifications no Core.

Com as URLs dos bancos **descartáveis** já configuradas como na seção de testes,
execute pelo checkout v1.3. O exportador aceita Core `1202_core`/`1301_core`, verifica
seu transporte drenado e correspondência entre recibos APPLIED e registros antigos.
Notifications exige `1301_notifications` e inbox vazia. Os comandos abortam se as
pré-condições falharem; não altere SQL para contorná-las.

```powershell
$archive = Join-Path $env:TEMP ('fulfillflow-legacy-' + [guid]::NewGuid() + '.json')
$env:DATABASE_URL = $env:TEST_DATABASE_URL
uv run python -m fulfillflow.core.notification_legacy --output $archive --expected-database fulfillflow_core
if ($LASTEXITCODE -ne 0) { throw 'Exportação offline recusada' }
$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
uv run alembic -c alembic_core.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration Core falhou' }
$env:DATABASE_URL = $env:TEST_NOTIFICATIONS_DATABASE_URL
uv run alembic -c alembic_notifications.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration Notifications falhou' }
uv run python -m fulfillflow.notifications.cutover --input $archive --expected-database fulfillflow_notifications
if ($LASTEXITCODE -ne 0) { throw 'Importação offline recusada' }
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $archiveHash) { throw 'Arquivo alterado' }
```

O arquivo contém os dados sintéticos originais: preserve-o localmente, sem incluí-lo
no Git. Importar novamente o mesmo arquivo antes de iniciar consumo é idempotente;
conteúdo, contagem ou IDs divergentes recusam a transação inteira. A tabela antiga
fica arquivada, sem acesso pelo runtime Core. Só depois de conferir o inventário
inicie os processos v1.3 apontando exclusivamente às cópias e consulte cada ID antigo
por `GET /api/v1/notifications/{id}` no Core (HTTP interno autenticado). Confira
conteúdo/datas originais e `origin=LEGACY` na consulta de progresso, sem publicação
ou processamento fabricados. Replay do mesmo tracking_event_id produz somente
`LEGACY_EVENT_SUPPRESSED` + inbox DONE, sem nova simulação.

O ensaio automatizado completo usa os mesmos CLIs em processos reais, consulta HTTP
e RabbitMQ, inclui reimportação e replay e não toca o histórico:

```powershell
uv run pytest tests/e2e/test_notification_cutover_cli.py -q
```

## Documentação

- [Demonstração congelada v1.2](docs/DEMO.md): jornada e capturas históricas; atualização v1.3 no IV.
- [DESIGN](DESIGN.md): contratos v1.3 e referências preservadas.
- [RELEASE_PLAN](RELEASE_PLAN.md): incrementos, validação e limites da implementação v1.3.
- [Ferramenta de benchmark](benchmarks/README.md): datasets, validação e artefatos.
- [Revisão da v1.1](benchmarks/V11_REVIEW.md): síntese e índice das evidências.
- [Baseline v1.0 publicada](benchmarks/baselines/v1.0/README.md): referência histórica.

## Transporte durável

Core/Tracking possuem `message_outbox`, `message_inbox` e `message_quarantine`;
Notifications tem somente os mecanismos de recepção/recuperação. ACK confirma persistência
técnica; não representa conclusão de negócio. Itens `BLOCKED` não retomam sozinhos;
o rearme por proprietário e o filtro do novo fluxo Core estão descritos na operação local.

Os testes de transporte exigem RabbitMQ real, além dos bancos já documentados:

```powershell
docker compose -p fulfillflow-v13-tests -f compose.test.yaml up -d --wait
$env:TEST_AMQP_URL = 'amqp://v12_test:v12-isolated-broker-test@127.0.0.1:18542/fulfillflow-v12-test'
uv run pytest tests/integration/test_message_transport.py tests/integration/test_notification_transport.py tests/integration/test_notifications_worker.py -q
```

Configure também os três `TEST_*DATABASE_URL` proprietários conforme a
seção de testes. Use projeto e volumes novos; não reutilize recursos históricos.
A imagem de teste é RabbitMQ 4.2.4 Alpine, fixada por digest no Compose; o cliente
é `aio-pika==9.5.8`. Os testes verificam confirmação, retorno, commit/ACK incerto,
duplicação, lease, esgotamento e recuperação local com a fila vazia.
