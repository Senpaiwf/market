# market/backend/marketplaces/service.py
from __future__ import annotations
import logging

from .models import FillResponse, RatingResult
from .rating_calculator import calculate_rating
from .yandex import YandexAdapter
from .ozon import OzonAdapter
from .wildberries import WildberriesAdapter

logger = logging.getLogger(__name__)


class MarketplaceAPIError(Exception):
    pass


class AttributeMappingError(Exception):
    pass


def _empty_rating() -> RatingResult:
    return RatingResult(
        score=0.0, missing_mandatory=[], recommendations=[], status="low", details=[]
    )


def _error_response(marketplace: str, category_id: str, error: str) -> FillResponse:
    return FillResponse(
        status="error",
        marketplace=marketplace,
        category_id=category_id,
        rating=0.0,
        rating_result=_empty_rating(),
        updated_fields={},
        warnings=[],
        errors=[error],
    )


async def fill_card_attributes(
    code: str,
    marketplace: str,
    category_id: str,
    ms_client,
    ozon_client=None,
) -> FillResponse:
    """Main entry point for autofill.

    Args:
        code: MoySklad product code
        marketplace: 'yandex' | 'ozon' | 'wb'
        category_id: marketplace category id string (e.g. '17677661' for YM, '17028910_91875' for Ozon)
        ms_client: MoySkladClient instance
        ozon_client: OzonClient instance (required for Ozon API fallback on cache miss)

    Returns:
        FillResponse with updated_fields ready to save into answers_store
    """
    # WB is a stub — return early with user-friendly error
    if marketplace == "wb":
        return _error_response(marketplace, category_id, "Wildberries: автозаполнение ещё не реализовано")

    if marketplace not in ("yandex", "ozon"):
        return _error_response(marketplace, category_id, f"Неизвестный маркетплейс: {marketplace}")

    # Fetch product data from MoySklad
    try:
        product = await ms_client.get_product_data(code)
    except Exception as e:
        raise MarketplaceAPIError(f"МойСклад недоступен: {e}") from e

    if not product.get("ok"):
        return _error_response(
            marketplace, category_id,
            product.get("error", "Ошибка получения данных из МойСклад"),
        )

    # Pick adapter
    adapter = YandexAdapter() if marketplace == "yandex" else OzonAdapter(ozon_client)

    warnings: list[str] = []
    errors: list[str] = []
    updated_fields: dict = {}

    # Fetch category attributes (from cache or API)
    try:
        attributes = await adapter.get_category_attributes(category_id)
    except Exception as e:
        logger.error("get_category_attributes failed: %s", e)
        attributes = []
        errors.append("Техническая задержка при загрузке атрибутов. Данные обновлены частично.")

    if not attributes:
        warnings.append("Категория не найдена в справочнике. Заполнение пропущено.")

    # Map MS product → attribute values
    try:
        updated_fields, attr_warnings, attr_errors = adapter.map_ms_to_attributes(
            product, attributes
        )
        warnings.extend(attr_warnings)
        errors.extend(attr_errors)
    except Exception as e:
        raise AttributeMappingError(f"Ошибка маппинга атрибутов: {e}") from e

    rating_result = calculate_rating(product, updated_fields, marketplace, category_id)

    # Determine overall status
    if errors and not updated_fields:
        status = "error"
    elif warnings or errors:
        status = "partial"
    else:
        status = "success"

    return FillResponse(
        status=status,  # type: ignore[arg-type]
        marketplace=marketplace,
        category_id=category_id,
        rating=rating_result.score,
        rating_result=rating_result,
        updated_fields=updated_fields,
        warnings=warnings,
        errors=errors,
    )
