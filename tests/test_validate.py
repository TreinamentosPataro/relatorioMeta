"""Testes das checagens de qualidade pós-gravação."""

from __future__ import annotations

from typing import Any

import pytest

from src.validate import (
    ValidationError,
    check_expected_rows_present,
    check_no_duplicate_keys,
    check_row_count,
    check_row_types,
    validate_pending_rows,
    validate_sheet,
)

# Serial do Sheets para 2026-07-13 e 2026-07-14 (como a API devolve a data).
SERIAL_13 = 46216
SERIAL_14 = 46217


def sheet_row(date: Any, ad_id: str, spend: float = 10.0, **metrics: Any) -> list[Any]:
    """Linha como o Sheets a devolve (25 colunas, data como serial)."""
    row: list[Any] = [""] * 25
    row[0] = date
    row[9] = ad_id
    row[11] = spend
    for index in range(12, 25):
        row[index] = 0
    for index, value in metrics.items():
        row[int(index.removeprefix("c"))] = value
    return row


def written_row(date: str, ad_id: str) -> list[Any]:
    """Linha como o transform a produz (data em texto ISO)."""
    return sheet_row(date, ad_id)


# -- Duplicatas ------------------------------------------------------------


def test_chaves_duplicadas_reprovam() -> None:
    rows = [sheet_row(SERIAL_13, "ad1"), sheet_row(SERIAL_13, "ad1")]

    check = check_no_duplicate_keys(rows)

    assert check.passed is False
    assert check.critical is True
    assert "2026-07-13/ad1" in check.detail


def test_mesmo_anuncio_em_dias_diferentes_nao_e_duplicata() -> None:
    rows = [sheet_row(SERIAL_13, "ad1"), sheet_row(SERIAL_14, "ad1")]

    assert check_no_duplicate_keys(rows).passed is True


def test_data_em_serial_e_em_texto_sao_a_mesma_chave() -> None:
    """A aba guarda serial; o lote traz texto ISO. Precisam colidir."""
    rows = [sheet_row(SERIAL_13, "ad1"), sheet_row("2026-07-13", "ad1")]

    assert check_no_duplicate_keys(rows).passed is False


def test_linhas_em_branco_nao_contam_como_duplicata() -> None:
    rows = [[], [""] * 25, sheet_row(SERIAL_13, "ad1")]

    assert check_no_duplicate_keys(rows).passed is True


# -- Linhas esperadas ------------------------------------------------------


def test_linha_esperada_ausente_reprova() -> None:
    sheet = [sheet_row(SERIAL_13, "ad1")]
    esperadas = [("2026-07-13", "ad1"), ("2026-07-13", "ad2")]

    check = check_expected_rows_present(sheet, esperadas)

    assert check.passed is False
    assert "2026-07-13/ad2" in check.detail


def test_todas_as_linhas_esperadas_presentes_aprova() -> None:
    sheet = [sheet_row(SERIAL_13, "ad1"), sheet_row(SERIAL_13, "ad2")]
    esperadas = [("2026-07-13", "ad1")]

    assert check_expected_rows_present(sheet, esperadas).passed is True


# -- Tipos -----------------------------------------------------------------


def test_data_invalida_reprova() -> None:
    check = check_row_types([sheet_row("13 de julho", "ad1")])

    assert check.passed is False
    assert "Date inválida" in check.detail


def test_spend_nao_numerico_reprova() -> None:
    check = check_row_types([sheet_row(SERIAL_13, "ad1", spend="muito caro")])

    assert check.passed is False
    assert "não numérico" in check.detail


def test_spend_negativo_reprova() -> None:
    check = check_row_types([sheet_row(SERIAL_13, "ad1", spend=-5.0)])

    assert check.passed is False
    assert "negativo" in check.detail


def test_metrica_negativa_reprova() -> None:
    row = sheet_row(SERIAL_13, "ad1")
    row[12] = -100  # impressions negativas

    check = check_row_types([row])

    assert check.passed is False
    assert "métrica negativa" in check.detail


def test_linha_saudavel_aprova() -> None:
    row = sheet_row(SERIAL_13, "ad1", spend=99.9)
    row[12] = 1000

    assert check_row_types([row]).passed is True


# -- Quantidade ------------------------------------------------------------


def test_menos_linhas_que_o_lote_reprova() -> None:
    check = check_row_count([sheet_row(SERIAL_13, "ad1")], expected_minimum=3)

    assert check.passed is False


def test_historico_maior_que_o_lote_aprova() -> None:
    """A aba acumula histórico: ter mais linhas que o lote é o normal."""
    sheet = [sheet_row(SERIAL_13, "ad1"), sheet_row(SERIAL_14, "ad2")]

    assert check_row_count(sheet, expected_minimum=1).passed is True


# -- Relatório -------------------------------------------------------------


def test_relatorio_aprovado_nao_levanta() -> None:
    sheet = [sheet_row(SERIAL_13, "ad1")]
    report = validate_sheet(sheet, [written_row("2026-07-13", "ad1")])

    assert report.passed is True
    report.raise_if_critical()  # não levanta


def test_relatorio_com_falha_critica_levanta() -> None:
    sheet = [sheet_row(SERIAL_13, "ad1"), sheet_row(SERIAL_13, "ad1")]  # duplicata
    report = validate_sheet(sheet, [written_row("2026-07-13", "ad1")])

    assert report.passed is False
    assert len(report.critical_failures) == 1

    with pytest.raises(ValidationError, match="chaves_unicas"):
        report.raise_if_critical()


def test_gravacao_perdida_e_detectada() -> None:
    """O upsert disse que gravou, mas a linha não está na aba."""
    report = validate_sheet([], [written_row("2026-07-13", "ad1")])

    assert report.passed is False
    with pytest.raises(ValidationError, match="linhas_esperadas"):
        report.raise_if_critical()


def test_dry_run_valida_o_lote_sem_ler_a_planilha() -> None:
    report = validate_pending_rows(
        [written_row("2026-07-13", "ad1"), written_row("2026-07-13", "ad2")]
    )

    assert report.passed is True


def test_dry_run_reprova_lote_com_chave_repetida() -> None:
    report = validate_pending_rows(
        [written_row("2026-07-13", "ad1"), written_row("2026-07-13", "ad1")]
    )

    assert report.passed is False
