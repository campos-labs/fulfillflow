# Pacote de evidências da avaliação funcional comparativa

Interpretação, método e limites: [EVALUATION.md](../../EVALUATION.md).
Este índice descreve acesso e integridade, sem duplicar a análise.

| Arquivo | Conteúdo |
| --- | --- |
| [evaluated.zip](evaluated.zip) | Cópias distribuíveis dos 1.549 arquivos do conjunto de 54 casos, preservando caminhos relativos, snapshots e scripts arquivados |
| [preparation.zip](preparation.zip) | 28 registros selecionados: protocolo, manifesto/liberação, inventários, validações e conferências; desenvolvimento explicitamente separado |
| [packages.json](packages.json) | Hashes dos ZIPs e de cada entrada, original e distribuível; seleção, exclusões e transformação declaradas |
| [reproduce.py](reproduce.py) | Confere integridade e reconstrói a matriz a partir dos registros; biblioteca padrão, sem Docker ou rede |
| [build_bundle.py](build_bundle.py) | Empacotador determinístico; requer os originais locais, não executa cenários |

Os originais em `results/c-evaluated-01` e `.artifacts` não foram modificados.
As cópias removem BOM UTF-8 quando presente e substituem prefixos pessoais de
caminhos por `<USER_HOME>` e `<PROJECTS>`. Outros conteúdos permanecem iguais.
`original_sha256` identifica os bytes locais; `distributed_sha256` identifica os
bytes no ZIP. Um hash original não é validável contra a cópia alterada. O pacote
verifica a integridade distribuída e registra a proveniência declarada, sem
pretender uma assinatura independente ou provar autenticidade histórica sozinho.

Manifestos e checksums originais preservados dentro dos ZIPs referem-se aos bytes
originais, não às projeções. Use `packages.json` para conferir as cópias.
IDs sintéticos e suas relações são preservados. Não há venv, dump, volume, imagem,
credencial de ambiente ou fonte integral de aplicação no pacote. Registros de
runtime e inventários não substituem esses componentes. As fontes de aplicação
são identificadas pelos três commits em EVALUATION.

Do diretório raiz do checkout, com Python 3.13:

```powershell
python validation/functional/evidence/c/reproduce.py
```

A saída reconstrói 18 linhas (cenário/versão), com três ocorrências por linha:
45 PASS e nove INCONCLUSIVE. A conferência não executa aplicações nem promove os
inconclusivos a PASS. Não confundir esse procedimento com repetir o experimento.

Para transportar o pacote, copiar esta pasta inteira; o verificador resolve os
ZIPs pela sua própria localização. Para referenciar uma versão no GitHub, usar o
commit de fechamento, não apenas o nome mutável da branch. O repositório privado
não garante acesso a leitores externos; os mesmos arquivos podem ser entregues
separadamente a destinatários autorizados. A cópia independente dos originais não
foi confirmada e não é substituída pela existência destes ZIPs.
