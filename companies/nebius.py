"""Nebius Group (NBIS) operating model - the second neocloud, a foreign private issuer.

Nebius files a 20-F once a year and furnishes 6-Ks in between, and 6-Ks carry no XBRL
obligation, so the company-facts feed is mostly annual (``CYyyyy`` frames) and quarterly
series can be sparse or empty. Its facts are tagged in both RUB and USD: the tidy loader
keeps USD and leaves the unit visible in ``self.reported["unit"]``. The CIK belonged to
Yandex N.V. until the July 2024 restructuring, so facts before 2024 describe a different
business and later filings restate the continuing operations. Plumbing is inherited from
``companies.base``; the driver logic is Philbert's to write in ``build()``.
"""

from __future__ import annotations

from companies.base import BaseCompanyModel


class Nebius(BaseCompanyModel):
    """Nebius Group N.V. - GPU cloud (20-F / 6-K filer); model pending until ``build()`` exists."""

    ticker = "NBIS"
    # name, cik and layer are copied from data.edgar.COMPANIES["NBIS"] by BaseCompanyModel.

    # Assumptions are read from assumptions/NBIS.csv when that file exists.
    engine_defaults = None

    def build(self) -> None:
        """Turn reported facts and assumptions into ``self.drivers`` and ``self.outputs``."""
        # TODO: Nebius operating model (see TODO.md, Stage 2), one column per period.
        #
        #   What is available after load_data():
        #     self.reported                     tidy SEC facts (same columns as CoreWeave);
        #                                       unit column is "USD" where SEC has both RUB
        #                                       and USD, "shares" for shares_outstanding
        #     self.reported_series("revenue", freq="A")   annual series; try freq="Q" but
        #                                       expect gaps because 6-Ks are rarely tagged
        #     self.filings                      20-F / 6-K list newest first; self.as_of is
        #                                       the latest filing date
        #     self.engine_defaults / self.default_inputs()   the assumptions above as an
        #                                       Inputs frame; set self.inputs to extend it
        #     engine.unit_economics             the per-GPU functions, shared with CoreWeave
        #
        #   What this method must produce:
        #     self.drivers, self.outputs        finance-layout frames per scripts/export_xlsx.py
        #                                       (index = snake_case line item, columns = period
        #                                       labels like "2025A" / "2026E", float values;
        #                                       optional attrs "labels", "units", "formulas")
        #
        #   Then delete the raise below.
        raise NotImplementedError("Nebius drivers not written yet - TODO.md, Stage 2")
