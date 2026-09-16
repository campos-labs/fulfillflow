# FulfillFlow — Plano de entrega v1.2

## 1. Objetivo e estado

Entregar Tracking com admissão durável e coordenação de comandos/resultados por
RabbitMQ, mantendo Notifications no Core. Contratos pertencem ao
[DESIGN.md](DESIGN.md); este documento define sequência, aceite e estado.

Base: `v1.1.0-rc.1`, SHA `217e29a230689da3bd6359790f0753b41a10a927`.
Branch: `feature/v1.2-tracking-async`. A v1.0 publicada e a pré-release v1.1
permanecem congeladas. Nenhum merge para main é necessário para iniciar esta linha.

| Marco | Estado |
| --- | --- |
| Arquitetura alvo e plano | Conferidos contra o código em 9d1d468; comando/recibo preservados |
| I — Contratos e transporte durável | Concluído: PostgreSQL/RabbitMQ reais, revisão e CI aprovada no SHA `5a50f97` |
| II — Fluxo assíncrono completo | Concluído: outboxes atômicas, 202/consulta, workers, clientes e recuperação local testados |
| III — Recuperação e operação | Concluído: rearme/diagnóstico, políticas, lifecycle/saúde e recuperação verificados |
| IV — UI, verificação integrada e congelamento funcional | Não iniciado |
| Comparação extensa | Adiada, sem execução autorizada |
| Tag/pré-release/release v1.2 | Não criada; depende de decisão após aceite funcional |

O checkout ativa o fluxo assíncrono da v1.2; a referência síncrona v1.1 permanece
congelada. A comparação anterior continua suspensa/incompleta. Resultados e
ressalvas estão em
[V11_REVIEW.md](benchmarks/V11_REVIEW.md) e seu histórico vinculado. Nada neste plano
autoriza retomar campanhas, reinterpretar o 503 ou reunir repetições de campanhas
diferentes. Integridade/WAL históricos e cópia independente não confirmados
permanecem pendências; não usar esses volumes no desenvolvimento.

## 2. Método de execução

Trabalhar por incremento autorizado, com testes e revisão do diff antes de avançar.
Não produzir outra rodada de planejamento para escolhas internas já cobertas pelo
DESIGN. Resolver autonomamente caminhos, erros de scripts, nomes privados e testes
pertinentes. Parar apenas a parte dependente de conflito real de contrato, mudança
de escopo, perda de evidência ou requisito externo indisponível.

No início, conferir branch/base, alterações existentes e os trechos de código
afetados. A arquitetura alvo substitui explicitamente a semântica do webhook e a
coordenação HTTP; outras regras da referência permanecem. Apontar incompatibilidade
concreta antes de implementar, sem legitimar divergência editando DESIGN depois.

Não atualizar ferramentas instaladas ou dependências alheias. Fixar somente as
novas dependências autorizadas pelo resolver e registrar a imagem RabbitMQ por
versão/digest. Usar recursos de teste isolados, nomes/volumes próprios e ownership
verificado. Não parar processos históricos, restaurar bases preservadas ou executar
Locust. Testes funcionais e demonstração curta são distintos de campanha de carga.

## 3. Incremento I — Contratos e transporte durável

**Resultado:** infraestrutura de mensagens verificável nos dois sentidos, sem
alterar prematuramente a resposta pública da aplicação.

1. Conferir os contratos existentes de comando, recibo e resultado contra DESIGN
   §§3–6. Definir envelopes, serialização canônica, limites, identidade lógica e
   rejeição de divergências, com testes desde a primeira mudança.
2. Acrescentar migrations locais para outbox, inbox técnica e quarentena, constraints
   e índices de busca de pendências. Testar upgrade limpo e a partir de cópia
   descartável representativa da v1.1, preservando registros existentes.
3. Implementar claims/leases, publicação persistente com confirms/mandatory,
   recepção durável antes de ACK e processamento local retomável. Compartilhar
   apenas primitivas técnicas necessárias; não criar framework de mensageria.
4. Fixar aio-pika e RabbitMQ, acrescentar Compose isolado de testes e configurar
   RabbitMQ real na CI afetada. Não substituir verificações por skips silenciosos.
5. Testar publicação nos dois sentidos, rejeição de roteamento, duplicatas,
   confirmação perdida, commit/ACK incerto, lease expirada e constraints concorrentes.

**Aceite:** testes unitários e PostgreSQL/RabbitMQ reais aprovados; identidade e
recuperação de transporte demonstradas; fluxo síncrono existente ainda verificável.
Não declarar a v1.2 operacional por haver somente filas e tabelas.
Demonstrar retomada local após reinício com uma mensagem já confirmada ao broker
e ainda não aplicada. Persistir e dar ACK sem recuperar esse trabalho não atende
ao aceite, mesmo com a fila RabbitMQ vazia.

## 4. Incremento II — Fluxo assíncrono completo

**Resultado:** evento novo recebe 202 após admissão durável e chega a resultado
terminal por comandos e resultados AMQP, sem reentrega obrigatória do webhook.
Esse caminho cobre trabalho retomável; bloqueio por esgotamento ou conflito exige
o rearme explícito previsto, cuja operação completa é verificada no incremento III.

1. Integrar recepção/normalização/outbox atômicas de Tracking e idempotência
   pendente/terminal. Implementar schema 202, Location e consulta de conclusão.
2. Reutilizar a aplicação local idempotente do Core, adicionando a outbox de
   resultado na mesma transação. Integrar o consumidor de resultados e a
   finalização local do Tracking com as validações de identidade/hash.
3. Criar entrypoints dos dois workers e ativar o Compose v1.2 com projeto, volumes,
   credenciais, limites e pools do DESIGN. Remover do runtime v1.2 o endpoint e o
   cliente de apply HTTP que o AMQP substitui; preservar as consultas HTTP previstas.
4. Adaptar o caminho mínimo de UI/simulador para compreender 202, sem apresentar
   pendência como sucesso concluído. Sincronizar README e versão de desenvolvimento
   do pacote quando o runtime alvo se tornar executável; não publicar imagens/tags.
5. Testar ponta a ponta ambos adapters, conflito de bytes, duplicatas antes/depois
   do resultado, rejeições, timeline, Shipment, Notification e conclusão do Order.

**Aceite:** fluxo completo em PostgreSQL/RabbitMQ reais; exatamente os efeitos
permitidos pelo domínio; nenhuma transação SQL durante HTTP/AMQP; Import Linter
coerente com a nova fronteira. Os testes de recuperação HTTP da v1.1 ficam na
referência congelada; ao substituir caminhos no checkout v1.2, manter testes
equivalentes das garantias e preservar cobertura dos caminhos HTTP remanescentes.

### Evidências do aceite I/II — 2026-09-16

- Implementação do II em `840cd91852a1c23db8c97af67da5b81a14d5f3cf`, seguida de
  adaptação dos testes de consulta de Notifications e ações de Order ao 202.
  DESIGN não foi alterado; Notifications permanece no Core.
- Validação local: suíte unitária, nove casos novos de polling, controles com
  PowerShell 7.6.5 e 186 casos de API/integração/UI/E2E/arquitetura. As quatro
  asserções síncronas identificadas na passagem ampla foram corrigidas e
  reexecutadas isoladamente. Cobertura integrada local: **86,80%**, gate de 80%.
- Ruff, formatação, Mypy e os dez contratos de Import Linter aprovados. Alembic
  confirmou `1201_core`/`1202_tracking`, sem drift; upgrade de registros v1.1
  preservou conteúdo e deixou campos novos nulos, sem criar trabalho legado.
- Testes reais demonstraram rollback do fato e da outbox juntos, interrupção
  antes/depois dos commits, idempotência, locks, resultado divergente bloqueado
  e retomada por processos novos após ACK com fila vazia, nos dois serviços.
- Build e smoke no projeto isolado `fulfillflow-f05e-v12-runtime`: Alpha/Beta,
  202 durante interrupção do broker, conclusão após seu retorno sem reenvio e
  Order `FULFILLED`. Processos encerrados; imagens, volumes e dados preservados.
- A CI executa suíte completa/cobertura sem duplicar suítes e exige ausência de
  skips nos testes críticos de transporte e recuperação. Conferir o resultado
  pelo **SHA exato da entrega**, não apenas pelo nome da branch.

Aceite I/II: CI aprovada em `f6bc1149e365650fce8eb6be3443d39ccc366852`,
com 1.011 testes e cobertura de 88,30%; skips exclusivos de Windows,
verificados localmente.

Limite do aceite I/II: **antes do III**. Rearme auditável de `BLOCKED`, lifecycle
completo, healthcheck e diagnóstico operacional dos workers ficaram para o III; refinamento da UI e congelamento funcional permanecem no IV. Não houve
campanha, merge, tag/release, alteração de evidência congelada nem atualização de
dependências existentes. A dependência nova é aio-pika com suas transitivas.

## 5. Incremento III — Recuperação e operação

**Resultado:** falhas nas novas fronteiras têm estado durável, diagnóstico e
procedimento finito de recuperação demonstrável.

1. Completar políticas de retry por item e pausa de dependência, bloqueio,
   quarentena, rearme explícito auditável e diagnóstico por ID. Não acrescentar
   retries ilimitados ou reposição automática de testes encerrados.
2. Implementar lifecycle dos workers, encerramento limitado, healthcheck e
   sinalização de falha de loop obrigatório. Diferenciar saúde de admissão e
   conclusão, sem adicionar portas HTTP aos workers.
3. Acrescentar logs controlados do fluxo novo e comandos de inspeção local conforme
   DESIGN §8. Preservar access logs atuais e registrar a diferença de observabilidade.
4. Exercitar crash/restart antes/depois dos commits e ACK, indisponibilidade e
   retorno de broker/bancos, conflito de conteúdo e resultado duplicado. Verificar
   recuperação de trabalho aceito e ausência de efeitos duplicados.
5. Testar concorrência real com múltiplos consumidores/sessões, resultado fora de
   ordem, lease vencida com proprietário antigo e esgotamento/rearme de tentativas.

**Aceite:** falhas injetadas produzem resultado esperado, pendência recuperável ou
bloqueio explícito; evidências identificam fronteira e causa controlada. Não afirmar
estabilidade prolongada ou solução do 503 histórico a partir desses testes.

### Implementação do III

- CLI proprietária de diagnóstico e rearme de inbox/outbox `BLOCKED`, com ID,
  hash conferido contra os bytes, banco esperado e motivo explícito. Auditoria
  e nova geração são atômicas; identidade/payload e conclusões anteriores são
  preservados. Quarentena não é reenfileirada por essa operação.
- Migrations `1202_core`/`1203_tracking`: auditoria e última tentativa, sem
  fabricar atividade histórica. Contagens por etapa e backlog do inbox de
  negócio permanecem distintos de mensagens prontas/sem ACK do broker.
- Retry por item inclui falhas SQL transitórias; indisponibilidade de dependência
  pausa o componente. Loops obrigatórios, encerramento até 15 s, heartbeat local,
  healthcheck leve e logs JSON controlados completam a operação prevista.
- DESIGN explicita a identidade estável do nó RabbitMQ ao reutilizar volume,
  uma condição operacional da durabilidade existente. HTTP, ambos os outboxes
  atômicos e Notifications permanecem
  com os contratos existentes. Prometheus/OTel mais amplos, estabilidade prolongada
  e campanhas continuam fora deste aceite.

### Verificação do III — 2026-09-16

- Validação focal PostgreSQL/RabbitMQ e validação funcional Windows: 219 casos
  cobertos entre a passagem ampla e as correções focais; cobertura local **88,13%**.
  Inclui oito cenários de queda/retorno no startup e com worker já ativo, CLI real,
  deadlock, rearme concorrente, lease antiga e resultados fora de ordem.
  Regressão focal de 20 casos confirma que a duração da tentativa não consome
  o intervalo de retry e que a conclusão recebe o instante após a aplicação.
- Cinco testes estruturais legados aprovados. Após interrupção nativa do dump
  de diagnóstico Python no Windows, essas verificações foram executadas em
  processo separado, sem atualizar ferramentas ou enfraquecer asserções.
- As expectativas de heads foram atualizadas para `1202_core`/`1203_tracking`.
  Um 503 pontual de UI durante lentidão local passou em reexecução isolada, sem
  mudar contrato ou timeout; isso não resolve nem reinterpreta o 503 histórico.
- Ruff/formatação, Mypy, dez contratos de importação, Alembic heads/drift e build
  aprovados. Smoke sequencial confirmou APIs e workers saudáveis, diagnóstico,
  SIGTERM com código zero e mensagem persistente após recriação do broker com
  identidade/volume preservados. Recursos isolados parados, dados preservados.
- CI exige todos os cenários críticos sem skips; o resultado deve ser conferido
  no SHA exato publicado. Nenhum benchmark, merge, tag/release ou atualização de
  dependências foi realizado.

Parada do aceite III: **antes do IV**. Próximo passo: refinamento da UI/simulador,
demonstração e congelamento funcional conforme a seção seguinte. Observabilidade
mais ampla e avaliação de estabilidade/capacidade continuam pendentes.

## 6. Incremento IV — Verificação integrada e referência funcional

**Resultado:** fluxo utilizável e referência rastreável, com limitações explícitas.

1. Finalizar UI/HTMX e simulador: pendência, polling limitado pela jornada, rejeição,
   timeout de observação e resultado. Verificar CSRF, CSP e assets locais.
2. Atualizar DEMO e conferir visualmente uma jornada curta em ambiente isolado:
   criar dados, enviar evento, observar pendência/conclusão, timeline, Notification,
   conclusão de Order e duplicata. Capturas novas somente se necessárias para
   documentar comportamento alterado; não substituir capturas históricas.
3. Executar validação integrada final, migrations e build/smoke pertinentes.
   Conferir fechamento de processos, isolamento de recursos e ausência de dados
   reais/segredos em código, logs e artefatos.
4. Atualizar estado neste plano e comportamento no README/DEMO. DESIGN registra
   contrato vigente; documentação de benchmark continua indicando protocolos e
   campanhas encerrados. Não replicar diário de execução em todos os documentos.
5. Consolidar SHA, lock, imagens/digests, migrations, comandos, testes/CI do SHA
   exato e limitações na descrição da entrega. Não criar outro documento de
   passagem de contexto com conteúdo redundante.

**Aceite:** API, persistência, recuperação, operação e UI coerentes; verificações
obrigatórias aprovadas e identidade congelável. Lacunas de observabilidade mais
amplas e comparação adiada continuam explícitas. Implementado, verificado,
pré-release e release final são estados distintos.

## 7. Validações e versionamento

Executar testes focais primeiro. Ao concluir alterações transversais e no aceite
final: Ruff, formatação, Mypy, Import Linter e suítes pytest pertinentes; ao final,
suíte integrada completa e cobertura. Migrations exigem Alembic upgrade,
verificação dos heads e drift. Runtime exige build e smoke das imagens afetadas.
Os comandos existentes do AGENTS continuam sendo o ponto de partida; acrescentar
somente comandos que já tenham sido implementados.

CI deve executar testes críticos PostgreSQL/RabbitMQ sem skips e associar o resultado
ao SHA revisado. Testes Windows/PowerShell são obrigatórios quando houver mudança
nesses fluxos, pelo executável real disponível; CI Linux não os substitui. Não
repetir suítes aprovadas sem mudança ou preocupação concreta que justifique.

Fazer commits por assunto/incremento na branch v1.2 quando autorizado pela tarefa,
sem reescrever histórico ou incluir arquivos alheios. Push, merge para main e
publicação de tag/release seguem a autorização específica; não são consequência
automática do aceite funcional. Tags existentes nunca são movidas. Uma nova
pré-release deve apontar ao SHA exato verificado e expor suas limitações.

## 8. Pausa obrigatória após o aceite funcional

Ao concluir IV, entregar síntese de comportamento, verificações, limitações e
identidades. **Não iniciar a matriz extensa nem a v1.3.** Solicitar decisão única:
encerrar este ciclo, preparar comparação delimitada ou propor evolução posterior.
Se a tarefa autorizar somente I/II, a parada ocorre ao concluir esses incrementos;
esta seção não amplia a autorização de implementação.

Uma comparação futura terá pacote e protocolo próprios, podendo avaliar referências
congeladas de várias versões com ferramenta identificada e adaptadores explícitos.
Não exigirá alterar tags antigas. Deverá decidir previamente orçamento total,
carga oferecida, admissão/conclusão, drain, falhas, observabilidade e controles do
host. Pilotos e tentativas anteriores permanecem classificados e separados.

## 9. Evolução posterior, não autorizada neste ciclo

Notifications poderá ser avaliado como serviço assíncrono independente após uma
referência funcional da v1.2. Isso exige contrato, propriedade dos dados, publicação,
idempotência, consultas/UI e recuperação próprios; não basta criar outro consumidor.
A branch futura parte da referência escolhida da v1.2, sem exigir publicação final
ou matriz extensa concluída, desde que o aceite funcional necessário esteja aprovado.

Esta intenção não autoriza componentes genéricos, eventos sem consumidor atual,
infraestrutura cloud ou extração antecipada. A decisão e o plano dessa evolução
pertencerão à branch futura; o DESIGN v1.2 continua restrito a Tracking.
