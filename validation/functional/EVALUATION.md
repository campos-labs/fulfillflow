# Avaliação funcional comparativa entre referências congeladas

## Resultado e interpretação

O conjunto avaliado de 03/10/2026 terminou com **54 casos: 45 PASS e nove
INCONCLUSIVE**, todos estes no cenário C4. As três rodadas apresentaram as mesmas
classificações. PASS significa conformidade com as verificações daquele cenário;
não é estimativa de confiabilidade ou aprovação geral da arquitetura.

| Cenário | v1.0.0 | v1.1.0-rc.1 | v1.2.0-rc.1 |
| --- | --- | --- | --- |
| C5 — conclusão sem intervenção | 3 PASS | 3 PASS | 3 PASS |
| C1 — duplicata após conclusão | 3 PASS | 3 PASS | 3 PASS |
| C2 — identificador repetido com conteúdo diferente | 3 PASS | 3 PASS | 3 PASS |
| C3 — controle, liberando a barreira | 3 PASS | 3 PASS | 3 PASS |
| C3 — interrupção antes do commit | 3 PASS | 3 PASS | 3 PASS |
| C4 — banco indisponível antes da oferta | 3 INCONCLUSIVE | 3 INCONCLUSIVE | 3 INCONCLUSIVE |

A contribuição operacional é identificar o que já estava persistido, quem retomou
trabalho interrompido e como se comprovou sua conclusão.

| Fronteira e observação | Estado persistido | Ação e resultado |
| --- | --- | --- |
| C3 v1.0: processo Core interrompido após SQL, antes do commit | Admissão RECEIVED; efeitos não confirmados | Reinício explícito da API; pendência durante 60 s; uma reentrega idêntica concluiu |
| C3 v1.1: processo Core interrompido na transação local | Admissão Tracking preservada; recibo/efeitos Core não confirmados | Reinício explícito da API; pendência durante 60 s; uma reentrega idêntica concluiu |
| C3 v1.2: worker Core interrompido antes do commit | Admissão e trabalho técnico duráveis; recibo/efeitos/outbox de resultado não confirmados | Reinício explícito do worker; retomada local e conclusão sem reentrega do webhook |
| C4 nas três versões: banco restaurado após oferta durante indisponibilidade | Nenhuma admissão ou efeito no snapshot final | SQL autenticado disponível, mas requisição correlacionada pendente até o limite; sem reentrega |

Em C3, o snapshot independente após o kill e antes do reinício coincidiu com o
snapshot externo à transação. Os efeitos SQL observados dentro da barreira não
apareceram parcialmente confirmados. Os controles concluíram ao liberar a mesma
barreira. Cada conclusão conferida manteve uma timeline APPLIED, uma Notification
SIMULATED, Shipment DELIVERED e Order FULFILLED; recibos e mensagens pertinentes
foram verificados. A v1.2 dispensou a reentrega, **não o reinício explícito**.

C1 retornou 200 na duplicata terminal, com snapshot inalterado. C2 retornou 409
para bytes autenticados diferentes sob o mesmo identificador, preservando o
snapshot. Os códigos de domínio também foram exigidos pelo executor, mas os
corpos HTTP integrais não foram exportados.

## C4 e os limites da observação

Nas três configurações, o retorno do acesso SQL não foi suficiente para observar
o encerramento das requisições correlacionadas no prazo. Na v1.0, o cliente recebeu
ReadTimeout e o webhook permaneceu ativo. Na v1.1/v1.2, a resposta pública terminou
com 503 enquanto uma chamada interna Core permaneceu ativa. O snapshot não mostrou
admissão, recibo, timeline, Notification ou mensagem técnica para o evento.

O comando de restauração começou entre 30,002 e 30,014 s após a oferta. Isso não
identifica o instante de prontidão do PostgreSQL: depois foi conferido SELECT
autenticado pelo endereço da aplicação, seguido de observação limitada. O executor
encerrou os processos após preservar o estado no limite. Não foi determinado o
desfecho com observação maior nem a causa interna da espera.

A dependência síncrona da admissão é um contrato documentado; não é diagnóstico
da causa dessa pendência. C4 precede admissão durável e não contradiz a recuperação
pós-admissão. Os seis 503 deste cenário não explicam o 503 histórico de desempenho.

## Método, identidade e critérios

| Referência da aplicação | Commit |
| --- | --- |
| v1.0.0 | `6235f6cb2a733e23ea76cf8264d2145f3759a871` |
| v1.1.0-rc.1 | `217e29a230689da3bd6359790f0753b41a10a927` |
| v1.2.0-rc.1 | `9b445f9b5466cd302c89f1deed7a9c051cb397ae` |

A ferramenta foi qualificada antes de fixar o conjunto avaliado. A ordem foi de
três rodadas de C5/C1/C2/C3 controle/C3 kill/C4, cada ação em v1.0/v1.1/v1.2.
O protocolo comum preservou os contratos distintos das versões. A seleção é
dirigida e informada pela implementação; não houve cegamento nem randomização.

O manifesto original tem SHA-256
`77c61cfeda13b8d9ca0074d2caedacaa5feddf0358a8f7ab476b231e965725f2`.
Ele vincula fontes, scripts e protocolo integral anteriores à execução. As
aplicações não foram alteradas. Cada versão usou seu venv congelado, Python 3.13.1,
HTTP/TCP real e PostgreSQL real; v1.2 empregou RabbitMQ no fluxo assíncrono.
Os hashes dos scripts efetivamente executados estão no manifesto e nas cópias por
caso, inclusive antes do commit de fechamento. O commit que contém este documento
identifica a distribuição final; não substitui a identidade do instrumento executado.

Recursos Docker foram exclusivos por caso: PostgreSQL e RabbitMQ com 1 CPU/512 MiB
cada, portas loopback e volumes próprios. APIs/workers rodaram no host Windows.
Esse ambiente não equivale ao orçamento agregado dos benchmarks históricos.
Snapshot por conexão SQL independente significa outra conexão fora da transação;
não significa validação por equipe independente.

A regra congelada permitiu continuar exclusivamente após o C4 inconclusivo com
intervenção, pendência correlacionada, ausência de efeitos, restauração, SQL e
limpeza comprovados. Os códigos individuais 2 permaneceram registrados. Outros
erros interromperiam a sequência, sem retry ou reposição. A qualificação anterior
é histórico de desenvolvimento, não parte das 54 execuções.

## Evidências e disponibilidade

O [índice distribuível](evidence/c/README.md) contém dois ZIPs, hashes por arquivo,
origem das cópias sanitizadas e reprodução offline da matriz. Os 1.549 arquivos do
conjunto avaliado foram inventariados após a execução; isso não é assinatura
externa anterior à coleta. Os originais locais permanecem intactos. Os 108
containers foram conferidos parados, com saída zero, sem OOM registrado e volumes
preservados. Não havia containers concorrentes registrados ou ativos na revisão;
isso não demonstra ausência de pressão global ou mudanças de energia no host.

O runtime era conferido de forma bloqueante a cada caso, mas o retorno detalhado
dessa função não era exportado novamente. A preparação preserva os inventários,
complementados pelo prefixo/PID do supervisor e pelas cópias da ferramenta.
Os testes Windows do supervisor foram realizados localmente. A CI padrão da
aplicação não executa automaticamente esta pasta nem substitui essa evidência.

Os pacotes permitem conferir dados e reconstruir a matriz sem Docker. Não contêm
venvs, imagens ou volumes e não demonstram reprodução externa da execução.
Disponibilidade no GitHub depende das permissões do repositório, atualmente privado.
A cópia independente dos originais continua sem confirmação.

## Alcance e encerramento

A avaliação cobre um evento sintético, um carrier e fronteiras instrumentadas.
As três rodadas não representam amostra de todas as falhas possíveis. A barreira
controla o instante de interrupção e influencia os tempos; não extrair latência,
capacidade ou superioridade geral destes registros. Os cenários complementam os
testes de desenvolvimento e não integram campanhas históricas de desempenho.

B permanece complementar: cobre também interrupção na finalização Tracking após
persistência técnica. O caso histórico v1.1 de perda de resposta após commit cobre
outra fronteira e também não é substituído por C3.

**C está encerrado no recorte definido.** Sem novas execuções, merge nas branches
de produto, alteração de tags ou nova candidata da aplicação. A ferramenta e suas
evidências são entregáveis distintos, mantidos na branch própria. Nenhuma mudança
posterior de instrumentação deve ser apresentada como executada neste conjunto.
