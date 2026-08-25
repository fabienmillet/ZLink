# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""StreamManager — gestion des instances Streamlink + MPV."""

from __future__ import annotations

import logging
import time
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

# Qualités streamlink
QUALITY_FULLSCREEN = "best"
# Twitch nomme ses rendus 160p30 / 360p30 / 480p30 / 720p60 / 1080p60 et
# streamlink exige une correspondance EXACTE : « 360p », « 480p » et « 720p »
# n'existent pas et retombaient silencieusement sur « worst », c'est-à-dire
# 284x160. Les deux derniers paliers donnaient donc la même image.
# Durée pendant laquelle un nouveau palier doit se confirmer avant qu'on
# relance la grille.
_QUALITY_DEBOUNCE_S = 45.0

QUALITY_GRID = "360p30,160p30,worst"

# Qualité adaptative : chaque palier est (nombre max de flux, qualité). Le premier
# palier dont le seuil couvre le nombre de flux actifs gagne ; au-delà du dernier,
# on retombe sur QUALITY_GRID. Budget visé : ~50 Mbps et VCN < 50 %.
_DEFAULT_ADAPTIVE_TIERS: list[tuple[int, str]] = [
    (1, "1080p60,best"),
    (4, "720p60,480p30,360p30"),
    (9, "480p30,360p30,160p30"),
]


def _parse_tiers(raw: object) -> list[tuple[int, str]]:
    """Valide des paliers venant de config.json. Retourne [] si inexploitable."""
    if not isinstance(raw, list) or not raw:
        return []
    tiers: list[tuple[int, str]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            logger.error("Palier adaptatif ignoré (format inattendu) : %r", entry)
            return []
        count, quality = entry
        if not isinstance(count, int) or count < 1:
            logger.error("Palier adaptatif ignoré (seuil invalide) : %r", entry)
            return []
        tiers.append((count, safe_quality(quality, QUALITY_GRID)))
    return sorted(tiers, key=lambda t: t[0])

# Résoudre l'exécutable streamlink : priorité au venv courant, fallback PATH
def _streamlink_exe() -> str:
    """Retourne le chemin absolu vers streamlink.

    Ordre de recherche :
    1. Scripts/ du Python en cours d'exécution (venv actif)
    2. .venv/Scripts/ ou .venv/bin/ relatif à la racine du projet
    3. Fallback : "streamlink" dans le PATH système
    """
    candidates: list[str] = []

    # 1. Même dossier que sys.executable (venv activé)
    venv_bin = os.path.dirname(sys.executable)
    candidates += [os.path.join(venv_bin, n) for n in ("streamlink.exe", "streamlink")]

    # 2. .venv relatif à la racine du projet (stream_manager.py est dans core/)
    project_root = pathlib.Path(__file__).resolve().parent.parent
    for scripts_dir in (project_root / ".venv" / "Scripts", project_root / ".venv" / "bin"):
        candidates += [str(scripts_dir / n) for n in ("streamlink.exe", "streamlink")]

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    # Dernier recours : PATH via shutil.which, qui renvoie un chemin absolu.
    # Ne jamais retourner le nom nu "streamlink" : sous Windows, CreateProcess
    # cherche d'abord le dossier de l'application et le répertoire courant.
    found = shutil.which("streamlink")
    if found:
        return found
    logger.error("streamlink introuvable (venv et PATH) — lecture vidéo indisponible")
    return ""

_STREAMLINK = _streamlink_exe()
logger.info("streamlink exe: %s", _STREAMLINK)


# Une qualité streamlink : tokens alphanumériques séparés par des virgules
# ("best", "360p,worst"). Tout ce qui commence par "-" serait interprété comme
# une option par streamlink (--plugin-dirs exécute du Python arbitraire).
_QUALITY_RE = re.compile(r"^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*$")


# Anciens sélecteurs, écrits avant qu'on découvre que Twitch nomme ses rendus
# « 360p30 » et non « 360p » : ils retombaient tous sur « worst », soit 284x160.
# Une config existante est donc migrée au chargement.
_LEGACY_QUALITY = {
    "360p,worst":            "360p30,160p30,worst",
    "480p,360p,worst":       "480p30,360p30,160p30",
    "720p,480p,worst":       "720p60,480p30,360p30",
    "1080p60,1080p,best":    "1080p60,best",
    "720p60,720p,480p":      "720p60,480p30,360p30",
}


def migrate_quality(value: str) -> str:
    """Traduit un sélecteur hérité vers un rendu qui existe réellement."""
    return _LEGACY_QUALITY.get((value or "").strip(), value)


def safe_quality(raw: object, default: str) -> str:
    """Qualité validée. Retourne `default` (et logge) si le format est inattendu."""
    quality = str(raw or "").strip()
    if not quality:
        return default
    if not _QUALITY_RE.match(quality):
        logger.error(
            "Qualité rejetée (format inattendu, ignorée) : %r — repli sur %s",
            quality[:60], default,
        )
        return default
    return quality


def _get_stream_url(
    twitch_login: str,
    quality: str = QUALITY_FULLSCREEN,
    timeout: int = 20,
) -> str:
    """Résolution synchrone via streamlink --stream-url.

    Bloquant — toujours appeler depuis un thread séparé.
    Retourne '' si échec.
    """
    if not _STREAMLINK:
        logger.error("_get_stream_url(%s): streamlink introuvable", twitch_login)
        return ""
    try:
        result = subprocess.run(
            [
                _STREAMLINK,
                f"twitch.tv/{twitch_login}",
                safe_quality(quality, QUALITY_FULLSCREEN),
                "--stream-url",
                "--twitch-disable-ads",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        url = result.stdout.strip()
        if result.returncode != 0 or not url:
            logger.error(
                "streamlink(%s, %s) rc=%d: %s",
                twitch_login, quality, result.returncode, result.stderr.strip()[:200],
            )
            return ""
        return url
    except FileNotFoundError:
        logger.error("streamlink introuvable — vérifier l'installation et le PATH")
        return ""
    except subprocess.TimeoutExpired:
        logger.error("streamlink(%s) timeout après %ds", twitch_login, timeout)
        return ""
    except Exception as exc:
        logger.error("streamlink(%s): %s", twitch_login, exc)
        return ""


class StreamManager(QObject):
    """Gère le stream fullscreen : résolution URL via streamlink + lecture MPV.

    Toute résolution streamlink se fait dans un thread QThread-compatible
    (via threading.Thread) pour ne pas bloquer l'UI.

    Signals :
        stream_ready(login, url)   — URL résolue, prête pour mpv.play()
        stream_error(login, msg)   — échec de résolution
        stream_stopped(login)      — stream arrêté proprement
    """

    stream_ready = pyqtSignal(str, str)   # login, url
    stream_error = pyqtSignal(str, str)   # login, message
    stream_stopped = pyqtSignal(str)      # login
    grid_quality_changed = pyqtSignal(str)  # new quality string

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_login: str = ""
        self._resolving: bool = False
        self._quality_fullscreen: str = QUALITY_FULLSCREEN
        self._grid_quality: str = QUALITY_GRID
        self._pending_quality: str | None = None
        self._pending_since: float = 0.0
        self._adaptive: bool = True
        self._adaptive_tiers: list[tuple[int, str]] = list(_DEFAULT_ADAPTIVE_TIERS)
        self._active_grid_count: int = 0
        self._max_active_streams: int = 20

    # -- public API -----------------------------------------------------------

    def reload_config(self, config: dict) -> None:
        """Met à jour les qualités et limites à chaud."""
        old_grid_quality = self._grid_quality
        self._adaptive = bool(config.get("grid_adaptive", True))
        tiers = _parse_tiers(config.get("grid_adaptive_tiers"))
        if tiers:
            self._adaptive_tiers = tiers
        if self._adaptive:
            self._grid_quality = self.quality_for_count(self._active_grid_count or 1)
        else:
            self._grid_quality = safe_quality(
                migrate_quality(config.get("grid_quality") or ""), QUALITY_GRID)
        self._quality_fullscreen = safe_quality(
            config.get("fullscreen_quality"), QUALITY_FULLSCREEN
        )
        self._max_active_streams = config.get("max_active_streams", 20)
        if old_grid_quality != self._grid_quality:
            logger.info(
                "StreamManager: qualité grille changée %s → %s — relance des streams actifs",
                old_grid_quality, self._grid_quality,
            )
            self.grid_quality_changed.emit(self._grid_quality)
        logger.info(
            "StreamManager: config rechargée — fs=%s grid=%s max=%d",
            self._quality_fullscreen, self._grid_quality, self._max_active_streams,
        )

    # -- qualité adaptative ---------------------------------------------------

    def quality_for_count(self, count: int) -> str:
        """Qualité correspondant à `count` flux simultanés dans la grille."""
        for threshold, quality in self._adaptive_tiers:
            if count <= threshold:
                return quality
        return QUALITY_GRID

    def set_active_grid_count(self, count: int) -> None:
        """Déclaré par la grille à chaque changement du nombre de flux joués.

        Recalcule la qualité et, si le palier change, émet grid_quality_changed —
        ce qui relance les cellules actives dans la nouvelle qualité.
        """
        self._active_grid_count = max(0, int(count))
        if not self._adaptive or self._active_grid_count == 0:
            return
        target = self.quality_for_count(self._active_grid_count)
        if target == self._grid_quality:
            self._pending_quality = None
            return
        # Anti-rebond : changer de palier relance TOUTES les cellules (arrêt,
        # résolution streamlink, rechargement mpv — une dizaine de secondes).
        # Pendant un event, un streamer qui oscille autour du seuil déclencherait
        # cette tempête à chaque sondage. On exige que le nouveau palier tienne.
        now = time.monotonic()
        if target != self._pending_quality:
            self._pending_quality = target
            self._pending_since = now
            logger.debug("Qualité adaptative : %s en attente de confirmation", target)
            return
        if now - self._pending_since < _QUALITY_DEBOUNCE_S:
            return
        logger.info(
            "Qualité adaptative : %d flux → %s (était %s)",
            self._active_grid_count, target, self._grid_quality,
        )
        self._grid_quality = target
        self._pending_quality = None
        self.grid_quality_changed.emit(target)

    def resolve_grid_quality(self, count: int) -> str:
        """Qualité à utiliser pour `count` flux — sans effet de bord.

        La grille l'appelle avant de démarrer ses cellules, ce qui évite de les
        lancer dans une qualité puis de les relancer aussitôt.
        """
        if self._adaptive and count > 0:
            return self.quality_for_count(count)
        return self._grid_quality

    @property
    def grid_quality(self) -> str:
        return self._grid_quality

    def play(self, twitch_login: str, quality: str | None = None) -> None:
        """Lance la résolution + lecture pour un streamer.

        Si un stream est déjà en cours, il est d'abord arrêté.
        La résolution streamlink se fait dans un thread séparé.
        """
        resolved_quality = quality if quality is not None else self._quality_fullscreen
        # Arrêter le stream actif uniquement s'il n'est pas déjà en cours de résolution
        # (pas de stop() pendant résolution : MPV ne joue rien, stopping serait erroné)
        if self._current_login and not self._resolving:
            self.stop()

        self._current_login = twitch_login
        self._resolving = True
        logger.info("StreamManager: résolution %s @ %s…", twitch_login, resolved_quality)

        t = threading.Thread(
            target=self._resolve_worker,
            args=(twitch_login, resolved_quality),
            daemon=True,
            name=f"streamlink-{twitch_login}",
        )
        t.start()

    def stop(self) -> None:
        """Marque le stream courant comme arrêté (l'arrêt MPV est géré par FullscreenWindow)."""
        if self._current_login:
            login = self._current_login
            self._current_login = ""
            self.stream_stopped.emit(login)
            logger.info("StreamManager: stream %s arrêté", login)

    @property
    def current_login(self) -> str:
        return self._current_login

    # -- internal -------------------------------------------------------------

    def _resolve_worker(self, login: str, quality: str) -> None:
        url = _get_stream_url(login, quality)
        self._resolving = False

        # Vérifier que le stream demandé est toujours le courant
        # (l'utilisateur a peut-être cliqué sur un autre entre-temps)
        if login != self._current_login:
            logger.debug("StreamManager: %s abandonné (remplacé par %s)", login, self._current_login)
            return

        if url:
            logger.info("StreamManager: URL résolue pour %s", login)
            self.stream_ready.emit(login, url)
        else:
            self._current_login = ""
            self.stream_error.emit(login, "Impossible de résoudre l'URL du stream")
