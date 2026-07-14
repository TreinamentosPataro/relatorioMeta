"""Normalização dos insights brutos da Meta para as linhas da aba "Meta ADS"."""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from src.logger import get_logger

logger = get_logger(__name__)

# Ordem exata das 25 colunas (A..Y) da aba bruta.
COLUMNS: Final[tuple[str, ...]] = (
    "Date",                                                    # A
    "Account Name",                                            # B
    "Account ID",                                              # C
    "Objective",                                               # D
    "Campaign Name",                                           # E
    "Campaign ID",                                             # F
    "Adset Name",                                              # G
    "Adset ID",                                                # H
    "Ad Name",                                                 # I
    "Ad ID",                                                   # J
    "Thumbnail URL",                                           # K
    "Spend (Cost, Amount Spent)",                              # L
    "Impressions",                                             # M
    "Action Link Clicks",                                      # N
    "Action Landing Page View",                                # O
    "Action Leads",                                            # P
    "Video Play Actions",                                      # Q
    "Video Thruplay Watched Actions",                          # R
    "Action 3s Video Views",                                   # S
    "Video 25 Percent Watched Actions",                        # T
    "Video 50 Percent Watched Actions",                        # U
    "Video 75 Percent Watched Actions",                        # V
    "Video 100 Percent Watched Actions",                       # W
    "Action Omni Purchase",                                    # X
    "Action Messaging Conversations Started (Onsite Conversion)",  # Y
)

COLUMN_COUNT: Final[int] = len(COLUMNS)

# Índices usados pela chave de upsert.
_DATE_INDEX: Final[int] = 0
_AD_ID_INDEX: Final[int] = 9

# action_type -> coluna, dentro do array "actions".
_ACTION_LINK_CLICK: Final[str] = "link_click"
_ACTION_LANDING_PAGE_VIEW: Final[str] = "landing_page_view"
_ACTION_LEAD: Final[str] = "lead"
_ACTION_VIDEO_VIEW: Final[str] = "video_view"
_ACTION_OMNI_PURCHASE: Final[str] = "omni_purchase"
_ACTION_MESSAGING_STARTED: Final[str] = (
    "onsite_conversion.messaging_conversation_started_7d"
)

Row = list[Any]
Key = tuple[str, str]


def _to_float(value: Any) -> float:
    """Converte para float; ausente ou inválido vira 0.0."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Valor numérico inválido; assumindo 0.", extra={"value": value})
        return 0.0


def _to_int(value: Any) -> int:
    """Converte para int; ausente ou inválido vira 0.

    A API devolve os números como string ("1234") e, em alguns campos de vídeo,
    com casas decimais ("12.0"). float() primeiro evita ValueError em "12.0".
    """
    return int(_to_float(value))


def _to_str(value: Any) -> str:
    """Converte para string; ausente vira ""."""
    return "" if value is None else str(value)


def _sum_action_values(actions: Any) -> int:
    """Soma o campo "value" de um array de actions (campos video_*_actions)."""
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        return 0
    return sum(_to_int(entry.get("value")) for entry in actions if isinstance(entry, Mapping))


def _actions_by_type(actions: Any) -> dict[str, int]:
    """Indexa o array "actions" por action_type, somando entradas repetidas."""
    totals: dict[str, int] = {}
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        return totals
    for entry in actions:
        if not isinstance(entry, Mapping):
            continue
        action_type = entry.get("action_type")
        if not action_type:
            continue
        totals[str(action_type)] = totals.get(str(action_type), 0) + _to_int(entry.get("value"))
    return totals


def to_rows(
    insights: list[dict[str, Any]],
    thumbnails: Mapping[str, str | None] | None = None,
) -> list[Row]:
    """Converte os insights brutos em linhas de 25 colunas na ordem A..Y.

    Valores ausentes viram 0 (numéricos) ou "" (texto), nunca None.
    """
    thumbnails = thumbnails or {}
    rows: list[Row] = [_to_row(insight, thumbnails) for insight in insights]

    logger.info(
        "Insights normalizados",
        extra={"input_records": len(insights), "rows": len(rows), "columns": COLUMN_COUNT},
    )
    return rows


def _to_row(insight: Mapping[str, Any], thumbnails: Mapping[str, str | None]) -> Row:
    """Monta uma linha de 25 colunas a partir de um registro bruto."""
    actions = _actions_by_type(insight.get("actions"))
    ad_id = _to_str(insight.get("ad_id"))

    row: Row = [
        _to_str(insight.get("date_start")),                                    # A Date
        _to_str(insight.get("account_name")),                                  # B
        _to_str(insight.get("account_id")),                                    # C
        _to_str(insight.get("objective")),                                     # D
        _to_str(insight.get("campaign_name")),                                 # E
        _to_str(insight.get("campaign_id")),                                   # F
        _to_str(insight.get("adset_name")),                                    # G
        _to_str(insight.get("adset_id")),                                      # H
        _to_str(insight.get("ad_name")),                                       # I
        ad_id,                                                                 # J
        _to_str(thumbnails.get(ad_id)),                                        # K
        _to_float(insight.get("spend")),                                       # L
        _to_int(insight.get("impressions")),                                   # M
        actions.get(_ACTION_LINK_CLICK, 0),                                    # N
        actions.get(_ACTION_LANDING_PAGE_VIEW, 0),                             # O
        actions.get(_ACTION_LEAD, 0),                                          # P
        _sum_action_values(insight.get("video_play_actions")),                 # Q
        _sum_action_values(insight.get("video_thruplay_watched_actions")),     # R
        actions.get(_ACTION_VIDEO_VIEW, 0),                                    # S
        _sum_action_values(insight.get("video_p25_watched_actions")),          # T
        _sum_action_values(insight.get("video_p50_watched_actions")),          # U
        _sum_action_values(insight.get("video_p75_watched_actions")),          # V
        _sum_action_values(insight.get("video_p100_watched_actions")),         # W
        actions.get(_ACTION_OMNI_PURCHASE, 0),                                 # X
        actions.get(_ACTION_MESSAGING_STARTED, 0),                             # Y
    ]

    if len(row) != COLUMN_COUNT:  # guarda contra edição descuidada da lista acima
        raise ValueError(f"Linha com {len(row)} colunas; esperado {COLUMN_COUNT}.")
    return row


def make_key(row: Sequence[Any]) -> Key:
    """Chave de upsert de uma linha: (date, ad_id)."""
    return (str(row[_DATE_INDEX]), str(row[_AD_ID_INDEX]))
