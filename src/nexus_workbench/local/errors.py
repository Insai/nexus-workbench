"""The one error type the local tools raise for expected failures."""

from __future__ import annotations


class LocalToolError(Exception):
    """An expected failure, with a message written for a person.

    The message says what happened and what to do next. It never carries a full path, only
    the base name of the file the caller passed.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message
