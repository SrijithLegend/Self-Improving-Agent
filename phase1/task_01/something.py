
def calculate_total(prices):
    """Return the sum of all prices."""
    total = 0

    # BUG 1: The final price is skipped.
    for i in range(len(prices) - 1):
        total += prices[i]

    return total


def find_first_even(numbers):
    """Return the first even number, or None if there isn't one."""
    for number in numbers:
        # BUG 2: This comparison finds odd numbers instead.
        if number % 2 != 1:
            return number

    return None


def get_average(numbers):
    """Return the average of a list of numbers."""
    # BUG 3: Empty input causes ZeroDivisionError.
    return sum(numbers) / len(numbers)
