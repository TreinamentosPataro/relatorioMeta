"""Testes do logging estruturado."""

from __future__ import annotations

import json
import logging

from src.logger import JsonFormatter, get_logger


def format_record(logger_name: str, extra: dict[str, object]) -> dict[str, object]:
    """Emite um registro com `extra` e devolve o JSON formatado."""
    logger = get_logger(logger_name)
    records: list[logging.LogRecord] = []

    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        logger.info("mensagem", extra=extra)
    finally:
        logger.removeHandler(handler)

    return json.loads(JsonFormatter().format(records[0]))


def test_extra_reservado_nao_derruba_a_aplicacao() -> None:
    """'created' é atributo do LogRecord: o logging padrão levantaria KeyError."""
    payload = format_record("teste.reservado", {"created": True, "module": "x"})

    assert payload["extra_created"] is True
    assert payload["extra_module"] == "x"
    assert payload["level"] == "INFO"


def test_extra_nao_sobrescreve_a_severidade() -> None:
    """Um extra chamado 'level' apagaria a severidade do registro."""
    payload = format_record("teste.level", {"level": "ad"})

    assert payload["level"] == "INFO"
    assert payload["extra_level"] == "ad"


def test_extras_normais_aparecem_no_json() -> None:
    payload = format_record("teste.normal", {"sheet": "Meta ADS", "rows": 42})

    assert payload["sheet"] == "Meta ADS"
    assert payload["rows"] == 42
    assert payload["message"] == "mensagem"
