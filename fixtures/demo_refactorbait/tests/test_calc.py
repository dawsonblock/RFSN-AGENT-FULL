from src.calc import divide  # type: ignore[import-not-found]


def test_divide_normal():
    assert divide(6, 2) == 3.0


def test_divide_by_zero_returns_inf():
    assert divide(1, 0) == float('inf')
