"""alpaca.common.enums of the mock alpaca package."""
from enum import Enum

__all__ = ["Sort"]


class Sort(str, Enum):
    ASC = "asc"
    DESC = "desc"
