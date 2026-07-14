"""Logging estruturado (JSON) para a automação."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

_CONFIGURED = False

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"asctime", "message", "taskName"}


class _SafeLogger(logging.Logger):
    """Logger que aceita qualquer chave em `extra` sem quebrar a aplicação.

    O logging padrão levanta KeyError quando um `extra` colide com um atributo do
    LogRecord ("created", "module", "name", "process"...). Isso transforma uma linha
    de log em um crash em produção — inaceitável para um job que roda sozinho. Aqui,
    a chave colidente é renomeada para extra_<nome> em vez de derrubar a execução.
    """

    def makeRecord(  # noqa: PLR0913 (assinatura ditada pela stdlib)
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: dict[str, Any] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        if extra:
            extra = {
                (f"extra_{key}" if key in _RESERVED else key): value
                for key, value in extra.items()
            }
        return super().makeRecord(
            name, level, fn, lno, msg, args, exc_info, func, extra, sinfo
        )


# Precisa valer antes de qualquer getLogger() dos outros módulos — por isso, no import.
logging.setLoggerClass(_SafeLogger)


class JsonFormatter(logging.Formatter):
    """Formata cada registro como uma linha JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Campos extras passados via logger.info("...", extra={...}).
        # Um extra jamais sobrescreve os campos centrais: um extra chamado "level"
        # apagaria a severidade do registro. Em caso de colisão, ele é renomeado.
        for key, value in record.__dict__.items():
            if key in _RESERVED:
                continue
            payload[f"extra_{key}" if key in payload else key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None) -> None:
    """Configura o logging global. Idempotente."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    # No Windows o console usa cp1252 por padrão e mutila os acentos das mensagens.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # Bibliotecas de terceiros são verbosas demais em DEBUG.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Retorna um logger nomeado, garantindo que o logging esteja configurado."""
    setup_logging()
    return logging.getLogger(name)
