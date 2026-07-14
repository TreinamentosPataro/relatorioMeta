"""Aba de métricas derivadas: reconstruída do zero a cada execução.

CONTRATO DE ESCRITA — leia antes de mexer neste arquivo.

Este módulo escreve EXCLUSIVAMENTE na aba DERIVED_SHEET_NAME ("Meta ADS - Métricas").
Ele JAMAIS escreve na aba bruta ("Meta ADS") nem em qualquer outra aba da planilha:

  * todo range de valores é qualificado com o nome da aba derivada;
  * todo request estrutural (limpeza de formato, número, etc.) carrega o sheetId da
    aba derivada, resolvido em tempo de execução por _resolve_sheet_id();
  * _assert_target_is_derived() aborta a execução se DERIVED_SHEET_NAME coincidir com
    RAW_SHEET_NAME — sem essa guarda, uma configuração errada apagaria o histórico.

A aba bruta é fonte de outras abas da planilha; destruí-la seria irreversível.

Os valores gravados são NÚMEROS, não fórmulas: fórmulas do tipo =Meta_ADS!L2/M2 quebram
quando as linhas da aba bruta são reordenadas ou inseridas. Aqui, o número é calculado em
Python e a planilha só o exibe.
"""

from __future__ import annotations

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
    classify_http_error,
    load_credentials,
)
from src.transform import Row

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

# Índices (0-based) na linha derivada, para os number formats.
_PERCENT_COLUMNS: Final[tuple[int, ...]] = (8, 15, 16, 17)  # CTR, Hook, Hold, Completion
_MONEY_COLUMNS: Final[tuple[int, ...]] = (5, 9, 10, 12, 14, 19, 21)  # Spend, CPC, CPM, CPL, LPV, CPA, Conversa
_DATE_COLUMN: Final[int] = 0

_PERCENT_FORMAT: Final[str] = "0.00%"
_MONEY_FORMAT: Final[str] = "#,##0.00"
_DATE_FORMAT: Final[str] = "yyyy-mm-dd"

_MAX_ATTEMPTS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class DerivedSummary:
    """Resultado da reconstrução da aba derivada."""

    rows: int
    created_sheet: bool
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


class DerivedSheetWriter:
    """Reconstrói a aba de métricas derivadas. Só ela — nunca a aba bruta."""

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

    def rebuild(self, raw_rows: Sequence[Row]) -> DerivedSummary:
        """Recalcula e reescreve a aba derivada inteira. Idempotente."""
        derived_rows = build_derived_rows(raw_rows)
        sheet = self._sheet_name

        if self._config.dry_run:
            logger.info(
                "DRY_RUN ativo: a aba derivada não foi tocada.",
                extra={
                    "sheet": sheet,
                    "would_write_rows": len(derived_rows),
                    "would_write_columns": COLUMN_COUNT,
                },
            )
            return DerivedSummary(rows=len(derived_rows), created_sheet=False, dry_run=True)

        sheet_id, created = self._ensure_sheet()
        logger.info(
            "Reconstruindo APENAS a aba derivada; a aba bruta não é tocada.",
            extra={
                "sheet": sheet,
                "sheet_id": sheet_id,
                "raw_sheet_preserved": self._config.raw_sheet_name,
                "sheet_created": created,
                "rows": len(derived_rows),
            },
        )

        self._clear_sheet()
        self._write_values([list(HEADER), *derived_rows])
        self._apply_formats(sheet_id, len(derived_rows))

        logger.info(
            "Aba derivada reconstruída",
            extra={"sheet": sheet, "rows": len(derived_rows), "sheet_created": created},
        )
        return DerivedSummary(rows=len(derived_rows), created_sheet=created)

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

    # -- Escrita -----------------------------------------------------------

    @_retry_transient
    def _clear_sheet(self) -> None:
        """Limpa a aba derivada inteira. O range é qualificado: só ela é afetada."""
        try:
            self._values.clear(
                spreadsheetId=self._config.spreadsheet_id,
                range=f"'{self._sheet_name}'",
                body={},
            ).execute()
        except HttpError as exc:
            raise self._classify(exc, "values.clear") from exc

    @_retry_transient
    def _write_values(self, values: list[Row]) -> None:
        """Escreve cabeçalho + linhas a partir de A1 da aba derivada."""
        try:
            self._values.update(
                spreadsheetId=self._config.spreadsheet_id,
                range=f"'{self._sheet_name}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise self._classify(exc, "values.update") from exc

    def _apply_formats(self, sheet_id: int, row_count: int) -> None:
        """Aplica os number formats (data, moeda, porcentagem) só na aba derivada."""
        if not row_count:
            return

        requests: list[dict[str, Any]] = [
            _format_request(sheet_id, _DATE_COLUMN, row_count, "DATE", _DATE_FORMAT)
        ]
        requests += [
            _format_request(sheet_id, column, row_count, "PERCENT", _PERCENT_FORMAT)
            for column in _PERCENT_COLUMNS
        ]
        requests += [
            _format_request(sheet_id, column, row_count, "NUMBER", _MONEY_FORMAT)
            for column in _MONEY_COLUMNS
        ]

        self._batch_update(requests)
        logger.info(
            "Number formats aplicados na aba derivada",
            extra={"sheet": self._sheet_name, "sheet_id": sheet_id, "requests": len(requests)},
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


def _format_request(
    sheet_id: int, column: int, row_count: int, format_type: str, pattern: str
) -> dict[str, Any]:
    """Monta um repeatCell de number format para uma coluna da aba derivada."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,  # sempre a aba derivada
                "startRowIndex": 1,  # linha 1 é cabeçalho (texto)
                "endRowIndex": 1 + row_count,
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

    Sem esta guarda, DERIVED_SHEET_NAME == RAW_SHEET_NAME faria o clear+rewrite
    apagar o histórico bruto, que é fonte de outras abas da planilha.
    """
    derived = config.derived_sheet_name.strip()
    raw = config.raw_sheet_name.strip()
    if not derived:
        raise SheetWriterError("DERIVED_SHEET_NAME está vazia.")
    if derived.casefold() == raw.casefold():
        raise SheetWriterError(
            f"DERIVED_SHEET_NAME ({derived!r}) é igual a RAW_SHEET_NAME ({raw!r}). "
            "A aba derivada é apagada e reescrita a cada execução; apontá-la para a "
            "aba bruta destruiria o histórico. Corrija a configuração."
        )
