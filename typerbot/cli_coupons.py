"""Polecenia etapu 3: ocena typów i generator kuponów (do czasu interfejsu graficznego)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from typerbot.config.settings import CouponSettings, MARKETS
from typerbot.data.db import Database
from typerbot.fmt import num as _pl, pct as _pct, plural, signed_pct
from typerbot.services.coupons import Coupon, CouponService, kickoff_local

LETTERS = "ABCDEFGHIJ"


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def coupon_settings_from_args(base: CouponSettings, args: argparse.Namespace) -> CouponSettings:
    cfg = replace(base, markets=list(base.markets), leagues=list(base.leagues))
    if getattr(args, "dzis", False):
        cfg.date_range = "today"
    elif getattr(args, "jutro", False):
        cfg.date_range = "tomorrow"
    elif getattr(args, "od", None) and getattr(args, "do", None):
        cfg.date_range, cfg.date_from, cfg.date_to = "custom", args.od, args.do
    elif getattr(args, "dni", None):
        cfg.date_range, cfg.days_ahead = "days", args.dni
    for attr, field in (("kurs", "target_odds"), ("tolerancja", "tolerance"), ("min", "min_events"),
                        ("max", "max_events"), ("min_p", "min_probability"), ("tryb", "mode"), ("stawka", "stake")):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, field, value)
    if getattr(args, "ligi", None):
        cfg.leagues = [x.strip().upper() for x in args.ligi.split(",")]
    if getattr(args, "rynki", None):
        cfg.markets = [x.strip().upper() for x in args.rynki.split(",") if x.strip().upper() in MARKETS]
    if getattr(args, "z_malo_danych", False):
        cfg.include_low_data = True
    return cfg


# -- typy ---------------------------------------------------------------------------------------
def print_selections(service: CouponService, cfg: CouponSettings, only_value: bool = False) -> None:
    settings = service.settings()
    evaluated = service.evaluate(cfg)
    w = settings.model.model_weight
    ref = settings.odds.bookmaker.capitalize() if settings.odds.reference == "bookmaker" else settings.odds.reference
    _out(f"Ocena typów – {len(evaluated)} meczów. Prognoza = {w:.0%} model + {1 - w:.0%} rynek; "
         f"kurs referencyjny: {ref} (gdy brak – średnia). ★ = value (prognoza·kurs > 1, przed podatkiem).")
    for info, evals in sorted(evaluated.values(), key=lambda x: x[0].kickoff):
        rows = [e for e in evals if not only_value or e.is_value]
        if not rows:
            continue
        flags = f"  [{'; '.join(info.flags)}]" if info.flags else ""
        _out(f"\n{kickoff_local(info.kickoff)}  {info.league:<4} {info.home} – {info.away}  "
             f"(oczekiwane gole {_pl(info.lam_home)}:{_pl(info.lam_away)}){flags}")
        _out(f"   {'Typ':<22}{'Prognoza':>9}{'Model':>7}{'Rynek':>7}{'Kurs':>7}  {'Źródło':<10}{'Implik.':>8}"
             f"{'EV':>8}{'EV po pod.':>12}")
        for e in rows:
            odds = _pl(e.odds) if e.odds else "–"
            ev = signed_pct(e.ev)
            after_txt = signed_pct(e.ev_after_tax(settings))
            star = " ★" if e.is_value else ""
            _out(f"   {e.label:<22}{_pct(e.probability):>9}{_pct(e.p_model):>7}{_pct(e.p_market):>7}{odds:>7}  "
                 f"{e.source_label:<10}{_pct(e.implied):>8}{ev:>8}{after_txt:>12}{star}")


# -- kupony -------------------------------------------------------------------------------------
def print_coupon(coupon: Coupon, letter: str, with_rationale: bool = True) -> None:
    lo, hi = coupon.target
    market = coupon.probability_market
    range_txt = "" if coupon.in_range else f"  ⚠ poza zakresem {_pl(lo)}–{_pl(hi)}"
    _out(f"\nKUPON {letter} – kurs {_pl(coupon.odds)} (po podatku {_pl(coupon.odds_after_tax)}), "
         f"{plural(len(coupon.legs), 'zdarzenie', 'zdarzenia', 'zdarzeń')}{range_txt}")
    _out(f"  szansa trafienia: {_pct(coupon.probability, 1)} (model {_pct(coupon.probability_model, 1)}"
         + (f", rynek {_pct(market, 1)}" if market is not None else "") + ")"
         f" · EV po podatku {signed_pct(coupon.ev)} (przed podatkiem {signed_pct(coupon.ev_before_tax)})"
         f" · stawka {_pl(coupon.stake)} zł → wygrana {_pl(coupon.payout)} zł")
    for i, leg in enumerate(coupon.legs, 1):
        s, m = leg.selection, leg.match
        odds = _pl(s.odds) if s.odds else "–"
        _out(f"  {letter}{i}. {kickoff_local(m.kickoff)} {m.league:<4} {m.home} – {m.away}: {s.label} "
             f"@ {odds} {s.source_label}".rstrip() + f" · prognoza {_pct(s.probability)}" + (" ★" if s.is_value else ""))
        if with_rationale:
            for line in leg.rationale:
                _out(f"       {line}")


def print_swap_options(service: CouponService, coupon: Coupon, letter: str, idx: int, cfg: CouponSettings) -> list:
    leg = coupon.legs[idx - 1]
    options = service.swap_options(coupon, leg.match.match_id, cfg)
    _out(f"\nZamienniki dla {letter}{idx} ({leg.match.home} – {leg.match.away}: {leg.selection.label}):")
    for n, o in enumerate(options, 1):
        s, m = o.leg.selection, o.leg.match
        mark = "" if o.in_range else "  (kurs poza zakresem)"
        _out(f"  {n:>2}. {kickoff_local(m.kickoff)} {m.home} – {m.away}: {s.label} @ {_pl(s.odds)} · prognoza "
             f"{_pct(s.probability)} → kurs kuponu {_pl(o.new_odds)}{mark}")
    if not options:
        _out("  brak zamienników spełniających warunki")
    return options


def _parse_leg(text: str) -> tuple[int, int]:
    letter, number = text[0].upper(), int(text[1:])
    return LETTERS.index(letter), number


def run_coupon_command(db: Database, args: argparse.Namespace, sync_service=None) -> int:
    service = CouponService(db)
    cfg = coupon_settings_from_args(service.settings().coupon, args)
    service.evaluate(cfg)
    if getattr(args, "dociagnij", False) and sync_service is not None:
        ids = service.top_candidate_matches(cfg, n=args.dociagnij_ile)
        _out(f"Dociągam kursy BTTS i podwójnej szansy dla {len(ids)} meczów (The Odds API)…")
        sync_service.run(lambda rep: sync_service.sync_event_markets(rep, ids))
        service.evaluate(cfg)
    coupons = service.generate(cfg, evaluate=False)
    start, end = service.date_window(cfg)
    mode = "najwyższe prawdopodobieństwo" if cfg.mode == "probability" else "najwyższa wartość (EV)"
    _out(f"Generator kuponów – mecze {kickoff_local(start.strftime('%Y-%m-%dT%H:%M:%SZ'))} – "
         f"{kickoff_local(end.strftime('%Y-%m-%dT%H:%M:%SZ'))}, kurs {_pl(cfg.target_odds)} ±{cfg.tolerance:.0%}, "
         f"{cfg.min_events}–{cfg.max_events} zdarzeń, min. {cfg.min_probability:.0%} na typ, tryb: {mode}")
    if not coupons:
        evaluated = service._evaluated
        with_odds = sum(any(e.odds for e in evals) for _, evals in evaluated.values())
        if not evaluated:
            _out("\nBrak nadchodzących meczów w tym zakresie dat i ligach – uruchom: python -m typerbot sync")
        elif not with_odds:
            _out(f"\nMeczów w zakresie: {len(evaluated)}, ale żaden nie ma kursów – uruchom: python -m typerbot sync")
        else:
            _out(f"\nNie udało się ułożyć kuponu (mecze z kursami: {with_odds}). Spróbuj: większej tolerancji, "
                 "niższego minimalnego prawdopodobieństwa, szerszego zakresu dat lub większej liczby zdarzeń.")
        return 1
    if getattr(args, "kurs_reczny", None):
        for item in args.kurs_reczny:
            leg_txt, value = item.split("=")
            ci, li = _parse_leg(leg_txt)
            coupon = coupons[ci]
            coupons[ci] = service.set_manual_odds(coupon, coupon.legs[li - 1].match.match_id, float(value.replace(",", ".")))
    if getattr(args, "wymien", None):
        ci, li = _parse_leg(args.wymien)
        coupon = coupons[ci]
        options = print_swap_options(service, coupon, LETTERS[ci], li, cfg)
        if args.na and options:
            coupons[ci] = service.swap(coupon, coupon.legs[li - 1].match.match_id, options[args.na - 1])
            _out(f"\nPo wymianie {args.wymien.upper()} na zamiennik nr {args.na}:")
            print_coupon(coupons[ci], LETTERS[ci])
            return 0
    for i, coupon in enumerate(coupons):
        print_coupon(coupon, LETTERS[i], with_rationale=not args.krotko)
    _out("\nSzansa trafienia zakłada niezależność meczów. „Model” bywa zawyżony przy kuponach – dlatego "
         "prognoza łączy go z rynkiem.")
    _out("Wymiana zdarzenia: --wymien A2 (pokaże zamienniki), potem --wymien A2 --na 3. "
         "Kurs z oferty: --kurs-reczny A2=1,95")
    return 0


def add_coupon_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dzis", action="store_true", help="tylko dzisiejsze mecze")
    g.add_argument("--jutro", action="store_true", help="tylko jutrzejsze mecze")
    g.add_argument("--dni", type=int, help="najbliższe X dni")
    p.add_argument("--od", help="własny zakres: od (RRRR-MM-DD)")
    p.add_argument("--do", help="własny zakres: do (RRRR-MM-DD)")
    p.add_argument("--kurs", type=float, help="kurs docelowy")
    p.add_argument("--tolerancja", type=float, help="np. 0.1 = ±10%%")
    p.add_argument("--min", type=int, help="minimalna liczba zdarzeń")
    p.add_argument("--max", type=int, help="maksymalna liczba zdarzeń")
    p.add_argument("--min-p", type=float, help="minimalne prawdopodobieństwo typu, np. 0.55")
    p.add_argument("--tryb", choices=["probability", "value"])
    p.add_argument("--ligi", help="np. PL,EKS")
    p.add_argument("--rynki", help="np. 1X2,DC,OU,BTTS")
    p.add_argument("--stawka", type=float)
    p.add_argument("--z-malo-danych", action="store_true", help="dopuść drużyny z małą liczbą meczów")
