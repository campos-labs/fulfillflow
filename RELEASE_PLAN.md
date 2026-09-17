# FulfillFlow — Plano de entrega v1.3

## 1. Objetivo, base e autorização

Entregar a extração assíncrona de Notifications com banco próprio, consulta HTTP
interna e entrega simulada. O [DESIGN](DESIGN.md) define contratos; este documento
define sequência, critérios e estado. Prioridade: funcionamento e recuperação.

Base: `v1.2.0-rc.1`, SHA `9b445f9b5466cd302c89f1deed7a9c051cb397ae`.
Branch: `feature/v1.3-notifications-async`, criada diretamente desse SHA em checkout
isolado para preservar as alterações do checkout v1.2. A tag anotada foi conferida
pelo commit resolvido, sem mover referências existentes.

**Autorização atual: implementar III a partir de
`bb002dc84321856ca9cd57b8bb8374683010da74`; parar antes do IV.** O desenho está aprovado com esclarecimentos
de supressão de mensagens LEGACY e limites de independência dos publicadores.
Commits por assunto e push somente nesta branch estão autorizados, com conferência
da CI do SHA final. IV, campanhas, publicação e merge permanecem não autorizados.

| Marco | Estado |
| --- | --- |
| Inspeção da referência, criação/consultas/idempotência e mecanismos v1.2 | Concluída sobre o SHA congelado |
| Desenho e plano v1.3 | Aprovados; esclarecimentos incorporados |
| I — Contratos e persistência | Concluído e revisado localmente; evidências abaixo |
| II — Fluxo integrado | Concluído; CI do SHA bb002dc aprovada |
| III — Recuperação e operação | Implementado e verificado localmente; CI do SHA enviado exigida |
| IV — UI e aceite funcional | Não iniciado |
| Comparação extensa / publicação | Suspensas; requerem decisão posterior |

O [plano congelado v1.2](https://github.com/campos-labs/fulfillflow/blob/9b445f9b5466cd302c89f1deed7a9c051cb397ae/RELEASE_PLAN.md)
preserva os aceites I–IV, CI, imagem local, lock, heads e proveniência da pré-release;
o [README congelado](https://github.com/campos-labs/fulfillflow/blob/9b445f9b5466cd302c89f1deed7a9c051cb397ae/README.md)
e o [DEMO congelado](https://github.com/campos-labs/fulfillflow/blob/9b445f9b5466cd302c89f1deed7a9c051cb397ae/docs/DEMO.md)
preservam o comportamento demonstrado. Não reinterpretar esse aceite como benchmark.
As referências v1.0/v1.1/v1.2, imagens e evidências permanecem intocadas. A comparação
anterior continua suspensa/incompleta, conforme [V11_REVIEW](benchmarks/V11_REVIEW.md);
não reunir campanhas ou concluir causa do 503 histórico.

## 2. Método e limites de execução

Trabalhar por incremento explicitamente autorizado, com testes desde a primeira
mudança. Conferir DESIGN, código e estado do checkout; preservar alterações alheias.
Escolhas privadas já cobertas pelo contrato são resolvidas autonomamente. Dividir
cada incremento em mudanças coerentes, sem implementar toda a extração de uma vez.

Não atualizar ferramentas/dependências alheias. A stack já possui tudo que este
alvo requer; preservar `uv.lock` e digests existentes. Extensões técnicas devem ser
estreitas, com regressão de Tracking, sem framework genérico ou serviços fictícios.
Recursos de verificação têm projeto, nomes e volumes próprios e ownership conferido.
Não executar carga, publicar imagens, criar tags, fazer merge ou alterar dados
históricos. Runtime e ensaios funcionais de I–III usam somente recursos descartáveis próprios.

## 3. Incremento I — Contratos e persistência

**Resultado:** contrato do fato e persistência proprietária testados, mantendo o
fluxo executável v1.2 até a ativação coordenada do II.

1. Introduzir DTO do evento e consultas definidos no DESIGN §§4/7, serialização,
   validação de IDs/timestamps/hash, limite e isolamento dos três tipos de mensagem.
   Testar compatibilidade dos envelopes Tracking, payload mínimo, status permitidos,
   snapshot do destinatário e rejeição de divergência.
2. Criar metadata/histórico Alembic/banco/role Notifications e persistência de
   Notification, inbox técnica, quarentena e auditoria, sem outbox fictícia.
   Manter os campos terminais e unique por efeito; referências externas sem FK.
   Ajustar checks/índices da outbox Core para o novo tipo, sem habilitar produção.
3. Preparar classificação explícita de recibos legados e ensaio offline de cópia de
   terminais conforme DESIGN §9. Testar importação idempotente, divergência, preservação
   exata de conteúdo/IDs/timestamps e bloqueio do corte incompleto. Não fabricar
   envelopes ou reaplicar efeitos antigos; não retirar ainda o escritor v1.2 ativo.
   Cobrir mensagem nova para efeito LEGACY: preservar registro, persistir quarentena
   LEGACY_EVENT_SUPPRESSED e DONE juntos, sem nova simulação/hash histórico.
4. Estender fixtures/Compose de teste/CI para três bancos e o novo fluxo RabbitMQ,
   com topologia pré-declarada e ACLs restritas. Exercitar transporte real do novo
   envelope, inbox antes de ACK, constraints concorrentes e retomada após ACK.
   Reutilizar primitivas existentes somente com composição estática explícita.
5. Testar upgrade limpo e cópia descartável representativa da v1.2, heads/drift,
   segregação de roles e capacidade de reconstruir schemas históricos. Preservar
   migrations antigas, referências de seeds e caminho funcional ainda ativo.

**Aceite:** unitários e PostgreSQL/RabbitMQ reais aprovados para os contratos
introduzidos; rollback e unicidades verificados por sessões independentes;
schemas novos isolados e fluxo v1.2 ainda funcional. Não declarar extração ativa
por existirem tabelas ou fila. O legado não pode ser classificado apenas pela
ausência de outbox, pois isso esconderia corrupção de recibo novo.

### Verificação do I — 2026-09-17

- Contratos/DTOs, inbox sem outbox fictícia, processor SQL e importação proprietária
  implementados sem ativar novos efeitos no Core. Heads `1301_core` e
  `1301_notifications`; Tracking permanece `1203_tracking`.
- PostgreSQL/RabbitMQ reais no projeto exclusivo `fulfillflow-v13-ii-tests`:
  transporte, filtro antes do lote, ACK/commit, duplicata/conflito, ACLs, roles,
  migrations/drift, concorrência, rollback, LEGACY/quarentena e importação aprovados.
  O ensaio rebaixou somente o banco descartável ao head `1202_core`, importou os
  terminais sem simulação e voltou ao head novo, preservando os registros.
- Regressão unitária/arquitetura/API/transporte: 918 aprovados. Dez testes Windows
  inicialmente omitidos por caminho do PowerShell foram executados com o caminho
  real configurado e aprovados. Testes novos de contratos/persistência passaram
  em rodadas focais; nenhum cenário crítico foi aceito por skip.
- Ruff, formatação, Mypy e dez contratos Import Linter aprovados. Revisão encontrou
  e corrigiu a ordem de limpeza da fixture conforme a FK local inbox→Notification.
  Dependências, referências históricas e runtime v1.2 preservados nesta etapa.
- O primeiro boot do broker descartável revelou cookie criado como root; corrigido
  somente nesse volume e prevenido no healthcheck novo, executado como rabbitmq.
  Nada foi alterado nos volumes históricos. Gate I encerrado antes de ativar II.

## 4. Incremento II — Fluxo integrado

**Resultado:** APPLIED gera evento durável no Core e uma simulação eventual em
Notifications; Core/Tracking concluem independentemente da disponibilidade do novo serviço.

1. Substituir a criação local de Notification pela outbox do fato na mesma transação
   do recibo/Shipment/Order/inbox e outbox de resultado Tracking. Remover todos os
   caminhos de simulação local, sem dual-write ou fallback. Testar rollback antes
   de cada fronteira, replay do recibo, todos os resultados sem notificação e
   destinatário congelado.
2. Acrescentar no Core worker publisher por tipo, com claim/lote/canal/backoff
   independentes. Ativar consumidor/processador Notifications com persistência
   antes do ACK e transação Notification + DONE, simulação determinística e falha
   esperada terminal distinta de infraestrutura. Nenhuma chamada remota no processor.
   Testar isolamento de backlog/falhas controladas Notifications; preservar
   encerramento por falha inesperada de loop obrigatório e limites comuns de
   processo, banco, broker e recursos.
3. Criar Notifications API interna, cliente Core e projeção de progresso por efeito.
   Trocar REST/HTML/dashboard para leitura HTTP na mesma ativação que remove a leitura
   local. Preservar schemas, filtros/paginação/ordem, auth e request ID; 503 controlado
   e dashboard parcial em indisponibilidade. Fechar sessões antes de HTTP.
4. Ativar entrypoints, configurações, migrações por proprietário e Compose v1.3 em
   volumes novos. Ajustar Dockerfile, schema gates, fronteiras Import Linter e seeds
   funcionais; arquivar tabela antiga apenas no corte coordenado. Atualizar README
   com comandos executáveis e versão de desenvolvimento somente quando este runtime existir.
5. Verificar ponta a ponta Alpha/Beta: 202, conclusão Tracking, Order FULFILLED e
   quatro simulações esperadas; duplicatas pendentes/terminais não geram quinto efeito.
   Validar chegada invertida de fatos, falha esperada FAILED e ausência de efeitos
   para criação/cancelamento manual, rejeição, stale, inválido e NO_STATE_CHANGE.

**Aceite:** fluxo real completo com PostgreSQL/RabbitMQ; publicação e leitura
desacopladas de Notifications; UI mínima já honesta sobre defasagem e erro.
Demonstrar resultado Tracking com worker Notifications parado e posterior
simulação sem reenvio. Não manter asserts síncronos que confundam conclusão
Tracking com simulação; substituí-los por verificação eventual delimitada.
Testes de atomicidade Core agora exigem outbox do fato, não Notification local.

### Verificação do II — 2026-09-17

- Escritor local substituído pelas duas outboxes atômicas. Registro antigo permanece
  como metadata de arquivo, sem leitor/escritor no runtime. Evento independente
  somente para APPLIED, destinatário congelado e replay sem novo efeito.
- Core tem dois publicadores com claims/canais/backoff separados; Notifications
  consome e processa sem outbox. Três cenários reais verificaram publisher
  Notifications aguardando/falhando enquanto Tracking publica, recepção e retomada
  de inbox com fila vazia. Falha inesperada de loop obrigatório continua fatal.
- API interna Notifications e cliente autenticado Core ativos; listagem/detalhe,
  contagens, progresso por efeito e UI mínima usam HTTP. Casos de indisponibilidade,
  pool SQL liberado, BLOCKED, FAILED, LEGACY e outbox ausente foram verificados.
- Gates focais: 56 testes de Core/Tracking/persistência, 19 de HTTP/UI/E2E e
  29 de workers/configuração aprovados. E2E usa seis processos reais e os dois
  adapters; cada jornada termina com quatro simulações únicas. O cenário de
  Notifications parado conclui Shipment/Order e retoma a simulação sem novo webhook.
- Ruff, formatação, Mypy (158 arquivos) e 12 contratos Import Linter aprovados.
  Ensaio estrutural histórico: cinco casos aprovados em container próprio.
- Build local de três imagens v1.3 e smoke do projeto exclusivo
  `fulfillflow-v13-ii-runtime` aprovados: APIs/workers saudáveis, processos não-root,
  preparação HTTP repetida sem duplicar registros e três heads Alembic sem drift.
  Recursos descartáveis do smoke foram removidos; imagens não foram publicadas.
- `uv.lock` alterado somente na versão do próprio pacote para `1.3.0.dev0`, por
  `uv lock`; nenhuma dependência externa ou digest atualizado. SHA-256 do arquivo:
  `cb0ffc810f44df83bb644d406895478030b9a572301d10084880c1220d8431fc`.
- Suíte ampla com cobertura: 1176 aprovados, zero skips/erros de execução e 88,54%
  de cobertura. A única falha foi a expectativa antiga `v1.2.0.dev0` no teste de
  leitura da versão; corrigida para `v1.3.0.dev0` e aprovada em execução focal.
  A rodada já havia carregado o teste anterior; não se repetiram os 1176 aprovados.
  XML conferido: todos os cenários críticos passaram sem skips. Junto dos cinco
  casos estruturais separados, os 1182 casos foram verificados.
- CI anterior aprovada no SHA `bb002dc84321856ca9cd57b8bb8374683010da74`:
  [execução 35174709946](https://github.com/campos-labs/fulfillflow/actions/runs/35174709946).
  1142 testes funcionais e cinco estruturais aprovados; cobertura 88,56%.
  Os 35 skips eram específicos de Windows e passaram localmente. O gate crítico
  PostgreSQL/RabbitMQ não aceitou skips; migrations, checks e build/smoke aprovados.

**Limite do aceite II:** recuperação operacional completa e UI/DEMO finais não
foram aceitas nessa etapa. III está agora autorizado; IV continua pendente.
Não há exactly-once externo nem isolamento de falhas comuns dos recursos compartilhados.

## 5. Incremento III — Recuperação e operação

**Resultado:** fronteiras de falha recuperáveis ou bloqueadas explicitamente,
com diagnóstico e procedimentos finitos.

1. Exercitar interrupção antes/depois de commit Core, publish/confirm, persistência
   técnica/ACK e simulação/DONE. Reiniciar processos reais com recursos próprios,
   inclusive após ACK com fila vazia; obter um único registro por efeito.
2. Validar cinco tentativas/geração, intervalos pelo Clock, dependência global sem
   consumo em massa, quarentena, conflito, leases expiradas/dono antigo e concorrência.
   Completar CLI de diagnóstico/rearme Notifications e filtro do novo fluxo Core:
   banco/ID/hash/motivo, auditoria atômica, preservação de DONE/FAILED e conteúdo.
3. Com backlog Notifications mais antigo, provocar retorno/timeout/nack específico
   de seu publisher e comprovar que tracking.result continua chegando. Parar
   separadamente API, worker e acesso ao banco Notifications; verificar Core/Tracking,
   503 de consulta e retomada. Queda global do broker segue a recuperação v1.2.
4. Completar lifecycle, sinais e healthchecks por loops reais, sem exigir publisher
   inexistente. Conferir readiness por banco/head, inicialização independente,
   encerramento até 15 s, falha de loop obrigatório e dependências intermitentes.
5. Registrar logs/diagnóstico sanitizados, backlog/idade por etapa, recursos,
   identidade estável de broker/volume e parâmetros efetivos. Não apresentar
   fila vazia, heartbeat ou SENT como entrega concluída. Testar redaction e ACLs.
6. Executar o corte offline completo na cópia descartável, verificar todos os
   registros antigos consultáveis pelo novo dono, replay sem nova simulação,
   inventário sem pendência e ausência de escritores/leitores ativos antigos.

**Aceite:** testes reais de recuperação e smoke aprovados, sem perda silenciosa,
dual-write ou bloqueio de Tracking causado pelo novo fluxo. Itens que atingem
BLOCKED exigem rearme auditado; não prometer recuperação automática ilimitada,
isolamento físico, estabilidade prolongada ou solução do 503 histórico.

### Cobertura reutilizada e complementos do III

| Contrato | Evidência I/II preservada | Complemento III |
| --- | --- | --- |
| Atomicidade Core e snapshot | `test_notification_core_flow`, `test_commit_boundaries` | Morte de processo antes/depois do commit e confirmação |
| Inbox antes de ACK, conflito e quarentena | `test_notification_transport`, `test_message_transport` | Interrupção real da recepção e retomada com fila vazia |
| Simulação + DONE; LEGACY; unicidade concorrente | `test_notifications_owned` | Interrupção real e guarda de terminais no rearme |
| Retry, leases, lock e auditoria técnica | `test_message_operations`, `test_message_transport` | Aplicação à inbox Notifications, CLI e filtro Core |
| Independência dos publishers, health e shutdown | `test_notifications_worker`, `test_worker_lifecycle`, `test_worker_dependencies` | Falhas específicas e lifecycle dos processos Notifications |
| Importação offline e leitura HTTP | `test_notification_cutover`, `test_notifications_http` | Comandos reais, inventário e consulta após corte/replay |

Reutilizar essas provas sem duplicar os cenários. Rodar regressão dos mecanismos
compartilhados quando alterados e os complementos com infraestrutura descartável.

### Entrega e validação do III

CLI Notifications concluída com diagnóstico sanitizado, guarda de proprietário,
ID/hash/motivo e geração, cinco tentativas e rearme auditado sob o lock da inbox.
Nenhum terminal SIMULATED/FAILED/LEGACY permite nova simulação. Core ganhou filtro
explícito do fato; heartbeat expõe somente estados controlados dos loops. Não houve
mudança de evento, schema, dependência, ACL ou contrato de negócio Tracking.

- `test_notification_recovery`: 11 cenários aprovados com interrupção abrupta de
  processos nas fronteiras Core/confirm/inbox/ACK/simulação/DONE, reinício, lease
  expirada, fila vazia após ACK, falha fatal e encerramento limitado. No Windows,
  o teste de shutdown entrega SIGTERM pelo próprio processo (watcher exclusivo de
  teste); Linux recebe SIGTERM externo. Cortes usam harness de teste, sem switches
  de falha no runtime. Heartbeat após morte abrupta respeita sua janela de 5 s.
- `test_notification_operations`: seis casos aprovados, incluindo concorrência de
  dois CLIs reais, auditoria/rollback, limite de tentativas, filtro Core e terminais.
  A regressão focal dos mecanismos compartilhados, ACLs, HTTP e atomicidade executou
  93 casos aprovados, sem skips, incluindo esses seis e recuperação Tracking.
- `test_notification_outages`, `test_notification_cutover_cli` e a extensão de
  `test_notifications_worker`: nove casos aprovados, sem skips. API, worker e acesso
  ao banco Notifications interrompidos separadamente; Order/Tracking concluem e a
  retomada dispensa novo webhook. Corte completo via CLIs, reimportação, consulta
  HTTP real e replay LEGACY mantêm arquivo/conteúdo/IDs/datas sem ressimulação.
  Retorno obrigatório é produzido por RabbitMQ real; timeout e nack são injeções
  controladas específicas do publisher, com tracking.result entregue pelo broker real.
- `test_worker_lifecycle`: 13 casos unitários aprovados, incluindo observação local
  sanitizada que distingue dependência, staleness e encerramento. Ao todo, 126 casos
  distintos verificados localmente nessas rodadas focais; nenhum skip crítico.
  As verificações Windows/PowerShell históricas não alteradas mantêm a evidência
  de I/II; os novos CLIs/processos foram executados no Windows com Python 3.13.1.
- Ruff, formatação, Mypy (116 arquivos), Import Linter (12 contratos) e diff check
  aprovados. Jobs de migration do smoke aplicaram os três heads; `current
  --check-heads` e `check` aprovados para `1301_core`, `1203_tracking` e
  `1301_notifications`, sem drift ou migration nova.
- Compose config/build/up --wait aprovados no projeto exclusivo
  `fulfillflow-v13-iii-runtime`; três APIs/três workers saudáveis. Live/ready,
  dashboard e consulta Notifications retornaram 200; CLIs reais nos containers
  mostraram os loops esperados e somente bancos proprietários. SIGTERM também
  verificado nos workers Docker. Recursos/pools/leases/prefetch permanecem os do
  DESIGN; broker com hostname `broker` e volumes novos próprios. Sem carga.
- Imagens locais novas, preservando as anteriores: `fulfillflow-core:v13-iii-local`
  `sha256:0c2b7819c8349c03274d74584fea29767826d9013901e807f1523b1b2fa7031a`;
  `fulfillflow-tracking:v13-iii-local`
  `sha256:26cca5747c6012c8eb5a169b2662c85d9c06288636a9144ef49316428c813a07`;
  `fulfillflow-notifications:v13-iii-local`
  `sha256:db4060ae8e92059479d2596d23c05bab4652437b21f3d5acca7dd0604b56c35f`.
  Digests de infraestrutura e hash do lock seguem os registrados no II.
- CI mantém suíte completa/cobertura, checks, migrations e build/smoke. O gate sem
  skips críticos passou a exigir os quatro novos módulos de recuperação/operação
  e cinco casos de worker Notifications. Conferir a execução do SHA final após
  push nesta branch; link/resultado exatos no relatório da tarefa, sem equiparar
  validação local à aprovação remota.

**Limite da entrega III:** parar antes do IV. Recomenda-se executar UI/polling e
DEMO com observação conservadora da indisponibilidade, utilizando esses contratos
já verificados. Não há migração online, isolamento de falhas comuns, exactly-once
externo, estabilidade prolongada ou solução demonstrada para o 503 histórico.
A pausa final permanece após IV, antes de comparações extensas ou publicação.

## 6. Incremento IV — UI e aceite funcional

**Resultado:** jornada utilizável e evidência funcional revisável, sem publicação.

1. Refinar UI para separar conclusão Tracking/Order de publicação, pendência,
   bloqueio e SIMULATED/FAILED. Polling limitado, falha de consulta conservando a
   última observação, prazo sem rejeição inventada e nova observação sem reenvio.
   Conferir dashboard parcial, filtros, detalhe e legado sem progresso fabricado.
2. Atualizar `docs/DEMO.md` com demonstração curta: Notifications parado enquanto
   Order conclui, retomada sem webhook novo, duplicata sem efeito adicional e consulta
   indisponível sem perda de estado. Manter contrato do simulador de Carriers;
   observar Notifications separadamente. Verificar em navegador real.
3. Produzir capturas/evidências novas identificadas por versão, sem sobrescrever
   as da v1.2. Sincronizar README apenas com comportamento efetivamente executável.
4. Executar suíte integrada completa/cobertura, checks estáticos/arquiteturais,
   Alembic e Docker build/smoke pertinentes. Conferir CI no SHA exato quando houver
   autorização de commit/push; preparação local não equivale a CI aprovada.
5. Entregar matriz de contratos conferidos, limitações, SHA/revisão, hash do lock,
   heads, identidade da imagem local, digests de infraestrutura, recursos e parâmetros.
   Registrar separadamente estado implementado, verificado e publicado.

**Aceite funcional:** todos os contratos do DESIGN demonstrados, gates preservados,
sem comportamento stubado, falha conhecida em check obrigatório ou documentação
divergente. Uma referência local revisável é suficiente para parar; criar tag,
publicar imagem/pré-release ou comparar versões não faz parte deste aceite.

## 7. Validação e controle de mudanças

Testes focais primeiro e validação ampla conforme impacto. Comandos existentes:
`uv sync --frozen`, `uv run pytest`, cobertura com `--cov=fulfillflow`,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` e
`uv run lint-imports`. Usar Alembic upgrade/current/check em cada configuração
proprietária afetada; registrar comandos Notifications somente quando implementados.
Runtime requer `docker compose config`, build e smoke de APIs/workers afetados.

PostgreSQL e RabbitMQ reais são obrigatórios nos caminhos pertinentes; CI não pode
substituí-los por SQLite/mocks ou skips silenciosos. Manter cobertura global mínima
de 80% e cobertura completa das regras críticas. Testes Windows/PowerShell pelo
executável real quando esses fluxos mudarem; CI Linux não os substitui.

Na preparação documental, validar diff, links locais/referências congeladas,
coerência entre os quatro documentos, ausência de mudança executável e preservação
do checkout original. Não iniciar testes de runtime, migrations ou containers por
uma alteração exclusivamente documental. Não criar documento extra de handoff.

## 8. Pausa obrigatória após o aceite funcional

**Ao concluir IV, parar antes de comparação extensa ou publicação.** Entregar
resultado e limitações para decisão posterior. Não executar campanha, criar tag,
publicar imagem/release, fazer merge, introduzir provedor real ou iniciar nova
extração automaticamente. Se a autorização abranger somente um incremento,
a parada ocorre no limite autorizado, sem esperar IV.

Comparação futura exige protocolo aprovado, identidades e orçamento total próprios,
com eventos oferecidos/aceitos, Tracking concluído, Notifications simuladas,
backlog/drain, latências, falhas e diferenças de observabilidade. Não alterar imagens
congeladas para paridade nem reutilizar 202 como trabalho concluído. Provedores reais,
cloud, Kubernetes, GitOps e autoscaling permanecem fora desta implementação.
