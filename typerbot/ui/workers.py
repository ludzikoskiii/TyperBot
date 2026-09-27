"""Zadania w tle – pobieranie danych i obliczenia nie blokują interfejsu.

`run_in_background(fn, on_done, on_error)` uruchamia `fn` w puli wątków Qt, a
wynik (lub błąd) wraca do wątku interfejsu przez sygnał. W testach można
ustawić SYNCHRONOUS = True, żeby zadania wykonywały się od razu.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)
SYNCHRONOUS = False
_active: set["_Task"] = set()   # referencje, żeby zadania nie zostały usunięte przed końcem


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # błąd trafia do interfejsu jako komunikat
            log.error("Błąd zadania w tle: %s\n%s", exc, traceback.format_exc())
            self.signals.error.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            self.signals.done.emit(result)
        finally:
            _active.discard(self)


def run_in_background(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
                      on_error: Callable[[str], None] | None = None) -> None:
    task = _Task(fn)
    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.error.connect(on_error)
    _active.add(task)
    if SYNCHRONOUS:
        task.run()
    else:
        QThreadPool.globalInstance().start(task)


def wait_all(timeout_ms: int = 60000) -> bool:
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
