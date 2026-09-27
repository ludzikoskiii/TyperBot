"""Wspólny kontekst okna: baza, klucze, serwisy i sygnały odświeżania."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from typerbot.config.secrets import KEYED_SOURCES, MemorySecretStore, SecretStore, default_secret_store
from typerbot.config.settings import Settings, SettingsStore
from typerbot.data.db import Database
from typerbot.paths import db_path
from typerbot.services.register import CouponRegister
from typerbot.services.stats import StatsService
from typerbot.services.sync import SyncReport, SyncService


class DataHub(QObject):
    data_changed = Signal()        # nowe dane meczów/kursów (po synchronizacji)
    settings_changed = Signal()
    coupons_changed = Signal()     # zmiany w rejestrze kuponów
    message = Signal(str)          # komunikat do paska stanu
    busy = Signal(str, bool)       # (nazwa zadania, trwa?)


@dataclass
class AppContext:
    db: Database
    secrets: SecretStore
    sync: SyncService
    hub: DataHub
    demo: bool = False
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @property
    def settings_store(self) -> SettingsStore:
        return SettingsStore(self.db)

    def settings(self) -> Settings:
        return self.settings_store.load()

    def save_settings(self, settings: Settings) -> None:
        self.settings_store.save(settings)
        self.hub.settings_changed.emit()

    @property
    def register(self) -> CouponRegister:
        return CouponRegister(self.db, now=self.now)

    @property
    def stats(self) -> StatsService:
        return StatsService(self.db, now=self.now)

    def refresh(self, force: bool = False) -> tuple[SyncReport, list[int]]:
        """Synchronizacja + rozliczenie kuponów (wywoływane w wątku w tle)."""
        report = self.sync.run_all(force=force)
        settled = self.register.settle_pending()
        return report, settled

    def has_any_key(self) -> bool:
        return any(self.secrets.get(s) for s in KEYED_SOURCES)


def real_context() -> AppContext:
    db = Database(db_path())
    secrets = default_secret_store()
    return AppContext(db, secrets, SyncService(db, secrets), DataHub())


def demo_context(now: datetime | None = None, folder: Path | None = None) -> AppContext:
    """Tryb demo: dane syntetyczne, tymczasowa baza, bez kluczy i internetu."""
    from typerbot.demo.transport import DemoTransport
    from typerbot.demo.world import DemoWorld

    moment = now or datetime.now(timezone.utc)
    folder = folder or Path(tempfile.mkdtemp(prefix="typerbot-demo-"))
    db = Database(folder / "demo.db")
    secrets = MemorySecretStore({s: "demo-key-1234" for s in KEYED_SOURCES})
    sync = SyncService(db, secrets, transport=DemoTransport(DemoWorld(moment)), now=lambda: moment,
                       rate_limits=False)
    for league in sync.leagues.all():
        sync.leagues.set_enabled(league.code, league.code in ("PL", "EKS"))
    return AppContext(db, secrets, sync, DataHub(), demo=True, now=lambda: moment)
