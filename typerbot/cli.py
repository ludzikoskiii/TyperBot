"""Narzędzie wiersza poleceń (etap 1 – zanim powstanie interfejs graficzny).

  python -m typerbot demo                 synchronizacja na danych syntetycznych
  python -m typerbot demo --awaria the_odds_api   (symulacja awarii źródła)
  python -m typerbot klucz api_football   zapis klucza API w Menedżerze poświadczeń
  python -m typerbot sync [--force]       pobranie prawdziwych danych
  python -m typerbot status               zużycie limitów i stan źródeł
  python -m typerbot mecze [--dni 3]      nadchodzące mecze z kursami
  python -m typerbot druzyny              dopasowania nazw do sprawdzenia
  python -m typerbot csv wlacz|wylacz     opcjonalny import CSV (uzupełnia sezon 2025/26)
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from typerbot.config.secrets import KEYED_SOURCES, MemorySecretStore, SecretStore, default_secret_store, mask
from typerbot.data.db import Database
from typerbot.data.errors import STATE_LABELS
from typerbot.data.repository import odds_view
from typerbot.logging_setup import setup_logging
from typerbot.paths import db_path
from typerbot.services.sync import SyncReport, SyncService

LOCAL = ZoneInfo("Europe/Warsaw")
STEP_LABELS = {"history": "historia", "fixtures": "terminarz/wyniki", "odds": "kursy", "xg": "xG",
               "results": "wyniki", "event_odds": "kursy BTTS/DC"}
SOURCE_LABELS = {"football_data_org": "football-data.org", "api_football": "API-Football",
                 "the_odds_api": "The Odds API", "oddspapi": "OddsPapi", "football_data_csv": "football-data.co.uk"}


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
    _out("Zużycie limitów API")
    _out(f"  {'Źródło':<22}{'Okres':<9}{'Zużyte':>8}{'Limit':>8}{'Zostało':>9}  {'Dziś zapytań':>12}  Stan")
    period_pl = {"minute": "minuta", "day": "dzień", "month": "miesiąc", "-": "-"}
    for q in service.quota_rows():
        used = "-" if q.used is None else str(q.used)
        limit = "-" if q.limit is None else str(q.limit)
        rem = "-" if q.remaining is None else str(q.remaining)
        label = STATE_LABELS.get(q.state, q.state)
        state = label + (f" ({q.message})" if q.message and q.state != "ok" and not q.message.startswith(label) else "")
        src = "nagł." if q.from_headers else "lok."
        _out(f"  {q.label:<22}{period_pl.get(q.period, q.period):<9}{used:>8}{limit:>8}{rem:>9}  "
             f"{q.calls_today:>12}  {state} [{src}]")
    _out(f"  Cache: {service.http.cache_hits} trafień, {service.http.network_calls} zapytań sieciowych w tej sesji")
    counts = service.matches.counts()
    _out("\nBaza danych")
    _out(f"  ligi aktywne: {counts['leagues']}, drużyny: {counts['teams']}, mecze: {counts['matches']} "
         f"(zakończone {counts['finished']}, nadchodzące {counts['upcoming']}), z xG: {counts['with_xg']}, "
         f"kursy: {counts['odds']}")
    _out(f"  nazwy drużyn do sprawdzenia: {counts['aliases_to_review']}")


def print_matches(service: SyncService, days: int) -> None:
    now = service.now()
    rows = service.matches.matches_between(now - timedelta(hours=2), now + timedelta(days=days))
    settings = service.settings()
    book = settings.odds.bookmaker.capitalize() or "bukmacher"
    _out(f"\nNadchodzące mecze ({len(rows)}) – kursy: {book} (średnia rynkowa)")
    _out(f"  {'Data':<12}{'Liga':<5}{'Mecz':<42}{'1':>13}{'X':>13}{'2':>13}{'>2.5':>13}{'BTTS tak':>13}")
    keys = [("1X2", "H", 0.0), ("1X2", "D", 0.0), ("1X2", "A", 0.0), ("OU", "O", 2.5), ("BTTS", "Y", 0.0)]
    for r in rows:
        view = odds_view(service.matches.odds_for_match(r["id"]), settings.odds.bookmaker)
        when = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00")).astimezone(LOCAL).strftime("%d.%m %H:%M")
        match = f"{r['home_name']} – {r['away_name']}"
        _out(f"  {when:<12}{r['league_code']:<5}{match[:41]:<42}" + "".join(f"{_odds_cell(view, k):>13}" for k in keys))


def _odds_cell(view, key) -> str:
    """„kurs bukmachera (średnia rynkowa)”."""
    b = f"{view.bookmaker[key]:.2f}" if key in view.bookmaker else "–"
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
    secrets = MemorySecretStore({s: "demo-key-1234" for s in KEYED_SOURCES})
    service = SyncService(db, secrets, transport=transport, now=lambda: now, rate_limits=False)
    for league in service.leagues.all():
        service.leagues.set_enabled(league.code, league.code in ("PL", "EKS"))
    if args.csv:
        settings = service.settings()
        settings.sync.csv_import = True
        service.settings_store.save(settings)
    _out(f"TRYB DEMO – dane syntetyczne, baza tymczasowa: {tmp / 'demo.db'}")
    if args.awaria:
        _out(f"Symulowana awaria: {', '.join(args.awaria)}")
    _out()
    report = service.run_all()
    print_report(report)
    report2 = service.run_all()
    _out(f"Druga synchronizacja: {sum(1 for s in report2.steps if s.state == 'ok')} kroków OK, "
         f"{service.http.network_calls} zapytań sieciowych łącznie (reszta z cache/bazy)\n")
    candidates = [r["id"] for r in service.matches.matches_between(now, now + timedelta(days=3))][:3]
    service.run(lambda rep: service.sync_event_markets(rep, candidates))
    print_status(service)
    print_coverage(service)
    print_matches(service, 4)
    print_aliases(service, "EKS")
    _out()
    print_review(service)
    db.close()
    return 0


def _real_service(secrets: SecretStore | None = None) -> SyncService:
    return SyncService(Database(db_path()), secrets or default_secret_store())


def cmd_key(args: argparse.Namespace) -> int:
    store = default_secret_store()
    if args.usun:
        store.delete(args.zrodlo)
        _out(f"Usunięto klucz {args.zrodlo}.")
        return 0
    value = getpass.getpass(f"Klucz API dla {SOURCE_LABELS[args.zrodlo]} (nie będzie widoczny): ").strip()
    if not value:
        _out("Nie podano klucza.")
        return 1
    store.set(args.zrodlo, value)
    _out(f"Zapisano klucz {mask(value)} w magazynie systemowym.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    service = _real_service()
    report = service.run_all(force=args.force, xg=not args.bez_xg)
    print_report(report)
    print_status(service)
    print_coverage(service)
    return 0 if not report.errors else 2


def cmd_status(args: argparse.Namespace) -> int:
    service = _real_service()
    store = service.secrets
    _out("Klucze API: " + ", ".join(f"{SOURCE_LABELS[s]}: {mask(store.get(s))}" for s in KEYED_SOURCES) + "\n")
    print_status(service)
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


def cmd_csv(args: argparse.Namespace) -> int:
    service = _real_service()
    settings = service.settings()
    settings.sync.csv_import = args.stan == "wlacz"
    service.settings_store.save(settings)
    _out("Import CSV z football-data.co.uk: " + ("WŁĄCZONY" if settings.sync.csv_import else "wyłączony"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="typerbot", description="TyperBot – narzędzia danych (etap 1)")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("demo", help="synchronizacja na danych syntetycznych (bez kluczy i internetu)")
    p.add_argument("--awaria", action="append", choices=list(SOURCE_LABELS), help="symuluj awarię źródła")
    p.add_argument("--csv", action="store_true", help="włącz opcjonalny import CSV z football-data.co.uk")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("klucz", help="zapisz lub usuń klucz API")
    p.add_argument("zrodlo", choices=list(KEYED_SOURCES))
    p.add_argument("--usun", action="store_true")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("sync", help="pobierz dane z prawdziwych źródeł")
    p.add_argument("--force", action="store_true", help="pomiń cache")
    p.add_argument("--bez-xg", action="store_true", help="nie pobieraj xG")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="limity API i stan źródeł")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mecze", help="nadchodzące mecze z kursami")
    p.add_argument("--dni", type=int, default=3)
    p.set_defaults(func=cmd_matches)

    p = sub.add_parser("csv", help="włącz/wyłącz opcjonalny import plików CSV (uzupełnia historię)")
    p.add_argument("stan", choices=["wlacz", "wylacz"])
    p.set_defaults(func=cmd_csv)

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
