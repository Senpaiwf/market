# market/backend/marketplaces/base.py
from abc import ABC, abstractmethod


class MarketplaceAdapter(ABC):
    @abstractmethod
    async def get_category_attributes(self, category_id: str) -> list[dict]:
        """Return list of attribute dicts for the given category_id."""
        ...

    @abstractmethod
    def map_ms_to_attributes(
        self, product: dict, attributes: list[dict]
    ) -> tuple[dict, list[str], list[str]]:
        """Map MoySklad product dict → marketplace attribute values.

        Returns:
            updated_fields: dict of {field_key: value} ready to save into answers_store
            warnings: non-critical messages (missing recommended fields)
            errors: critical errors
        """
        ...
