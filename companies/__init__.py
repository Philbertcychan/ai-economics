"""Company models: one class per covered company, plus the registry the tools look up.

``scripts/export_xlsx.py`` and ``scripts/refresh.py`` never import a company module
directly; they call ``get_model(ticker)`` so adding a company is a one-line change here.
The shared interface (``CompanyModel``), base plumbing (``BaseCompanyModel``) and the
``ModelNotBuilt`` error are re-exported for the same reason.
"""

from __future__ import annotations

from companies.base import FRAME_ORDER, BaseCompanyModel, CompanyModel, ModelNotBuilt
from companies.coreweave import CoreWeave
from companies.nebius import Nebius

# Ticker -> model class. Only companies with a model class appear here; the wider list of
# covered filers (hyperscalers, NVIDIA) lives in data.edgar.COMPANIES and is refreshed
# without a model until one is written.
REGISTRY: dict[str, type[BaseCompanyModel]] = {
    CoreWeave.ticker: CoreWeave,
    Nebius.ticker: Nebius,
}


def get_model(ticker: str) -> type[BaseCompanyModel]:
    """Return the model class for ``ticker`` (case-insensitive).

    Raises:
        KeyError: no model is registered for the ticker; the message lists the known ones.
    """
    try:
        return REGISTRY[ticker.upper()]
    except KeyError:
        known = ", ".join(REGISTRY)
        raise KeyError(f"no company model for ticker {ticker!r}; known: {known}") from None


__all__ = [
    "FRAME_ORDER",
    "REGISTRY",
    "BaseCompanyModel",
    "CompanyModel",
    "CoreWeave",
    "ModelNotBuilt",
    "Nebius",
    "get_model",
]
