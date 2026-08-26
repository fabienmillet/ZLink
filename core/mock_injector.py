# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""MockInjector — simule des données live ZEvent pour tester l'UI hors-event.

Utilisation :
    python main.py --mock

Le mode mock :
- Démarre avec les vrais streamers ZEvent 2025 (depuis le panel.py)
- Force website_mode = "live" pour activer toutes les animations
- Toutes les 3 s  : ajoute une donation aléatoire sur un streamer
- Toutes les 15 s : varie les viewers de chaque streamer
- Toutes les 30 s : fait passer un streamer offline / online (simulation départs/retours)
- Toutes les 45 s : annonce un objectif imminent, avec son URL de don
- Toutes les 90 s : déclenche un objectif accompli (pour tester le flash GridWidget)
- Au démarrage : injecte des événements centrés sur l'heure actuelle (timeline)
  et trois donation goals par streamer
"""

from __future__ import annotations

import copy
import logging
# `random` sert ici a FABRIQUER DES DONNEES DE DEMONSTRATION : dons, viewers,
# passages en ligne. Aucune valeur n'est un secret ni ne protege quoi que ce
# soit, d'ou le generateur standard plutot que `secrets`. Les suppressions
# d'avertissement de ce fichier renvoient toutes a cette note.
import random  # NOSONAR
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QTimer

if TYPE_CHECKING:  # import differe : core.data_manager importe ce module
    from core.data_manager import DataManager

from core.api_client import (
    DonationGoal,
    EventItem,
    GlobalStats,
    GoalWithStreamer,
    StreamerInfo,
)

logger = logging.getLogger(__name__)

_JUST_CHATTING = "Just Chatting"

# Paliers de dons simules : ((min, max), poids). Un tres gros don reste rare.
_PALIERS_DON: tuple[tuple[tuple[int, int], int], ...] = (
    ((5, 50), 50),          # micro-don
    ((100, 500), 30),       # don moyen
    ((500, 2000), 15),      # gros don
    ((2000, 10000), 5),     # tres gros don
)


# ---------------------------------------------------------------------------
# Données de départ (copie des _TEST_STREAMERS de panel.py)
# ---------------------------------------------------------------------------

_MOCK_STREAMERS_SEED: list[tuple] = [
    # (login, display, game, location, viewers, donation)
    ("zerator",       "ZeratoR",        "Minecraft",         "LAN",    42_350, 694_000.0),
    ("domingo",       "Domingo",         _JUST_CHATTING,     "LAN",    38_200, 1_200_000.0),
    ("antoinedaniel", "Antoine Daniel",  "Gartic Phone",      "LAN",    22_100,   533_000.0),
    ("mistermv",      "MisterMV",        "Balatro",           "LAN",    18_700,   325_000.0),
    ("joyca",         "Joyca",           "GeoGuessr",         "LAN",    15_400,   278_000.0),
    ("squeezie",      "Squeezie",        "Fortnite",          "LAN",    54_800,   289_000.0),
    ("samueletienne", "Samuel Etienne",  _JUST_CHATTING,     "LAN",     8_400,   464_000.0),
    ("bagherajones",  "Baghera Jones",   "Minecraft",         "LAN",    11_800,   198_000.0),
    ("ponce",         "Ponce",           "Trackmania",        "LAN",    11_200,   187_000.0),
    ("etoiles",       "Etoiles",         "Genshin Impact",    "Online",  6_900,   143_000.0),
    ("moman",         "MoMaN",           _JUST_CHATTING,     "LAN",     7_600,   112_000.0),
    ("lapi",          "Lapi",            "Valorant",          "Online",  5_300,    89_000.0),
    ("avamind",       "Avamind",         _JUST_CHATTING,     "Online",  4_800,    76_000.0),
    ("mastu",         "Mastu",           "League of Legends", "LAN",     3_900,    65_000.0),
    ("helydia",       "Helydia",         "Art",               "Online",  3_400,    54_000.0),
    ("deujna",        "Deujna",          _JUST_CHATTING,     "LAN",     2_100,    45_000.0),
    ("chelxie",       "Chelxie",         "Art",               "Online",  1_800,    23_000.0),
]

# Objectifs fictifs pour le test du flash
_MOCK_GOALS = [
    ("zerator",       "J'enlève ma casquette en live"),
    ("domingo",       "Je mange un piment Habanero"),
    ("squeezie",      "Je fais un battle royale en pyjama"),
    ("antoinedaniel", "Lecture de fanfics en direct"),
    ("ponce",         "Course Trackmania les yeux fermés"),
    ("mistermv",      "Blindtest 200 musiques"),
    ("bagherajones",  "ASMR de 2 heures"),
]

# Intitulés piochés pour garnir l'onglet Goals : trois par streamer.
_MOCK_GOAL_NOMS = [
    "J'enlève ma casquette en live",
    "Je mange un piment Habanero",
    "Battle royale en pyjama",
    "Lecture de fanfics en direct",
    "Course Trackmania les yeux fermés",
    "Blindtest 200 musiques",
    "ASMR de 2 heures",
    "Je repeins le décor en rose",
    "Karaoké avec le chat",
    "Speedrun sans regarder l'écran",
    "Je cuisine en direct pour la LAN",
    "Marathon rétro toute la nuit",
]

# Paliers appliqués à la cagnotte de départ de chaque streamer :
# (objectif = cagnotte × ratio, déjà accompli ?).
#   0.55 → largement dépassé, marqué accompli
#   1.05 → environ 95 %, donc « imminent » au sens de DataManager (90 ≤ pct ≤ 100)
#   1.60 → environ 62 %, en cours
# Les montants sont figés au démarrage : les dons continuant d'arriver toutes
# les 3 s, les pourcentages montent tout seuls et finissent par franchir la
# barre — de quoi voir l'onglet vivre sans attendre un vrai ZEvent.
_MOCK_GOAL_PALIERS: tuple[tuple[float, bool], ...] = (
    (0.55, True),
    (1.05, False),
    (1.60, False),
)


class MockInjector(QObject):
    """Injecte des données simulées dans un DataManager existant.

    Connecte des timers Qt qui émettent les mêmes signaux que DataManager.
    À utiliser uniquement en développement (python main.py --mock).
    """

    def __init__(self, data_manager: "DataManager", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dm = data_manager
        self._streamers: list[StreamerInfo] = self._build_initial_streamers()
        self._stats = GlobalStats(
            donation_total=sum(s.donation for s in self._streamers),
            donation_formatted="",
            viewers_total=sum(s.viewers for s in self._streamers),
            website_mode="live",
        )
        self._stats.donation_formatted = self._fmt(self._stats.donation_total)
        self._goal_index = 0
        self._imminent_index = 0
        # Figé une fois pour toutes : voir _MOCK_GOAL_PALIERS.
        self._goals_cache = self._build_goals_cache()

        # Timer donations : toutes les 3 secondes
        self._t_donation = QTimer(self)
        self._t_donation.setInterval(3_000)
        self._t_donation.timeout.connect(self._tick_donation)

        # Timer viewers : toutes les 15 secondes
        self._t_viewers = QTimer(self)
        self._t_viewers.setInterval(15_000)
        self._t_viewers.timeout.connect(self._tick_viewers)

        # Timer online toggle : toutes les 30 secondes
        self._t_online = QTimer(self)
        self._t_online.setInterval(30_000)
        self._t_online.timeout.connect(self._tick_online)

        # Timer objectifs : toutes les 90 secondes
        self._t_goals = QTimer(self)
        self._t_goals.setInterval(90_000)
        self._t_goals.timeout.connect(self._tick_goal)

        # Timer objectifs imminents : toutes les 45 secondes. C'est cette alerte
        # qui porte l'URL de don, donc la seule qui ouvre le navigateur.
        self._t_imminent = QTimer(self)
        self._t_imminent.setInterval(45_000)
        self._t_imminent.timeout.connect(self._tick_imminent)

    # ── public ──────────────────────────────────────────────────────────────

    @property
    def streamers(self) -> list[StreamerInfo]:
        """Copie de la liste des streamers simulés."""
        return list(self._streamers)

    def start(self) -> None:
        """Lance l'injection et envoie un premier batch immédiat."""
        logger.warning(
            "⚠️  MODE MOCK ACTIF — données simulées. "
            "Ne pas utiliser en production."
        )
        # Événements centrés sur maintenant pour la timeline
        mock_events = self._build_mock_events()
        self._dm.events_updated.emit(mock_events)
        # Émission initiale — remplace ce que DataManager aurait émis
        self._emit_all()
        self._t_donation.start()
        self._t_viewers.start()
        self._t_online.start()
        self._t_goals.start()
        self._t_imminent.start()

    def stop(self) -> None:
        self._t_donation.stop()
        self._t_viewers.stop()
        self._t_online.stop()
        self._t_goals.stop()
        self._t_imminent.stop()

    # ── ticks ───────────────────────────────────────────────────────────────

    def _tick_donation(self) -> None:
        """Ajoute une donation aléatoire sur un streamer en live."""
        live = [s for s in self._streamers if s.online]
        if not live:
            return
        target = random.choice(live)  # NOSONAR
        # On tire le PALIER, puis un montant dedans. La version precedente
        # tirait les quatre montants pour n'en garder qu'un : trois tirages
        # jetes a chaque don, toutes les trois secondes.
        (bas, haut), = random.choices(  # NOSONAR
            [p for p, _ in _PALIERS_DON], weights=[w for _, w in _PALIERS_DON])
        amount = random.randint(bas, haut)  # NOSONAR

        target.donation += amount
        target.donation_formatted = self._fmt(target.donation)
        self._stats.donation_total = sum(s.donation for s in self._streamers)
        self._stats.donation_formatted = self._fmt(self._stats.donation_total)

        logger.debug("MOCK donation +%d€ → %s (total %.0f€)", amount, target.display, self._stats.donation_total)
        self._emit_all()

    def _tick_viewers(self) -> None:
        """Varie les viewers de chaque streamer (±15%)."""
        for s in self._streamers:
            if not s.online:
                continue
            delta = int(s.viewers * random.uniform(-0.15, 0.15))  # NOSONAR
            s.viewers = max(100, s.viewers + delta)
        self._stats.viewers_total = sum(s.viewers for s in self._streamers if s.online)
        self._emit_all()

    def _tick_online(self) -> None:
        """Toggle le statut d'un streamer aléatoire."""
        target = random.choice(self._streamers)  # NOSONAR
        target.online = not target.online
        if not target.online:
            target.viewers = 0
        else:
            # Reprend avec ~50% de ses viewers habituels
            seed_viewers = next(
                (s[4] for s in _MOCK_STREAMERS_SEED if s[0] == target.twitch_login), 5000
            )
            target.viewers = int(seed_viewers * random.uniform(0.3, 0.7))  # NOSONAR
        self._stats.viewers_total = sum(s.viewers for s in self._streamers if s.online)
        self._emit_all()

    def _tick_imminent(self) -> None:
        """Annonce un objectif proche de tomber, avec son URL de don.

        C'est l'alerte dont le bouton ouvre la page de don dans le navigateur :
        sans elle, ce chemin ne se teste qu'un vrai jour de ZEvent.
        """
        live = [s for s in self._streamers if s.online]
        if not live:
            return
        cible = live[self._imminent_index % len(live)]
        self._imminent_index += 1
        objectifs = self._goals_cache.get(cible.twitch_login, [])
        proches = [g for g in objectifs
                   if not g.accomplished and g.amount > cible.donation]
        if not proches:
            return
        objectif = min(proches, key=lambda g: g.amount)
        reste = objectif.amount - cible.donation
        logger.debug("MOCK goal_imminent: %s — %s (reste %.0f €)",
                     cible.display, objectif.name, reste)
        self._dm.goal_imminent.emit(
            cible.twitch_login, cible.display, objectif.name, reste,
            cible.donation_url,
        )

    def _tick_goal(self) -> None:
        """Déclenche un objectif accompli pour tester le flash."""
        if not _MOCK_GOALS:
            return
        login, goal_name = _MOCK_GOALS[self._goal_index % len(_MOCK_GOALS)]
        self._goal_index += 1
        logger.debug("MOCK goal_accomplished: %s — %s", login, goal_name)
        self._dm.goal_accomplished.emit(login, goal_name)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _emit_all(self) -> None:
        self._dm.streamers_updated.emit(list(self._streamers))
        self._dm.global_stats_updated.emit(self._stats)
        self._emit_goals()

    def _emit_goals(self) -> None:
        """Rejoue les deux signaux d'objectifs de DataManager.

        Le cache brut alimente l'onglet Goals, la liste enrichie le bandeau des
        objectifs proches. Réémis à chaque batch pour que les pourcentages
        suivent les dons qui tombent toutes les 3 s.
        """
        self._dm.goals_raw_updated.emit(dict(self._goals_cache))
        self._dm.goals_updated.emit(self._goals_enrichis())

    def _goals_enrichis(self) -> list[GoalWithStreamer]:
        """Objectifs entre 90 % et 100 %, même règle que DataManager.

        Le plafond est volontaire : au-delà de 100 % le proxy
        « cagnotte du streamer / montant de l'objectif » n'a plus de sens.
        """
        resultats: list[GoalWithStreamer] = []
        for streamer in self._streamers:
            for objectif in self._goals_cache.get(streamer.twitch_login, []):
                if objectif.accomplished or objectif.amount <= 0:
                    continue
                pct = streamer.donation / objectif.amount * 100.0
                if pct < 90.0 or pct > 100.0:
                    continue
                resultats.append(GoalWithStreamer(
                    streamer_login=streamer.twitch_login,
                    streamer_display=streamer.display,
                    goal_name=objectif.name,
                    amount_target=objectif.amount,
                    accomplished=False,
                    pct=pct,
                ))
        resultats.sort(key=lambda g: -g.pct)
        return resultats

    def _build_goals_cache(self) -> dict[str, list[DonationGoal]]:
        """Trois objectifs par streamer, calés sur sa cagnotte de départ."""
        cache: dict[str, list[DonationGoal]] = {}
        for i, streamer in enumerate(self._streamers):
            objectifs: list[DonationGoal] = []
            for j, (ratio, accompli) in enumerate(_MOCK_GOAL_PALIERS):
                nom = _MOCK_GOAL_NOMS[
                    (i * len(_MOCK_GOAL_PALIERS) + j) % len(_MOCK_GOAL_NOMS)
                ]
                objectifs.append(DonationGoal(
                    id=f"mock-{streamer.twitch_login}-{j}",
                    name=nom,
                    amount=max(1_000.0, round(streamer.donation * ratio, -2)),
                    accomplished=accompli,
                    category="mock",
                    links=[streamer.donation_url],
                ))
            cache[streamer.twitch_login] = objectifs
        return cache

    @staticmethod
    def _build_mock_events() -> list[EventItem]:
        """Génère des événements fictifs sur 3 jours pour tester timeline + Programme."""
        _UTC2 = timezone(timedelta(hours=2))
        now_ts = _time.time()
        now_local = datetime.fromtimestamp(now_ts, tz=_UTC2)

        # Jours : hier, aujourd'hui, demain
        day0 = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
        day2 = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")

        # Slots relatifs à maintenant (en minutes) pour la timeline du jour courant
        # + programmes fixes pour les autres jours

        def make(start_delta_min: int, end_delta_min: int,
                 name: str, host: str) -> EventItem:
            s_ts = now_ts + start_delta_min * 60
            e_ts = now_ts + end_delta_min * 60
            s_dt = datetime.fromtimestamp(s_ts, tz=_UTC2)
            e_dt = datetime.fromtimestamp(e_ts, tz=_UTC2)
            return EventItem(
                id=f"mock_{name[:8].replace(' ', '_')}",
                name=name,
                day=s_dt.strftime("%Y-%m-%d"),
                start_local=s_dt.strftime("%H:%M"),
                end_local=e_dt.strftime("%H:%M"),
                description="",
                host_uuids=[host],
                participant_uuids=[],
                start_ts=s_ts,
                end_ts=e_ts,
            )

        def make_fixed(day: str, hh_start: str, hh_end: str,
                       name: str, host: str) -> EventItem:
            s_dt = datetime.strptime(f"{day} {hh_start}", "%Y-%m-%d %H:%M")
            e_dt = datetime.strptime(f"{day} {hh_end}",   "%Y-%m-%d %H:%M")
            s_ts = s_dt.replace(tzinfo=timezone(timedelta(hours=2))).timestamp()
            e_ts = e_dt.replace(tzinfo=timezone(timedelta(hours=2))).timestamp()
            return EventItem(
                id=f"mock_{name[:8].replace(' ', '_')}_{day}",
                name=name, day=day,
                start_local=hh_start, end_local=hh_end,
                description="", host_uuids=[host], participant_uuids=[],
                start_ts=s_ts, end_ts=e_ts,
            )

        events: list[EventItem] = []

        # ── Jour en cours : events centrés sur maintenant (pour la timeline) ──
        events += [
            make(-240, -120, "Tournoi Trackmania",     "zerator"),
            make( -90,  -30, "Interview Ligue Cancer", "domingo"),
            make( -20,   60, "Blind Test Musical",     "antoinedaniel"),  # EN COURS
            make(  75,  195, "Course Minecraft",       "ponce"),
            make( 210,  270, "Karaoké géant",          "squeezie"),
            make( 285,  345, "GeoGuessr Battle Royale","joyca"),
        ]

        # ── Jour précédent (programme fixe) ──────────────────────────────────
        events += [
            make_fixed(day0, "10:00", "12:00", "Ouverture + présentation", "zerator"),
            make_fixed(day0, "13:00", "15:00", "Speedrun Minecraft", "mistermv"),
            make_fixed(day0, "15:30", "17:30", "Tournoi Among Us",   "squeezie"),
            make_fixed(day0, "18:00", "20:00", "Blind Test 90s",     "antoinedaniel"),
            make_fixed(day0, "21:00", "23:30", "Show Karaoké",       "domingo"),
        ]

        # ── Jour suivant (programme fixe) ────────────────────────────────────
        events += [
            make_fixed(day2, "10:00", "12:00", "Marathon Donations",  "zerator"),
            make_fixed(day2, "13:00", "14:30", "Interview streamers", "domingo"),
            make_fixed(day2, "15:00", "17:00", "Tournoi GeoGuessr",   "joyca"),
            make_fixed(day2, "18:00", "19:30", "Lecture Goals live",  "ponce"),
            make_fixed(day2, "20:00", "23:59", "Clôture ZEvent 2026", "zerator"),
        ]

        return events

    @staticmethod
    def _fmt(n: float) -> str:
        return f"{int(n):,} €".replace(",", "\u00a0")

    @staticmethod
    def _build_initial_streamers() -> list[StreamerInfo]:
        result = []
        for login, display, game, location, viewers, donation in _MOCK_STREAMERS_SEED:
            s = StreamerInfo(
                twitch_login=login,
                display=display,
                online=True,
                game=game,
                location=location,
                viewers=viewers,
                donation=donation,
                donation_formatted=f"{int(donation):,} €".replace(",", "\u00a0"),
                profile_url="",
                # Hôte zevent.fr obligatoire : _DONATION_HOSTS rejette tout le
                # reste, et sans URL valide le bouton « donner » ne fait rien.
                donation_url=f"https://zevent.fr/dons?streamer={login}",
            )
            result.append(s)
        # Les 2 derniers démarrent offline
        result[-1].online = False
        result[-1].viewers = 0
        result[-2].online = False
        result[-2].viewers = 0
        return result
