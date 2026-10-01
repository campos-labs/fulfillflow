# FulfillFlow — Verificação funcional entre referências congeladas

## 1. Estado, objetivo e escopo

Revisão documental 3, de 2026-09-23. **Incrementos I e II concluídos;
conjunto avaliado B encerrado com 12 casos PASS. C permanece pendente.** Esta revisão define o complemento focal da v1.2
(escopo B) e os limites da expansão v1.0–v1.2 (escopo C). A expansão exige fechar
os procedimentos específicos antes das execuções que integrarão seus resultados.

Verificar invariantes e mecanismos de recuperação diante de intervenções
identificadas. Não estimar confiabilidade geral, capacidade, SLA ou superioridade
de arquitetura. Não retomar campanhas de carga, investigar o 503 histórico,
alterar energia do host, incluir v1.3 ou implementar funcionalidades de produto.

Este documento rege somente a ferramenta de verificação. Os contratos de cada
aplicação continuam pertencendo ao DESIGN de sua referência congelada. Toda adição
ou alteração desta atividade fica em `validation/functional/`, incluindo futuros
scripts, testes, configurações e registros. Não editar arquivos fora da pasta,
inclusive DESIGN, RELEASE_PLAN, README da raiz, AGENTS, código, migrations, locks,
workflows e testes existentes. Uma necessidade concreta fora desse escopo deve ser
apresentada antes de qualquer alteração; não reescrever contratos para aprovar testes.

## 2. Referências e identidade independente

Branch de trabalho: `test/cross-version-functional`, em worktree próprio,
originada de `v1.2.0-rc.1`. A base da ferramenta não substitui as aplicações avaliadas.

| Referência | Commit da aplicação |
| --- | --- |
| v1.0.0 | `6235f6cb2a733e23ea76cf8264d2145f3759a871` |
| v1.1.0-rc.1 | `217e29a230689da3bd6359790f0753b41a10a927` |
| v1.2.0-rc.1 | `9b445f9b5466cd302c89f1deed7a9c051cb397ae` |

Tags e commits foram resolvidos localmente nesta preparação. Confirmar a resolução
novamente antes de executar. Esses commits são referências funcionais; não atribuir
automaticamente a eles resultados produzidos por SHAs anteriores de benchmarks.

O pacote executável registrará, separadamente:

- revisão/hash do protocolo e SHA/hash dos componentes da ferramenta;
- commit, caminho de importação efetivo, lock e hashes do código de cada aplicação;
- Python e dependências instaladas; imagens por ID/digest quando usadas;
- migrations, topologia, limites de recursos, configurações efetivas permitidas;
- host/SO, engine, horários UTC e durações monotônicas.

O ambiente do escopo B está descrito na seção 10; os ambientes do escopo C
ainda não foram selecionados. Não presumir que uma tag de imagem corresponde ao commit. Reusar uma imagem somente após comprovar
proveniência; eventual build de teste terá identidade nova, sem sobrescrever imagens
preservadas nem ser apresentado como a imagem historicamente medida.

Executar cada versão em processo e ambiente próprios, com suas dependências
congeladas. Não importar v1.0/v1.1 usando silenciosamente os módulos ou o lock da v1.2.
Fixtures e adaptadores da ferramenta também terão origem identificada. Não atualizar
dependências, fazer merge, mover tags ou publicar release como parte desta preparação.

## 3. Regras comuns de avaliação

1. Fixar casos, critérios, ordem e limites antes das execuções avaliadas. O protocolo
   é informado pelos registros anteriores; não alegar avaliação cega ou concepção
   anterior à implementação das versões.
2. Separar desenvolvimento da ferramenta, execução avaliada e regressão existente.
   Uma execução de desenvolvimento não será promovida retrospectivamente a resultado.
3. Usar PostgreSQL real e, para entrega/ACK/retomada AMQP, RabbitMQ real. Declarar
   individualmente transporte TCP, ASGI, barreira de teste e falha injetada.
4. Observar estados em conexões SQL independentes; resposta HTTP, fila vazia,
   quantidade total de linhas ou retorno de função isoladamente não comprovam conclusão.
5. Preservar tentativas e resultados desfavoráveis. Não ajustar prazos, repetir até
   passar ou selecionar apenas sucessos. Correção da ferramenta exige revisão nova
   e identificação dos casos afetados, sem apagar o resultado anterior.
6. Não confundir correção de um contrato com facilidade de recuperação. Reentrega
   pelo cliente, reinício de processo e rearme administrativo são ações distintas.
7. O complemento não substitui os testes de desenvolvimento nem as campanhas
   históricas. Seus resultados não integram matrizes de desempenho anteriores.

## 4. Isolamento e execução segura

Preparar projetos Docker, redes, volumes, bancos, roles, vhost RabbitMQ, portas e
diretórios novos, identificados por execução. Nenhuma URL herdada do shell ou
`.env` deve selecionar implicitamente um recurso. Validar propriedade e destinos
antes de migrations, escrita, parada ou limpeza. Não reutilizar volumes históricos,
não restaurar bancos sobre eles e não parar processos por nome genérico.

Um worktree isola arquivos, não o daemon Docker. Antes de iniciar, conferir recursos
concorrentes e disponibilidade das dependências; não encerrar recursos alheios
automaticamente. Não é necessário recuperar bancos históricos para usar dados novos.

Aplicações e processos auxiliares usarão dados sintéticos, sockets locais e nenhuma
integração externa. Evitar escrita de caches, ambientes e artefatos fora da pasta
desta atividade; configurar explicitamente seus destinos. Não editar checkouts
congelados para instalar hooks.

Preservar configuração da aplicação, retries, timeouts e política de logs. Os limites
do observador não são timeouts novos do produto. Registrar recursos de APIs, workers,
PostgreSQL e broker; não alegar orçamento total equivalente ao benchmark histórico.

PowerShell bloqueante pelo executável real
`C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe`.
A entrada resolve caminhos pela própria localização, funciona fora da
raiz do checkout, propaga falhas e recusa sobrescrita. Não oferecer comando de
execução antes de sua implementação e revisão. Nenhuma carga está autorizada.

## 5. Escopo B — interrupção abrupta durante aplicação SQL

### 5.1 Casos e condições iniciais

Dois casos independentes, cada um com controle sem interrupção:

| ID | Trabalho já confirmado antes da intervenção | Fronteira do processo interrompido |
| --- | --- | --- |
| B-CORE | Admissão Tracking, comando/outbox e inbox técnica Core persistidos; entrega AMQP confirmada por ACK | Handler real do Core executou os efeitos SQL e a outbox de resultado, antes do commit da transação externa |
| B-TRACKING | Core já confirmou recibo/efeitos/outbox; resultado recebido e confirmado por ACK na inbox técnica Tracking | Handler real do Tracking executou timeline/finalização, antes do commit da transação externa |

Usar um Order, um Shipment e um evento Alpha determinístico cuja transição para
`DELIVERED` seja válida e conclua o Order. Cenários não compartilham banco nem
identidades entre tentativas. Preservar o mesmo conteúdo lógico no controle e na
interrupção, com namespaces rastreáveis. Esse recorte não cobre todos os adapters,
transições, múltiplos Shipments ou concorrência entre réplicas.

Com o processador-alvo ainda impedido de consumir trabalho local, comprovar por
SQL independente inbox técnica `PENDING`, identidade/hash e estado de negócio
anterior. Comprovar entrega e ACK, sem mensagem pronta ou não confirmada no broker
para o item-alvo, por observação atualizada do canal/broker; não reutilizar contagem
antiga de declaração da fila. Fila vazia é condição do caso, não prova de conclusão.
A preparação pode usar adaptadores explícitos e funções de transporte reais;
não apresentar preparação orquestrada como jornada integral sem intervenção.

### 5.2 Barreira e encerramento

Executar o worker em processo filho real. Um wrapper exclusivo da ferramenta,
fornecido ao runtime existente, chama o handler real e verifica os efeitos esperados
na própria sessão. Sinaliza por IPC local a fronteira atingida e aguarda sem retornar.
A posição é depois do SQL/flush do handler, ainda dentro do savepoint e da transação
externa, antes de marcar a inbox técnica `DONE` e confirmar o commit.

Registrar PID/identidade do filho, caso, evento, marcador da transação e confirmação
da barreira. Comprovar que o filho continua vivo e que os efeitos ainda não aparecem
em conexão independente. O controle usa o mesmo wrapper e libera a barreira, sem kill.

Somente após essas confirmações, encerrar abruptamente o filho identificado
(`SIGKILL` no Linux ou mecanismo equivalente de término forçado no Windows),
aguardar saída e registrar resultado nativo. Não usar SIGTERM, Ctrl+C ou parada
graciosa como substitutos. Não matar PostgreSQL, RabbitMQ ou o coordenador.

Não usar atraso arbitrário para adivinhar a fronteira. Barreiras por arquivo sujeito
a disputa não são a opção inicial; preferir IPC com mensagem pequena e espera
limitada. A instrumentação não pode fazer commit, trocar handlers por stubs,
alterar payloads ou acrescentar recuperação. Seu efeito temporal será declarado;
esse ensaio não produz medidas comparáveis de latência.

### 5.3 Evidência antes de reiniciar

Consultar novamente o banco, depois da saída do filho e da liberação de sua conexão:

- **B-CORE:** inbox técnica continua retomável, sem incremento confirmado de tentativa
  causado pela transação abortada; recibo, Notification e outbox de resultado desse
  evento não foram confirmados; Shipment/Order preservam o estado anterior.
- **B-TRACKING:** inbox técnica de resultado continua retomável; não há timeline nem
  finalização de negócio confirmadas; recibo, resultado e efeitos já confirmados
  no Core permanecem íntegros. A admissão Tracking continua `RECEIVED`.
- Em ambos, a leitura deve comprovar o registro específico e seus vínculos.
  Pendência não pode ter sido consumida por outro processador antes desse snapshot.

Preservar IDs, hashes, correlação e timestamps **já duráveis antes da falha**.
UUIDs ou horários gerados somente na transação abortada não precisam reaparecer
iguais. Depois da recuperação confirmada, duplicatas devem preservar o resultado
durável original. Não usar a igualdade de relógio artificial como prova disso.

### 5.4 Retomada e critério de aprovação

O coordenador reinicia o worker proprietário sem a barreira e inicia somente os
demais workers necessários ao fluxo. Isso verifica retomada após reinício explícito,
não reinício automático de processo por supervisor externo. Usar os entrypoints normais, sem chamada direta a `drain`,
processamento manual, SQL corretivo, rearme ou reentrega do webhook para recuperar.

Observar até conclusão ou prazo: um recibo, uma Notification, uma timeline para o
evento, Shipment `DELIVERED`, Order `FULFILLED`, inbox de negócio `PROCESSED`,
inboxes técnicas pertinentes `DONE`, resultado/correlação coerentes e ausência de
trabalho bloqueado ou publicação ainda pendente do item. Não somar a mesma unidade
de negócio em inboxes/outboxes como se fossem eventos distintos.

Após conclusão, uma reentrega idêntica adicional verifica idempotência; ela não
participa da recuperação. Deve retornar a duplicata prevista e preservar efeitos,
identidades e resultados finais. Registrar quantas chamadas foram efetuadas.

O braço de interrupção passa somente se fronteira, kill, rollback observado,
retomada sem reentrega e invariantes finais forem demonstrados. O controle deve
atingir/verificar a mesma barreira, liberá-la sem kill e demonstrar commit, conclusão
e idempotência. Encerramento forçado e snapshot pós-kill não se aplicam ao controle.
Ambos verificam efeitos e resultado confirmado. Não equivale a avaliar perda
permanente de armazenamento, queda elétrica, partição prolongada, reinício após
qualquer instrução ou confiabilidade geral.

### 5.5 Quantidade, ordem e limites do observador

Três pares controle/interrupção por proprietário, sempre com estado novo:
B-CORE 1–3 e depois B-TRACKING 1–3, controle imediatamente antes da interrupção.
São 12 execuções de cenário. Ordem fixa e três repetições verificam repetibilidade
limitada; não sustentam significância estatística nem probabilidade de falha.

| Etapa | Limite |
| --- | --- |
| Inicialização e preparação por cenário | 120 s |
| Atingir/verificar barreira | 30 s |
| Encerrar filho forçado | 15 s |
| Observar rollback antes da retomada | 30 s |
| Observar conclusão após liberação/reinício | 60 s |
| Exportação e encerramento | 30 s |
| Limite global por cenário | 300 s |

Usar relógio monotônico para esperas, com polling do observador de 100 ms e prazo
global prevalente. Tempo de domínio e autenticação devem ser coerentes; não avançar
artificialmente o relógio do worker para antecipar retries. Controles seguem os
mesmos limites aplicáveis. Esses prazos são limites da ferramenta, não SLA.

Fixar a identificação executável antes do primeiro resultado avaliado. Se a
viabilidade demonstrar limite inadequado, revisar o protocolo antes desse conjunto,
preservando diagnósticos de desenvolvimento. Não aumentar prazo durante a sequência.
Falha interrompe o conjunto; não há reposição automática de tentativas.

## 6. Escopo C — expansão condicionada

Esta seção delimita a expansão; **ainda não é um protocolo executável liberado**.
Antes das execuções avaliadas, completar aqui o mapeamento de cada versão, precondições,
pontos de intervenção, consultas, respostas esperadas, ordem, quantidade e prazos.
Não construir um framework genérico nem incluir todos os testes antigos.

| Cenário | Critério comum | Diferenças a explicitar |
| --- | --- | --- |
| C1 — Evento repetido | Mesmo ID/bytes não gera novo efeito | Duplicata pendente/terminal; 202 não é conclusão |
| C2 — Mesmo ID, conteúdo diferente | Conflito preserva o original | Autenticar os novos bytes; verificar conflito de conteúdo, não HMAC inválido |
| C3 — Processo interrompido antes do commit dos efeitos | Atomicidade local e recuperação do trabalho específico | Processo HTTP na v1.0/v1.1 versus worker na v1.2; fronteiras adicionais não são réplicas do mesmo caso |
| C4 — Dependência indisponível | Estado aceito/pendente/recusado e retomada identificáveis | Definir dependência e instante por contrato; broker não se aplica à v1.0/v1.1 |
| C5 — Conclusão do fluxo | Mesmos efeitos de negócio e ausência de duplicação | Separar resposta de admissão, efeito no Core e finalização no Tracking |

Para C3, verificar primeiro viabilidade de hard-kill antes do commit de efeitos
na aplicação de cada versão, sem alterar produto. Uma exceção SQL simulada não
substitui silenciosamente a queda de processo. B-TRACKING é uma fronteira adicional
da arquitetura distribuída, não uma vantagem atribuível por contagem de testes.

Distinguir recuperação autônoma de recuperação após **uma reentrega idêntica
predefinida**, quando aplicável ao contrato. Ações e janelas serão registradas
separadamente. Ausência de recuperação autônoma onde ela não é prometida é uma
diferença operacional, não automaticamente um defeito. Casos sem correspondência
ficam explicitamente não aplicáveis; não recebem aprovação ou reprovação artificial.

B pode fornecer casos/adaptadores para C. Seus resultados não serão incorporados
retroativamente ao conjunto comum como se tivessem seguido um protocolo ainda não
fixado. Preservar referências e registros originais; novas execuções comuns terão
identidade própria. Decidir continuidade por viabilidade, escopo e tempo disponível,
nunca pela conveniência dos resultados. Resultados adversos não podem ser omitidos.

## 7. Registros, classificação e falhas da ferramenta

Cada execução futura terá destino novo e inventário de arquivos. Registrar protocolo,
ferramenta/aplicação, ambiente, cenário/repetição, modo (desenvolvimento/avaliado),
etapas/horários, barreira, processo, intervenção, snapshots, asserções e resultado.

Exportar relatório estruturado, JUnit com IDs dos casos, logs permitidos e checksums.
Preservar diagnóstico primário, falha de exportação e falha de encerramento
separadamente, inclusive registros parciais. Usar campos controlados: nunca ambiente
completo, credenciais, DSNs secretos, assinaturas ou webhook completo nos logs.

Separar validade da execução e resultado funcional:

- `PASS`: procedimento completo e critérios satisfeitos.
- `FAIL`: intervenção/precondições comprovadas, mas invariante violado ou conclusão
  não observada no limite previsto; não extrapolar para impossibilidade permanente.
- `INCONCLUSIVE`: não é possível atribuir o desfecho à aplicação ou à ferramenta.
- `INVALID`: fonte incorreta, fronteira não atingida, dependência de preparação
  ausente ou falha de instrumentação que impede executar o caso.
- `NOT_RUN` / `NOT_APPLICABLE`: sem execução / sem correspondência prevista,
  sempre com motivo; nunca contar como sucesso.

Não reclassificar falha funcional como inválida apenas para repetir. Falha de
dependência só invalida quando ela é requisito de preparação; em C4 pode ser a
intervenção pretendida. Encerramento deliberado do filho é esperado, mas timeout
do coordenador não é. Qualquer `FAIL`, `INCONCLUSIVE` ou `INVALID` interrompe a
sequência e exige análise antes de uma revisão/continuação identificada.

Registros existentes mantêm sua classificação. Nova verificação não resolve o
503 histórico nem comprova a integridade dos volumes antigos. Cópia independente
dos novos registros terá destino autorizado e confirmação própria; worktree não
é backup. Não transferir evidências externamente nesta atividade sem autorização.

## 8. Incrementos, validação e ponto de parada

| Incremento | Entrega | Saída esperada |
| --- | --- | --- |
| Preparação atual | Este protocolo e branch/worktree isolados | Documento revisado; nenhum teste novo executado |
| I | Instrumentação, isolamento e casos B; testes da ferramenta e execução focal autorizada | Evidência do ponto de falha, rollback e retomada, ou limitação concreta |
| II | Revisão de B, identidades e resultados completos/parciais | Síntese técnica; decisão sobre iniciar C |
| III, condicionado | Fechamento do protocolo C, adaptadores e execução autorizada | Matriz por caso/versão, incluindo falhas e não aplicáveis |
| IV | Consolidação das evidências e limitações | Encerramento da atividade; nenhuma campanha extensa automática |

A validação da ferramenta deve cobrir: barreira ausente,
filho encerrado antes do ponto, tempo esgotado, destino existente, origem incorreta,
preservação de erro e exportação parcial. Realizar checks focais primeiro; não repetir
suítes integrais sem impacto que justifique. Não substituir testes reais por mocks.

Executar explicitamente os testes novos no Windows e, para alegar suporte Linux,
também nessa plataforma. Skips críticos bloqueiam o aceite do caso. A CI existente
não cobre automaticamente uma pasta nova; registrar o comando e escopo realmente
executados. Se integrar CI exigir arquivo fora desta pasta, propor a alteração
separadamente; não declarar cobertura que não ocorreu.

Scripts/testes ficarão nesta pasta e usarão a stack aprovada, sem dependência nova
ou edição de locks. Commits, push, publicação e execução dos próximos incrementos
não fazem parte da preparação atual. Entregar comandos somente quando necessários
e revisados, indicando destino, duração limitada e comportamento de interrupção.

## 9. Evidência existente usada na preparação

Leitura de contratos e testes, sem novas execuções nesta revisão:

| Referência | Arquivo/caso consultado | Alcance |
| --- | --- | --- |
| v1.2.0-rc.1 | [DESIGN §§6 e 10](../../DESIGN.md) | Atomicidade local, ACK técnico e limites de recuperação |
| v1.2.0-rc.1 | [test_worker_recovery.py](../../tests/e2e/test_worker_recovery.py), `test_fresh_worker_recovers_acknowledged_work_with_empty_queue` | Worker novo retoma pendência após ACK; não encerra worker dentro do handler |
| v1.2.0-rc.1 | [test_commit_boundaries.py](../../tests/api/test_commit_boundaries.py), `test_commit_interruption_preserves_atomic_stage_and_recovers_locally` | Exceções em fronteiras de commit; não equivalem a hard-kill |
| v1.2.0-rc.1 | [messaging/store.py](../../src/fulfillflow/messaging/store.py), `process_one`; [messaging/worker.py](../../src/fulfillflow/messaging/worker.py), `serve` | Handler/savepoint/DONE dentro da transação externa; shutdown SIGTERM gracioso |
| v1.1.0-rc.1 | `tests/api/test_http_recovery.py::test_real_http_interruption_after_core_commit_recovers_without_duplicate_effects` | HTTP de loopback interrompido após commit; recuperação por reentrega |
| v1.0.0 | `tests/api/test_tracking.py::test_transaction_a_survives_failed_b_and_identical_delivery_resumes` | Transação A preservada após falha injetada em B; recuperação por reentrega, sem hard-kill |

Os links locais apontam ao checkout-base v1.2. Para arquivos de outras versões,
usar `git show '<commit>:<caminho>'` com o SHA da seção 2. Linguagem de planejamento
nos documentos congelados não autoriza reescrita retrospectiva; interpretar sua
origem junto dos registros de aceite, preservando cada original.

## 10. Concretização do incremento I

A ferramenta usa os módulos congelados da v1.2 por caminho explícito, com verificação
da origem importada e de todas as versões instaladas contra o lock. O Python 3.13
existente é somente reutilizado, sem instalar/atualizar pacotes nem modificar seu
ambiente. A ferramenta não modifica os fontes da aplicação. O pacote de cada
execução preserva cópia dos componentes e hashes antes de iniciar dependências.

A preparação pública e as consultas entre APIs usam ASGI no coordenador; os
workers são processos nativos independentes no Windows. PostgreSQL e RabbitMQ
usam containers reais, com imagens por digest, uma CPU e 512 MiB cada. Esses limites
servem à verificação funcional e não reproduzem o orçamento das campanhas.
Os processos nativos não têm quota Docker de CPU/memória. Pools usam três conexões,
sem overflow; timeout de pool 5 s e de statement 5.000 ms. Retries, relógio real,
polling do worker e configurações restantes seguem os contratos congelados.
Registrar a concorrência no host; não inferir desempenho desses tempos.

A ferramenta cria roles e bancos Core/Tracking separados por caso e aplica as
migrations congeladas. Cada caso recebe um vhost exclusivo, com usuários Core e
Tracking e as permissões AMQP da referência. Credencial administrativa serve só
à preparação da topologia; não é usada pelos workers. A preparação do caso
B-TRACKING aplica o comando antes do worker-alvo por funções reais e transação
explícita, como precondição; a recuperação posterior usa somente workers normais.

Os workers iniciam no diretório da aplicação para localizar as configurações de
migrations originais. A ferramenta recusa `.env` nesse diretório; temporários e
heartbeats permanecem dentro do destino do caso. O healthcheck espera a aplicação
RabbitMQ (`check_running`), pois o ping da VM Erlang pode responder antes dela.

A opção `-NetworkSubnet` permite declarar uma sub-rede privada IPv4 `/28` para a
rede nova, com identidade efetiva registrada. Conferir previamente conflitos com
as rotas do host; o Docker também rejeita sobreposição de suas redes. Não selecionar
ou tentar sub-redes automaticamente, remover redes antigas ou modificar o daemon.
Na preparação local, as faixas automáticas estavam ocupadas e foi conferida a
faixa `10.254.240.0/28`. Essa seleção não altera imagens ou política da aplicação.

No Windows/venv CPython 3.13.1 verificado, lançar o executável-base com a identidade
`__PYVENV_LAUNCHER__` da mesma venv evita um PID intermediário do redirecionador.
O teste nativo confere PID, prefixo e origem de dependências; outro patch Python
fica bloqueado até verificação específica. Nenhuma dependência é instalada.

O modo de desenvolvimento admite seleção focal de proprietário/repetições.
Essas execuções verificam a ferramenta e não integram automaticamente as 12
execuções avaliadas da seção 5.5. O modo avaliado exige arquivo de liberação e
SHA-256 explícito, vinculando destino, sub-rede, identidade integral de aplicação,
runtime e ferramenta e a ordem exata dos 12 casos. As identidades são reconferidas
antes e depois de cada caso; divergência interrompe a sequência. Não repetir um
identificador encerrado. A liberação concluída está registrada na seção 12.

A tentativa de desenvolvimento anterior `results/b-dev-01` permanece preservada:
nenhum cenário foi iniciado; o RabbitMQ não ficou disponível. A inspeção identificou
erro de permissão na leitura de seu cookie. O healthcheck/CLI da ferramenta passou
a usar o usuário do broker; isso não constitui alteração da aplicação ou evidência
de falha de recuperação. Registros posteriores receberão destinos novos.


## 11. Resultado do incremento I — desenvolvimento

`results/b-dev-06` passou nos quatro casos (controle e hard-kill de Core e Tracking),
com PostgreSQL/RabbitMQ reais, processos Windows e sub-rede `10.254.240.16/28`.
A barreira comprovou SQL local ainda não visível externamente. Após os dois kills,
o snapshot coincidiu com o estado anterior; o worker normal retomou sem reentrega.
Houve um recibo, uma Notification e uma timeline finais; a duplicata posterior
preservou o resultado. Nos casos Tracking, o estado Core já confirmado não mudou.
Os códigos de saída do encerramento forçado de limpeza não são falhas da aplicação;
a intervenção do caso é registrada separadamente no campo `kill`.

A revisão independente dos snapshots e o inventário de 69 arquivos do conjunto
passaram. Os 157 arquivos identificados da aplicação continuam idênticos aos hashes
anteriores à execução. O pacote executado, protocolo e testes foram arquivados
antes de iniciar as dependências; esta atualização documental posterior não
altera esse pacote. Relatório: [summary.json](results/b-dev-06/summary.json).

Validação da ferramenta: **181 testes, sem skips**, Ruff, formatação, Mypy dos sete
módulos e dez contratos existentes de Import Linter aprovados. Os casos aplicaram
as migrations congeladas em bancos novos. Não se repetiram suítes integrais do
produto nem builds de aplicação: seus arquivos e dependências não mudaram.
Não houve validação Linux nem CI nova; os checks registrados foram locais Windows.

As tentativas `b-dev-01` a `b-dev-05` estão preservadas e são inválidas, sem
promoção ao conjunto avaliado. Houve indisponibilidade inicial do RabbitMQ,
classificação insuficiente da preparação, readiness que verificava somente Erlang,
recusa de alocação de rede e bloqueio da identidade do processo. A prontidão prematura
foi reproduzida em `b-cli-check-01` e corrigida/conferida em `b-cli-check-02`.
A reprodução focal do redirecionador Windows comprovou PID intermediário e validou
o lançamento direto com a mesma venv. A mensagem nativa da recusa de rede não foi
preservada; seu inventário e a seleção explícita estão registrados separadamente.
Essas ocorrências não constituem falhas de recuperação demonstradas na aplicação.

Todos os novos containers encerraram preservando dados, volumes e redes; o recurso
concorrente inventariado permaneceu ativo. Checksums anteriores foram reconferidos.
Não houve alteração em DESIGN/RELEASE_PLAN ou qualquer arquivo versionado fora desta
pasta, commit, push, carga ou publicação. A cópia independente continua não confirmada.

**Ponto de parada do incremento I:** revisão do incremento II, congelamento do pacote avaliado e
liberação explícita das 12 execuções B. Os quatro casos acima não substituem esse
conjunto. O escopo C e todas as campanhas extensas continuam suspensos.


## 12. Resultado do incremento II — conjunto avaliado B

`results/b-evaluated-01` encerrou com saída zero, seguindo a ordem e os limites da
seção 5.5, sem reposição, reclassificação ou incorporação dos casos de desenvolvimento.

| Fronteira | Controles | Interrupções abruptas | Resultado |
| --- | --- | --- | --- |
| Core, depois do SQL do handler e antes do commit externo | 3 | 3 | Todos PASS |
| Tracking, depois do SQL do handler e antes do commit externo | 3 | 3 | Todos PASS |

Em cada interrupção, o PID da barreira foi o processo encerrado, os efeitos não
confirmados desapareceram e o estado anterior permaneceu retomável. Após reinício
explícito pelo coordenador, os workers normais concluíram sem reentrega do webhook.
Uma duplicata adicional, enviada somente depois da conclusão, preservou o resultado.
Nos casos Tracking, os efeitos Core já confirmados permaneceram idênticos.
Os controles liberaram a mesma barreira e concluíram sem interrupção experimental.
O término forçado de limpeza, posterior às verificações, tem registro separado.

A revisão dos snapshots confirmou um recibo, uma Notification simulada, uma timeline,
Shipment `DELIVERED`, Order `FULFILLED`, inbox de negócio `PROCESSED`, inboxes técnicas
`DONE` e outboxes `SENT`, sem quarentena nem rearme. Fila vazia antes do worker foi
verificada em todos os casos. Não se atribui conclusão de negócio ao ACK técnico.

Registros, com funções distintas:

- [Resultado original](results/b-evaluated-01/summary.json), snapshots por caso,
  JUnit e checksums: evidências da execução.
- [Revisão consolidada](.artifacts/increment2-review-01/report.json): matriz dos 12
  casos, conferências de preservação, validações da ferramenta e limites.
- [Liberação anterior à execução](.artifacts/b-evaluated-package-01/release.json):
  identidade fixa, ordem e destino; `readiness.json` registra imagens, rotas e recursos.

A aplicação permaneceu em `9b445f9b5466cd302c89f1deed7a9c051cb397ae`, com os 157
arquivos identificados inalterados. A ferramenta executada foi
`81d8dac30905127963b04541b25d5541b8d3902b35acac801b2a72d5bd8e514d`;
a liberação SHA-256 foi
`28a2dfd7658325e8bd40842edf2e7794e0f81de0eccece89dce0e595d494b786`.
A rede nova usou `10.254.240.32/28`, após conferência de conflitos. Os dois containers
próprios encerraram com saída zero, sem OOM; dados e volumes foram preservados.
O container concorrente inventariado permaneceu ativo.

Validação da ferramenta: **192 testes, zero skips**, incluindo PowerShell real,
recusa de identidade divergente e parada sem próximo caso. Ruff, formatação e
Mypy dos módulos alterados passaram. A revisão reconferiu 341 arquivos de evidência
incluindo conjuntos anteriores. Não foram repetidos builds ou suítes integrais do
produto, nem Import Linter: não houve mudança de aplicação, dependências ou fronteiras
de imports. A aprovação anterior dos dez contratos permanece identificada na seção 11.
Não houve nova CI nem execução da ferramenta em host Linux.

Nesta atualização posterior, a codificação do README foi corrigida e seu estado
atualizado. A reversão da codificação, normalizando quebras de linha, reproduziu o
hash documental anterior do incremento I. O pacote arquivado conserva os bytes
executados e a nota de estado anterior; a autorização do conjunto consta do arquivo
de liberação específico. Nenhum critério, ordem ou prazo foi alterado durante o
conjunto, nem evidência reescrita para acompanhar esta atualização documental.

**Conclusão e limite:** a v1.2 congelada recuperou o trabalho durável nas duas
fronteiras e condições testadas, após reinício pelo coordenador. Isso complementa a
cobertura de retomada após ACK; não uniformiza retrospectivamente os testes das três
versões, não estima confiabilidade, capacidade ou latência, e não resolve o 503.
A aplicação não exigiu correção. Não foi identificada necessidade de mudar sua
arquitetura a partir destes casos.

**Ponto de parada:** B concluído. C exige decisão e detalhamento prévio da seção 6,
inclusive fronteiras correspondentes e ações de recuperação por versão. Uma
expansão será nova verificação, não promoção destes resultados a comparação comum.
Campanhas extensas continuam suspensas. Até o encerramento da execução não houve
commit, push ou publicação;
DESIGN, RELEASE_PLAN e os demais arquivos externos à pasta permanecem inalterados.
A cópia independente das evidências continua não confirmada.


## 13. Preservação do encerramento B

Código, testes e protocolo são versionados nesta branch em commit posterior ao
conjunto avaliado. Esse commit registra a ferramenta; não é o SHA da aplicação
executada. As identidades e os pacotes originais das seções 11 e 12 permanecem
intactos. Não repetir os launchers ou identificadores encerrados.

O pacote local de transporte fica em `.artifacts/b-closure-01/`, com índice,
checksums e cópias byte a byte das evidências selecionadas. Inclui todos os
conjuntos de desenvolvimento/avaliação B com manifest, a liberação avaliada,
revisões de I/II e os registros finais de testes. Não inclui volumes, imagens,
credenciais, caches ou uma instalação completa. Um ZIP no mesmo disco não
constitui backup independente; a confirmação exige destino autorizado e
conferência dos hashes após a cópia.

O validador exige a aplicação no SHA congelado da seção 2. Após este commit,
o checkout da ferramenta deixa de ser esse checkout de aplicação; a recusa do
launcher neste diretório é esperada. Uma futura execução exigirá fonte congelada
separada e pacote novo, sem relaxar essa validação. C permanece não liberado.
