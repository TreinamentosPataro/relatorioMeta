"""Testes do backfill da aba derivada a partir da aba bruta."""

from __future__ import annotations

from typing import Any

from src.backfill import EXIT_OK, run, sanitize_raw_rows
from src.derived import COLUMN_COUNT as DERIVED_COLUMN_COUNT
from src.derived import build_derived_rows
from src.transform import COLUMN_COUNT as RAW_COLUMN_COUNT
from tests.test_sheet_writer import make_config


def raw_sheet_row(**kwargs: Any) -> list[Any]:
    """Linha como o Sheets a devolve: 25 colunas, data em serial."""
    row: list[Any] = [""] * RAW_COLUMN_COUNT
    row[0] = kwargs.get("date", 46216)  # 2026-07-13 em serial
    row[9] = kwargs.get("ad_id", "ad1")
    row[11] = kwargs.get("spend", 10.0)
    row[12] = kwargs.get("impressions", 1000)
    row[13] = kwargs.get("link_clicks", 20)
    return row


# -- Saneamento das linhas lidas da planilha -------------------------------


def test_data_em_serial_vira_iso() -> None:
    rows = sanitize_raw_rows([raw_sheet_row(date=46216)])

    assert rows[0][0] == "2026-07-13"


def test_data_em_texto_e_preservada() -> None:
    rows = sanitize_raw_rows([raw_sheet_row(date="2026-07-13")])

    assert rows[0][0] == "2026-07-13"


def test_linha_curta_e_completada_ate_25_colunas() -> None:
    """O Sheets corta as células vazias do fim; sem completar, dá IndexError."""
    curta: list[Any] = [46216, "", "", "", "", "", "", "", "", "ad1", "", 5.0]

    rows = sanitize_raw_rows([curta])

    assert len(rows[0]) == RAW_COLUMN_COUNT
    assert rows[0][9] == "ad1"


def test_linha_longa_e_truncada() -> None:
    longa = raw_sheet_row() + ["lixo", "mais lixo"]

    rows = sanitize_raw_rows([longa])

    assert len(rows[0]) == RAW_COLUMN_COUNT


def test_linha_sem_data_e_descartada() -> None:
    assert sanitize_raw_rows([raw_sheet_row(date="")]) == []


def test_linha_sem_ad_id_e_descartada() -> None:
    assert sanitize_raw_rows([raw_sheet_row(ad_id="")]) == []


def test_celula_de_texto_onde_se_espera_numero_vira_zero() -> None:
    """Uma célula editada à mão não pode matar o backfill inteiro."""
    rows = sanitize_raw_rows([raw_sheet_row(spend="n/d", impressions="—")])

    assert rows[0][11] == 0.0
    assert rows[0][12] == 0.0


def test_lista_vazia() -> None:
    assert sanitize_raw_rows([]) == []


def test_linha_saneada_atravessa_o_calculo_real_sem_quebrar() -> None:
    """O ponto de integração: sanitize devolve floats, e o cálculo faz int()."""
    rows = build_derived_rows(
        sanitize_raw_rows(
            [
                raw_sheet_row(date=46216, spend=100.0, impressions=10_000, link_clicks=200),
                raw_sheet_row(date="", ad_id="x"),  # descartada antes do cálculo
                raw_sheet_row(date=46217, spend="n/d", impressions="—"),
            ]
        )
    )

    assert len(rows) == 2
    assert all(len(row) == DERIVED_COLUMN_COUNT for row in rows)
    assert rows[0][0] == "2026-07-13"
    assert rows[0][6] == 10_000 and isinstance(rows[0][6], int)
    assert rows[0][8] == 200 / 10_000       # CTR calculado normalmente
    assert rows[1][5] == 0.0 and rows[1][8] == 0.0  # célula suja virou zero, sem explodir


# -- Execução --------------------------------------------------------------


class FakeSheetWriter:
    instances: list["FakeSheetWriter"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.service = object()
        FakeSheetWriter.instances.append(self)

    def read_rows(self, sheet_name: str | None = None) -> list[list[Any]]:
        return [
            raw_sheet_row(date=46216, ad_id="ad1"),
            raw_sheet_row(date=46217, ad_id="ad1"),
            raw_sheet_row(date="", ad_id="ad9"),  # lixo: sem data
        ]


class FakeSummary:
    rows = 2
    updated = 0
    appended = 2
    dry_run = False


class FakeDerivedWriter:
    instances: list["FakeDerivedWriter"] = []

    def __init__(self, config: Any, service: Any = None) -> None:
        self.config = config
        self.service = service
        self.upserted: list[list[Any]] = []
        FakeDerivedWriter.instances.append(self)

    def upsert(self, rows: list[list[Any]]) -> FakeSummary:
        self.upserted = rows
        return FakeSummary()


def install_fakes(monkeypatch: Any) -> None:
    FakeSheetWriter.instances.clear()
    FakeDerivedWriter.instances.clear()
    import src.backfill as backfill_module

    monkeypatch.setattr(backfill_module, "SheetWriter", FakeSheetWriter)
    monkeypatch.setattr(backfill_module, "DerivedSheetWriter", FakeDerivedWriter)


def test_le_a_aba_bruta_e_faz_upsert_na_derivada(monkeypatch: Any) -> None:
    install_fakes(monkeypatch)

    assert run(make_config()) == EXIT_OK

    derived = FakeDerivedWriter.instances[0]
    assert len(derived.upserted) == 2, "a linha sem data foi descartada"
    assert [row[0] for row in derived.upserted] == ["2026-07-13", "2026-07-14"]


def test_reusa_o_cliente_autenticado(monkeypatch: Any) -> None:
    """Uma autenticação só; a segunda seria desperdício."""
    install_fakes(monkeypatch)

    run(make_config())

    assert FakeDerivedWriter.instances[0].service is FakeSheetWriter.instances[0].service


def test_as_linhas_entregues_tem_a_largura_da_aba_bruta(monkeypatch: Any) -> None:
    """O DerivedSheetWriter espera 25 colunas e devolve 22."""
    install_fakes(monkeypatch)

    run(make_config())

    for row in FakeDerivedWriter.instances[0].upserted:
        assert len(row) == RAW_COLUMN_COUNT
    assert DERIVED_COLUMN_COUNT == 22


def test_aba_bruta_vazia_encerra_sem_escrever(monkeypatch: Any) -> None:
    install_fakes(monkeypatch)
    monkeypatch.setattr(FakeSheetWriter, "read_rows", lambda self, sheet_name=None: [])

    assert run(make_config()) == EXIT_OK
    assert FakeDerivedWriter.instances == [], "nem instanciar o writer da derivada"


def test_dry_run_e_repassado(monkeypatch: Any) -> None:
    """Quem respeita o DRY_RUN é o upsert; o backfill só não pode atrapalhar."""
    install_fakes(monkeypatch)

    run(make_config(dry_run=True))

    assert FakeDerivedWriter.instances[0].config.dry_run is True
