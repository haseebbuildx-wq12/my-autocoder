import calc

def test_add():
    assert calc.add(2, 3) == 5

def test_divide():
    assert calc.divide(10, 2) == 5.0

def test_divide_by_zero():
    try:
        calc.divide(10, 0)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
