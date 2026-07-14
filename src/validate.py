"""Checagens de qualidade executadas APÓS a gravação na planilha.

A ideia é simples: reler o que foi gravado e provar que a aba está sã. Uma falha
crítica aqui significa que a planilha não pode ser confiada — o job termina com
código != 0 para o CI acusar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Iterable, Sequence

from src.logger import get_logger
from src.sheet_writer import _normalize_key_date
from src.transform import COLUMN_COUNT, Key, Row, make_key

logger = get_logger(__name__)

# Índices na linha de 25 colunas.
_DATE: Final[int] = 0
_AD_ID: Final[int] = 9
_SPEND: Final[int] = 11
_FIRST_METRIC: Final[int] = 12  # M..Y são métricas inteiras não negativas

_MAX_SAMPLES: Final[int] = 5  # amostras de exemplo por checagem, para não poluir o log


class ValidationError(RuntimeError):
    """Ao menos uma checagem crítica falhou. A planilha não está confiável."""


@dataclass(frozen=True, slots=True)
class Check:
    """Resultado de uma checagem."""

    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass(slots=True)
class ValidationReport:
    """Relatório consolidado das checagens."""

    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def critical_failures(self) -> list[Check]:
        return [check for check in self.failures if check.critical]

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def raise_if_critical(self) -> None:
        """Levanta ValidationError se alguma checagem crítica tiver falhado."""
        criticals = self.critical_failures
        if criticals:
            resumo = "; ".join(f"{check.name}: {check.detail}" for check in criticals)
            raise ValidationError(f"Validação falhou ({len(criticals)} crítica(s)): {resumo}")


def _is_number(value: Any) -> bool:
    """True para número de verdade (o Sheets devolve números como int/float)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_number(value: Any) -> float | None:
    """Converte para número quando possível; None quando não é numérico."""
    if _is_number(value):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _is_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _sheet_key(row: Sequence[Any]) -> Key:
    """Chave (date, ad_id) de uma linha lida da planilha (data vem como serial)."""
    date_value = _normalize_key_date(row[_DATE] if row else "")
    ad_id = str(row[_AD_ID]).strip() if len(row) > _AD_ID and row[_AD_ID] is not None else ""
    return (date_value, ad_id)


def check_no_duplicate_keys(sheet_rows: Sequence[Sequence[Any]]) -> Check:
    """Nenhuma chave (Date, Ad ID) pode aparecer duas vezes na aba."""
    seen: set[Key] = set()
    duplicates: list[Key] = []

    for row in sheet_rows:
        key = _sheet_key(row)
        if not key[0] or not key[1]:
            continue  # linha em branco/incompleta não é chave
        if key in seen:
            duplicates.append(key)
        seen.add(key)

    if duplicates:
        amostra = ", ".join(f"{date}/{ad_id}" for date, ad_id in duplicates[:_MAX_SAMPLES])
        return Check(
            name="chaves_unicas",
            passed=False,
            detail=f"{len(duplicates)} chave(s) (Date, Ad ID) duplicada(s) na aba: {amostra}",
        )

    return Check(
        name="chaves_unicas",
        passed=True,
        detail=f"{len(seen)} chaves únicas, nenhuma duplicata.",
    )


def check_expected_rows_present(
    sheet_rows: Sequence[Sequence[Any]], expected_keys: Iterable[Key]
) -> Check:
    """Toda linha que deveria ter sido gravada precisa estar na aba."""
    expected = set(expected_keys)
    present = {_sheet_key(row) for row in sheet_rows}
    missing = expected - present

    if missing:
        amostra = ", ".join(f"{date}/{ad_id}" for date, ad_id in sorted(missing)[:_MAX_SAMPLES])
        return Check(
            name="linhas_esperadas",
            passed=False,
            detail=(
                f"{len(missing)} de {len(expected)} linha(s) esperada(s) não estão na aba: "
                f"{amostra}"
            ),
        )

    return Check(
        name="linhas_esperadas",
        passed=True,
        detail=f"as {len(expected)} linha(s) esperada(s) estão na aba (que tem {len(present)}).",
    )


def check_row_types(sheet_rows: Sequence[Sequence[Any]]) -> Check:
    """Date é data, Spend é numérico e as métricas não são negativas."""
    problemas: list[str] = []

    for offset, row in enumerate(sheet_rows):
        linha = offset + 2  # a aba começa na linha 2 (linha 1 é cabeçalho)
        if not any(str(cell).strip() for cell in row):
            continue  # linha em branco

        date_value = _normalize_key_date(row[_DATE] if row else "")
        if not _is_iso_date(date_value):
            problemas.append(f"linha {linha}: Date inválida ({row[_DATE]!r})")

        spend = _to_number(row[_SPEND]) if len(row) > _SPEND else 0.0
        if spend is None:
            problemas.append(f"linha {linha}: Spend não numérico ({row[_SPEND]!r})")
        elif spend < 0:
            problemas.append(f"linha {linha}: Spend negativo ({spend})")

        for index in range(_FIRST_METRIC, min(len(row), COLUMN_COUNT)):
            valor = _to_number(row[index])
            if valor is None:
                problemas.append(f"linha {linha}: métrica não numérica na coluna {index + 1}")
            elif valor < 0:
                problemas.append(f"linha {linha}: métrica negativa na coluna {index + 1} ({valor})")

    if problemas:
        return Check(
            name="tipos_coerentes",
            passed=False,
            detail=f"{len(problemas)} problema(s) de tipo: " + "; ".join(problemas[:_MAX_SAMPLES]),
        )

    return Check(
        name="tipos_coerentes",
        passed=True,
        detail=f"{len(sheet_rows)} linha(s) com Date, Spend e métricas coerentes.",
    )


def check_row_count(sheet_rows: Sequence[Sequence[Any]], expected_minimum: int) -> Check:
    """A aba tem ao menos as linhas do lote (ela também guarda o histórico)."""
    total = sum(1 for row in sheet_rows if any(str(cell).strip() for cell in row))

    if total < expected_minimum:
        return Check(
            name="quantidade_de_linhas",
            passed=False,
            detail=(
                f"a aba tem {total} linha(s), menos que as {expected_minimum} do lote — "
                "algo não foi gravado."
            ),
        )

    return Check(
        name="quantidade_de_linhas",
        passed=True,
        detail=f"{total} linha(s) na aba, >= as {expected_minimum} do lote.",
        critical=False,
    )


def validate_sheet(
    sheet_rows: Sequence[Sequence[Any]], written_rows: Sequence[Row]
) -> ValidationReport:
    """Roda todas as checagens contra a aba relida e devolve o relatório.

    `written_rows` são as linhas que o transform produziu nesta execução.
    """
    expected_keys = [make_key(row) for row in written_rows]

    report = ValidationReport()
    report.add(check_no_duplicate_keys(sheet_rows))
    report.add(check_expected_rows_present(sheet_rows, expected_keys))
    report.add(check_row_types(sheet_rows))
    report.add(check_row_count(sheet_rows, len(set(expected_keys))))

    log_report(report)
    return report


def validate_pending_rows(rows: Sequence[Row]) -> ValidationReport:
    """Checagens possíveis sem ler a planilha — usado no DRY_RUN.

    Valida o que seria gravado: chaves únicas no lote e tipos coerentes.
    """
    report = ValidationReport()
    report.add(check_no_duplicate_keys(rows))
    report.add(check_row_types(rows))

    log_report(report)
    return report


def log_report(report: ValidationReport) -> None:
    """Loga cada checagem e o veredito final."""
    for check in report.checks:
        log = logger.info if check.passed else (logger.error if check.critical else logger.warning)
        log(
            f"Checagem '{check.name}': {'OK' if check.passed else 'FALHOU'}",
            extra={
                "check": check.name,
                "passed": check.passed,
                "critical": check.critical,
                "detail": check.detail,
            },
        )

    logger.info(
        "Relatório de validação",
        extra={
            "checks": len(report.checks),
            "failures": len(report.failures),
            "critical_failures": len(report.critical_failures),
            "passed": report.passed,
        },
    )
