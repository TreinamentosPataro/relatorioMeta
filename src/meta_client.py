"""Cliente da Meta Marketing API: coleta de insights em nível de anúncio."""

from __future__ import annotations

from typing import Any, Callable, Final, Iterator, Sequence

from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.api import Cursor, FacebookAdsApi, FacebookAdsApiBatch, FacebookResponse
from facebook_business.exceptions import FacebookError, FacebookRequestError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Config
from src.logger import get_logger

logger = get_logger(__name__)

INSIGHT_FIELDS: Final[tuple[str, ...]] = (
    "account_name",
    "account_id",
    "objective",
    "campaign_name",
    "campaign_id",
    "adset_name",
    "adset_id",
    "ad_name",
    "ad_id",
    "spend",
    "impressions",
    "actions",
    "video_play_actions",
    "video_thruplay_watched_actions",
    "video_p25_watched_actions",
    "video_p50_watched_actions",
    "video_p75_watched_actions",
    "video_p100_watched_actions",
    "date_start",
)

# Registros por página. O máximo aceito pela API é 500; acima disso ela devolve
# erro de "reduce the amount of data" em contas grandes.
_PAGE_LIMIT: Final[int] = 500

# Anúncios por batch. O limite da Graph API é 50 sub-requisições por batch.
_BATCH_SIZE: Final[int] = 50

_MAX_ATTEMPTS: Final[int] = 6
_MAX_BATCH_RETRIES: Final[int] = 3

# Token inválido/expirado, sessão revogada, permissão insuficiente. Não adianta repetir.
_AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({102, 190, 200, 2500, 10})

# Rate limit / throttling da plataforma e das contas de anúncio.
_RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset(
    {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014}
)

# Falhas temporárias genéricas da API ("please retry your request later").
_TRANSIENT_ERROR_CODES: Final[frozenset[int]] = frozenset({1, 2, 341})


class MetaClientError(RuntimeError):
    """Falha permanente ao falar com a Marketing API."""


class MetaAuthError(MetaClientError):
    """Credencial inválida, expirada ou sem permissão. Nunca é retentada."""


class MetaRetryableError(MetaClientError):
    """Falha temporária (rate limit, indisponibilidade). Elegível a retry."""


def _describe(exc: FacebookRequestError) -> str:
    """Resumo legível de um erro da API."""
    return (
        f"http={exc.http_status()} code={exc.api_error_code()} "
        f"subcode={exc.api_error_subcode()} message={exc.api_error_message()!r}"
    )


def _classify(exc: FacebookRequestError) -> MetaClientError:
    """Traduz um erro da API na exceção interna correspondente."""
    code = exc.api_error_code()

    if code in _AUTH_ERROR_CODES:
        logger.error(
            "Falha de autenticação na Marketing API. Verifique META_ACCESS_TOKEN "
            "(token expirado/revogado) e as permissões ads_read do app.",
            extra={"error": _describe(exc)},
        )
        return MetaAuthError(f"Autenticação/permissão recusada pela Meta: {_describe(exc)}")

    if (
        code in _RATE_LIMIT_CODES
        or code in _TRANSIENT_ERROR_CODES
        or exc.api_transient_error()
        or (exc.http_status() or 0) >= 500
    ):
        return MetaRetryableError(f"Erro temporário da Meta: {_describe(exc)}")

    return MetaClientError(f"Erro da Marketing API: {_describe(exc)}")


def _log_retry(state: RetryCallState) -> None:
    """Loga cada nova tentativa com o tempo de espera aplicado."""
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "Erro temporário na Marketing API; tentando novamente.",
        extra={
            "attempt": state.attempt_number,
            "sleep_seconds": round(getattr(state.next_action, "sleep", 0.0), 1),
            "error": str(exc),
        },
    )


# Backoff exponencial: 5s, 10s, 20s, 40s, 80s (teto de 120s), 6 tentativas no total.
_retry_transient = retry(
    retry=retry_if_exception_type(MetaRetryableError),
    wait=wait_exponential(multiplier=5, max=120),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    before_sleep=_log_retry,
    reraise=True,
)


class MetaClient:
    """Encapsula o acesso à Marketing API do Meta (insights de anúncios)."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._api = FacebookAdsApi.init(
            app_id=config.meta_app_id,
            app_secret=config.meta_app_secret,
            access_token=config.meta_access_token,
            api_version=None,  # usa a versão padrão do SDK instalado
            crash_log=False,
        )
        self._account = AdAccount(self._normalized_account_id(), api=self._api)

    def _normalized_account_id(self) -> str:
        """Garante o prefixo act_ exigido pela API."""
        account_id = self._config.meta_ad_account_id.strip()
        return account_id if account_id.startswith("act_") else f"act_{account_id}"

    # -- Insights ----------------------------------------------------------

    def get_insights(self, since_date: str, until_date: str) -> list[dict[str, Any]]:
        """Coleta insights por anúncio e por dia no intervalo [since_date, until_date].

        Datas no formato YYYY-MM-DD. Devolve os registros brutos, sem normalizar
        as actions — isso é responsabilidade do transform.py.
        """
        params: dict[str, Any] = {
            "level": "ad",
            "time_increment": 1,
            "time_range": {"since": since_date, "until": until_date},
            "limit": _PAGE_LIMIT,
        }

        logger.info(
            "Coletando insights da Meta",
            extra={
                "account_id": self._normalized_account_id(),
                "since": since_date,
                "until": until_date,
                "insight_level": "ad",
            },
        )

        cursor = self._request_insights(params)

        records: list[dict[str, Any]] = []
        pages = 0
        # execute() já carregou a primeira página no cursor; daí em diante,
        # cada load_next_page() é uma requisição HTTP nova (e retentável).
        while True:
            page = self._drain_page(cursor)
            if page:
                pages += 1
                records.extend(page)
                logger.debug(
                    "Página de insights recebida",
                    extra={"page": pages, "records_in_page": len(page), "total": len(records)},
                )
            if not self._load_next_page(cursor):
                break

        logger.info(
            "Coleta de insights concluída",
            extra={
                "since": since_date,
                "until": until_date,
                "pages": pages,
                "records": len(records),
            },
        )
        return records

    @_retry_transient
    def _request_insights(self, params: dict[str, Any]) -> Cursor:
        """Dispara a consulta de insights e devolve o cursor com a primeira página."""
        try:
            return self._account.get_insights(fields=list(INSIGHT_FIELDS), params=params)
        except FacebookRequestError as exc:
            raise _classify(exc) from exc
        except FacebookError as exc:
            raise MetaClientError(f"Falha inesperada do SDK da Meta: {exc}") from exc

    @_retry_transient
    def _load_next_page(self, cursor: Cursor) -> bool:
        """Carrega a próxima página no cursor. False quando não há mais páginas."""
        try:
            return cursor.load_next_page()
        except FacebookRequestError as exc:
            raise _classify(exc) from exc
        except FacebookError as exc:
            raise MetaClientError(f"Falha inesperada do SDK da Meta: {exc}") from exc

    @staticmethod
    def _drain_page(cursor: Cursor) -> list[dict[str, Any]]:
        """Extrai como dicts os objetos já bufferizados no cursor.

        Lê o buffer por índice em vez de iterar o cursor: iterar dispara
        load_next_page() implicitamente, o que escaparia do retry.
        """
        return [cursor[index].export_all_data() for index in range(len(cursor))]

    # -- Thumbnails --------------------------------------------------------

    def get_thumbnails(self, ad_ids: Sequence[str]) -> dict[str, str | None]:
        """Busca a thumbnail_url do criativo de cada anúncio.

        Consulta em lotes de 50 via batch da Graph API. Anúncios sem criativo,
        sem thumbnail ou inacessíveis vêm com None.
        """
        unique_ids = list(dict.fromkeys(str(ad_id) for ad_id in ad_ids if ad_id))
        if not unique_ids:
            logger.info("Nenhum ad_id para buscar thumbnail.")
            return {}

        logger.info("Buscando thumbnails dos criativos", extra={"ads": len(unique_ids)})

        thumbnails: dict[str, str | None] = {ad_id: None for ad_id in unique_ids}
        for chunk in _chunked(unique_ids, _BATCH_SIZE):
            self._fetch_thumbnail_chunk(chunk, thumbnails)

        found = sum(1 for url in thumbnails.values() if url)
        logger.info(
            "Thumbnails concluídas",
            extra={"ads": len(unique_ids), "found": found, "missing": len(unique_ids) - found},
        )
        return thumbnails

    def _fetch_thumbnail_chunk(
        self, ad_ids: Sequence[str], thumbnails: dict[str, str | None]
    ) -> None:
        """Executa um batch de até 50 anúncios, gravando o resultado em thumbnails."""
        batch = self._api.new_batch()
        for ad_id in ad_ids:
            Ad(ad_id, api=self._api).api_get(
                fields=["adcreatives{thumbnail_url}"],
                batch=batch,
                success=_on_thumbnail_success(ad_id, thumbnails),
                failure=_on_thumbnail_failure(ad_id),
            )

        pending: FacebookAdsApiBatch | None = self._execute_batch(batch)
        attempts = 0
        # execute() devolve um batch com as sub-requisições que a Meta não chegou a
        # executar (resposta nula). Erros de verdade já foram tratados no callback.
        while pending is not None and attempts < _MAX_BATCH_RETRIES:
            attempts += 1
            logger.warning(
                "Sub-requisições do batch não executadas; reenviando.",
                extra={"attempt": attempts},
            )
            pending = self._execute_batch(pending)

        if pending is not None:
            logger.warning(
                "Batch de thumbnails desistiu após esgotar as tentativas; "
                "os anúncios restantes ficam sem thumbnail.",
                extra={"retries": _MAX_BATCH_RETRIES},
            )

    @_retry_transient
    def _execute_batch(self, batch: FacebookAdsApiBatch) -> FacebookAdsApiBatch | None:
        """Executa o batch. Devolve o batch das sub-requisições não executadas, ou None."""
        try:
            return batch.execute()
        except FacebookRequestError as exc:
            raise _classify(exc) from exc
        except FacebookError as exc:
            raise MetaClientError(f"Falha inesperada do SDK da Meta: {exc}") from exc


def _on_thumbnail_success(
    ad_id: str, thumbnails: dict[str, str | None]
) -> Callable[[FacebookResponse], None]:
    """Callback de sucesso: extrai a thumbnail_url do primeiro criativo."""

    def handler(response: FacebookResponse) -> None:
        body = response.json() or {}
        creatives = (body.get("adcreatives") or {}).get("data") or []
        url = creatives[0].get("thumbnail_url") if creatives else None
        thumbnails[ad_id] = url or None
        if not url:
            logger.debug("Anúncio sem thumbnail", extra={"ad_id": ad_id})

    return handler


def _on_thumbnail_failure(ad_id: str) -> Callable[[FacebookResponse], None]:
    """Callback de falha: erro de auth aborta; o resto vira thumbnail ausente."""

    def handler(response: FacebookResponse) -> None:
        exc = response.error()
        if isinstance(exc, FacebookRequestError):
            classified = _classify(exc)
            if isinstance(classified, MetaAuthError):
                raise classified
            logger.warning(
                "Falha ao buscar thumbnail do anúncio.",
                extra={"ad_id": ad_id, "error": _describe(exc)},
            )
        else:
            logger.warning(
                "Falha ao buscar thumbnail do anúncio.",
                extra={"ad_id": ad_id, "error": str(exc)},
            )

    return handler


def _chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Fatia a sequência em blocos de no máximo `size` elementos."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _main() -> int:
    """Teste manual: imprime a contagem de registros de ontem."""
    from datetime import datetime, timedelta

    import pytz

    from src.config import ConfigError, load_config
    from src.logger import setup_logging

    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging()
        logger.error("Falha de configuração: %s", exc)
        return 1

    setup_logging(config.log_level)

    tz = pytz.timezone(config.account_timezone)
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")

    if config.dry_run:
        logger.info(
            "DRY_RUN ativo: nenhuma chamada à Meta será feita.",
            extra={"would_query": {"since": yesterday, "until": yesterday}},
        )
        return 0

    client = MetaClient(config)
    records = client.get_insights(yesterday, yesterday)
    logger.info(
        "Teste manual concluído",
        extra={"date": yesterday, "records": len(records)},
    )

    if records:
        ad_ids = [row["ad_id"] for row in records if row.get("ad_id")]
        thumbnails = client.get_thumbnails(ad_ids[:_BATCH_SIZE])
        logger.info(
            "Amostra de thumbnails",
            extra={"requested": len(ad_ids[:_BATCH_SIZE]), "resolved": len(thumbnails)},
        )

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
