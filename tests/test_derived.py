"""Testes das métricas derivadas e da reconstrução da aba."""

from __future__ import annotations

from typing import Any

import pytest

from src.derived import (
    COLUMN_COUNT,
    HEADER,
    DerivedSheetWriter,
    build_derived_rows,
)
from src.sheet_writer import SheetWriterError
from tests.test_sheet_writer import SERVICE_ACCOUNT, make_config


def make_raw(**kwargs: Any) -> list[Any]:
    """Linha bruta de 25 colunas (A..Y) do transform, com os campos relevantes."""
    row: list[Any] = [""] * 25
    row[0] = kwargs.get("date", "2026-07-13")
    row[4] = kwargs.get("campaign", "Campanha A")
    row[6] = kwargs.get("adset", "Conjunto A")
    row[8] = kwargs.get("ad_name", "Anúncio A")
    row[9] = kwargs.get("ad_id", "ad1")
    row[11] = kwargs.get("spend", 0.0)
    row[12] = kwargs.get("impressions", 0)
    row[13] = kwargs.get("link_clicks", 0)
    row[14] = kwargs.get("lpv", 0)
    row[15] = kwargs.get("leads", 0)
    row[16] = kwargs.get("video_plays", 0)
    row[17] = kwargs.get("thruplay", 0)
    row[18] = kwargs.get("video_3s", 0)
    row[22] = kwargs.get("video_100", 0)
    row[23] = kwargs.get("purchases", 0)
    row[24] = kwargs.get("conversations", 0)
    return row


# -- Cálculos --------------------------------------------------------------


def test_cabecalho_tem_22_colunas_na_ordem_pedida() -> None:
    assert len(HEADER) == 22 == COLUMN_COUNT
    assert HEADER[0] == "Date"
    assert HEADER[8] == "CTR (Link)"
    assert HEADER[17] == "Video Completion (100%/Plays)"
    assert HEADER[21] == "Custo por Conversa"


def test_nao_existe_coluna_de_roas() -> None:
    """ROAS exigiria action_values de compra, que não coletamos."""
    assert not any("ROAS" in nome.upper() for nome in HEADER)


def test_todos_os_calculos_com_denominadores_validos() -> None:
    row = build_derived_rows(
        [
            make_raw(
                spend=100.0,
                impressions=10_000,
                link_clicks=200,
                lpv=50,
                leads=10,
                video_plays=1_000,
                thruplay=300,
                video_3s=600,
                video_100=250,
                purchases=5,
                conversations=8,
            )
        ]
    )[0]

    assert len(row) == 22
    assert row[:5] == ["2026-07-13", "Campanha A", "Conjunto A", "Anúncio A", "ad1"]
    assert row[5] == 100.0                     # F Spend
    assert row[6] == 10_000                    # G Impressions
    assert row[7] == 200                       # H Link Clicks
    assert row[8] == 200 / 10_000              # I CTR = 0.02 (fração, não 2)
    assert row[9] == 100.0 / 200               # J CPC = 0.5
    assert row[10] == 100.0 / 10_000 * 1000    # K CPM = 10.0
    assert row[11] == 10                       # L Leads
    assert row[12] == 100.0 / 10                # M CPL = 10.0
    assert row[13] == 50                       # N LPV
    assert row[14] == 100.0 / 50               # O Custo por LPV = 2.0
    assert row[15] == 600 / 10_000             # P Hook Rate = 0.06
    assert row[16] == 300 / 10_000             # Q Hold Rate = 0.03
    assert row[17] == 250 / 1_000              # R Completion = 0.25
    assert row[18] == 5                        # S Purchases
    assert row[19] == 100.0 / 5                # T CPA = 20.0
    assert row[20] == 8                        # U Conversas
    assert row[21] == 100.0 / 8                # V Custo por Conversa = 12.5


def test_percentuais_sao_fracao_e_nao_valor_ja_multiplicado() -> None:
    """CTR de 2% grava 0.02; a planilha formata como porcentagem."""
    row = build_derived_rows([make_raw(impressions=100, link_clicks=2)])[0]

    assert row[8] == 0.02


def test_denominador_zero_nunca_explode_e_vira_zero() -> None:
    """Anúncio novo/pausado: sem impressões, cliques, vídeo ou conversões."""
    row = build_derived_rows([make_raw(spend=50.0)])[0]

    for index, nome in [
        (8, "CTR"),
        (9, "CPC"),
        (10, "CPM"),
        (12, "CPL"),
        (14, "Custo por LPV"),
        (15, "Hook Rate"),
        (16, "Hold Rate"),
        (17, "Video Completion"),
        (19, "CPA"),
        (21, "Custo por Conversa"),
    ]:
        assert row[index] == 0, f"{nome} deveria ser 0 com denominador zero"


def test_linha_totalmente_zerada_nao_explode() -> None:
    row = build_derived_rows([make_raw()])[0]

    assert len(row) == 22
    assert all(valor == 0 for valor in row[5:])


def test_spend_sem_conversao_gera_cpa_zero_nao_infinito() -> None:
    """Gastou e não converteu: CPA 0 (e não divisão por zero)."""
    row = build_derived_rows([make_raw(spend=300.0, impressions=5000, purchases=0)])[0]

    assert row[19] == 0
    assert row[5] == 300.0


def test_lista_vazia() -> None:
    assert build_derived_rows([]) == []


# -- Escrita: só a aba derivada -------------------------------------------


class FakeCall:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeValues:
    def __init__(self) -> None:
        self.clears: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def clear(self, **kwargs: Any) -> FakeCall:
        self.clears.append(kwargs)
        return FakeCall({})

    def update(self, **kwargs: Any) -> FakeCall:
        self.updates.append(kwargs)
        return FakeCall({})


class FakeSpreadsheets:
    def __init__(self, sheets: list[dict[str, Any]], values: FakeValues) -> None:
        self._sheets = sheets
        self._values = values
        self.batch_updates: list[dict[str, Any]] = []
        self.gets = 0

    def get(self, **kwargs: Any) -> FakeCall:
        self.gets += 1
        return FakeCall({"sheets": self._sheets})

    def batchUpdate(self, **kwargs: Any) -> FakeCall:  # noqa: N802 (nome da API)
        self.batch_updates.append(kwargs)
        for request in kwargs["body"]["requests"]:
            if "addSheet" in request:  # simula a criação: passa a existir
                self._sheets.append(
                    {"properties": {"sheetId": 999, "title": request["addSheet"]["properties"]["title"]}}
                )
        return FakeCall({})

    def values(self) -> FakeValues:
        return self._values


class FakeService:
    def __init__(self, spreadsheets: FakeSpreadsheets) -> None:
        self._spreadsheets = spreadsheets

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


def make_writer(sheets: list[dict[str, Any]], config: Any = None) -> tuple[DerivedSheetWriter, FakeSpreadsheets, FakeValues]:
    """DerivedSheetWriter sem rede nem credencial real."""
    values = FakeValues()
    spreadsheets = FakeSpreadsheets(sheets, values)
    writer = object.__new__(DerivedSheetWriter)
    writer._config = config or make_config()
    writer._credentials = type("C", (), {"service_account_email": SERVICE_ACCOUNT["client_email"]})()
    writer._service = FakeService(spreadsheets)
    writer._spreadsheets = spreadsheets
    writer._values = values
    return writer, spreadsheets, values


DERIVED = "Meta ADS - Métricas"
RAW = "Meta ADS"


def existing_sheets() -> list[dict[str, Any]]:
    return [
        {"properties": {"sheetId": 0, "title": RAW}},
        {"properties": {"sheetId": 77, "title": DERIVED}},
    ]


def test_escreve_apenas_na_aba_derivada() -> None:
    """Nenhum range de valor e nenhum sheetId estrutural aponta para a aba bruta."""
    writer, spreadsheets, values = make_writer(existing_sheets())

    writer.rebuild([make_raw(spend=10.0, impressions=100, link_clicks=5)])

    assert values.clears[0]["range"] == f"'{DERIVED}'"
    assert values.updates[0]["range"] == f"'{DERIVED}'!A1"

    for update in spreadsheets.batch_updates:
        for request in update["body"]["requests"]:
            sheet_id = request["repeatCell"]["range"]["sheetId"]
            assert sheet_id == 77, "formato aplicado fora da aba derivada"
            assert sheet_id != 0, "jamais tocar no sheetId da aba bruta"


def test_cria_a_aba_se_nao_existir() -> None:
    writer, spreadsheets, _ = make_writer([{"properties": {"sheetId": 0, "title": RAW}}])

    summary = writer.rebuild([make_raw()])

    assert summary.created_sheet is True
    add = spreadsheets.batch_updates[0]["body"]["requests"][0]["addSheet"]
    assert add["properties"]["title"] == DERIVED


def test_aba_existente_e_limpa_e_reescrita_com_cabecalho() -> None:
    writer, _, values = make_writer(existing_sheets())

    writer.rebuild([make_raw(ad_id="ad1"), make_raw(ad_id="ad2")])

    assert len(values.clears) == 1
    escrito = values.updates[0]["body"]["values"]
    assert escrito[0] == list(HEADER)   # linha 1 é o cabeçalho
    assert len(escrito) == 3            # cabeçalho + 2 linhas
    assert values.updates[0]["valueInputOption"] == "USER_ENTERED"


def test_formatos_de_numero_aplicados_nas_colunas_certas() -> None:
    writer, spreadsheets, _ = make_writer(existing_sheets())

    writer.rebuild([make_raw()])

    formatos: dict[int, str] = {}
    for update in spreadsheets.batch_updates:
        for request in update["body"]["requests"]:
            rng = request["repeatCell"]["range"]
            pattern = request["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"]["pattern"]
            formatos[rng["startColumnIndex"]] = pattern
            assert rng["startRowIndex"] == 1, "o cabeçalho não pode ser formatado como número"

    assert formatos[0] == "yyyy-mm-dd"   # Date
    assert formatos[8] == "0.00%"        # CTR
    assert formatos[15] == "0.00%"       # Hook Rate
    assert formatos[17] == "0.00%"       # Video Completion
    assert formatos[5] == "#,##0.00"     # Spend
    assert formatos[19] == "#,##0.00"    # CPA


def test_dry_run_nao_toca_na_planilha() -> None:
    writer, spreadsheets, values = make_writer(existing_sheets(), make_config(dry_run=True))

    summary = writer.rebuild([make_raw(), make_raw(ad_id="ad2")])

    assert (summary.rows, summary.dry_run) == (2, True)
    assert values.clears == [] and values.updates == []
    assert spreadsheets.batch_updates == []
    assert spreadsheets.gets == 0, "DRY_RUN nem deve consultar a estrutura"


def test_derivada_igual_a_bruta_e_recusada() -> None:
    """A guarda que impede apagar o histórico por configuração errada."""
    config = make_config(derived_sheet_name="Meta ADS", raw_sheet_name="Meta ADS")

    with pytest.raises(SheetWriterError, match="destruiria o histórico"):
        DerivedSheetWriter(config)


def test_derivada_igual_a_bruta_ignora_maiusculas() -> None:
    config = make_config(derived_sheet_name="meta ads", raw_sheet_name="Meta ADS")

    with pytest.raises(SheetWriterError, match="destruiria o histórico"):
        DerivedSheetWriter(config)
