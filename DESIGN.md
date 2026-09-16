# FulfillFlow — Arquitetura alvo v1.2

## 1. Estado e autoridade

Este documento especifica a evolução autorizada de Tracking para admissão durável
e coordenação por mensagens. **O alvo ainda não está implementado.** O estado de
cada incremento pertence ao [RELEASE_PLAN.md](RELEASE_PLAN.md); o README descreve
somente o comportamento executável. AGENTS define o método de trabalho.

A base é `v1.1.0-rc.1`, commit `217e29a230689da3bd6359790f0753b41a10a927`.
O [DESIGN congelado da v1.1](https://github.com/campos-labs/fulfillflow/blob/217e29a230689da3bd6359790f0753b41a10a927/DESIGN.md)
permanece a referência dos contratos não substituídos aqui, inclusive regras de
domínio, adapters, segurança, UI e schemas públicos. Suas seções 1–29 são lidas
com as substituições da seção 30. Referências históricas a essas seções continuam
apontando àquela versão; não devem ser reinterpretadas pela numeração deste alvo.

Tag, imagens, dependências e evidências congeladas da v1.0/v1.1 não serão alteradas.
A comparação v1.1 está suspensa e incompleta; a pré-release funcional não comprova
capacidade, estabilidade geral ou causa do 503 histórico.

## 2. Escopo e topologia

Tracking recebe o webhook por HTTP, persiste o trabalho e responde sem esperar os
efeitos no Core. Tracking envia um **comando** pelo RabbitMQ; Core aplica efeitos
locais e publica um **resultado** pelo RabbitMQ; Tracking conclui inbox e timeline.
Não haverá chamada HTTP de aplicação do comando nem fallback síncrono nesse fluxo.

| Componente | Responsabilidade |
| --- | --- |
| Core API | Entrada pública, UI, Orders, Shipments, Carriers e consultas internas |
| Tracking API | Autenticação do webhook, admissão e consultas de Tracking |
| Core worker | Receber comandos, aplicar efeitos e publicar resultados |
| Tracking worker | Publicar comandos e receber/aplicar resultados |
| PostgreSQL 18 | Uma instância, dois bancos e roles segregados |
| RabbitMQ | Transportar comandos e resultados duráveis |

APIs e workers do mesmo serviço compartilham código e banco próprios, com processos
e entrypoints distintos. Worker não é um novo domínio nem expõe portas HTTP.
Uma imagem de aplicação pode oferecer os quatro entrypoints. Somente Core API
publica porta no host. Notifications **permanece no Core**, com persistência local
e entrega simulada, sem consumidor independente ou integração externa.

Permanecem HTTP: cliente→Core, encaminhamento Core→Tracking e consultas de Tracking
ao cadastro de Carriers e às projeções de Shipments no Core. Nenhuma conexão,
transação ou lock SQL pode permanecer retido durante HTTP ou publicação AMQP.
A admissão ainda depende da consulta de Carrier no Core; esta versão não promete
aceitar webhooks durante indisponibilidade completa do Core.

Não entram: extração de Notifications, gateway, Kubernetes, cloud, autoscaling,
broker em cluster, integração real de transportadoras/notificações ou matriz
extensa de desempenho. Esses itens não são critérios de conclusão da v1.2.

## 3. Contratos preservados

- Python 3.13, FastAPI, Pydantic v2, SQLAlchemy async, psycopg 3, Alembic, PostgreSQL
  18, Jinja2/HTMX/Bootstrap locais, uv e as verificações existentes permanecem.
- HMAC-SHA256 usa timestamp, ID externo e bytes originais conforme o contrato
  congelado. Autenticar antes de parsing/persistência; preservar limites, janela,
  validação dos headers, comparação constante e `raw_body` byte a byte.
- A restrição única `(carrier_id, external_event_id)` e o hash dos bytes decidem
  idempotência. Identidade, timestamps originais e resultado não são regenerados.
- Preservar `ApplyEventCommand`, seu hash canônico, a identidade determinística do
  evento e os resultados aplicados/rejeitados já existentes. Envelope de transporte
  não altera o conteúdo ou a identidade do comando de negócio.
- Shipment mantém transições e os resultados `APPLIED`, `NO_STATE_CHANGE`,
  `IGNORED_STALE` e `IGNORED_INVALID_TRANSITION`. Ordenação permanece a tupla
  `(occurred_at, received_at, external_event_id)`; TrackingEvent é append-only.
- Core conserva a transação local de recibo, Shipment, Notification e avaliação
  de conclusão do Order. Locks de Shipment precedem Order; `READ COMMITTED`,
  constraints e locks reais continuam sendo as autoridades de concorrência.
- Não há acesso SQL entre bancos, imports entre implementações de serviços,
  transação distribuída, promessa de exactly-once no transporte ou atomicidade global.

## 4. API: aceitação e conclusão

### 4.1 Admissão

`POST /api/v1/carriers/{carrier_code}/events` mantém rota, headers e payload de
entrada. **A resposta de evento novo passa de 200 com conclusão para 202 com
aceitação durável.** É uma alteração incompatível de semântica para clientes que
dependem de conclusão imediata, mesmo preservando o prefixo `/api/v1`. UI,
simulador, documentação e testes devem ser adaptados no mesmo incremento de ativação.
Não declarar compatibilidade integral com a v1.1 nem mudar as imagens antigas.

Após autenticação e consulta de Carrier, Tracking executa uma transação local:

1. Resolve a identidade do inbox e a restrição única, preservando bytes e hash.
2. Normaliza pelo adapter permitido e congela o comando de negócio.
3. Persiste inbox `RECEIVED`, comando e outbox na mesma transação.
4. Somente após commit confirmado devolve 202. Não aguarda publicação no broker.

A separação histórica entre persistir a recepção e normalizar é substituída nesta
admissão: um evento aceito para processamento sempre tem comando e outbox duráveis.
Falha permanente de parsing/normalização autenticada persiste inbox `REJECTED`
e devolve o erro documentado, sem criar trabalho de aplicação.

| Condição | Resposta |
| --- | --- |
| Evento novo aceito | 202, mesmo que um worker conclua antes da resposta chegar |
| Mesmo ID/hash ainda pendente | 202, referenciando o inbox original |
| Mesmo ID/hash já processado | 200, `DUPLICATE` e resultado original |
| Mesmo ID/hash rejeitado | Erro permanente original, sem recriar trabalho |
| Mesmo ID com bytes/hash diferentes | 409, sem sobrescrever o original |
| HMAC, tamanho, media type ou Carrier inválido | Códigos e ausência/presença de persistência do contrato congelado |
| Falha de infraestrutura na admissão | 503 sanitizado; commit incerto não significa ausência de efeitos |

O schema 202 contém `inbox_event_id`, `external_event_id`, `status=RECEIVED`,
`received_at` e `request_id`. `Location` aponta à consulta pública do inbox e
`Retry-After: 1` orienta polling. Duplicatas preservam a correlação original no
trabalho; o request ID da resposta corresponde à requisição atual. Uma reentrega
idêntica pode esclarecer admissão cujo commit/resposta ficou incerto.

Broker indisponível não impede 202 se as dependências de admissão e a transação
local estiverem disponíveis. Isso acumula trabalho e não garante capacidade
ilimitada de armazenamento ou prazo de conclusão.

### 4.2 Consulta

`GET /api/v1/carrier-events/{inbox_event_id}` preserva os campos existentes e expõe
o resultado terminal, `tracking_event_id` quando aplicável e `completed_at`.
O timestamp de conclusão é obtido pelo Clock durante a transação de finalização;
não representa uma medição exata do instante do commit ou do fsync.
`processed_at` preserva a semântica existente, inclusive a decisão original de
rejeição no Core; `completed_at` registra separadamente a finalização no Tracking.

O estado de negócio permanece `RECEIVED`, `PROCESSED` ou `REJECTED`. O progresso
local adicional distingue `QUEUED`, `AWAITING_RESULT`, `COMPLETED` e
`BLOCKED_LOCAL`, derivado do trabalho durável de Tracking. `AWAITING_RESULT` não
prova que Core está processando: não se inventa uma visão global sincronizada.
Erros posteriores são consultados no resultado; não podem alterar o HTTP já enviado.
Falhas operacionais não viram rejeições de negócio. IDs desconhecidos retornam 404.

## 5. Mensagens, identidade e segurança

São dois contratos explícitos: `tracking.apply.v1` (comando Tracking→Core) e
`tracking.result.v1` (resultado Core→Tracking). Resultado descreve uma decisão
já persistida e não é um evento genérico destinado a futuros consumidores.

Envelope versionado, com campos tipados e extras rejeitados:
`schema_version`, `type`, `message_id`, `event_id`, `correlation_id`,
`causation_id`, `request_id`, `created_at`, `payload` e `payload_sha256`.
`correlation_id` é o inbox original; no resultado, `causation_id` é o ID da mensagem
de comando; no comando, é o ID do inbox original. IDs de mensagem são criados uma
vez e persistidos. O payload do comando usa `ApplyEventCommand`; o payload de
resultado contém `command_sha256` e `result`, cuja união aplicada/rejeitada
permanece inalterada. Validar esse vínculo no Tracking antes de finalizar.

Serialização canônica e UTC são determinísticos. O hash de transporte cobre o
payload canônico e não substitui HMAC ou autenticação do broker. Propriedades AMQP
e envelope devem coincidir. Limitar o envelope a 64 KiB em UTF-8 e testar os limites
dos schemas, sem transportar raw webhook, segredos ou assinaturas.
Não usar pickle, import dinâmico, headers fornecidos pelo cliente para roteamento
ou desserialização executável.

Outbox possui unicidade por `(type, event_id)`; a recepção técnica verifica tanto
`message_id` quanto identidade lógica e hash. Duplicata idêntica reutiliza o
resultado. Reutilização de identidade com conteúdo divergente é conflito técnico,
preserva o original e é isolada para diagnóstico, sem efeitos adicionais.

## 6. Persistência, entrega e recuperação

### 6.1 Outboxes

Cada serviço possui outbox no próprio banco. A gravação acompanha a transação que
cria o fato publicável. Estados: `PENDING`, `LEASED`, `SENT`, `BLOCKED`; armazenar
tentativas, próxima tentativa, lease/token, identidade e motivo controlado.

O publicador reclama lotes com `FOR UPDATE SKIP LOCKED`, confirma a transação,
publica fora de qualquer sessão SQL retida e exige mensagem persistente,
`mandatory` e publisher confirm. Somente confirmação positiva sem retorno permite
marcar `SENT`, condicionada ao token da lease. Timeout, retorno ou perda de conexão
não equivalem a publicação concluída. Lease expirada pode ser retomada; um dono
antigo não pode sobrescrever uma lease nova. Confirmação perdida pode gerar nova
publicação da mesma mensagem, nunca identidade nova.

### 6.2 Recepção técnica e aplicação

Cada consumidor primeiro valida e grava uma inbox técnica durável no banco do
serviço receptor. **ACK ocorre após essa persistência**, transferindo a
responsabilidade de recuperação do RabbitMQ para PostgreSQL. ACK não significa
conclusão de negócio. Se o commit é incerto, não confirmar; fechar o canal e
reconectar com espera, permitindo redelivery idempotente.

Um processador local reclama trabalho da inbox técnica com lock e executa apenas
operações SQL locais. Estados: `PENDING`, `RETRY_WAIT`, `DONE`, `BLOCKED`.
Não há AMQP dentro dessa transação.
Uma falha pode reverter os efeitos sem perder a atualização de tentativa: usar
savepoint dentro da transação que mantém o lock do trabalho. Se a conexão cair,
o rollback integral deixa o item retomável; não fabricar um commit de diagnóstico.

| Serviço | Uma única transação de aplicação |
| --- | --- |
| Core | Inbox técnica + recibo/efeitos locais + outbox de resultado + `DONE` |
| Tracking | Inbox técnica de resultado + timeline/finalização do inbox de negócio + `DONE` |

No Core, um recibo existente idêntico fornece o resultado original; nunca repetir
Notification, transição ou avaliação com novos timestamps. No Tracking, validar
identidade e hash do comando antes de aplicar o resultado. Finalização duplicada
é inócua; resultado divergente não substitui uma conclusão anterior.

Reinício antes do commit deixa trabalho retomável; reinício após commit e antes
de ACK/publicação pode causar duplicatas toleradas. O resultado permanece
recuperável sem nova chamada do cliente enquanto o trabalho estiver retomável.
Itens `BLOCKED` exigem o rearme auditável da seção 6.3; não há recuperação automática
ilimitada. A garantia depende dos armazenamentos duráveis e de recuperação das
dependências; perda permanente de disco não é coberta.

### 6.3 Tentativas e mensagens inválidas

Falha transitória atribuível a um item permite até cinco tentativas por geração,
com esperas de 1, 5, 15 e 60 s. Contador, prazo e motivo são duráveis. Esgotamento
leva a `BLOCKED`; erro permanente de negócio produz resultado terminal, sem retry.
Erros inesperados ou conflitos de contrato são bloqueados com diagnóstico.

Indisponibilidade global de banco/broker pausa o componente com backoff limitado,
sem consumir rapidamente as tentativas de todos os itens. Separar essa condição
de erro de item por categorias explícitas; não usar requeue imediato em loop.

Mensagem malformada/desconhecida é isolada em quarentena durável local antes de
ACK. Guardar bytes técnicos com limite e acesso restrito, fingerprint e motivo;
nunca despejá-los em logs. Se não for possível preservar, não confirmar nem
descartar silenciosamente. Não depender de TTL ou dead-lettering para recuperar
trabalho que já foi aceito.

Uma CLI local do serviço proprietário poderá rearmar um item bloqueado por ID,
hash esperado e motivo explícito, criando geração auditável de tentativas. Não
reescreve payload/identidade, não reprocessa `DONE` nem opera bancos históricos
por padrão. Não adicionar endpoint público administrativo.

### 6.4 Concorrência e ordenação

Não presumir FIFO global entre usuários, filas ou réplicas. Core continua a
decidir transições com locks e a tupla temporal congelada. Um comando antigo pode
concluir como stale; todos os resultados válidos integram a timeline sem regressão
de estado. A ordem de chegada dos resultados não muda suas decisões persistidas.

## 7. Runtime e dados

Adicionar somente RabbitMQ 4.x e cliente async `aio-pika`, com dependências
transitivas necessárias. No incremento I, fixar versão exata compatível com Python
3.13, lock e digest da imagem verificados; não atualizar o restante da stack.
O adaptador AMQP fica fora de domain e shared; contratos de negócio permanecem
independentes do cliente. Não introduzir framework genérico de eventos.

Topologia local: um broker, duas exchanges direct, duas filas duráveis, sem
exclusive/auto-delete. Filas classic são suficientes para este contrato de nó
único; não há alegação de alta disponibilidade. Nomes, bindings e versões são
declarados e validados na inicialização. Usar vhost próprio, credenciais distintas
e permissões mínimas por serviço. Management e AMQP não ficam expostos externamente;
segredos vêm de settings. TLS e implantação externa exigem decisão própria.

Parâmetros iniciais explícitos: um processo por worker, prefetch 8, lote de
publicação 20, polling local de 500 ms, lease de 30 s e confirm timeout de 5 s.
Polling, lotes e concorrência são parâmetros da implementação e influenciam a
latência e a capacidade do fluxo completo. Seus valores efetivos devem acompanhar
a identidade do runtime; uma diferença futura não será atribuível só ao RabbitMQ.
Encerramento gracioso: parar admissão de trabalho no worker e aguardar até 15 s;
fechar canais ao expirar. Trabalho incompleto permanece durável. Falha inesperada
de qualquer loop obrigatório encerra o processo com erro, sem worker parcialmente
ativo aparentando saúde. Heartbeat local e comando de healthcheck não exigem HTTP.

| Processo | CPU | Memória | Pool SQL / overflow |
| --- | --- | --- | --- |
| Core API | 0,5 | 384 MiB | 2 / 0 |
| Core worker | 0,5 | 384 MiB | 3 / 0 |
| Tracking API | 0,5 | 384 MiB | 2 / 0 |
| Tracking worker | 0,5 | 384 MiB | 3 / 0 |
| PostgreSQL | 2 | 2560 MiB | — |
| RabbitMQ | 0,5 | 512 MiB | — |

São limites iniciais de operação funcional, ainda não validados. Aplicação soma
2 CPUs/1536 MiB; broker acrescenta recursos. Não apresentar isso como orçamento
total equivalente à v1.1. Uma comparação futura deve decidir explicitamente o
orçamento de toda a topologia, sem multiplicá-lo por componente.

Compose v1.2 usa projeto e volumes novos. A identidade do nó RabbitMQ deve
permanecer estável ao recriar seu container com o mesmo volume; preservar apenas
o volume não autoriza mudar a identidade sob a qual os dados são localizados. Alembic mantém dois históricos de
migração, com heads independentes. Verificar banco limpo e upgrade de uma cópia
descartável representativa da v1.1; jamais migrar volume histórico para testar.
Registros antigos `RECEIVED` sem outbox não são publicados automaticamente: devem
ser inventariados como legado pendente. Uma migração não executa efeitos de negócio.
Dados terminais existentes continuam consultáveis; novos campos precisam tolerar
ausência de informação histórica sem inventar timestamps ou resultados.
Para inbox legado sem trabalho de transporte, `progress` fica nulo; não o mostrar
como enfileirado ou em recuperação automática.

## 8. Observabilidade e operação

Preservar a política atual de access logs e mensagens Uvicorn. Acrescentar logs
estruturados INFO para o novo fluxo de mensagens, em stdout, sem duplicação de
handlers: horário UTC, serviço, etapa, identidades técnicas, tentativa, duração,
resultado e categoria controlada de erro. Não registrar bodies, credenciais,
assinaturas, argv, ambiente ou traces brutos em respostas públicas.

Essa instrumentação é uma diferença declarada da v1.2. Não retroaplicá-la às
imagens v1.1 nem alegar paridade automática. Requisitos históricos mais amplos de
logging, Prometheus e OpenTelemetry continuam pendentes; não são removidos nem
declarados implementados por estes logs.

Diagnóstico local deve mostrar contagem/idade de trabalho pendente e bloqueado,
última atividade, publicação e conclusão por ID e motivos sanitizados. Distinguir
mensagens no RabbitMQ (prontas e sem ACK), pendências de cada outbox/inbox técnica
e eventos de negócio ainda não concluídos. ACK após persistência técnica permite
fila RabbitMQ vazia com trabalho pendente no PostgreSQL; prefetch limita entregas
sem ACK, não o acúmulo durável local.

Contagens técnicas são por etapa e não devem ser somadas como total de eventos.
Usar o inbox de negócio do Tracking como referência de eventos aceitos ainda não
terminais, incluindo bloqueados, e correlacionar etapas por identidade estável.
Conservar horários de aceitação, decisão e finalização disponíveis, estado e
categoria de falha; não inventar uma fotografia atômica entre bancos e broker.
Esses dados servem à operação e aos testes funcionais, sem novo runner de carga.
Saúde da API para admitir não equivale à saúde dos workers para concluir. Expor
essa distinção na operação, inclusive broker indisponível com admissão disponível.
Não prometer causa raiz quando os registros não a sustentarem.

## 9. UI e simulador

A UI mantém templates no servidor e os controles CSRF/CSP existentes. Mostrar
aceito/pendente, finalizado e rejeitado sem apresentar 202 como aplicação concluída.
Polling a cada segundo termina ao obter estado terminal, sair da página ou
esgotar os 30 segundos da observação local. Uma nova observação explícita apenas
consulta o mesmo evento; não reenvia admissão. Erros de consulta preservam a tela
e não alteram estado persistido. Prazo esgotado não significa rejeição ou perda. Atualizar timeline e resultado quando
disponíveis, sem regras de negócio em templates.

O simulador reconhece 202, consulta `Location` com prazo configurável e apresenta
separadamente aceitação e conclusão. Prazo esgotado significa resultado ainda não
observado, não rejeição nem perda. Preservar modo explícito de cliente síncrono
quando necessário para imagens antigas, sem modificar simuladores congelados.

## 10. Verificação e limites de conclusão

Testes acompanham cada incremento. PostgreSQL e RabbitMQ reais são obrigatórios
para persistência, ACK, confirms, reinício e concorrência. Mocks servem a falhas
focais, não substituem essas verificações.

Cobrir: ambos adapters/HMAC/bytes; duplicata pendente e terminal; conflito de hash;
rejeições antes/depois de 202; concorrência; ordering; conclusão de Order e uma
Notification por efeito; publicação confirmada/retornada/incerta; queda antes e
depois dos commits/ACK; resultado repetido; retomada após reinício; bloqueio e
rearme explícito; quarentena; lease vencida e dono antigo; ausência de SQL retido
durante HTTP/AMQP; indisponibilidade e retorno de dependências sem perda silenciosa.

Barreiras e falhas injetadas controlam fronteiras transacionais. Limites de espera
evitam testes pendurados. Verificações de reinício usam recursos descartáveis e
dados sintéticos. Preservar cobertura global mínima de 80% e cobertura completa
das regras críticas existentes; não enfraquecer testes para acomodar o novo fluxo.

O aceite funcional não depende da matriz de desempenho. Após o fluxo, recuperação,
UI e operação aprovados, congelar uma referência e **parar para decisão** sobre
encerramento, comparação futura ou evolução. Não iniciar carga extensa ou outra
extração automaticamente.

Uma comparação posterior deverá separar eventos oferecidos, aceitos e concluídos;
latência HTTP e ponta a ponta; backlog/idade; tempo e limite de drain; falhas e
recursos totais. Polling de conclusão também tem custo. Mesma quantidade de usuários
em carga fechada não garante mesma taxa oferecida quando a resposta muda para 202.
Aplicar as distinções de backlog da seção 8 e registrar os parâmetros da seção 7,
incluindo o polling local de 500 ms. Fila do broker vazia não encerra o drain:
a conclusão precisa considerar o trabalho durável e os resultados de negócio.
Não reutilizar o validador síncrono como se aceitação significasse conclusão.
Protocolo e identidades novos exigem aprovação prévia e baselines afetadas próprias;
resultados de campanhas diferentes continuam separados.

## 11. Referências técnicas

- [RabbitMQ: confirmações de publicação e acknowledgements](https://www.rabbitmq.com/docs/confirms).
- [RabbitMQ: limites de dead-lettering](https://www.rabbitmq.com/docs/dlx).
- [aio-pika](https://docs.aio-pika.com/).
- [HTTP assíncrono: aceitação e consulta de resultado](https://learn.microsoft.com/en-us/azure/architecture/patterns/asynchronous-request-reply).

Essas referências sustentam os mecanismos. Os limites e garantias do FulfillFlow
são os definidos neste contrato, não propriedades presumidas de uma biblioteca.
