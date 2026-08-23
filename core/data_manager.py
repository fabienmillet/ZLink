"""DataManager — polling QTimer-based des APIs ZEvent."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

from dotenv import load_dotenv
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

load_dotenv()  # charge .env depuis le dossier courant (ou parents)

_T = TypeVar("_T")


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Lance une coroutine sans installer les signal handlers asyncio (requis sous Qt).

    asyncio.run() installe SIGINT/SIGTERM sur Python 3.12+ et entre en conflit
    avec la boucle d'événements Qt, provoquant un CancelledError → KeyboardInterrupt.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()

from core.api_client import (
    DonationGoal,
    EventItem,
    GlobalStats,
    GoalWithStreamer,
    Participation,
    StreamerInfo,
    _format_euros,
    fetch_donation_goals,
    fetch_events,
    fetch_participations,
    fetch_zevent_data,
)
from core.history_store import HistoryStore

logger = logging.getLogger(__name__)

# ZEvent 2026 : 3 sept. 18:00 UTC → 7 sept. 00:00 UTC (schedule de l'API events)
_EVENT_DAYS = ["2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"]

_AVATAR_MAX_BYTES = 2 * 1024 * 1024   # plafond de lecture pour un avatar

_STREAMER_POLL_MS = 30_000    # 30 s — zevent.fr/api/ + participations
_EVENTS_POLL_MS  = 600_000   # 10 min
_GOALS_POLL_MS   = 300_000   # 5 min


# ---------------------------------------------------------------------------
# Async helpers (run several coroutines in one asyncio.run() call)
# ---------------------------------------------------------------------------

async def _gather_zevent_gdoc() -> tuple[list[Participation], GlobalStats, list[StreamerInfo]]:
    """Appels zevent.fr/api/ et participations evenmorestats en parallèle."""
    participations, (stats, streamers) = await asyncio.gather(
        fetch_participations(),
        fetch_zevent_data(),
    )
    return participations, stats, streamers  # type: ignore[return-value]


def _apply_participations_to_streamers(
    streamers: list[StreamerInfo],
    stats: GlobalStats,
    participations: list[Participation],
) -> None:
    """Complète les StreamerInfo avec les données des participations evenmorestats.

    Hors event, zevent.fr/api/ ne renvoie ni location, ni cagnotte par streamer,
    ni statut live : l'API communautaire fournit tout ça, rafraîchi côté serveur
    toutes les ~55 s. En mode live, les données ZEvent font foi et ne sont
    jamais écrasées.
    """
    by_login = {p.twitch_login: p for p in participations}
    live_mode = stats.website_mode == "live"

    for s in streamers:
        part = by_login.get(s.twitch_login.lower())
        if part is None:
            continue
        if not s.display or s.display == s.twitch_login:
            s.display = part.display or s.display
        if not s.location:
            s.location = part.location
        if s.donation <= 0.0 and part.donation > 0.0:
            s.donation = part.donation
            s.donation_formatted = _format_euros(part.donation)
        if not s.profile_url:
            s.profile_url = part.profile_url
        if not live_mode:
            s.online = part.live
            s.viewers = part.viewers
            s.game = part.game
            s.title = ""

    if not live_mode:
        stats.viewers_total = sum(p.viewers for p in participations if p.live)


async def _gather_events() -> list[list[EventItem]]:
    return await asyncio.gather(*[fetch_events(d) for d in _EVENT_DAYS])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DataManager
# ---------------------------------------------------------------------------

class DataManager(QObject):
    """Orchestre les appels API et propage les données via Qt signals."""

    streamers_updated      = pyqtSignal(list)    # list[StreamerInfo]
    global_stats_updated   = pyqtSignal(object)  # GlobalStats
    events_updated         = pyqtSignal(list)    # list[EventItem] (tous les jours)
    history_updated        = pyqtSignal(object)  # HistoryStore
    goals_updated          = pyqtSignal(list)    # list[GoalWithStreamer]
    goals_raw_updated      = pyqtSignal(dict)    # dict[login, list[DonationGoal]] — cache brut
    goal_accomplished      = pyqtSignal(str, str) # (login, goal_name) — nouvel objectif accompli

    # signaux internes pour le cross-thread (worker → main thread)
    _sig_streamers_ready   = pyqtSignal(object, object, list)  # participations, stats, streamers
    _sig_events_ready      = pyqtSignal(list)                  # list[list[EventItem]]
    _sig_goals_ready       = pyqtSignal(list)                  # list[GoalWithStreamer]

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._streamers: list[StreamerInfo] = []
        self._participations: list[Participation] = []
        self._stats = GlobalStats(0.0, "0 €", 0, "offline")
        self._events: dict[str, list[EventItem]] = {}
        self._gdoc_map: dict[str, str] = {}          # twitch_login → streamer_id
        self._participation_map: dict[str, str] = {}  # twitch_login → participation_id
        self._uuid_to_name: dict[str, str] = {}      # streamer_id → display_name
        self._history: HistoryStore = HistoryStore()
        self._goals_cache: dict[str, list[DonationGoal]] = {}
        self._accomplished_goals: set[tuple[str, str]] = set()
        self._goals_init_done: bool = False

        # Verrous anti-overlap pour les workers background
        self._polling_streamers: bool = False
        self._polling_events: bool = False


        self._timer_streamers = QTimer(self)
        self._timer_streamers.setInterval(_STREAMER_POLL_MS)
        self._timer_streamers.timeout.connect(self._poll_streamers)

        self._timer_events = QTimer(self)
        self._timer_events.setInterval(_EVENTS_POLL_MS)
        self._timer_events.timeout.connect(self._poll_events)

        self._timer_goals = QTimer(self)
        self._timer_goals.setInterval(_GOALS_POLL_MS)
        self._timer_goals.timeout.connect(self._start_goals_prefetch)

        # connexions internes cross-thread (queued automatiquement si thread différent)
        self._sig_streamers_ready.connect(self._apply_streamers)
        self._sig_events_ready.connect(self._apply_events)
        self._sig_goals_ready.connect(self._apply_goals)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Lance les polls (premier appel immédiat, puis périodique)."""
        # Historique 2026 en background — ne pas bloquer l'UI au démarrage
        threading.Thread(target=self._history_worker, daemon=True).start()
        self._poll_streamers()   # 1. zevent + participations → peuple _streamers et _stats
        self._poll_events()
        # goals déclenchés depuis _apply_streamers au 1er chargement
        self._timer_streamers.start()
        self._timer_events.start()
        self._timer_goals.start()

    def stop_polling(self) -> None:
        """Arrête tous les timers de polling (mode mock)."""
        self._timer_streamers.stop()
        self._timer_events.stop()
        self._timer_goals.stop()

    def _history_worker(self) -> None:
        """Charge l'historique 2026 en arrière-plan puis émet le signal."""
        try:
            _run(self._history.load_historical_2026())
        except Exception as exc:
            logger.error("_history_worker: %s", exc)
        self.history_updated.emit(self._history)

    # -- queries --------------------------------------------------------------

    def reload_config(self, config: dict) -> None:
        """Point d'entrée de rechargement à chaud (aucun réglage réseau à ce jour)."""
        return

    def get_streamers_live(self) -> list[StreamerInfo]:
        return [s for s in self._streamers if s.online]

    def get_top_donations(self, n: int = 5) -> list[StreamerInfo]:
        return sorted(self._streamers, key=lambda s: -s.donation)[:n]

    def get_events_for_day(self, day: str) -> list[EventItem]:
        return self._events.get(day, [])

    def get_streamer(self, login: str) -> StreamerInfo | None:
        key = login.lower()
        for s in self._streamers:
            if s.twitch_login.lower() == key:
                return s
        return None

    def get_gdoc_id(self, login: str) -> str | None:
        """streamer_id evenmorestats (identifie le streamer entre les éditions)."""
        return self._gdoc_map.get(login.lower())

    def get_participation_id(self, login: str) -> str | None:
        """participation_id de l'édition courante — clé des donation goals."""
        return self._participation_map.get(login.lower())

    def resolve_participant_uuid(self, uuid: str) -> str:
        """Retourne le display name d'un participant depuis son UUID gdoc."""
        return self._uuid_to_name.get(uuid, "")

    def get_stats(self) -> GlobalStats:
        return self._stats

    # -- polling --------------------------------------------------------------

    def _poll_streamers(self) -> None:
        """Déclenche le poll en background (ne bloque pas l'UI)."""
        if self._polling_streamers:
            logger.debug("_poll_streamers: déjà en cours, ignoré")
            return
        self._polling_streamers = True
        threading.Thread(target=self._streamers_worker, daemon=True).start()

    def _streamers_worker(self) -> None:
        """Worker thread — zevent.fr/api/ + gdoc."""
        logger.debug("Polling streamer data…")
        try:
            participations, stats, streamers = _run(_gather_zevent_gdoc())
            self._sig_streamers_ready.emit(participations, stats, streamers)
        except Exception as exc:
            logger.error("_poll_streamers: %s", exc)
            self._polling_streamers = False

    def _apply_streamers(self, participations: list[Participation], stats: GlobalStats,
                         streamers: list[StreamerInfo]) -> None:
        """Applique les résultats streamer sur le main thread Qt."""
        self._polling_streamers = False

        if participations:
            self._participations = participations
            self._gdoc_map = {p.twitch_login: p.streamer_id for p in participations}
            self._participation_map = {
                p.twitch_login: p.participation_id for p in participations if p.participation_id
            }

        # Hors-event / période inscription : zevent.fr/api/ ne renvoie pas de streamers.
        # Les participations evenmorestats fournissent déjà live / viewers / cagnotte.
        if not streamers and stats.website_mode != "live" and self._participations:
            streamers = [
                StreamerInfo(
                    twitch_login=p.twitch_login,
                    display=p.display or p.twitch_login,
                    online=p.live,
                    game=p.game,
                    location=p.location,
                    viewers=p.viewers,
                    donation=p.donation,
                    donation_formatted=_format_euros(p.donation),
                    profile_url=p.profile_url,
                    gdoc_id=p.streamer_id,
                    participation_id=p.participation_id,
                )
                for p in self._participations
            ]
            logger.info(
                "Hors-event : %d streamers chargés depuis les participations (fallback)",
                len(streamers),
            )

        _apply_participations_to_streamers(streamers, stats, self._participations)

        # Enrichir les StreamerInfo avec les ids evenmorestats
        for s in streamers:
            key = s.twitch_login.lower()
            s.gdoc_id = self._gdoc_map.get(key)
            s.participation_id = self._participation_map.get(key)

        self._streamers = streamers
        self._stats = stats
        self._uuid_to_name = {p.streamer_id: p.display for p in self._participations}
        self._uuid_to_name.update({s.gdoc_id: s.display for s in streamers if s.gdoc_id})

        self.streamers_updated.emit(self._streamers)
        self.global_stats_updated.emit(self._stats)
        self._history.add_point(self._stats.donation_total, self._stats.viewers_total)
        self.history_updated.emit(self._history)
        source = "ZEvent" if stats.website_mode == "live" else "evenmorestats"
        logger.info(
            "Streamers: %d total, %d live (%s) | Cagnotte: %s | Mode: %s",
            len(streamers),
            sum(1 for s in streamers if s.online),
            source,
            stats.donation_formatted,
            stats.website_mode,
        )
        # Pré-télécharge les avatars en background (sans bloquer l'UI)
        self._prefetch_avatars(streamers)
        # Premier chargement : lancer le prefetch goals maintenant que les streamers sont dispo
        if not self._goals_cache:
            self._start_goals_prefetch()

    def _prefetch_avatars(self, streamers: list[StreamerInfo]) -> None:
        """Pré-télécharge tous les avatars ZEvent profileUrl en background."""
        to_fetch = [
            (s.twitch_login, s.profile_url)
            for s in streamers
            if s.profile_url and s.twitch_login
        ]
        if not to_fetch:
            return
        threading.Thread(
            target=self._avatars_prefetch_worker,
            args=(to_fetch,),
            daemon=True,
        ).start()

    def _avatars_prefetch_worker(self, entries: list[tuple[str, str]]) -> None:
        """Télécharge les avatars manquants en parallèle (10 workers max)."""
        cache_dir = pathlib.Path.home() / ".zlink" / "avatars"
        cache_dir.mkdir(parents=True, exist_ok=True)

        missing = [
            (login, url)
            for login, url in entries
            if not (cache_dir / f"{login}.png").exists()
        ]
        if not missing:
            return

        logger.debug("Avatar prefetch : %d à télécharger", len(missing))

        def _one(item: tuple[str, str]) -> None:
            login, url = item
            dest = cache_dir / f"{login}.png"
            # Le login vient d'une API tierce : vérifier qu'il n'a pas fait sortir
            # du cache (pathlib ne normalise pas ".."), et que l'URL est https
            # (urlopen accepte sinon file:// et ftp://).
            if not dest.resolve().parent == cache_dir.resolve():
                logger.error("Avatar %r: chemin hors du cache, ignoré", login[:40])
                return
            if not url.lower().startswith("https://"):
                logger.error("Avatar %s: URL non https, ignorée", login)
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ZLink/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = resp.read(_AVATAR_MAX_BYTES + 1)
                if len(payload) > _AVATAR_MAX_BYTES:
                    logger.error("Avatar %s: réponse > %d octets, ignorée",
                                 login, _AVATAR_MAX_BYTES)
                    return
                dest.write_bytes(payload)
            except Exception as exc:
                logger.debug("Avatar prefetch %s: %s", login, exc)

        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(_one, missing)

        logger.info("Avatar prefetch terminé : %d photos téléchargées", len(missing))

    def _poll_events(self) -> None:
        """Déclenche le poll events en background."""
        if self._polling_events:
            logger.debug("_poll_events: déjà en cours, ignoré")
            return
        self._polling_events = True
        threading.Thread(target=self._events_worker, daemon=True).start()

    def _events_worker(self) -> None:
        """Worker thread — programme ZEvent."""
        logger.debug("Polling events…")
        try:
            results: list[list[EventItem]] = _run(_gather_events())
            self._sig_events_ready.emit(results)
        except Exception as exc:
            logger.error("_poll_events: %s", exc)
            self._polling_events = False

    def _apply_events(self, results: list) -> None:
        """Applique les résultats events sur le main thread Qt."""
        self._polling_events = False
        all_events: list[EventItem] = []
        for day, day_events in zip(_EVENT_DAYS, results):
            if isinstance(day_events, Exception):
                logger.error("_poll_events(%s): %s", day, day_events)
                continue
            self._events[day] = day_events
            all_events.extend(day_events)

        if all_events:
            self.events_updated.emit(all_events)
            logger.info(
                "Events: %d au total sur %d jours",
                len(all_events), len(_EVENT_DAYS),
            )

    # -- goals ----------------------------------------------------------------

    def _get_near_completion_goals(self) -> list[GoalWithStreamer]:
        """Goals entre 90% et 100% de complétion (proxy : streamer.donation / goal.amount).

        On exclut les goals déjà dépassés (pct > 100) : le proxy serait faussé
        pour les gros streamers dont la cagnotte totale dépasse largement les petits objectifs.
        """
        results: list[GoalWithStreamer] = []
        streamer_map = {s.twitch_login: s for s in self._streamers}
        for login, goals in self._goals_cache.items():
            s = streamer_map.get(login)
            if s is None:
                continue
            for g in goals:
                if g.accomplished or g.amount <= 0:
                    continue
                raw_pct = s.donation / g.amount * 100.0
                # 90 ≤ pct ≤ 100 : goal proche mais pas encore dépassé
                if raw_pct < 90.0 or raw_pct > 100.0:
                    continue
                results.append(GoalWithStreamer(
                    streamer_login=login,
                    streamer_display=s.display,
                    goal_name=g.name,
                    amount_target=g.amount,
                    accomplished=False,
                    pct=raw_pct,
                ))
        results.sort(key=lambda g: -g.pct)
        return results

    def _start_goals_prefetch(self) -> None:
        """Lance le prefetch des goals en background (ne bloque pas l'UI)."""
        threading.Thread(target=self._goals_worker, daemon=True).start()

    def _goals_worker(self) -> None:
        """Worker thread — fetch les goals, émet goals_updated."""
        try:
            _run(self._prefetch_top_goals())
        except Exception as exc:
            logger.error("_goals_worker: %s", exc)
            return
        goals = self._get_near_completion_goals()
        self._sig_goals_ready.emit(goals)

    def _apply_goals(self, goals: list) -> None:
        """Applique les goals sur le main thread Qt."""
        self.goals_updated.emit(goals)
        self.goals_raw_updated.emit(dict(self._goals_cache))
        self._check_newly_accomplished()

    def _check_newly_accomplished(self) -> None:
        """Détecte les objectifs nouvellement accomplis et émet goal_accomplished."""
        current: set[tuple[str, str]] = {
            (login, g.name)
            for login, goals in self._goals_cache.items()
            for g in goals if g.accomplished
        }
        if self._goals_init_done:
            for login, name in current - self._accomplished_goals:
                self.goal_accomplished.emit(login, name)
        self._accomplished_goals = current
        self._goals_init_done = True

    async def _prefetch_top_goals(self, n: int = 20) -> None:
        """Fetch les goals des top N streamers par cagnotte, en parallèle."""
        import asyncio as _asyncio
        top = sorted(self._streamers, key=lambda s: -s.donation)[:n]
        entries = [
            (s.twitch_login,
             s.participation_id or self._participation_map.get(s.twitch_login.lower()))
            for s in top
        ]
        entries = [(login, pid) for login, pid in entries if pid]

        async def _one(login: str, participation_id: str) -> None:
            try:
                goals = await fetch_donation_goals(participation_id)
                self._goals_cache[login] = goals
            except Exception as exc:
                logger.debug("Goals %s: %s", login, exc)

        await _asyncio.gather(*[_one(login, pid) for login, pid in entries])
        logger.info(
            "Goals pré-chargés pour %d streamers (%d proches de complétion)",
            len(self._goals_cache),
            len(self._get_near_completion_goals()),
        )
