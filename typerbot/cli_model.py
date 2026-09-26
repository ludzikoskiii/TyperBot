"""Raporty tekstowe etapu 2: prognozy i backtest (do czasu interfejsu graficznego)."""

from __future__ import annotations

import math
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from typerbot.config.settings import CouponSettings, ModelSettings
from typerbot.model.backtest import BacktestResult
from typerbot.services.predict import MatchPrediction

LOCAL = ZoneInfo("Europe/Warsaw")
MARKET_NAMES = {"1X2": "1X2", "DC": "Podwójna szansa", "OU": "Powyżej/poniżej 2,5", "BTTS": "Obie strzelą"}


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _pct(x: float | None, digits: int = 1) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{digits}f}%"


def _num(x: float | None, digits: int = 3) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def _money(x: float) -> str:
    return f"{x:+,.2f} zł".replace(",", " ").replace(".", ",")


def print_model_settings(m: ModelSettings) -> None:
    _out(f"Model: Dixon-Coles{' (z korektą ρ)' if m.dixon_coles else ''}, ostatnie {m.last_matches} meczów drużyny, "
         f"półokres wygaszania {m.half_life_days:g} dni, udział xG {m.xg_weight:.0%}, "
         f"regularyzacja {m.regularization:g}, „mało danych” < {m.min_matches} meczów w roku")


def print_predictions(preds: list[MatchPrediction], summary: dict | None = None) -> None:
    if summary:
        strength = ", ".join(f"{k} {v:+.2f}" for k, v in summary["league_strength"].items())
        _out(f"Dopasowanie: {summary['matches']} meczów, {summary['teams']} drużyn, ρ = {summary['rho']:+.3f}"
             + (f", siła lig: {strength}" if len(summary["league_strength"]) > 1 else ""))
    _out(f"\nPrognozy ({len(preds)} meczów)")
    _out(f"  {'Data':<12}{'Liga':<5}{'Mecz':<40}{'xG':>10}{'1':>7}{'X':>7}{'2':>7}{'1X':>7}{'X2':>7}"
         f"{'>2.5':>7}{'BTTS':>7}  Uwagi")
    for p in preds:
        pr = p.prediction.probs
        when = datetime.fromisoformat(p.kickoff.replace("Z", "+00:00")).astimezone(LOCAL).strftime("%d.%m %H:%M")
        flags = []
        if p.prediction.low_data_home or p.prediction.low_data_away:
            who = [n for n, low in ((p.home, p.prediction.low_data_home), (p.away, p.prediction.low_data_away)) if low]
            flags.append("mało danych: " + ", ".join(who))
        if p.prediction.cross_league:
            flags.append("różne ligi – niższa pewność")
        match = f"{p.home} – {p.away}"
        cols = [pr[("1X2", "H", 0.0)], pr[("1X2", "D", 0.0)], pr[("1X2", "A", 0.0)], pr[("DC", "1X", 0.0)],
                pr[("DC", "X2", 0.0)], pr[("OU", "O", 2.5)], pr[("BTTS", "Y", 0.0)]]
        xg = f"{p.prediction.lam_home:.2f}:{p.prediction.lam_away:.2f}"
        _out(f"  {when:<12}{p.league:<5}{match[:39]:<40}{xg:>10}" + "".join(f"{_pct(c, 0):>7}" for c in cols)
             + ("  " + "; ".join(flags) if flags else ""))


def _bar(p: float, width: int = 20) -> str:
    filled = int(round(p * width))
    return "█" * filled + "·" * (width - filled)


def print_backtest(res: BacktestResult, coupon: CouponSettings) -> None:
    cfg = res.config
    seasons = ", ".join(f"{s}/{(s + 1) % 100:02d}" for s in cfg.seasons)
    _out(f"\nBACKTEST – ligi: {', '.join(cfg.leagues)} | sezony: {seasons}")
    _out(f"{len(res.rows)} meczów, {res.fits} dopasowań modelu (co tydzień), {res.seconds:.1f} s"
         + (f", pominięte tygodnie bez wystarczającej historii: {res.skipped_weeks}" if res.skipped_weeks else ""))
    print_model_settings(res.model)

    _out("\n1) SKUTECZNOŚĆ – porównanie z rynkiem (kursy zamknięcia bez marży). Niższy log-loss/Brier = lepiej.")
    _out(f"  {'Rynek':<22}{'Mecze':>7}{'Trafność':>10}{'Log-loss':>10}{'Brier':>8}   |"
         f"{'z kursami':>10}{'Model LL':>10}{'Rynek LL':>10}{'Rynek traf.':>12}  Model lepszy od rynku?")
    for m, x in res.metrics.items():
        if x.n == 0:
            continue
        verdict = {True: "tak", False: "nie", None: "brak kursów"}[x.beats_market]
        _out(f"  {MARKET_NAMES[m]:<22}{x.n:>7}{_pct(x.accuracy):>10}{_num(x.log_loss):>10}{_num(x.brier):>8}   |"
             f"{x.n_market:>10}{_num(x.model_log_loss_on_market):>10}{_num(x.market_log_loss):>10}"
             f"{_pct(x.market_accuracy):>12}  {verdict}")

    _out("\n2) KALIBRACJA – czy np. 60% w modelu to ok. 60% trafień (wszystkie typy danego rynku)")
    for m in ("1X2", "OU", "BTTS"):
        bins = res.calibration.get(m) or []
        if not bins:
            continue
        _out(f"  {MARKET_NAMES[m]} – średni błąd kalibracji (ECE): {100 * res.ece[m]:.1f} pkt proc.")
        for b in bins:
            _out(f"    {100 * b.lo:>3.0f}–{100 * b.hi:<3.0f}%  n={b.n:>5}  model {_pct(b.predicted):>6}  "
                 f"faktycznie {_pct(b.observed):>6}  {_bar(b.observed)}")

    s = res.singles
    tax = "pokrywa bukmacher" if res.tax.bookmaker_pays_tax else f"{res.tax.stake_tax:.0%} od stawki"
    _out(f"\n3) WYNIK FINANSOWY – stawka {cfg.stake:.0f} zł, podatek: {tax}"
         + (f", kursy obniżone o {cfg.odds_haircut:.0%}" if cfg.odds_haircut else ""))
    _out(f"  Pojedyncze typy „value” (p·kurs − 1 > {cfg.value_threshold:.0%}, max 1 na mecz): {s.bets} zakładów, "
         f"trafność {_pct(s.hit_rate)}, śr. kurs {s.avg_odds:.2f}, wynik {_money(s.profit)}, ROI {_pct(s.roi)}")
    for label, group in (("wg rynku", res.singles_by_market), ("wg ligi", res.singles_by_league),
                         ("wg przewagi modelu nad kursem", res.singles_by_edge)):
        items = group.items() if label.startswith("wg przewagi") else sorted(group.items())
        parts = [f"{k}: {v.bets} zakł., ROI {_pct(v.roi)}" for k, v in items]
        if parts:
            _out(f"    {label}: " + " | ".join(parts))
    p = res.singles_positive_after_tax
    _out(f"    z dodatnim EV także po podatku (p·kurs·0,88 > 1): {p.bets} zakładów, wynik {_money(p.profit)}, "
         f"ROI {_pct(p.roi)}")
    c = res.coupons
    mode = "najwyższe prawdopodobieństwo" if coupon.mode == "probability" else "najwyższa wartość (EV)"
    _out(f"  Kupony (kurs {coupon.target_odds:.2f} ±{coupon.tolerance:.0%}, {coupon.min_events}–{coupon.max_events} "
         f"zdarzeń, min. {coupon.min_probability:.0%} na typ, tryb: {mode}; 1 kupon na tydzień):")
    if c.bets:
        recs = res.coupon_records
        avg_p = sum(r.probability for r in recs) / len(recs)
        mk = [r.market_probability for r in recs if r.market_probability is not None]
        market_txt = f", rynek {_pct(sum(mk) / len(mk))}" if mk else ""
        _out(f"    {c.bets} kuponów, trafione {c.hits} ({_pct(c.hit_rate)}), śr. kurs {c.avg_odds:.2f}, "
             f"wynik {_money(c.profit)}, ROI {_pct(c.roi)}")
        _out(f"    szacowana szansa trafienia kuponu: model {_pct(avg_p)}{market_txt}, faktycznie {_pct(c.hit_rate)}")
    else:
        _out("    brak kuponów spełniających warunki")
    if res.blend:
        best_w, best_ll = min(res.blend, key=lambda x: x[1])
        model_ll = res.blend[-1][1]
        market_ll = res.blend[0][1]
        _out("\n4) MODEL A RYNEK – log-loss 1X2 dla mieszanki „w·model + (1−w)·rynek”")
        _out("  " + " ".join(f"{int(100 * w)}%:{ll:.4f}" for w, ll in res.blend))
        _out(f"  sam rynek {market_ll:.4f}, sam model {model_ll:.4f}, najlepiej przy {best_w:.0%} modelu ({best_ll:.4f})")
        if best_w == 0.0:
            _out("  → model nie wnosi informacji ponad kursy – typy „value” to głównie jego błędy.")
        elif best_w < 1.0:
            _out("  → model wnosi część informacji; mieszanka z rynkiem jest lepsza niż każdy z osobna.")
    for note in res.notes:
        _out(f"  Uwaga: {note}")
