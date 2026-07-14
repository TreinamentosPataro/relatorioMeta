"""Configuração central da aplicação, lida exclusivamente de variáveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS: Final[tuple[str, ...]] = (
    "META_APP_ID",
    "META_APP_SECRET",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "SPREADSHEET_ID",
)

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


class ConfigError(RuntimeError):
    """Erro de configuração: variável obrigatória ausente ou inválida."""


@dataclass(frozen=True, slots=True)
class Config:
    """Configuração imutável da execução."""

    # Meta (Marketing API)
    meta_app_id: str
    meta_app_secret: str
    meta_access_token: str
    meta_ad_account_id: str

    # Google Sheets
    google_service_account_json: str
    spreadsheet_id: str

    # Opcionais
    lookback_days: int
    account_timezone: str
    raw_sheet_name: str
    derived_sheet_name: str
    log_level: str
    dry_run: bool


def _get_required(name: str) -> str:
    """Retorna a variável obrigatória ou levanta ConfigError."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Variável de ambiente obrigatória ausente ou vazia: {name}. "
            "Consulte o .env.example."
        )
    return value


def _get_int(name: str, default: int) -> int:
    """Lê um inteiro opcional do ambiente."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um inteiro, recebido: {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    """Lê um booleano opcional do ambiente."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def load_config() -> Config:
    """Monta o Config a partir do ambiente, validando os campos obrigatórios."""
    missing = [name for name in REQUIRED_VARS if not os.getenv(name, "").strip()]
    if missing:
        raise ConfigError(
            "Variáveis de ambiente obrigatórias ausentes: "
            + ", ".join(missing)
            + ". Consulte o .env.example."
        )

    return Config(
        meta_app_id=_get_required("META_APP_ID"),
        meta_app_secret=_get_required("META_APP_SECRET"),
        meta_access_token=_get_required("META_ACCESS_TOKEN"),
        meta_ad_account_id=_get_required("META_AD_ACCOUNT_ID"),
        google_service_account_json=_get_required("GOOGLE_SERVICE_ACCOUNT_JSON"),
        spreadsheet_id=_get_required("SPREADSHEET_ID"),
        lookback_days=_get_int("LOOKBACK_DAYS", 7),
        account_timezone=os.getenv("ACCOUNT_TIMEZONE", "").strip() or "America/Sao_Paulo",
        raw_sheet_name=os.getenv("RAW_SHEET_NAME", "").strip() or "Meta ADS",
        derived_sheet_name=os.getenv("DERIVED_SHEET_NAME", "").strip()
        or "Meta ADS - Métricas",
        log_level=os.getenv("LOG_LEVEL", "").strip().upper() or "INFO",
        dry_run=_get_bool("DRY_RUN", False),
    )
