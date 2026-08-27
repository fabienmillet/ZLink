# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Historique temps réel des données globales ZEvent."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# En test → renvoie les données même hors fenêtre event.
# Mettre à False avant l'event pour activer le filtrage.
DEBUG: bool = True

# Bornes réelles de l'édition 2026 (schedule de l'API events)
_EVENT_START: float = datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc).timestamp()
_EVENT_END: float = datetime(2026, 9, 7, 2, 0, 0, tzinfo=timezone.utc).timestamp()

# Plage de timestamps acceptée pour les données tierces (2020 → 2035)
_TS_MIN: float = 1_577_836_800.0
_TS_MAX: float = 2_051_222_400.0


def _sane_point(ts_ms: object, val: object) -> tuple[float, float] | None:
    """Valide un point (timestamp ms, valeur) issu du dépôt tiers.

    Retourne None si le point est inexploitable : une valeur aberrante
    remonterait jusqu'à datetime.fromtimestamp() dans un slot Qt sans
    try/except, ce qui ferait tomber le panel.
    """
    if not isinstance(ts_ms, (int, float)) or not isinstance(val, (int, float)):
        return None
    if isinstance(ts_ms, bool) or isinstance(val, bool):
        return None
    ts = float(ts_ms) / 1000.0
    if not (_TS_MIN <= ts <= _TS_MAX):
        logger.warning("Historique: timestamp hors plage ignoré (%r)", ts_ms)
        return None
    return ts, float(val)


class HistoryStore:
    """Stocke les séries temporelles donation + viewers depuis le démarrage.

    4320 points × poll 30 s = 36 heures de rétention (couvre l'event 3 jours).
    Les données historiques pré-chargées depuis GitHub remplissent la base ;
    les données live s'ajoutent par-dessus en temps réel.
    """

    def __init__(self, max_points: int = 4320) -> None:
        self._donation: deque[tuple[float, float]] = deque(maxlen=max_points)
        self._viewers: deque[tuple[float, int]] = deque(maxlen=max_points)
        # Courbe de l'édition PRÉCÉDENTE, conservée à part. Elle sert de point
        # de comparaison — « à la même heure l'an dernier » — et ne doit donc
        # pas se mélanger aux points de l'édition en cours, que get_*_series
        # filtre par fenêtre.
        self._previous: list[tuple[float, float]] = []
        self._previous_peak_viewers: int = 0
        #: Instant du premier relevé pris EN DIRECT depuis le dernier
        #: préchargement. Sépare ce que ZLink a observé lui-même de la courbe
        #: chargée depuis GitHub, les deux vivant dans le même deque.
        self._live_depuis: float | None = None

    def add_point(self, donation: float, viewers: int,
                  instant: float | None = None) -> None:
        """Range un relevé. `instant` permet d'en antidater un.

        Antidater sert au mode mock, qui simule un événement DÉJÀ en cours :
        sans un peu de passé, la vitesse de collecte n'a rien à comparer et
        reste muette six minutes durant — le temps qu'exige son plus petit
        écart mesurable.
        """
        ts = time.time() if instant is None else float(instant)
        if self._live_depuis is None or ts < self._live_depuis:
            self._live_depuis = ts
        self._donation.append((ts, donation))
        self._viewers.append((ts, viewers))

    def _in_event_window(self) -> bool:
        return _EVENT_START <= time.time() <= _EVENT_END

    def _garder(self, ts: float) -> bool:
        """Ce point appartient-il à l'édition EN COURS.

        Un point de la fenêtre de l'édition en est toujours.

        En dehors, DEBUG tranche — et seulement pour les relevés pris EN
        DIRECT. C'est ce que le module annonce depuis toujours (« en test,
        renvoie les données même hors fenêtre event ») sans jamais le faire :
        le premier garde respectait DEBUG, ce filtre-ci l'ignorait, et la
        série revenait vide quand même. Le mode mock restait donc sans
        courbe, sans vitesse et sans projection.

        L'historique préchargé, lui, reste borné à sa fenêtre quoi qu'il
        arrive : le deque porte aussi la courbe de l'édition précédente, et
        la publier donnerait la vitesse de fin du ZEvent 2025.
        """
        if _EVENT_START <= ts <= _EVENT_END:
            return True
        return DEBUG and self._live_depuis is not None and ts >= self._live_depuis

    def get_donation_series(self) -> tuple[list[float], list[float]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._donation:
            return [], []
        pairs = [(ts, v) for ts, v in self._donation if self._garder(ts)]
        if not pairs:
            return [], []
        ts, vals = zip(*pairs)
        return list(ts), list(vals)

    # -- vitesse et projection -------------------------------------------------

    def donation_rate(self, window_s: float = 900.0) -> float | None:
        """Vitesse de collecte en euros par minute sur la fenêtre demandée.

        Ne lit QUE les points de l'édition en cours : le deque contient aussi
        l'historique de l'édition précédente, préchargé depuis GitHub. Le lire
        brut donnait la vitesse de fin du ZEvent 2025, ou une pente négative au
        raccord entre les deux séries.

        Retourne None tant qu'on n'a pas deux points espacés d'au moins un
        dixième de la fenêtre : sur un intervalle trop court, le bruit de
        l'arrondi de la cagnotte donnerait une vitesse fantaisiste.
        """
        ts_all, vals_all = self.get_donation_series()
        if len(ts_all) < 2:
            return None
        now = ts_all[-1]
        cutoff = now - window_s
        window = [(t, v) for t, v in zip(ts_all, vals_all) if t >= cutoff]
        if len(window) < 2:
            window = list(zip(ts_all, vals_all))[-2:]
        (t0, v0), (t1, v1) = window[0], window[-1]
        span = t1 - t0
        if span < max(window_s * 0.1, 30.0):
            return None
        rate = (v1 - v0) / (span / 60.0)
        # Une cagnotte est cumulative : elle ne décroît pas. Une pente négative
        # ne peut venir que d'une correction de l'API ou d'un raccord de séries,
        # et l'extrapoler produirait une projection négative.
        return rate if rate >= 0.0 else None

    def _horizon(self, end_ts: float) -> float | None:
        """Jusqu'où extrapoler, ou None si extrapoler n'a pas de sens.

        Pendant l'édition, c'est sa fin. En dehors, rien : projeter jusqu'au
        7 septembre depuis une date d'août donnait des milliards, et c'est
        pour ça que le garde-fou existe.

        En test, ce garde-fou fermait la porte que DEBUG venait d'ouvrir : la
        série et la vitesse revenaient bien, mais la projection restait vide et
        l'Accueil affichait « disponible au début de l'event » alors que le
        mode mock injectait des dons à chaque seconde. On extrapole alors sur
        la DURÉE d'une édition comptée depuis le premier relevé — le mock
        simule un événement en cours, la projection porte donc sur un
        événement de même longueur, et garde un ordre de grandeur plausible
        au lieu de courir sur deux semaines.
        """
        maintenant = time.time()
        if _EVENT_START <= maintenant <= _EVENT_END:
            return end_ts
        if not DEBUG or self._live_depuis is None:
            return None
        return self._live_depuis + (_EVENT_END - _EVENT_START)

    def projected_total(self, end_ts: float, window_s: float = 3600.0) -> float | None:
        """Extrapole la cagnotte finale à la vitesse récente.

        Volontairement linéaire : la collecte n'est pas linéaire sur trois jours
        (pics de soirée, dernière ligne droite), donc c'est un ordre de grandeur
        « au rythme actuel », pas une prédiction. À afficher comme tel.

        Retourne None hors de l'édition : extrapoler quatorze jours avant le
        coup d'envoi n'a aucun sens et donnait des milliards.
        """
        horizon = self._horizon(end_ts)
        if horizon is None:
            return None
        end_ts = horizon
        ts_all, vals_all = self.get_donation_series()
        if not ts_all:
            return None
        rate = self.donation_rate(window_s)
        if rate is None:
            return None
        now, current = ts_all[-1], vals_all[-1]
        remaining_min = (end_ts - now) / 60.0
        if remaining_min <= 0:
            return current
        return current + rate * remaining_min

    # -- comparaison avec l'édition précédente ---------------------------------

    def previous_total_at(self, elapsed_s: float) -> float | None:
        """Cagnotte de l'édition précédente après `elapsed_s` de course.

        L'alignement se fait sur le TEMPS ÉCOULÉ depuis le coup d'envoi, pas
        sur la date : les deux éditions ne tombent pas les mêmes jours. Le
        premier point relevé sert d'origine — la collecte publiée commence avec
        l'événement.

        Interpolation linéaire entre les deux points encadrants ; None si l'on
        sort de la plage couverte.
        """
        pts = self._previous
        if len(pts) < 2 or elapsed_s < 0:
            return None
        t0 = pts[0][0]
        cible = t0 + elapsed_s
        if cible < t0 or cible > pts[-1][0]:
            return None
        # Les points sont peu nombreux (une centaine) : la recherche linéaire
        # coûte moins qu'un index à maintenir.
        for (ta, va), (tb, vb) in zip(pts, pts[1:]):
            if ta <= cible <= tb:
                if tb == ta:
                    return vb
                k = (cible - ta) / (tb - ta)
                return va + (vb - va) * k
        return None

    def compare_to_previous(self, current_total: float,
                            now_ts: float | None = None) -> tuple[float, float] | None:
        """(total de l'édition précédente au même instant, écart en %).

        None hors de l'édition en cours, ou si la référence ne couvre pas ce
        moment — comparer sans référence n'aurait aucun sens.
        """
        now = now_ts if now_ts is not None else time.time()
        if not (_EVENT_START <= now <= _EVENT_END):
            return None
        # Origine = premier point RELEVÉ de l'édition en cours, pas _EVENT_START :
        # celui-ci est une frontière de minuit servant à filtrer la fenêtre,
        # alors que le direct démarre en soirée. Prendre minuit décalait la
        # comparaison de plus de quinze heures, et faisait sortir de la plage
        # couverte bien avant la fin. Les deux courbes sont ainsi alignées sur
        # « l'instant où la cagnotte a commencé à être comptée ».
        ts_live, _ = self.get_donation_series()
        origine = ts_live[0] if ts_live else _EVENT_START
        ref = self.previous_total_at(now - origine)
        if ref is None or ref <= 0 or current_total <= 0:
            return None
        return ref, (current_total - ref) / ref * 100.0

    @property
    def previous_peak_viewers(self) -> int:
        return self._previous_peak_viewers

    @property
    def event_start_ts(self) -> float:
        return _EVENT_START

    @property
    def event_end_ts(self) -> float:
        return _EVENT_END

    def get_viewers_series(self) -> tuple[list[float], list[int]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._viewers:
            return [], []
        pairs = [(ts, v) for ts, v in self._viewers if self._garder(ts)]
        if not pairs:
            return [], []
        ts, vals = zip(*pairs)
        return list(ts), list(vals)

    async def load_historical_2026(self):
        """Charge l'historique depuis un dépôt GitHub Pages tiers.

        Les valeurs sont filtrées : un timestamp aberrant se propagerait jusqu'à
        datetime.fromtimestamp() dans un slot Qt sans try/except (crash du panel).
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://maniarr.github.io/cache.zevent.gdoc.fr/statistics/all.json"
                )
                r.raise_for_status()
                data = r.json()

            # CAGNOTTE — pools.large est en ordre DÉCROISSANT → inverser
            labels_ms = data["pools"]["large"]["labels"]  # ex: [1757286000000, ..., 1757089800000]
            values = data["pools"]["large"]["values"]
            # Zip puis inverser pour avoir ordre chronologique (ancien → récent)
            pairs = list(zip(labels_ms, values))
            pairs.reverse()
            # Tout-ou-rien : les listes sont construites EN LOCAL, et l'état
            # n'est remplacé qu'une fois le dépôt entièrement lu. Vider
            # self._donation avant de lire la section viewers laissait, si
            # celle-ci manquait, la courbe de l'édition PRÉCÉDENTE en place
            # avec self._previous vide : donation_rate() mesurait alors la
            # vitesse de l'édition passée en la présentant comme celle en cours.
            dons = [p for p in (_sane_point(ts, v) for ts, v in pairs) if p]

            # VIEWERS — viewers.large est en ordre CROISSANT → ne pas inverser
            v_labels_ms = data["viewers"]["large"]["labels"]  # ex: [1757088000000, ..., 1757286000000]
            v_values = data["viewers"]["large"]["values"]
            vues = [p for p in (_sane_point(ts, v)
                                for ts, v in zip(v_labels_ms, v_values)) if p]

            self._donation.clear()
            self._donation.extend(dons)
            self._viewers.clear()
            self._viewers.extend(vues)

            # Copie de la courbe précédente AVANT que les points en direct ne
            # s'y ajoutent : c'est elle qui servira de référence.
            self._previous = [(t, v) for t, v in self._donation]
            # Le préchargement remplace tout : les relevés en direct
            # recommencent après lui.
            self._live_depuis = None
            if self._viewers:
                # int() : _sane_point convertit tout en float, et la propriété
                # annonce un entier — un nombre de spectateurs n'est pas décimal.
                self._previous_peak_viewers = int(max(v for _, v in self._viewers))
            logger.info(
                "Édition précédente : %d points, total final %.0f €, pic %d viewers",
                len(self._previous),
                self._previous[-1][1] if self._previous else 0.0,
                self._previous_peak_viewers,
            )

            # VÉRIFICATION — logger les 3 premiers points pour confirmer
            if self._donation:
                from datetime import datetime
                ts0 = self._donation[0][0]
                ts1 = self._donation[-1][0]
                logger.info(f"Cagnotte: {datetime.fromtimestamp(ts0, tz=timezone.utc)} → {datetime.fromtimestamp(ts1, tz=timezone.utc)}")
            # Doit afficher: 2026-09-03 07:30:00+00:00 → 2026-09-05 18:00:00+00:00
            if self._viewers:
                ts0 = self._viewers[0][0]
                ts1 = self._viewers[-1][0]
                logger.info(f"Viewers: {datetime.fromtimestamp(ts0, tz=timezone.utc)} → {datetime.fromtimestamp(ts1, tz=timezone.utc)}")
            # Doit afficher: 2026-09-03 06:00:00+00:00 → 2026-09-05 18:00:00+00:00

        except Exception:
            logger.exception("Impossible de charger historique 2026")
