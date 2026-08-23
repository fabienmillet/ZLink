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

    def add_point(self, donation: float, viewers: int) -> None:
        ts = time.time()
        self._donation.append((ts, donation))
        self._viewers.append((ts, viewers))

    def _in_event_window(self) -> bool:
        return _EVENT_START <= time.time() <= _EVENT_END

    def get_donation_series(self) -> tuple[list[float], list[float]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._donation:
            return [], []
        pairs = [(ts, v) for ts, v in self._donation if _EVENT_START <= ts <= _EVENT_END]
        if not pairs:
            return [], []
        ts, vals = zip(*pairs)
        return list(ts), list(vals)

    def get_viewers_series(self) -> tuple[list[float], list[int]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._viewers:
            return [], []
        pairs = [(ts, v) for ts, v in self._viewers if _EVENT_START <= ts <= _EVENT_END]
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
            self._donation.clear()
            for ts_ms, val in pairs:
                point = _sane_point(ts_ms, val)
                if point:
                    self._donation.append(point)

            # VIEWERS — viewers.large est en ordre CROISSANT → ne pas inverser
            v_labels_ms = data["viewers"]["large"]["labels"]  # ex: [1757088000000, ..., 1757286000000]
            v_values = data["viewers"]["large"]["values"]
            self._viewers.clear()
            for ts_ms, val in zip(v_labels_ms, v_values):
                point = _sane_point(ts_ms, val)
                if point:
                    self._viewers.append(point)

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

        except Exception as e:
            logger.error(f"Impossible de charger historique 2026: {e}")
