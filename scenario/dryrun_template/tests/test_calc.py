from src.calc import add


def test_add_ints():
    assert add(2, 3) == 5


def test_add_floats():
    assert add(0.5, 0.25) == 0.75
