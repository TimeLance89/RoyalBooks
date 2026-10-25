"""Only operator-configured indexers are active in the default book search.

Public-catalog implementations remain importable for historical data and tests.
"""
from __future__ import annotations
from royalbooks.providers import Provider


def configured_providers() -> list[Provider]:
    return []
