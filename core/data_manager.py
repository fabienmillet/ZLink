# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""DataManager — polling QTimer-based des APIs ZEvent."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
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
            # Le client HTTP partage par cette boucle doit etre ferme AVANT
            # elle : ses sockets lui survivraient sinon.
            loop.run_until_complete(_close_loop_client())
        except Exception:
            logger.debug("Nettoyage de boucle incomplet", exc_info=True)
        finally:
            loop.close()

from core.api_client import close_loop_client as _close_loop_client
from core.api_client import (
    DonationGoal,
    EventItem,
    GlobalStats,
    GoalWithStreamer,
    Participation,
    StreamerInfo,
    _client,
    _format_euros,
    fetch_donation_goals,
    fetch_events,
    fetch_participations,
    fetch_zevent_data,
)
from core.cagnotte_marentdev import RelaisCagnotte
from core.cagnotte_socket import FluxCagnotte
from core.history_store import HistoryStore

from core import alerts as _alerts
from core import avatar_cache

logger = logging.getLogger(__name__)

# ZEvent 2026 : 3 sept. 18:00 UTC → 7 sept. 00:00 UTC (schedule de l'API events)
# L'édition 2026 court du jeudi 3 au lundi 7 septembre. Il en manquait un :
# le 7 n'était jamais interrogé, son programme n'existait donc pas pour l'app.
_EVENT_DAYS = ["2026-09-03", "2026-09-04", "2026-09-05",
               "2026-09-06", "2026-09-07"]

# Alertes de dons. Le seuil porte sur l'écart entre DEUX SONDAGES, pas sur un
# don unique : l'API ne publie qu'un cumul par streamer.
_DONATION_ALERT_EUR: float = 1000.0
# Un « bombardement » — le streamer demande à son chat d'envoyer des pièces, et
# des milliers d'euros arrivent en dons de 1 ou 2 € — se reconnaît à sa DURÉE :
# le seuil est franchi plusieurs relevés d'affilée, là où un don unique fait un
# pic puis retombe. C'est la seule distinction possible depuis un cumul.
_DONATION_FLOOD_POLLS: int = 3

# Objectif « imminent » : à portée de quelques dons. Les deux critères sont
# alternatifs — 500 € restants sur un objectif de 50 000 € ne fait que 1 %, et
# 98 % d'un petit objectif ne fait que quelques dizaines d'euros.
_GOAL_IMMINENT_EUR: float = 500.0
_GOAL_IMMINENT_PCT: float = 98.0

# Entrée dans les premières audiences du ZEvent. Trois places : au-delà, le
# classement bouge trop pour que l'entrée signifie quoi que ce soit.
_TOP_ENTRY_N: int = 3
_TOP_ENTRY_COOLDOWN_S: float = 900.0
_DONATION_ALERT_COOLDOWN_S: float = 300.0   # une chaîne ne monopolise pas
_DONATION_ALERTS_PER_HOUR: int = 12

_STREAMER_POLL_MS = 30_000    # 30 s — zevent.fr/api/ + participations
_EVENTS_POLL_MS  = 600_000   # 10 min
_GOALS_POLL_MS   = 300_000   # 5 min


# ---------------------------------------------------------------------------
# Async helpers (run several coroutines in one asyncio.run() call)
# ---------------------------------------------------------------------------

#: Un seul relais pour toute l'application : il porte l'ETag du dernier rapport,
#: et c'est ce qui permet au serveur de nous répondre 304 plutôt que de renvoyer
#: 180 ko toutes les trente secondes.
_RELAIS_CAGNOTTE = RelaisCagnotte()


async def _gather_zevent_gdoc() -> tuple[list[Participation], GlobalStats, list[StreamerInfo]]:
    """Appels zevent.fr/api/, participations evenmorestats et relais, en parallèle."""
    participations, (stats, streamers), releve = await asyncio.gather(
        fetch_participations(),
        fetch_zevent_data(),
        _RELAIS_CAGNOTTE.relever(_client()),
    )
    _appliquer_relais(stats, releve)          # type: ignore[arg-type]
    return participations, stats, streamers   # type: ignore[return-value]


def _hausser_la_cagnotte(stats: GlobalStats, total: float | None) -> bool:
    """Porte la cagnotte de `stats` à `total`, si celui-ci est meilleur.

    La règle unique des TROIS sources — zevent.fr, le relais HTTP, le flux
    temps réel. Elles ne comptent pas au même rythme et l'écart atteint
    quelques dizaines de milliers d'euros, sans qu'aucune ait tort : on garde
    la mieux renseignée, jamais la plus basse.

    Une cagnotte qui recule à l'écran est toujours une erreur de lecture,
    jamais un fait.
    """
    try:
        valeur = float(total)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if valeur <= stats.donation_total:
        return False
    stats.donation_total = valeur
    stats.donation_formatted = _format_euros(valeur)
    return True


def _appliquer_relais(stats: GlobalStats, releve) -> None:
    """Applique le relevé du relais HTTP à la cagnotte du poll."""
    _hausser_la_cagnotte(stats, releve.total if releve is not None else None)


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
        if part is not None:
            _completer_streamer(s, part, live_mode)

    if not live_mode:
        stats.viewers_total = sum(p.viewers for p in participations if p.live)


def _completer_streamer(s: StreamerInfo, part: Participation,
                        live_mode: bool) -> None:
    """Comble les champs vides d'un StreamerInfo depuis sa participation.

    On ne remplit que ce qui manque : en mode live, les données ZEvent font foi
    et ne doivent jamais être écrasées. Hors event elles n'existent pas, et
    l'API communautaire fournit alors aussi le direct, l'audience et le jeu.
    """
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


async def _gather_events() -> list[list[EventItem]]:
    return await asyncio.gather(*[fetch_events(d) for d in _EVENT_DAYS])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DataManager
# ---------------------------------------------------------------------------

class DataManager(QObject):
    """Orchestre les appels API et propage les données via Qt signals."""

    streamers_updated      = pyqtSignal(list)    # list[StreamerInfo]
    global_stats_updated   = pyqtSignal(object)  # GlobalStats
    don_recu               = pyqtSignal(object)  # un don qui vient d'arriver
    historique_dons        = pyqtSignal(object)  # lot des derniers dons passés
    flux_dons_ouvert       = pyqtSignal(bool)    # état du flux temps réel
    events_updated         = pyqtSignal(list)    # list[EventItem] (tous les jours)
    history_updated        = pyqtSignal(object)  # HistoryStore
    goals_updated          = pyqtSignal(list)    # list[GoalWithStreamer]
    goals_raw_updated      = pyqtSignal(dict)    # dict[login, list[DonationGoal]] — cache brut
    goal_accomplished      = pyqtSignal(str, str) # (login, goal_name) — nouvel objectif accompli
    favorite_live          = pyqtSignal(str, str) # (login, display) — un favori vient de passer en direct
    programme_added        = pyqtSignal(str, str) # (nom, quand) — nouveau show au programme
    milestone_reached      = pyqtSignal(float, str) # (montant, libellé) — palier de cagnotte franchi
    goal_imminent          = pyqtSignal(str, str, str, float, str) # (login, display, objectif, reste €, url de don)
    top_stream_entered     = pyqtSignal(str, str, int, int) # (login, display, viewers, rang) — entrée dans le top
    #: (login, display, montant, nature, donateur). `nature` vaut "don" ou
    #: "bombardement" ; `donateur` n'est renseigné QUE par le flux temps réel,
    #: seul à connaître qui a donné — le sondage, lui, ne voit qu'un cumul.
    #: Ajouté EN FIN de signature : PyQt tronque les arguments qu'un slot
    #: n'accepte pas, les branchements à quatre paramètres restent valides.
    big_donation           = pyqtSignal(str, str, float, str, str)

    # signaux internes pour le cross-thread (worker → main thread)
    _sig_streamers_ready   = pyqtSignal(object, object, list)  # participations, stats, streamers
    _sig_events_ready      = pyqtSignal(list)                  # list[list[EventItem]]
    _sig_goals_ready       = pyqtSignal(list)                  # list[GoalWithStreamer]
    #: Les durées de direct viennent d'être relevées — de quoi redessiner.
    durees_updated         = pyqtSignal()

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
        self._polling_durees: bool = False
        self._polling_events: bool = False


        # Cagnotte poussée en direct. Optionnelle : sans PyQt6-WebEngine elle
        # reste inerte, et le relais HTTP de `_gather_zevent_gdoc` suffit.
        self._flux = FluxCagnotte(self)
        self._flux.cagnotte_changee.connect(self._sur_cagnotte_temps_reel)
        # Relayés tels quels : le panel écoute le concentrateur de données,
        # il n'a pas à connaître le moteur web qui les apporte.
        self._flux.don_recu.connect(self.don_recu)
        self._flux.don_recu.connect(self._sur_don_temps_reel)
        self._flux.historique_recu.connect(self.historique_dons)
        self._flux.etat_change.connect(self.flux_dons_ouvert)

        self._timer_streamers = QTimer(self)
        self._timer_streamers.setInterval(_STREAMER_POLL_MS)
        self._timer_streamers.timeout.connect(self._poll_streamers)

        self._timer_events = QTimer(self)
        self._timer_events.setInterval(_EVENTS_POLL_MS)
        self._timer_events.timeout.connect(self._poll_events)

        self._timer_goals = QTimer(self)
        self._timer_goals.setInterval(_GOALS_POLL_MS)
        self._goals_running = False
        # Suivi des transitions, pour ne notifier que ce qui CHANGE.
        self._online_logins: set[str] = set()
        self._live_init_done = False
        self._known_events: set[str] = set()
        self._events_init_done = False
        self._last_milestone: float | None = None
        self._prev_donations: dict[str, float] = {}
        self._donations_init_done = False
        # Clé (login, nature) : un pic et un bombardement sont deux événements.
        self._donation_alert_at: dict[tuple[str, str], float] = {}
        self._donation_streak: dict[str, int] = {}
        self._donation_run: dict[str, float] = {}
        self._imminent_announced: set[tuple[str, str]] = set()
        self._imminent_init_done = False
        self._prev_top: set[str] = set()
        self._top_init_done = False
        self._top_alert_at: dict[str, float] = {}
        self._donation_alert_times: list[float] = []
        # Lu au démarrage puis rafraîchi par reload_config : interroger le
        # disque à chaque sondage pour trois nombres serait absurde.
        from core import config_store as _cfg
        self._alert_cfg: dict = _cfg.load()
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
        self._flux.demarrer()

    def stop_polling(self) -> None:
        """Arrête tous les timers de polling (mode mock)."""
        self._timer_streamers.stop()
        self._timer_events.stop()
        self._timer_goals.stop()
        self._flux.arreter()

    def _sur_cagnotte_temps_reel(self, total: float) -> None:
        """Cagnotte poussée par le flux, entre deux polls.

        Ne touche QUE l'affichage et les paliers. L'historique reste alimenté
        par le poll de trente secondes : un point par don ferait quatre mille
        relevés en une soirée, et la courbe d'une édition de quatre jours n'en
        garde que 4320 en tout.

        Une cagnotte qui recule est toujours une erreur de lecture : on ne
        redescend pas. Le poll, lui, réaligne sur la source officielle.
        """
        if not _hausser_la_cagnotte(self._stats, total):
            return
        self._detect_milestone(total)
        self.global_stats_updated.emit(self._stats)

    def _history_worker(self) -> None:
        """Charge l'historique en arrière-plan puis émet le signal.

        Les éditions passées viennent du cache PAR ÉDITION, plus fin que le
        dépôt historique — 332 relevés de cagnotte contre 110 — et surtout
        adressable année par année : c'est ce qui permet d'en superposer
        plusieurs. En cas d'échec complet, on retombe sur le dépôt, qui publie
        la dernière en plus grossier : mieux vaut une comparaison approximative
        que pas de comparaison.
        """
        try:
            # L'édition EN COURS d'abord : sans elle, ZLink ne trace que ce
            # qu'il a relevé depuis son lancement — une minute de courbe sur un
            # graphe qui en annonce soixante-douze heures. La source la publie
            # depuis l'ouverture, au même format que les précédentes.
            from core.api_client import GDOC_EVENT_ID

            _run(self._history.charger_edition_en_cours(GDOC_EVENT_ID))
            if not _run(self._history.charger_editions()):
                _run(self._history.load_historical_2026())
        except Exception:
            logger.exception("_history_worker")
        self.history_updated.emit(self._history)

    # -- queries --------------------------------------------------------------

    def reload_config(self, config: dict) -> None:
        """Rechargement à chaud : seuls les seuils d'alerte sont concernés."""
        self._alert_cfg = dict(config or {})

    def participant_logins(self) -> set[str]:
        """Logins de TOUS les participants, en ligne ou non, en minuscules.

        Sert à distinguer un raid entre participants du ZEvent d'un raid venu
        de n'importe où — un ami, un petit streamer de passage.
        """
        return {s.twitch_login.lower() for s in self._streamers if s.twitch_login}

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

    def rafraichir_durees(self, logins) -> None:
        """Relève depuis quand ces chaînes sont en direct, en tâche de fond.

        Seules celles AFFICHÉES sont demandées : trois cents chaînes en direct
        feraient douze requêtes toutes les cinq minutes à une interface non
        documentée, pour des durées que personne ne regarde.
        """
        from core import live_uptime

        besoin = live_uptime.a_rafraichir(logins)
        if not besoin or self._polling_durees:
            return
        self._polling_durees = True
        threading.Thread(target=self._durees_worker, args=(besoin,),
                         daemon=True).start()

    def _durees_worker(self, logins: list[str]) -> None:
        """Worker thread — gql.twitch.tv."""
        from core import live_uptime

        try:
            _run(live_uptime.rafraichir(logins))
        except Exception:
            logger.exception("rafraichir_durees")
        finally:
            self._polling_durees = False
        self.durees_updated.emit()

    def _streamers_worker(self) -> None:
        """Worker thread — zevent.fr/api/ + gdoc."""
        logger.debug("Polling streamer data…")
        try:
            participations, stats, streamers = _run(_gather_zevent_gdoc())
            self._sig_streamers_ready.emit(participations, stats, streamers)
        except Exception:
            logger.exception("_poll_streamers")
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

        self._detect_favorites_live(streamers)
        self._detect_big_donations(streamers)
        self._detect_top_entry(streamers)

        self._streamers = streamers
        # Le poll reconstruit un GlobalStats complet et l'installait tel quel,
        # ÉCRASANT ce que le flux temps réel avait déjà poussé — plus frais de
        # trente secondes. Le compteur redescendait à chaque tour de poll, puis
        # remontait au don suivant : une dent de scie toutes les trente
        # secondes, et un point d'historique trop bas à chaque relevé.
        _hausser_la_cagnotte(stats, self._flux.total)
        self._stats = stats
        self._uuid_to_name = {p.streamer_id: p.display for p in self._participations}
        self._uuid_to_name.update({s.gdoc_id: s.display for s in streamers if s.gdoc_id})

        self.streamers_updated.emit(self._streamers)
        if _alerts.enabled("milestone"):
            self._detect_milestone(self._stats.donation_total)
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
        """Télécharge les avatars manquants en parallèle (10 workers max).

        Le téléchargement lui-même vit dans core.avatar_cache : la mosaïque et
        le panel réclament les mêmes images, et deux implémentations séparées
        tiraient la même URL deux fois.
        """
        cache_dir = avatar_cache.CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)

        missing = [
            (login, url)
            for login, url in entries
            if not avatar_cache.path_for(login).exists()
        ]
        if not missing:
            return

        logger.debug("Avatar prefetch : %d à télécharger", len(missing))

        # thread_name_prefix seulement pour le diagnostic ; l'essentiel est que
        # ce pool soit fermé : ses threads sont NON-DAEMON et Python les joint à
        # la sortie via un hook atexit. Un téléchargement lent bloquait donc
        # l'arrêt de l'application pendant tout son timeout.
        with ThreadPoolExecutor(max_workers=10, thread_name_prefix="avatars") as pool:
            done = sum(pool.map(lambda it: avatar_cache.download(*it), missing))

        logger.info("Avatar prefetch terminé : %d/%d photos disponibles",
                    done, len(missing))

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
        except Exception:
            logger.exception("_poll_events")
            self._polling_events = False

    def _detect_top_entry(self, streamers: list[StreamerInfo]) -> None:
        """Signale l'entrée d'une chaîne dans les toutes premières audiences.

        Surveiller la progression des trois cents participants produirait un
        flux continu — tout le monde monte et descend en permanence. Entrer
        dans le TOP 3, en revanche, est rare et veut dire quelque chose : un
        show vient de commencer, un raid a atterri, il se passe quelque chose
        d'assez gros pour déplacer l'audience du ZEvent.

        Le filtre « pas déjà à l'écran » n'est pas appliqué ici : le gestionnaire
        de données ignore ce que la grille affiche. C'est l'appelant qui écarte
        les chaînes déjà visibles — une alerte pour ce qu'on regarde déjà
        n'apprendrait rien.
        """
        if not _alerts.enabled("top_entry"):
            return
        live = sorted(
            [s for s in streamers if s.online and s.twitch_login and s.viewers > 0],
            key=lambda s: -s.viewers,
        )[:_TOP_ENTRY_N]
        actuels = [s.twitch_login for s in live]
        now = time.monotonic()
        if self._top_init_done:
            for rang, s in enumerate(live, 1):
                if s.twitch_login in self._prev_top:
                    continue
                # Une chaîne qui oscille autour de la troisième place ne doit
                # pas se signaler à chaque sondage.
                # -inf et non 0.0 : time.monotonic() compte depuis le
                # demarrage de la MACHINE, donc 0.0 signifie « il y a peu »
                # pendant les premieres secondes d'uptime — et etouffait la
                # toute premiere alerte.
                if now - self._top_alert_at.get(
                        s.twitch_login, float("-inf")) < _TOP_ENTRY_COOLDOWN_S:
                    continue
                self._top_alert_at[s.twitch_login] = now
                logger.info("Entrée dans le top %d : %s (%d viewers, rang %d)",
                            _TOP_ENTRY_N, s.twitch_login, s.viewers, rang)
                self.top_stream_entered.emit(
                    s.twitch_login, s.display or s.twitch_login, s.viewers, rang)
        self._prev_top = set(actuels)
        self._top_init_done = True

    def _detect_big_donations(self, streamers: list[StreamerInfo]) -> None:
        """Signale une montée notable de la cagnotte d'une chaîne.

        Ce qu'on mesure est l'écart entre deux sondages, pas un don unique :
        l'API ne donne qu'un cumul par streamer, et trente secondes peuvent
        agréger un gros don ou vingt petits. Le message dit donc « vient de
        recevoir », ce qui reste vrai dans les deux cas.

        Trois garde-fous, pour la même raison que partout ailleurs : le premier
        relevé ne déclenche rien, une chaîne ne peut pas monopoliser les
        alertes, et le nombre d'alertes par heure est plafonné — un ZEvent
        distribue des dons en continu sur trois cents chaînes.
        """
        if not _alerts.enabled("donation"):
            return
        # Le flux temps réel annonce le don EXACT, avec son donateur. Tant
        # qu'il tient, cette détection-ci ferait doublon — et en moins bien :
        # elle ne mesure qu'un écart de cumul sur trente secondes, où un gros
        # don et vingt petits se ressemblent. Elle reprend la main dès que le
        # flux tombe, ce qui est exactement son rôle.
        if self._flux.ouvert:
            self._donations_init_done = True
            return
        hw = self._donation_alert_config()
        seuil = float(hw.get("threshold", _DONATION_ALERT_EUR))
        cooldown = float(hw.get("cooldown_s", _DONATION_ALERT_COOLDOWN_S))
        par_heure = max(1, int(hw.get("per_hour", _DONATION_ALERTS_PER_HOUR)))
        now = time.monotonic()

        candidats: list[tuple[float, StreamerInfo, str]] = []
        for s in streamers:
            entree = self._candidat_don(s, seuil, cooldown, now)
            if entree is not None:
                candidats.append(entree)

        self._donations_init_done = True
        if candidats:
            self._emettre_alertes_dons(candidats, par_heure, now)

    def _candidat_don(self, s: StreamerInfo, seuil: float, cooldown: float,
                      now: float) -> "tuple[float, StreamerInfo, str] | None":
        """(montant, streamer, nature) si cette chaîne mérite une alerte.

        Renvoie None dans tous les autres cas : pas d'identifiant, cagnotte
        illisible, premier relevé, hausse sous le seuil, ou cooldown en cours.
        """
        login = s.twitch_login
        if not login:
            return None
        try:
            courant = float(s.donation or 0.0)
        except (TypeError, ValueError):
            return None
        avant = self._prev_donations.get(login)
        self._prev_donations[login] = courant
        if avant is None or not self._donations_init_done:
            return None
        delta = courant - avant
        if delta < seuil:
            # La série s'interrompt : ce qui suivra sera un nouvel épisode.
            self._donation_streak.pop(login, None)
            self._donation_run.pop(login, None)
            return None
        serie = self._donation_streak.get(login, 0) + 1
        self._donation_streak[login] = serie
        cumul = self._donation_run.get(login, 0.0) + delta
        self._donation_run[login] = cumul

        nature = "bombardement" if serie >= _DONATION_FLOOD_POLLS else "don"
        # Le cooldown vaut par NATURE : un bombardement qui s'installe après
        # un premier pic mérite d'être annoncé, c'est un autre événement.
        # -inf et non 0.0 : voir la note de _detect_top_entry. Un defaut a
        # zero etouffait la premiere alerte de dons apres un demarrage.
        if now - self._donation_alert_at.get(
                (login, nature), float("-inf")) < cooldown:
            return None
        return (cumul if nature == "bombardement" else delta, s, nature)

    def _emettre_alertes_dons(self, candidats: list, par_heure: int,
                              now: float) -> None:
        """Émet les alertes retenues, dans la limite du plafond horaire.

        Comme pour HypeWatcher : sous une pluie de dons, on garde les plus gros
        plutôt que d'inonder l'écran.
        """
        self._donation_alert_times = [
            t for t in self._donation_alert_times if now - t < 3600.0]
        place = max(0, par_heure - len(self._donation_alert_times))
        if place == 0:
            logger.debug("Alertes de dons : plafond horaire atteint, %d ignorée(s)",
                         len(candidats))
            return
        candidats.sort(key=lambda c: -c[0])
        for montant, s, nature in candidats[:place]:
            self._donation_alert_at[(s.twitch_login, nature)] = now
            self._donation_alert_times.append(now)
            logger.info("Dons — %s sur %s : +%.0f €",
                        nature, s.twitch_login, montant)
            self.big_donation.emit(
                s.twitch_login, s.display or s.twitch_login, montant, nature, "")

    def _sur_don_temps_reel(self, don: object) -> None:
        """Un don EXACT, poussé par le flux : on sait qui, combien, et pour qui.

        C'est ce que le sondage ne pouvait pas dire. Il ne voyait qu'un cumul
        par chaîne entre deux relevés, d'où son « vient de recevoir » : trente
        secondes agrègent aussi bien un don de cinq cents euros que vingt de
        vingt-cinq.

        Le plafond horaire est PARTAGÉ avec la détection par sondage : c'est le
        même quota d'alertes, quelle que soit la porte par laquelle elles
        entrent, et les deux ne tournent jamais en même temps de toute façon.
        """
        if not isinstance(don, dict) or not _alerts.enabled("donation"):
            return
        try:
            montant = float(don.get("amount") or 0.0)
        except (TypeError, ValueError):
            return
        reglages = self._donation_alert_config()
        if montant < float(reglages.get("threshold", _DONATION_ALERT_EUR)):
            return

        now = time.monotonic()
        par_heure = max(1, int(reglages.get("per_hour", _DONATION_ALERTS_PER_HOUR)))
        self._donation_alert_times = [
            t for t in self._donation_alert_times if now - t < 3600.0]
        if len(self._donation_alert_times) >= par_heure:
            logger.debug("Don de %.0f € non annoncé : plafond horaire atteint",
                         montant)
            return
        self._donation_alert_times.append(now)

        login, display = self._identifier_la_chaine(str(don.get("streamer") or ""))
        donateur = str(don.get("donor") or "").strip()
        logger.info("Don — %s : %.0f € pour %s", donateur or "anonyme",
                    montant, display or "?")
        self.big_donation.emit(login, display, montant, "don", donateur)

    def _identifier_la_chaine(self, annonce: str) -> tuple[str, str]:
        """(login Twitch, nom affiché) pour la chaîne nommée par le flux.

        Le flux annonce un nom d'affichage — « Joueur_du_Grenier » — là où le
        reste de ZLink travaille sur des logins en minuscules. Sans cette
        traduction, la cellule à faire clignoter ne serait jamais retrouvée.

        Rend le nom annoncé tel quel si la chaîne est inconnue : mieux vaut
        une alerte qu'on ne peut pas rattacher qu'une alerte perdue.
        """
        vise = annonce.strip()
        if not vise:
            return "", ""
        bas = vise.lower()
        for s in self._streamers:
            if s.twitch_login.lower() == bas or (s.display or "").lower() == bas:
                return s.twitch_login, s.display or s.twitch_login
        return bas, vise

    def _donation_alert_config(self) -> dict:
        """Réglages des alertes de dons, tenus à jour par reload_config."""
        raw = self._alert_cfg.get("donations")
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _milestone_step(total: float) -> float:
        """Pas entre deux paliers, resserré en début d'édition.

        Un pas fixe conviendrait mal aux deux bouts : à 250 000 € le premier
        million produirait quarante annonces, et à un million les premières
        heures n'en produiraient aucune. Le pas suit donc l'ordre de grandeur.
        """
        if total < 1_000_000:
            return 250_000.0
        if total < 5_000_000:
            return 500_000.0
        return 1_000_000.0

    @staticmethod
    def _fmt_milestone(amount: float) -> str:
        """1000000 → « 1 M€ », 1500000 → « 1,5 M€ », 500000 → « 500 k€ »."""
        if amount >= 1_000_000:
            millions = amount / 1_000_000
            texte = f"{millions:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{texte} M€"
        return f"{amount / 1000:.0f} k€"

    def _detect_milestone(self, total: float) -> None:
        """Signale le franchissement d'un palier rond de cagnotte.

        Le premier relevé ne déclenche rien : au lancement, la cagnotte a déjà
        franchi tous les paliers de la journée, et les annoncer d'un coup
        n'apprendrait rien. On ne signale que ce qui se produit sous les yeux.
        """
        try:
            total = float(total or 0.0)
        except (TypeError, ValueError):
            return
        if total <= 0:
            return
        step = self._milestone_step(total)
        current = int(total // step) * step
        if self._last_milestone is not None and current > self._last_milestone:
            # Un sondage peut sauter plusieurs paliers d'un coup : on n'annonce
            # que le plus haut, sinon trois messages se chassent l'un l'autre.
            logger.info("Palier de cagnotte franchi : %s", self._fmt_milestone(current))
            self.milestone_reached.emit(current, self._fmt_milestone(current))
        if self._last_milestone is None or current > self._last_milestone:
            self._last_milestone = current

    def _detect_favorites_live(self, streamers: list[StreamerInfo]) -> None:
        """Signale les favoris qui viennent de passer en direct.

        Le premier sondage est ignoré : sans ce garde-fou, tous les favoris déjà
        en ligne au lancement produiraient une notification, alors qu'il ne
        s'est rien passé.
        """
        if not _alerts.enabled("favorite_live"):
            return
        online = {s.twitch_login for s in streamers if s.online and s.twitch_login}
        if self._live_init_done:
            from core import favorites
            favs = favorites.get()
            if favs:
                for s in streamers:
                    if (s.online and s.twitch_login
                            and s.twitch_login not in self._online_logins
                            and s.twitch_login.lower() in favs):
                        logger.info("Favori en direct : %s", s.twitch_login)
                        self.favorite_live.emit(
                            s.twitch_login, s.display or s.twitch_login)
        self._online_logins = online
        self._live_init_done = True

    @staticmethod
    def _event_key(ev: EventItem) -> str:
        """Identité stable d'un show, même sans id fourni par l'API."""
        return str(getattr(ev, "id", "") or
                   f"{ev.day}_{ev.start_local}_{ev.name}")

    def _detect_new_events(self, all_events: list) -> None:
        """Signale les shows ajoutés au programme depuis le sondage précédent."""
        keys = {self._event_key(ev) for ev in all_events}
        if self._events_init_done:
            for ev in all_events:
                if self._event_key(ev) not in self._known_events:
                    when = " ".join(x for x in (ev.day, ev.start_local) if x)
                    logger.info("Nouveau au programme : %s (%s)", ev.name, when)
                    self.programme_added.emit(ev.name or "Événement", when)
        self._known_events = keys
        self._events_init_done = True

    def _apply_events(self, results: list) -> None:
        """Applique les résultats events sur le main thread Qt."""
        self._polling_events = False
        # Dédoublonné par identité : un show qui déborde sur le lendemain est
        # rendu par l'API dans les DEUX journées interrogées. Tant que son
        # jour était celui qu'on demandait, les deux copies se distinguaient
        # — mal, mais elles se distinguaient. Maintenant qu'il porte son vrai
        # jour de début, ce sont deux fois la même ligne.
        par_identite: dict[str, EventItem] = {}
        for jour_demande, day_events in zip(_EVENT_DAYS, results):
            if isinstance(day_events, Exception):
                logger.error("_poll_events(%s): %s", jour_demande, day_events)
                continue
            for ev in day_events:
                par_identite.setdefault(self._event_key(ev), ev)

        all_events = list(par_identite.values())
        # Regroupés sur le jour où ils COMMENCENT, et non celui qui les a
        # ramenés : c'est ce que `get_events_for_day` est censé rendre.
        self._events = {}
        for ev in all_events:
            self._events.setdefault(ev.day, []).append(ev)

        if all_events:
            self._detect_new_events(all_events)
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
        """Lance le prefetch des goals en background (ne bloque pas l'UI).

        Garde de recouvrement : ce prefetch interroge l'API une fois par
        streamer suivi. Sur une connexion lente il dépasse sa période, et sans
        garde le timer empilait des passes concurrentes qui multipliaient les
        requêtes tout en se disputant le même cache.
        """
        if self._goals_running:
            logger.debug("_start_goals_prefetch : passe précédente en cours, ignorée")
            return
        self._goals_running = True
        threading.Thread(target=self._goals_worker, daemon=True).start()

    def _goals_worker(self) -> None:
        """Worker thread — fetch les goals, émet goals_updated."""
        try:
            _run(self._prefetch_top_goals())
        except Exception:
            logger.exception("_goals_worker")
            return
        finally:
            # Dans le finally : une exception laissait le drapeau levé pour
            # toujours, et plus aucun objectif n'était rafraîchi ensuite.
            self._goals_running = False
        goals = self._get_near_completion_goals()
        self._sig_goals_ready.emit(goals)

    def _apply_goals(self, goals: list) -> None:
        """Applique les goals sur le main thread Qt."""
        self.goals_updated.emit(goals)
        self.goals_raw_updated.emit(dict(self._goals_cache))
        self._check_newly_accomplished()
        self._check_imminent_goals()

    def _check_imminent_goals(self) -> None:
        """Signale les objectifs sur le point de tomber.

        Le bandeau dit déjà « X est à 92 % de son objectif », mais 92 % peut
        rester 92 % pendant deux heures. Ce qui mérite qu'on bascule, c'est le
        dernier palier : quelques centaines d'euros, quelques minutes.

        On repart des objectifs ENRICHIS : _goals_cache ne contient que des
        DonationGoal bruts, sans pourcentage — celui-ci se calcule à partir de
        la cagnotte du streamer, ce que fait déjà _get_near_completion_goals.

        Chaque objectif n'est annoncé qu'une fois : sinon il reviendrait à
        chaque sondage tant qu'il n'est pas atteint.
        """
        if not _alerts.enabled("goal_imminent"):
            return
        for g in self._get_near_completion_goals():
            self._signaler_si_imminent(g)
        self._imminent_init_done = True

    def _signaler_si_imminent(self, g) -> None:
        """Annonce un objectif s'il est vraiment sur le point de tomber.

        Chaque objectif n'est annoncé qu'une fois, et le premier passage reste
        muet : au lancement, plusieurs objectifs sont déjà tout près du but.
        """
        reste = g.amount_target * max(0.0, 100.0 - g.pct) / 100.0
        if reste > _GOAL_IMMINENT_EUR and g.pct < _GOAL_IMMINENT_PCT:
            return
        cle = (g.streamer_login, g.goal_name)
        if cle in self._imminent_announced:
            return
        self._imminent_announced.add(cle)
        if not self._imminent_init_done:
            return
        # La restriction aux favoris se contrôle ici, pas à l'affichage : une
        # alerte écartée plus tard resterait dans le fil et dans le journal.
        # On la marque quand même comme annoncée juste au-dessus, sinon elle
        # reviendrait à chaque sondage le jour où la restriction est levée.
        if not _alerts.enabled_pour("goal_imminent", g.streamer_login):
            return
        logger.info("Objectif imminent : %s — %s (reste %.0f €)",
                    g.streamer_login, g.goal_name, reste)
        self.goal_imminent.emit(
            g.streamer_login, g.streamer_display or g.streamer_login,
            g.goal_name, reste, self._url_de_don(g.streamer_login))

    def _url_de_don(self, login: str) -> str:
        """URL de don d'une chaîne, ou "" si elle n'en publie pas.

        Elle voyage avec l'alerte : sans elle, proposer de donner obligerait le
        destinataire à retrouver le streamer lui-même.
        """
        for st in self._streamers:
            if st.twitch_login == login:
                return getattr(st, "donation_url", "") or ""
        return ""

    def _check_newly_accomplished(self) -> None:
        """Détecte les objectifs nouvellement accomplis et émet goal_accomplished."""
        current: set[tuple[str, str]] = {
            (login, g.name)
            for login, goals in self._goals_cache.items()
            for g in goals if g.accomplished
        }
        if self._goals_init_done:
            for login, name in current - self._accomplished_goals:
                if _alerts.enabled_pour("goal_done", login):
                    self.goal_accomplished.emit(login, name)
        self._accomplished_goals = current
        self._goals_init_done = True

    async def _prefetch_top_goals(self, n: int = 20) -> None:
        """Objectifs des N plus grosses cagnottes, PLUS tous les favoris.

        Les favoris s'ajoutent inconditionnellement au top : sans eux, un
        streamer suivi mais hors des vingt premières cagnottes n'avait jamais
        ses objectifs chargés. Son objectif accompli n'était donc jamais
        détecté, ni signalé dans le fil d'événements — précisément le cas où
        l'utilisateur attend une notification.
        """
        import asyncio as _asyncio
        from core import favorites
        # L'audience départage les cagnottes égales. Hors événement elles le
        # sont TOUTES — à zéro : le tri ne triait alors rien, et les vingt
        # premières lignes rendues par l'API partaient au chargement, sans
        # rapport avec qui est en direct. Les rares chaînes qui publient déjà
        # leurs objectifs n'étaient donc presque jamais du lot, et la colonne
        # Objectifs restait vide pour tout le monde. Pendant l'événement la
        # cagnotte reprend la main, comme avant.
        ranked = sorted(self._streamers, key=lambda s: (-s.donation, -s.viewers))
        top = ranked[:n]
        favs = favorites.get()
        if favs:
            already = {s.twitch_login for s in top}
            top = top + [
                s for s in ranked[n:]
                if s.twitch_login not in already and s.twitch_login.lower() in favs
            ]
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
