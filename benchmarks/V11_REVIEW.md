# Incremento II — pacote de revisão

Este pacote prepara a comparação sem executar Locust, calibração, warm-up ou measurement.
Os resultados v1.0, seu dataset e manifests publicados permanecem imutáveis. Não há
resultado de desempenho v1.1. A revisão e a preparação do host antecedem o incremento III.

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
Não há exportação de `docker logs` da baseline v1.0 que prove sua saída histórica; a
equivalência é comprovada por imagens, Compose e entrypoints, não inferida da contagem de
linhas antiga.

A extração acrescenta requisições internas e, portanto, linhas de access log para esses
saltos. Esse é custo inerente do fluxo observado, não evidência de uma política diferente.
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

## Entrada operacional do piloto — chat 11, execução pendente

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

### Controles atuais v1.0 no Windows 26200.9445 — preparados, execução pendente

Esta é a proposta atual autorizada: somente dois controles não oficiais v1.0,
mixed/4, q=430, uma repetição cada, no build `26200.9445`. A entrada cria um checkout
destacado no commit efetivamente medido `ae15e0a…7265`, usa os executáveis e imagens v1.0
originais, cria destinos novos e interrompe a sequência na primeira recusa, falha ou
interrupção. Ela não faz pull, build, tag, atualização do lock ou repetição automática.
Cada falha exporta diagnósticos antes de preservar a infraestrutura isolada para revisão.

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
dispara nova tentativa. Esses dois resultados não validam equivalência nem as seis células.

Comando manual, somente após autorização de execução, a partir da raiz e usando o
executável conferido neste terminal:

```powershell
& 'C:\Users\natoc\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe' `
  -NoProfile -File .\scripts\Invoke-V10Controls.ps1
```

Acrescentar `-PlanOnly` só confirma commit e destinos; não materializa checkout, consulta
o host, cria infraestrutura ou executa carga. A execução normal materializa o ambiente
isolado pelo lock v1.0, depois inicia no máximo os dois controles autorizados.

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
