# Baseline v1.0 — publicação seletiva e reprodução offline

Aceita em 2026-09-08: **30 repetições oficiais válidas**, mixed/timeline/ingestion ×4/12 users,
cinco por ponto. Não contar pilotos, cópias ou a tentativa parcial. Consulte [RELATORIO.md](RELATORIO.md)
para métricas, dispersão e limitações; [summary.json](summary.json) contém os valores completos.

## Identidades e manifests imutáveis

Commit medido: `ae15e0a2da465f4aec3d9c699655441ad1947265`.
O futuro commit documental/tag não altera o commit medido, imagens, resultados ou manifests.

| Manifest em `benchmarks/campaigns/` | SHA-256 |
|---|---|
| v1-baseline-mixed.json | ff618b7509108c1e6b3c5185e36784870ca467d5c3c19b052a3e3894dcbaec79 |
| v1-baseline-mixed-12-complement01.json | aaf7de7949e8724a6c3931ee4e4b4fc2ba42fb5e8f8881aa236c3f80adc4b25a |
| v1-baseline-timeline.json | d6b29a69d3ce06b8d6e5ad7c6b87caf72cdcde9832f323ced254c1d41feb3cb1 |
| v1-baseline-ingestion.json | 26cf9bb4f03b3088001a597f024afe3b7e7522ef5bddcda5aa782698e37441c4 |

O complemento contém somente o load mixed/12; todos os outros campos coincidem com mixed.
Cinco mixed/4 válidas vêm da primeira sessão; cinco mixed/12 do complemento. A causa original
da interrupção permanece não confirmada. [run-index.json](run-index.json) registra a seleção exata
e hashes, sem mover, sobrescrever ou contar duas vezes os arquivos.

## Arquivo local e portabilidade

Raiz lógica: `v1.0-baseline-20260908/`, guardada fora de sincronização com serviços externos.
Todos os caminhos do índice são relativos a essa raiz:

- `repository/`: fonte Git do commit medido, quatro manifests originais e resultados oficiais,
  incluindo a tentativa parcial excluída;
- `operations/`: quatro pacotes operacionais completos, receipts, evidências e cópias verificadas;
- `diagnostics/`: pilotos, falhas e recuperação anteriores, separados e não contados;
- `report-original/`: relatório e consolidator originais intactos, com seus hashes;
- `worktree-inputs/uv.lock`: bytes LF do lockfile medido no Windows;
- `runtime/python313/`: CPython3.13.1 Windows x64 e biblioteca padrão, sem site-packages;
- `publication/`: cópia desta publicação portátil necessária à reprodução;
- `measured-source.tar`: exportação Git do commit medido, sem `.git` ou configuração pessoal;
- `archive-inventory.json` e `ARCHIVE.sha256`: inventário final e hash do inventário.

Nesta máquina, a exportação Git contém `repository/uv.lock` em CRLF (SHA256
`02379cd63ac251f02490c0ce1dfd1d2d729d1a0aef13faaff5c741c2558d3697`). Os bytes medidos eram LF
(`ecbe34e90d7481fce12aa4d35ea4680968129729e4a21ec45842236c5bee9e56`); ambas as versões foram
preservadas separadamente. A validação exige o hash exato medido, sem normalizar evidências.

O Git contém apenas publicação seletiva; CSVs e diagnósticos não estão embutidos. O recálculo
precisa do arquivo local completo. Arquivo no mesmo notebook ainda não é cópia independente.
Evidências originais podem conter caminhos históricos privados: não publicar o arquivo completo
sem revisão; as versões desta pasta removem caminhos de máquina. Nenhuma evidência original
foi sanitizada por sobrescrita. Imagens Docker não foram exportadas: são identificadas por digest;
não são dependências do recálculo offline e nenhum replay de carga foi feito.

## Recalcular sem Docker, PostgreSQL, Locust, uv ou pastas temporárias

Na raiz de qualquer checkout que contenha esta publicação, PowerShell:

```powershell
$archiveRoot = Read-Host 'Caminho local da pasta v1.0-baseline-20260908'
& (Join-Path $archiveRoot 'runtime/python313/python.exe') -I -S -B `
  benchmarks/baselines/v1.0/consolidate.py --archive $archiveRoot --verify-only
if ($LASTEXITCODE -ne 0) { throw 'Reproducao offline recusada.' }
```

`-I -S` isola o runtime e desabilita site-packages. Não há subprocessos, rede ou imports da aplicação
no consolidator portátil. Somente leitura quando `--verify-only`; gera saídas exclusivamente novas
com `--output <diretorio-novo>`, sem sobrescrever saídas existentes. Não usar Python `-O`.
Para reprodução sem checkout, trocar o caminho do script por
`(Join-Path $archiveRoot 'publication/consolidate.py')`. O modo verify compara summary/index
com os JSONs ao lado do script. O runtime Windows preservado exige Windows x64 compatível;
em outra plataforma, fornecer Python3.13 compatível e verificar os resultados.

O cálculo reutiliza as mesmas fórmulas aceitas; o adaptador apenas resolve evidências relativas
ao arquivo e valida sua integridade sem importar os antigos pacotes de execução. Summary deve
ser idêntico ao original; index muda somente os caminhos para referências portáveis.
O script não reconstrói o banco nem substitui provas realizadas pelo runner durante a medição.

## Comparação v1.0 × v1.1 e encerramento da release

Preservar workload, dataset, rotas, q, durações, coleta, ambiente e orçamento agregado separado
de aplicação/dados; serviços ou bancos extraídos dividem esse orçamento, sem multiplicar pool.
Loadgen permanece idêntico. HEAD, imagens da aplicação, Alembic head e schema da v1.1 terão
identidades próprias e validação exata dentro dessa versão. Não supor vantagem híbrida.

Após revisar este diff: versionar somente os arquivos selecionados, sem CSVs/diagnósticos;
executar os gates de release pertinentes e verificar a CI do futuro commit. Só com aprovação
posterior criar tag anotada `v1.0.0` no commit documental aprovado e publicar release descrevendo
o SHA medido e hashes desta baseline. Não retaggear, reescrever resultados nem associar métricas
ao novo SHA como se ele tivesse sido medido. Nenhuma dessas operações foi executada aqui.
