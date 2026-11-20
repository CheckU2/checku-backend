import pytest


@pytest.fixture(autouse=True)
def disable_real_database(monkeypatch):
    from app import database

    monkeypatch.setattr(
        database,
        "connect_database",
        lambda: None,
    )

    monkeypatch.setattr(
        database,
        "get_database",
        lambda: None,
    )

    monkeypatch.setattr(
        database,
        "ping_database",
        lambda: False,
    )