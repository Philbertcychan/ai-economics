"""CoreWeave (CRWV) operating model - the first neocloud in the repository.

CoreWeave is a US domestic filer (10-K / 10-Q, plus the S-1 and 424B4 from its March 2025
IPO), so ``self.reported`` carries quarterly ``CYyyyyQn`` frames and ``load_data()`` needs
no special handling. This file is deliberately a shell: the plumbing lives in
``companies.base`` and the driver logic is Philbert's to write in ``build()``.
"""

from __future__ import annotations

from companies.base import BaseCompanyModel


class CoreWeave(BaseCompanyModel):
    """CoreWeave, Inc. - GPU cloud; model pending until ``build()`` is written."""

    ticker = "CRWV"
    # name, cik and layer are copied from data.edgar.COMPANIES["CRWV"] by BaseCompanyModel.

    # TODO(philbert): set engine_defaults = GPUEconomicsInputs(...) with CoreWeave's assumptions.
    engine_defaults = None

    def build(self) -> None:
        """Turn reported facts and assumptions into ``self.drivers`` and ``self.outputs``."""
        # TODO(philbert): write the CoreWeave driver structure here, one column per period.
        #
        #   What is available after load_data():
        #     self.reported                     tidy SEC facts, one row per XBRL fact (columns:
        #                                       concept, tag, taxonomy, unit, start, end, val,
        #                                       fy, fp, form, filed, accn, frame); None offline
        #     self.reported_series("revenue")   calendar quarters (period, end, val, derived);
        #                                       freq="A" for years; any data.edgar.STANDARD_CONCEPTS
        #     self.filings                      list[Filing] newest first (.form, .filing_date,
        #                                       .url); self.as_of is the latest filing date
        #     self.engine_defaults / self.default_inputs()   the assumptions above as an
        #                                       Inputs frame; set self.inputs to extend it
        #     engine.unit_economics             cost_per_m_tokens, margin_per_gpu_hour,
        #                                       payback_months for the per-GPU layer
        #
        #   What this method must produce:
        #     self.drivers, self.outputs        pandas frames in the finance layout of
        #                                       scripts/export_xlsx.py: index = snake_case line
        #                                       item (unique), columns = period labels such as
        #                                       "2025A" / "2026E" / "2025Q2A", values float/NaN;
        #                                       optional df.attrs "labels", "units", "formulas"
        #                                       (formula templates make the Excel cells live)
        #
        #   Then delete the raise below; tests/test_companies.py expects NotImplementedError
        #   only while this is pending.
        raise NotImplementedError("CoreWeave drivers not written yet - TODO.md, Stage 1")
