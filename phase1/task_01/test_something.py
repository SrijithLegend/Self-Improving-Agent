from something import (
    calculate_total,
    find_first_even,
    get_average,
)


def test_total_multiple_prices():
    assert calculate_total([10, 20, 30]) == 60


def test_total_single_price():
    assert calculate_total([15]) == 15


def test_total_empty_list():
    assert calculate_total([]) == 0


def test_find_first_even():
    assert find_first_even([3, 7, 8, 10]) == 8


def test_no_even_number():
    assert find_first_even([1, 3, 5]) is None


def test_average_normal_list():
    assert get_average([2, 4, 6]) == 4


def test_average_empty_list():
    assert get_average([]) is None
