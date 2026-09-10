# Incremento II — pacote de revisão

Os incrementos I e II estão concluídos. O piloto posterior não oficial v1.1 mixed/4,
q=430, no Windows 26200.9445 foi válido: 108,04 req/s, p95 82 ms, sem erros e com
conciliação. Evidências: `results/v11-pilot-win9445-attempt-01` e
`results/v11-pilot-win9445-analysis/metrics-summary.json`.
A campanha oficial do incremento III permanece pendente. A revisão atual prepara apenas
dois controles exploratórios v1.0, sem executar carga. A baseline publicada em 26200.9278,
seu dataset e manifests, e as evidências do piloto permanecem imutáveis.

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

### Controles atuais v1.0 no Windows 26200.9445 — preparação verificada, carga pendente

Esta é a proposta atual autorizada: somente dois controles não oficiais v1.0,
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

Todos esses bootstraps pararam antes de iniciar containers ou carga. Os destinos dos dois
controles continuam ausentes. Os testes anteriores simulavam as partes que falharam, e
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

#### Pacote atualmente verificado sem carga

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
identidades das imagens originais, Compose e identidade do host. Nenhum container foi
iniciado e nenhuma fase medida foi executada. Os hashes do wrapper e dos candidatos são
conferidos novamente antes da carga, junto de `uv sync --frozen --offline --check`.
Mudanças posteriores no pacote bloqueiam execução. Essa preparação não antecipa a aprovação
dos gates dinâmicos, dos bancos ou da conciliação que só podem ocorrer na tentativa real.

Validação desta revisão: suíte unitária local (569 aprovados), regressões finais do controle
(24 aprovadas, incluindo Git e PowerShell reais no Windows), Ruff, formatação, Mypy e os
dez contratos de imports aprovados. O Mypy exclui somente `benchmarks/results`, onde os
checkouts históricos preservados geravam colisão de módulos; o código mantido continua
sob as mesmas regras estritas. Os 75 registros de checksum da baseline e do piloto e os
nove registros do bootstrap atual foram conferidos sem divergências. A CI do commit final
é registrada no relatório de entrega. Não houve alteração de DESIGN, aplicação ou política
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

Comando manual dos dois controles preparados, usando o executável conferido neste terminal
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
deadlines e preparação parcial. A validação funcional não substitui ensaio de carga. Não houve
calibração, measurement, merge, tag ou release neste incremento.
