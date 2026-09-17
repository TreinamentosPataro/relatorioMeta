"""Backfill da aba derivada a partir da aba bruta. Execução avulsa, idempotente.

POR QUE ESTE MÓDULO EXISTE

Até a mudança para upsert, a aba derivada era limpa e reescrita a cada execução, então
só sobrevivia nela a janela de LOOKBACK_DAYS. A aba bruta, essa sim, sempre foi upsert e
guarda o histórico inteiro. Este script reconstrói a aba derivada a partir dela, de uma
vez, sem tocar na Meta.

A fonte é a aba BRUTA, não a Marketing API, por três motivos: ela já tem todo o
histórico, a leitura é uma chamada só (contra centenas de páginas paginadas) e a Meta
retém insights por um tempo limitado — dias muito antigos simplesmente não voltariam
mais pela API.

SEGURANÇA

Não há nada de destrutivo aqui. O script apenas LÊ a aba bruta e delega a escrita ao
mesmo DerivedSheetWriter.upsert() usado na coleta diária: linha existente é atualizada
no lugar, linha nova é acrescentada, nenhuma linha é apagada. Rodar duas vezes seguidas
produz o mesmo resultado (a segunda execução vira 100% updates).

DRY_RUN=1 mostra quantas linhas entrariam sem escrever nada.
"""

from __future__ import annotations

import sys
from typing import Any, Final, Sequence

from src.config import Config, ConfigError, load_config
from src.derived import DerivedSheetWriter
from src.logger import get_logger, setup_logging
from src.sheet_writer import SheetWriter, SheetWriterError, _normalize_key_date
from src.transform import COLUMN_COUNT as RAW_COLUMN_COUNT
from src.transform import Row, _to_float

logger = get_logger(__name__)

EXIT_OK: Final[int] = 0
EXIT_CONFIG: Final[int] = 1
EXIT_API: Final[int] = 2

# Índices da linha bruta que a aba derivada consome como número.
_NUMERIC_COLUMNS: Final[tuple[int, ...]] = (11, 12, 13, 14, 15, 16, 17, 18, 22, 23, 24)

# Índices da chave na linha bruta.
_RAW_DATE: Final[int] = 0
_RAW_AD_ID: Final[int] = 9


def sanitize_raw_rows(values: Sequence[Sequence[Any]]) -> list[Row]:
    """Prepara as linhas lidas da aba bruta para o cálculo das métricas derivadas.

    A leitura da planilha não devolve linhas prontas para uso:

      * o Sheets corta as células vazias do fim, então uma linha pode vir com
        menos de 25 colunas e estourar um IndexError no cálculo;
      * a data volta como número de série (a leitura usa SERIAL_NUMBER), e não
        como 'yyyy-mm-dd';
      * uma célula editada à mão pode conter texto onde se espera número, e um
        int() cru mataria o backfill inteiro por causa de uma linha só.

    Linhas sem data ou sem Ad ID são descartadas: sem chave, todas colidiriam
    entre si no dedupe e virariam uma única linha de lixo.
    """
    prepared: list[Row] = []
    skipped = 0

    for value in values:
        row: Row = list(value)[:RAW_COLUMN_COUNT]
        row += [""] * (RAW_COLUMN_COUNT - len(row))

        date = _normalize_key_date(row[_RAW_DATE])
        ad_id = str(row[_RAW_AD_ID] or "").strip()
        if not date or not ad_id:
            skipped += 1
            continue

        row[_RAW_DATE] = date
        row[_RAW_AD_ID] = ad_id
        for index in _NUMERIC_COLUMNS:
            row[index] = _to_float(row[index])

        prepared.append(row)

    logger.info(
        "Linhas da aba bruta preparadas",
        extra={"rows_read": len(values), "rows_usable": len(prepared), "skipped": skipped},
    )
    if skipped:
        logger.warning(
            "Linhas sem Data ou sem Ad ID foram ignoradas: não têm chave de upsert.",
            extra={"skipped": skipped},
        )
    return prepared


def run(config: Config) -> int:
    """Lê a aba bruta inteira e faz upsert dela na aba derivada."""
    logger.info(
        "Backfill da aba derivada a partir da aba bruta",
        extra={
            "raw_sheet": config.raw_sheet_name,
            "derived_sheet": config.derived_sheet_name,
            "dry_run": config.dry_run,
        },
    )

    writer = SheetWriter(config)
    raw_values = writer.read_rows()
    rows = sanitize_raw_rows(raw_values)

    if not rows:
        logger.warning(
            "A aba bruta não tem nenhuma linha utilizável; nada a fazer.",
            extra={"raw_sheet": config.raw_sheet_name},
        )
        return EXIT_OK

    # Reusa o cliente já autenticado e o mesmo caminho de escrita da coleta diária.
    derived_writer = DerivedSheetWriter(config, service=writer.service)
    summary = derived_writer.upsert(rows)

    logger.info(
        "Backfill concluído",
        extra={
            "rows_processed": summary.rows,
            "updated": summary.updated,
            "appended": summary.appended,
            "dry_run": summary.dry_run,
        },
    )
    return EXIT_OK


def main() -> int:
    """Carrega a configuração e roda o backfill."""
    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging()
        logger.error("Falha de configuração: %s", exc)
        return EXIT_CONFIG

    setup_logging(config.log_level)

    try:
        return run(config)
    except SheetWriterError as exc:
        logger.error("Falha na integração com o Sheets.", extra={"error": str(exc)})
        return EXIT_API
    except Exception:  # o script pode rodar no CI: nada pode escapar sem log
        logger.exception("Falha inesperada no backfill.")
        return EXIT_API


if __name__ == "__main__":
    sys.exit(main())
