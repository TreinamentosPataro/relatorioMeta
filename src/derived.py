"""Aba de métricas derivadas: escrita idempotente (upsert), nunca destrutiva.

CONTRATO DE ESCRITA — leia antes de mexer neste arquivo.

Este módulo escreve EXCLUSIVAMENTE na aba DERIVED_SHEET_NAME ("Meta ADS - Métricas").
Ele JAMAIS escreve na aba bruta ("Meta ADS") nem em qualquer outra aba da planilha:

  * todo range de valores é qualificado com o nome da aba derivada;
  * todo request estrutural (number format) carrega o sheetId da aba derivada,
    resolvido em tempo de execução por _resolve_sheet_id();
  * _assert_target_is_derived() aborta a execução se DERIVED_SHEET_NAME coincidir com
    RAW_SHEET_NAME — as duas abas têm larguras diferentes (22 x 25 colunas), e escrever
    uma sobre a outra corromperia o histórico bruto.

A aba derivada É HISTÓRICO: um dashboard depende de ter TODOS os dias desde o início,
não só a janela de LOOKBACK_DAYS da execução atual. Por isso a escrita aqui é um UPSERT,
igual ao da aba bruta:

  * a aba NUNCA é limpa (não existe values.clear neste módulo, de propósito);
  * a chave de uma linha é (Data, Ad ID) — colunas A e E;
  * chave que já existe é ATUALIZADA in-place, no número da linha em que já está;
  * chave nova é ACRESCENTADA ao fim, via values.append com INSERT_ROWS;
  * linha antiga que não veio na coleta atual é simplesmente IGNORADA — nada a
    referencia, nada a sobrescreve, nada a apaga.

Nenhuma operação deste módulo remove linhas. As únicas escritas são values.update
(cabeçalho, linha 1), values.batchUpdate (linhas existentes, uma a uma, pelo número da
linha) e values.append. Não há clear, deleteDimension, deleteRange nem deleteSheet.

Os valores gravados são NÚMEROS, não fórmulas: fórmulas do tipo =Meta_ADS!L2/M2 quebram
quando as linhas da aba bruta são reordenadas ou inseridas. Aqui, o número é calculado em
Python e a planilha só o exibe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import Config
from src.logger import get_logger
from src.sheet_writer import (
    SheetRetryableError,
    SheetWriterError,
    _chunked,
    _normalize_key_date,
    classify_http_error,
    load_credentials,
)
from src.transform import Key, Row

logger = get_logger(__name__)

# Cabeçalho da aba derivada (22 colunas, A..V).
HEADER: Final[tuple[str, ...]] = (
    "Date",                     # A
    "Campaign Name",            # B
    "Adset Name",               # C
    "Ad Name",                  # D
    "Ad ID",                    # E
    "Spend",                    # F
    "Impressions",              # G
    "Link Clicks",              # H
    "CTR (Link)",               # I
    "CPC (Link)",               # J
    "CPM",                      # K
    "Leads",                    # L
    "CPL",                      # M
    "Landing Page Views",       # N
    "Custo por LPV",            # O
    "Hook Rate (3s/Impr)",      # P
    "Hold Rate (Thruplay/Impr)",  # Q
    "Video Completion (100%/Plays)",  # R
    "Omni Purchases",           # S
    "CPA (Spend/Compras)",      # T
    "Conversas Iniciadas",      # U
    "Custo por Conversa",       # V
)

COLUMN_COUNT: Final[int] = len(HEADER)

# Índices na linha bruta de 25 colunas produzida pelo transform.
_RAW_DATE: Final[int] = 0
_RAW_CAMPAIGN_NAME: Final[int] = 4
_RAW_ADSET_NAME: Final[int] = 6
_RAW_AD_NAME: Final[int] = 8
_RAW_AD_ID: Final[int] = 9
_RAW_SPEND: Final[int] = 11
_RAW_IMPRESSIONS: Final[int] = 12
_RAW_LINK_CLICKS: Final[int] = 13
_RAW_LANDING_PAGE_VIEWS: Final[int] = 14
_RAW_LEADS: Final[int] = 15
_RAW_VIDEO_PLAYS: Final[int] = 16
_RAW_THRUPLAY: Final[int] = 17
_RAW_VIDEO_3S: Final[int] = 18
_RAW_VIDEO_100: Final[int] = 22
_RAW_OMNI_PURCHASE: Final[int] = 23
_RAW_MESSAGING: Final[int] = 24

# Índices (0-based) na linha DERIVADA que formam a chave de upsert.
_DERIVED_DATE: Final[int] = 0   # coluna A
_DERIVED_AD_ID: Final[int] = 4  # coluna E

# Índices (0-based) na linha derivada, para os number formats.
_PERCENT_COLUMNS: Final[tuple[int, ...]] = (8, 15, 16, 17)  # CTR, Hook, Hold, Completion
_MONEY_COLUMNS: Final[tuple[int, ...]] = (5, 9, 10, 12, 14, 19, 21)  # Spend, CPC, CPM, CPL, LPV, CPA, Conversa
_DATE_COLUMN: Final[int] = 0

_PERCENT_FORMAT: Final[str] = "0.00%"
_MONEY_FORMAT: Final[str] = "#,##0.00"
_DATE_FORMAT: Final[str] = "yyyy-mm-dd"

# A aba vai de A a V (22 colunas). A linha 1 é o cabeçalho; os dados começam na 2.
_FIRST_COLUMN: Final[str] = "A"
_LAST_COLUMN: Final[str] = "V"
_KEY_LAST_COLUMN: Final[str] = "E"  # basta ler A..E para montar a chave (Data, Ad ID)
_FIRST_DATA_ROW: Final[int] = 2

# Limites de lote, iguais aos da aba bruta: a API aceita mais, mas payloads grandes
# estouram o timeout.
_UPDATE_CHUNK: Final[int] = 200
_APPEND_CHUNK: Final[int] = 1000

_MAX_ATTEMPTS: Final[int] = 5

# Última linha de um range A1 devolvido pela API ("'Aba'!A15:V17" -> 17).
_RANGE_END_ROW: Final[re.Pattern[str]] = re.compile(r":[A-Z]+([0-9]+)\s*$")


@dataclass(frozen=True, slots=True)
class DerivedSummary:
    """Resultado do upsert na aba derivada."""

    rows: int
    created_sheet: bool
    updated: int = 0
    appended: int = 0
    dry_run: bool = False


_retry_transient = retry(
    retry=retry_if_exception_type(SheetRetryableError),
    wait=wait_exponential(multiplier=3, max=60),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    reraise=True,
)


def _safe_div(numerator: float, denominator: float) -> float:
    """Divisão protegida: denominador zero (ou ausente) devolve 0."""
    if not denominator:
        return 0.0
    return numerator / denominator


def build_derived_rows(rows: Sequence[Row]) -> list[Row]:
    """Calcula as métricas derivadas a partir das linhas brutas (25 colunas).

    Toda divisão é protegida contra denominador zero. Um anúncio sem impressões,
    sem cliques ou sem vídeo é comum (anúncio novo, pausado), não é erro.

    ROAS não é calculado de propósito: teríamos apenas a CONTAGEM de compras
    (Omni Purchases), não o valor monetário delas. ROAS = receita / investimento
    exigiria coletar os action_values de compra na Marketing API (campo
    "action_values", com action_type omni_purchase), o que hoje não fazemos.
    """
    derived: list[Row] = [_to_derived_row(row) for row in rows]

    logger.info(
        "Métricas derivadas calculadas",
        extra={"input_rows": len(rows), "rows": len(derived), "columns": COLUMN_COUNT},
    )
    return derived


def _to_derived_row(row: Row) -> Row:
    """Monta uma linha derivada (22 colunas) a partir de uma linha bruta."""
    spend = float(row[_RAW_SPEND] or 0)
    impressions = int(row[_RAW_IMPRESSIONS] or 0)
    link_clicks = int(row[_RAW_LINK_CLICKS] or 0)
    landing_page_views = int(row[_RAW_LANDING_PAGE_VIEWS] or 0)
    leads = int(row[_RAW_LEADS] or 0)
    video_plays = int(row[_RAW_VIDEO_PLAYS] or 0)
    thruplay = int(row[_RAW_THRUPLAY] or 0)
    video_3s = int(row[_RAW_VIDEO_3S] or 0)
    video_100 = int(row[_RAW_VIDEO_100] or 0)
    purchases = int(row[_RAW_OMNI_PURCHASE] or 0)
    conversations = int(row[_RAW_MESSAGING] or 0)

    derived: Row = [
        row[_RAW_DATE],                                  # A Date
        row[_RAW_CAMPAIGN_NAME],                         # B Campaign Name
        row[_RAW_ADSET_NAME],                            # C Adset Name
        row[_RAW_AD_NAME],                               # D Ad Name
        row[_RAW_AD_ID],                                 # E Ad ID
        spend,                                           # F Spend
        impressions,                                     # G Impressions
        link_clicks,                                     # H Link Clicks
        _safe_div(link_clicks, impressions),             # I CTR (Link)
        _safe_div(spend, link_clicks),                   # J CPC (Link)
        _safe_div(spend, impressions) * 1000,            # K CPM
        leads,                                           # L Leads
        _safe_div(spend, leads),                         # M CPL
        landing_page_views,                              # N Landing Page Views
        _safe_div(spend, landing_page_views),            # O Custo por LPV
        _safe_div(video_3s, impressions),                # P Hook Rate
        _safe_div(thruplay, impressions),                # Q Hold Rate
        _safe_div(video_100, video_plays),               # R Video Completion
        purchases,                                       # S Omni Purchases
        _safe_div(spend, purchases),                     # T CPA
        conversations,                                   # U Conversas Iniciadas
        _safe_div(spend, conversations),                 # V Custo por Conversa
    ]

    if len(derived) != COLUMN_COUNT:  # guarda contra edição descuidada da lista acima
        raise ValueError(f"Linha derivada com {len(derived)} colunas; esperado {COLUMN_COUNT}.")
    return derived


def make_derived_key(row: Sequence[Any]) -> Key:
    """Chave de upsert de uma linha derivada: (Data, Ad ID) — colunas A e E.

    A data passa pela mesma normalização usada na leitura da planilha, senão a
    linha recém-calculada ("2026-07-13") jamais casaria com a que já está lá
    (número de série do Sheets) e todo dia viraria um append duplicado.
    """
    return (
        _normalize_key_date(row[_DERIVED_DATE]),
        str(row[_DERIVED_AD_ID] or "").strip(),
    )


class DerivedSheetWriter:
    """Faz upsert na aba de métricas derivadas. Só nela — nunca na aba bruta."""

    def __init__(self, config: Config, service: Any | None = None) -> None:
        _assert_target_is_derived(config)
        self._config = config
        self._credentials = load_credentials(config.google_service_account_json)
        self._service = service or build(
            "sheets", "v4", credentials=self._credentials, cache_discovery=False
        )
        self._spreadsheets = self._service.spreadsheets()
        self._values = self._spreadsheets.values()

    @property
    def _client_email(self) -> str:
        return getattr(self._credentials, "service_account_email", "(desconhecido)")

    @property
    def _sheet_name(self) -> str:
        return self._config.derived_sheet_name

    # -- API pública -------------------------------------------------------

    def upsert(self, raw_rows: Sequence[Row]) -> DerivedSummary:
        """Recalcula a janela atual e faz upsert dela na aba derivada.

        Atualiza as linhas cuja chave (Data, Ad ID) já existe e acrescenta as novas
        ao fim. O histórico fora da janela de coleta não é tocado nem apagado.
        """
        derived_rows = build_derived_rows(raw_rows)
        _validate_rows(derived_rows)
        deduped = _dedupe_by_key(derived_rows)
        sheet = self._sheet_name

        if self._config.dry_run:
            logger.info(
                "DRY_RUN ativo: a aba derivada não foi tocada.",
                extra={
                    "sheet": sheet,
                    "would_write_rows": len(deduped),
                    "would_write_columns": COLUMN_COUNT,
                },
            )
            return DerivedSummary(rows=len(deduped), created_sheet=False, dry_run=True)

        sheet_id, created = self._ensure_sheet()
        self._ensure_header(created)

        existing, rows_read = self._read_key_index()
        last_row = max(
            _FIRST_DATA_ROW - 1 + rows_read, *existing.values(), _FIRST_DATA_ROW - 1
        )

        updates: list[tuple[int, Row]] = []
        appends: list[Row] = []
        for key, row in deduped.items():
            row_number = existing.get(key)
            if row_number is None:
                appends.append(row)
            else:
                updates.append((row_number, row))

        logger.info(
            "Upsert planejado na aba derivada; nenhuma linha antiga é apagada.",
            extra={
                "sheet": sheet,
                "sheet_id": sheet_id,
                "raw_sheet_preserved": self._config.raw_sheet_name,
                "sheet_created": created,
                "rows_in": len(derived_rows),
                "rows_deduped": len(deduped),
                "existing_rows": len(existing),
                "to_update": len(updates),
                "to_append": len(appends),
                "preserved_rows": max(len(existing) - len(updates), 0),
            },
        )

        updated = self._apply_updates(updates)
        appended, append_last_row = self._apply_appends(appends)
        last_row = max(last_row, append_last_row)

        self._apply_formats(sheet_id, last_row)

        logger.info(
            "Upsert concluído na aba derivada",
            extra={
                "sheet": sheet,
                "updated": updated,
                "appended": appended,
                "processed": len(deduped),
                "total_data_rows": max(last_row - _FIRST_DATA_ROW + 1, 0),
                "sheet_created": created,
            },
        )
        return DerivedSummary(
            rows=len(deduped),
            created_sheet=created,
            updated=updated,
            appended=appended,
        )

    # -- Estrutura da aba --------------------------------------------------

    def _ensure_sheet(self) -> tuple[int, bool]:
        """Devolve (sheetId, criada_agora) da aba derivada, criando-a se preciso."""
        sheet_id = self._resolve_sheet_id()
        if sheet_id is not None:
            return sheet_id, False

        logger.info("Aba derivada não existe; criando.", extra={"sheet": self._sheet_name})
        self._batch_update(
            [
                {
                    "addSheet": {
                        "properties": {
                            "title": self._sheet_name,
                            "gridProperties": {
                                "frozenRowCount": 1,
                                "columnCount": COLUMN_COUNT,
                            },
                        }
                    }
                }
            ]
        )

        sheet_id = self._resolve_sheet_id()
        if sheet_id is None:
            raise SheetWriterError(
                f"Criei a aba {self._sheet_name!r}, mas não consegui resolver o sheetId."
            )
        return sheet_id, True

    @_retry_transient
    def _resolve_sheet_id(self) -> int | None:
        """Procura o sheetId da aba derivada pelo título. None se não existir."""
        try:
            response = (
                self._spreadsheets.get(
                    spreadsheetId=self._config.spreadsheet_id,
                    fields="sheets(properties(sheetId,title))",
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "spreadsheets.get") from exc

        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == self._sheet_name:
                return int(properties["sheetId"])
        return None

    def _ensure_header(self, created: bool) -> None:
        """Escreve o cabeçalho na linha 1 apenas se ela estiver vazia.

        Ler antes de escrever evita sobrescrever um cabeçalho que alguém tenha
        renomeado na mão. Em nenhum caso alguma linha de DADOS é tocada aqui: o
        range é A1:V1.
        """
        if not created:
            header_range = f"'{self._sheet_name}'!{_FIRST_COLUMN}1:{_LAST_COLUMN}1"
            current = self._get_values(header_range).get("values", [])
            if current and any(str(cell).strip() for cell in current[0]):
                return

        self._write_header()
        logger.info(
            "Cabeçalho da aba derivada gravado na linha 1.",
            extra={"sheet": self._sheet_name, "sheet_created": created},
        )

    @_retry_transient
    def _write_header(self) -> None:
        """Escreve o cabeçalho em A1. Não alcança nenhuma linha de dados."""
        try:
            self._values.update(
                spreadsheetId=self._config.spreadsheet_id,
                range=f"'{self._sheet_name}'!{_FIRST_COLUMN}1",
                valueInputOption="USER_ENTERED",
                body={"values": [list(HEADER)]},
            ).execute()
        except HttpError as exc:
            raise self._classify(exc, "values.update(header)") from exc

    # -- Leitura -----------------------------------------------------------

    def _read_key_index(self) -> tuple[dict[Key, int], int]:
        """Mapeia (Data, Ad ID) -> número da linha, lendo A2:E da aba derivada.

        Devolve também quantas linhas foram lidas, para saber onde o histórico
        termina sem precisar de uma segunda chamada à API.
        """
        range_ = (
            f"'{self._sheet_name}'!{_FIRST_COLUMN}{_FIRST_DATA_ROW}:{_KEY_LAST_COLUMN}"
        )
        values = self._get_values(range_).get("values", [])

        index: dict[Key, int] = {}
        duplicates = 0
        for offset, row in enumerate(values):
            row_number = _FIRST_DATA_ROW + offset
            date_value = _normalize_key_date(row[0] if row else "")
            ad_id = (
                str(row[_DERIVED_AD_ID]).strip()
                if len(row) > _DERIVED_AD_ID and row[_DERIVED_AD_ID] is not None
                else ""
            )
            if not date_value or not ad_id:
                continue  # linha em branco ou incompleta: não vira chave
            key = (date_value, ad_id)
            if key in index:
                duplicates += 1
                continue  # histórico já duplicado: mantém a primeira ocorrência
            index[key] = row_number

        logger.info(
            "Índice de chaves da aba derivada construído",
            extra={
                "sheet": self._sheet_name,
                "rows_read": len(values),
                "keys": len(index),
                "duplicate_keys": duplicates,
            },
        )
        if duplicates:
            logger.warning(
                "A aba derivada tem chaves (Data, Ad ID) repetidas; a primeira "
                "ocorrência é a que será atualizada.",
                extra={"sheet": self._sheet_name, "duplicate_keys": duplicates},
            )
        return index, len(values)

    @_retry_transient
    def _get_values(self, range_: str) -> dict[str, Any]:
        """Lê um intervalo, devolvendo datas como número de série (locale-independente)."""
        try:
            return (
                self._values.get(
                    spreadsheetId=self._config.spreadsheet_id,
                    range=range_,
                    valueRenderOption="UNFORMATTED_VALUE",
                    dateTimeRenderOption="SERIAL_NUMBER",
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, range_) from exc

    # -- Escrita -----------------------------------------------------------

    def _apply_updates(self, updates: Sequence[tuple[int, Row]]) -> int:
        """Atualiza in-place as linhas existentes, em lotes, via values.batchUpdate.

        Cada range cobre UMA linha (A{n}:V{n}) que a chave identificou, então nenhuma
        linha fora da janela de coleta entra no payload.
        """
        if not updates:
            return 0

        written = 0
        for chunk in _chunked(updates, _UPDATE_CHUNK):
            data = [
                {
                    "range": (
                        f"'{self._sheet_name}'!{_FIRST_COLUMN}{row_number}:"
                        f"{_LAST_COLUMN}{row_number}"
                    ),
                    "values": [row],
                }
                for row_number, row in chunk
            ]
            response = self._values_batch_update(data)
            written += int(response.get("totalUpdatedRows", 0) or 0)
            logger.info(
                "Lote de updates enviado à aba derivada",
                extra={
                    "sheet": self._sheet_name,
                    "ranges": len(data),
                    "updated_rows": written,
                },
            )

        return written

    @_retry_transient
    def _values_batch_update(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """Envia um lote de atualizações de intervalos da aba derivada."""
        try:
            return (
                self._values.batchUpdate(
                    spreadsheetId=self._config.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "values.batchUpdate") from exc

    def _apply_appends(self, appends: Sequence[Row]) -> tuple[int, int]:
        """Insere as linhas novas no fim da aba, em lotes, via values.append.

        Devolve (linhas_inseridas, última_linha_ocupada); a última linha serve só
        para saber até onde aplicar os number formats.
        """
        if not appends:
            return 0, _FIRST_DATA_ROW - 1

        written = 0
        last_row = _FIRST_DATA_ROW - 1
        for chunk in _chunked(appends, _APPEND_CHUNK):
            response = self._append(chunk)
            updates = response.get("updates", {})
            written += int(updates.get("updatedRows", 0) or 0)
            last_row = max(last_row, _end_row(updates.get("updatedRange", "")))
            logger.info(
                "Lote de appends enviado à aba derivada",
                extra={
                    "sheet": self._sheet_name,
                    "rows": len(chunk),
                    "appended_rows": written,
                    "range": updates.get("updatedRange"),
                },
            )

        return written, last_row

    @_retry_transient
    def _append(self, rows: Sequence[Row]) -> dict[str, Any]:
        """Acrescenta linhas ao fim da aba derivada, sem sobrescrever nada."""
        try:
            return (
                self._values.append(
                    spreadsheetId=self._config.spreadsheet_id,
                    range=f"'{self._sheet_name}'!{_FIRST_COLUMN}{_FIRST_DATA_ROW}",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [list(row) for row in rows]},
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "values.append") from exc

    def _apply_formats(self, sheet_id: int, last_row: int) -> None:
        """Aplica os number formats (data, moeda, porcentagem) só na aba derivada.

        Cobre todas as linhas de dados, inclusive as históricas: repeatCell de
        numberFormat muda apenas a APARÊNCIA da célula, nunca o conteúdo.
        """
        if last_row < _FIRST_DATA_ROW:
            return

        requests: list[dict[str, Any]] = [
            _format_request(sheet_id, _DATE_COLUMN, last_row, "DATE", _DATE_FORMAT)
        ]
        requests += [
            _format_request(sheet_id, column, last_row, "PERCENT", _PERCENT_FORMAT)
            for column in _PERCENT_COLUMNS
        ]
        requests += [
            _format_request(sheet_id, column, last_row, "NUMBER", _MONEY_FORMAT)
            for column in _MONEY_COLUMNS
        ]

        self._batch_update(requests)
        logger.info(
            "Number formats aplicados na aba derivada",
            extra={
                "sheet": self._sheet_name,
                "sheet_id": sheet_id,
                "requests": len(requests),
                "last_row": last_row,
            },
        )

    @_retry_transient
    def _batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Requests estruturais. Todos carregam o sheetId da aba derivada."""
        try:
            return (
                self._spreadsheets.batchUpdate(
                    spreadsheetId=self._config.spreadsheet_id,
                    body={"requests": requests},
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "spreadsheets.batchUpdate") from exc

    def _classify(self, exc: HttpError, context: str) -> SheetWriterError:
        return classify_http_error(
            exc, context, self._config.spreadsheet_id, self._client_email
        )


def _end_row(range_: str) -> int:
    """Extrai a última linha de um range A1 ("'Aba'!A15:V17" -> 17). 0 se não casar."""
    match = _RANGE_END_ROW.search(range_ or "")
    return int(match.group(1)) if match else 0


def _validate_rows(rows: Sequence[Row]) -> None:
    """Garante que toda linha derivada tem exatamente 22 colunas antes de escrever."""
    for position, row in enumerate(rows):
        if len(row) != COLUMN_COUNT:
            raise SheetWriterError(
                f"Linha derivada {position} tem {len(row)} colunas; a aba exige "
                f"{COLUMN_COUNT} (A..V). Nada foi escrito."
            )


def _dedupe_by_key(rows: Sequence[Row]) -> dict[Key, Row]:
    """Colapsa linhas derivadas com a mesma chave (Data, Ad ID); a última vence.

    Sem isso, duas linhas com a mesma chave virariam dois appends e criariam a
    duplicata que o upsert existe para evitar.
    """
    deduped: dict[Key, Row] = {}
    for row in rows:
        deduped[make_derived_key(row)] = list(row)

    dropped = len(rows) - len(deduped)
    if dropped:
        logger.warning(
            "Linhas derivadas com chave (Data, Ad ID) repetida no lote; mantida a última.",
            extra={"dropped": dropped},
        )
    return deduped


def _format_request(
    sheet_id: int, column: int, last_row: int, format_type: str, pattern: str
) -> dict[str, Any]:
    """Monta um repeatCell de number format para uma coluna da aba derivada."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,  # sempre a aba derivada
                "startRowIndex": _FIRST_DATA_ROW - 1,  # linha 1 é cabeçalho (texto)
                "endRowIndex": last_row,
                "startColumnIndex": column,
                "endColumnIndex": column + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "numberFormat": {"type": format_type, "pattern": pattern}
                }
            },
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _assert_target_is_derived(config: Config) -> None:
    """Impede que a aba derivada aponte para a aba bruta.

    As duas abas têm larguras diferentes (22 x 25 colunas) e a chave mora em colunas
    diferentes (E x J). Sem esta guarda, DERIVED_SHEET_NAME == RAW_SHEET_NAME faria
    este módulo sobrescrever linhas da aba bruta com linhas derivadas, destruindo o
    histórico que é fonte de outras abas da planilha.
    """
    derived = config.derived_sheet_name.strip()
    raw = config.raw_sheet_name.strip()
    if not derived:
        raise SheetWriterError("DERIVED_SHEET_NAME está vazia.")
    if derived.casefold() == raw.casefold():
        raise SheetWriterError(
            f"DERIVED_SHEET_NAME ({derived!r}) é igual a RAW_SHEET_NAME ({raw!r}). "
            "As duas abas têm formatos diferentes; apontá-las para o mesmo lugar "
            "sobrescreveria as linhas da aba bruta e destruiria o histórico. "
            "Corrija a configuração."
        )
