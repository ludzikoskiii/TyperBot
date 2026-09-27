"""Błędy źródeł danych.

Atrybut `state` trafia do tabeli `source_status` i jest pokazywany w interfejsie,
dzięki czemu użytkownik widzi, dlaczego dane z danego źródła są niedostępne.
"""

from __future__ import annotations


class SourceError(Exception):
    state = "error"

    def __init__(self, message: str, *, source: str = ""):
        super().__init__(message)
        self.source = source
        self.message = message


class MissingKeyError(SourceError):
    state = "no_key"


class AuthError(SourceError):
    state = "auth"


class QuotaExceededError(SourceError):
    state = "quota"


class PlanRestrictionError(SourceError):
    state = "plan"


class SourceUnavailableError(SourceError):
    state = "offline"


class ParseError(SourceError):
    state = "error"


STATE_LABELS = {
    "ok": "OK",
    "no_key": "Brak klucza API",
    "auth": "Nieprawidłowy klucz",
    "quota": "Limit wyczerpany",
    "plan": "Niedostępne w planie darmowym",
    "offline": "Brak połączenia",
    "error": "Błąd",
    "idle": "Jeszcze nie pobierano",
    "disabled": "Wyłączone",
}
