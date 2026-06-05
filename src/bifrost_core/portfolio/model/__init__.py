"""R-M8: Portfolio-level model analysis (payoff envelope, CAR, Greeks, stress).

Public entry point: compute_model_analysis(conn, account_id) -> dict.
"""

from bifrost_core.portfolio.model.core import compute_model_analysis

__all__ = ["compute_model_analysis"]
