"""Testes do upsert no Google Sheets, com o serviço da API dublado."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from src.config import Config
from src.sheet_writer import (
    SheetPermissionError,
    SheetWriter,
    SheetWriterError,
    _normalize_key_date,
    _parse_service_account,
)

SERVICE_ACCOUNT = {"type": "service_account", "client_email": "bot@projeto.iam.gserviceaccount.com"}


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "meta_app_id": "x",
        "meta_app_secret": "x",
        "meta_access_token": "x",
        "meta_ad_account_id": "act_1",
        "google_service_account_json": json.dumps(SERVICE_ACCOUNT),
        "spreadsheet_id": "sheet-123",
        "lookback_days": 7,
        "account_timezone": "America/Sao_Paulo",
        "raw_sheet_name": "Meta ADS",
        "derived_sheet_name": "Meta ADS - Métricas",
        "log_level": "INFO",
        "dry_run": False,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_row(date: str, ad_id: str, spend: float = 1.0) -> list[Any]:
    """Linha completa de 25 colunas (A..Y)."""
    row: list[Any] = [""] * 25
    row[0] = date
    row[9] = ad_id
    row[11] = spend
    return row


class FakeCall:
    def __init__(self, result: Any, error: Exception | None = None) -> None:
        self._result, self._error = result, error

    def execute(self) -> Any:
        if self._error:
            raise self._error
        return self._result


class FakeValues:
    """Dublê de spreadsheets().values(): registra as chamadas recebidas."""

    def __init__(self, existing: list[list[Any]] | None = None, error: Exception | None = None):
        self.existing = existing or []
        self.error = error
        self.get_calls: list[dict[str, Any]] = []
        self.batch_updates: list[dict[str, Any]] = []
        self.appends: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> FakeCall:
        self.get_calls.append(kwargs)
        return FakeCall({"values": self.existing}, self.error)

    def batchUpdate(self, **kwargs: Any) -> FakeCall:  # noqa: N802 (nome da API)
        self.batch_updates.append(kwargs)
        rows = len(kwargs["body"]["data"])
        return FakeCall({"totalUpdatedRows": rows}, self.error)

    def append(self, **kwargs: Any) -> FakeCall:
        self.appends.append(kwargs)
        rows = len(kwargs["body"]["values"])
        return FakeCall({"updates": {"updatedRows": rows, "updatedRange": "A9:Y9"}}, self.error)


def make_writer(values: FakeValues, config: Config | None = None) -> SheetWriter:
    """SheetWriter sem rede: injeta o dublê no lugar do serviço autenticado."""
    writer = object.__new__(SheetWriter)
    writer._config = config or make_config()
    writer._credentials = type("C", (), {"service_account_email": SERVICE_ACCOUNT["client_email"]})()
    writer._service = None
    writer._values = values
    return writer


# -- Credenciais -----------------------------------------------------------


def test_credencial_aceita_json_puro() -> None:
    assert _parse_service_account(json.dumps(SERVICE_ACCOUNT)) == SERVICE_ACCOUNT


def test_credencial_aceita_base64() -> None:
    encoded = base64.b64encode(json.dumps(SERVICE_ACCOUNT).encode()).decode()
    assert _parse_service_account(encoded) == SERVICE_ACCOUNT


def test_credencial_aceita_caminho_de_arquivo(tmp_path: Any) -> None:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(SERVICE_ACCOUNT), encoding="utf-8")
    assert _parse_service_account(str(path)) == SERVICE_ACCOUNT


def test_credencial_invalida_falha_com_mensagem_clara() -> None:
    with pytest.raises(SheetWriterError, match="nem base64"):
        _parse_service_account("isto-nao-e-credencial")


# -- Normalização da data lida ---------------------------------------------


def test_data_lida_como_numero_de_serie_vira_iso() -> None:
    """USER_ENTERED grava data de verdade; a leitura devolve o serial."""
    assert _normalize_key_date(46216) == "2026-07-13"


def test_data_lida_como_texto_iso_ou_br_vira_iso() -> None:
    assert _normalize_key_date("2026-07-13") == "2026-07-13"
    assert _normalize_key_date("13/07/2026") == "2026-07-13"


# -- Upsert ----------------------------------------------------------------


def test_chave_existente_atualiza_e_nova_insere() -> None:
    """ad1/13-07 já existe na linha 5: vira update. ad2 é novo: vira append."""
    existing = [
        [46215] + [""] * 8 + ["ad1"],  # linha 2: 2026-07-12
        [46216] + [""] * 8 + ["ad1"],  # linha 3: 2026-07-13
    ]
    values = FakeValues(existing)
    writer = make_writer(values)

    summary = writer.upsert(
        [make_row("2026-07-13", "ad1", 50.0), make_row("2026-07-13", "ad2", 70.0)]
    )

    assert (summary.updated, summary.appended, summary.processed) == (1, 1, 2)

    data = values.batch_updates[0]["body"]["data"]
    assert len(data) == 1
    assert data[0]["range"] == "'Meta ADS'!A3:Y3"  # a linha exata da chave existente
    assert data[0]["values"][0][11] == 50.0

    assert values.appends[0]["body"]["values"] == [make_row("2026-07-13", "ad2", 70.0)]


def test_nunca_escreve_na_linha_1_nem_fora_da_aba() -> None:
    """O cabeçalho (linha 1) e as outras abas ficam intocados."""
    existing = [[46216] + [""] * 8 + ["ad1"]]
    values = FakeValues(existing)
    writer = make_writer(values)

    writer.upsert([make_row("2026-07-13", "ad1"), make_row("2026-07-14", "ad9")])

    assert values.get_calls[0]["range"] == "'Meta ADS'!A2:J"  # leitura começa na linha 2

    for update in values.batch_updates:
        for item in update["body"]["data"]:
            assert item["range"].startswith("'Meta ADS'!")
            linha = int(item["range"].split("!A")[1].split(":")[0])
            assert linha >= 2, "jamais escrever na linha 1 (cabeçalho)"

    for append in values.appends:
        assert append["range"].startswith("'Meta ADS'!")
        assert append["insertDataOption"] == "INSERT_ROWS"  # não sobrescreve nada


def test_usa_user_entered_para_a_data_virar_data() -> None:
    values = FakeValues([[46216] + [""] * 8 + ["ad1"]])
    writer = make_writer(values)

    writer.upsert([make_row("2026-07-13", "ad1"), make_row("2026-07-14", "ad2")])

    assert values.batch_updates[0]["body"]["valueInputOption"] == "USER_ENTERED"
    assert values.appends[0]["valueInputOption"] == "USER_ENTERED"


def test_leitura_pede_serial_number_e_nao_valor_formatado() -> None:
    """Sem isso, a data voltaria no formato do locale e nenhuma chave casaria."""
    values = FakeValues()
    writer = make_writer(values)

    writer.upsert([make_row("2026-07-13", "ad1")])

    assert values.get_calls[0]["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert values.get_calls[0]["dateTimeRenderOption"] == "SERIAL_NUMBER"


def test_reexecucao_do_mesmo_dia_nao_duplica() -> None:
    """A 2ª execução do dia atualiza in-place em vez de acrescentar."""
    row = make_row("2026-07-13", "ad1", 10.0)

    primeira = FakeValues([])  # aba vazia
    writer = make_writer(primeira)
    assert writer.upsert([row]).appended == 1

    # A aba agora contém a linha gravada (serial, como a API devolve).
    segunda = FakeValues([[46216] + [""] * 8 + ["ad1"]])
    writer = make_writer(segunda)
    summary = writer.upsert([make_row("2026-07-13", "ad1", 99.0)])

    assert (summary.updated, summary.appended) == (1, 0)
    assert segunda.appends == [], "não pode haver append na reexecução"


def test_linhas_duplicadas_no_lote_sao_colapsadas() -> None:
    """Mesma chave duas vezes no mesmo lote: a última vence, um único append."""
    values = FakeValues()
    writer = make_writer(values)

    summary = writer.upsert(
        [make_row("2026-07-13", "ad1", 10.0), make_row("2026-07-13", "ad1", 20.0)]
    )

    assert (summary.appended, summary.processed) == (1, 1)
    assert values.appends[0]["body"]["values"] == [make_row("2026-07-13", "ad1", 20.0)]


def test_linha_incompleta_e_recusada_antes_de_escrever() -> None:
    values = FakeValues()
    writer = make_writer(values)

    with pytest.raises(SheetWriterError, match="24 colunas"):
        writer.upsert([[""] * 24])

    assert values.batch_updates == [] and values.appends == []


def test_linhas_em_branco_no_historico_nao_viram_chave() -> None:
    """Linha vazia ou sem ad_id na aba é ignorada pelo índice."""
    existing = [[], [46216] + [""] * 8 + [""], [46216] + [""] * 8 + ["ad1"]]
    values = FakeValues(existing)
    writer = make_writer(values)

    summary = writer.upsert([make_row("2026-07-13", "ad1")])

    assert summary.updated == 1
    assert values.batch_updates[0]["body"]["data"][0]["range"] == "'Meta ADS'!A4:Y4"


# -- DRY_RUN e erros -------------------------------------------------------


def test_dry_run_nao_escreve_nada() -> None:
    values = FakeValues([[46216] + [""] * 8 + ["ad1"]])
    writer = make_writer(values, make_config(dry_run=True))

    summary = writer.upsert([make_row("2026-07-13", "ad1"), make_row("2026-07-14", "ad2")])

    assert (summary.updated, summary.appended, summary.dry_run) == (1, 1, True)
    assert values.batch_updates == [], "DRY_RUN não pode escrever"
    assert values.appends == [], "DRY_RUN não pode escrever"


def test_erro_403_pede_compartilhar_como_editor() -> None:
    from googleapiclient.errors import HttpError

    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    values = FakeValues(error=HttpError(resp, b'{"error": {"message": "forbidden"}}'))
    writer = make_writer(values)

    with pytest.raises(SheetPermissionError, match="Editor"):
        writer.upsert([make_row("2026-07-13", "ad1")])


def _http_error(status: int) -> Any:
    from googleapiclient.errors import HttpError

    resp = type("R", (), {"status": status, "reason": "x"})()
    return HttpError(resp, b'{"error": {"message": "erro"}}')


def test_classificacao_de_erros_http() -> None:
    """Cada status vira a exceção certa, com a mensagem certa."""
    from src.sheet_writer import (
        SheetRetryableError,
        SheetWriterError,
        classify_http_error,
    )

    email = "bot@x.iam.gserviceaccount.com"

    assert isinstance(
        classify_http_error(_http_error(401), "get", "sid", email), SheetPermissionError
    )
    assert isinstance(
        classify_http_error(_http_error(403), "get", "sid", email), SheetPermissionError
    )
    for status in (429, 500, 502, 503, 504):
        exc = classify_http_error(_http_error(status), "get", "sid", email)
        assert isinstance(exc, SheetRetryableError), f"{status} deveria ser retryable"

    nao_encontrada = classify_http_error(_http_error(404), "get", "sid", email)
    assert isinstance(nao_encontrada, SheetWriterError)
    assert "SPREADSHEET_ID" in str(nao_encontrada)


def test_rate_limit_persistente_acaba_levantando(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 em toda tentativa: após esgotar o retry, a exceção sobe (job falha)."""
    from src.sheet_writer import SheetRetryableError

    values = FakeValues(error=_http_error(429))
    writer = make_writer(values)
    # Neutraliza a espera do backoff para o teste rodar instantâneo.
    monkeypatch.setattr(writer._get_values.retry, "sleep", lambda _s: None)

    with pytest.raises(SheetRetryableError, match="temporário"):
        writer.upsert([make_row("2026-07-13", "ad1")])

    assert len(values.get_calls) >= 2, "tem de ter retentado antes de desistir"


def test_updates_sao_enviados_em_lotes() -> None:
    """Mais de 200 updates viram mais de um batchUpdate (limite de escrita)."""
    existing = [[46216] + [""] * 8 + [f"ad{i}"] for i in range(250)]
    values = FakeValues(existing)
    writer = make_writer(values)

    rows = [make_row("2026-07-13", f"ad{i}") for i in range(250)]
    summary = writer.upsert(rows)

    assert summary.updated == 250
    assert len(values.batch_updates) == 2, "250 updates em lotes de 200 = 2 chamadas"
    assert values.appends == []
