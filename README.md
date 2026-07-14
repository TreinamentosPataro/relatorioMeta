# Relatório Meta — Meta ADS → Google Sheets

Automação em Python 3.11 que coleta métricas de anúncios da **Meta Marketing API** e as escreve
em um **Google Sheets**: uma aba com os dados brutos e outra com métricas derivadas.

> Estado atual: **esqueleto**. A configuração e o logging estão prontos; os módulos de coleta,
> transformação, validação, derivação e escrita têm apenas as assinaturas.

## Arquitetura

O pipeline é uma sequência linear de etapas, cada uma em seu módulo:

```
MetaClient.fetch_insights()   -> dados crus da API
        ↓
transform.to_rows()           -> linhas tabulares normalizadas
        ↓
validate.validate_rows()      -> falha cedo se o schema não bate
        ↓
derived.build_derived_rows()  -> métricas calculadas (CTR, CPC, CPA, ROAS...)
        ↓
SheetWriter.write()           -> aba bruta + aba de métricas
```

| Arquivo | Responsabilidade |
| --- | --- |
| `src/config.py` | Lê e valida toda a configuração vinda do ambiente. Único ponto de acesso a `os.environ`. |
| `src/logger.py` | Logging estruturado em JSON, nível controlado por `LOG_LEVEL`. |
| `src/meta_client.py` | Acesso à Marketing API (paginação, retries). |
| `src/transform.py` | Resposta da API → linhas da planilha. |
| `src/validate.py` | Checagem de schema e consistência antes de escrever. |
| `src/derived.py` | Cálculo das métricas derivadas. |
| `src/sheet_writer.py` | Escrita nas abas do Google Sheets (respeita `DRY_RUN`). |
| `src/main.py` | Ponto de entrada; orquestra o pipeline. |

Nenhuma credencial vive no código: tudo vem de variáveis de ambiente, carregadas de um `.env`
local em desenvolvimento e de secrets no CI.

## Configuração

O projeto é fixado em **Python 3.11** (`requires-python = ">=3.11,<3.12"` no `pyproject.toml`,
mais `.python-version` para pyenv/uv). Use exatamente essa versão: é a mesma que roda no CI.

```bash
py -3.11 -m venv .venv           # Linux/macOS: python3.11 -m venv .venv
.venv\Scripts\activate           # Linux/macOS: source .venv/bin/activate
python --version                 # deve imprimir Python 3.11.x

pip install -r requirements.txt

cp .env.example .env             # e preencha os valores
```

### Variáveis de ambiente

Obrigatórias — a aplicação encerra com erro claro se faltar alguma:

| Variável | Descrição |
| --- | --- |
| `META_APP_ID` | ID do app no Meta for Developers. |
| `META_APP_SECRET` | Segredo do app. |
| `META_ACCESS_TOKEN` | Token de longa duração com permissão `ads_read`. |
| `META_AD_ACCOUNT_ID` | Conta de anúncios, com prefixo `act_`. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON da service account (caminho do arquivo ou conteúdo). |
| `SPREADSHEET_ID` | ID da planilha de destino. |

Opcionais, com default:

| Variável | Default |
| --- | --- |
| `LOOKBACK_DAYS` | `7` |
| `ACCOUNT_TIMEZONE` | `America/Sao_Paulo` |
| `RAW_SHEET_NAME` | `Meta ADS` |
| `DERIVED_SHEET_NAME` | `Meta ADS - Métricas` |
| `LOG_LEVEL` | `INFO` |
| `DRY_RUN` | `false` |

### Pré-requisitos externos

1. **Meta**: app com o produto *Marketing API* e um token de longa duração com `ads_read`.
2. **Google**: uma service account com a **Google Sheets API** habilitada. Compartilhe a planilha
   com o e-mail da service account, dando permissão de **Editor**.

## Como rodar

### 1. Ambiente

```bash
py -3.11 -m venv .venv           # Linux/macOS: python3.11 -m venv .venv
.venv\Scripts\activate           # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + pytest
```

### 2. Variáveis

Preencha o `.env` (o `python-dotenv` o carrega sozinho). Para exportar na mão:

```bash
# PowerShell
$env:META_APP_ID="..."; $env:META_APP_SECRET="..."; $env:META_ACCESS_TOKEN="..."
$env:META_AD_ACCOUNT_ID="act_..."; $env:GOOGLE_SERVICE_ACCOUNT_JSON="C:\caminho\sa.json"
$env:SPREADSHEET_ID="..."; $env:DRY_RUN="true"; $env:LOG_LEVEL="DEBUG"

# bash
export META_APP_ID=... META_APP_SECRET=... META_ACCESS_TOKEN=...
export META_AD_ACCOUNT_ID=act_... GOOGLE_SERVICE_ACCOUNT_JSON=./sa.json
export SPREADSHEET_ID=... DRY_RUN=true LOG_LEVEL=DEBUG
```

### 3. Ensaio (não escreve nada)

**Rode sempre isto primeiro.** Com `DRY_RUN=true` a coleta na Meta é real, mas nada é
escrito na planilha:

```bash
DRY_RUN=true LOG_LEVEL=DEBUG python -m src.main
```

Confira nos logs: a janela (`since`/`until`), quantos registros vieram, e o
`would_update` / `would_append` do upsert.

### 4. Execução real

```bash
DRY_RUN=false LOOKBACK_DAYS=1 python -m src.main   # comece por 1 dia
```

Depois de conferir a aba, volte ao `LOOKBACK_DAYS` normal (7).

### Códigos de saída

O CI usa o código para distinguir o tipo de falha:

| Código | Significado |
| --- | --- |
| `0` | Sucesso (inclui "a Meta não devolveu dados na janela"). |
| `1` | Configuração inválida (variável de ambiente faltando). |
| `2` | Falha na Meta ou no Google Sheets (token, permissão, quota). |
| `3` | A validação pós-gravação reprovou (duplicatas, tipos, linhas faltando). |

## Testes

```bash
pytest
```
