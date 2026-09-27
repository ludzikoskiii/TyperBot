"""Strojenie parametrów modelu na historii (siatka wartości + backtest).

Kryterium: log-loss rynku 1X2 (im niższy, tym lepsze prognozy) – nie zysk,
bo zysk w backteście jest bardzo zaszumiony i łatwo go „przeuczyć”.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace

from typerbot.config.settings import CouponSettings, ModelSettings, TaxSettings
from typerbot.data.db import Database
from typerbot.model.backtest import BacktestConfig, run_backtest

DEFAULT_GRID = {
    "last_matches": [10, 15, 20, 30],
    "half_life_days": [90.0, 180.0, 365.0],
    "regularization": [3.0, 10.0, 30.0],
}


@dataclass
class TuningResult:
    settings: ModelSettings
    log_loss: float
    brier: float
    ece: float
    ou_log_loss: float
    market_log_loss: float | None
    best_model_weight: float | None = None   # najlepszy udział modelu w mieszance z rynkiem
    blend_log_loss: float | None = None


def tune(db: Database, config: BacktestConfig, base: ModelSettings, grid: dict[str, list] | None = None,
         progress=None) -> list[TuningResult]:
    grid = grid or DEFAULT_GRID
    names = list(grid)
    combos = list(itertools.product(*(grid[n] for n in names)))
    results: list[TuningResult] = []
    for i, values in enumerate(combos):
        settings = replace(base, **dict(zip(names, values)))
        if progress:
            progress(i, len(combos))
        res = run_backtest(db, config, settings, CouponSettings(), TaxSettings())
        m = res.metrics["1X2"]
        best_w, best_ll = min(res.blend, key=lambda x: x[1]) if res.blend else (None, None)
        results.append(TuningResult(settings, m.log_loss, m.brier, res.ece["1X2"], res.metrics["OU"].log_loss,
                                    m.market_log_loss, best_w, best_ll))
    return sorted(results, key=lambda r: r.log_loss)
