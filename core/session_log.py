# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Journal de la session en cours, pour le récapitulatif de fin.

Tout est déjà signalé quelque part — le fil d'événements, les toasts, la
grille — mais rien n'est conservé : à la fermeture, la soirée disparaît. Ce
module retient l'essentiel au fil de l'eau, en mémoire, sans rien écrire tant
qu'on ne le lui demande pas.

Volontairement modeste : quelques listes plafonnées et un dictionnaire de
durées. Les compteurs sont alimentés depuis le thread Qt, mais un verrou
protège les écritures — les clips et les alertes peuvent arriver d'ailleurs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: Au-delà, on ne garde que les plus récents : un récapitulatif de trois cents
#: lignes ne se lit pas, et la mémoire n'a pas à croître sans limite.
_MAX_ITEMS = 200


@dataclass
class _Moment:
    ts: float
    login: str
    text: str
    extra: str = ""


@dataclass
class SessionSummary:
    """Instantané exploitable par l'interface."""
    started_at: float = 0.0
    duration_s: float = 0.0
    watch: list[tuple[str, float]] = field(default_factory=list)
    hype: list[_Moment] = field(default_factory=list)
    goals: list[_Moment] = field(default_factory=list)
    milestones: list[_Moment] = field(default_factory=list)
    clips: list[_Moment] = field(default_factory=list)
    donation_start: float = 0.0
    donation_end: float = 0.0
    viewers_peak: int = 0


class SessionLog:
    """Enregistre ce qui s'est passé pendant la session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_wall = time.time()
        self._started_mono = time.monotonic()
        self._watch: dict[str, float] = {}
        self._current: str = ""
        self._current_since: float = 0.0
        self._hype: list[_Moment] = []
        self._goals: list[_Moment] = []
        self._milestones: list[_Moment] = []
        self._clips: list[_Moment] = []
        self._donation_start: float = 0.0
        self._donation_end: float = 0.0
        self._viewers_peak: int = 0

    # -- alimentation ----------------------------------------------------

    def set_current_stream(self, login: str) -> None:
        """Change le direct regardé, en clôturant le temps du précédent."""
        now = time.monotonic()
        with self._lock:
            self._close_current(now)
            self._current = login or ""
            self._current_since = now

    def _close_current(self, now: float) -> None:
        if self._current and self._current_since:
            self._watch[self._current] = (
                self._watch.get(self._current, 0.0) + (now - self._current_since))
        self._current_since = now

    def _add(self, bucket: list[_Moment], moment: _Moment) -> None:
        with self._lock:
            bucket.append(moment)
            del bucket[:-_MAX_ITEMS]

    def add_hype(self, login: str, label: str, score: float) -> None:
        self._add(self._hype, _Moment(time.time(), login, label, f"{score:.0%}"))

    def add_goal(self, login: str, goal_name: str) -> None:
        self._add(self._goals, _Moment(time.time(), login, goal_name))

    def add_milestone(self, label: str) -> None:
        self._add(self._milestones, _Moment(time.time(), "", label))

    def add_clip(self, login: str, path: str) -> None:
        if path:
            self._add(self._clips, _Moment(time.time(), login, path))

    def observe_stats(self, donation_total: float, viewers_total: int) -> None:
        """Suit la cagnotte et le pic d'audience. Sans effet si les données manquent."""
        with self._lock:
            try:
                don = float(donation_total or 0.0)
                vue = int(viewers_total or 0)
            except (TypeError, ValueError):
                return
            if don > 0:
                # Le premier relevé NON NUL sert de point de départ : avant
                # l'event l'API renvoie zéro, et partir de zéro afficherait
                # toute la cagnotte comme récoltée pendant la session.
                if self._donation_start <= 0:
                    self._donation_start = don
                self._donation_end = max(self._donation_end, don)
            self._viewers_peak = max(self._viewers_peak, vue)

    # -- lecture ---------------------------------------------------------

    def summary(self) -> SessionSummary:
        now = time.monotonic()
        with self._lock:
            watch = dict(self._watch)
            if self._current and self._current_since:
                watch[self._current] = (
                    watch.get(self._current, 0.0) + (now - self._current_since))
            return SessionSummary(
                started_at=self._started_wall,
                duration_s=now - self._started_mono,
                watch=sorted(watch.items(), key=lambda kv: -kv[1]),
                hype=list(self._hype),
                goals=list(self._goals),
                milestones=list(self._milestones),
                clips=list(self._clips),
                donation_start=self._donation_start,
                donation_end=self._donation_end,
                viewers_peak=self._viewers_peak,
            )


def fmt_duration(seconds: float) -> str:
    """3725 → « 1 h 02 », 420 → « 7 min », 30 → « 30 s »."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm:02d}"


def fmt_euros(amount: float) -> str:
    return f"{amount:,.0f} €".replace(",", " ")


#: Journal unique de l'application.
SESSION = SessionLog()
