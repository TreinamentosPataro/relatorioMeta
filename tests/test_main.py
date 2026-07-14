"""Testes da orquestração: janela de datas, fluxo e códigos de saída."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import pytz

from src import main as main_module
from src.main import EXIT_API, EXIT_OK, EXIT_VALIDATION, compute_window, run
from src.meta_client import MetaAuthError
from tests.test_sheet_writer import make_config, make_row


# -- Janela ----------------------------------------------------------------


def test_janela_vai_de_hoje_menos_lookback_ate_ontem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ontem é o limite: o dia corrente ainda está aberto na Meta."""
    tz = pytz.timezone("America/Sao_Paulo")
    agora = tz.localize(datetime(2026, 7, 14, 3, 0))

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tzinfo: Any = None) -> datetime:
            return agora

    monkeypatch.setattr(main_module, "datetime", FakeDatetime)

    since, until = compute_window(make_config(lookback_days=7))

    assert (since, until) == ("2026-07-07", "2026-07-13")


def test_lookback_de_1_dia_coleta_so_ontem(monkeypatch: pytest.MonkeyPatch) -> None:
    tz = pytz.timezone("America/Sao_Paulo")
    agora = tz.localize(datetime(2026, 7, 14, 3, 0))

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tzinfo: Any = None) -> datetime:
            return agora

    monkeypatch.setattr(main_module, "datetime", FakeDatetime)

    assert compute_window(make_config(lookback_days=1)) == ("2026-07-13", "2026-07-13")


def test_fuso_da_conta_e_respeitado(monkeypatch: pytest.MonkeyPatch) -> None:
    """03:00 em São Paulo já é dia 14 em UTC-3, mas ainda 13 em outro fuso."""
    utc = pytz.timezone("UTC")
    agora = utc.localize(datetime(2026, 7, 14, 2, 0))  # 23:00 do dia 13 em SP

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tzinfo: Any = None) -> datetime:
            return agora.astimezone(tzinfo) if tzinfo else agora

    monkeypatch.setattr(main_module, "datetime", FakeDatetime)

    since, until = compute_window(make_config(lookback_days=2))

    assert until == "2026-07-12", "em SP ainda é dia 13; ontem é 12"
    assert since == "2026-07-11"


# -- Fluxo -----------------------------------------------------------------


INSIGHT = {"ad_id": "ad1", "date_start": "2026-07-13", "spend": "10", "impressions": "100"}


class FakeMetaClient:
    instances: list["FakeMetaClient"] = []

    def __init__(self, config: Any, insights: list[dict[str, Any]] | None = None) -> None:
        self.config = config
        self.insights = [INSIGHT] if insights is None else insights
        self.thumbnail_calls: list[list[str]] = []
        FakeMetaClient.instances.append(self)

    def get_insights(self, since: str, until: str) -> list[dict[str, Any]]:
        self.window = (since, until)
        return self.insights

    def get_thumbnails(self, ad_ids: list[str]) -> dict[str, str | None]:
        self.thumbnail_calls.append(list(ad_ids))
        return {ad_id: "http://img/x.png" for ad_id in ad_ids}


class FakeSummary:
    updated = 0
    appended = 1
    processed = 1
    rows = 1


class FakeSheetWriter:
    instances: list["FakeSheetWriter"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.upserted: list[list[Any]] = []
        self.read_calls = 0
        self.service = object()
        FakeSheetWriter.instances.append(self)

    def upsert(self, rows: list[list[Any]]) -> FakeSummary:
        self.upserted = rows
        return FakeSummary()

    def read_rows(self, sheet_name: str | None = None) -> list[list[Any]]:
        self.read_calls += 1
        # Devolve exatamente o que foi gravado: a aba está sã.
        return [list(row) for row in self.upserted]


class FakeDerivedWriter:
    instances: list["FakeDerivedWriter"] = []

    def __init__(self, config: Any, service: Any = None) -> None:
        self.config = config
        self.service = service
        self.rebuilt: list[list[Any]] = []
        FakeDerivedWriter.instances.append(self)

    def rebuild(self, rows: list[list[Any]]) -> FakeSummary:
        self.rebuilt = rows
        return FakeSummary()


@pytest.fixture(autouse=True)
def reset_fakes() -> None:
    FakeMetaClient.instances.clear()
    FakeSheetWriter.instances.clear()
    FakeDerivedWriter.instances.clear()


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, insights: list[dict[str, Any]] | None = None
) -> None:
    monkeypatch.setattr(
        main_module, "MetaClient", lambda config: FakeMetaClient(config, insights)
    )
    monkeypatch.setattr(main_module, "SheetWriter", FakeSheetWriter)
    monkeypatch.setattr(main_module, "DerivedSheetWriter", FakeDerivedWriter)


def test_fluxo_completo_grava_e_valida(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)

    assert run(make_config()) == EXIT_OK

    writer = FakeSheetWriter.instances[0]
    assert len(writer.upserted) == 1
    assert writer.upserted[0][9] == "ad1"
    assert writer.upserted[0][10] == "http://img/x.png"  # thumbnail entrou na coluna K
    assert writer.read_calls == 1, "a validação precisa reler a aba"

    derived = FakeDerivedWriter.instances[0]
    assert derived.rebuilt == writer.upserted
    assert derived.service is writer.service, "reusa o cliente autenticado"


def test_sem_dados_encerra_sem_erro_e_sem_escrever(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch, insights=[])

    assert run(make_config()) == EXIT_OK
    assert FakeSheetWriter.instances == [], "não pode nem autenticar no Sheets"


def test_dry_run_coleta_mas_valida_sem_reler(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRY_RUN: a coleta é real; a validação não relê a planilha."""
    install_fakes(monkeypatch)

    assert run(make_config(dry_run=True)) == EXIT_OK

    meta = FakeMetaClient.instances[0]
    assert meta.thumbnail_calls == [["ad1"]], "a coleta acontece de verdade em DRY_RUN"

    writer = FakeSheetWriter.instances[0]
    assert writer.read_calls == 0, "DRY_RUN não relê a planilha"


def test_falha_de_validacao_devolve_codigo_proprio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A aba volta com duplicata: o job precisa falhar para o CI acusar."""
    install_fakes(monkeypatch)

    def read_duplicado(self: Any, sheet_name: str | None = None) -> list[list[Any]]:
        self.read_calls += 1
        return [make_row("2026-07-13", "ad1"), make_row("2026-07-13", "ad1")]

    monkeypatch.setattr(FakeSheetWriter, "read_rows", read_duplicado)

    assert main_module.main_with_config(make_config()) == EXIT_VALIDATION


def test_erro_de_auth_na_meta_devolve_codigo_de_api(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)

    def explode(self: Any, since: str, until: str) -> list[dict[str, Any]]:
        raise MetaAuthError("token expirado")

    monkeypatch.setattr(FakeMetaClient, "get_insights", explode)

    assert main_module.main_with_config(make_config()) == EXIT_API


def test_erro_inesperado_nao_escapa_sem_log(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fakes(monkeypatch)

    def explode(self: Any, since: str, until: str) -> list[dict[str, Any]]:
        raise RuntimeError("algo muito errado")

    monkeypatch.setattr(FakeMetaClient, "get_insights", explode)

    assert main_module.main_with_config(make_config()) == EXIT_API
