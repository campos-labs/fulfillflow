# FulfillFlow — Release Plan

Incrementos I e II concluídos; comparação oficial e release v1.1 pendentes.
Piloto, dois controles e novo ABBA válido preservados. A matriz contemporânea
concluiu dez repetições mixed/4 (cinco por versão), mas parou no primeiro warm-up
v1.1 mixed/12, antes da medição. A única verificação manual limitada ao warm-up
reproduziu quota incompleta. Os launchers estão encerrados; células de 12 usuários
bloqueadas na matriz original, sem repetição ou retomada automática. Uma campanha
separada de sensibilidade à política de warm-up foi concluída conforme §30.5.
A comparação simétrica de 120 s foi liberada em `02fe942` e parou antes do
segundo warm-up por falha de coordenação, preservando uma medição v1.0 mixed/4
válida. A continuação depois executada encerrou na r04 v1.1 mixed/4, com um 503
na medição: a campanha conserva oito medições válidas pelos validadores e permanece
incompleta. Ambos os launchers estão encerrados. O procedimento atual prepara somente
cinco repetições não oficiais v1.1 mixed/4 com tela e sistema ativos, conforme §30.5;
a verificação ociosa 02 está encerrada e aprovada. A operação 02 encerrou com quatro
medições não oficiais válidas e r05 interrompida na estabilização por falha de leitura
do heartbeat, antes do warm-up. O critério de cinco sucessos não foi atendido.
Corrigem-se somente transporte do heartbeat e captura de energia, sem nova carga.
A revisão da condição de avanço foi aprovada para preparar 60 medições novas com
host ativo nos dois lados, sem incorporar resultados anteriores nem completar r05.
O diagnóstico permanece incompleto. A carga exige pacote próprio, CI e isolamento.

## 1. Referência e objetivo

A v1.0.0 é o monólito modular publicado e preservado. A v1.1 extrai deliberadamente a capacidade de Tracking para avaliar autonomia, comunicação, consistência e custo operacional. Ganho de desempenho não é requisito de aprovação nem conclusão antecipada.

- Commit apontado pela tag anotada v1.0.0 e base documental: `6235f6cb2a733e23ea76cf8264d2145f3759a871`.
- Commit efetivamente medido na baseline: `ae15e0a2da465f4aec3d9c699655441ad1947265`.
- Integração da release em main por fast-forward conferida: main e release/v1.0.0, locais e remotas, estão nessa base no início do trabalho.
- Branch de trabalho criada localmente: `codex/v1.1-tracking`.
- Conferir o estado real antes de editar; não repetir integração ou recriar referências existentes.
- Preservar tag, manifests, resultados e publicação histórica da v1.0.

## 2. Escopo e fronteira alvo

Manter Python, FastAPI, PostgreSQL e Docker Compose. Dois serviços de aplicação: Core e Tracking. Sem RabbitMQ, Redis, Kubernetes, AKS, gateway adicional ou novas capacidades de produto nesta release.

| Responsabilidade | Proprietário |
| --- | --- |
| Orders, Shipments, Notifications e suas máquinas de estados | Core |
| Cadastro de transportadoras: identidade, código, nome, situação ativa e associação às Shipments | Core |
| HMAC, schemas externos, adapters Alpha/Beta e normalização para evento canônico | Tracking |
| Inbox bruto, idempotência de recepção, coordenação do processamento, eventos e timeline | Tracking |
| Aplicação do evento canônico sobre Shipment, Order e Notification | Core |

Tracking interpreta o evento externo; Core decide seus efeitos sobre o domínio. Consultas necessárias ao cadastro e aplicação de eventos usam contratos internos estreitos, autenticados e com timeouts finitos. Credenciais HMAC ficam disponíveis ao serviço que verifica a assinatura, sem divulgação em contratos ou logs.

A entrada pública permanece no Core, encaminhando as operações de Tracking por cliente HTTP assíncrono. Preservar rotas, payload bruto para autenticação, headers relevantes, códigos, schemas e X-Request-ID. Encaminhamento não mantém transação ou conexão SQL aberta. Não confundir I/O assíncrono com processamento em fila: a coordenação permanece síncrona no fluxo da requisição.

Usar uma instância PostgreSQL 18 com dois bancos e credenciais segregadas. Cada serviço acessa somente seu banco. Remover dependências de FKs entre proprietários e substituir as garantias necessárias por contratos e validações explícitas; não duplicar regras de domínio. Manter migrações por proprietário e documentar a preparação dos dados da v1.1, sem alterar as evidências da v1.0. O compartilhamento da instância limita o isolamento de infraestrutura e deve ser registrado.

## 3. Consistência e recuperação

Substituir a transação global do monólito por três passos duráveis:

1. Tracking autentica, persiste inbox bruto e fixa identidade estável do evento/comando recuperável, respeitando os casos sem persistência do contrato vigente (DESIGN §14.3).
2. Core aplica em transação local: recibo idempotente, Shipment, Notification e Order; preserva regras de ordenação e locks aplicáveis, incluindo Shipment antes de Order.
3. Tracking grava o resultado/timeline e finaliza o inbox em transação local. Uma confirmação HTTP 200 exige a conclusão dos efeitos previstos para esse resultado.

O recibo Core possui chave única por identidade do evento, vinculada à transportadora, hash de conteúdo imutável e resultado original persistido. Preservar a unicidade de recepção por `(carrier_id, external_event_id)`. A retomada mantém identidade e `received_at` original; perda de resposta não reaplica estado, duplica notificações nem recalcula o resultado usando um estado posterior. Mesmo ID com conteúdo diferente é conflito. Rejeições permanentes têm resultado reproduzível. Distingui-las de `NO_STATE_CHANGE` e `IGNORED_*`, que continuam resultados HTTP 200 com timeline; rejeições permanentes não criam TrackingEvent (DESIGN §13.1 e §14.3).

Distinguir retomada de RECEIVED de DUPLICATE já finalizado. Após a recepção, falhas transitórias preservam RECEIVED e retornam 503; erros inesperados retornam 500, como no contrato vigente. Reentrega idêntica retoma o fluxo e rejeição já registrada é reproduzida. Não há worker de retry nem promessa de recuperação automática sem reentrega.

Entre commits pode haver Core atualizado e timeline pendente. A v1.1 não oferece atomicidade global. Documentar e testar esse intervalo, inclusive a chegada de evento posterior antes da finalização do anterior, concorrência, respostas perdidas e eventos fora de ordem. O mecanismo concreto deve ser o mínimo necessário para preservar as regras e impedir efeitos duplicados; não acrescentar broker, saga genérica ou compensação destrutiva.

## 4. Contrato da comparação

Referências: DESIGN.md, benchmarks/README.md e benchmarks/baselines/v1.0/RELATORIO.md, além dos manifests oficiais publicados. A baseline não identifica Tracking como causa exclusiva de contenção.

Preservar workload, contratos externos, dataset lógico, coortes, pesos, q=430, spawn rate 16 users/s, matriz 4/12 users × mixed/timeline/ingestion × cinco repetições válidas por ponto. O protocolo original conserva warm-up de 60 s. A nova campanha simétrica de §30.5 mantém estabilização de 300 s após preparação, warm-up de 120 s com ritmo proporcional, measurement de 300 s, coleta de 1 s e as regras congeladas de admissão, drain, timeouts e energia. Logging, tracing/sampling, timeouts de pool/SQL e healthcheck do loadgen permanecem conforme os manifests e Compose publicados.

| Componente | CPU | Memória | Pool |
| --- | --- | --- | --- |
| Core | 1 | 768 MiB | 5, overflow 0 |
| Tracking | 1 | 768 MiB | 5, overflow 0 |
| PostgreSQL compartilhado | 2 | 2560 MiB | Conforme serviços acima |
| Loadgen | 2 | 1536 MiB | Não aplicável |

Um worker por serviço. Totais de aplicação: 2 CPUs, 1536 MiB e 10 conexões de pool. Não multiplicar orçamento ao extrair. Dois processos versus um, partição fixa de CPU/pool e encaminhamento pelo Core são diferenças explícitas do experimento. Não mudar essa divisão após observar resultados oficiais; qualquer necessidade material de revisão deve ser decidida antes do congelamento.

Preservar o ambiente aprovado nos manifests, inclusive versões e tolerância de memória Docker de 1 MiB. Não atualizar ferramentas ou recalibrar silenciosamente. HEAD, imagens de aplicação, migrações e schemas v1.1 terão identidades próprias; não fingir que hashes físicos de schemas separados são iguais ao schema v1.0.

Preservar locustfile, artefato/seed determinística e conteúdo lógico do dataset congelados; não exigir que o loader físico monolítico permaneça idêntico. Adaptar somente a preparação, distribuição dos mesmos dados lógicos e verificações exigidas pelos dois bancos. O gerador atual (`benchmarks/dataset.py`) importa normalização de Carriers e contratos de Shipments/Notifications: a extração exige conferir esses acoplamentos, sem duplicar regras nem mudar os dados. Se for necessária mudança em artefato congelado, apresentar conflito concreto antes de prosseguir.

Compatibilidade do loadgen resolvida e validada no incremento II: o parent congelado aceita somente v1.0/app; a imagem derivada autorizada substitui apenas `benchmarks/campaign.py` e aceita manifest v2 com release/topologia Core e Tracking explícitas. A auditoria comprovou preservação de locustfile, dataset e dependências. Parent e candidata têm identidades distintas registradas no pacote; a diferença de imagem/validação é declarada na comparação. Não alterar novamente imagem ou protocolo sem proposta específica.

Coletar Core e Tracking separadamente e agregados, incluindo o custo de comunicação e recibos. Reconciliar HTTP, Locust e efeitos nos dois bancos; preservar exportação final após drain e completude da telemetria. Dados auxiliares novos, como recibos, devem ser identificados sem alterar silenciosamente o estado lógico inicial.

## 5. Incrementos

### I — Extração funcional integrada

**Estado: concluído e enviado na branch autorizada, com CI aprovada.** Core e Tracking executam com bancos/roles e
migrações segregados; API/UI encaminham os contratos públicos e a preparação
funcional pela API está disponível em `scripts/prepare_demo_v11.py`.

- Reconciliar este plano com código e instruções locais; aplicar a arquitetura alvo planejada já registrada no DESIGN §30, preservando sua distinção do contrato histórico v1.0. O plano organiza a execução, não substitui o DESIGN.
- Implementar os contratos internos e o recibo Core, separação de persistência, migrações e configuração Compose.
- Extrair adapters, HMAC, normalização, inbox e timeline; integrar encaminhamento e adaptar API/UI.
- Entregar um fluxo completo executável, incluindo preparação local dos dados necessária à validação funcional.
- Testar desde o primeiro incremento HMAC/adapters, contratos, persistência em PostgreSQL, caminho feliz, duplicatas concorrentes e retomada após perda de resposta do Core. Manter gates aprovados; adaptar os contratos arquiteturais somente à fronteira aprovada.

Aceite: aplicação utilizável pelos contratos públicos, bancos segregados, eventos processados sem duplicação no fluxo validado e documentação de execução coerente. Não deixar mecanismos essenciais de consistência como placeholders. Casos adversos restantes ficam explicitamente listados para II.

### II — Consistência e prontidão experimental

**Estado: implementação concluída; prontidão operacional sob revisão.** Os testes válidos de I foram
reutilizados. II acrescenta interrupções antes/depois de cada commit, perda de resposta
de rejeição, indisponibilidade dos peers e concorrência combinada com falhas. O harness
prepara e verifica os dois bancos, registra suas identidades e conta recibos, coleta
recursos separados/agregados e concilia efeitos. O diff do loadgen foi apresentado e
autorizado: imagem derivada com somente `campaign.py` alterado, sem mudar workload,
dataset ou dependências. O pacote é `benchmarks/V11_REVIEW.md`. Piloto e controles
posteriores não substituem a matriz oficial; sua situação está registrada abaixo.

- Completar testes reais de concorrência, rejeições, falhas em cada fronteira de commit, respostas perdidas, reentrega e ordenação.
- Validar ausência de acessos cruzados aos bancos e de recursos SQL retidos durante HTTP.
- Adaptar harness de preparação, identidades, telemetria e reconciliação ao desenho v1.1; preservar o protocolo aceito.
- Validar restauração determinística dos dois bancos e gates funcionais, de integração e CI pertinentes. A preparação só declara prontidão quando ambos conferem com o estado inicial esperado; falha parcial bloqueia o ensaio, sem prometer commit SQL global entre bancos.
- Preparar manifests candidatos, comandos e estimativa de duração para a campanha, sem iniciá-la.

Aceite: regras de recuperação comprovadas, nenhum efeito duplicado, efeitos previstos conciliados após HTTP 200, limites compartilhados conferidos e pacote pronto para revisão. Não criar uma nova campanha ampla de calibração; ensaios exploratórios adicionais exigem uma lacuna concreta e escopo delimitado.

### III — Comparação e release

- Após autorização específica e preparação do host, realizar validação prévia sob carga estritamente necessária e congelar a identidade candidata.
- Obter as 30 repetições oficiais válidas previstas, com destino novo, preservação dos artefatos e interrupção em falhas. Não contar diagnósticos ou tentativas inválidas como oficiais nem repetir execuções válidas sem justificativa.
- Consolidar v1.1 e comparação com v1.0, relatando dispersão, limitações, diferenças de arquitetura e ausência de garantia causal fora do desenho observado.
- Arquivar evidências, verificar cópia independente e publicar seletivamente documentação, manifests e índice; manter arquivos brutos fora do Git.
- Preparar integração final, tag v1.1.0 e notas para autorização. Não mover v1.0.0.

Aceite: matriz concluída sem duplicação, contagens e checksums íntegros, relatório reproduzível, gates aprovados e release rastreável. Ganho de throughput não é critério de aceite.

## 6. Documentação e continuidade

- RELEASE_PLAN.md registra escopo, incrementos e situação de conclusão; não vira diário de comandos.
- DESIGN.md distingue o incremento funcional implementado da preparação experimental planejada. Não reescrever decisões históricas como se a v1.0 já fosse distribuída.
- README.md descreve o que funciona e seus comandos; não apresenta incrementos pendentes como entregues.
- Ler AGENTS.md existente e instruções aplicáveis. Corrigir somente orientações obsoletas que conflitem com a separação aprovada; não duplicar o plano, criar regras genéricas ou enfraquecer gates.
- Sem HANDOFF.md permanente. Ao fim de cada incremento, fornecer resumo curto para o próximo chat: branch/commit, mudanças, validações, pendências, estado local e próxima ação.
- Preservar .vscode/settings.json e artefatos locais preexistentes fora do trabalho autorizado.

Este plano não autoriza por si só push, merge, publicação, cargas oficiais ou operações destrutivas. O prompt de cada incremento define as ações autorizadas. Leituras, implementação e verificações pertinentes ao incremento autorizado não exigem aprovações repetidas.

## 7. Limite da release e evolução futura

A v1.1 termina com Core + Tracking, contratos estáveis, consistência explicitada e comparação concluída. Extrações adicionais, mensageria e operação Kubernetes/AKS ficam fora deste plano; qualquer evolução posterior depende de planejamento próprio, sem compromisso antecipado com versão ou arquitetura.

## Situação ao concluir o incremento I

Registro histórico do primeiro incremento; a situação atual está abaixo.

- v1.0 publicada e main integrada, com referências locais/remotas conferidas nesta auditoria.
- Branch local `codex/v1.1-tracking`, com implementação, testes e documentação do incremento I.
- I: concluído. II: pendente. III: pendente de implementação e autorização de execução.
- Gates locais: pytest completo com cobertura acima de 80%, testes estruturais históricos sem alteração de hashes, Ruff/format, Mypy, Import Linter, Alembic dos dois proprietários e build/smoke Docker com Alpha/Beta. CI adaptada, sem execução remota nesta tarefa.
- Somente os imports necessários do gerador foram relocados; workload, protocolo, manifests publicados, imagem congelada do loadgen e evidências v1.0 preservados.
- Preparação funcional não equivale a prontidão de benchmark. Não houve push, merge, tag, release ou campanha.

## Situação do incremento II

Incrementos I e II concluídos. O piloto posterior não oficial mixed/4, q=430, no Windows
`26200.9445` foi válido: 108,04 req/s, p95 82 ms, sem erros e com conciliação. Evidências em
`benchmarks/results/v11-pilot-win9445-attempt-01` e análise em
`benchmarks/results/v11-pilot-win9445-analysis/metrics-summary.json`.
A baseline `26200.9278` permanece histórica; não atribuir a diferença exclusivamente à
arquitetura ou ao Windows. Campanha oficial e decisão sobre a comparação continuam pendentes.
Os dois controles exploratórios v1.0 mixed/4 no host atual foram executados manualmente e
verificados: 190,0128/189,8441 req/s, p95 35/35 ms, zero falhas e efeitos conciliados.
Ambos ficaram dentro das margens práticas fixadas antes da execução; isso não prova
equivalência nem valida seis células. Análise derivada em
`benchmarks/results/v10-controls-win9445-analysis-01/metrics-summary.json`.
`benchmarks/V11_REVIEW.md` registra a comparação descritiva e a preparação autorizada de
dois pares AB/BA (quatro execuções novas), com fontes e imagens congelados por versão,
destinos novos e regras de leitura prévias. A execução manual começou, mas A1 v1.0
foi invalidado por coleta incompleta; B1/B2/A2 não começaram. O diagnóstico ocioso
posterior não reproduziu a falha nem estabeleceu sua causa. DESIGN §30.5 delimita a extensão.
Nenhuma nova carga foi executada na análise e nenhum resultado histórico foi alterado.

- Branch autorizada: `codex/v1.1-tracking`; incremento I enviado e CI remota aprovada.
- Implementados matriz adversa adicional, loader/restauração segregados, identidades v1.1,
  deadlines internos compatíveis, telemetria separada/agregada, conciliação e geração de manifests candidatos.
- Adaptação da imagem do loadgen revisada e autorizada: parent preservado e somente
  `campaign.py` substituído. Dataset, locustfile, dependências e evidências v1.0 preservados.
- Pacote de revisão: `benchmarks/V11_REVIEW.md`. Manifests e relatórios observados ficam
  em destino novo sob `benchmarks/results`, com checksums, sem fingir congelamento experimental.
- Verificações executáveis: testes funcionais/integração, gates estáticos, CI, restauração
  repetida e smoke de telemetria ociosa. O relatório final do chat informa resultados e commits.
- A matriz oficial do incremento III não foi executada; o piloto não a substitui.
  A entrada PowerShell original foi preparada sem carga no chat 11; a execução posterior
  e a revisão atual estão distinguidas no pacote. Não houve merge, tag ou release v1.1.

## Correção auditada e referência contemporânea

Foi autorizada a correção do coletor/runner, com diagnóstico sanitizado e supervisão
limitada a 250 ms, preservando consultas, cálculos, cadência e gates. A identidade da
aplicação é validada em checkout separado; a ferramenta de host tem proveniência própria.
As imagens e evidências medidas permanecem intactas. A política efetiva de observabilidade
é preservada; suas lacunas de implementação continuam explícitas e não foram corrigidas
por reescrita dos requisitos históricos.

Preparar novo ABBA mixed/4 e, depois, uma referência no Windows `26200.9445` com 30
execuções novas por versão. Cada célula possui dois blocos de cinco: mixed/4 v1.0→v1.1;
mixed/12 v1.1→v1.0; timeline/4 v1.0→v1.1; timeline/12 v1.1→v1.0;
ingestion/4 v1.0→v1.1; ingestion/12 v1.1→v1.0. A ordem é fixa, não randomizada;
permanece possível efeito de ordem dentro da célula. A referência publicada é histórica.

Os pacotes são preparados sem carga; execução será manual após prontidão comprovada.
Qualquer falha interrompe a sequência, preservando válidas e inválidas sem repetição
automática. Preparação, resultados válidos e encerramento de release são estados distintos.

## Interrupção da matriz e verificação limitada ao warm-up

O novo ABBA foi concluído e revisado; a matriz seguinte tem dez repetições mixed/4
válidas preservadas. A primeira v1.1 mixed/12 encerrou o Locust com código 2 no
warm-up: 3.722 aplicações de 5.160 exigidas, sem erros HTTP. O coletor registrou
61 ciclos completos e nenhuma falha; o rótulo histórico `snapshot` está incorreto.
A correção do runner distingue as fontes de falha sem reescrever esse diagnóstico.

A única tentativa diagnóstica v1.1/12 autorizada em DESIGN §30.5 foi executada
manualmente: 3.497/5.160 aplicações, zero falhas HTTP, Locust exit 2 e 61 ciclos
completos de coleta. Não houve medição. O diagnóstico corrigido registrou
`process_exit`; a conciliação posterior confirmou 3.497 comandos/recibos/finalizações.
A regra prévia de falha equivalente mantém 12 usuários bloqueados. Nenhuma repetição
válida foi substituída ou promovida a outra campanha. Não repetir o launcher.
A proposta antiga de doze diagnósticos permanece inativa.

Decisão posterior autorizada: preparar uma campanha separada de sensibilidade
60/120 s, duas observações por versão/duração, somente warm-up, na ordem de §30.5.
Essa autorização não amplia nem retoma a matriz original. Preserva recursos,
aplicação, dataset e quota; altera explicitamente duração e ritmo de admissão.
Não prepara uma nova matriz oficial. Evidências, interpretação e bloqueios em
`benchmarks/V11_REVIEW.md`; a proposta antiga de doze diagnósticos segue inativa.


## Nova comparação simétrica de 120 s — interrupção de coordenação

A sensibilidade encerrou oito tentativas. v1.0: duas quotas completas em 60 s e
duas em 120 s. v1.1: 3.677 e 3.674/5.160 em 60 s, duas completas em 120 s.
Isso sustenta a política comum; não prova teto físico, estabilidade, causalidade
exclusiva ou ausência de defeitos. Os 60 s permanecem requisito do protocolo
histórico, sem reclassificação como SLA. Evidências e próxima entrada estão em
`benchmarks/V11_REVIEW.md`; todos os launchers anteriores estão encerrados.

A nova campanha terá 30 medições por versão, cinco por perfil/carga, em blocos na
ordem aprovada. Nenhuma medição anterior será incorporada. Piso temporal: 12 horas,
além de preparação, verificações, drain e exportação. Qualquer falha interrompe;
não se aplica a exceção da sensibilidade para quota incompleta. Fontes, imagens
da aplicação, dados e observabilidade permanecem congelados. Runner e loadgen têm
identidades próprias. Preparação e testes sem carga antecedem revisão independente,
commit em `codex/v1.1-tracking`, CI do commit e conferência final do pacote.
Release pendente; cópia independente das evidências sem confirmação.


A execução liberada em `02fe942` preservou r01 v1.0 mixed/4 válida e uma r02
parcial anterior ao warm-up. O verificador exigia `loads` dentro de
`protocol_expected`, embora o runner exporte `load` separadamente. Corrigir a
comparação e o diagnóstico não altera carga ou aplicação. A sequência encerrada
não será repetida; a continuação implementada exige identidade e vínculo explícitos com
a medição preservada, sem reescrever seus metadados ou completar artificialmente
o bloco histórico. Ver `benchmarks/V11_REVIEW.md`.
