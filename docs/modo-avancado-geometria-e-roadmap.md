# Modo avançado: correção da geometria orbital (job 141) e próximos passos

## O que causava o erro do job 141

| Sintoma no log | Causa encontrada no código | Correção |
|---|---|---|
| `nav_candidates=0`, `nav_path=None` mesmo com `.26n` local | `_resolve_candidate_base_paths` descartava o `best_navigation_file` do sincronizador RBMC; a busca de NAV só olhava a pasta da observação (`converted/` ou `inputs/` do job) e não a pasta `extracted/` onde fica o `.26n` | Caminhos NAV agora seguem do RBMC até o pipeline; busca recursiva na pasta diária `storage/rbmc/<est>/<data>` |
| (latente) NAV encontrado, mas nenhuma posição | O parser lia o PRN do RINEX 2 (`" 1"`, `"12"`) como sistema inválido e descartava 100% dos registros; RINEX 3 tinha colunas deslocadas | Parser novo para RINEX 2.x / 3.x / 4.x (GPS), testado com os dois formatos |
| (latente) posições erradas fora do domingo | `tk` calculado com segundos do dia contra TOE em segundos da semana GPS | Tempo GPS absoluto (semana + segundos), seleção de efeméride saudável dentro de 4 h |
| (latente) `TypeError` no fallback | Épocas `...Z` (com fuso) comparadas a datas sem fuso | Normalização de datas |
| CDDIS devolvendo HTML de login | `requests` remove o `Authorization` quando o CDDIS redireciona para `urs.earthdata.nasa.gov` | `EarthdataSession` (receita oficial NASA) + rejeição de `text/html` |
| Timeouts do IGN travando o job | Cada um dos ~12 arquivos esperava 60 s no mesmo host | Host marcado como indisponível após o 1º timeout; espelho BKG adicionado; nomes longos IGS (`IGS0OPSFIN/RAP/ULT`) usados a partir da semana 2238 |
| HTTP 400 genérico | Falha só aparecia depois, na interpolação | Erro claro logo após a geometria, com resumo do SP3 (HTML/timeout/404) e do NAV e o que fazer |

## Ganhos de precisão incluídos

* SP3 interpolado por Lagrange grau 9 (antes: linear entre nós de 15 min, erro de até ~50 km na posição do satélite).
* Distância geométrica com tempo de propagação e rotação da Terra (Sagnac) no transporte base → VRS.
* Efemérides de todas as estações RBMC são mescladas (mais satélites cobertos).
* Sem credenciais Earthdata o modo avançado não aborta mais: usa espelhos anônimos e NAV broadcast. Em último caso baixa o BRDC diário do IGS.

Ordem de prioridade da geometria: SP3 final → rápido → ultra-rápido → NAV do ZIP RBMC → BRDC IGS.

## Avaliação geral do projeto (pontos para as próximas etapas)

1. **Coordenadas das bases**: o pipeline usa `APPROX POSITION XYZ` do cabeçalho RINEX. Para VRS com sigma baixo junto ao IBGE, as bases devem usar as coordenadas oficiais SIRGAS2000 (relatório descritivo RBMC, já existe `rbmc_station_report_parser_service.py`) propagadas para a época da sessão, ou o resultado PPP-IBGE.
2. **Atmosfera**: a VRS é hoje uma média ponderada das observações transportadas geometricamente. Em distâncias de RBMC (100–300 km no Pará/Amapá) o erro dominante é ionosférico/troposférico diferencial. Próximo passo técnico: modelar ionosfera (combinação geometry-free entre bases) e troposfera (Saastamoinen + resíduo interpolado) por satélite.
3. **Código duplicado**: `vrs_processing_service.py` (2,6 mil linhas), `vrs_generation_service.py` e `routers/jobs.py` (2,1 mil linhas) concentram lógica dos dois modos. Vale extrair um núcleo único (leitura RINEX, geometria, escrita) usado pelo básico e pelo avançado.
4. **Credenciais**: a senha Earthdata é guardada em base64, que não é criptografia. Recomendado cifrar com chave do servidor.
5. **Arquivos duplicados**: `backend/requeriments.txt` (grafia errada) convive com `requirements.txt`.
6. **Testes**: não havia nenhum; esta mudança cria `backend/tests` (pytest) e um workflow do GitHub Actions.

## Plano proposto para a etapa seguinte (VRS por hora/minuto/segundo)

1. Interface única "Gerar VRS": data, hora inicial e final com segundos, intervalo (1/5/15/30 s), coordenada da VRS (SIRGAS2000) e seleção automática das 3–5 RBMC mais próximas.
2. Cabeçalho RINEX 2.11 compatível com Topcon Tools (MARKER NAME, ANT # / TYPE = NONE, ANTENNA DELTA 0, APPROX POSITION = coordenada da VRS, TIME OF FIRST/LAST OBS exatos).
3. Validação automática antes de entregar: continuidade de fase (cycle slips), número de satélites por época, comparação VRS × base real (gerar VRS sobre uma RBMC existente e medir a diferença — teste de regressão de precisão).
4. Relatório por job com fonte da geometria (SP3 final/rápido/NAV), bases usadas e indicadores de qualidade.
