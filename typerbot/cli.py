"""Narzędzie wiersza poleceń (interfejs graficzny: python -m typerbot lub python -m typerbot gui).

  python -m typerbot demo                 synchronizacja na danych syntetycznych
  python -m typerbot demo --awaria openfootball   (symulacja awarii źródła)
  python -m typerbot sync [--force]       pobranie prawdziwych danych (bez kluczy API)
  python -m typerbot status               stan źródeł i data ostatnich danych
  python -m typerbot mecze [--dni 3]      nadchodzące mecze z kursami
  python -m typerbot druzyny              dopasowania nazw do sprawdzenia
  python -m typerbot prognozy [--dni 3]   prognozy modelu dla nadchodzących meczów
  python -m typerbot backtest [--ligi PL,EKS] [--sezony 2023,2024,2025]
  python -m typerbot strojenie [--zapisz] dobór parametrów modelu na historii
  python -m typerbot typy [--dni 3] [--value]   ocena typów (prognoza, kurs, EV, value)
  python -m typerbot kupon [--kurs 5] [--dni 3] [--tryb value] [--wymien A2 --na 3]
  python -m typerbot diagnoza [--kurs 5] [--dni 3]   dlaczego nie ma kuponu (źródła, filtry, powód)
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from typerbot.config.secrets import SecretStore, default_secret_store
from typerbot.data.db import Database
from typerbot.data.errors import STATE_LABELS
from typerbot.data.repository import odds_view
from typerbot.data.sources import SOURCE_LABELS
from typerbot.logging_setup import setup_logging
from typerbot.paths import db_path
from typerbot.services.sync import SyncReport, SyncService

LOCAL = ZoneInfo("Europe/Warsaw")
STEP_LABELS = {"history": "historia", "fixtures": "terminarz/wyniki", "names": "nazwy klubów"}


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def print_report(report: SyncReport) -> None:
    duration = (report.finished or report.started) - report.started
    _out(f"Synchronizacja zakończona w {duration:.1f} s\n")
    for source, steps in report.by_source().items():
        _out(f"  {SOURCE_LABELS.get(source, source)}")
        for s in steps:
            mark = "✓" if s.state == "ok" else ("·" if s.state == "skipped" else "✗")
            label = STEP_LABELS.get(s.step, s.step)
            league = f"[{s.league}]" if s.league else ""
            detail = f"{s.records} rekordów" if s.state == "ok" else f"{STATE_LABELS.get(s.state, s.state)}"
            extra = f" – {s.message}" if s.message and not s.message.startswith(detail) else ""
            _out(f"    {mark} {label:<17}{league:<7}{detail}{extra}")
    _out()


def print_status(service: SyncService) -> None:
    from typerbot.services.diagnostics import when_label

    _out("Źródła danych (wszystkie bez klucza i rejestracji)")
    _out(f"  {'Źródło':<38}{'Stan':<26}{'Dane z':<14}{'Dziś zapytań':>12}")
    for row in service.source_rows():
        label = STATE_LABELS.get(row.state, row.state)
        state = label + (f" ({row.message})" if row.message and row.state != "ok" and not row.message.startswith(label)
                         else "")
        _out(f"  {row.label:<38}{state[:25]:<26}{when_label(row.last_ok):<14}{row.calls_today:>12}")
    _out(f"  Cache: {service.http.cache_hits} trafień, {service.http.network_calls} zapytań sieciowych w tej sesji")
    counts = service.matches.counts()
    _out("\nBaza danych")
    _out(f"  ligi aktywne: {counts['leagues']}, drużyny: {counts['teams']}, mecze: {counts['matches']} "
         f"(zakończone {counts['finished']}, nadchodzące {counts['upcoming']}), z xG: {counts['with_xg']}, "
         f"kursy: {counts['odds']}")
    _out(f"  nazwy drużyn do sprawdzenia: {counts['aliases_to_review']}")
    from typerbot.services.diagnostics import sync_problems

    problems = sync_problems(service.last_report())
    if problems:
        _out(f"\nProblemy ze źródeł przy ostatniej synchronizacji ({len(problems)}):")
        for pr in problems:
            _out(f"  • {pr.text()}" + (f" → {pr.hint}" if pr.hint else ""))


def print_matches(service: SyncService, days: int) -> None:
    now = service.now()
    rows = service.matches.matches_between(now - timedelta(hours=2), now + timedelta(days=days))
    settings = service.settings()
    book = settings.odds.reference if settings.odds.reference in ("pinnacle", "bet365") else ""
    _out(f"\nNadchodzące mecze ({len(rows)}) – kursy: {book.capitalize() or 'najwyższy'} (średnia rynkowa)")
    _out(f"  {'Data':<12}{'Liga':<5}{'Mecz':<42}{'1':>13}{'X':>13}{'2':>13}{'>2.5':>13}{'BTTS tak':>13}")
    keys = [("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0), ("OU", "O", 2.5), ("BTTS", "Y", 0.0)]
    for r in rows:
        view = odds_view(service.matches.odds_for_match(r["id"]), book)
        when = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00")).astimezone(LOCAL).strftime("%d.%m %H:%M")
        match = f"{r['home_name']} – {r['away_name']}"
        _out(f"  {when:<12}{r['league_code']:<5}{match[:41]:<42}" + "".join(f"{_odds_cell(view, k):>13}" for k in keys))


def _odds_cell(view, key) -> str:
    """„kurs bukmachera (średnia rynkowa)”."""
    price = view.bookmaker.get(key) or view.best.get(key)
    b = f"{price:.2f}" if price else "–"
    a = f"({view.average[key]:.2f})" if key in view.average else "(–)"
    return f"{b} {a}"


def print_coverage(service: SyncService) -> None:
    """Ile zakończonych meczów z wynikiem mamy w każdej lidze i sezonie."""
    rows = service.db.query(
        "SELECT league_code, season, COUNT(*) AS n, SUM(home_xg IS NOT NULL) AS xg FROM matches "
        "WHERE status = 'FINISHED' AND home_goals IS NOT NULL GROUP BY league_code, season ORDER BY league_code, season")
    by_league: dict[str, list[str]] = {}
    for r in rows:
        xg = f", xG {r['xg']}" if r["xg"] else ""
        by_league.setdefault(r["league_code"], []).append(f"{r['season']}/{(r['season'] + 1) % 100:02d}: {r['n']}{xg}")
    _out("\nHistoria wyników w bazie (sezon: mecze)")
    for code, parts in by_league.items():
        _out(f"  {code:<5}" + " | ".join(parts))


def print_review(service: SyncService) -> None:
    rows = service.matches.matcher.review_list()
    _out(f"Dopasowania nazw do sprawdzenia: {len(rows)}")
    for r in rows:
        score = f"{r['score']:.0f}" if r["score"] is not None else "-"
        _out(f"  [{r['league_code']}] {SOURCE_LABELS.get(r['source'], r['source'])}: „{r['name']}” -> "
             f"„{r['team_name']}” (metoda: {r['method']}, zgodność {score})")


def print_aliases(service: SyncService, league: str) -> None:
    rows = service.db.query(
        "SELECT t.name AS team, a.source, a.name, a.method FROM team_aliases a JOIN teams t ON t.id = a.team_id "
        "WHERE a.league_code = ? ORDER BY t.name, a.source", (league,))
    by_team: dict[str, list[str]] = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(f"{r['name']} ({r['method']})")
    _out(f"\nDrużyny w lidze {league} i ich nazwy w źródłach ({len(by_team)}):")
    for team, names in by_team.items():
        _out(f"  {team:<28} {' | '.join(names)}")


def cmd_demo(args: argparse.Namespace) -> int:
    from typerbot.demo.transport import DemoTransport
    from typerbot.demo.world import DemoWorld

    now = datetime.now(timezone.utc)
    world = DemoWorld(now)
    transport = DemoTransport(world, fail=set(args.awaria or []))
    tmp = Path(tempfile.mkdtemp(prefix="typerbot-demo-"))
    db = Database(tmp / "demo.db")
    service = SyncService(db, transport=transport, now=lambda: now, rate_limits=False)
    world.enable_leagues(service.leagues)
    _out(f"TRYB DEMO – dane syntetyczne, baza tymczasowa: {tmp / 'demo.db'}")
    if args.awaria:
        _out(f"Symulowana awaria: {', '.join(args.awaria)}")
    _out()
    report = service.run_all()
    print_report(report)
    report2 = service.run_all()
    _out(f"Druga synchronizacja: {sum(1 for s in report2.steps if s.state == 'ok')} kroków OK, "
         f"{service.http.network_calls} zapytań sieciowych łącznie (reszta z cache/bazy)\n")
    print_status(service)
    print_coverage(service)
    print_matches(service, 4)
    print_aliases(service, "EKS")
    _out()
    print_review(service)
    _predict(db, 4, now=now)
    _demo_coupons(db, now)
    if args.backtest:
        _backtest(db, args, current=service.current_season())
    db.close()
    return 0


def _real_service(secrets: SecretStore | None = None) -> SyncService:
    return SyncService(Database(db_path()), secrets or default_secret_store())


def cmd_sync(args: argparse.Namespace) -> int:
    service = _real_service()
    report = service.run_all(force=args.force)
    print_report(report)
    print_status(service)
    print_coverage(service)
    return 0 if not report.errors else 2


def cmd_status(args: argparse.Namespace) -> int:
    print_status(_real_service())
    return 0


def cmd_matches(args: argparse.Namespace) -> int:
    print_matches(_real_service(), args.dni)
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    service = _real_service()
    print_review(service)
    if args.liga:
        print_aliases(service, args.liga)
    return 0




def _demo_coupons(db: Database, now: datetime) -> None:
    from typerbot.cli_coupons import print_coupon, print_selections
    from typerbot.services.coupons import CouponService

    service = CouponService(db, now=lambda: now)
    cfg = service.settings().coupon
    cfg.days_ahead = 7
    _out()
    print_selections(service, cfg, only_value=True)
    coupons = service.generate(cfg)
    _out(f"\nGenerator: {len(coupons)} kupony o kursie {cfg.target_odds:.2f} ±{cfg.tolerance:.0%} (mecze z 7 dni)")
    for letter, coupon in zip("ABC", coupons):
        print_coupon(coupon, letter)


def _predict(db: Database, days: int, now: datetime | None = None) -> None:
    from typerbot.cli_model import print_predictions
    from typerbot.services.predict import PredictionService

    now = now or datetime.now(timezone.utc)
    service = PredictionService(db, now=lambda: now)
    model = service.fit(now)
    if model is None:
        _out("Brak danych do dopasowania modelu – uruchom najpierw synchronizację.")
        return
    print_predictions(service.predict_between(now, now + timedelta(days=days)), model.summary())


def _has_history(db: Database) -> bool:
    row = db.query_one("SELECT COUNT(*) FROM matches WHERE status = 'FINISHED' AND home_goals IS NOT NULL")
    if row[0] < 200:
        _out("Za mało historii meczów w bazie – uruchom najpierw: python -m typerbot sync")
        return False
    return True


def _backtest(db: Database, args: argparse.Namespace, current: int) -> None:
    from typerbot.cli_model import print_backtest
    from typerbot.config.settings import SettingsStore
    from typerbot.model.backtest import BacktestConfig, default_seasons, run_backtest, save_run

    if not _has_history(db):
        return
    settings = SettingsStore(db).load()
    leagues = [x.strip().upper() for x in args.ligi.split(",")] if args.ligi else [
        r["code"] for r in db.query("SELECT code FROM leagues WHERE enabled = 1 AND is_cup = 0 ORDER BY sort_order")]
    seasons = [int(x) for x in args.sezony.split(",")] if args.sezony else default_seasons(db, leagues, current)
    coupon = settings.coupon
    if args.tryb:
        coupon.mode = args.tryb
    if args.kurs:
        coupon.target_odds = args.kurs
    config = BacktestConfig(leagues=leagues, seasons=seasons, value_threshold=args.prog,
                            odds_haircut=args.obnizka)

    def progress(league, li, nl, wi, nw):
        if wi % 10 == 0 and sys.stdout.isatty():
            sys.stdout.write(f"\r  liczę: {league} ({li + 1}/{nl}), tydzień {wi + 1}/{nw}   ")
            sys.stdout.flush()

    result = run_backtest(db, config, settings.model, coupon, settings.tax, progress=progress)
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 60 + "\r")
    save_run(db, result)
    print_backtest(result, coupon)


def cmd_tune(args: argparse.Namespace) -> int:
    from typerbot.config.settings import SettingsStore
    from typerbot.model.backtest import BacktestConfig, default_seasons
    from typerbot.model.tuning import tune

    service = _real_service()
    db = service.db
    if not _has_history(db):
        return 1
    store = SettingsStore(db)
    settings = store.load()
    leagues = [x.strip().upper() for x in args.ligi.split(",")] if args.ligi else [
        r["code"] for r in db.query("SELECT code FROM leagues WHERE enabled = 1 AND is_cup = 0 ORDER BY sort_order")]
    seasons = [int(x) for x in args.sezony.split(",")] if args.sezony else default_seasons(
        db, leagues, service.current_season())
    config = BacktestConfig(leagues=leagues, seasons=seasons)

    def progress(i, n):
        if sys.stdout.isatty():
            sys.stdout.write(f"\r  sprawdzam ustawienia {i + 1}/{n}   ")
            sys.stdout.flush()

    results = tune(db, config, settings.model, progress=progress)
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 50 + "\r")
    if not results:
        _out("Brak danych do strojenia.")
        return 1
    market = results[0].market_log_loss
    _out(f"Strojenie modelu – ligi {', '.join(leagues)}, sezony {', '.join(map(str, seasons))}"
         + (f" (log-loss rynku: {market:.4f})" if market else ""))
    _out(f"  {'Mecze':>6}{'Półokres':>10}{'Regular.':>10}{'Log-loss 1X2':>14}{'Brier':>8}{'Kalibracja':>12}{'LL O/U':>9}")
    current = (settings.model.last_matches, settings.model.half_life_days, settings.model.regularization)
    for r in results[:10]:
        m = r.settings
        mark = "  ← obecne" if (m.last_matches, m.half_life_days, m.regularization) == current else ""
        _out(f"  {m.last_matches:>6}{m.half_life_days:>10g}{m.regularization:>10g}{r.log_loss:>14.4f}{r.brier:>8.4f}"
             f"{100 * r.ece:>10.1f}pp{r.ou_log_loss:>9.4f}{mark}")
    best = results[0].settings
    weight = results[0].best_model_weight
    if weight is not None:
        _out(f"Najlepszy udział modelu w prognozie (mieszanka z rynkiem): {weight:.0%} "
             f"(log-loss {results[0].blend_log_loss:.4f})"
             + (" – model nie wnosi informacji ponad kursy" if weight == 0 else ""))
    if args.zapisz:
        settings.model.last_matches = best.last_matches
        settings.model.half_life_days = best.half_life_days
        settings.model.regularization = best.regularization
        if weight is not None:
            settings.model.model_weight = weight
        store.save(settings)
        _out(f"Zapisano: {best.last_matches} meczów, półokres {best.half_life_days:g} dni, "
             f"regularyzacja {best.regularization:g}"
             + (f", udział modelu {weight:.0%}." if weight is not None else "."))
    else:
        _out("Aby zapisać najlepsze ustawienia: python -m typerbot strojenie --zapisz")
    return 0


def cmd_selections(args: argparse.Namespace) -> int:
    from typerbot.cli_coupons import coupon_settings_from_args, print_selections
    from typerbot.services.coupons import CouponService

    service = CouponService(Database(db_path()))
    print_selections(service, coupon_settings_from_args(service.settings().coupon, args), only_value=args.value)
    return 0


def cmd_coupon(args: argparse.Namespace) -> int:
    from typerbot.cli_coupons import run_coupon_command

    sync = _real_service() if getattr(args, "diagnoza", False) else None
    return run_coupon_command(sync.db if sync else Database(db_path()), args, sync_service=sync)


def cmd_predict(args: argparse.Namespace) -> int:
    _predict(Database(db_path()), args.dni)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    service = _real_service()
    _backtest(service.db, args, service.current_season())
    return 0


def _add_backtest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ligi", help="np. PL,EKS (domyślnie włączone ligi krajowe)")
    p.add_argument("--sezony", help="rok rozpoczęcia sezonu, np. 2023,2024 (domyślnie 3 ostatnie zakończone)")
    p.add_argument("--tryb", choices=["probability", "value"], help="tryb doboru kuponów")
    p.add_argument("--kurs", type=float, help="kurs docelowy kuponu")
    p.add_argument("--prog", type=float, default=0.0, help="minimalna przewaga typu value, np. 0.05")
    p.add_argument("--obnizka", type=float, default=0.0,
                   help="obniżka kursów względem średniej rynkowej, np. 0.03 (wyższa marża polskiego bukmachera)")


def cmd_gui(args: argparse.Namespace) -> int:
    from typerbot.ui.app import run

    return run(demo=args.demo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="typerbot", description="TyperBot – dane, model i backtest")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("gui", help="interfejs graficzny (to samo co uruchomienie bez argumentów)")
    p.add_argument("--demo", action="store_true", help="tryb demo – dane syntetyczne, bez internetu")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("demo", help="synchronizacja na danych syntetycznych (bez internetu)")
    p.add_argument("--awaria", action="append", choices=list(SOURCE_LABELS), help="symuluj awarię źródła")
    p.add_argument("--backtest", action="store_true", help="uruchom też backtest modelu na danych demo")
    _add_backtest_args(p)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("sync", help="pobierz dane z prawdziwych źródeł")
    p.add_argument("--force", action="store_true", help="pomiń cache")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="stan źródeł i data ostatnich danych")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mecze", help="nadchodzące mecze z kursami")
    p.add_argument("--dni", type=int, default=3)
    p.set_defaults(func=cmd_matches)

    p = sub.add_parser("prognozy", help="prognozy modelu dla nadchodzących meczów")
    p.add_argument("--dni", type=int, default=3)
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser("backtest", help="test modelu na historycznych sezonach")
    _add_backtest_args(p)
    p.set_defaults(func=cmd_backtest)

    from typerbot.cli_coupons import add_coupon_args

    p = sub.add_parser("typy", help="ocena typów: prognoza, kurs, implikowane, EV, value")
    add_coupon_args(p)
    p.add_argument("--value", action="store_true", help="pokaż tylko typy value")
    p.set_defaults(func=cmd_selections)

    p = sub.add_parser("kupon", help="generator kuponów o zadanym kursie (3 alternatywy)")
    add_coupon_args(p)
    p.add_argument("--wymien", help="zdarzenie do wymiany, np. A2 (kupon A, pozycja 2)")
    p.add_argument("--na", type=int, help="numer zamiennika z listy")
    p.add_argument("--kurs-reczny", action="append", help="kurs z oferty bukmachera, np. A2=1,95")
    p.add_argument("--krotko", action="store_true", help="bez uzasadnień")
    p.set_defaults(func=cmd_coupon)

    p = sub.add_parser("diagnoza", help="dlaczego nie ma kuponu: źródła, kursy i mecze po każdym filtrze")
    add_coupon_args(p)
    p.set_defaults(func=cmd_coupon, diagnoza=True)

    p = sub.add_parser("strojenie", help="dobór parametrów modelu na historii (siatka + backtest)")
    p.add_argument("--ligi")
    p.add_argument("--sezony")
    p.add_argument("--zapisz", action="store_true", help="zapisz najlepsze ustawienia")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("druzyny", help="dopasowania nazw drużyn")
    p.add_argument("--liga", help="pokaż wszystkie nazwy w lidze, np. EKS")
    p.set_defaults(func=cmd_teams)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # polskie znaki w konsoli Windows
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    setup_logging(logging.INFO)
    return int(args.func(args) or 0)
