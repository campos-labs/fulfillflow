# Incremento II — pacote de revisão

Os incrementos I e II estão concluídos. O piloto posterior não oficial v1.1 mixed/4,
q=430, no Windows 26200.9445 foi válido: 108,04 req/s, p95 82 ms, sem erros e com
conciliação. Evidências: `results/v11-pilot-win9445-attempt-01` e
`results/v11-pilot-win9445-analysis/metrics-summary.json`.
A campanha oficial do incremento III permanece incompleta. O novo ABBA revisado foi
válido; a matriz seguinte concluiu dez repetições mixed/4 e parou no primeiro warm-up
v1.1 mixed/12. A única verificação manual limitada ao warm-up reproduziu a quota
incompleta; células de 12 usuários permanecem bloqueadas. A baseline publicada em 26200.9278,
seu dataset e manifests, e as evidências do piloto permanecem imutáveis.

## Caminho atual — diagnóstico com tela e sistema ativos, aguardando revisão

A continuação encerrou com **8/60** medições aprovadas pelos validadores: cinco
v1.0 mixed/4 (incluindo r01 original) e três v1.1 mixed/4. A r04 v1.1 permanece
inválida por 503 durante a medição, após warm-up completo. Há 82 recibos APPLIED,
80 finalizações e dois inboxes RECEIVED com efeitos no Core. As investigações
`comparison120-r04-readonly-investigation-01`, `comparison120-energy-admin-review-01`
e `comparison120-r04-recovery-assessment-01` permanecem seladas e intactas.
Os relatórios administrativos registram Screen Off sem segmentos Sleep; não
explicam os segundos do 503. Os testes funcionais passaram nos cenários examinados,
sem identificar a causa histórica. Não repetir essas suítes ou os launchers encerrados.

Pacote atual: `results/active-screen-mixed4-review-07`. Contrato:
`active-screen-mixed4-diagnostic-v1`, cinco repetições não oficiais v1.1 mixed/4,
inelegíveis para a matriz. O pacote 01 e sua primeira imagem derivados durante a
preparação ficam preservados, não liberados: a revisão corrigiu a seleção necessária
à exportação dos contadores finais de warm-up. Aplicação, dependências e imagens
históricas não mudam. `image/audit.json` identifica parent, imagem, hashes e diferenças.
A revisão 03 reutiliza a imagem da 02 e preserva esse pacote: acrescenta o fallback
do relatório completo no stderr caso sua própria exportação falhe.
A revisão 04 preserva as anteriores e a mesma imagem, limita a espera de encerramento
do filho a 180 s e identifica o runner como
`active-screen-warmup-and-measurement-not-matrix-eligible`. O rótulo anterior era
um valor fixo herdado da ferramenta, não descrição da execução deste bloco.
Se o filho não encerrar, preservam-se PID, início UTC e condição em
`child-shutdown.json` e stderr, com saída 2 e inspeção manual obrigatória. Nenhum
processo é terminado à força e nenhum recurso é removido por esse tratamento.
No Windows, o PID acompanhado pode ser o redirector Python do venv, pai do
interpretador; não se deve terminar processos por nome ou presumir ausência de filhos.
O guard já estará liberado e inválido. Não repetir o comando após esse bloqueio.
A revisão 05 preserva o selo 04 e incorpora somente a correção de lint do teste
ocioso e os apontamentos para o pacote final; protocolo e imagem não mudam. A revisão 06 normaliza finais de linha dos arquivos
Python após essa correção, preservando o pacote 05.
A revisão 07 preserva o pacote 06, seleciona explicitamente o resultado ocioso 02
para a futura liberação e permite criá-lo por `-IdleAttempt 2`, sem aceitar essa
opção no modo de carga. A tentativa 01 permanece evidência inválida para esse gate.

Destino novo: `results/reviewed-active-screen-mixed-4-win9445-01-screen-v11/run/`,
com `mixed-4-users-r01` até `r05`; falhas conservam o sufixo `.partial`.
Projeto: `fulfillflow-active-screen-mixed4-01-v11`. Preparação limpa por repetição,
300/120/300 s, q430, demais condições congeladas. Piso de uma hora, além de
preparação, verificações, drain e exportação. Qualquer falha encerra o bloco.

Entrada preparada, **não liberada**: `scripts/Invoke-ActiveScreenDiagnostic.ps1`.
O modo `IdleCheck` realiza uma única verificação manual de 960 s em
`results/active-screen-idle-01`, sem benchmark. O modo `Execute` exige o selo
separado `results/active-screen-release-01`, ainda inexistente, e produz o journal
`results/active-screen-operation-01`. Ambos recusam destinos existentes e funcionam
fora do diretório do repositório. A verificação ociosa deve usar os mesmos hashes
do guard e launcher que serão liberados. Não fechar a janela PowerShell: Ctrl+C
permite encerramento; término forçado perde parte dos diagnósticos, embora Windows
remova a solicitação de energia quando o thread termina.
`IdleCheck` não inicia Python, Docker ou qualquer comando de parada. Não exige
administrador para o guard; `/requests` pode exigir elevação e seu erro é capturado
como consulta inconclusiva. Não elevar todo o fluxo. Manter a sessão sem interação
rotineira durante os 960 s para observar o comportamento além do timeout de tela.
O selo de carga exige revisão do isolamento, commit limpo autorizado e CI aprovada
para seu SHA exato, além do resultado ocioso aprovado e dos hashes correspondentes.
Tentativas ociosas posteriores exigem `-IdleAttempt N` e criam destinos numerados
novos, sem sobrescrever as anteriores. Esse parâmetro é recusado no modo `Execute`.

Manter tomada, tampa aberta e sessão desbloqueada durante todo o procedimento.
O guard usa solicitação temporária de tela/sistema e observações nativas a cada
250 ms, sem alterar Samsung Mode, CPU máxima 99% ou plano. Tela Off/Dim, mudança
de sessão, AC não confirmado, suspensão e heartbeat com lacuna superior a 3 s
interrompem. A solicitação não bloqueia toda ação manual, screen saver ou política
de sessão; não confiar nela com tampa fechada ou sessão bloqueada. Capturam-se
powercfg antes/depois e `/requests` ativo/depois com códigos; falha/vazio não
significa ausência de solicitações. A validação ociosa longa permanece manual.

Captura adicional: logs com timestamps entre repetições/falha, stdout e stderr
sanitizados separados do runner, códigos de processo, recursos parciais e horários
reais. A exportação textual usa a sanitização existente, limitada aos últimos
12.000 caracteres; não garante guardar toda a atividade. Nenhuma consulta SQL
periódica adicional ou instrumento da aplicação é introduzido. Lacunas de coleta
não são interpoladas. A exceção original e a correspondência entre respostas e
eventos podem continuar desconhecidas se outro 503 ocorrer.

Isolamento manual posterior à revisão: conferir IDs, imagens e labels do inventário
do pacote; exportar logs finais sanitizados e inventário em destino novo antes de
qualquer parada. Parar graciosamente primeiro Core/Tracking históricos, depois seu
PostgreSQL, e o PostgreSQL de testes `fulfillflow-r04-recovery-tests-01`. Não usar
down, rm, prune ou exclusão de volumes históricos. Divergência ou exportação falha
bloqueia a parada. Containers e volumes históricos ficam preservados; estado
volátil se perde, inclusive todo o banco sintético de testes em tmpfs. Nenhuma
parada foi executada na preparação. O gate recusa quaisquer containers concorrentes
ativos antes do bloco. Limpeza entre repetições atinge apenas o projeto novo próprio.
A cópia independente das evidências ainda não foi confirmada.

Cinco sucessos sustentam somente avaliar a próxima decisão. Novo 503 ou qualquer
falha exige análise; condição ambiental insuficiente impede conclusão sobre a
aplicação. Não há liberação, commit final, CI das mudanças ou nova matriz preparada.

## Histórico — comparação de 120 s interrompida na coordenação

A liberação do commit `02fe942b597f2e85e1bd2df5b9a3be6507561257` teve CI
aprovada e foi executada manualmente. O primeiro bloco parou antes do segundo
warm-up: r01 v1.0 mixed/4 tem warm-up e measurement completos, 186,705826 req/s,
p95 36 ms e zero erros HTTP; r02 contém somente o marcador parcial. Nenhum
bloco v1.1 começou. Preservar todos esses arquivos e suas classificações.

A diferença comprovada é exclusivamente `loads` na comparação de
`protocol_expected`; o runner registra a carga em `load`. O verificador passa
a comparar o protocolo sem `loads` e a validar nome, usuários e spawn rate em
`load` separadamente. O stderr da preparação antes descartado será preservado
sanitizado em `preparation-error.json`, com código de saída, horário, duração,
erro primário e encerramento separados. Não recuperar retrospectivamente uma
mensagem que não foi exportada. Esta falha não demonstra defeito no PostgreSQL
nem insuficiência de capacidade da aplicação.

**A entrada `scripts/Invoke-Comparison120.ps1` e sua liberação 01 estão encerradas;
não repetir.** A correção tem identidade nova e ainda não possui commit/CI ou
liberação de continuação. O pacote pré-commit e a liberação anterior permanecem
intactos. A revisão da correção e o mapa proposto da continuação ficam em
`results/comparison120-coordination-review-01`.

A continuação implementada referencia r01 sob seu runner original e executa
somente r02–r05 de v1.0 mixed/4 e os onze blocos posteriores. São **59 medições
novas**, piso **11 h 48 min**, além de preparação, verificações, drain e exportação.
A r02 parcial anterior permanece encerrada e intacta. Não se reduz o manifest
a quatro repetições nem se copia r01 para os destinos novos.

Pacote para revisão: `results/comparison120-continuation-review-02/review.json`.
Entrada nova: `scripts/Invoke-Comparison120Continuation.ps1`, **não liberada**.
O inventário contém doze destinos com sufixo `-continuation-01`; projetos
`fulfillflow-comparison120-continuation-01-v10` e `-v11`. O journal novo será
`results/comparison120-continuation-execution-02`; a liberação pós-revisão/CI será
`results/comparison120-continuation-release-02`, sem alterar o pacote pré-commit.

O contrato operacional `comparison120-explicit-continuation-v1` exige manifest,
destino e referência externa exatos. Resumo do primeiro bloco: r01 original mais
quatro diretórios novos, com as duas identidades de runner explicitadas. O modo
histórico não aceita o parâmetro de continuação. Prazos e protocolo de medição
não mudam; a alteração dos manifests é somente o nome do projeto isolado.
Falhas interrompem os 59 itens sem retry; não existe retomada genérica.
A revisão 01 foi preservada e substituída pela 02 antes de qualquer execução;
a revisão final acrescentou a verificação do checkout antes da limpeza antiga.

O projeto antigo ainda pode conter containers de aplicação/banco ativos e
containers de migração encerrados. A preparação apenas os identifica. A entrada
manual, após liberação, confere `owned.json` e IDs do inventário selado, exporta
logs no journal novo e remove exclusivamente esse projeto antes de verificar a
admissão dinâmica do host. Containers divergentes bloqueiam essa operação.

Se a exportação de `preparation-error.json` falhar, o relatório completo
sanitizado fica disponível na exceção e no stderr capturado pelo coordenador,
incluindo os erros de processo, encerramento e exportação separadamente.
A correção não reconstrói a mensagem descartada na tentativa histórica.



A sensibilidade foi executada e revisada integralmente:

| Condição | Observação 1 | Observação 2 | Warm-up |
|---|---:|---:|---|
| v1.0 / 60 s | 5.160 | 5.160 | Ambos completos |
| v1.0 / 120 s | 5.160 | 5.160 | Ambos completos |
| v1.1 / 60 s | 3.677 | 3.674 | Ambos inválidos, quota incompleta |
| v1.1 / 120 s | 5.160 | 5.160 | Ambos completos |

Integridade e conciliação confirmadas nas oito; quotas incompletas seguem inválidas
e sem measurement. Consolidação separada, com hashes das revisões preservadas:
`results/warmup-sensitivity-win9445-consolidated-01/metrics-summary.json`.
A continuação 4–8 terminou com código agregado 2, preservando a invalidade da sexta
tentativa. Todos esses launchers estão encerrados; não repetir suas entradas.

A política comum de 120 s foi escolhida para uma campanha nova. Alteram-se duração
e ritmo nominal (14,333… aplicações/s para 4 usuários, 43/s para 12, q430 por usuário).
Não se demonstrou teto universal, causa exclusiva, ausência de defeitos, superioridade,
equivalência ou estabilidade. Os 60 s continuam requisito histórico, sem renomeá-los
como SLA. Nenhuma medição anterior será incorporada à nova matriz.

Protocolo: `symmetric-warmup120-comparison-v1`. Pacote:
`results/reviewed-comparison120-win9445-review-01`. A entrada original
`scripts/Invoke-Comparison120.ps1` foi liberada após revisão e CI e está agora
encerrada pela interrupção descrita acima.

O primeiro pré-requisito de preparação foi bloqueado pela indisponibilidade do
engine Linux (`sailor-ingest.sock`); o registro está preservado em
`results/comparison120-code-review-01`. Após reinício manual do Desktop, o engine
29.7.2 e a identidade exigida do host foram conferidos novamente, sem mudança das
expectativas. Esse incidente ocorreu antes da derivação de imagens e não constitui
uma tentativa da campanha. Esse pacote foi posteriormente liberado e sua execução está encerrada.

| Célula | Primeiro bloco | Segundo bloco |
|---|---|---|
| mixed/4 | v1.0 × 5 | v1.1 × 5 |
| mixed/12 | v1.1 × 5 | v1.0 × 5 |
| timeline/4 | v1.0 × 5 | v1.1 × 5 |
| timeline/12 | v1.1 × 5 | v1.0 × 5 |
| ingestion/4 | v1.0 × 5 | v1.1 × 5 |
| ingestion/12 | v1.1 × 5 | v1.0 × 5 |

Os doze destinos usam
`reviewed-comparison120-{profile}-{users}-win9445-01-c{cell}-{version}-{version}`;
inventário materializado em `review.json` no pacote. Projetos isolados
`fulfillflow-comparison120-win9445-01-v10` e `-v11`, com propriedade comprovada antes
da criação e limpeza. Preparação limpa por repetição reutiliza o procedimento
verificado. Piso 12 horas: 300 s estabilização + 120 s warm-up + 300 s measurement
por repetição; preparação, verificações, drain e exportação acrescentam tempo.

O loader histórico de 60 s permanece estrito. A execução usa contrato separado,
admissão proporcional e timeout do processo de warm-up 90→150 s. Loadgens derivados
dos parents de sensibilidade alteram somente `locustfile.py` e acrescentam
`comparison_protocol.py`; dependências, aplicação e observabilidade preservadas.
Revisões programáticas entre repetições e blocos só permitem continuidade após
validade integral. **Qualquer falha para**, inclusive quota incompleta; não se
aplica a exceção da sensibilidade. Não há reposição ou retomada implícita.

O pacote pré-commit registra hashes dos arquivos em revisão e o SHA de partida.
A liberação original foi registrada separadamente em `results/comparison120-win9445-release-01`,
vinculado ao commit limpo e à sua CI, sem reescrever o pacote de revisão. A cópia
independente das evidências continua sem confirmação; não há transferência externa.

## Campanha encerrada — sensibilidade à política de warm-up

Autorizada após a revisão documental `61543b8`, preservando as dez repetições
mixed/4 válidas e as duas interrupções v1.1/12. Os launchers anteriores permanecem
encerrados; a proposta antiga de doze diagnósticos continua inativa. A matriz
original de seis células permanece incompleta e bloqueada em 12 usuários.

Registro histórico da preparação; comandos desta seção estão encerrados.
Protocolo: `warmup-policy-sensitivity-v1`. Pacote preservado:
`results/warmup-sensitivity-win9445-review-01`. Revisões individuais novas:
`results/warmup-sensitivity-win9445-reviews-01`. A existência de `ready.json`
íntegro e concordante com runner/coordenador é obrigatória; código implementado
não significa pacote pronto, warm-up válido ou release concluída.

| Ordem | Versão | Admissão | Tentativa da condição | Destino em `results/` |
|---|---|---|---|---|
| 1 | v1.0 | 60 s | 1 | `warmup-sensitivity-win9445-01-01-v10-60s` |
| 2 | v1.1 | 120 s | 1 | `warmup-sensitivity-win9445-01-02-v11-120s` |
| 3 | v1.1 | 60 s | 1 | `warmup-sensitivity-win9445-01-03-v11-60s` |
| 4 | v1.0 | 120 s | 1 | `warmup-sensitivity-win9445-01-04-v10-120s` |
| 5 | v1.0 | 120 s | 2 | `warmup-sensitivity-win9445-01-05-v10-120s` |
| 6 | v1.1 | 60 s | 2 | `warmup-sensitivity-win9445-01-06-v11-60s` |
| 7 | v1.1 | 120 s | 2 | `warmup-sensitivity-win9445-01-07-v11-120s` |
| 8 | v1.0 | 60 s | 2 | `warmup-sensitivity-win9445-01-08-v10-60s` |

Projetos isolados `fulfillflow-sensitivity-win9445-01-01` a `-08`, portas locais
18041 a 18048. Ordem fixa, não randomizada, sem eliminação de variação temporal.
Tentativas históricas não substituem condições. Não acrescentar tentativas ou
selecionar apenas sucessos. Os 120 s foram escolhidos após conhecer as falhas
anteriores: variam duração e ritmo nominal (86/s em 60 s; 43/s em 120 s), com
quota idêntica de 430 por usuário/5.160 total. O piso conjunto é 52 minutos de
estabilização/admissão, além de preparação, drain, exportação e revisões.

`scripts/Invoke-WarmupSensitivity.ps1` exige `-Attempt N`, `-PlanOnly` ou
`-PrepareOnly`; executar sem opção é erro. Cada chamada de execução faz somente
uma tentativa, bloqueante e independente do diretório corrente. Usar exclusivamente
o PowerShell verificado em
`C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe`.
Comandos de carga são liberados individualmente após prontidão e revisão; não
reexecutar comandos encerrados. A preparação do pacote verifica fontes, imagens,
dataset, host e Compose sem carga; cada chamada manual prepara e verifica os bancos
limpos antes dos 300 s de estabilização e da admissão. Divergências bloqueiam, sem
atualizar expectativas. Nenhuma janela de medição é executada.

Os manifests de preparação conservam o formato histórico de 60 s para os probes
das fontes congeladas. Os manifests executáveis têm protocolo novo explícito;
somente protocolo, duração e extensão equivalente do timeout de supervisão podem
diferir. O loadgen derivado de cada parent usa os mesmos três arquivos revisados,
com inventário completo e dependências idênticas às respectivas imagens originais.
O override Compose altera apenas a imagem do loadgen. Aplicações, fontes históricas,
recursos, workers/pools, logs, request/drain, coortes e dataset não mudam.

O artefato final `warmup-progress.json` registra contadores por usuário, requisições
pendentes e código controlado da causa nas imagens novas. Verificar os prefixos de
eventos e efeitos esperados, incluindo estados com número ímpar, par ou zero de
aplicações. O total agregado sozinho não aprova o warm-up. `warmup_valid` e
`observation_integrity_verified` são independentes; `valid=false`,
`matrix_eligible=false` e `measurement_executed=false` são permanentes neste modo.
O diretório `.partial` nunca é promovido a uma repetição válida da matriz.

Após exportar diagnósticos, parar apenas os contêineres cuja propriedade foi
conferida; preservar os contêineres, volumes e dados para inspeção. A próxima
condição exige revisão explícita e checksums intactos. Somente quota incompleta
com integridade e conciliação confirmadas permite seguir para a condição seguinte
planejada, com saída não zero e warm-up inválido preservados. Falhas de preparação,
identidade, coleta, exportação, integridade, conciliação, reinícios ou OOM bloqueiam.
A revisão não dispara carga. Não repetir até passar, nem repor tentativa inválida.

Interpretação prévia: v1.1 incompleta em 60 s/completa em 120 s indica sensibilidade
à política, compatível com exigência de ingestão acima do ritmo observado em 60 s;
não estabelece causa exclusiva. Ambas completas em 120 s com conciliação tornam a
política candidata à preparação comum, sem estabilidade estatística ou aprovação
da matriz. Divergências entre repetições devem ser relatadas; erros, inconsistência
ou perda observável de progresso exigem investigação antes de ampliar a campanha.
Duas observações por condição não sustentam significância ou equivalência, e
amostras de 1 s não são repetições independentes. Ausência de erros/OOM/crescimento
em dois minutos não exclui vazamentos ou deadlocks. Não há consultas de deadlocks,
instrumentação nova ou ensaio prolongado. Operacionalmente, os resultados orientam
a política e o custo da preparação; não fundamentam promessas de capacidade ou SLA.

A cópia independente das evidências continua sem confirmação; nenhuma transferência
externa está autorizada. A validação sem carga do conferidor sobre o PostgreSQL
preservado e a conferência dos hashes ficam em
`results/sensitivity-preparation-audit-01`; são validações da ferramenta, não novas
tentativas nem recuperação da mensagem interna do Locust histórico.

## Verificação anterior encerrada — 12 usuários bloqueados na matriz original

A verificação `reviewed-warmup-mixed-12-win9445-01-w1-v11` foi executada manualmente
e encerrada com falha equivalente à anterior, sem measurement. O runner corrigido
registrou `process_exit`, código 2, `collector=null`, sem erros secundários de
encerramento ou exportação. A quota incompleta é evidência derivada dos arquivos
finais; a razão textual interna do Locust continua indisponível.

| Evidência do warm-up v1.1/12 | Primeira interrupção | Verificação única |
| --- | ---: | ---: |
| Aplicações confirmadas / quota | 3.722 / 5.160 | 3.497 / 5.160 |
| Déficit | 1.438 | 1.663 |
| Throughput final de ingestão | 61,9253 req/s | 58,1752 req/s |
| p95 | 240 ms | 260 ms |
| Erros HTTP | 0 | 0 |
| Ciclos completos de coleta | 61 | 61 |

Preparação, dataset, imagens, recursos e identidade/condições exigidas do host
conferiram; as identidades de aplicação, runner e coordenador correspondem ao
pacote. Na verificação, os intervalos descritivos de 10 s mostraram Core em
96,7–98,4% de uma CPU e Tracking em 88,1–90,8%, com memória abaixo dos limites.
A inspeção posterior não registrou OOM ou reinício. Os bancos conciliaram 3.497
comandos, recibos e finalizações. Esses achados são compatíveis com restrição de
capacidade nas condições congeladas; não isolam a contribuição da arquitetura,
logging ou Windows nem estabelecem probabilidade de falha.

A análise nova é `results/warmup12-verification-analysis-01/metrics-summary.json`;
`preserved-runtime-observation.json` identifica a observação SQL posterior.
Checksums da tentativa, diário, preparação e resultados anteriores foram conferidos.
Os dez resultados mixed/4 válidos estão preservados. A comparação permanece incompleta.
O requisito de realizar a única verificação manual foi cumprido; seu warm-up não
passou. Nenhuma nova tentativa está preparada ou autorizada por esse resultado.

A decisão pendente é manter o protocolo atual e registrar o gate de 12 usuários
não atendido, ou autorizar uma revisão específica do protocolo, simétrica entre
versões e com campanha separada. Não ajustar quota, duração, recursos ou aplicação
para repetir até passar; não reutilizar resultados antigos como membros de uma
campanha com protocolo diferente. Esta documentação registra a decisão necessária,
sem implementar uma mudança metodológica.

### Preparação e primeira interrupção — referência preservada

Preservar as cinco repetições v1.0 e cinco v1.1 em
`results/reviewed-official-mixed-4-win9445-01-c1-{v10-v10,v11-v11}`.
A tentativa `reviewed-official-mixed-12-win9445-01-c2-v11-v11` parou antes da
medição. O diário `reviewed-official-win9445-execution-01` está encerrado.
Não repetir suas séries ou usar seus pacotes com o runner corrigido.

O Locust registrou exit 2. Os arquivos finais confirmam 3.722 aplicações,
nenhum erro HTTP e quota incompleta (5.160 exigidas; déficit 1.438). A razão
textual interna não foi exportada pela imagem congelada; essa quota é conclusão
derivada dos artefatos, não uma mensagem interna recuperada. O coletor tem 61
ciclos completos e `collector=null`. O rótulo `snapshot` e a mensagem de falha
de coleta no JSON antigo estão incorretos e permanecem intactos como evidência.
O novo runner registra `process_exit`, fase e código, distinguindo coleta,
encerramento, exportação e validação. Não muda prazos, supervisão de 250 ms,
consultas, cálculos, amostragem ou imagem do loadgen.

A análise nova está em `results/warmup12-investigation-01/analysis.json`.
Após o trecho inicial, intervalos descritivos de aproximadamente 10 s mostram
59,7–62,7 aplicações/s; p95 móvel exportado 230–270 ms. O Core consumiu em média
97,1–98,9% de uma CPU por intervalo, ante limite de uma CPU; Tracking 87,6–89,9%.
Memória abaixo dos limites. Timestamps inteiros e atraso do CSV periódico limitam
a precisão desses intervalos; prevalece o snapshot final drenado: 61,9253 req/s,
p95 240 ms. Esses números descrevem ingestão de warm-up, independentemente do
perfil posterior, e não são comparados diretamente ao throughput medido de mixed/4.
Não são limites novos de aprovação nem identificam causalidade exclusiva.

A observação posterior dos contêineres preservados confirmou imagens, recursos,
um worker e pool 5/overflow 0 por serviço, sem OOM ou reinício registrado.
Os bancos conciliaram 3.722 comandos, recibos e finalizações. Essa observação é
posterior à interrupção, não um snapshot histórico recuperado. O exit 143 do
contêiner loadgen decorre de sua parada posterior e é distinto do exit 2 do
processo Locust. Logs e parâmetros permanecem congelados.

O pacote novo é `results/reviewed-warmup-win9445-review-01`; sua prontidão exige
`ready.json`, checksums, fonte v1.1 congelada, imagens, host, preparação sem carga
e identidades separadas de aplicação, runner e coordenador. A fonte reutilizada
é `comparison-win9445-v11-source-01`, no SHA `948cefdf881b10af2c073be5536411daefb3faf2`.
O projeto novo é `fulfillflow-ii-warmup12-win9445-01`, porta local 18039.
O host é verificado contra o candidato exato antes da execução; divergência bloqueia.

Entrada da verificação já encerrada: `scripts/Invoke-V11WarmupDiagnostic.ps1`.
Não repetir sua preparação ou execução. O processo foi bloqueante, independente
do diretório corrente, com 300 s de estabilização e 60 s de admissão, além de
preparação, drain e exportação; sem measurement de 300 s. Destino preservado:
`results/reviewed-warmup-mixed-12-win9445-01-w1-v11`; diário:
`results/reviewed-warmup-win9445-execution-01`. Preserva evidências em sucesso e falha;
falha retém os recursos para revisão. Não repete nem sobrescreve destinos existentes.
O resultado usa `diagnostic_warmup_only`, `valid=false`, `matrix_eligible=false`,
`measurement_executed=false` e estados próprios de conclusão/validade do warm-up.

Interpretação fixada antes dessa execução:

- Falha equivalente: manter células de 12 usuários bloqueadas e propor a decisão
  necessária. Não repetir até passar.
- Sucesso: preservar a falha anterior e avaliar a divergência. Não declarar
  estabilidade nem retomar automaticamente a matriz.
- Defeito demonstrado: corrigir no escopo autorizado e avaliar identidades e
  impacto sobre as evidências.

A proposta anterior de doze diagnósticos continua inativa. Esta tentativa não
substitui nenhuma repetição oficial, não conclui a comparação e não autoriza release.
A existência de cópia independente das evidências continua sem confirmação.

## Compatibilidade do loadgen

O parent congelado é `sha256:f5b7118626bc3cf156029b9d31399bcba78013a035835db16815616c46bdd906`.
Seu `campaign.py` aceita somente v1.0 e app/postgres/loadgen. O locustfile não consulta
schemas SQL nem a topologia: consome o protocolo HTTP e as coortes do bundle.
O diff em `proposals/loadgen-v11.patch` foi apresentado e autorizado neste incremento.
A implementação final acrescenta schema v2, identidades dos dois proprietários e
validação de budgets/deadlines; schema v1 continua aceito com seu contrato original.

`Dockerfile.loadgen-v11` acrescenta somente `campaign.py` ao parent. O comando de build
verifica a identidade antes de criar uma nova referência local para `FROM`; Docker não
aceita o ID local escrito como `FROM sha256:...` nesse builder. Não se monta código por
cima da imagem nem se atribui release v1.0 à execução v1.1. O audit compara todos os
arquivos em `/work/benchmarks` e versões de todas as distribuições instaladas: somente
`campaign.py` pode diferir. Locustfile, dataset, dependências e comportamento do workload
permanecem idênticos. A nova imagem tem SHA próprio, registrado nos manifests e no audit.
Essa diferença de empacotamento/validação é declarada na comparação; não constitui uma
nova baseline medida nem elimina a necessidade da validação prévia autorizada em III.

## Ferramenta revisada e preparação sem carga

Após o bloco ABBA ter sido iniciado manualmente, somente A1 v1.0 foi executado.
Ele é inválido: a coleta obrigatória terminou após 78 ciclos e o runner não
realizou a conciliação final. B1, B2 e A2 não começaram. A causa específica da
exceção foi perdida pelo coletor congelado; o exit code 2 não a identifica.
O diagnóstico posterior ocioso concluiu 300 ciclos sem reproduzir a falha.
Isso não valida A1 nem comprova estabilidade sob carga.

A revisão autorizada mantém as aplicações congeladas e corrige diagnóstico,
supervisão e proveniência do runner de host. A identidade desse runner fica
separada da aplicação via `--application-source`; fórmulas, consultas, cadência,
workload e gates permanecem preservados. A prontidão de cada novo pacote é
comprovada por seu `ready.json`, checksums e identidade do runner. `-PlanOnly`
não comprova prontidão. Não repetir os comandos dos controles antigos nem
contar essa revisão como nova execução de carga.

Logs estruturados, Prometheus e OpenTelemetry previstos no DESIGN continuam
pendências de implementação herdadas. A decisão atual preserva a política
efetiva das imagens para a comparação; não declara conformidade integral de
observabilidade nem altera retroativamente os requisitos ou a publicação v1.0.

### Entradas anteriores e aceites — séries encerradas

O launcher das séries encerradas é `scripts/Invoke-ReviewedControls.ps1`. Ele exige PowerShell 7,
usa caminhos absolutos a partir de seu próprio diretório e preserva stdout/stderr
e o código do filho. O executável verificado neste host é:
`C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe`.

| Série | Estado | Pacote sob `results` | Resultado |
| --- | --- | --- | --- |
| Novo ABBA | Encerrada | `reviewed-abba-win9445-review-01` | Quatro válidas mixed/4 |
| Matriz atual | Interrompida | `reviewed-official-win9445-review-01` | Dez válidas mixed/4; primeira mixed/12 inválida no warm-up |

Acrescentar `-PlanOnly` no lugar de `-PrepareOnly` somente mostra a seleção.
A preparação ABBA verifica as duas fontes/imagens, os parsers congelados, a política
efetiva de logs, o estado inicial e o coletor revisado por 300 s ociosos em cada
topologia. Não inicia Locust. A matriz reutiliza as fontes verificadas e verifica
os doze candidatos. Dependências são instaladas somente pelo lock e cache local;
ausência de cache bloqueia, sem atualizar versões ou baixar imagens.

Os fontes novos são `comparison-win9445-{v10,v11}-source-01`, nos SHAs medidos
`ae15e0a…7265` e `948cefd…faf2`. O runner de host tem SHA, lock e hashes próprios.
Os projetos isolados são `fulfillflow-task08-prepare-e2e-reviewed01` e
`fulfillflow-ii-reviewed-win9445`, com portas 18037/18038. Projetos existentes são
recusados antes de registrar propriedade. Os containers ociosos da preparação são
removidos somente após exportação de diagnósticos bem-sucedida.

O comando manual do novo ABBA já foi executado; não o repetir.

Piso de 44 minutos de fases. Os destinos começam por `reviewed-abba-mixed-4-win9445-01-`
e terminam em `a1-v10`, `b1-v11`, `b2-v11`, `a2-v10`; o diário é
`reviewed-abba-win9445-execution-01`. Qualquer destino existente bloqueia repetição.
Falha para a sequência e preserva recursos, runtime parcial, diagnóstico primário
e eventuais falhas secundárias. O motivo e o caminho são mostrados no terminal.

A revisão usa `100*(B/A-1)` para req/s e `p95(B)-p95(A)` por par. Somente quatro
válidas com contrastes concordantes permitem avançar à revisão da matriz; isso
não prova equivalência estatística ou causalidade exclusiva. Os dois controles
antigos e o piloto não são membros do bloco novo.

A matriz tem 30 execuções novas por versão no build 26200.9445; preserva q430,
workload/dataset, 300/60/300 s, coleta de 1 s e os budgets por componente/total.
Ordem fixa: mixed/4 A→B; mixed/12 B→A; timeline/4 A→B; timeline/12 B→A;
ingestion/4 A→B; ingestion/12 B→A, cinco repetições por bloco. A=v1.0, B=v1.1.
O piso das fases é de 11 horas, além da preparação. A alternância não randomiza
a execução nem elimina efeito de ordem dentro da célula. A entrada oficial exige
novo ABBA completo, evidências íntegras e contrastes concordantes; nenhum comando
inicia a matriz como continuação automática do ABBA. Falha não repete válidas.

A auditoria funcional reutiliza testes PostgreSQL de HMAC, bytes, idempotência,
locks, falhas de commit, reentrega e ausência de SQL durante HTTP, além do E2E.
Não foi demonstrada regressão funcional que exija mudar a imagem do piloto.

| Invariante de §30 | Implementação inspecionada sob `src/fulfillflow` | Evidência executável sob `tests` |
| --- | --- | --- |
| Autenticar antes de interpretar; preservar bytes | `tracking/authentication.py`, `tracking/service.py` | `api/test_tracking.py` (pré-autenticação, Alpha/Beta e bytes), `api/test_service_contracts.py` (autenticação interna) |
| Identidade persistida do comando e recibo idempotente | `tracking/service.py`, `core/events.py` | `api/test_service_contracts.py` (resposta perdida, recibos concorrentes, conflitos) |
| Commits locais e recuperação por reentrega | `tracking/service.py`, `core/events.py` | `api/test_commit_boundaries.py` (interrupções em cada fronteira e peers indisponíveis) |
| Ordenação total e locks Shipment antes de Order | `core/events.py`, `shipments/service.py` | `api/test_tracking_concurrency.py` (eventos distintos, entregas finais e retomadas concorrentes) |
| Nenhum recurso SQL retido durante HTTP | `tracking/service.py`, `tracking/core_client.py`, `core/tracking_client.py` | `api/test_service_contracts.py::test_no_sql_checkout_crosses_http_and_correlation_survives_forwarding` |
| Bancos e acesso por proprietário; jornada completa | `tracking/base.py`, migrations por serviço e contratos públicos | `integration/test_service_databases.py`, `e2e/test_external_simulator_journey.py` |

Há registro de arquivo local da baseline, mas não confirmação documental de cópia
independente. Essa pendência de preservação permanece explícita.

As seções operacionais abaixo preservam o histórico das preparações anteriores;
seus comandos de execução encerrados não são instruções para uma nova tentativa.

## Política efetiva de logs — revisão sem carga

O contrato declarado nos manifests v1.0 e v1.1 é igual: `LOG_LEVEL=WARNING`,
`LOG_FORMAT=json`, tracing desligado e sampling zero. As duas imagens de aplicação foram
inspecionadas sem rede, sem iniciar servidor e sem carga: a v1.0 congelada
`sha256:0fd492…1fc1` e a imagem v1.1 do piloto `sha256:3f084f…b40` usam Uvicorn 0.52.4.
Em ambas, o entrypoint de imagem é `python -m fulfillflow`; o Compose de benchmark troca
o comando por `python -m uvicorn … --workers 1`, sem `--log-level`, `--log-config` ou
`--no-access-log`.

Isso comprova a política efetiva do servidor nos dois casos: `uvicorn.access` fica em
INFO, com formato texto padrão, no stdout; `uvicorn`/`uvicorn.error` ficam em INFO no
stderr. `access_log=true` e o argumento de nível é nulo. O driver Docker observado no
host atual é `json-file`; não há destino de arquivo ou coletor externo configurado pelo
Compose. O driver atual não demonstra o destino usado na baseline histórica.

Já `LOG_LEVEL` e `LOG_FORMAT` são settings validados e injetados pelo Compose, mas o
código e os entrypoints das duas imagens não instalam um configurador de logging que os
aplique ao logger da aplicação ou ao Uvicorn. Logo, eles são configuração declarada e
observável no ambiente, mas não prova de JSON/WARNING efetivo para logs de aplicação.
Não foram encontrados emissores estruturados da aplicação no caminho de benchmark. Os
access logs INFO existentes no diagnóstico do piloto são Uvicorn, não logs da aplicação.
Sem configurador, o root mantém WARNING e nenhum handler instalado; se houver emissão
nessa condição, o fallback padrão envia mensagem simples ao stderr. Isso descreve a
configuração inspecionada, sem afirmar que ocorreu emissão histórica da aplicação.
Não há exportação de `docker logs` da baseline v1.0 que prove sua saída histórica; a
equivalência é comprovada por imagens, Compose e entrypoints, não inferida da contagem de
linhas antiga.

A extração acrescenta requisições internas e, portanto, linhas de access log para esses
saltos. Esse é custo inerente do fluxo observado, não evidência de uma política diferente.
Sua contribuição isolada para throughput ou latência não foi medida. As inspeções estão
em `results/v10-controls-win9445-preparation-01/{v10,v11}-image-logging.json`.
Nenhum log será desativado, reformatado ou otimizado para novos controles.

## Preparação e restauração

`seed_v11` autentica o artefato e a sidecar, verifica seu replay semântico e o gerador,
e distribui os mesmos registros: Core recebe Orders, Shipments e Notifications;
Tracking recebe os 15.000 inboxes e eventos. O cadastro Alpha/Beta vem das migrações.
Os inboxes históricos importados mantêm `command=NULL`; o Core começa com **zero recibos**.
São eventos já finalizados: reentrega devolve a timeline original. Comandos e recibos
novos são produzidos exclusivamente pelas requisições v1.1, sem mudar IDs históricos.

Cada proprietário confirma sua transação separadamente. Uma falha parcial não declara
o par pronto. A repetição aceita somente a projeção inteira exata ou tabelas vazias,
nunca sobrescreve dados divergentes. Depois dos commits, ambos são verificados novamente.
Schema/head físicos são específicos por proprietário; o hash lógico segue o artefato.

`prepare_v11 restore` recria somente um projeto reclamado quando seus containers e volumes
estavam ausentes. O marcador de propriedade fica em `benchmarks/results/preparation`.
Projeto preexistente sem marcador é recusado, assim como projetos locais/v1.0. A confirmação
literal limita o alvo. A prontidão anterior é invalidada antes da restauração; na entrada
operacional, falhas preservam erro e diagnósticos antes de remover apenas recursos próprios.
Falha de exportação mantém os recursos para revisão. O relatório `.ready.json` é escrito somente ao final,
com hashes, contagens e duração. O budget total continua 120 s, sem extensão silenciosa.

## Comandos PowerShell de preparação (sem carga)

Os valores do arquivo de exemplo são sintéticos e destinados apenas à infraestrutura isolada.
Execute a partir da raiz, com o parent congelado disponível localmente e o fonte revisado
commitado. `uv sync --frozen` conserva o lock. Os dois serviços podem usar a mesma imagem
de código com configurações distintas; cada manifest registra o digest observado de cada um.

```powershell
uv sync --frozen
Get-Content .env.benchmark-v11.example |
  Where-Object { $_ -and -not $_.StartsWith('#') } |
  ForEach-Object {
    $pair = $_.Split('=', 2)
    [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'Process')
  }
$revision = git rev-parse HEAD
docker build --target runtime --label "org.opencontainers.image.revision=$revision" -t fulfillflow-core:v11-candidate .
docker tag fulfillflow-core:v11-candidate fulfillflow-tracking:v11-candidate
uv run python -m benchmarks.prepare_v11 build-loadgen
uv run python -m benchmarks.prepare_v11 restore --project fulfillflow-benchmark-v11 --confirm-project fulfillflow-benchmark-v11 --with-loadgen
uv run python -m benchmarks.prepare_v11 manifest --project fulfillflow-benchmark-v11 --destination benchmarks/results/v11-review --audit-report benchmarks/results/v11-review-02e98a9/loadgen-compatibility.json
```

O container loadgen fica ocioso; o init apenas importa Locust. Nenhum comando acima invoca
o workload. `manifest` exige fonte limpo, labels das imagens correspondentes ao HEAD,
audit do loadgen, schemas reais, dataset íntegro e recursos/configurações observados.
O destino deve ser novo. Um erro deixa `.incomplete.json`, e nunca libera execução.
`--audit-report` reutiliza a auditoria concluída de imagens imutáveis: confere checksum,
parent/candidata e somente o hash do `campaign.py` atual dentro da imagem. Não repete a
comparação inteira de arquivos/distribuições. A evidência histórica `validation.json`
continua vinculada ao commit nela registrado; não representa automaticamente um novo HEAD.
Os três manifests candidatos são derivados dos oficiais v1.0, com o mesmo host esperado,
perfis, cargas, coortes, pesos, tempos e parâmetros comparativos. Eles não atualizam
expectativas do host para acomodar divergências. O HostProbe completo continua obrigatório
antes de qualquer execução; não é acionado como substituto de preparação do host neste chat.

Após revisão, qualquer novo commit requer reconstrução identificada das imagens e novos
manifests. Não editar SHA manualmente. Para remover apenas o projeto preparado:

```powershell
uv run python -m benchmarks.prepare_v11 cleanup --project fulfillflow-benchmark-v11 --confirm-project fulfillflow-benchmark-v11
```

Para uma campanha **somente após autorização em III**, o runner existente aceita os manifests
v2. O `--base-url` visto pelo loadgen é `http://core:8000`. O argv de preparação é o comando
`prepare_v11 restore` acima, com `--with-loadgen` e `--manifest` apontando ao manifest do perfil.
Reutilizar esse argv em `--prepare-command-json` e a confirmação literal da campanha;
usar sempre um destino novo. A entrada operacional abaixo monta esses argumentos, sem
alterar os gates do runner. Preparar o pacote não autoriza executar essa entrada sem `-PlanOnly`.

## Entrada operacional do piloto — registro da preparação no chat 11

O piloto no build atual já foi executado posteriormente, conforme o estado no início
deste documento. Os comandos desta seção são históricos; não repetir destinos consumidos.

`scripts/Invoke-V11Pilot.ps1` exige PowerShell 7 e a `.venv` sincronizada pelo lock. Resolve
caminhos relativos ao diretório do chamador; transmite JSON UTF-8 via stdin ao módulo
`benchmarks.pilot_v11`, sem `Invoke-Expression`, shell intermediário ou escape manual de argv.
Carrega o arquivo de ambiente sintético e restaura o ambiente do chamador ao terminar.
Use o caminho do pacote materializado no HEAD revisado, indicado no relatório final do chat.

```powershell
pwsh -NoProfile -File .\scripts\Invoke-V11Pilot.ps1 `
  -Candidate .\benchmarks\results\v11-chat11-review\v11-candidate-mixed.json `
  -Audit .\benchmarks\results\v11-chat11-review\loadgen-compatibility.json `
  -Destination .\benchmarks\results\v11-pilot-mixed-4-q430-attempt-01 `
  -PlanOnly
```

`-PlanOnly` valida a derivação e exibe o JSON/argv; não consulta o host, cria infraestrutura
ou executa carga. Sua aprovação **não** equivale à aprovação do preflight real. Após
autorização específica no chat 12, remover apenas `-PlanOnly` executa uma única tentativa.
O destino deve estar ausente. A entrada deriva mixed/4, q=430, uma repetição não oficial;
pesos, coortes, spawn rate, budgets, deadlines, estabilização, warm-up, measurement e
critérios de validade permanecem congelados. Não há repetição automática nem promoção a oficial.

O preflight bloqueia fonte sujo, branch/SHA divergentes, candidato com checksum incorreto,
imagens/revisões ausentes, auditoria incompatível, projeto preexistente e divergências do
host/energia. Todas as expectativas de host são preservadas, mesmo no piloto não oficial.
Os digests do manifest são usados na preparação, sem depender de tags mutáveis. O runner
repete seus gates de Docker, bancos, estado inicial e host antes de iniciar warm-up.

Evidências por tentativa: `pilot-manifest.json`, `preparation-argv.json`, `preflight.json`,
`result.json`, `run/` (inclusive `.partial` em falha), `preparation/error.json` quando houver,
`diagnostics/` e `checksums.sha256`. Os diagnósticos incluem logs sanitizados e cópia dos
artefatos do loadgen, mesmo quando uma fase falha antes da exportação normal. Não apagar
tentativa inválida. Falha de exportação impede cleanup e exige revisão dos recursos próprios.
Erros de cleanup são secundários ao erro original. Saídas: 0 para conclusão válida;
2 para recusa/falha operacional; 130 para interrupção tratada. Interrupção forçada do
processo/host pode impedir o relatório final; preservar destino e recursos antes de retomada.

Estimativa de uma tentativa: piso de **11 min** (300+60+300 s), mais preparação, drain,
checagens e exportações. Reservar **15–20 min**, além de correções do host antes do preflight;
isso não amplia nenhum timeout. Uma recusa inicial encerra antes da carga.

## Budgets, telemetria e conciliação

Core e Tracking: cada um 1 CPU/768 MiB/um worker/pool 5/overflow 0. Total 2 CPUs/1536 MiB/10
conexões. PostgreSQL compartilhado: 2 CPUs/2560 MiB; loadgen: 2 CPUs/1536 MiB. Pool e SQL
continuam em 5 s e 5000 ms. Logging WARNING/json, tracing desligado e sampling 0 permanecem.
O probe verifica identidades, proprietário da URL SQL, limites de cada processo e agregado.

HTTPX tem limites por operação de I/O. Um deadline assíncrono agora limita também a chamada
inteira: no Compose experimental, Tracking→Core 6 s e Core→Tracking 8 s, dentro dos 10 s
de request/drain externos. Defaults funcionais 10/30 s permanecem fora do benchmark.
Timeout não prova rollback remoto, não cancela atomicamente serviços e pode deixar RECEIVED;
continua exigindo reentrega. Um erro/timeout invalida a repetição pelas regras congeladas.

`resources.csv` mantém ciclos completos de Core, Tracking, PostgreSQL e loadgen a cada 1 s;
conexões são observadas por banco e somadas. `resources.application.csv` soma CPU/memória
dos dois serviços por timestamp, sem inventar um container agregado. Shared PostgreSQL não
permite atribuir CPU/memória do banco a um proprietário. As leituras completas de conciliação
acontecem fora da janela medida, depois de drain, sem SQL cruzado no produto.

O runner conserva quota/coortes/verificação de warm-up, exportação final, checksums e gates
HTTP/Locust. Acrescenta deltas de recibos e `reconciliation.json`: comando, hash, recibo,
resultado original, evento e notificação devem concordar após HTTP 200. Não há promessa de
snapshot SQL global nem recovery sem reentrega. Falhas deixam a repetição incompleta.

## Duração e limites de aceite

### Candidato exclusivo do piloto no Windows 26200.9445

Foi autorizada a geração, sem execução, de um novo candidato não oficial no build
`26200.9445`. A baseline `26200.9278` e seus manifests/resultados continuam históricos
e imutáveis. `prepare_v11 manifest --pilot-windows-26200-9445` usa o fluxo de identidades
observadas, produz somente mixed/4, q=430, uma repetição, e grava `environment-decision.json`
com a referência/hash da baseline e a divergência explícita. A opção não consulta o OS
para escolher a expectativa; qualquer build diferente continuará bloqueado pelo comparador.

Pacote novo: `benchmarks/results/v11-pilot-win9445-review`. Arquivo candidato:
`v11-pilot-mixed-4-q430-win-26200-9445.json`. A evidência do preflight completo fica no
mesmo pacote. O launcher aceita essa decisão delimitada, preservando todos os outros
parâmetros. Uma consulta Docker vazia só confirma zero containers quando retorna sucesso;
erro, saída inválida ou containers esperados ausentes continuam bloqueando o preflight.

Comando manual, **somente após autorização da execução**, a partir da raiz:

```powershell
pwsh -NoProfile -File .\scripts\Invoke-V11Pilot.ps1 `
  -Candidate .\benchmarks\results\v11-pilot-win9445-review\v11-pilot-mixed-4-q430-win-26200-9445.json `
  -Audit .\benchmarks\results\v11-pilot-win9445-review\loadgen-compatibility.json `
  -Destination .\benchmarks\results\v11-pilot-win9445-attempt-01
```

Acrescentar `-PlanOnly` verifica o plano sem carga. O pacote anterior continua reproduzível
e recusará o build atual. O launcher exige `pwsh` 7; registrar versão e caminho observados
no pacote. Um preflight aprovado é uma observação, não dispensa os gates na execução futura.

### Controles v1.0 no Windows 26200.9445 — executados e verificados

Os dois controles foram executados manualmente e concluídos com validade confirmada.
A análise derivada está em `results/v10-controls-win9445-analysis-01/metrics-summary.json`,
com script de conferência e checksums próprios. Os resultados originais não foram editados.

| Referência mixed/4 | req/s Locust | p95 | Falhas | Requisições medidas |
| --- | ---: | ---: | ---: | ---: |
| Mediana histórica v1.0, Windows 26200.9278, n=5 | 185,9261 | 36 ms | 0 | — |
| Controle v1.0 atual 01 | 190,0128 | 35 ms | 0 | 57.003 |
| Controle v1.0 atual 02 | 189,8441 | 35 ms | 0 | 56.954 |
| Piloto v1.1 anterior, Windows 26200.9445, n=1 | 108,0411 | 82 ms | 0 | 32.412 |

Verificação dos controles: `complete=true`, saída 0, metadados válidos/não oficiais,
fonte e lock v1.0, candidatos iguais à derivação autorizada, imagens/configurações,
identidade e condições dinâmicas do host, duração das fases, ausência de marcadores de
incompletude e CSVs de falhas/exceções vazios. Foram autenticados 47 registros de checksum
por controle e novamente 68 do piloto. Os validadores congelados conferiram os arquivos
obrigatórios e ciclos completos de recursos (300 ciclos medidos por controle).
Warm-up: 1.720 eventos aplicados em cada controle. Measurement: 17.132 e 17.092 eventos
aplicados, respectivamente, concordando entre HTTP e os deltas de inbox, eventos e
notificações. As demais contagens permaneceram estáveis; cleanup foi concluído.
A revisão posterior usa evidências exportadas, sem recriar bancos para reconsultá-los.

Aplicação das regras registradas no commit `4a777ef`, antes dessas execuções:
throughput +2,1980% e +2,1073% em relação à mediana histórica, p95 −1 ms em ambos.
Diferença entre controles: 0,08883% da média e 0 ms no p95. Portanto, ambos são válidos,
estáveis e estão dentro de todas as margens práticas previamente declaradas.
Isso torna plausível a referência v1.0 no host atual; não demonstra equivalência estatística.

A média dos controles é 189,9285 req/s. O piloto v1.1 fica 43,1149% abaixo dessa referência,
com p95 47 ms maior. As maiores diferenças descritivas por rota estão em timeline
(p95 17 → 37 ms) e webhook (40 → 92 ms); detalhe de Shipment fica em 14–15 → 16 ms e
listagem em 17–18 → 20 ms. A CPU média da aplicação v1.0 ficou em 98,24%/98,56% de um núcleo;
no piloto, Core/Tracking registraram 85,41%/60,97%, respectivamente. Essas observações são
compatíveis com custo adicional no fluxo distribuído, mas não isolam HTTP, SQL, recibos,
partição de recursos ou logs. O piloto ocorreu em outro horário e não forma pares temporais
com estes controles. Os dados não sustentam atribuir toda a diferença ao Windows ou à
arquitetura, nem alterar a aplicação ou observabilidade para aproximar métricas.

#### Registro histórico do primeiro bloco ABBA — encerrado

Antes de ampliar para a matriz oficial, foi autorizada a preparação para verificar repetibilidade da diferença
com **dois blocos pareados exploratórios**, quatro novas execuções não oficiais mixed/4:

| Ordem fixa | Bloco | Versão |
| ---: | ---: | --- |
| 1 | 1 | v1.0 (A1) |
| 2 | 1 | v1.1 (B1) |
| 3 | 2 | v1.1 (B2) |
| 4 | 2 | v1.0 (A2) |

Dois pares em ordens opostas são a menor proposta que repete o contraste sem deixar uma
versão sempre em primeiro lugar. Isso reduz um confundimento de ordem; não é randomização
nem dimensionamento estatístico. Os dois controles atuais e o piloto não serão renomeados,
reclassificados ou usados como membros desses novos pares.

Preservar imagens originais, código medido v1.0 `ae15e0a…7265` e imagem/código do piloto
v1.1 `948cefd…faf2`, em fontes isolados, com Windows 26200.9445 explícito em candidatos novos.
Dataset, workload, q=430, spawn rate, divisão/total de recursos, workers/pools e telemetria
ficam congelados. Restaurar e verificar cada estado inicial; repetir 300 s de estabilização,
60 s de warm-up e 300 s medidos, com os mesmos deadlines, gates e conciliação por versão.
Entrada bloqueante, destinos novos para cada uma das quatro execuções, exportação de
diagnósticos e parada na primeira falha, sem repetir automaticamente. Piso de 44 minutos
de fases; reservar aproximadamente uma hora a uma hora e vinte para a execução completa.

Leitura fixada antes da execução: calcular, em cada bloco, `100 × (B/A − 1)` para req/s e
`p95(B) − p95(A)` em ms; mostrar os quatro resultados individuais e os dois contrastes.
Todos os gates devem passar. Se ambos os contrastes mantiverem menor throughput e maior
p95 na v1.1, haverá repetição direcional do custo nessa configuração e poderá ser proposta
a campanha oficial com uma decisão própria de referência no host atual. Direções divergentes
ou qualquer invalidez tornam a etapa inconclusiva e pedem diagnóstico. Não haverá p-valor,
declaração de equivalência, nova margem ajustada aos resultados ou otimização para favorecer
uma versão. Mesmo dois contrastes concordantes não estabelecem causalidade exclusiva.

Esta proposta amplia o desenho além dos dois controles originalmente autorizados. Após
apresentação específica, o usuário autorizou sua preparação cuidadosa, sem execução pelo
agente. A mudança do Windows para toda a matriz oficial e a publicação da release continuam
decisões separadas; a proposta histórica de 12 diagnósticos segue inativa. Os quatro novos
controles também não substituem as seis células oficiais nem mudam as margens dos dois
controles anteriores.

A entrada `scripts/Invoke-PairedControls.ps1` separa `-PlanOnly` (apenas mostra o plano),
`-PrepareOnly` (prepara e verifica sem carga) e execução manual sem switches. A preparação
usa checkouts destacados `results/paired-win9445-v10-source-01` e
`results/paired-win9445-v11-source-01`, nos commits medidos de cada versão. Cada fonte tem
sua própria `.venv` com lock congelado; runner, parser, seeds e probes são os originais.
Não há alteração ou desvio dos validadores. Os candidatos ficam fora desses checkouts,
em `results/paired-win9445-review-01/candidates`, mantendo ambos os fontes limpos.

| Etapa | Novo destino sob `benchmarks/results` |
| --- | --- |
| A1 | `paired-mixed-4-win9445-01-a1-v10` |
| B1 | `paired-mixed-4-win9445-02-b1-v11` |
| B2 | `paired-mixed-4-win9445-03-b2-v11` |
| A2 | `paired-mixed-4-win9445-04-a2-v10` |

O pacote registra hashes dos scripts/candidatos, identidades de imagens/host/PowerShell,
validação dos quatro manifests, auditoria da imagem derivada e verificação do estado inicial
de cada versão sem iniciar fases de carga. O projeto v1.0 continua `fulfillflow-benchmark`;
o v1.1 novo é `fulfillflow-ii-paired-win9445`. A entrada recusa qualquer recurso preexistente
desses projetos antes de registrar propriedade. Usa imagens locais verificadas, sem pull
ou build. Após a verificação ociosa, exporta diagnósticos e remove somente os recursos
isolados que criou. Na execução real, repete preparação/restauração e os gates congelados;
a verificação ociosa não antecipa a aprovação da admissão dinâmica ou da conciliação final.

Qualquer destino de execução ou diário `results/paired-win9445-execution-01` existente
bloqueia repetição. Checksums do pacote, alterações nos scripts/candidatos, checkout sujo
ou ambiente isolado divergente também bloqueiam carga. Na primeira falha, interrompe a
sequência e para o loadgen; preserva o erro primário, diagnósticos e recursos para revisão.
Após sucesso exporta o runtime antes da limpeza. Ctrl+C concede o prazo de finalização
ao runner congelado; encerramento forçado do host ainda exige inspeção manual.

Comando manual, somente depois da prontidão confirmada em `paired-win9445-review-01/ready.json`:

```powershell
& 'C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe' -NoProfile -File 'C:\Projetos\campos-labs\fulfillflow\scripts\Invoke-PairedControls.ps1'
```

O caminho é absoluto e funciona a partir de `C:\Users\natoc`. Executar uma vez, manter o
terminal aberto e não iniciar outra carga simultânea. O comando antigo dos dois controles
v1.0 já concluídos, preservado no registro abaixo, não deve ser repetido.

Prontidão operacional verificada em 10/09/2026: `-PrepareOnly` terminou com código 0,
chamado pelo caminho absoluto a partir de `C:\Users\natoc`, usando PowerShell 7.6.5.
Os quatro parsers aceitaram os candidatos; as imagens originais e o host conferiram.
O seed/restauração e os probes originais verificaram PostgreSQL v1.0 e ambos os bancos
v1.1 com o hash lógico `5897d744…a63bf`. Os dois projetos foram removidos após exportar
diagnósticos. Os 42 registros de checksum do novo pacote e o bloqueio de prontidão foram
conferidos; os quatro destinos de execução e o diário permanecem ausentes.

Validação do código: suíte unitária completa local aprovada (600 testes nessa passagem),
seguida da suíte focal final com 32 testes, incluindo os dois novos casos de preparação
e a execução real do launcher PowerShell com processo filho simulado, sem carga.
Ruff, formatação, Mypy e os dez contratos de imports passaram. A CI do commit entregue
registra a validação integrada. Nenhum código de aplicação, imagem, lock, política de logs
ou evidência histórica foi alterado; DESIGN §30.5 registra somente a extensão autorizada.

#### Registro da preparação e das regras anteriores à execução

A autorização original delimitou somente dois controles não oficiais v1.0,
mixed/4, q=430, uma repetição cada, no build `26200.9445`. A preparação cria um checkout
destacado no commit efetivamente medido `ae15e0a…7265`, usa os executáveis e imagens v1.0
originais, cria destinos novos e interrompe a sequência na primeira recusa, falha ou
interrupção. Ela não faz pull, build, tag, atualização do lock ou repetição automática.
Cada falha exporta diagnósticos antes de preservar a infraestrutura isolada para revisão.

#### Auditoria das falhas e da prontidão anteriormente declarada

O código 2 é uma saída operacional genérica, não um resultado de benchmark. A revisão dos
commits `b01ba0e`, `e9ceb7c` e `d81f762` e dos arquivos preservados identificou:

| Destino preservado | Causa comprovada |
| --- | --- |
| `v10-controls-win9445-source` | Sync tentou buscar Ruff 0.16.5 no cache padrão incompleto e falhou por DNS. A hipótese anterior de fonte sujo não era a causa registrada. |
| `v10-controls-win9445-source-02` / `bootstrap-02` | Sync offline não encontrou psycopg-binary 3.3.4. |
| `v10-controls-win9445-source-03` / `bootstrap-03` | Comparação textual de `C:/…` retornado por Git com `C:\…` recusou um checkout independente válido. |
| `v10-controls-win9445-source-04` / `bootstrap-04` | Na revisão sem carga, o cache do repositório também se mostrou parcial: faltava Jinja2 3.1.6. |
| `v10-controls-win9445-source-05` / `bootstrap-05` | As 85 dependências congeladas foram instaladas, mas o manifest publicado não existe ainda no commit medido. |

Todos esses bootstraps pararam antes de iniciar containers ou carga. Naquele momento os
destinos dos dois controles estavam ausentes. Os testes anteriores simulavam as partes que falharam, e
`-PlanOnly` verificava apenas commit/destinos: nem esses testes nem a CI Linux demonstravam
prontidão operacional no Windows. A instrução inicial com `-File .\scripts\…` também dependia
do diretório corrente e falhava quando chamada de `C:\Users\natoc`; o comando abaixo é absoluto.

Outros defeitos corrigidos antes da carga: a consulta `HostProbe.dynamic({})` antecedia a
infraestrutura e recusaria containers vazios no probe congelado; agora a admissão dinâmica
permanece exclusivamente no runner original, depois da preparação/estabilização e com IDs
reais. O fallback para a `.venv` ativa da v1.1 foi retirado. O subprocesso compartilha a
saída do PowerShell e informa causa/arquivo de diagnóstico antes da mensagem de código 2.
Em Ctrl+C, o supervisor concede até 90 s à finalização do runner antes de considerar kill;
falhas também param o loadgen do projeto cuja criação foi registrada nesta tentativa,
preservando containers/volumes e impedindo o segundo controle. Isso não altera tempos das
fases válidas. Encerramento forçado do host pode impedir finalização e requer inspeção.

#### Pacote verificado sem carga antes da execução manual

`v10-controls-win9445-source-06` contém o código medido `ae15e0a…7265`, em HEAD destacado,
e sua própria `.venv`, sincronizada com `uv sync --frozen --all-groups`. O cache local é
explícito; apenas artefatos das versões congeladas faltantes podem ser baixados durante
preparação. Nenhum lock, Python, dependência do ambiente ativo ou imagem foi atualizado.
O Python ativo serve apenas ao wrapper; runner, probes, seed e aplicação usam fonte/imagens
v1.0. Não há exclusão manual de dependências ou substituição por módulos v1.1 no runner.

O manifest publicado é lido pelo blob imutável `fe0fd0fa46241cade00086091158cdba3a86f6fd`,
presente no tag v1.0.0 (`6235f6c…a871`), pois sua publicação sucedeu o commit medido. Não se
copia esse arquivo para alterar o checkout congelado. Somente os dois novos candidatos
aparecem como arquivos não rastreados nesse checkout.

`v10-controls-win9445-bootstrap-06/ready.json` registra a validação real dos dois candidatos
pelo parser v1.0, replay/hash do dataset, release/metadados/imports da `.venv` isolada,
identidades das imagens originais, Compose e identidade do host. Naquela preparação nenhum
container foi iniciado e nenhuma fase medida foi executada. Os hashes do wrapper e dos candidatos são
conferidos novamente antes da carga, junto de `uv sync --frozen --offline --check`.
Mudanças posteriores no pacote bloqueiam execução. Essa preparação não antecipa a aprovação
dos gates dinâmicos, dos bancos ou da conciliação que só podem ocorrer na tentativa real.

Validação da revisão anterior dos dois controles v1.0: suíte unitária local (569 aprovados), regressões finais do controle
(24 aprovadas, incluindo Git e PowerShell reais no Windows), Ruff, formatação, Mypy e os
dez contratos de imports aprovados. O Mypy exclui somente `benchmarks/results`, onde os
checkouts históricos preservados geravam colisão de módulos; o código mantido continua
sob as mesmas regras estritas. Os 75 registros de checksum da baseline e do piloto e os
nove registros do bootstrap atual foram conferidos sem divergências. A CI do commit final
é registrada no relatório daquela entrega. Não houve alteração de DESIGN, aplicação ou política
de observabilidade, nem execução local de carga, migração ou build de imagem.

Os candidatos mudam exclusivamente: nome não oficial, seleção de 4 usuários, uma
repetição, expectativa explícita do build Windows e o caminho relativo do mesmo artefato
de dataset. Workload, coortes, pesos, q, spawn rate, imagens, recursos, pool, tempos,
tracing e telemetria declarada ficam iguais aos manifestos v1.0 publicados. As identidades
observadas do host, o caminho e a versão do PowerShell são gravados em cada tentativa.

As regras de leitura foram fixadas antes de medir. Primeiro, ambas precisam ser válidas:
gates do runner, HTTP/Locust sem erros, efeitos e estado inicial/final conferidos,
completude e checksums. Para triagem descritiva, cada throughput deve ficar a até 5% da
mediana histórica v1.0 mixed/4 (185,93 req/s) e cada p95 a até 5 ms da mediana histórica
(36 ms); entre os dois controles, a diferença absoluta de throughput deve ser no máximo
5% de sua média e a de p95, no máximo 5 ms. Os 5% arredondam para cima duas vezes o IQR
relativo histórico de throughput (4,45%); 5 ms também é maior que a faixa histórica
35–37 ms. São margens práticas prévias, não testes de significância nem prova de
equivalência.

Dois controles válidos e estáveis dentro dessas margens tornam plausível uma referência
v1.0 no host novo: a diferença do piloto v1.1 continua descritiva e não causal, mas a
decisão seguinte pode propor a menor amostra pareada que responda à dúvida restante.
Se ambos forem válidos, estáveis e fora das margens, o host/versão passa a ser explicação
plausível e será necessário decidir uma referência v1.0 atual maior antes de atribuir
diferença à extração. Instabilidade entre eles ou qualquer invalidez pede diagnóstico; não
dispara nova tentativa. Se apenas um cruzar uma margem ou os indicadores divergirem,
a triagem é inconclusiva e exige revisão, sem execução adicional automática. Esses dois
resultados não validam equivalência nem as seis células.

Comando histórico dos dois controles já concluídos; **não executar novamente**. Usa o executável conferido neste terminal
(PowerShell 7.6.5). Funciona também a partir de `C:\Users\natoc`, em uma linha:

```powershell
& 'C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe' -NoProfile -File 'C:\Projetos\campos-labs\fulfillflow\scripts\Invoke-V10Controls.ps1'
```

`-PlanOnly` só exibe o plano. `-PrepareOnly` materializa e verifica sem carga, mas já foi
executado neste pacote: não repeti-lo. A entrada normal exige `ready.json` válido e não
refaz preparação nem escolhe novos destinos automaticamente. Cada controle leva pelo
menos 11 minutos nas fases temporizadas; reservar aproximadamente 30–40 minutos para os
dois. Uma saída rápida não significa dois controles concluídos: conferir ambos os
`result.json` com `complete=true`, artefatos válidos e checksums. Em qualquer falha, preservar
os destinos e revisar o diagnóstico indicado; não executar novamente às cegas.

### Proposta histórica de 12 diagnósticos — não é a próxima ação

A proposta anterior de 12 diagnósticos, duas execuções por célula dos três perfis × 4/12
users, permanece apenas como registro histórico. Ela não está incluída na entrada atual,
não é iniciada pelos dois controles e não foi autorizada. Uma expansão futura depende dos
resultados acima e de decisão específica.

Matriz futura: três perfis × 4/12 users × cinco repetições = 30 válidas. O piso temporal é
30 × (300 s estabilização + 60 s warm-up + 300 s measurement) = **5 h 30 min**, mais preparo,
drain, verificações e exportações. O teto configurado das esperas principais, sem exportação,
é aproximadamente 7 h 30 min (120+300+60 de partida+90+330 s por repetição); isso não é uma
previsão de desempenho. Execuções inválidas não contam como oficiais e exigem diagnóstico.

Os testes do incremento I continuam válidos. II acrescenta falhas antes/depois dos commits,
perda de rejeição, indisponibilidade dos peers, concorrência com rollback/resposta perdida,
deadlines e preparação parcial. A validação funcional não substitui ensaio de carga. A
preparação do incremento II não executou calibração ou measurement; o piloto e os controles
posteriores estão distinguidos acima. Não houve merge, tag ou release v1.1.
