# Registro da coleta histórica de avaliações de Cyberpunk 2077

## Identificação

- Projeto: análise de emoções em avaliações da Steam por país
- Etapa: passo 1 — coleta das avaliações desde o início
- Jogo: Cyberpunk 2077
- Steam App ID: `1091500`
- Período coberto: do início histórico disponibilizado pela API até 26/07/2026
- Arquivo SQLite: `data/cyberpunk_2077_reviews.sqlite`

Este documento registra o procedimento adotado e os resultados observados. Ele não substitui
os registros detalhados das tabelas `sync_runs`, `sync_jobs`, `coverage`, `reviews` e
`review_versions` do banco SQLite.

## Objetivo e escopo

O objetivo principal desta etapa foi arquivar as avaliações de Cyberpunk 2077 disponibilizadas
pela API da Steam desde o início do histórico, independentemente do idioma da avaliação.
Depois da conclusão desse arquivo histórico, foi iniciado um enriquecimento complementar dos
autores com o país declarado em seus perfis públicos. A análise de emoções ainda não fez parte
desta etapa.

Foram adotados os seguintes critérios:

- todos os idiomas de avaliação suportados pela API da Steam;
- avaliações positivas e negativas;
- compras realizadas na Steam e aquisições externas;
- exclusão de avaliações classificadas pela Steam como atividade off-topic;
- deduplicação pelo identificador único `recommendationid`;
- preservação de versões quando o conteúdo mutável de uma avaliação mudou;
- armazenamento transacional em SQLite;
- cobertura somente considerada verificada após convergência.

O idioma da avaliação não foi tratado como indicador do país do autor. O particionamento por
idioma foi utilizado apenas como estratégia técnica de coleta.

## Configuração da coleta

O comando principal utilizado foi:

```bash
cd /home/murillo/Documentos/projeto-vi/steam-extractor
source .venv/bin/activate

steam-reviews-sync 1091500 \
  --language all \
  --database data/cyberpunk_2077_reviews.sqlite \
  --verification-passes 3
```

No comportamento final do sincronizador, `--language all` não envia uma única consulta
`language=all` para a Steam. Ele percorre separadamente os 31 códigos oficiais de idioma e
reúne os resultados no mesmo banco por `recommendationid`.

Para executar ou recuperar apenas uma partição, foi usado o formato:

```bash
steam-reviews-sync 1091500 \
  --language <idioma> \
  --database data/cyberpunk_2077_reviews.sqlite \
  --verification-passes 3
```

Exemplos efetivamente empregados durante a recuperação:

```bash
steam-reviews-sync 1091500 --language tchinese \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3

steam-reviews-sync 1091500 --language german \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3

steam-reviews-sync 1091500 --language polish \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3

steam-reviews-sync 1091500 --language russian \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3

steam-reviews-sync 1091500 --language schinese \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3

steam-reviews-sync 1091500 --language english \
  --database data/cyberpunk_2077_reviews.sqlite --verification-passes 3
```

Após verificar todas as partições, a cobertura agregada foi consolidada com:

```bash
steam-reviews-sync 1091500 \
  --language all \
  --database data/cyberpunk_2077_reviews.sqlite \
  --no-sync-updates
```

## Etapas necessárias para reproduzir o processo

### 1. Preparar o ambiente

No diretório do repositório:

```bash
cd /home/murillo/Documentos/projeto-vi/steam-extractor
source .venv/bin/activate
```

O enriquecimento de perfis exige uma chave da Steam Web API. Ela pode ser fornecida por
`--api-key`, mas durante o processo foi mantida na variável de ambiente:

```bash
export STEAM_API_KEY="<chave>"
test -n "$STEAM_API_KEY" && echo "Chave configurada" || echo "Chave ausente"
```

A chave não deve ser escrita em logs, documentos ou arquivos versionados.

### 2. Coletar e persistir as avaliações

Executar `steam-reviews-sync` com `--language all`, banco SQLite persistente e três passagens
de verificação. O sincronizador percorre os idiomas separadamente e pode ser retomado sem
recomeçar as partições concluídas.

### 3. Recuperar partições incompletas

Consultar o resumo de cobertura e executar novamente apenas os idiomas pendentes com
`--language <idioma>`. Uma partição recuperada deve passar por novas passagens completas até
convergir sem inserir novos identificadores.

### 4. Consolidar e validar a cobertura

Executar a consolidação com `--language all --no-sync-updates` e confirmar:

```text
31/31 partições verificadas
language=all complete_history=true
nenhuma partição pendente
```

### 5. Sincronizar os países dos autores

Executar `steam-profiles-sync` contra o mesmo banco e intervalo da pesquisa. Repetir o comando
enquanto `after.complete` for `false`. Cada execução reutiliza os estados terminais e tenta
novamente apenas autores ainda não consultados ou com `request_failed`.

### 6. Executar por períodos longos sem depender do terminal

Para uma execução desacoplada do terminal, usar `nohup` e redirecionar explicitamente a saída:

```bash
nohup .venv/bin/steam-profiles-sync 1091500 \
  --database data/cyberpunk_2077_reviews.sqlite \
  --start 10/12/2020 \
  --end 26/07/2026 \
  --max-runtime 3600 \
  > logs/profiles_sync_<numero>.log 2>&1 &
```

O símbolo `>` antes do caminho do log é obrigatório. Sem ele, o caminho é interpretado como
argumento do programa e a execução termina com status 2.

Verificações operacionais:

```bash
pgrep -af steam-profiles-sync
tail -f logs/profiles_sync_<numero>.log
```

`Ctrl+C` encerra apenas o acompanhamento feito por `tail`; o processo iniciado com `nohup`
continua em segundo plano. Duas instâncias não devem acessar simultaneamente o mesmo banco.

### 7. Confirmar a conclusão

O enriquecimento só está concluído quando o resumo final apresenta:

```text
processing_coverage_percent: 100.0
pending_users: 0
request_failed_users: 0
complete: true
```

Os arquivos SQLite, CSV, logs, `nohup.out` e demais artefatos gerados não devem ser enviados
ao Git. Somente código, testes e documentação pertinentes devem ser versionados.

## Evolução do procedimento

### Tentativas iniciais com um único fluxo global

A primeira tentativa com `language=all` armazenou 404.862 avaliações, mas a API informou
961.563 avaliações esperadas. O fluxo terminou em 19/01/2023, sem alcançar o lançamento:

```text
status: incomplete
completion_reason: api_exhausted_before_expected_total
```

Uma segunda passagem global armazenou mais 36.408 avaliações e avançou até 27/10/2022, mas
também terminou prematuramente. Depois das duas tentativas, o banco continha 441.270
avaliações únicas.

### Particionamento por idioma

Consultas de primeira página mostraram que a soma dos totais por idioma era praticamente
igual ao total global, com pequenas diferenças causadas pelo fluxo ativo. Como cada avaliação
possui um idioma declarado, as partições formam conjuntos naturalmente separados.

O sincronizador passou então a utilizar os 31 idiomas aceitos pela API. Essa estratégia
completou a maioria das partições imediatamente. Algumas partições ainda receberam páginas
vazias prematuras: inglês, chinês simplificado, chinês tradicional, alemão, polonês e russo.

### Confirmação de páginas vazias

O comportamento da API demonstrou que uma resposta vazia isolada não era evidência suficiente
do fim do histórico. O sincronizador foi ajustado para:

1. comparar o total acumulado com `total_reviews`;
2. repetir uma página vazia no mesmo cursor;
3. exigir três respostas vazias consecutivas antes de declarar esgotamento prematuro;
4. continuar normalmente se alguma confirmação voltar a retornar avaliações;
5. persistir o total esperado mesmo quando uma partição possui zero avaliações.

As avaliações off-topic foram mantidas explicitamente excluídas por meio do parâmetro
`filter_offtopic_activity=1`.

### Convergência após recuperação

Uma passagem recuperada não foi considerada suficiente para comprovar cobertura. Depois de
alcançar o final, novas passagens independentes começaram no topo e foram unidas por
`recommendationid`. A partição foi marcada como `research_verified=true` somente quando uma
passagem completa não inseriu nenhum novo identificador.

Os principais casos de recuperação foram:

| Idioma | Novos IDs na 1ª recuperação | Passagens adicionais | Resultado |
|---|---:|---:|---|
| Chinês tradicional | 6.248 | 0 | verificado |
| Alemão | 3.790 | 2 | verificado após sequência `3790 → 2 → 0` |
| Polonês | 11.126 | 1 | verificado após sequência `11126 → 0` |
| Russo | 33.795 | 1 | verificado após sequência `33795 → 0` |
| Chinês simplificado | 141.355 | 2 | verificado após sequência `141355 → 3 → 0` |
| Inglês | 220.088 | 3 | verificado após sequência `220088 → 16 → 8 → 0` |

O árabe retornou total zero. Uma nova execução persistiu `expected_reviews=0` e normalizou o
estado para `complete`, `operational_complete=true` e `research_verified=true`.

## Critérios de conclusão

Foram registrados dois níveis distintos:

- `operational_complete`: o fluxo alcançou sua condição operacional de término;
- `research_verified`: uma passagem ininterrupta satisfez a comparação com o total esperado
  e, quando houve recuperação, as passagens completas convergiram sem novos IDs.

A etapa só foi considerada formalmente concluída quando:

1. as 31 partições estavam com estado consistente;
2. todas estavam com `research_verified=true`;
3. nenhuma partição permanecia pendente;
4. a cobertura sintética `language=all` estava com `complete_history=true`.

## Resultado final

Estado observado após a consolidação em 26/07/2026:

| Indicador | Resultado |
|---|---:|
| Avaliações únicas armazenadas | 962.737 |
| Soma dos totais informados por idioma | 962.779 |
| Diferença | 42 |
| Diferença percentual aproximada | 0,0044% |
| Avaliações com versões editadas | 174 |
| Partições processadas | 31 |
| Partições verificadas | 31 |
| Partições pendentes | 0 |
| Novas avaliações na consolidação final | 313 |
| Avaliações atualizadas na consolidação final | 17 |
| Schema SQLite | 3 |

Cobertura agregada:

```text
language: all
filter_type: recent
complete_history: true
oldest review (UTC): 2020-12-10 00:10:31
newest review observed (UTC): 2026-07-26 15:09:49
```

A diferença de 42 avaliações entre o arquivo e a soma dos totais representa aproximadamente
0,0044%. Ela é compatível com uma API ativa consultada em momentos diferentes: avaliações
podem ser publicadas, removidas, moderadas ou alteradas durante as várias horas de coleta.
Registros observados anteriormente não são apagados automaticamente do arquivo.

### Tempo da coleta de avaliações

A tabela `sync_runs` registrou 80 execuções entre
`2026-07-23T02:59:20.259444+00:00` e `2026-07-26T15:49:16.655579+00:00`.
O intervalo de calendário foi de aproximadamente 3 dias, 12 horas e 50 minutos, incluindo
pausas entre execuções, análises e ajustes.

A soma das durações das execuções registradas foi de 49.119 segundos, ou 13h38min39s de
atividade efetiva:

| Estado das execuções | Quantidade | Tempo ativo acumulado |
|---|---:|---:|
| `complete` | 71 | aproximadamente 10h05min18s |
| `incomplete` | 9 | aproximadamente 3h33min21s |
| Total | 80 | 13h38min39s |

Esse tempo mede as execuções persistidas no SQLite, não o trabalho humano de análise,
implementação, testes e acompanhamento.

## Estrutura de auditoria

O banco preserva:

- `reviews`: estado corrente de cada avaliação;
- `review_versions`: versões observadas do conteúdo mutável;
- `sync_runs`: resultado e métricas de cada execução;
- `run_reviews`: avaliações observadas por execução;
- `sync_jobs`: checkpoints por aplicação, idioma e ordenação;
- `coverage`: limites temporais e indicação de histórico completo.

O schema 3 separa a quantidade recebida em uma execução do progresso acumulado atravessando
retomadas. As páginas e o cursor seguinte são gravados na mesma transação.

## Limitações e interpretação

- O resultado corresponde ao conteúdo disponibilizado pela API da Steam sob os filtros
  declarados; não constitui prova externa de existência de avaliações que a Steam não expõe.
- Avaliações off-topic foram excluídas deliberadamente.
- Os totais da Steam variam durante a coleta.
- Pequenas diferenças entre recebido, armazenado e esperado são avaliadas com tolerância para
  deriva do fluxo ativo.
- O banco preserva avaliações anteriormente observadas mesmo que depois sejam removidas ou
  deixem de aparecer na API.
- País não pode ser inferido pelo idioma. A identificação por país depende de uma etapa
  posterior baseada nos perfis públicos dos autores.

## Enriquecimento complementar dos perfis dos autores

### Motivação e princípio de preservação

Após a conclusão do arquivo de avaliações, iniciou-se a consulta dos perfis públicos dos
autores para obter o código de país declarado na Steam. O procedimento foi desenhado para não
descartar avaliações nem autores quando o país não está disponível.

Cada autor é preservado e recebe um dos seguintes estados:

- `country_available`: perfil consultado e país declarado disponível;
- `country_unavailable`: perfil consultado, mas sem país declarado;
- `profile_unavailable`: a API não retornou o perfil;
- `request_failed`: a consulta falhou temporariamente e deve ser repetida;
- `not_checked`: perfil ainda não consultado.

Esses resultados são persistidos na tabela `player_profiles`. Os estados terminais são
reutilizados nas execuções seguintes, enquanto `request_failed` retorna automaticamente à fila.
O resultado exportado mantém também a coluna `country_status`, inclusive quando são aplicados
filtros de país.

### Sincronizador retomável

Foi criado o comando dedicado `steam-profiles-sync`. Ele lê apenas os identificadores dos
autores armazenados no SQLite, consulta os perfis em lotes e grava cada lote separadamente.
Assim, uma interrupção por tempo, terminal, rede ou indisponibilidade da API não elimina o
progresso anterior.

Comando básico usado:

```bash
steam-profiles-sync 1091500 \
  --database data/cyberpunk_2077_reviews.sqlite \
  --start 10/12/2020 \
  --end 26/07/2026 \
  --max-runtime 1800
```

Para execuções desacopladas do terminal, foi usado `nohup`. Exemplo de uma hora:

```bash
nohup .venv/bin/steam-profiles-sync 1091500 \
  --database data/cyberpunk_2077_reviews.sqlite \
  --start 10/12/2020 \
  --end 26/07/2026 \
  --max-runtime 3600 \
  > logs/profiles_sync_<numero>.log 2>&1 &
```

Também foram realizadas execuções de 30 minutos (`--max-runtime 1800`) e uma execução de
quatro horas (`--max-runtime 14400`). Não devem ser executadas duas instâncias simultâneas
contra o mesmo banco.

### Tratamento de rate limiting

As consultas aos perfis sofreram limitação frequente da API. O tratamento evoluiu para:

1. repetir falhas transitórias e timeouts com limite de tentativas;
2. respeitar o prazo máximo da execução também durante as esperas;
3. preservar como pendente um lote interrompido pelo prazo;
4. honrar `Retry-After`, em segundos ou data HTTP, quando enviado pela Steam;
5. usar esperas locais de 30 e 60 segundos quando `Retry-After` não é informado;
6. não acrescentar uma espera de 90 segundos depois da última tentativa malsucedida;
7. aumentar o intervalo normal entre requisições após um HTTP 429, até o teto de 5 segundos;
8. reduzir gradualmente esse intervalo após sequências de lotes bem-sucedidos.

As respostas observadas até a execução 25 não forneceram `Retry-After`. Mesmo com o controle
adaptativo, aproximadamente metade de algumas execuções foi consumida pelas esperas de retry.
Os lotes que terminaram em `request_failed` foram recuperados em execuções posteriores, o que
confirmou o funcionamento da retomada.

### Evolução das execuções

As primeiras execuções de enriquecimento foram usadas para validar persistência, retomada,
classificação dos estados e métricas. A partir da execução 14, o log passou a registrar
explicitamente eventos de rate limiting, tempo acumulado de retry e atrasos inicial e final.

Marcos observados:

| Execução | Duração máxima | Classificados na execução | HTTP 429 | Falhas ao final da execução | Cobertura acumulada |
|---|---:|---:|---:|---:|---:|
| 13 | 30 min | 23.400 | não instrumentado | 0 | 45,26% |
| 14 | 30 min | 21.000 | 29 | 100 | 47,44% |
| 15 | 4 h | 145.100 | 202 | 300 | 62,51% |
| 16 | 1 h | 39.300 | 53 | 100 | 66,59% |
| 17 | 1 h | 35.100 | 54 | 100 | 70,24% |
| 18 | 1 h | 40.900 | 46 | 100 | 74,49% |
| 19 | 1 h | 34.600 | 42 | 0 | 78,08% |
| 20 | 1 h | 41.700 | 48 | 0 | 82,41% |
| 21 | 1 h | 40.600 | 48 | 100 | 86,63% |
| 22 | 1 h | 41.300 | 48 | 100 | 90,92% |
| 23 | 1 h | 39.900 | 48 | 100 | 95,07% |
| 24 | 1 h | 32.300 | 55 | 200 | 98,42% |
| 25 | até 1 h | 15.210 | 20 | 0 | 100% |

As variações de vazão indicam que a limitação não depende apenas do intervalo entre chamadas;
há indícios de uma cota ou janela acumulada controlada pela Steam. Alterar o endereço IP não
foi adotado como parte do método.

### Tempo do enriquecimento dos perfis

Os logs preservados de `profiles_sync_02.log` a `profiles_sync_25.log` documentam 810.410
novas classificações. Antes da execução 02, já existiam 152.324 autores classificados por
execuções preliminares cujos logs detalhados não foram preservados.

As execuções 02 a 24 consumiram seus limites configurados: treze execuções de 30 minutos, uma
de quatro horas e nove de uma hora. A execução 25 possuía limite de uma hora, mas terminou em
aproximadamente 23min19s porque esgotou a fila. Assim, os logs 02–25 documentam cerca de
19h53min de processamento efetivo.

O período de calendário documentado começou em 26/07/2026 às 18:29 e terminou em 27/07/2026
às 17:06, aproximadamente 22h36min depois. Esse intervalo inclui pausas entre comandos. O
tempo total de enriquecimento foi superior a 19h53min, pois a fase preliminar responsável
pelos primeiros 152.324 autores não possui duração integral recuperável. Portanto, 19h53min
deve ser interpretado como tempo mínimo documentado, e não como duração exata de todo o
enriquecimento.

### Estado final após a execução 25

O último lote terminou em 27/07/2026 aproximadamente às 17:06. A execução 25 processou os
15.210 autores restantes, recuperou as 200 falhas temporárias da execução anterior e encerrou
com `complete=true`.

| Indicador | Resultado final |
|---|---:|
| Autores únicos no escopo | 962.734 |
| Autores classificados | 962.734 |
| Autores ainda não consultados | 0 |
| Falhas temporárias pendentes | 0 |
| Cobertura de processamento | 100% |
| País disponível | 503.867 |
| País não declarado | 458.839 |
| Perfil indisponível | 28 |
| País disponível entre classificados | 52,34% |
| Estados Unidos (`US`) | 107.867 |
| China (`CN`) | 93.134 |
| Brasil (`BR`) | 24.768 |

### Quantidade de avaliações por disponibilidade de país

Uma consulta em modo somente leitura à base integral confirmou a distribuição
das 962.737 avaliações pelo estado do perfil:

| Situação no recorte analítico | Avaliações | Autores |
|---|---:|---:|
| País não declarado (`country_unavailable`) | 458.840 | 458.839 |
| Perfil indisponível (`profile_unavailable`) | 28 | 28 |
| Outro país declarado | 278.100 | 278.098 |
| País selecionado (`BR`, `CN` ou `US`) | 225.769 | 225.769 |
| **Total de avaliações** | **962.737** | — |

A diferença entre 458.840 avaliações e 458.839 autores sem país declarado
ocorre porque um desses autores possui duas avaliações no corpus. Considerando
também os perfis indisponíveis, 458.868 avaliações não possuíam país utilizável
para a comparação entre Brasil, China e Estados Unidos.

Essas avaliações não foram apagadas ou descartadas da base integral. Elas foram
somente excluídas do recorte analítico por país. As 278.100 avaliações de outros
países declarados também permanecem preservadas e ficaram fora do recorte
porque a pesquisa selecionou apenas `BR`, `CN` e `US`.

Cobertura final por ano:

| Ano | Autores no escopo e classificados | País disponível | Cobertura de processamento | Disponibilidade de país |
|---|---:|---:|---:|---:|
| 2020 | 309.991 | 172.068 | 100% | 55,51% |
| 2021 | 143.109 | 78.949 | 100% | 55,17% |
| 2022 | 95.998 | 51.279 | 100% | 53,42% |
| 2023 | 122.930 | 62.647 | 100% | 50,96% |
| 2024 | 106.963 | 52.691 | 100% | 49,26% |
| 2025 | 119.324 | 57.006 | 100% | 47,77% |
| 2026 | 64.419 | 29.227 | 100% | 45,37% |

A redução da disponibilidade de país ao longo dos anos, de 55,51% em 2020 para 45,37% em
2026, deve ser apresentada nas análises temporais para evitar confundir ausência de país com
mudança emocional.

### Limitações metodológicas do país

- O código representa o país voluntariamente declarado no perfil público no momento da
  consulta; não comprova nacionalidade nem residência.
- O país atual do perfil pode não ser o país do autor na data em que a avaliação foi escrita.
- Perfis privados, removidos ou sem país continuam no corpus e são identificados pelo estado
  correspondente.
- A disponibilidade do país varia entre os anos já processados. Comparações temporais devem
  apresentar essa cobertura e considerar possível viés de ausência.
- O idioma da avaliação não foi usado para preencher ou inferir países ausentes.
- China terminou como o país quantitativamente mais representado depois dos Estados Unidos e
  é o candidato natural a terceiro país. A escolha metodológica definitiva ainda deve
  considerar os objetivos comparativos da pesquisa.

## Situação da etapa

O arquivo histórico de avaliações do passo 1 foi considerado concluído em 26/07/2026:

```text
31/31 partições verificadas
language=all complete_history=true
962.737 avaliações únicas arquivadas
```

Resumo dos tempos ativos documentados:

| Fase | Tempo ativo documentado |
|---|---:|
| Coleta e verificação das avaliações | 13h38min39s |
| Enriquecimento de perfis, logs 02–25 | aproximadamente 19h53min |
| Total mínimo documentado | aproximadamente 33h32min |

O total real é maior porque não inclui integralmente as execuções preliminares que
classificaram os primeiros 152.324 autores, nem o tempo humano dedicado a análise,
implementação, testes e acompanhamento.

O enriquecimento complementar por país foi concluído em 27/07/2026:

```text
962.734/962.734 autores classificados
processing_coverage_percent=100.0
pending_users=0
request_failed_users=0
complete=true
```

O procedimento não alterou nem excluiu as avaliações coletadas. Autores sem país declarado
ou com perfil indisponível permaneceram no corpus com estado explícito.
