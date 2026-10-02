"""Legacy oracles for the golden tests (TD-42 Black-Scholes, TD-25 contract_key).

Each module is a verbatim copy of code as it stood before a dedup, kept so a test can
prove the shared replacement returns the same bytes / the same floats. They are test
fixtures, not library code: never import them from src/, and never edit them to make a
test pass -- a difference means the replacement changed a number or a key.
"""
