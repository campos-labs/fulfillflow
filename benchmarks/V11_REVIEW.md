# FulfillFlow v1.1 — síntese e índice das evidências

## Estado atual

Candidata funcional v1.1.0-rc.1 com aceite autorizado; comparação suspensa e
release final pendente. Todas as campanhas
e launchers preparados estão encerrados. Nenhum comando de carga, preparação ou
continuação está liberado. Instruções atuais da aplicação: [README](../README.md).
Contratos: [DESIGN](../DESIGN.md). Etapas e aceite: [RELEASE_PLAN](../RELEASE_PLAN.md).
Uso da ferramenta: [benchmarks/README](README.md).

A última campanha conservou nove repetições aprovadas pelos validadores (cinco
v1.0 e quatro v1.1). A r05 v1.1 permanece inválida por falha obrigatória de coleta:
helper de transporte com `OSError`, saída 2; a causa nativa não foi preservada.
A conciliação somente leitura encontrou 3.296 comandos, recibos e finalizações,
sem inbox `RECEIVED` pendente. Resultados HTTP parciais não são medição completa.
O 503 de uma campanha anterior continua sem causa determinada. Não combinar
campanhas nem inferir equivalência ou estabilidade a partir das tentativas parciais.

## Índice das evidências recentes

Os links em `results/` apontam para artefatos locais preservados; não fazem parte
da publicação Git. Cada pacote mantém seus inventários e identidades originais.

| Registro | Evidência | Classificação |
| --- | --- | --- |
| Última campanha: nove válidas e r05 inválida | [Análise final](results/active03-r05-final-analysis-01/summary.json) | Comparação incompleta |
| Correção de perda de códigos nativos | [CI e preservação](results/active03-r05-final-ci-01/report.json) | Ferramenta corrigida, sem nova carga |
| Alteração manual de brilho na operação 01 | [Confirmação](results/comparison-active-brightness-confirmation-01/report.json) | r01 preservada com ressalva ambiental |
| Operação 02 e pré-requisitos Docker | [Revisão](results/comparison-active03-preflight-review-01/report.json) | Causa histórica não comprovada |
| Recuperação funcional e identidade examinada | [Identidade](results/comparison120-r04-recovery-assessment-01/application-identity.json) | Evidência funcional, não causalidade do 503 |
| Estado persistido após o 503 anterior | [Investigação somente leitura](results/comparison120-r04-readonly-investigation-01/metrics-summary.json) | Histórico preservado |

A cópia independente das evidências e a integridade/WAL históricos permanecem
pendentes. As lacunas de observabilidade constam do [histórico de logs](V11_HISTORY.md#política-efetiva-de-logs--revisão-sem-carga) e do DESIGN.

## Rastreabilidade da candidata funcional

Escopo aprovado: DESIGN §30.1–30.4 e §30.6; Core + Tracking síncronos,
PostgreSQL segregado, contratos públicos preservados e recuperação por reentrega.
Nenhuma mudança funcional ou de observabilidade acompanha este fechamento.

| Contrato | Testes existentes / evidência | Limite |
| --- | --- | --- |
| HMAC sobre bytes originais, adapters Alpha/Beta | `tests/unit/test_carrier_adapters.py`, testes de webhook em `tests/api` | Autenticação anterior à persistência; sem transportadora real |
| Autenticação interna e preservação de contratos públicos | `tests/api/test_service_contracts.py` | Rotas internas fora do OpenAPI público |
| Recibo e resultado original após perda de resposta | `test_lost_core_response_recovers_original_result_after_a_later_event` | Transporte ASGI com falha simulada; não explica o 503 |
| Fronteiras de commit e retomada | `tests/api/test_commit_boundaries.py` | PostgreSQL real, interrupções injetadas nos commits locais |
| Concorrência, conteúdo conflitante, ordenação e finalização única | `tests/api/test_tracking_concurrency.py` | Sessões independentes e contenção exercitada |
| Nenhum checkout SQL durante HTTP | `test_no_sql_checkout_crosses_http_and_correlation_survives_forwarding` | Fronteiras exercitadas pelo teste |
| UI, formulários e jornada do simulador | `tests/ui`, `tests/e2e/test_external_simulator_journey.py` | Testes automatizados, não aprovação visual |
| Timeout/cancelamento HTTP após commit confirmado | [37 testes focais e limites](results/comparison120-r04-recovery-assessment-01/README.md) | Dois casos incorporados em `tests/api/test_http_recovery.py`; CI exige execução sem skips |

A [CI do SHA de partida](https://github.com/campos-labs/fulfillflow/actions/runs/34853513728)
aprovou os gates existentes, incluindo UI, E2E, migrações, cobertura e smoke Compose.
Os 37 testes da investigação são evidência local preservada, com identidade própria;
não equivalem a testes executados dentro da imagem congelada.

**UI/DEMO conferido em 14/09/2026:** a jornada foi repetida no navegador, com
projeto `fulfillflow-rc-demo-01`, volume próprio e porta loopback `18841`, usando
a imagem congelada da aplicação. Dois Orders e duas Shipments criados pelos
formulários; seis envios sintéticos (cinco APPLIED e um DUPLICATE), todos HTTP 200.
Conferidos navegação, filtros, timeline, inbox sanitizado, Notifications e conclusão
automática do Order. A duplicata não acrescentou inbox, timeline ou Notification.
[Resultado e identidades](results/v11-rc-demo-visual-01/report.json),
[recursos](results/v11-rc-demo-visual-01/resources.json) e
[checksums](results/v11-rc-demo-visual-01/checksums.sha256) preservados.
Somente os containers novos foram parados graciosamente; o volume permanece.
Nenhum benchmark, rebuild ou alteração dos recursos históricos foi realizado.

As quatro imagens de `docs/assets/demo` continuam idênticas à tag v1.0.0
(origem `893d7375a83a41bcf475f8037b7ff174b8a5381a`); suas legendas no DEMO
agora identificam essa origem. Não foram criados novos arquivos de captura.
Esta verificação funcional não demonstra estabilidade sob carga nem explica o 503.

## Identidades para o fechamento

- SHA de partida documental: `8786247ff52db3575eaff9246464df3d7d277310`.
- Aplicação medida: `948cefdf881b10af2c073be5536411daefb3faf2`.
  `src`, `uv.lock` e `infrastructure/init-databases.sh` não diferem do SHA de partida.
- Core e Tracking usam a identidade de imagem registrada
  `sha256:3f084ff174304f3d475c6daa22fe058f2aafc9bc9126139b4529f8328cd10b40`.
- PostgreSQL registrado: `sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280`.
- Loadgen comparativo v1.1 registrado: `sha256:543c5756796ac4173aa57771ac846c060aa68ae1872184cc0bf42afb76b29431`.
- Fonte: [manifest da operação 03](results/comparison-active-review-03/candidates/reviewed-comparison120-mixed-4-win9445-01-active03-c1-v11-v11-execution.json).
  Runner/coordenador e hashes dos componentes: [pacote](results/comparison-active-review-03/review.json);
  derivação do loadgen: [inventário](results/comparison-active-review-03/images.json).

Estas são identidades registradas nos artefatos, sem nova inspeção do engine ou
reconstrução. O campo histórico `release=v1.1.0` do manifest não significa tag ou
release publicada. A futura rc terá identidade Git própria, sem renomear imagens.

## Resultados separados por campanha

Nenhuma soma entre linhas forma uma matriz; resultados parciais e diagnósticos
não são promovidos a medições válidas. Todas as operações estão encerradas.

| Campanha | Resultado preservado | Evidência |
| --- | --- | --- |
| Baseline v1.0 publicada | 30 válidas, host histórico | [Publicação](baselines/v1.0/README.md) |
| Piloto v1.1 | Uma mixed/4 válida, não oficial | [Análise](results/v11-pilot-win9445-analysis/metrics-summary.json) |
| Controles v1.0 | Duas mixed/4 válidas, não oficiais | [Análise](results/v10-controls-win9445-analysis-01/metrics-summary.json) |
| Primeiro ABBA | Inválido por coleta | [Análise](results/paired-win9445-failure-analysis-01/failure-summary.json) |
| ABBA revisado | Quatro válidas, exploratórias | [Análise](results/reviewed-abba-win9445-analysis-01/metrics-summary.json) |
| Matriz original 60 s | Dez mixed/4 válidas; warm-up v1.1/12 incompleto, 3.722/5.160 | [Análise](results/reviewed-official-win9445-analysis-01/metrics-summary.json) |
| Verificação warm-up 60 s | 3.497/5.160, inválida e sem medição | [Análise](results/warmup12-verification-analysis-01/metrics-summary.json) |
| Sensibilidade 60/120 s | Oito observações; v1.1/60 s incompletas (3.677 e 3.674); demais quotas completas; sem medição | [Revisões individuais](results/warmup-sensitivity-win9445-reviews-01) |
| Comparação 120 s e sua continuação | Oito válidas (5 v1.0, 3 v1.1), incluindo r01 preservada; r04 inválida por 503 | [Análise](results/comparison120-continuation-failure-analysis-01/metrics-summary.json) |
| Diagnóstico de tela ativa | Quatro válidas não oficiais; r05 interrompida antes do warm-up; critério de cinco não atendido | [Análise](results/active-screen-block-analysis-01/report.json) |
| Comparação ativa 01 | r01 preservada pelos validadores com incerteza ambiental; r02 bloqueada por alteração de brilho | [Revisão](results/comparison-active-brightness-confirmation-01/report.json) |
| Operação ativa 02 | Encerrada; diagnóstico insuficiente para atribuição histórica ao Docker | [Revisão](results/comparison-active03-preflight-review-01/report.json) |
| Comparação ativa 03 | Nove válidas (5 v1.0, 4 v1.1); r05 inválida por coleta | [Análise](results/active03-r05-final-analysis-01/summary.json) |

## Histórico e limites

[Histórico integral encerrado](V11_HISTORY.md): redação, comandos, pacotes,
auditorias de energia/logs e decisões anteriores. Nenhum desses comandos está liberado.
Logging estruturado, métricas e tracing permanecem lacunas; não há conformidade
integral de observabilidade. Comparação incompleta, 503 sem causa determinada,
integridade/WAL históricos e cópia independente não confirmada continuam explícitos.

## Fechamento da pré-release funcional

Os dois casos de deadline/cancelamento HTTP após commit foram incorporados ao
fechamento. No deadline, o timeout total asyncio pode cancelar o transporte antes do read timeout
HTTP; o teste aceita essas duas interrupções reais e exige 503. No cancelamento
explícito, exige CancelledError. Ambos verificam recibo original e finalização única. Tracking → Core
usa TCP real; a entrada pública usa ASGI. Isso não reproduz nem explica o 503 histórico.
A CI inclui gate explícito dos dois casos sem skips. O resultado final está vinculado
ao commit da tag anotada `v1.1.0-rc.1` nas notas da pré-release; não se confunde com
a CI do SHA de partida acima. Evidências locais desta validação ficam em
[final-validation-01](results/v11-rc-final-validation-01), sem inclusão dos arquivos
gerados na publicação Git. Aceite funcional não conclui desempenho ou observabilidade.
