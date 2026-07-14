"""Escrita idempotente (upsert) na aba bruta do Google Sheets."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Iterator, Sequence

from google.auth.exceptions import GoogleAuthError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Config
from src.logger import get_logger
from src.transform import COLUMN_COUNT, Key, Row, make_key

logger = get_logger(__name__)

SCOPES: Final[tuple[str, ...]] = ("https://www.googleapis.com/auth/spreadsheets",)

# A aba vai de A a Y (25 colunas). A linha 1 é o cabeçalho e nunca é tocada.
_FIRST_COLUMN: Final[str] = "A"
_LAST_COLUMN: Final[str] = "Y"
_KEY_LAST_COLUMN: Final[str] = "J"  # basta ler A..J para montar a chave (date, ad_id)
_FIRST_DATA_ROW: Final[int] = 2

# Limites de lote. A API aceita mais, mas payloads grandes estouram o timeout.
_UPDATE_CHUNK: Final[int] = 200
_APPEND_CHUNK: Final[int] = 1000

_MAX_ATTEMPTS: Final[int] = 5

# Epoch do Sheets: o dia 0 é 30/12/1899.
_SHEETS_EPOCH: Final[date] = date(1899, 12, 30)

_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class SheetWriterError(RuntimeError):
    """Falha permanente ao falar com o Google Sheets."""


class SheetPermissionError(SheetWriterError):
    """A service account não tem acesso de edição à planilha."""


class SheetRetryableError(SheetWriterError):
    """Falha temporária (quota, indisponibilidade). Elegível a retry."""


@dataclass(frozen=True, slots=True)
class WriteSummary:
    """Resultado de um upsert."""

    updated: int
    appended: int
    processed: int
    dry_run: bool = False


def _log_retry(state: RetryCallState) -> None:
    """Loga cada nova tentativa com o tempo de espera aplicado."""
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "Erro temporário no Google Sheets; tentando novamente.",
        extra={
            "attempt": state.attempt_number,
            "sleep_seconds": round(getattr(state.next_action, "sleep", 0.0), 1),
            "error": str(exc),
        },
    )


_retry_transient = retry(
    retry=retry_if_exception_type(SheetRetryableError),
    wait=wait_exponential(multiplier=3, max=60),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    before_sleep=_log_retry,
    reraise=True,
)


def load_credentials(raw: str) -> Credentials:
    """Constrói as credenciais a partir de GOOGLE_SERVICE_ACCOUNT_JSON.

    Aceita três formas: caminho de arquivo, JSON puro ou JSON em base64.
    """
    info = _parse_service_account(raw)
    try:
        return Credentials.from_service_account_info(info, scopes=list(SCOPES))
    except (GoogleAuthError, ValueError) as exc:
        raise SheetWriterError(
            "GOOGLE_SERVICE_ACCOUNT_JSON não é uma credencial de service account válida: "
            f"{exc}"
        ) from exc


def _parse_service_account(raw: str) -> dict[str, Any]:
    """Resolve o conteúdo da env em um dict, detectando o formato."""
    value = (raw or "").strip()
    if not value:
        raise SheetWriterError("GOOGLE_SERVICE_ACCOUNT_JSON está vazia.")

    # 1) Caminho de arquivo.
    if not value.startswith("{") and os.path.isfile(value):
        logger.debug("Credencial lida de arquivo", extra={"path": value})
        try:
            with open(value, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetWriterError(
                f"Não consegui ler o JSON da service account em {value!r}: {exc}"
            ) from exc

    # 2) JSON puro.
    if value.startswith("{"):
        try:
            logger.debug("Credencial lida como JSON puro")
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SheetWriterError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON parece JSON, mas não é válido: {exc}"
            ) from exc

    # 3) Base64 de um JSON.
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        logger.debug("Credencial lida como base64")
        return json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SheetWriterError(
            "GOOGLE_SERVICE_ACCOUNT_JSON não é um caminho existente, nem JSON, "
            f"nem base64 de um JSON: {exc}"
        ) from exc


def _serial_to_date(serial: float) -> str:
    """Converte o número de série de data do Sheets em 'yyyy-mm-dd'."""
    return (_SHEETS_EPOCH + timedelta(days=int(serial))).isoformat()


def _normalize_key_date(value: Any) -> str:
    """Normaliza a coluna A lida da planilha para 'yyyy-mm-dd'.

    Escrita com USER_ENTERED, a data vira uma data de verdade na célula, e a
    leitura devolve o número de série (não a string original). Sem converter de
    volta, nenhuma chave casaria e todo dia viraria append duplicado.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _serial_to_date(value)

    text = str(value or "").strip()
    if not text:
        return ""

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    return text  # conteúdo inesperado: preserva como veio, sem casar chave por engano


def _chunked(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Fatia a sequência em blocos de no máximo `size` elementos."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def classify_http_error(
    exc: HttpError, context: str, spreadsheet_id: str, client_email: str
) -> SheetWriterError:
    """Traduz um HttpError do Sheets na exceção interna correspondente."""
    status = getattr(exc.resp, "status", None)
    detail = f"status={status} context={context!r} message={exc}"

    if status in (401, 403):
        logger.error(
            "Acesso negado à planilha. Compartilhe a planilha com a service "
            "account, dando permissão de Editor.",
            extra={
                "service_account": client_email,
                "spreadsheet_id": spreadsheet_id,
                "error": detail,
            },
        )
        return SheetPermissionError(
            f"Sem permissão de escrita na planilha {spreadsheet_id}. "
            f"Compartilhe-a como Editor com {client_email}."
        )

    if status == 404:
        logger.error(
            "Planilha não encontrada. Confira SPREADSHEET_ID.",
            extra={"spreadsheet_id": spreadsheet_id, "error": detail},
        )
        return SheetWriterError(
            f"Planilha {spreadsheet_id!r} não encontrada (404). "
            "Confira o SPREADSHEET_ID."
        )

    if status == 400:
        # Aba inexistente cai aqui ("Unable to parse range").
        return SheetWriterError(
            f"Requisição rejeitada pelo Sheets. Verifique se a aba existe e se o "
            f"intervalo é válido. {detail}"
        )

    if status in _RETRYABLE_STATUS:
        return SheetRetryableError(f"Erro temporário do Sheets: {detail}")

    return SheetWriterError(f"Erro do Google Sheets: {detail}")


class SheetWriter:
    """Escreve linhas na aba de destino preservando o histórico existente.

    Nunca apaga linhas, nunca reordena, nunca toca em outras abas nem na linha 1.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._credentials = load_credentials(config.google_service_account_json)
        self._service = build(
            "sheets",
            "v4",
            credentials=self._credentials,
            cache_discovery=False,
        )
        self._values = self._service.spreadsheets().values()

    @property
    def _client_email(self) -> str:
        """E-mail da service account, usado nas mensagens de erro de permissão."""
        return getattr(self._credentials, "service_account_email", "(desconhecido)")

    @property
    def service(self) -> Any:
        """Cliente autenticado do Sheets, para reuso (evita uma 2ª autenticação)."""
        return self._service

    # -- API pública -------------------------------------------------------

    def upsert(self, rows: Sequence[Row], sheet_name: str | None = None) -> WriteSummary:
        """Atualiza as linhas cuja chave (date, ad_id) já existe e insere o resto.

        Devolve o resumo com atualizadas, inseridas e total processado.
        """
        sheet = sheet_name or self._config.raw_sheet_name
        _validate_rows(rows)

        deduped = _dedupe_by_key(rows)
        existing = self._read_key_index(sheet)

        updates: list[tuple[int, Row]] = []
        appends: list[Row] = []
        for key, row in deduped.items():
            row_number = existing.get(key)
            if row_number is None:
                appends.append(row)
            else:
                updates.append((row_number, row))

        logger.info(
            "Upsert planejado",
            extra={
                "sheet": sheet,
                "rows_in": len(rows),
                "rows_deduped": len(deduped),
                "to_update": len(updates),
                "to_append": len(appends),
                "existing_rows": len(existing),
            },
        )

        if self._config.dry_run:
            logger.info(
                "DRY_RUN ativo: nada foi escrito na planilha.",
                extra={
                    "sheet": sheet,
                    "would_update": len(updates),
                    "would_append": len(appends),
                },
            )
            return WriteSummary(
                updated=len(updates),
                appended=len(appends),
                processed=len(deduped),
                dry_run=True,
            )

        updated = self._apply_updates(sheet, updates)
        appended = self._apply_appends(sheet, appends)

        summary = WriteSummary(
            updated=updated, appended=appended, processed=len(deduped)
        )
        logger.info(
            "Upsert concluído",
            extra={
                "sheet": sheet,
                "updated": summary.updated,
                "appended": summary.appended,
                "processed": summary.processed,
            },
        )
        return summary

    def read_rows(self, sheet_name: str | None = None) -> list[list[Any]]:
        """Lê as linhas de dados da aba (A2:Y), para conferência pós-gravação.

        As datas voltam como número de série do Sheets; quem consome normaliza.
        """
        sheet = sheet_name or self._config.raw_sheet_name
        range_ = f"'{sheet}'!{_FIRST_COLUMN}{_FIRST_DATA_ROW}:{_LAST_COLUMN}"
        values = self._get_values(range_).get("values", [])

        logger.info("Aba relida para validação", extra={"sheet": sheet, "rows": len(values)})
        return values

    # -- Leitura -----------------------------------------------------------

    def _read_key_index(self, sheet: str) -> dict[Key, int]:
        """Mapeia (date, ad_id) -> número da linha, lendo A2:J da aba."""
        range_ = (
            f"'{sheet}'!{_FIRST_COLUMN}{_FIRST_DATA_ROW}:{_KEY_LAST_COLUMN}"
        )
        response = self._get_values(range_)
        values = response.get("values", [])

        index: dict[Key, int] = {}
        duplicates = 0
        for offset, row in enumerate(values):
            row_number = _FIRST_DATA_ROW + offset
            date_value = _normalize_key_date(row[0] if row else "")
            ad_id = str(row[9]).strip() if len(row) > 9 and row[9] is not None else ""
            if not date_value or not ad_id:
                continue  # linha em branco ou incompleta: não vira chave
            key = (date_value, ad_id)
            if key in index:
                duplicates += 1
                continue  # histórico já duplicado: mantém a primeira ocorrência
            index[key] = row_number

        logger.info(
            "Índice de chaves construído",
            extra={
                "sheet": sheet,
                "rows_read": len(values),
                "keys": len(index),
                "duplicate_keys": duplicates,
            },
        )
        if duplicates:
            logger.warning(
                "A aba tem chaves (date, ad_id) repetidas; a primeira ocorrência é a "
                "que será atualizada.",
                extra={"sheet": sheet, "duplicate_keys": duplicates},
            )
        return index

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

    def _apply_updates(self, sheet: str, updates: Sequence[tuple[int, Row]]) -> int:
        """Atualiza in-place as linhas existentes, em lotes, via values.batchUpdate."""
        if not updates:
            return 0

        written = 0
        for chunk in _chunked(updates, _UPDATE_CHUNK):
            data = [
                {
                    "range": (
                        f"'{sheet}'!{_FIRST_COLUMN}{row_number}:"
                        f"{_LAST_COLUMN}{row_number}"
                    ),
                    "values": [row],
                }
                for row_number, row in chunk
            ]
            response = self._batch_update(data)
            written += int(response.get("totalUpdatedRows", 0) or 0)
            logger.info(
                "Lote de updates enviado",
                extra={"sheet": sheet, "ranges": len(data), "updated_rows": written},
            )

        return written

    @_retry_transient
    def _batch_update(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """Envia um lote de atualizações de intervalos."""
        try:
            return (
                self._values.batchUpdate(
                    spreadsheetId=self._config.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "batchUpdate") from exc

    def _apply_appends(self, sheet: str, appends: Sequence[Row]) -> int:
        """Insere as linhas novas no fim da aba, em lotes, via values.append."""
        if not appends:
            return 0

        written = 0
        for chunk in _chunked(appends, _APPEND_CHUNK):
            response = self._append(sheet, chunk)
            updates = response.get("updates", {})
            written += int(updates.get("updatedRows", 0) or 0)
            logger.info(
                "Lote de appends enviado",
                extra={
                    "sheet": sheet,
                    "rows": len(chunk),
                    "appended_rows": written,
                    "range": updates.get("updatedRange"),
                },
            )

        return written

    @_retry_transient
    def _append(self, sheet: str, rows: Sequence[Row]) -> dict[str, Any]:
        """Acrescenta linhas ao fim da aba, sem sobrescrever nada."""
        try:
            return (
                self._values.append(
                    spreadsheetId=self._config.spreadsheet_id,
                    range=f"'{sheet}'!{_FIRST_COLUMN}{_FIRST_DATA_ROW}",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [list(row) for row in rows]},
                )
                .execute()
            )
        except HttpError as exc:
            raise self._classify(exc, "append") from exc

    # -- Erros -------------------------------------------------------------

    def _classify(self, exc: HttpError, context: str) -> SheetWriterError:
        """Traduz um HttpError na exceção interna correspondente."""
        return classify_http_error(
            exc, context, self._config.spreadsheet_id, self._client_email
        )


def _validate_rows(rows: Sequence[Row]) -> None:
    """Garante que toda linha tem exatamente 25 colunas antes de escrever."""
    for position, row in enumerate(rows):
        if len(row) != COLUMN_COUNT:
            raise SheetWriterError(
                f"Linha {position} tem {len(row)} colunas; a aba exige {COLUMN_COUNT} "
                "(A..Y). Nada foi escrito."
            )


def _dedupe_by_key(rows: Sequence[Row]) -> dict[Key, Row]:
    """Colapsa linhas com a mesma chave (date, ad_id); a última vence.

    Sem isso, duas linhas com a mesma chave virariam dois appends e criariam a
    duplicata que o upsert existe para evitar.
    """
    deduped: dict[Key, Row] = {}
    for row in rows:
        deduped[make_key(row)] = list(row)

    dropped = len(rows) - len(deduped)
    if dropped:
        logger.warning(
            "Linhas com chave (date, ad_id) repetida no lote; mantida a última.",
            extra={"dropped": dropped},
        )
    return deduped
