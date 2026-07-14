"""Testes da normalização dos insights da Meta."""

from __future__ import annotations

from typing import Any

import pytest

from src.transform import COLUMN_COUNT, COLUMNS, make_key, to_rows


def build_insight(**overrides: Any) -> dict[str, Any]:
    """Registro bruto típico da API, com todos os campos preenchidos."""
    insight: dict[str, Any] = {
        "date_start": "2026-07-13",
        "account_name": "Pataro",
        "account_id": "123",
        "objective": "OUTCOME_SALES",
        "campaign_name": "Campanha A",
        "campaign_id": "c1",
        "adset_name": "Conjunto A",
        "adset_id": "as1",
        "ad_name": "Anúncio A",
        "ad_id": "ad1",
        "spend": "123.45",
        "impressions": "1000",
        "actions": [
            {"action_type": "link_click", "value": "10"},
            {"action_type": "landing_page_view", "value": "8"},
            {"action_type": "lead", "value": "3"},
            {"action_type": "video_view", "value": "500"},
            {"action_type": "omni_purchase", "value": "2"},
            {
                "action_type": "onsite_conversion.messaging_conversation_started_7d",
                "value": "7",
            },
            {"action_type": "post_engagement", "value": "999"},  # ignorado
        ],
        "video_play_actions": [{"action_type": "video_play", "value": "600"}],
        "video_thruplay_watched_actions": [{"action_type": "video_view", "value": "300"}],
        "video_p25_watched_actions": [{"action_type": "video_view", "value": "400"}],
        "video_p50_watched_actions": [{"action_type": "video_view", "value": "250"}],
        "video_p75_watched_actions": [{"action_type": "video_view", "value": "150"}],
        "video_p100_watched_actions": [{"action_type": "video_view", "value": "100"}],
    }
    insight.update(overrides)
    return insight


def test_ordem_e_conteudo_das_25_colunas() -> None:
    """A linha tem exatamente 25 elementos, na ordem A..Y."""
    rows = to_rows([build_insight()], {"ad1": "http://img/1.png"})

    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 25 == COLUMN_COUNT

    assert row == [
        "2026-07-13",      # A Date
        "Pataro",          # B Account Name
        "123",             # C Account ID
        "OUTCOME_SALES",   # D Objective
        "Campanha A",      # E Campaign Name
        "c1",              # F Campaign ID
        "Conjunto A",      # G Adset Name
        "as1",             # H Adset ID
        "Anúncio A",       # I Ad Name
        "ad1",             # J Ad ID
        "http://img/1.png",  # K Thumbnail URL
        123.45,            # L Spend
        1000,              # M Impressions
        10,                # N Link Clicks
        8,                 # O Landing Page View
        3,                 # P Leads
        600,               # Q Video Play Actions
        300,               # R Video Thruplay
        500,               # S 3s Video Views
        400,               # T Video 25%
        250,               # U Video 50%
        150,               # V Video 75%
        100,               # W Video 100%
        2,                 # X Omni Purchase
        7,                 # Y Messaging Started
    ]


def test_cabecalho_declara_25_colunas_na_ordem_da_planilha() -> None:
    """COLUMNS descreve a aba: 25 nomes, primeiro Date, décimo Ad ID."""
    assert len(COLUMNS) == 25
    assert COLUMNS[0] == "Date"
    assert COLUMNS[9] == "Ad ID"
    assert COLUMNS[10] == "Thumbnail URL"
    assert COLUMNS[24] == "Action Messaging Conversations Started (Onsite Conversion)"


def test_extracao_de_actions_por_action_type() -> None:
    """Cada coluna de action lê o seu action_type, ignorando os demais."""
    insight = build_insight(
        actions=[
            {"action_type": "lead", "value": "42"},
            {"action_type": "post_reaction", "value": "1000"},  # não mapeado
        ]
    )
    row = to_rows([insight])[0]

    assert row[15] == 42  # P Leads
    assert row[13] == 0   # N Link Clicks — ausente do array
    assert row[14] == 0   # O Landing Page View — ausente
    assert row[18] == 0   # S 3s Video Views — ausente
    assert row[23] == 0   # X Omni Purchase — ausente
    assert row[24] == 0   # Y Messaging — ausente


def test_action_type_ausente_vira_zero() -> None:
    """Sem o array actions (ou com ele vazio), todas as colunas de action viram 0."""
    sem_actions = to_rows([build_insight(actions=[])])[0]
    campo_ausente = to_rows([{"date_start": "2026-07-13", "ad_id": "ad1"}])[0]

    for row in (sem_actions, campo_ausente):
        for index in (13, 14, 15, 18, 23, 24):
            assert row[index] == 0, f"coluna {index} deveria ser 0"


def test_registro_vazio_nunca_produz_none() -> None:
    """Campos ausentes viram 0 (numérico) ou "" (texto), jamais None."""
    row = to_rows([{}])[0]

    assert len(row) == 25
    assert None not in row
    assert row[:11] == [""] * 11        # A..K são texto
    assert row[11] == 0.0               # L Spend
    assert row[12:] == [0] * 13         # M..Y são inteiros


def test_soma_de_video_actions_com_multiplas_entradas() -> None:
    """Os campos video_* somam o "value" de todas as entradas do array."""
    insight = build_insight(
        video_play_actions=[
            {"action_type": "video_play", "value": "100"},
            {"action_type": "video_play", "value": "50"},
        ],
        video_p100_watched_actions=[
            {"action_type": "video_view", "value": "7"},
            {"action_type": "video_view", "value": "3"},
        ],
    )
    row = to_rows([insight])[0]

    assert row[16] == 150  # Q Video Play Actions
    assert row[22] == 10   # W Video 100%


def test_video_actions_ausentes_viram_zero() -> None:
    """Campo de vídeo ausente ou vazio vira 0, não None."""
    insight = build_insight(
        video_play_actions=None,
        video_thruplay_watched_actions=[],
    )
    del insight["video_p50_watched_actions"]
    row = to_rows([insight])[0]

    assert row[16] == 0  # Q
    assert row[17] == 0  # R
    assert row[20] == 0  # U


def test_video_action_com_valor_decimal_vira_inteiro() -> None:
    """A API às vezes devolve "12.0"; int("12.0") explodiria."""
    insight = build_insight(
        video_p25_watched_actions=[{"action_type": "video_view", "value": "12.0"}],
        impressions="1500.0",
    )
    row = to_rows([insight])[0]

    assert row[19] == 12
    assert row[12] == 1500


def test_spend_e_float_e_metricas_sao_inteiras() -> None:
    """L é float; M e as colunas de action/vídeo são int."""
    row = to_rows([build_insight()])[0]

    assert isinstance(row[11], float) and row[11] == 123.45
    for index in [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]:
        assert isinstance(row[index], int), f"coluna {index} deveria ser int"


def test_thumbnail_ausente_vira_string_vazia() -> None:
    """ad_id sem thumbnail (None ou fora do dict) vira ""."""
    none_no_dict = to_rows([build_insight()], {"ad1": None})[0]
    fora_do_dict = to_rows([build_insight()], {"outro": "http://img/x.png"})[0]
    sem_dict = to_rows([build_insight()])[0]

    assert none_no_dict[10] == ""
    assert fora_do_dict[10] == ""
    assert sem_dict[10] == ""


def test_make_key_devolve_date_e_ad_id() -> None:
    """A chave de upsert é (date, ad_id) — colunas A e J."""
    row = to_rows([build_insight()], {"ad1": "http://img/1.png"})[0]

    assert make_key(row) == ("2026-07-13", "ad1")


def test_make_key_distingue_dias_do_mesmo_anuncio() -> None:
    """Com time_increment=1, o mesmo ad_id aparece em vários dias."""
    rows = to_rows(
        [
            build_insight(date_start="2026-07-12"),
            build_insight(date_start="2026-07-13"),
        ]
    )

    chaves = [make_key(row) for row in rows]
    assert chaves == [("2026-07-12", "ad1"), ("2026-07-13", "ad1")]
    assert len(set(chaves)) == 2


def test_lista_vazia_devolve_lista_vazia() -> None:
    assert to_rows([]) == []


@pytest.mark.parametrize("valor", ["", None, "abc"])
def test_spend_invalido_ou_ausente_vira_zero(valor: Any) -> None:
    """Valor numérico corrompido não derruba a execução; vira 0."""
    row = to_rows([build_insight(spend=valor)])[0]

    assert row[11] == 0.0
