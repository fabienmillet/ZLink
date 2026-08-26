# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""AdWatcher — détecte les coupures publicitaires Twitch via polling M3U8.

Principes :
- Aucun flux vidéo téléchargé : on ne récupère que le texte de la playlist HLS
  (quelques Ko toutes les 3 s).
- Marqueurs surveillés : EXT-X-DATERANGE avec CLASS="twitch-stitched-ad",
  EXT-X-CUE-OUT, et les valeurs SCTE35 indiquant un ad break.
- Pour éviter les faux positifs, une pub n'est confirmée qu'après 2 polls positifs
  consécutifs ; la fin de pub est confirmée après 3 polls négatifs consécutifs.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING

import httpx
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

_POLL_INTERVAL  = 3.0   # secondes entre deux polls
_AD_CONFIRM     = 3     # polls positifs consécutifs pour confirmer ad_start
_END_CONFIRM    = 2     # polls négatifs consécutifs pour confirmer ad_end
_STARTUP_GRACE  = 12.0  # secondes d'immunité après watch() — évite les faux positifs de démarrage

# Marqueurs HLS indiquant une publicité Twitch.
# On exclut EXT-X-CUE-OUT seul (trop générique, présent lors de la mise en buffer initiale).
# On garde uniquement les marqueurs spécifiques Twitch.
_AD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'EXT-X-DATERANGE.*CLASS="twitch-stitched-ad"',  re.IGNORECASE),
    re.compile(r'EXT-X-DATERANGE.*CLASS="stitched-ad"',         re.IGNORECASE),
    re.compile(r'/ad_video/',                                    re.IGNORECASE),
    re.compile(r'EXT-X-ASSET:.*CAID=',                          re.IGNORECASE),
]


def _playlist_has_ad(text: str) -> bool:
    """Retourne True si le texte M3U8 contient des marqueurs de publicité Twitch."""
    return any(p.search(text) for p in _AD_PATTERNS)


class _StreamWatcher:
    """Surveille un seul flux HLS dans un thread daemon."""

    def __init__(
        self,
        login: str,
        hls_url: str,
        on_ad_start: "callable[[str], None]",
        on_ad_end:   "callable[[str], None]",
    ) -> None:
        self.login    = login
        self.hls_url  = hls_url
        self._stop    = threading.Event()
        self._on_start = on_ad_start
        self._on_end   = on_ad_end
        # État de la machine à confirmer, lu et écrit par le seul thread.
        self._pub_active = False
        self._pos_streak = 0   # polls consécutifs avec marqueurs
        self._neg_streak = 0   # polls consécutifs sans marqueurs

        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"ad-watch-{login}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        start_time = time.monotonic()
        client = httpx.Client(timeout=5.0, follow_redirects=True)
        try:
            while not self._stop.is_set():
                text = self._fetch(client)
                has_ad = _playlist_has_ad(text) if text else False
                # Les marqueurs sont ignorés pendant la période de grâce du
                # démarrage : le début d'un flux en contient souvent.
                if (time.monotonic() - start_time) >= _STARTUP_GRACE:
                    self._transition(has_ad)
                self._stop.wait(_POLL_INTERVAL)
        finally:
            client.close()

    def _transition(self, has_ad: bool) -> None:
        """Compte les polls consécutifs et bascule l'état une fois confirmé.

        Une pub n'est annoncée qu'après _AD_CONFIRM relevés positifs d'affilée,
        et sa fin après _END_CONFIRM négatifs : un relevé aberrant isolé ne doit
        pas faire clignoter le bandeau.
        """
        if has_ad:
            self._pos_streak += 1
            self._neg_streak = 0
            if not self._pub_active and self._pos_streak >= _AD_CONFIRM:
                self._pub_active = True
                logger.info("AdWatcher: pub détectée sur %s", self.login)
                self._on_start(self.login)
            return
        self._neg_streak += 1
        self._pos_streak = 0
        if self._pub_active and self._neg_streak >= _END_CONFIRM:
            self._pub_active = False
            logger.info("AdWatcher: pub terminée sur %s", self.login)
            self._on_end(self.login)

    def _fetch(self, client: httpx.Client) -> str:
        """Récupère le texte du M3U8. Retourne '' en cas d'erreur."""
        try:
            r = client.get(self.hls_url)
            if r.status_code == 200:
                return r.text
        except Exception as exc:
            logger.debug("AdWatcher._fetch(%s): %s", self.login, exc)
        return ""


class AdWatcher(QObject):
    """Gestionnaire global de surveillance des pubs pour plusieurs streams.

    Signaux :
        ad_detected(login)  — une pub a commencé sur ce stream
        ad_ended(login)     — la pub est terminée sur ce stream
    """

    ad_detected = pyqtSignal(str)   # login
    ad_ended    = pyqtSignal(str)   # login

    def __init__(self, parent: "QObject | None" = None) -> None:
        super().__init__(parent)
        self._watchers: dict[str, _StreamWatcher] = {}  # login → watcher

    # -- public API -----------------------------------------------------------

    def watch(self, login: str, hls_url: str) -> None:
        """Démarre (ou redémarre) la surveillance d'un flux.

        Appeler depuis le main thread après avoir reçu l'URL HLS résolue.
        """
        self.unwatch(login)   # arrête l'éventuel watcher précédent
        if not hls_url:
            return
        logger.debug("AdWatcher: surveillance démarrée pour %s", login)
        self._watchers[login] = _StreamWatcher(
            login, hls_url,
            on_ad_start=lambda lg: self.ad_detected.emit(lg),
            on_ad_end=lambda lg: self.ad_ended.emit(lg),
        )

    def unwatch(self, login: str) -> None:
        """Arrête la surveillance d'un flux."""
        watcher = self._watchers.pop(login, None)
        if watcher:
            watcher.stop()
            logger.debug("AdWatcher: surveillance arrêtée pour %s", login)

    def unwatch_all(self) -> None:
        """Arrête toutes les surveillances (ex: fermeture de l'app)."""
        for w in self._watchers.values():
            w.stop()
        self._watchers.clear()
