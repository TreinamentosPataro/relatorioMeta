"""Ponto de entrada: coleta da Meta ADS e gravação no Google Sheets."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Final

import pytz

from src.config import Config, ConfigError, load_config
from src.derived import DerivedSheetWriter
from src.logger import get_logger, setup_logging
from src.meta_client import MetaClient, MetaClientError
from src.sheet_writer import SheetWriter, SheetWriterError
from src.transform import to_rows
from src.validate import ValidationError, validate_pending_rows, validate_sheet

logger = get_logger(__name__)

# Códigos de saída, para o CI distinguir o tipo de falha.
EXIT_OK: Final[int] = 0
EXIT_CONFIG: Final[int] = 1
EXIT_API: Final[int] = 2
EXIT_VALIDATION: Final[int] = 3


def compute_window(config: Config) -> tuple[str, str]:
    """Janela de coleta: de (hoje - LOOKBACK_DAYS) até ontem, no fuso da conta.

    Ontem é o limite porque o dia corrente ainda está aberto: os números da Meta
    seguem mudando até o dia fechar no fuso da conta.
    """
    tz = pytz.timezone(config.account_timezone)
    today = datetime.now(tz).date()
    until = today - timedelta(days=1)
    since = today - timedelta(days=config.lookback_days)
    return since.isoformat(), until.isoformat()


def run(config: Config) -> int:
    """Executa o pipeline completo. Devolve o código de saída."""
    since, until = compute_window(config)
    logger.info(
        "Janela de coleta definida",
        extra={
            "since": since,
            "until": until,
            "lookback_days": config.lookback_days,
            "timezone": config.account_timezone,
            "dry_run": config.dry_run,
        },
    )

    # 3. Coleta na Meta (acontece de verdade mesmo em DRY_RUN).
    client = MetaClient(config)
    insights = client.get_insights(since, until)

    # 4. Sem dados não é erro: conta nova, ou período sem veiculação.
    if not insights:
        logger.warning(
            "A Meta não devolveu nenhum insight na janela; encerrando sem escrever.",
            extra={"since": since, "until": until},
        )
        return EXIT_OK

    # A thumbnail é só a coluna K; não vale perder o dia inteiro de insights se a
    # busca de criativos falhar (rate limit persistente, criativo inacessível).
    # Best-effort: sem thumbnail, a coluna sai vazia e o resto segue.
    ad_ids = [str(row["ad_id"]) for row in insights if row.get("ad_id")]
    try:
        thumbnails = client.get_thumbnails(ad_ids)
    except MetaClientError as exc:
        logger.warning(
            "Não foi possível buscar as thumbnails; seguindo sem elas.",
            extra={"error": str(exc), "ads": len(ad_ids)},
        )
        thumbnails = {}

    # 5. Normalização para as 25 colunas da aba bruta.
    rows = to_rows(insights, thumbnails)

    # 6. Upsert na aba bruta (não escreve em DRY_RUN).
    writer = SheetWriter(config)
    write_summary = writer.upsert(rows)

    # 7. Reconstrução da aba derivada (não escreve em DRY_RUN).
    derived_writer = DerivedSheetWriter(config, service=writer.service)
    derived_summary = derived_writer.rebuild(rows)

    logger.info(
        "Gravação concluída",
        extra={
            "updated": write_summary.updated,
            "appended": write_summary.appended,
            "processed": write_summary.processed,
            "derived_rows": derived_summary.rows,
            "dry_run": config.dry_run,
        },
    )

    # 8. Validação pós-gravação.
    if config.dry_run:
        logger.info("DRY_RUN: validando apenas as linhas que seriam gravadas.")
        report = validate_pending_rows(rows)
    else:
        report = validate_sheet(writer.read_rows(), rows)

    report.raise_if_critical()

    logger.info(
        "Execução concluída com sucesso",
        extra={"rows": len(rows), "dry_run": config.dry_run},
    )
    return EXIT_OK


def main_with_config(config: Config) -> int:
    """Roda o pipeline traduzindo cada falha em um código de saída distinto."""
    try:
        return run(config)
    except ValidationError as exc:
        logger.error(
            "A planilha não passou na validação pós-gravação.", extra={"error": str(exc)}
        )
        return EXIT_VALIDATION
    except (MetaClientError, SheetWriterError) as exc:
        logger.error("Falha na integração com a Meta ou com o Sheets.", extra={"error": str(exc)})
        return EXIT_API
    except Exception:  # o job roda sozinho: nada pode escapar sem log
        logger.exception("Falha inesperada.")
        return EXIT_API


def main() -> int:
    """Carrega a configuração e roda o pipeline."""
    # 1. Configuração.
    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging()
        logger.error("Falha de configuração: %s", exc)
        return EXIT_CONFIG

    setup_logging(config.log_level)
    logger.info(
        "Iniciando a coleta Meta ADS -> Google Sheets",
        extra={
            "ad_account_id": config.meta_ad_account_id,
            "raw_sheet": config.raw_sheet_name,
            "derived_sheet": config.derived_sheet_name,
            "dry_run": config.dry_run,
        },
    )

    return main_with_config(config)


if __name__ == "__main__":
    sys.exit(main())
