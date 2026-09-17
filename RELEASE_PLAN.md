# FulfillFlow — Plano de entrega v1.3

## 1. Objetivo, base e autorização

Preparar a extração assíncrona de Notifications com banco próprio, consulta HTTP
interna e entrega simulada. O [DESIGN](DESIGN.md) define contratos; este documento
define sequência, critérios e estado. Prioridade: funcionamento e recuperação.

Base: `v1.2.0-rc.1`, SHA `9b445f9b5466cd302c89f1deed7a9c051cb397ae`.
Branch: `feature/v1.3-notifications-async`, criada diretamente desse SHA em checkout
isolado para preservar as alterações do checkout v1.2. A tag anotada foi conferida
pelo commit resolvido, sem mover referências existentes.

**Autorização atual: implementar I e II, sequencialmente, com revisão e testes
antes de avançar; parar antes do III.** O desenho está aprovado com esclarecimentos
de supressão de mensagens LEGACY e limites de independência dos publicadores.
Commits por assunto e push somente nesta branch estão autorizados, com conferência
da CI do SHA final. III/IV, campanhas, publicação e merge permanecem não autorizados.

| Marco | Estado |
| --- | --- |
| Inspeção da referência, criação/consultas/idempotência e mecanismos v1.2 | Concluída sobre o SHA congelado |
| Desenho e plano v1.3 | Aprovados; esclarecimentos incorporados |
| I — Contratos e persistência | Em implementação |
| II — Fluxo integrado | Não iniciado |
| III — Recuperação e operação | Não iniciado |
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
históricos. Runtime e ensaios funcionais de I/II usam somente recursos descartáveis próprios.

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
