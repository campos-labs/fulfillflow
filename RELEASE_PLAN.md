# FulfillFlow — etapas e aceite da v1.1

## Estado atual e aceite pendente

| Estado | Situação e limite |
| --- | --- |
| Candidata funcional | Incrementos I e II implementados; aceite funcional autorizado para a pré-release v1.1.0-rc.1, com rastreabilidade na síntese e conferência UI/DEMO concluída. Não equivale à release final. |
| Comparação suspensa | Matrizes incompletas e classificações preservadas por campanha. Nenhuma retomada, carga ou continuação autorizada. |
| Release final pendente | Mantém o aceite do incremento III: matriz, relatório reproduzível, checksums, gates e autorização de publicação. |

A matriz incompleta não autoriza retomar campanhas nem impede, por si, propor
uma evolução seguinte. Qualquer evolução exige proposta e aprovação próprias;
este fechamento não implementa nem aprova novos contratos.

A rastreabilidade está em [V11_REVIEW](benchmarks/V11_REVIEW.md). Recuperação
funcional, desempenho e observabilidade são aceites distintos. Logging estruturado,
métricas e tracing continuam lacunas; cópia independente e integridade/WAL dos
bancos históricos não estão confirmadas. O 503 histórico segue sem causa determinada.

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

Preservar workload, contratos externos, dataset lógico, coortes, pesos, q=430, spawn rate 16 users/s, matriz 4/12 users × mixed/timeline/ingestion × cinco repetições válidas por ponto. O protocolo original conserva warm-up de 60 s. O contrato simétrico de §30.5 mantém estabilização de 300 s após preparação, warm-up de 120 s com ritmo proporcional, measurement de 300 s, coleta de 1 s e as regras congeladas de admissão, drain, timeouts e energia. Logging, tracing/sampling, timeouts de pool/SQL e healthcheck do loadgen permanecem conforme os manifests e Compose publicados.

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
posteriores não substituem a matriz oficial; sua situação está registrada na síntese V11_REVIEW.

- Completar testes reais de concorrência, rejeições, falhas em cada fronteira de commit, respostas perdidas, reentrega e ordenação.
- Validar ausência de acessos cruzados aos bancos e de recursos SQL retidos durante HTTP.
- Adaptar harness de preparação, identidades, telemetria e reconciliação ao desenho v1.1; preservar o protocolo aceito.
- Validar restauração determinística dos dois bancos e gates funcionais, de integração e CI pertinentes. A preparação só declara prontidão quando ambos conferem com o estado inicial esperado; falha parcial bloqueia o ensaio, sem prometer commit SQL global entre bancos.
- Preparar manifests candidatos, comandos e estimativa de duração para a campanha, sem iniciá-la.

Aceite: regras de recuperação comprovadas, nenhum efeito duplicado, efeitos previstos conciliados após HTTP 200, limites compartilhados conferidos e pacote pronto para revisão. Não criar uma nova campanha ampla de calibração; ensaios exploratórios adicionais exigem uma lacuna concreta e escopo delimitado.

### III — Comparação e release

- Após autorização específica e preparação do host, realizar validação prévia sob carga estritamente necessária e congelar a identidade candidata.
- Obter as 30 repetições válidas por versão previstas no contrato simétrico (60 no conjunto), com destino novo, preservação dos artefatos e interrupção em falhas. Não contar diagnósticos ou tentativas inválidas como oficiais nem repetir execuções válidas sem justificativa.
- Consolidar v1.1 e comparação com v1.0, relatando dispersão, limitações, diferenças de arquitetura e ausência de garantia causal fora do desenho observado.
- Arquivar evidências, verificar cópia independente e publicar seletivamente documentação, manifests e índice; manter arquivos brutos fora do Git.
- Preparar integração final, tag v1.1.0 e notas para autorização. Não mover v1.0.0.

Aceite: matriz concluída sem duplicação, contagens e checksums íntegros, relatório reproduzível, gates aprovados e release rastreável. Ganho de throughput não é critério de aceite.

## 6. Documentação e continuidade

- RELEASE_PLAN.md registra escopo, incrementos e situação de conclusão; não vira diário de comandos.
- DESIGN.md distingue o incremento funcional implementado da preparação experimental planejada. Não reescrever decisões históricas como se a v1.0 já fosse distribuída.
- README.md descreve o que funciona e seus comandos; não apresenta incrementos pendentes como entregues.
- Ler AGENTS.md existente e instruções aplicáveis. Corrigir somente orientações obsoletas que conflitem com a separação aprovada; não duplicar o plano, criar regras genéricas ou enfraquecer gates.
- Sem HANDOFF.md permanente. Ao fim de cada incremento, fornecer resumo operacional curto: branch/commit, mudanças, validações, pendências, estado local e próxima ação.
- Preservar .vscode/settings.json e artefatos locais preexistentes fora do trabalho autorizado.

Este plano não autoriza por si só push, merge, publicação, cargas oficiais ou operações destrutivas. O prompt de cada incremento define as ações autorizadas. Leituras, implementação e verificações pertinentes ao incremento autorizado não exigem aprovações repetidas.

## 7. Limite da release

O aceite de release prevê Core + Tracking, contratos estáveis, consistência explicitada e comparação concluída. Esse aceite ainda não foi atingido. O congelamento como candidata funcional não equivale à publicação da release.

Evolução futura: uma eventual v1.2 exige proposta e aprovação próprias; não está autorizada por este plano.

## Histórico e notas da pré-release

O [histórico encerrado](benchmarks/V11_HISTORY.md) preserva os registros anteriores;
a [síntese da candidata](benchmarks/V11_REVIEW.md) vincula contratos, testes,
identidades e campanhas. Não reutilizar instruções históricas.

### Pré-release funcional — v1.1.0-rc.1

**Aceite funcional aprovado; publicação como pré-release condicionada à CI do commit final.** Extração funcional síncrona
Core + Tracking, com bancos/credenciais/migrações segregados, HMAC sobre os bytes
originais, recibos idempotentes e recuperação por reentrega. API e UI preservam os
contratos aprovados. Não há atomicidade global nem recuperação automática.

Aprovados: contratos do DESIGN §30, implementação dos incrementos I/II,
conferência UI/DEMO e incorporação dos dois cenários HTTP após commit. A CI exige
explicitamente ambos sem skips. A aplicação e as imagens medidas permanecem
preservadas; as mudanças de fechamento são testes, verificação CI e documentação.
As lacunas de observabilidade, comparação suspensa, 503 desconhecido,
integridade/WAL e cópia independente não confirmados acompanham a rc.
Nenhuma alegação de estabilidade geral, equivalência ou ganho de desempenho.

Base do fechamento: `8786247ff52db3575eaff9246464df3d7d277310`. O commit final é
identificado pela tag anotada `v1.1.0-rc.1` e por sua CI vinculada nas notas GitHub.
A CI da base não substitui a validação do commit final.

Após CI aprovada, a autorização permite criar `release/v1.1.0` e a tag anotada
`v1.1.0-rc.1` no mesmo commit, sem mudar a branch de trabalho, main ou referências
históricas. A publicação é somente pré-release, sem latest e sem release estável.
Referências existentes não podem ser sobrescritas. A matriz incompleta e as ressalvas
permanecem abertas; nenhuma nova campanha ou contrato posterior é autorizado.
