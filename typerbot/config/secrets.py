"""Bezpieczne przechowywanie kluczy API.

Klucze trafiają do systemowego magazynu poświadczeń przez bibliotekę `keyring`
(na Windows: Menedżer poświadczeń / Windows Credential Locker). Nigdy nie są
zapisywane w bazie ani w plikach aplikacji.
"""

from __future__ import annotations

import logging
import threading

from typerbot import APP_NAME

log = logging.getLogger(__name__)

SERVICE = APP_NAME
KEYED_SOURCES = ("football_data_org", "the_odds_api", "oddspapi")
REMOVED_SOURCES = ("api_football",)   # źródła usunięte z aplikacji – ich klucze kasujemy z magazynu


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """Interfejs magazynu kluczy."""

    def get(self, source: str) -> str | None:
        raise NotImplementedError

    def set(self, source: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, source: str) -> None:
        raise NotImplementedError

    def has(self, source: str) -> bool:
        return bool(self.get(source))


class KeyringSecretStore(SecretStore):
    def __init__(self) -> None:
        import keyring  # import lokalny: biblioteka ładuje backend systemowy

        self._keyring = keyring

    def get(self, source: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE, source)
        except Exception as exc:  # backend niedostępny
            log.warning("Nie można odczytać klucza %s: %s", source, exc)
            return None

    def set(self, source: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE, source, value.strip())
        except Exception as exc:
            raise SecretStoreError(f"Nie można zapisać klucza w magazynie systemowym: {exc}") from exc

    def delete(self, source: str) -> None:
        try:
            self._keyring.delete_password(SERVICE, source)
        except Exception:
            pass


class MemorySecretStore(SecretStore):
    """Magazyn w pamięci – do testów i trybu demo."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data = dict(initial or {})
        self._lock = threading.Lock()

    def get(self, source: str) -> str | None:
        with self._lock:
            return self._data.get(source)

    def set(self, source: str, value: str) -> None:
        with self._lock:
            self._data[source] = value.strip()

    def delete(self, source: str) -> None:
        with self._lock:
            self._data.pop(source, None)


def default_secret_store() -> SecretStore:
    return KeyringSecretStore()


def mask(key: str | None) -> str:
    """Postać do wyświetlenia: tylko ostatnie 4 znaki."""
    if not key:
        return "brak"
    return "•" * 8 + key[-4:]
