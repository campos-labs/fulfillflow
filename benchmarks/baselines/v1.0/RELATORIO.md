# FulfillFlow v1.0 — consolidação técnica da baseline oficial

Consolidação em 2026-09-08. **30 repetições oficiais válidas: seis pontos, cinco por ponto.**
Nenhum piloto/diagnóstico anterior foi incorporado. A tentativa parcial de mixed/12 foi
excluída; as cinco válidas de mixed/4 foram preservadas e contadas uma única vez.
Esta consolidação não publica release nem cria tag; não executou nova carga. Esta é uma versão
portátil derivada do relatório aceito; as evidências originais permanecem byte a byte no arquivo local.

## Resultado principal

Valores centrais são **medianas entre cinco repetições**, inclusive para p50 e p95.
Não são percentis de uma população formada pela mistura das requests das cinco execuções.

| Profile | Users | Throughput (req/s) | p50 (ms) | p95 (ms) | Erros HTTP |
|---|---:|---:|---:|---:|---:|
| mixed | 4 | 185,93 | 13 | 36 | 0 |
| mixed | 12 | 190,40 | 43 | 110 | 0 |
| timeline | 4 | 276,37 | 11 | 16 | 0 |
| timeline | 12 | 282,09 | 38 | 50 | 0 |
| ingestion | 4 | 116,93 | 29 | 40 | 0 |
| ingestion | 12 | 115,04 | 96 | 120 | 0 |

Totais: 1.746.464 respostas medidas, todas HTTP200; 514.456 webhooks APPLIED na measurement.
Warm-up separado: 103.200 APPLIED. Cada repetição concluiu sua quota1720 (4 users) ou5160
(12 users), sem DUPLICATE, REJECTED, IGNORED ou NO_STATE_CHANGE. Em timeline, a measurement
não alterou os snapshots do banco. Nos demais profiles, POST/HTTP/TrackingEvent/inbox/Notification
foram conciliados. Requests GET não são comparadas como se fossem linhas novas no banco.

## Dispersão descritiva

| Ponto | Throughput mínimo–máximo | IQR throughput | CV throughput | p50 mínimo–máximo | p95 mínimo–máximo |
|---|---:|---:|---:|---:|---:|
| mixed/4 | 181,34–190,90 | 4,13 | 2,05% | 13–14 | 35–37 |
| mixed/12 | 185,94–191,55 | 4,31 | 1,37% | 43–44 | 110–120 |
| timeline/4 | 275,18–281,29 | 4,32 | 0,99% | 10–11 | 15–16 |
| timeline/12 | 274,16–285,01 | 3,26 | 1,54% | 37–39 | 50–52 |
| ingestion/4 | 114,03–117,63 | 1,39 | 1,26% | 29–30 | 39–41 |
| ingestion/12 | 110,10–116,61 | 4,27 | 2,50% | 94–100 | 120–130 |

IQR=Q3−Q1, quartis inclusivos (`statistics.quantiles`, método inclusive); CV=desvio-padrão
amostral/ média ×100. Média, desvio-padrão, quartis e valores por repetição constam em summary.json.
n=5 por ponto é pequeno: não são apresentados intervalos inferenciais nem alegação de
significância estatística. Nenhum resultado foi removido por desempenho ou tratado como outlier.

## Recursos e coleta

CPU: mediana entre as médias temporais ponderadas das cinco execuções. Memória: maior amostra
do componente nas cinco execuções; não é soma dos máximos em um mesmo instante.

| Ponto | CPU app / PostgreSQL / loadgen (%) | Pico memória app / PostgreSQL / loadgen (MiB) |
|---|---:|---:|
| mixed/4 | 98,4 / 25,4 / 24,0 | 93,2 / 139,9 / 160,8 |
| mixed/12 | 101,7 / 27,5 / 24,2 | 97,5 / 186,1 / 133,6 |
| timeline/4 | 98,3 / 16,5 / 29,0 | 91,7 / 93,4 / 163,1 |
| timeline/12 | 102,3 / 18,5 / 30,3 | 96,6 / 130,4 / 151,6 |
| ingestion/4 | 99,1 / 31,4 / 17,6 | 95,5 / 198,3 / 161,7 |
| ingestion/12 | 101,2 / 34,4 / 17,8 | 98,6 / 219,0 / 132,2 |

Docker100%=um núcleo, não100% da quota de2CPUs. CPU integrada: soma(cpu_i/100 ×delta_t_i),
usando a amostra direita de cada intervalo entre timestamps observados. A média divide essa
integral pelo intervalo observado; não extrapola a borda anterior à primeira amostra nem
preenche a borda final até300s. Trata-se de aproximação sobre os intervalos disponíveis;
os timestamps de coleta e os instantes dos contadores Docker não são perfeitamente coincidentes.
Memória é a métrica do harness com desconto de cache; não representa toda a RAM física do host.

Todas as30 repetições têm60 ciclos completos no warm-up e300 na measurement, com os três
componentes presentes. Maior intervalo:1,021s no warm-up e1,019s na measurement. Não foram
interpoladas amostras, nem introduzido um novo threshold de aceitação para cadência.
O CSV `postgres_active_connections` chegou a12: apesar do nome, a consulta conta conexões
ao banco em pg_stat_activity, sem filtrar state='active'. Inclui sessões ociosas/de inspeção;
não prova12 queries simultâneas nem violação do pool10 da aplicação.

## Protocolo e identidade congelados

- Branch release/v1.0.0; HEAD ae15e0a2da465f4aec3d9c699655441ad1947265.
- Dataset:5897d7441f73fec77d98ff97196aff0becc3f301e45c708febff493d8f4a63bf.
- Schema:0ee99170b78404380541ac12cf676126d047fc60275a4e2947fbe5a68b32652b;
  Alembic0004_notifications. Identidade lógica inicial do banco igual nas30 execuções.
- uv.lock:ecbe34e90d7481fce12aa4d35ea4680968129729e4a21ec45842236c5bee9e56.
- app:sha256:0fd4920104d4fa867fe9a0ba4a46e9761c0ff80ba5c03178dac5b85080db1fc1.
- loadgen:sha256:f5b7118626bc3cf156029b9d31399bcba78013a035835db16815616c46bdd906.
- PostgreSQL:sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280.
- Windows10.0.26200/build26200.9278; Intel Core5 120U,10 núcleos físicos/12 lógicos;
  RAM física16.847.921.152bytes. Docker Engine29.7.2, Compose5.5.0,
  WSL2.3.24.0/kernel5.15.153.1-microsoft-standard-WSL2; VM12CPUs.
- Docker nominal8.165.453.824bytes, tolerância absoluta inclusiva1.048.576bytes;
  observado nas30 execuções8.165.457.920bytes (+4096). Demais campos essenciais: igualdade exata.
  Três boots independentes já documentados sustentaram a tolerância, mas não garantem universalidade.
- app2CPUs/1536MiB; dados2CPUs/2560MiB; loadgen2CPUs/1536MiB; worker Uvicorn1.
- Pool10, overflow0, timeout5s, statement timeout5000ms; JSON/WARNING, tracing off, sampling0.
- q430 comum, warm-up de ingestion em todos os profiles; spawn16users/s; coleta1s;
  estabilização300s, warm-up60s e measurement300s; request/drain10s; processos90/330s.
- AC, plano ab6534a3-bc02-4c44-94d1-a8535b2eb070, gate RAM>=4GiB antes de cada preparação;
  restauração determinística a cada repetição; awake guard do sistema/tela durante as sessões.
- Mesma arquitetura de coleta e healthcheck: importação obrigatória de Locust na inicialização,
  healthcheck periódico python -c pass e supervisão de execução pelo runner.

A estabilização observada variou de300,000195 a300,001081s. RAM disponível observada pelo
HostProbe antes do warm-up:3,255–7,655GiB; swap WSL usado nessas observações:zero.
Esse snapshot ocorre depois da preparação/estabilização, não é o gate anterior à preparação.
Não se impõe retroativamente RAM>=4GiB durante measurement. Não há telemetria contínua de
swap/energia/temperatura do host que prove ausência de interferência durante toda a carga.

## Semântica de contagem e percentis

A janela de admissão permanece300s. O runner considera as respostas finais das requests admitidas,
incluindo conclusões no drain limitado, e promove o CSV final após o processo terminar. A cópia
interna locust_final_stats.csv foi comparada byte a byte com o CSV canônico para ambas as fases
das30 execuções. Os totais conciliam com response_codes e efeitos esperados no banco.

Throughput principal preserva Requests/s do CSV final Locust, como no summary do runner.
Também se registra N_final/300s no JSON, sem mudar a janela ou substituir silenciosamente o
estimador original. Diferença relativa máxima entre ambos:0,0152%. Percentis do Locust são
baseados nos seus histogramas/arredondamentos, não em uma lista preservada de latências individuais.

## Rastreabilidade, execução parcial e limitações

Fontes aceitas, sempre sem mover/reescrever originais:

1. mixed/4 — r01 a r05 — `repository/benchmarks/results/v1-baseline-mixed-attempt01`.
2. mixed/12 — complemento r01 a r05 — `repository/benchmarks/results/v1-baseline-mixed-12-complement01`.
3. timeline — 4/12, cinco cada — `repository/benchmarks/results/v1-baseline-timeline-attempt01`.
4. ingestion — 4/12, cinco cada — `repository/benchmarks/results/v1-baseline-ingestion-attempt01`.

A parcial mixed-12-users-r01.partial permanece preservada e excluída. A causa original da
recusa de preparação não foi confirmada; uma consulta posterior reproduziu RAM<4GiB,
o que não prova a causa no instante original. Nenhuma válida foi descartada ou repetida por isso.
O complemento mudou somente a seleção de load; todos os parâmetros originais de mixed/12
coincidem. Os quatro hashes de manifest e cada arquivo/receipt aceito estão em run-index.json.

Ordem efetiva: mixed/4 → interrupção → mixed/12 → timeline/4 → timeline/12 → ingestion/4 →
ingestion/12; r01–r05 dentro de cada ponto. Não houve contrabalançamento. Há possível efeito
de ordem, temperatura, processos do host e diferenças entre sessões. Pilotos do Engine antigo
não servem como controle causal nem são misturados a esta baseline.

Ao passar4→12 users, as medianas de throughput mudaram +2,41% (mixed), +2,07% (timeline) e
−1,62% (ingestion), enquanto p95 aumentou aproximadamente3 vezes. Junto à aplicação ocupando
cerca de um núcleo, isso é compatível com um platô de capacidade do worker. Não demonstra
sozinho a causa exclusiva do gargalo nem posiciona precisamente o joelho com apenas dois loads.
PostgreSQL/loadgen não atingiram suas quotas médias; isso não prova ausência de limites transitórios.
Pequenas oscilações não invalidam resultados. Não se atribui ganho de throughput ao healthcheck.

q430 produz1720 ou5160 eventos antes da measurement: procedimento repetível por carga, mas não
o mesmo volume de dados entre cargas. Warm-up somente de ingestion pode deixar transientes
de leitura; não se afirma aquecimento completo de todos os caches. Resultados descrevem este
workload e notebook; não são extrapolação para produção nem evidência de disponibilidade contínua.

Na comparação v1.0 × v1.1, preservar protocolo, workload, dados, ambiente de medição e tolerância;
orçamentos agregados fixos separados para aplicação e dados, incluindo overhead/serviços/bancos
extraídos; loadgen separado e idêntico. Não multiplicar pool ou recursos por componente.
HEAD, imagens da aplicação, Alembic head e schema da v1.1 terão identidades próprias, verificadas
contra seus respectivos manifests. Igualdade de identidades entre arquiteturas não é exigida;
a comparabilidade do protocolo e orçamento é. Não se antecipa vantagem da arquitetura híbrida.

## Artefatos derivados e fechamento

- [summary.json](summary.json): seis pontos, dispersão,
  métricas por repetição, recursos, identidade e estado sanitizado observados.
- [run-index.json](run-index.json):30 selecionadas,
  hashes dos manifests, metadata, checksums, receipts e arquivos-fonte; parcial excluída.
- [consolidate.py](consolidate.py): cálculo offline
  reproduzível; `--archive <pasta>` aponta ao arquivo preservado e `--verify-only` compara
  resultados sem escrever ou executar carga. Usa exclusivamente a biblioteca padrão Python.
- SHA256SUMS identifica os arquivos derivados deste pacote; não substitui checksums originais.

Nenhuma aplicação, harness, dataset, dependência, imagem, manifest congelado, resultado ou checksum
original foi alterado. O diff documental adiciona versões portáveis, documentação e exclusões de Git.
Ruff, format, testes focais e recálculo das saídas foram usados na validação; não foram repetidos
testes da aplicação, builds ou workloads, pois não houve mudança da aplicação ou do harness de execução.

## Commit medido e release documental

O commit medido é `ae15e0a2da465f4aec3d9c699655441ad1947265`. O futuro commit documental e
sua tag não foram medidos, nem substituem esse SHA nos manifests e metadata. A tag v1.0.0 deverá
identificar a revisão final aprovada com esta documentação, apontando explicitamente ao commit
medido. Não reconstruir nem reidentificar as imagens da baseline para fazê-las coincidir com a tag.

O arquivo local `v1.0-baseline-20260908` preserva as fontes completas, os quatro pacotes operacionais,
a tentativa parcial, diagnósticos anteriores separados, fonte Git do commit medido e Python isolado.
A seleção Git não inclui CSVs brutos nem cópias de diagnósticos. O índice usa caminhos relativos à
raiz desse arquivo, não ao checkout. Ver [README.md](README.md) para o mapa e os comandos offline.

Os originais foram preservados. Ainda falta cópia independente do notebook; arquivo no mesmo
computador não protege contra perda do dispositivo. Nenhum envio externo foi feito. Commit, push,
tag e release dependem de revisão e autorização posteriores.
