from src.demo import add  # type: ignore[import-not-found]


def test_add():
    assert add(1, 2) == 3
