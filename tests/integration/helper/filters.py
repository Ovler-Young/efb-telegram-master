"""
Useful filters to use with :meth:`TelegramIntegrationTestHelper.wait_for_update`.

Code structure inspired by Filters in python-telegram-bot_, which is licensed
under GPL v3.

.. _python-telegram-bot: https://github.com/python-telegram-bot/python-telegram-bot
"""

from typing import Optional

__all__ = ["BaseFilter", "MergedFilter", "InvertedFilter", "everything"]


class BaseFilter:
    """Base class for filters used with ``wait_for_update`` methods."""

    def __call__(self, event):
        return self.filter(event)

    def __and__(self, other):
        return MergedFilter(self, and_filter=other)

    def __or__(self, other):
        return MergedFilter(self, or_filter=other)

    def __invert__(self):
        return InvertedFilter(self)

    def filter(self, event) -> bool:
        raise NotImplementedError

    def __repr__(self):
        return f"{self.__class__.__name__}()"


class MergedFilter(BaseFilter):
    def __init__(self, base: BaseFilter, and_filter: Optional[BaseFilter] = None, or_filter: Optional[BaseFilter] = None):
        self.base = base
        self.and_filter = and_filter
        self.or_filter = or_filter

    def filter(self, event) -> bool:
        if self.and_filter is not None:
            return self.base(event) and self.and_filter(event)
        if self.or_filter is not None:
            return self.base(event) or self.or_filter(event)
        raise ValueError("and_filter and or_filter is both None.")

    def __repr__(self):
        if self.and_filter:
            symbol = "&"
            other = self.and_filter
        else:
            symbol = "|"
            other = self.or_filter
        return f"<{self.base} {symbol} {other}>"


class InvertedFilter(BaseFilter):
    def __init__(self, base: BaseFilter):
        self.base = base

    def filter(self, event) -> bool:
        return not self.base(event)

    def __repr__(self):
        return f"<! {self.base}>"


class _Everything(BaseFilter):
    def filter(self, _):
        return True

    def __repr__(self):
        return "Everything"


everything = _Everything()
"""Filter that allows every event to pass through."""
