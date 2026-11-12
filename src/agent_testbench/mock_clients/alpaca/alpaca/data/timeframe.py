"""alpaca.data.timeframe of the mock alpaca package: TimeFrame and TimeFrameUnit as in alpaca-py 0.45.0."""
from enum import Enum

__all__ = ["TimeFrame", "TimeFrameUnit"]


class TimeFrameUnit(str, Enum):
    Minute = "Min"
    Hour = "Hour"
    Day = "Day"
    Week = "Week"
    Month = "Month"


class _preset:
    def __init__(self, unit):
        self.unit = unit

    def __get__(self, obj, owner):
        return owner(1, self.unit)


class TimeFrame:
    """A bar size: TimeFrame(5, TimeFrameUnit.Minute), or a preset such as TimeFrame.Day."""

    Minute = _preset(TimeFrameUnit.Minute)
    Hour = _preset(TimeFrameUnit.Hour)
    Day = _preset(TimeFrameUnit.Day)
    Week = _preset(TimeFrameUnit.Week)
    Month = _preset(TimeFrameUnit.Month)

    def __init__(self, amount: int, unit: TimeFrameUnit) -> None:
        self.validate_timeframe(amount, unit)
        self.amount_value = amount
        self.unit_value = unit

    @property
    def amount(self) -> int:
        return self.amount_value

    @property
    def unit(self) -> TimeFrameUnit:
        return self.unit_value

    @property
    def value(self) -> str:
        return f"{self.amount}{self.unit.value}"

    def __str__(self):
        return self.value

    @staticmethod
    def validate_timeframe(amount: int, unit: TimeFrameUnit):
        if not isinstance(unit, TimeFrameUnit):
            raise TypeError(f"unit must be a TimeFrameUnit (e.g. TimeFrameUnit.Minute), got {unit!r}; for a preset "
                            "pass it as the timeframe itself, e.g. TimeFrame.Hour")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("Amount must be a positive integer value.")
        if unit == TimeFrameUnit.Minute and amount > 59:
            raise ValueError("Second or Minute units can only be used with amounts between 1-59.")
        if unit == TimeFrameUnit.Hour and amount > 23:
            raise ValueError("Hour units can only be used with amounts 1-23")
        if unit in (TimeFrameUnit.Day, TimeFrameUnit.Week) and amount != 1:
            raise ValueError("Day and Week units can only be used with amount 1")
        if unit == TimeFrameUnit.Month and amount not in (1, 2, 3, 6, 12):
            raise ValueError("Month units can only be used with amount 1, 2, 3, 6 and 12")
