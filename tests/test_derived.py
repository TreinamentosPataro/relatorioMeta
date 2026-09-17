"""Testes das métricas derivadas e do upsert na aba derivada."""

from __future__ import annotations

import re
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


# -- Escrita: upsert, só na aba derivada, sem apagar histórico -------------

DERIVED = "Meta ADS - Métricas"
RAW = "Meta ADS"

_RANGE = re.compile(
    r"^'(?P<sheet>[^']*)'!(?P<c1>[A-Z]+)(?P<r1>[0-9]+)(?::(?P<c2>[A-Z]+)(?P<r2>[0-9]*))?$"
)


def _col_index(letters: str) -> int:
    """'A' -> 0, 'E' -> 4, 'V' -> 21."""
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def derived_row(date: Any, ad_id: str, spend: float = 1.0) -> list[Any]:
    """Linha derivada de 22 colunas, como a que já mora na planilha."""
    row: list[Any] = [0] * 22
    row[0] = date
    row[1] = "Campanha Velha"
    row[4] = ad_id
    row[5] = spend
    return row


class FakeCall:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeValues:
    """Simula a aba de verdade: linha 1 é o cabeçalho, dados a partir da linha 2.

    Guardar o conteúdo (e não só as chamadas) é o que permite afirmar que uma
    linha histórica continuou byte a byte igual depois da execução.
    """

    def __init__(self, grid: list[list[Any]] | None = None) -> None:
        self.grid: list[list[Any]] = [list(row) for row in (grid or [])]
        self.clears: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.batch_updates: list[dict[str, Any]] = []
        self.appends: list[dict[str, Any]] = []

    # -- helpers internos --

    def _row(self, number: int) -> list[Any]:
        while len(self.grid) < number:
            self.grid.append([])
        return self.grid[number - 1]

    def _write(self, range_: str, values: list[list[Any]]) -> int:
        parts = _RANGE.match(range_)
        assert parts is not None, "range inesperado: " + range_
        start_row = int(parts.group("r1"))
        start_col = _col_index(parts.group("c1"))
        for offset, row in enumerate(values):
            target = self._row(start_row + offset)
            while len(target) < start_col + len(row):
                target.append("")
            target[start_col : start_col + len(row)] = list(row)
        return len(values)

    def _last_filled_row(self) -> int:
        for number in range(len(self.grid), 0, -1):
            if any(str(cell).strip() for cell in self.grid[number - 1]):
                return number
        return 0

    # -- API dublada --

    def clear(self, **kwargs: Any) -> FakeCall:
        self.clears.append(kwargs)
        raise AssertionError("A aba derivada jamais pode ser limpa.")

    def get(self, **kwargs: Any) -> FakeCall:
        parts = _RANGE.match(kwargs["range"])
        assert parts is not None, "range inesperado: " + kwargs["range"]
        start_row = int(parts.group("r1"))
        end_row = int(parts.group("r2") or 0) or len(self.grid)
        start_col = _col_index(parts.group("c1"))
        end_col = _col_index(parts.group("c2") or parts.group("c1"))

        rows: list[list[Any]] = []
        for number in range(start_row, end_row + 1):
            if number > len(self.grid):
                break
            rows.append(list(self.grid[number - 1][start_col : end_col + 1]))
        while rows and not any(str(cell).strip() for cell in rows[-1]):
            rows.pop()  # o Sheets não devolve as linhas vazias do fim
        return FakeCall({"values": rows} if rows else {})

    def update(self, **kwargs: Any) -> FakeCall:
        self.updates.append(kwargs)
        self._write(kwargs["range"], kwargs["body"]["values"])
        return FakeCall({})

    def batchUpdate(self, **kwargs: Any) -> FakeCall:  # noqa: N802 (nome da API)
        self.batch_updates.append(kwargs)
        total = 0
        for item in kwargs["body"]["data"]:
            total += self._write(item["range"], item["values"])
        return FakeCall({"totalUpdatedRows": total})

    def append(self, **kwargs: Any) -> FakeCall:
        self.appends.append(kwargs)
        rows = kwargs["body"]["values"]
        first = max(self._last_filled_row() + 1, 2)
        self._write("'" + DERIVED + "'!A" + str(first), rows)
        last = first + len(rows) - 1
        updated_range = "'" + DERIVED + "'!A" + str(first) + ":V" + str(last)
        return FakeCall(
            {"updates": {"updatedRows": len(rows), "updatedRange": updated_range}}
        )


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
            assert "deleteDimension" not in request, "jamais apagar linhas"
            assert "deleteRange" not in request, "jamais apagar células"
            assert "deleteSheet" not in request, "jamais apagar a aba"
            if "addSheet" in request:  # simula a criação: passa a existir
                self._sheets.append(
                    {
                        "properties": {
                            "sheetId": 999,
                            "title": request["addSheet"]["properties"]["title"],
                        }
                    }
                )
        return FakeCall({})

    def values(self) -> FakeValues:
        return self._values


class FakeService:
    def __init__(self, spreadsheets: FakeSpreadsheets) -> None:
        self._spreadsheets = spreadsheets

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


def existing_sheets() -> list[dict[str, Any]]:
    return [
        {"properties": {"sheetId": 0, "title": RAW}},
        {"properties": {"sheetId": 77, "title": DERIVED}},
    ]


def make_writer(
    sheets: list[dict[str, Any]] | None = None,
    grid: list[list[Any]] | None = None,
    config: Any = None,
) -> tuple[DerivedSheetWriter, FakeSpreadsheets, FakeValues]:
    """DerivedSheetWriter sem rede nem credencial real."""
    values = FakeValues(grid)
    spreadsheets = FakeSpreadsheets(
        existing_sheets() if sheets is None else sheets, values
    )
    writer = object.__new__(DerivedSheetWriter)
    writer._config = config or make_config()
    writer._credentials = type(
        "C", (), {"service_account_email": SERVICE_ACCOUNT["client_email"]}
    )()
    writer._service = FakeService(spreadsheets)
    writer._spreadsheets = spreadsheets
    writer._values = values
    return writer, spreadsheets, values


def populated_grid() -> list[list[Any]]:
    """Aba com cabeçalho e três dias de histórico já gravados."""
    return [
        list(HEADER),                                  # linha 1
        derived_row("2026-01-02", "ad1", spend=5.0),   # linha 2
        derived_row("2026-01-03", "ad1", spend=6.0),   # linha 3
        derived_row("2026-07-13", "ad1", spend=7.0),   # linha 4
    ]


# -- A garantia central: nada é apagado -----------------------------------


def test_a_aba_nunca_e_limpa() -> None:
    """values.clear explode no dublê; passar significa que ninguém o chamou."""
    writer, _, values = make_writer(grid=populated_grid())

    writer.upsert([make_raw(date="2026-07-14", ad_id="ad1", spend=9.0)])

    assert values.clears == []


def test_historico_fora_da_janela_e_preservado_intacto() -> None:
    """As linhas de janeiro não vieram na coleta e não podem ser tocadas."""
    writer, _, values = make_writer(grid=populated_grid())
    antes = [list(values.grid[1]), list(values.grid[2])]

    writer.upsert(
        [
            make_raw(date="2026-07-13", ad_id="ad1", spend=70.0),
            make_raw(date="2026-07-14", ad_id="ad1", spend=80.0),
        ]
    )

    assert values.grid[1] == antes[0], "a linha de 2026-01-02 foi alterada"
    assert values.grid[2] == antes[1], "a linha de 2026-01-03 foi alterada"
    assert len(values.grid) == 5, "histórico preservado + 1 linha nova"


def test_nenhum_range_de_escrita_alcanca_linha_fora_da_coleta() -> None:
    """Todo range escrito aponta para uma linha que a coleta atual trouxe."""
    writer, _, values = make_writer(grid=populated_grid())

    writer.upsert([make_raw(date="2026-07-13", ad_id="ad1", spend=70.0)])

    escritos = [
        item["range"] for lote in values.batch_updates for item in lote["body"]["data"]
    ]
    assert escritos == ["'" + DERIVED + "'!A4:V4"], "só a linha de 2026-07-13"
    assert values.appends == [], "nada novo a inserir"


# -- Update x append -------------------------------------------------------


def test_chave_existente_e_atualizada_no_lugar() -> None:
    writer, _, values = make_writer(grid=populated_grid())

    summary = writer.upsert([make_raw(date="2026-07-13", ad_id="ad1", spend=70.0)])

    assert (summary.updated, summary.appended) == (1, 0)
    assert values.grid[3][0] == "2026-07-13"  # continua na linha 4
    assert values.grid[3][5] == 70.0          # Spend atualizado
    assert len(values.grid) == 4              # nenhuma linha criada


def test_chave_nova_vai_para_o_fim() -> None:
    writer, _, values = make_writer(grid=populated_grid())

    summary = writer.upsert([make_raw(date="2026-07-14", ad_id="ad9", spend=3.0)])

    assert (summary.updated, summary.appended) == (0, 1)
    assert values.grid[4][0] == "2026-07-14"
    assert values.grid[4][4] == "ad9"
    assert values.appends[0]["insertDataOption"] == "INSERT_ROWS"


def test_mistura_de_update_e_append_no_mesmo_lote() -> None:
    writer, _, values = make_writer(grid=populated_grid())

    summary = writer.upsert(
        [
            make_raw(date="2026-07-13", ad_id="ad1", spend=70.0),  # já existe
            make_raw(date="2026-07-13", ad_id="ad2", spend=20.0),  # novo
            make_raw(date="2026-07-14", ad_id="ad1", spend=30.0),  # novo
        ]
    )

    assert (summary.updated, summary.appended, summary.rows) == (1, 2, 3)
    assert len(values.grid) == 6  # 4 linhas + 2 acréscimos


def test_mesma_chave_duas_vezes_no_lote_nao_duplica() -> None:
    writer, _, values = make_writer(grid=populated_grid())

    summary = writer.upsert(
        [
            make_raw(date="2026-07-14", ad_id="ad1", spend=1.0),
            make_raw(date="2026-07-14", ad_id="ad1", spend=2.0),  # a última vence
        ]
    )

    assert (summary.rows, summary.appended) == (1, 1)
    assert values.grid[4][5] == 2.0


def test_data_gravada_como_serial_ainda_casa_a_chave() -> None:
    """Gravada com USER_ENTERED, a data volta da planilha como número de série."""
    grid = populated_grid()
    grid[3][0] = 46216  # 2026-07-13 em serial do Sheets
    writer, _, values = make_writer(grid=grid)

    summary = writer.upsert([make_raw(date="2026-07-13", ad_id="ad1", spend=70.0)])

    assert (summary.updated, summary.appended) == (1, 0), "o serial tem de casar"
    assert len(values.grid) == 4


def test_execucao_repetida_e_idempotente() -> None:
    writer, _, values = make_writer(grid=populated_grid())
    lote = [make_raw(date="2026-07-14", ad_id="ad1", spend=9.0)]

    writer.upsert(lote)
    altura = len(values.grid)
    writer.upsert(lote)

    assert len(values.grid) == altura, "a 2ª execução não pode criar linha nova"


def test_aba_vazia_recebe_cabecalho_e_dados() -> None:
    writer, _, values = make_writer(grid=[])

    writer.upsert([make_raw(date="2026-07-13", ad_id="ad1")])

    assert values.grid[0] == list(HEADER)
    assert values.grid[1][4] == "ad1"


def test_cabecalho_existente_nao_e_reescrito() -> None:
    writer, _, values = make_writer(grid=populated_grid())

    writer.upsert([make_raw(date="2026-07-13", ad_id="ad1")])

    assert values.updates == [], "o cabeçalho já estava lá; não reescrever"


# -- Isolamento e estrutura ------------------------------------------------


def test_escreve_apenas_na_aba_derivada() -> None:
    """Nenhum range de valor e nenhum sheetId estrutural aponta para a aba bruta."""
    writer, spreadsheets, values = make_writer(grid=populated_grid())

    writer.upsert(
        [
            make_raw(date="2026-07-13", ad_id="ad1", spend=10.0),
            make_raw(date="2026-07-20", ad_id="ad5", spend=10.0),
        ]
    )

    ranges = (
        [item["range"] for lote in values.batch_updates for item in lote["body"]["data"]]
        + [update["range"] for update in values.updates]
        + [append["range"] for append in values.appends]
    )
    assert ranges, "o teste precisa ter exercitado alguma escrita"
    for range_ in ranges:
        assert range_.startswith("'" + DERIVED + "'!"), "escrita fora da derivada"

    for update in spreadsheets.batch_updates:
        for request in update["body"]["requests"]:
            sheet_id = request["repeatCell"]["range"]["sheetId"]
            assert sheet_id == 77, "formato aplicado fora da aba derivada"
            assert sheet_id != 0, "jamais tocar no sheetId da aba bruta"


def test_cria_a_aba_se_nao_existir() -> None:
    writer, spreadsheets, values = make_writer(
        sheets=[{"properties": {"sheetId": 0, "title": RAW}}], grid=[]
    )

    summary = writer.upsert([make_raw()])

    assert summary.created_sheet is True
    add = spreadsheets.batch_updates[0]["body"]["requests"][0]["addSheet"]
    assert add["properties"]["title"] == DERIVED
    assert values.grid[0] == list(HEADER)


def test_formatos_cobrem_todo_o_historico_e_poupam_o_cabecalho() -> None:
    writer, spreadsheets, _ = make_writer(grid=populated_grid())

    writer.upsert([make_raw(date="2026-07-14", ad_id="ad1")])  # vira a linha 5

    formatos: dict[int, str] = {}
    for update in spreadsheets.batch_updates:
        for request in update["body"]["requests"]:
            rng = request["repeatCell"]["range"]
            cell = request["repeatCell"]["cell"]
            formatos[rng["startColumnIndex"]] = cell["userEnteredFormat"]["numberFormat"][
                "pattern"
            ]
            assert rng["startRowIndex"] == 1, "o cabeçalho não é número"
            assert rng["endRowIndex"] == 5, "o formato deve cobrir o histórico inteiro"

    assert formatos[0] == "yyyy-mm-dd"   # Date
    assert formatos[8] == "0.00%"        # CTR
    assert formatos[15] == "0.00%"       # Hook Rate
    assert formatos[17] == "0.00%"       # Video Completion
    assert formatos[5] == "#,##0.00"     # Spend
    assert formatos[19] == "#,##0.00"    # CPA


def test_dry_run_nao_toca_na_planilha() -> None:
    writer, spreadsheets, values = make_writer(
        grid=populated_grid(), config=make_config(dry_run=True)
    )

    summary = writer.upsert([make_raw(), make_raw(ad_id="ad2")])

    assert (summary.rows, summary.dry_run) == (2, True)
    assert values.updates == [] and values.batch_updates == [] and values.appends == []
    assert spreadsheets.batch_updates == []
    assert spreadsheets.gets == 0, "DRY_RUN nem deve consultar a estrutura"


def test_derivada_igual_a_bruta_e_recusada() -> None:
    """A guarda que impede escrever linhas derivadas por cima da aba bruta."""
    config = make_config(derived_sheet_name="Meta ADS", raw_sheet_name="Meta ADS")

    with pytest.raises(SheetWriterError, match="destruiria o histórico"):
        DerivedSheetWriter(config)


def test_derivada_igual_a_bruta_ignora_maiusculas() -> None:
    config = make_config(derived_sheet_name="meta ads", raw_sheet_name="Meta ADS")

    with pytest.raises(SheetWriterError, match="destruiria o histórico"):
        DerivedSheetWriter(config)
