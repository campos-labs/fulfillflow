# Tutorial: simulando requisições com Locust

Este tutorial mostra como usar o workload Locust existente no FulfillFlow v1.0.0. O projeto não
usa um `locustfile.py` genérico: o runner prepara um PostgreSQL isolado, executa aquecimento e
medição em processos separados, coleta recursos e valida os efeitos persistidos. Esse é o caminho
correto para produzir resultados reproduzíveis.

> Todos os dados são sintéticos. A preparação remove somente o projeto Compose
> `fulfillflow-benchmark`, incluindo seu volume, depois de exigir a confirmação literal do nome.

## 1. O que será simulado

O arquivo `benchmarks/locustfile.py` envia somente requisições para a API HTTP pública. Há três
perfis:

| Perfil | Requisições |
| --- | --- |
| `timeline` | 50% detalhe de Shipment e 50% timeline |
| `ingestion` | 100% webhooks autenticados |
| `mixed` | 25% detalhe, 25% timeline, 30% webhook e 20% listagem filtrada |

Os webhooks usam os dois carriers, assinatura HMAC sobre os bytes exatos e IDs de evento exclusivos.
Uma resposta mutável diferente de HTTP 200 com `result=APPLIED` invalida a repetição.

Cada repetição:

1. recria banco, containers e dataset determinístico;
2. aguarda a estabilização declarada;
3. aquece por 60 segundos, sem misturar suas estatísticas;
4. mede por 300 segundos;
5. confere banco, respostas, recursos e checksums.

## 2. Pré-requisitos

- Python 3.13 e `uv`;
- Docker Engine com Docker Compose;
- Git e PowerShell;
- porta `127.0.0.1:18007` disponível.

Na raiz do repositório, instale as dependências, incluindo o grupo que contém o Locust:

```powershell
uv sync --frozen --all-groups
uv run python -c "import locust; print(locust.__version__)"
```

O Locust fica na imagem `loadgen`; ele não é instalado na imagem da aplicação.

## 3. Conheça e valide um manifest

O manifest controla perfil, usuários, spawn rate, durações, repetições, recursos e identidade do
ambiente. Valide a fixture sintética sem iniciar containers nem gerar tráfego:

```powershell
uv run python -m benchmarks.run_campaign `
  --manifest benchmarks/fixtures/smoke-campaign.json `
  --validate-only
```

A saída resume o perfil `mixed`, a carga de quatro usuários, as duas fases e as coortes. A fixture
`smoke-campaign.json` é apenas para validação de contrato: seus digests de imagem são marcadores e
seu `git_sha` é congelado. Portanto, ela não deve ser passada diretamente a `--execute`.

Os manifests `benchmarks/campaigns/v1-baseline-*.json` descrevem as campanhas publicadas. Eles só
podem ser reproduzidos no commit, nas imagens e no host declarados; o runner recusa divergências.

## 4. Configure o ambiente isolado

Use valores sintéticos exclusivos desta execução. Não coloque credenciais na linha de comando ou
no manifest.

```powershell
$env:BENCH_POSTGRES_DB = "fulfillflow_benchmark"
$env:BENCH_POSTGRES_USER = "fulfillflow_benchmark"
$env:BENCH_POSTGRES_PASSWORD = "<senha-sintetica-forte>"
$env:BENCH_DATABASE_URL = "postgresql+psycopg://fulfillflow_benchmark:$($env:BENCH_POSTGRES_PASSWORD)@db:5432/fulfillflow_benchmark"

$env:SESSION_SECRET = "<secret-de-sessao-com-pelo-menos-32-caracteres>"
$env:CARRIER_ALPHA_WEBHOOK_SECRET = "<secret-alpha>"
$env:CARRIER_BETA_WEBHOOK_SECRET = "<secret-beta-diferente>"

$env:BENCH_DB_POOL_SIZE = "5"
$env:BENCH_DB_MAX_OVERFLOW = "0"
$env:BENCH_DB_POOL_TIMEOUT_SECONDS = "5"
$env:BENCH_DB_STATEMENT_TIMEOUT_MS = "5000"
$env:BENCH_APP_CPUS = "1.0"
$env:BENCH_APP_MEMORY = "512m"
$env:BENCH_POSTGRES_CPUS = "1.0"
$env:BENCH_POSTGRES_MEMORY = "512m"
$env:BENCH_LOADGEN_CPUS = "1.0"
$env:BENCH_LOADGEN_MEMORY = "512m"
$env:BENCH_LOG_LEVEL = "WARNING"
$env:BENCH_LOG_FORMAT = "json"
$env:BENCH_OTEL_ENABLED = "false"
$env:BENCH_OTEL_TRACES_SAMPLER_ARG = "0.0"
```

Esses limites correspondem à fixture sintética. Ao usar outro manifest, copie os valores das
seções `pool`, `resources` e `telemetry` dele. Os secrets Alpha e Beta precisam ser distintos.

Confira a interpolação sem exibir o ambiente completo em logs compartilhados:

```powershell
docker compose -f compose.benchmark.yaml config --quiet
```

## 5. Construa as imagens

```powershell
docker build --target runtime --tag fulfillflow:benchmark-local .
docker build --target loadgen --tag fulfillflow-loadgen:benchmark-local .
docker pull postgres:18-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280
```

Para uma campanha local não oficial, copie a fixture para um novo arquivo dentro de
`benchmarks/campaigns/` e mantenha `"official": false`. Atualize nesse arquivo:

- `git_sha`, obtido com `git rev-parse HEAD`;
- `images.app` e `images.loadgen`, obtidos com `docker image inspect --format '{{.Id}}'`;
- `images.postgres`, usando o digest fixado no `compose.benchmark.yaml`;
- um `name` exclusivo, por exemplo `local-mixed-smoke`.

Consulte os dois IDs locais com:

```powershell
docker image inspect --format '{{.Id}}' fulfillflow:benchmark-local
docker image inspect --format '{{.Id}}' fulfillflow-loadgen:benchmark-local
```

Não altere a fixture nem um manifest oficial apenas para contornar uma recusa. Digests, SHA e
limites diferentes representam outra campanha e precisam permanecer identificados como tal.

## 6. Execute a campanha

O diretório de resultados deve ainda não existir. O JSON abaixo é um array de argumentos, não um
comando de shell; o runner o executa antes de cada repetição.

```powershell
$manifest = "benchmarks/campaigns/local-mixed-smoke.json"
$results = "benchmark-results/local-mixed-smoke-01"
$prepare = @(
  ".venv\Scripts\python.exe", "-B", "-m", "benchmarks.prepare_database",
  "--manifest", $manifest,
  "--project-name", "fulfillflow-benchmark",
  "--confirm-project-name", "fulfillflow-benchmark",
  "--database-name", "fulfillflow_benchmark",
  "--confirm-database-name", "fulfillflow_benchmark",
  "--timeout-seconds", "85",
  "--cleanup-timeout-seconds", "20"
) | ConvertTo-Json -Compress

uv run python -m benchmarks.run_campaign `
  --manifest $manifest `
  --execute `
  --confirm-campaign "local-mixed-smoke" `
  --base-url "http://app:8000" `
  --results-directory $results `
  --prepare-command-json $prepare
```

Se o runner for iniciado dentro do WSL/Linux, troque somente o primeiro item de `$prepare` por
`.venv/bin/python` e serialize o mesmo array JSON. `http://app:8000` é proposital: o Locust roda no
container `loadgen` e acessa a aplicação pelo nome do serviço Compose.

O modo suportado é headless. Abrir a UI web do Locust ou executar diretamente
`locust -f benchmarks/locustfile.py` ignora a preparação, os arquivos de runtime, as duas fases e as
validações; esse resultado serve, no máximo, para experimentação e não é comparável.

## 7. Leia os resultados

Ao final, o diretório de cada repetição contém, entre outros:

- `locust_stats.csv`: snapshot final, incluindo requisições drenadas;
- `locust_stats_history.csv`: série temporal periódica;
- `response_codes.csv`: contagem sanitizada por HTTP status e problem code;
- `operational_results.csv`: resultados de negócio e persistência;
- `database_counts.csv`: cardinalidades antes/depois das fases;
- `resources.csv` e `warmup/resources.csv`: CPU, memória e conexões;
- `metadata.json`: ambiente esperado e observado;
- `checksums.sha256`: integridade dos artefatos.

Em `locust_stats.csv`, observe principalmente `Requests/s`, `Failures/s`, mediana e percentil 95 da
linha `Aggregated`. Uma campanha oficial válida exige cinco repetições por perfil/carga e usa as
medianas, não o melhor resultado. Um diretório `.partial` ou um marcador `.incomplete.json` indica
execução inválida ou interrompida.

## 8. Limpeza segura

Depois de guardar os artefatos, remova somente os containers, a rede e o volume exclusivos:

```powershell
uv run python -m benchmarks.prepare_database `
  --cleanup `
  --project-name fulfillflow-benchmark `
  --confirm-project-name fulfillflow-benchmark `
  --timeout-seconds 30
```

## 9. Problemas comuns

- **`manifest release/SHA does not match`**: o checkout atual não corresponde ao manifest.
- **`environment mismatch`**: imagem, recurso, worker, pool, tracing ou host divergiu.
- **diretório de resultados já existe**: escolha um caminho novo; o runner não sobrescreve.
- **falha na preparação**: confirme Docker ativo, variáveis `BENCH_*` e o nome literal do banco.
- **Locust invalidou a repetição**: confira `response_codes.csv` e o diretório `.partial`; não
  inclua essa repetição nas medianas.
- **erro de HMAC**: os secrets do `loadgen` devem ser os mesmos da aplicação e distintos entre si.

Para os detalhes normativos de dataset, host, coleta e validação, consulte
`benchmarks/README.md` e a seção 22 de `DESIGN.md`.
