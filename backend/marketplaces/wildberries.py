# market/backend/marketplaces/wildberries.py
from .base import MarketplaceAdapter


class WildberriesAdapter(MarketplaceAdapter):
    """WB autofill stub — not yet implemented.

    Future implementation:
    1. GET /content/v2/object/charcs/{subjectID}?locale=ru  → list of charcs
    2. Map MS product fields using charcType: 1=string, 4=number
    3. Return {wb_{charcID}: value} dict to merge into answers_store['wb_chars']
    4. Extend categories_rating_weights.json with 'wb' section

    To activate: replace NotImplementedError with real logic in both methods.
    """

    async def get_category_attributes(self, category_id: str) -> list[dict]:
        raise NotImplementedError(
            "WildberriesAdapter.get_category_attributes not implemented. "
            "Use WildberriesClient from wb.py for now."
        )

    def map_ms_to_attributes(
        self, product: dict, attributes: list[dict]
    ) -> tuple[dict, list[str], list[str]]:
        raise NotImplementedError(
            "WildberriesAdapter.map_ms_to_attributes not implemented. "
            "Extend using wb_chars key in answers_store."
        )
