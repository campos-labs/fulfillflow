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
literal limita o alvo. A prontidão anterior é invalidada antes da restauração; falhas removem
apenas os recursos desse projeto. O relatório `.ready.json` é escrito somente ao final,
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
uv run python -m benchmarks.prepare_v11 manifest --project fulfillflow-benchmark-v11 --destination benchmarks/results/v11-review
```

O container loadgen fica ocioso; o init apenas importa Locust. Nenhum comando acima invoca
o workload. `manifest` exige fonte limpo, labels das imagens correspondentes ao HEAD,
audit do loadgen, schemas reais, dataset íntegro e recursos/configurações observados.
O destino deve ser novo. Um erro deixa `.incomplete.json`, e nunca libera execução.
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
usar sempre um destino novo. Não há comando automático de carga neste pacote.

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

Matriz futura: três perfis × 4/12 users × cinco repetições = 30 válidas. O piso temporal é
30 × (300 s estabilização + 60 s warm-up + 300 s measurement) = **5 h 30 min**, mais preparo,
drain, verificações e exportações. O teto configurado das esperas principais, sem exportação,
é aproximadamente 7 h 30 min (120+300+60 de partida+90+330 s por repetição); isso não é uma
previsão de desempenho. Execuções inválidas não contam como oficiais e exigem diagnóstico.

Os testes do incremento I continuam válidos. II acrescenta falhas antes/depois dos commits,
perda de rejeição, indisponibilidade dos peers, concorrência com rollback/resposta perdida,
deadlines e preparação parcial. A validação funcional não substitui ensaio de carga. Não houve
calibração, measurement, merge, tag ou release neste incremento.
