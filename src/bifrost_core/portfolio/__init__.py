"""Portfolio domain: account/position parsing, IB refresh helpers, and position book.

DB-backed ledger and analytics may live under ``src.portfolio.reader`` / ``src.portfolio.model``.
Runtime pricing math used for greeks remains in ``src.daemon.pricing``; this package may import it.
"""
