# FulfillFlow — Arquitetura alvo v1.3

## 1. Estado, autoridade e referência

Notifications opera como serviço assíncrono, com persistência própria e entrega
simulada, preservando Tracking assíncrono. Contratos/persistência e fluxo integrado
correspondem aos incrementos I/II; recuperação/operação do III e UI/DEMO do IV estão
implementados. O RELEASE_PLAN registra a verificação do aceite funcional.
O desenho foi aprovado, com implementação autorizada do incremento IV após I–III,
com parada no aceite funcional, antes de comparação extensa ou publicação. Commits por assunto e push somente na
branch v1.3 estão autorizados, com conferência da CI do SHA final.
O [RELEASE_PLAN](RELEASE_PLAN.md) registra incrementos e estado;
AGENTS define como trabalhar; README continua descrevendo o runtime executável.

Base congelada: tag `v1.2.0-rc.1`, SHA
`9b445f9b5466cd302c89f1deed7a9c051cb397ae`, branch de evolução
`feature/v1.3-notifications-async`. O
[DESIGN v1.2](https://github.com/campos-labs/fulfillflow/blob/9b445f9b5466cd302c89f1deed7a9c051cb397ae/DESIGN.md)
rege os contratos não substituídos aqui, inclusive sua referência ao
[DESIGN v1.1](https://github.com/campos-labs/fulfillflow/blob/217e29a230689da3bd6359790f0753b41a10a927/DESIGN.md).
O aviso de alvo não implementado naquele DESIGN é histórico; o
[RELEASE_PLAN congelado](https://github.com/campos-labs/fulfillflow/blob/9b445f9b5466cd302c89f1deed7a9c051cb397ae/RELEASE_PLAN.md)
registra o aceite funcional e a proveniência da v1.2.

Este desenho substitui a propriedade e criação local de Notification, sua fronteira
de consulta e os trechos de topologia/operação correspondentes da v1.2. Mantém
admissão 202, HMAC/bytes, comando e resultado Tracking, recibos, transições,
idempotência, ordenação, timeline, conclusão de Order, segurança e HTTP restantes.
Não altera tags, branches anteriores, imagens, locks ou evidências congeladas.
Campanhas extensas seguem suspensas; não há conclusão nova sobre o 503 histórico.

## 2. Comportamento inspecionado e mudança de fronteira

Na base, `core/events.py` reclama o recibo, trava Shipment e cria Notification
somente para uma transição de Tracking com resultado **APPLIED**. O destinatário
vem do email do Order, pelo contrato público de Shipments. A mesma transação
avalia a conclusão do Order; `core/message_handler.py` acrescenta a outbox de
resultado. Recibo repetido retorna a decisão original sem repetir efeitos.

`notifications/service.py` renderiza conteúdo determinístico: `EMAIL`, resultado
`SIMULATED` ou `FAILED` para falha esperada e sanitizada. O banco Core impõe
unicidade de `tracking_event_id` e FK restritiva de `shipment_id`. Lista, detalhe
REST/HTML e contadores do dashboard consultam esse banco diretamente. Na v1.3,
essas leituras e a simulação passam ao serviço Notifications.

Não geram notificação: `NO_STATE_CHANGE`, `IGNORED_STALE`,
`IGNORED_INVALID_TRANSITION`, rejeição, duplicata, criação de Shipment,
cancelamento manual ou conclusão de Order por si só. O nome do evento abaixo
não amplia esse conjunto. Uma transição posterior válida gera outra notificação,
com sua própria identidade; não se deduplica por Shipment ou por status.

## 3. Serviços e comunicação

| Processo | Responsabilidade e dependências |
| --- | --- |
| Core API | Entrada pública, UI, Orders/Shipments/Carriers; banco Core; consultas HTTP a Tracking e Notifications |
| Core worker | Aplicar comandos Tracking; banco Core; publicar resultado Tracking e fato para Notifications |
| Tracking API / worker | Responsabilidades, banco e dois fluxos da v1.2 preservados |
| Notifications API | HTTP interno autenticado de leitura e saúde; somente banco Notifications |
| Notifications worker | Receber fatos, persistir inbox técnica e simular entrega; somente banco Notifications e RabbitMQ |
| PostgreSQL 18 / RabbitMQ | Mesmas tecnologias e instâncias locais; terceiro banco/role e novo fluxo durável |

API e worker de cada serviço têm entrypoints/processos separados e implantação
independente. Uma imagem de aplicação pode servir a todos; não exigir repositório
ou imagem por processo. Só Core API publica porta de aplicação no host. O worker
Notifications não tem HTTP; sua API de leitura resolve a necessidade de consulta.
Não existe callback de Notifications para Tracking nem fluxo de resultado de
notificação para Core. Core não mantém réplica dos registros de Notification.

Cada role acessa apenas seu banco. Não há FK, join, sessão SQL ou import de
implementação entre serviços. DTOs explícitos em `contracts` não dependem de ORM
ou cliente de transporte; `shared` continua sem negócio/infraestrutura.
Módulos Core usam interfaces públicas locais; o cliente HTTP de Notifications
usa DTOs de contrato, sem importar seu domínio, serviços, schemas internos ou ORM.

## 4. Fato publicável e contrato de mensagem

Novo evento **`shipment.status_changed.v1`**, Core → Notifications: uma transição
de Shipment decorrente de comando Tracking foi aplicada e será confirmada no
commit Core. É distinto de `tracking.result.v1`, que continua exclusivo de Tracking,
com schema e significado inalterados. Notifications não consome comando nem resultado.

Reutilizar a forma de envelope v1.2, validada por tipo explícito e extras proibidos:

| Campo | Significado neste evento |
| --- | --- |
| `schema_version`, `type` | `1`, `shipment.status_changed.v1` |
| `message_id` | UUID criado uma vez e persistido na outbox Core |
| `event_id` | `ApplyEventCommand.event_id`, identidade causal já estável do efeito |
| `correlation_id` | Inbox original de Tracking |
| `causation_id` | `message_id` do comando recebido pelo Core |
| `request_id` | Correlação original preservada, sem regeneração em retry |
| `created_at` | `decided_at` do recibo Core, em UTC pelo Clock; instante da decisão, não do commit |
| `payload` | Somente `shipment_id` UUID, `resulting_status` canônico e `recipient` snapshot |
| `payload_sha256` | SHA-256 dos bytes canônicos do payload |

`event_id` será também `Notification.tracking_event_id`; ele existe antes da
timeline de Tracking e não exige consulta a ela. Não duplicar esse ID no payload.
`resulting_status` admite somente os seis status do comando atual (`POSTED`,
`IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED`, `EXCEPTION`, `RETURNED`). `recipient`
é o email capturado durante a aplicação, sem espaços externos, não vazio e com
máximo de 254 caracteres. Notifications não consulta Order para reobter destinatário.
O canal fixo EMAIL e o mapeamento determinístico de template pertencem a Notifications;
não transportar texto do Carrier, corpo renderizado, dados do Order ou raw webhook.

Manter serialização canônica/UTC da v1.2, limite de 64 KiB UTF-8, validação dos
campos e correspondência de propriedades AMQP/envelope. Hash não autentica remetente:
credenciais/permissões do broker autenticam o fluxo. Destinatário é dado restrito;
não registrar payload, email, assinatura, segredo ou bytes de quarentena em logs.

## 5. Publicação atômica e substituição da criação local

Uma transação Core confirma: inbox técnica do comando, recibo, alteração de
Shipment, avaliação de Order, outbox `tracking.result.v1`, outbox
`shipment.status_changed.v1` quando APPLIED e `DONE` da inbox técnica. A nova
outbox substitui **integralmente** `NotificationsPublic.record_applied_transition`
nesse caminho. Não há dual-write de Notification local/remota, fallback síncrono,
publicação no router ou simulação no Core. Todos os caminhos de aplicação do Core
devem passar por essa coordenação; seeds funcionais também não criam efeitos locais.

Falha ao gravar a outbox local reverte o fato e todos os efeitos da transação.
Isso é diferente de Notifications indisponível: a transação não consulta esse
serviço nem faz I/O AMQP. Preservar locks de Shipment antes de Order e `READ COMMITTED`.
Uma sessão por operação; nenhum lock/conexão SQL retido durante HTTP, publish ou ACK.

Unicidade `(type, event_id)` da outbox é a autoridade final junto ao recibo.
Recibo duplicado retorna o resultado original, sem novo fato, mensagem, destinatário
ou timestamp. Uma pendência existente é republicada com os mesmos bytes. Ausência
indevida da outbox de um recibo v1.3 APPLIED é violação de integridade, não licença
para reconstruí-la de dados mutáveis. Recibos legados seguem a seção 9.

O Core worker mantém dois loops explícitos de publicação, com claims filtrados
por tipo **antes do limite do lote**, conexões/canais, lotes e backoffs separados.
Uma falha ou backlog de Notifications não ocupa a frente da fila de resultados
Tracking nem pausa sua publicação. Não acrescentar um processo só para separar
esses loops. Uma falha inesperada de loop obrigatório encerra o worker; dependência
indisponível é estado controlado daquele loop, sem interrupção dos demais.
Essa independência cobre backlog e falhas controladas específicas do fluxo
Notifications. Processo, banco, broker e recursos compartilhados continuam sujeitos
a falhas comuns; os dois publicadores não são domínios independentes de falha.

Publicação usa os mecanismos existentes: `PENDING/LEASED/SENT/BLOCKED`, claim por
`FOR UPDATE SKIP LOCKED`, token/lease, mensagem persistente, `mandatory`, confirms
e atualização condicionada ao token após liberar a sessão SQL. Timeout/retorno ou
confirm perdido não provam envio; repetir com a mesma identidade é permitido.
`SENT` significa confirmação do broker, nunca simulação concluída.

## 6. Dados, idempotência, processamento e recuperação

Notifications possui metadata, histórico Alembic e banco próprios: registros
Notification, inbox técnica, quarentena e auditoria de rearme. Não precisa de
outbox, publicador ou tabela vazia para imitar os outros serviços. UUIDs causais de
Shipment/Tracking são referências externas sem FK; a unicidade nomeada de
`tracking_event_id` permanece. Usar PostgreSQL nativo UUID/timestamptz, enums varchar
com checks nomeados e índices para pendências e filtros existentes.

O consumidor aceita somente o evento autorizado. Persiste envelope imutável e
identidade/hash na inbox técnica e só então dá ACK. Commit incerto: não confirmar,
fechar canal e reconectar com backoff. Duplicata exata por `message_id` ou por
`(type, event_id)` é inócua. Reutilização de identidade com payload ou metadados
imutáveis divergentes é isolada em quarentena, preservando original e diagnóstico,
sem novo efeito. Contrato desconhecido, inválido ou excessivo também exige
quarentena durável limitada antes de ACK; falha ao preservar não autoriza descarte.

Se o efeito já tem registro importado `LEGACY`, o processador preserva integralmente
esse registro e não simula novamente. Na mesma transação, grava a mensagem realmente
recebida em quarentena com motivo `LEGACY_EVENT_SUPPRESSED` e conclui sua inbox
técnica como `DONE`. Esse DONE significa disposição técnica concluída, não nova
entrega. O diagnóstico distingue essa supressão de duplicata ASYNC: não há envelope
histórico para provar igualdade de conteúdo, portanto não inventar hash, envelope
ou tentativa anterior nem comparar o novo payload como se fosse o original.
Uma reentrega exata da mensagem técnica já registrada permanece idempotente;
divergências nessa identidade técnica seguem a quarentena de conflito normal.
Falha ao persistir quarentena/DONE reverte ambos e deixa o trabalho retomável.

O processador local reclama a inbox com lock, renderiza o conteúdo atual da v1.2
e confirma **Notification + `DONE` da inbox** em uma transação SQL local. Não chama
provedor nem outro serviço. `Notification.id` é criado na primeira gravação
confirmada e preservado; sua identidade lógica é `tracking_event_id`. Retry após
rollback pode gerar outro UUID não observado, nunca uma segunda linha confirmada.
`created_at` e `simulated_at` são obtidos pelo Clock durante a simulação (iguais no
sucesso atual); deixam de representar execução na transação Core. O instante do
fato permanece no envelope. Falha esperada de renderização/simulação persiste
`FAILED`, detalhe sanitizado e `simulated_at=null`; é terminal, com inbox `DONE`.

Erro operacional não cria `FAILED`: mantém trabalho `PENDING` ou `RETRY_WAIT`,
com até cinco tentativas por geração e esperas 1, 5, 15 e 60 s; esgotamento ou erro
inesperado leva a `BLOCKED`. Usar savepoint/lock e persistência de tentativas da
v1.2; queda da conexão reverte a transação inteira, deixando trabalho retomável.
Indisponibilidade global de banco/broker pausa com backoff 1–30 s sem consumir
rapidamente tentativas por item. A política aplica-se também à nova outbox Core.

Reinício retoma inbox após ACK mesmo com fila RabbitMQ vazia. Rearme local do
proprietário exige ID, hash esperado, banco alvo e motivo; registra auditoria e
nova geração atomicamente, sem mudar payload/identidade. Não rearma `DONE`, não
ressimula `SIMULATED/FAILED` nem reenvia quarentena automaticamente. Não criar
endpoint público administrativo. Ausência de FIFO não altera o fato consumido:
eventos aplicados distintos são simulados independentemente, sem reavaliar estado
atual do Shipment ou suprimir evento porque outro mais recente chegou primeiro.

Esta entrega demonstra **um registro simulado por efeito**, não exactly-once de
transporte ou de provedor externo. Aqui o único efeito de entrega é SQL local,
atômico com a inbox. Um provedor real traria uma fronteira remota e resultado
incerto que essa transação não cobre; ele está fora do escopo.

## 7. Consultas, indisponibilidade e experiência operacional

Notifications API oferece `/internal/v1/notifications` (lista),
`/internal/v1/notifications/{notification_id}` (detalhe),
`/internal/v1/notification-counts` (contagens por SIMULATED/FAILED) e
`/internal/v1/notification-status/{tracking_event_id}` (progresso por efeito).
São consultas GET. Contagens retornam `simulated` e `failed` inteiros não negativos,
sem filtros, e `observed_at` UTC; lista/detalhe usam os schemas públicos preservados.
Autenticação via `X-FulfillFlow-Internal-Token` com segredo próprio Core↔Notifications e
comparação constante; sem cookies, exposição pública ou autorização via URL do
cliente. Reutilizar cliente HTTP técnico/erros/request ID existentes com destino
explícito e validação dos DTOs; não reutilizar o segredo dos Carriers.

Core preserva `GET /api/v1/notifications` e `/{notification_id}`, campos de leitura,
filtros `status`, `shipment_id`, `created_from/to` inclusivos, paginação
1/25/máximo 100 e ordenação `created_at DESC, id DESC`. Lista contém apenas registros
terminais; lista vazia não prova ausência de trabalho. Detalhe desconhecido é 404
somente após resposta válida do proprietário. Falha de conexão, timeout, auth
interna ou resposta inválida vira 503 `application/problem+json` sanitizado, nunca
lista vazia, 404 artificial ou leitura de tabela antiga. A requisição de consulta
tem timeout total configurável de 2 s inicialmente, sem retry HTTP automático.

Nova consulta Core: `GET /api/v1/notification-status/{tracking_event_id}`. Core lê
recibo/outbox pelo contrato público local, fecha a transação e só então consulta
Notifications quando necessário. Retorna 404 para recibo inexistente; decisão que
não gera notificação retorna `required=false`, sem consulta remota. Para APPLIED,
`required=true` e observações separadas, sem fingir snapshot distribuído:

- `publication`: estado local `PENDING/LEASED/SENT/BLOCKED` ou null para legado;
- `processing`: `NOT_RECEIVED/PENDING/RETRY_WAIT/BLOCKED/DONE`, observado no serviço;
- `notification_id`, `status` (`SIMULATED/FAILED`) e `simulated_at`: nulos até haver
  registro terminal; para legado importado, `processing=null` e registro original;
- `origin`: `ASYNC` ou `LEGACY`; `core_observed_at` e `notifications_observed_at`
  identificam instantes de leitura, além de `tracking_event_id`.

Na variante `required=false`, preencher somente `tracking_event_id`, `required`
e `core_observed_at`; os demais campos acima são null. Para APPLIED sem outbox,
apenas um registro remoto explicitamente importado como `LEGACY` permite a
variante legada. `NOT_RECEIVED` ou registro `ASYNC` sem outbox é 503 de integridade,
nunca classificação de legado por ausência de trabalho local.

O endpoint interno de progresso retorna 200/`NOT_RECEIVED` para identidade ainda
desconhecida em Notifications; quem decide a existência do fato é Core. O estado
terminal remoto prevalece visualmente sobre publicação ainda pendente por confirm
perdido. Dados inconsistentes são erro controlado, não conclusão inventada.
Dependência remota indisponível retorna 503; a UI conserva a última observação e
o resultado Core/Tracking, identificando falha de consulta. Não inferir perda,
ausência de notificação ou entrega a partir de timeout, `SENT` ou fila vazia.

Notifications fora do ar não desfaz Shipment/Order nem bloqueia a conclusão
Tracking: Core persiste o fato, o broker ou a outbox acumulam pendência e o serviço
retoma após recuperação. A ordem entre resultado Tracking e simulação é livre.
O compartilhamento físico de PostgreSQL/broker ainda permite falhas ou exaustão
globais; não prometer isolamento de recursos ou armazenamento ilimitado.

HTML preserva rotas, filtros, templates no servidor, CSRF e CSP. Dashboard obtém
contagens remotas em consulta independente das contagens Core e as mostra como
indisponíveis em falha, nunca zero. Página de notificação indisponível apresenta
erro controlado; páginas de Order/Shipment continuam utilizáveis. Após resultado
APPLIED, UI pode observar a nova consulta a cada segundo por até 30 s, encerrando
em terminal, bloqueio, navegação ou prazo. Renovar observação apenas consulta;
não reenvia webhook nem rearma trabalho. Prazo não é falha de negócio.

DEMO mostrará separadamente 202, conclusão Tracking/Order e simulação, incluindo
worker Notifications parado e retomada sem reenvio. O simulador de Carriers mantém
seu contrato de esperar Tracking; concluir seu cenário não comprova Notifications.
O aceite verifica as notificações pela consulta própria. Novas capturas v1.3 não
substituem as anteriores; DEMO/README descrevem o comportamento executável.

## 8. Implantação, operação e reutilização delimitada

Manter versões/digests atuais de PostgreSQL/RabbitMQ e `uv.lock`; nenhuma ferramenta
ou dependência de produção nova é necessária. Novo fluxo: exchange direct durável
`shipment.status_changed.v1`, fila classic durável `shipment.status_changed.v1.queue`,
routing key igual ao tipo, sem exclusive/auto-delete/TTL. Declarar binding antes
de iniciar publicação, mesmo com consumidor desligado. Não alterar os dois fluxos
Tracking. No projeto v1.3, usar vhost próprio, usuário Notifications com consumo
apenas da nova fila; Core recebe permissão de publicar no novo fluxo, Tracking não.
Testar permissões efetivas, inclusive declaração passiva/ativa necessária ao startup.
AMQP e management permanecem internos; sem cluster ou alegação de HA.

Configuração por processo valida role `notifications`, DSN/banco esperado e head
próprio; worker exige AMQP, API exige segredo interno; não exigir HMAC ou sessão
HTML de Notifications. Core recebe URL interna e timeout de Notifications e o
segredo desse vínculo. Nunca fornecer DSNs alheios aos processos. Health da API
distingue liveness de readiness (banco/head); não depende de worker/broker para
servir registros. Worker usa heartbeat/comando local, sem servidor HTTP, com
estados de receive/process; Core distingue os dois publishers no diagnóstico.
Loops obrigatórios mortos não podem aparentar saúde; backlog isolado não equivale
a processo morto. Dependências e bloqueios devem estar visíveis separadamente.

Reutilizar leases, ACK durável, retry, quarentena, auditoria, cliente HTTP e
supervisão somente onde os contratos acima se aplicam. O código v1.2 infere dois
fluxos a partir de core/else, fixa tipos nos checks SQL e exige publish/receive/process
na saúde: ajustar explicitamente esses pontos e Import Linter, sem framework
genérico, registry dinâmico, outbox fictícia em Notifications ou refatoração de Tracking.

Parâmetros iniciais: um processo por worker, prefetch 8, lote 20 por publisher,
polling local 500 ms, lease 30 s, confirm timeout 5 s, shutdown até 15 s. Parar novos
claims/entregas, aguardar trabalho em curso até o limite e fechar canais/sessões;
trabalho incompleto permanece durável. Compose deve dar margem ao encerramento
e permitir iniciar/reiniciar Notifications sem reiniciar Core/Tracking. Migrações
são jobs explícitos por proprietário; não criar schema na API/worker nem condicionar
readiness do Core ao serviço Notifications.

| Processo | CPU | Memória | Pool SQL / overflow |
| --- | --- | --- | --- |
| Core API / worker | 0,5 / 0,5 | 384 / 384 MiB | 2 / 0; 3 / 0 |
| Tracking API / worker | 0,5 / 0,5 | 384 / 384 MiB | 2 / 0; 3 / 0 |
| Notifications API / worker | 0,5 / 0,5 | 384 / 384 MiB | 2 / 0; 3 / 0 |
| PostgreSQL | 2 | 2560 MiB | — |
| RabbitMQ | 0,5 | 512 MiB | — |

Limites usados no smoke funcional, sem validação de capacidade: aplicação 3 CPUs/2304 MiB;
topologia 5,5 CPUs/5376 MiB, excluindo jobs de migração/testes. Registrar pools,
concorrência e parâmetros efetivos no aceite. Não afirmar equivalência de orçamento
com v1.2 nem dimensionar por uma quantidade desejada de componentes.

Logs JSON controlados preservam IDs/correlação e distinguem publicação, recepção,
tentativa e conclusão. Diagnóstico proprietário mostra contagem/idade, tentativas,
geração, motivo, última atividade e rearme; separar outbox Core, inbox Notifications,
simulações terminais e filas ready/unacked. Não somar etapas como eventos únicos.
Prometheus/OTel mais amplos continuam pendentes; logs não comprovam tracing.

## 9. Migrações, legado e dados de demonstração

Usar projeto/volumes v1.3 novos e identidade estável do nó RabbitMQ ao recriar seu
container. Três históricos Alembic independentes. Evoluir Core para nova outbox/tipos
e retirar a tabela Notification do modelo de negócio ativo; banco Notifications
nasce com suas tabelas. Não modificar migrations antigas ou o head Tracking sem
necessidade demonstrada. Preservar a reconstrução de metadata legada usada pelas
migrations antigas e os caminhos de seeds históricos, sem adaptá-los silenciosamente.

Além de banco limpo, verificar upgrade **somente em cópia descartável** da v1.2.
O corte é offline: suspender novos webhooks e escritores antigos, drenar trabalho
retomável v1.2 e inventariar bloqueados; exportar Notifications pelo proprietário
Core e importar pelo proprietário Notifications em comandos locais separados.
Não há conexão SQL cruzada em runtime. Bloqueados ou pendências não resolvidas
impedem o corte dessa cópia; não descartá-los nem misturar escritores v1.2/v1.3.

Importação é idempotente e verifica contagens e conteúdo completo por identidade;
preserva IDs, destinatário, mensagem, status, erros e timestamps. Conteúdo divergente
interrompe o corte. Registros copiados são `LEGACY`, sem inventar envelope, hash,
tentativa ou tempo de recepção. Preservar tabela original Core como arquivo inativo,
sem writes/leituras de runtime, e ativar apenas Notifications como proprietário
consultável após verificação. Não apagar os dados originais nessa entrega.
Representar essa tabela explicitamente na metadata de migrations do Core, separada
do modelo de negócio ativo, para Alembic reconhecer sua retenção sem propor DROP
nem desabilitar a detecção de drift das demais tabelas.

Recibos históricos APPLIED já correspondem a registros legados: nunca fabricar
outbox para ressimulação nem usar ausência de outbox como pendência nova. O corte
verifica esse vínculo e a consulta por ID original no novo proprietário. O alvo
não promete upgrade online/rolling entre v1.2 e v1.3 nem rollback de esquema após
novos eventos; recuperar falha do ensaio restaurando apenas a cópia descartável.

Seed funcional v1.3 usa dados sintéticos determinísticos e o fluxo autorizado;
seus IDs não colidem com campanhas antigas. Reexecução não duplica efeitos.
Não portar loadgen, mudar dataset/pesos de benchmark ou tocar volumes históricos.

## 10. Verificação, aceite e limites

Testes acompanham cada incremento do RELEASE_PLAN, começando por contratos,
constraints e migrations. PostgreSQL real para transações/locks/concorrência;
RabbitMQ real para routing/permissões, confirms/ACK e reinício. Mocks apenas
complementam falhas focais; CI não omite silenciosamente caminhos críticos.

Verificar APPLIED e todas as exclusões da seção 2, destinatário congelado, ambos
adapters, uma notificação por efeito, duplicatas simultâneas/divergentes, ordem de
chegada invertida, rollback conjunto Core e rollback conjunto Notifications,
queda antes/depois de commit/confirm/ACK, recuperação com fila vazia, esgotamento,
rearme auditado, quarentena e lease antiga. Demonstrar conclusão de Shipment/Order
e Tracking durante indisponibilidade de API/worker/banco Notifications, e ausência
de starvation de resultados por falha do novo publisher. Validar também ausência
de SQL retido em I/O, isolamento de roles, consultas/503/UI e legado sem ressimulação.
Incluir teste focal de mensagem nova para efeito LEGACY: registro preservado,
quarentena diagnosticável e DONE atômicos, replay sem nova simulação e sem fabricar
metadados históricos; contrastar com duplicata ASYNC verificável.

Preservar gate global de 80%, cobertura completa das regras críticas e testes de
regressão Tracking. Aplicar Ruff/formatação, Mypy, Import Linter, pytest/cobertura,
Alembic em cada banco afetado e build/smoke ao mudar runtime. O aceite funcional
comprova fluxo, recuperação e operação limitada; não prova capacidade, estabilidade
prolongada, entrega externa, HA ou observabilidade ampla.

**Parar após o aceite funcional**, antes de campanha extensa ou publicação.
Comparação futura exige decisão/protocolo próprios, distinguindo eventos oferecidos,
aceitos, Tracking concluído e notificações simuladas, latências/backlogs/drain,
recursos totais e instrumentação. Sem publicação, tag, merge ou evolução automática.
Não entram provedores reais, cloud, Kubernetes, GitOps, autoscaling, novos brokers,
framework de eventos ou componentes sem necessidade deste fluxo.
