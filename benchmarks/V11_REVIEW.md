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

### Proposta delimitada antes da comparação oficial — ainda não autorizada

Os dados existentes sustentam a descrição da v1.0 no build antigo e os testes funcionais
de consistência. Uma comparação com v1.1 no build novo deve declarar a mudança como fator
de confusão: não permite atribuir a diferença de desempenho somente à extração. Um piloto
v1.1 válido também não estima o efeito da atualização sobre v1.0 nem prova efeito nulo.

Para sustentar uma comparação no host atual, proponho revalidação delimitada de v1.0:
inicialmente **12 execuções diagnósticas**, duas por célula dos três perfis × 4/12 users,
com imagens/lock/dataset/workload e tempos v1.0 preservados, apenas a expectativa explícita
do novo build e identidades próprias em destino novo. Isso cobre toda a matriz com um
escopo menor que repetir automaticamente as 30 oficiais. Duas repetições são triagem;
não bastam para demonstrar equivalência ou efeito nulo.

Antes da triagem, acordar margens de relevância prática para throughput, latências e
recursos, usando a dispersão histórica como referência, sem escolher limites depois de
ver resultados. Verificar primeiro validade HTTP/efeitos, completude e condições do host.
Diferenças sistemáticas ou resultados inconclusivos justificam ampliar a amostra nas
células afetadas e estabelecer referência v1.0 pareada no novo host antes de alegações
quantitativas. Se não houver revalidação, restringir conclusões a comparação descritiva
entre ambientes, explicitando a impossibilidade de separar seus efeitos. A quantidade
final e eventual necessidade de uma nova baseline oficial serão decididas com essas
evidências; nenhuma repetição histórica é sobrescrita nem 30 novas execuções presumidas.

Matriz futura: três perfis × 4/12 users × cinco repetições = 30 válidas. O piso temporal é
30 × (300 s estabilização + 60 s warm-up + 300 s measurement) = **5 h 30 min**, mais preparo,
drain, verificações e exportações. O teto configurado das esperas principais, sem exportação,
é aproximadamente 7 h 30 min (120+300+60 de partida+90+330 s por repetição); isso não é uma
previsão de desempenho. Execuções inválidas não contam como oficiais e exigem diagnóstico.

Os testes do incremento I continuam válidos. II acrescenta falhas antes/depois dos commits,
perda de rejeição, indisponibilidade dos peers, concorrência com rollback/resposta perdida,
deadlines e preparação parcial. A validação funcional não substitui ensaio de carga. Não houve
calibração, measurement, merge, tag ou release neste incremento.
