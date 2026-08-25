# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Widget MPV réutilisable — embed libmpv dans un QWidget via wid (HWND)."""

from __future__ import annotations

import logging
import os
import re
import pathlib
import locale
import shutil
import subprocess
import sys
import threading
import time
import weakref
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import mpv as _mpv_type

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QOpenGLContext
from PyQt6.QtWidgets import QApplication, QWidget

from core.stream_manager import safe_quality

# mpv n'implémente --wid que sur X11, win32 (HWND) et Android : sur macOS il
# ouvrirait sa propre fenêtre. On y passe donc par l'API de rendu libmpv, où
# c'est nous qui dessinons les frames dans le FBO d'un QOpenGLWidget.
_RENDER_API = sys.platform == "darwin"

if _RENDER_API:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    _MpvBase: type = QOpenGLWidget
else:
    _MpvBase = QWidget


def _gl_proc_address(_ctx: object, name: bytes) -> int:
    """Résolution des symboles OpenGL pour libmpv, via le contexte Qt courant."""
    gl_ctx = QOpenGLContext.currentContext()
    if gl_ctx is None:
        return 0
    return int(gl_ctx.getProcAddress(name))

from core import x11_guard as _x11_guard

logger = logging.getLogger(__name__)

# main.py bascule normalement sur xcb tout seul en session Wayland : on n'avertit
# que si ce garde-fou a été contourné (QT_QPA_PLATFORM forcé, ou import du widget
# hors du point d'entrée), auquel cas --wid ne fonctionnera pas.
if (
    sys.platform.startswith("linux")
    and os.environ.get("XDG_SESSION_TYPE") == "wayland"
    and os.environ.get("QT_QPA_PLATFORM") != "xcb"
):
    logger.warning(
        "Session Wayland sans XWayland : mpv n'implémente --wid que sur X11, la "
        "vidéo s'ouvrirait dans des fenêtres séparées. Lancer avec "
        "QT_QPA_PLATFORM=xcb pour passer par XWayland."
    )

# Import paresseux : mpv.py lève OSError au module-level si libmpv-2.dll est absent.
# Sur Windows, ctypes.util.find_library cherche dans os.environ["PATH"].
# os.add_dll_directory() est aussi nécessaire pour Python 3.8+.
from core.paths import RESOURCE_ROOT as _RES_ROOT

_PROJECT_ROOT = str(_RES_ROOT)
_path_entries = os.environ.get("PATH", "").split(os.pathsep)
if _PROJECT_ROOT not in _path_entries:
    os.environ["PATH"] = _PROJECT_ROOT + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(_PROJECT_ROOT)  # type: ignore[attr-defined]
    except OSError:
        pass


def _bundled_libmpv() -> str:
    """Chemin de la libmpv livrée avec l'application, vide s'il n'y en a pas."""
    for nom in ("libmpv.2.dylib", "libmpv.dylib", "libmpv.so.2", "libmpv.so"):
        chemin = _RES_ROOT / nom
        if chemin.is_file():
            return str(chemin)
    return ""


if not sys.platform.startswith("win"):
    # python-mpv appelle ctypes.util.find_library('mpv'), qui ne regarde QUE les
    # emplacements système. Une libmpv livrée dans le paquet lui est donc
    # invisible — et une application signée ne peut pas compter sur
    # DYLD_LIBRARY_PATH, que macOS efface pour les processus durcis. On répond
    # nous-mêmes pour ce seul nom, avant l'import de mpv.
    _LIBMPV_EMBARQUEE = _bundled_libmpv()
    if _LIBMPV_EMBARQUEE:
        import ctypes.util as _ctypes_util

        _find_library_origine = _ctypes_util.find_library

        def _find_library(nom: str):        # noqa: ANN202 - signature imposée
            if nom == "mpv":
                return _LIBMPV_EMBARQUEE
            return _find_library_origine(nom)

        _ctypes_util.find_library = _find_library
        logger.info("libmpv livrée avec l'application : %s", _LIBMPV_EMBARQUEE)

try:
    import mpv as _mpv_module
    _MPV_AVAILABLE = True
except OSError as _mpv_err:
    _mpv_module = None  # type: ignore[assignment]
    _MPV_AVAILABLE = False
    logger.warning(
        "libmpv introuvable — lecture vidéo désactivée. "
        "Placer mpv-2.dll ou libmpv-2.dll dans le dossier du projet. (%s)",
        _mpv_err,
    )


def _find_streamlink() -> str:
    """Retourne le chemin vers streamlink : venv courant, puis .venv projet, puis PATH."""
    project_root = pathlib.Path(__file__).resolve().parent.parent
    venv_bin = pathlib.Path(sys.executable).parent
    candidates = [venv_bin]
    for scripts in (project_root / ".venv" / "Scripts", project_root / ".venv" / "bin"):
        candidates.append(scripts)
    for folder in candidates:
        for name in ("streamlink.exe", "streamlink"):
            p = folder / name
            if p.is_file():
                return str(p)
    # Chemin absolu uniquement : un nom nu serait résolu par CreateProcess
    # depuis le dossier de l'application avant le PATH (Windows).
    found = shutil.which("streamlink")
    if not found:
        logger.error("streamlink introuvable (venv et PATH)")
    return found or ""


# Cadence maximale de repaint d'une cellule de grille (macOS, backend rendu).
# 15 images/s suffisent pour une vignette et divisent par deux les passes de
# composition par rapport à la cadence native du flux.
_GRID_FRAME_INTERVAL = 1.0 / 15.0

_STREAMLINK = _find_streamlink()
# Limite de concurrence globale pour streamlink (évite les timeouts quand la grille charge 20 streams d'un coup)
_STREAMLINK_SEMAPHORE = threading.Semaphore(3)

# Signal audio de HypeWatcher. Le filtre astats coûte ~2,9 % d'un cœur et
# 12 threads PAR FLUX — soit +72 % d'un cœur et +300 threads sur une grille de
# 25. Désactivé par défaut ; ZLINK_AUDIO_SIGNAL=1 le réactive.
_AUDIO_SIGNAL = os.environ.get("ZLINK_AUDIO_SIGNAL") == "1"


# Tous les MpvWidget vivants. QApplication.quit() ne délivre PAS closeEvent aux
# widgets enfants : Qt détruit directement les fenêtres natives pendant que le
# thread de rendu de mpv y présente encore, ce qui provoque une erreur X fatale
# « BadWindow » sur l'extension Present. On termine donc les players nous-mêmes
# avant que Qt ne démonte quoi que ce soit.
# Inventaire des lecteurs vivants, pour le diagnostic. Il a servi un temps a
# tous les arreter en sortie : ce demontage groupe provoquait une avalanche
# d'erreurs Xlib fatales et corrompait le tas. On quitte desormais sans
# demonter — voir le commentaire d'arret dans main.py.
_LIVE_PLAYERS: "weakref.WeakSet[MpvWidget]" = weakref.WeakSet()


_MPV_LOG_PATH = pathlib.Path.home() / ".zlink" / "mpv.log"
_MPV_LOG_LOCK = threading.Lock()
# Plafond dur : un journal de diagnostic ne doit jamais remplir le disque.
# Au-delà, on repart d'un fichier vide plutôt que de croître sans fin.
_MPV_LOG_MAX_BYTES = 8 * 1024 * 1024


# Modules dont on veut le détail : cycle de vie de la fenêtre et du rendu.
# Tout le reste n'est retenu qu'à partir de « warn ».
_MPV_VERBOSE_PREFIXES = ("vo", "x11", "gpu", "cplayer", "video", "opengl", "egl")
_MPV_IMPORTANT_LEVELS = frozenset({"fatal", "error", "warn"})


#: Paramètres d'URL portant un jeton de lecture signé. Une URL HLS Twitch
#: complète vaut accès au flux : la retrouver dans un journal en clair, c'est
#: la donner à quiconque lit le fichier.
_SECRET_PARAMS = re.compile(
    r"([?&](?:dna|sig|token|Signature|Policy|Key-Pair-Id|hdnts)=)[^&\s\"']+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Retire les jetons signés d'une ligne de journal."""
    return _SECRET_PARAMS.sub(r"\1[expurgé]", text)


def _mpv_log_handler(widget: "MpvWidget"):
    """Écrit les messages mpv utiles dans un fichier, préfixés par la cellule.

    Un journal en « debug » intégral est inexploitable : le démultiplexeur HLS
    produit une ligne par segment, soit 570 000 lignes sur 646 000 dans un
    relevé réel — les URL noient ce qu'on cherche.
    """
    tag = "grille" if widget._grid_mode else "plein-écran"

    def _handle(level: str, prefix: str, text: str) -> None:
        lvl = (level or "").lower()
        mod = (prefix or "").split("/", 1)[0].lower()
        if lvl not in _MPV_IMPORTANT_LEVELS and mod not in _MPV_VERBOSE_PREFIXES:
            return
        line = (f"{time.strftime('%H:%M:%S')} [{tag}] {level} {prefix}: "
                f"{_redact(text.rstrip())}\n")
        try:
            with _MPV_LOG_LOCK:
                _MPV_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                if (_MPV_LOG_PATH.exists()
                        and _MPV_LOG_PATH.stat().st_size > _MPV_LOG_MAX_BYTES):
                    # On garde la fin, c'est elle qui précède un crash.
                    tail = _MPV_LOG_PATH.read_bytes()[-_MPV_LOG_MAX_BYTES // 2:]
                    _MPV_LOG_PATH.write_bytes(tail)
                nouveau = not _MPV_LOG_PATH.exists()
                with _MPV_LOG_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                if nouveau:
                    # Lisible par le seul propriétaire : même expurgé, ce
                    # journal décrit ce que la personne regarde.
                    os.chmod(_MPV_LOG_PATH, 0o600)
        except OSError:
            pass

    return _handle


def _grid_back_bytes(secs: int) -> int:
    """Plafond du tampon arrière pour `secs` secondes de flux de grille.

    Dimensionné sur le débit du palier le plus élevé qu'une grille puisse
    atteindre (720p60, ~3 Mbit/s) et non sur le palier courant : la qualité
    s'adapte en cours de route, et un plafond calculé pour du 160p tronquerait
    le clip dès que la grille repasse en haute qualité. Comme mpv ne retient
    que ce que le flux produit, ce plafond ne coûte rien aux paliers bas.
    """
    secs = max(15, min(180, int(secs)))
    return int(secs * 400_000)        # ~3,2 Mbit/s, avec de la marge


class MpvWidget(_MpvBase):  # type: ignore[misc,valid-type]
    """Widget hébergeant une instance python-mpv.

    Deux backends selon la plateforme, même API publique :

    - Windows (cible) : embed natif via `wid` (HWND), hwdec=d3d11va,
      gpu-api=d3d11, vo=gpu — mpv dessine lui-même dans la fenêtre.
    - macOS : `--wid` n'est pas supporté par mpv, donc vo=libmpv +
      MpvRenderContext, et les frames sont rendues dans le FBO du
      QOpenGLWidget (hwdec=videotoolbox).

    Si libmpv est absente, le widget fonctionne en mode dégradé.

    Args:
        grid_mode: True pour les cellules de grille (muet, faible latence, 480p).
                   False (défaut) pour le fullscreen (qualité maximale).
    """

    # Emitted (from any thread) the first time time-pos > 0 after each play() call.
    playback_started = pyqtSignal()
    # Émis quand mpv repasse au repos : fin de flux, coupure réseau, ou stream
    # terminé côté Twitch. L'API met souvent plusieurs minutes à basculer le
    # streamer en « offline », la cellule restait donc noire entre-temps.
    playback_ended = pyqtSignal()
    # Interne : libmpv signale une nouvelle frame depuis son thread de rendu ;
    # la connexion queued ramène le repaint sur le thread GUI.
    _frame_ready = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, grid_mode: bool = False, clip_buffer_secs: int = 90) -> None:
        super().__init__(parent)

        _LIVE_PLAYERS.add(self)
        self._grid_mode = grid_mode
        self._player: "_mpv_type.MPV | None" = None
        self._stop_flag: threading.Event | None = None
        self._time_pos_started: bool = False
        self._time_pos_cb = None
        self._render_ctx: object | None = None
        self._min_frame_interval: float = 0.0
        self._last_paint_ts: float = 0.0

        if not _MPV_AVAILABLE:
            return

        # libmpv exige LC_NUMERIC="C" : avec une locale à virgule décimale, mpv_create()
        # part en segfault. Normalement déjà réglé dans main.py, on le garantit ici pour
        # les widgets créés hors de ce chemin (tests, scripts).
        if locale.getlocale(locale.LC_NUMERIC)[0] not in (None, "C", "POSIX"):
            logger.warning(
                "LC_NUMERIC=%s incompatible avec libmpv — forcé à C",
                locale.getlocale(locale.LC_NUMERIC)[0],
            )
            locale.setlocale(locale.LC_NUMERIC, "C")

        # État audio VOULU, indépendant du lecteur : il survit à un changement
        # de flux, à une relance et à un changement de qualité.
        self._want_volume: int = 100
        self._want_muted: bool = grid_mode      # une cellule démarre muette

        mpv_kwargs: dict[str, object] = {
            "ytdl": False,
            "osc": False,
            # osc=False ne desactive QUE osc.lua, et load_scripts=False ne vise
            # que les scripts de l'utilisateur : mpv chargeait encore SIX
            # scripts internes par lecteur (console, stats, select, positioning,
            # commands, context-menu), soit six interpreteurs Lua et leurs
            # threads pour CHAQUE cellule de la grille. Aucun n'est atteignable
            # ici : ni OSC, ni raccourcis clavier, ni menu mpv.
            "load_scripts": False,
            "load_console": False,
            "load_stats_overlay": False,
            "load_select": False,
            "load_positioning": False,
            "load_commands": False,
            "load_context_menu": False,
            "load_auto_profiles": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            # Sans cela, la fenêtre X11 que mpv crée dans notre wid s'abonne à
            # ButtonPressMask et absorbe tous les clics : Qt n'en voit aucun et
            # le menu contextuel des cellules de la grille ne s'ouvre jamais.
            # Désabonnée, X propage les clics au widget parent.
            "input_cursor": False,
            "really_quiet": True,
        }
        if _RENDER_API:
            # Le contexte de rendu est créé dans initializeGL(), quand le
            # contexte OpenGL de Qt est courant.
            mpv_kwargs.update(vo="libmpv", hwdec="videotoolbox")
            # Chaque repaint d'une surface OpenGL déclenche une passe de
            # composition de la fenêtre : à 24 cellules et 30 fps par flux, le
            # coût est en dizaines de passes par seconde. Les vignettes sont
            # plafonnées, le plein écran garde la cadence native du flux.
            self._min_frame_interval = _GRID_FRAME_INTERVAL if grid_mode else 0.0
            self._frame_ready.connect(
                self._on_frame_ready, Qt.ConnectionType.QueuedConnection,
            )
            logger.debug("MpvWidget: backend rendu libmpv (grid=%s)", grid_mode)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
            if grid_mode:
                self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
            wid = int(self.winId())
            logger.debug("MpvWidget: wid=%d grid=%s", wid, grid_mode)
            if sys.platform.startswith("linux"):
                _plat = QApplication.platformName() if QApplication.instance() else ""
                if _plat and _plat != "xcb":
                    # Sur une plateforme sans fenêtre X11, winId() ne désigne
                    # rien de valide pour mpv — qui ouvre sa PROPRE connexion X
                    # et opère dessus. La moindre requête sur cet identifiant
                    # lève un BadWindow, fatal : le gestionnaire d'erreur Xlib
                    # par défaut termine le process. On rend donc le widget
                    # inerte plutôt que de prendre ce risque.
                    logger.error(
                        "Plateforme Qt '%s' : mpv n'implémente --wid que sur X11. "
                        "Lecture vidéo désactivée — relancer sous XWayland avec "
                        "QT_QPA_PLATFORM=xcb.",
                        _plat,
                    )
                    self._player = None
                    return
                # X11 : mpv gère --wid nativement (embed sans composition Qt).
                # hwdec auto-safe couvre vaapi (AMD/Intel) et nvdec.
                # gpu-context doit être EXPLICITE : en autodétection, mpv voit
                # WAYLAND_DISPLAY dans l'environnement et choisit son backend
                # Wayland même quand Qt tourne sous XWayland. Il ouvre alors sa
                # propre fenêtre au lieu de se greffer sur le wid X11 — c'est la
                # cause des flux qui s'affichent hors de l'application.
                mpv_kwargs.update(
                    wid=str(wid), hwdec="auto-safe", gpu_api="auto", vo="gpu",
                    gpu_context="x11egl",
                )
            else:
                # Windows : HWND + décodage D3D11 (plateforme cible).
                mpv_kwargs.update(
                    wid=str(wid), hwdec="d3d11va", gpu_api="d3d11", vo="gpu",
                )
        if grid_mode:
            mpv_kwargs.update(
                mute=True,
                demuxer_readahead_secs=2,
                cache_pause=False,
                # Le back-buffer du démuxeur n'était borné par RIEN : il croît
                # linéairement au débit du flux, sans éviction, et ni
                # demuxer-max-bytes ni demuxer-max-back-bytes ne l'arrêtaient.
                # Mesuré : 161 Mo sur une seule cellule en 6 minutes, soit
                # ~110 Mo/min pour 25 flux — ce qui explique les 8 Go de RSS
                # après trois quarts d'heure et la pression mémoire qui suit.
                # Tampon arrière PLAFONNÉ. Il était à zéro, ce qui rendait
                # impossible tout clip depuis la grille : dump-cache n'a alors
                # rien à écrire (vérifié — fichier de 0 octet). Le remettre sans
                # plafond ramènerait la fuite de 8,2 Go d'origine.
                # C'est bien un plafond, pas une réservation : mpv ne retient
                # que ce que le flux produit. Mesuré à 480p, 32 s de vidéo
                # pèsent 1,3 Mo — une cellule de grille, plus basse en qualité,
                # tient largement sous ce plafond.
                demuxer_max_back_bytes=(
                    _grid_back_bytes(clip_buffer_secs) if clip_buffer_secs > 0
                    else 0
                ),
            )
            if _AUDIO_SIGNAL:
                # Filtre ÉTIQUETÉ : la propriété n'existe que sous la forme
                # af-metadata/<label>. Coûteux (~2,9 % d'un cœur et 12 threads
                # par flux), d'où l'activation explicite.
                mpv_kwargs["af"] = (
                    "@zl:lavfi=[astats=metadata=1:reset=1:length=0.5]"
                )
        else:
            # Fullscreen : cache configurable pour permettre la sauvegarde de clips
            _buf = max(clip_buffer_secs + 30, 90)
            mpv_kwargs.update(
                demuxer_max_bytes="200MiB",
                demuxer_readahead_secs=_buf,
            )

        # Journal mpv optionnel : ZLINK_MPV_LOG=1 écrit les messages de chaque
        # player dans ~/.zlink/mpv.log. Sert à voir ce que faisait mpv juste
        # avant une erreur X « BadWindow », que le message X seul ne situe pas.
        if os.environ.get("ZLINK_MPV_LOG") == "1":
            mpv_kwargs["log_handler"] = _mpv_log_handler(self)
            # « v » suffit pour le cycle de vie fenêtre/VO ; « debug » ajoutait
            # surtout le détail des segments HLS.
            mpv_kwargs["loglevel"] = "v"
            mpv_kwargs.pop("really_quiet", None)

        try:
            self._player = _mpv_module.MPV(**mpv_kwargs)  # type: ignore[union-attr]
            _x11_guard.install()
            # C'est à la CONFIGURATION DE L'AFFICHAGE que mpv pose son propre
            # gestionnaire d'erreur X — pas à la construction. On le reprend au
            # moment précis où il vient d'être remplacé, plutôt que d'attendre
            # le prochain passage du chien de garde.
            try:
                self._player.observe_property(
                    "vo-configured", self._on_vo_configured)
            except Exception as exc:      # noqa: BLE001 — garde-fou d'agrément
                logger.debug("MpvWidget: vo-configured non observable — %s", exc)
        except Exception as exc:
            logger.error("MpvWidget: impossible d'initialiser MPV — %s", exc)
            self._player = None

        if self._player is not None:
            def _on_time_pos(name: str, value: object) -> None:  # noqa: ANN001
                if value is not None and float(value) > 0 and not self._time_pos_started:
                    self._time_pos_started = True
                    self.playback_started.emit()
                    # mpv émet cette propriété à CHAQUE image : sur 25 cellules
                    # cela faisait ~7 500 appels Python par seconde, et autant
                    # de prises du GIL en concurrence avec le thread graphique,
                    # alors qu'on ne cherche que la première image. On se
                    # désabonne ; play_stream se réabonne au flux suivant.
                    self._unobserve_time_pos()
            self._time_pos_cb = _on_time_pos
            self._player.observe_property("time-pos", _on_time_pos)

            def _on_idle(name: str, value: object) -> None:  # noqa: ANN001
                # Ne signaler que la fin d'une lecture RÉELLE : mpv est aussi au
                # repos avant le premier play, et on ne veut pas vider une
                # cellule qui n'a jamais commencé.
                if value and self._time_pos_started:
                    self._time_pos_started = False
                    self.playback_ended.emit()
            self._player.observe_property("idle-active", _on_idle)

    # -- backend rendu (macOS) ------------------------------------------------

    def _on_frame_ready(self) -> None:
        """Demande un repaint, au plus une fois par intervalle configuré."""
        if self._min_frame_interval > 0.0:
            now = time.monotonic()
            if now - self._last_paint_ts < self._min_frame_interval:
                return
            self._last_paint_ts = now
        self.update()

    @property
    def uses_render_backend(self) -> bool:
        """True quand la vidéo est rendue par nos soins (macOS), pas par mpv."""
        return _RENDER_API and self._render_ctx is not None

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        # Sans repaint explicite, le FBO garde la frame de l'ancienne taille :
        # après un retour de PiP l'image reste figée ou noire.
        super().resizeEvent(event)
        if _RENDER_API:
            self.update()

    def initializeGL(self) -> None:  # type: ignore[override]
        """Crée le contexte de rendu libmpv une fois le contexte OpenGL courant."""
        if not _RENDER_API or self._player is None or self._render_ctx is not None:
            return
        try:
            self._render_ctx = _mpv_module.MpvRenderContext(  # type: ignore[union-attr]
                self._player,
                "opengl",
                opengl_init_params={
                    "get_proc_address": _mpv_module.MpvGlGetProcAddressFn(_gl_proc_address),  # type: ignore[union-attr]
                },
            )
            self._render_ctx.update_cb = self._frame_ready.emit  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("MpvWidget: contexte de rendu indisponible — %s", exc)
            self._render_ctx = None

    def paintGL(self) -> None:  # type: ignore[override]
        """Dessine la frame courante dans le FBO du widget."""
        if self._render_ctx is None:
            return
        ratio = self.devicePixelRatioF()
        try:
            self._render_ctx.render(  # type: ignore[attr-defined]
                flip_y=True,
                opengl_fbo={
                    "w": int(self.width() * ratio),
                    "h": int(self.height() * ratio),
                    "fbo": self.defaultFramebufferObject(),
                },
            )
        except Exception as exc:
            logger.error("MpvWidget.paintGL: %s", exc)

    # -- public API -----------------------------------------------------------

    def play(self, url: str) -> None:
        """Lance la lecture d'une URL HTTP directe (fullscreen via StreamManager)."""
        if self._player is None:
            logger.warning("MpvWidget.play: MPV non disponible")
            return
        # L'URL HLS porte un jeton de lecture signé : ne logger que l'hôte.
        logger.debug("MpvWidget.play: %s", urllib.parse.urlparse(url).netloc)
        self._time_pos_started = False
        self._player.play(url)
        self._reapply_audio()

    def play_stream(self, twitch_login: str, quality: str = "360p30,160p30,worst") -> None:  # noqa: D401
        """Résout l'URL streamlink en arrière-plan puis lance la lecture.

        Prévu pour les cellules de grille. Non bloquant.
        """
        self.stop()  # annule toute résolution en cours
        # Nouveau flux : on remet l'observateur pour en détecter la 1re image.
        self._time_pos_started = False
        self._observe_time_pos()
        if self._player is None:
            logger.warning("MpvWidget.play_stream: MPV non disponible")
            return

        if not _STREAMLINK:
            logger.error("play_stream(%s): streamlink introuvable", twitch_login)
            return

        stop_flag = threading.Event()
        self._stop_flag = stop_flag
        player = self._player

        def _worker() -> None:
            try:
                with _STREAMLINK_SEMAPHORE:
                    if stop_flag.is_set():
                        return
                    result = subprocess.run(
                        [_STREAMLINK, f"twitch.tv/{twitch_login}",
                         safe_quality(quality, "360p30,160p30,worst"),
                         "--stream-url", "--twitch-disable-ads"],
                        capture_output=True, text=True, timeout=25,
                    )
                if stop_flag.is_set():
                    return
                url = result.stdout.strip()
                if result.returncode != 0 or not url:
                    logger.error(
                        "play_stream(%s): rc=%d — %s",
                        twitch_login, result.returncode, result.stderr.strip()[:120],
                    )
                    return
                if not stop_flag.is_set():
                    self._time_pos_started = False
                    player.play(url)
                    self._reapply_audio()
                    logger.info("MpvWidget: grille — lecture démarrée pour %s", twitch_login)
            except FileNotFoundError:
                logger.error("play_stream: streamlink introuvable (%s)", _STREAMLINK)
            except subprocess.TimeoutExpired:
                logger.error("play_stream(%s): timeout streamlink", twitch_login)
            except Exception as exc:
                logger.error("play_stream(%s): %s", twitch_login, exc)

        threading.Thread(target=_worker, daemon=True).start()

    def _unobserve_time_pos(self) -> None:
        """Retire l'observateur d'image, s'il est posé. Idempotent."""
        cb = getattr(self, "_time_pos_cb", None)
        if cb is None or self._player is None:
            return
        self._time_pos_cb = None
        try:
            self._player.unobserve_property("time-pos", cb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MpvWidget: désabonnement time-pos — %s", exc)

    def _observe_time_pos(self) -> None:
        """Repose l'observateur pour détecter la première image du flux suivant."""
        if self._player is None or getattr(self, "_time_pos_cb", None) is not None:
            return

        def _cb(name: str, value: object) -> None:  # noqa: ANN001
            if value is not None and float(value) > 0 and not self._time_pos_started:
                self._time_pos_started = True
                self.playback_started.emit()
                self._unobserve_time_pos()

        self._time_pos_cb = _cb
        try:
            self._player.observe_property("time-pos", _cb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MpvWidget: observation time-pos — %s", exc)
            self._time_pos_cb = None

    def stop(self) -> None:
        """Stoppe la lecture et annule toute résolution streamlink en cours."""
        self._time_pos_started = False
        if self._stop_flag is not None:
            self._stop_flag.set()
            self._stop_flag = None
        if self._player is not None:
            # Arrêter la lecture démonte l'affichage : même exposition qu'un
            # terminate, donc même précaution.
            _x11_guard.install()
            try:
                self._player.stop()
            except Exception:
                pass

    def set_mute(self, muted: bool) -> None:
        self._want_muted = bool(muted)
        if self._player is not None:
            self._player.mute = self._want_muted

    def set_volume(self, volume: int) -> None:
        """Volume entre 0 et 100."""
        self._want_volume = max(0, min(100, int(volume)))
        if self._player is not None:
            self._player.volume = self._want_volume

    @staticmethod
    def _on_vo_configured(_nom: str, valeur: object) -> None:
        """L'affichage vient d'être (re)configuré : reprendre le gestionnaire X."""
        if valeur:
            _x11_guard.install()

    def _reapply_audio(self) -> None:
        """Réimpose l'état audio voulu après un (re)démarrage de lecture.

        Le volume et la coupure sont des propriétés du LECTEUR, mais rien ne
        garantit qu'elles survivent au chargement d'un nouveau flux — et de
        fait, relancer une cellule ramenait le son à fond alors que la console
        affichait toujours le réglage précédent. Le widget garde donc l'état
        voulu et le repose lui-même.
        """
        if self._player is None:
            return
        try:
            self._player.volume = self._want_volume
            self._player.mute = self._want_muted
        except Exception as exc:      # noqa: BLE001 — réglage d'agrément
            logger.debug("MpvWidget: état audio non réappliqué — %s", exc)

    def save_clip(self, secs: int = 60, directory: str = "") -> str | None:
        """Écrit les `secs` dernières secondes dans `directory` (~/Videos/ZLink/).

        S'appuie sur `dump-cache`, qui puise dans le TAMPON ARRIÈRE du démuxeur :
        sans lui la commande produit un fichier vide, quelle que soit la valeur
        de readahead. Voir `demuxer_max_back_bytes` à la construction.

        Non bloquant — mpv écrit en arrière-plan. Retourne le chemin visé, ou
        None si la sauvegarde n'a pas pu être lancée.
        """
        if self._player is None:
            return None
        try:
            import datetime
            pos = self._player.time_pos
            if pos is None:
                return None
            start = max(0.0, float(pos) - secs)
            end   = float(pos)
            clip_dir = pathlib.Path(directory) if directory else pathlib.Path.home() / "Videos" / "ZLink"
            clip_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output = clip_dir / f"clip_{ts}.ts"
            self._player.command("dump-cache", start, end, str(output))
            logger.info("Clip sauvegardé : %s", output)
            return str(output)
        except Exception as exc:
            logger.error("save_clip: %s", exc)
            return None

    def set_clip_buffer(self, secs: int) -> None:
        """Ajuste le tampon arrière sans relancer la lecture.

        `demuxer-max-back-bytes` est modifiable à chaud (vérifié) : activer les
        clips depuis les réglages prend effet tout de suite, sans couper les
        vingt-cinq flux.
        """
        if self._player is None:
            return
        try:
            self._player.demuxer_max_back_bytes = (
                _grid_back_bytes(secs) if secs > 0 else 0
            )
        except Exception as exc:      # noqa: BLE001 — réglage d'agrément
            logger.debug("MpvWidget: tampon de clip non ajusté — %s", exc)

    def get_audio_rms_db(self) -> float | None:
        """Retourne le niveau RMS audio en dBFS lu via le filtre astats.

        Disponible seulement si ZLINK_AUDIO_SIGNAL=1 (le filtre astats coûte
        cher : ~2,9 % d'un cœur et 12 threads par flux).
        Retourne None si non disponible (pas de filtre, pas de lecture, silence pur).
        Thread-safe — libmpv gère l'accès concurrent aux propriétés.
        """
        if not (_AUDIO_SIGNAL and self._grid_mode) or self._player is None:
            return None
        try:
            # player["af-metadata"] lisait une OPTION (python-mpv préfixe par
            # « options/ »), pas une propriété — et la propriété n'existe que
            # sous la forme af-metadata/<label>. Les deux erreurs cumulées
            # faisaient que cette fonction renvoyait toujours None.
            meta = self._player._get_property("af-metadata/zl")
            if not meta:
                return None
            rms_str = meta.get("lavfi.astats.Overall.RMS_level", "")
            if not rms_str or rms_str == "-inf":
                return None
            return float(rms_str)
        except Exception:
            return None

    @property
    def is_playing(self) -> bool:
        if self._player is None:
            return False
        return not self._player.idle_active

    # -- cleanup --------------------------------------------------------------

    def shutdown(self) -> None:
        """Libère le contexte de rendu et termine le player. Idempotent."""
        # Ordre important : le contexte de rendu référence le player.
        if self._render_ctx is not None:
            try:
                self._render_ctx.free()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("MpvWidget: libération du contexte de rendu — %s", exc)
            self._render_ctx = None
        if self._player is not None:
            player, self._player = self._player, None
            # AVANT le terminate, et c'est tout l'intérêt : mpv installe SON
            # gestionnaire d'erreur Xlib dès le début de la lecture (vérifié),
            # et détruit sa fenêtre alors que des requêtes Present sont encore
            # en vol. Le BadWindow qui s'ensuit terminerait le processus depuis
            # un thread de rendu. On reprend la main juste avant, et de nouveau
            # après, car mpv laisse le gestionnaire par défaut derrière lui.
            _x11_guard.install()
            try:
                player.terminate()
            except Exception as exc:
                logger.debug("MpvWidget: terminate — %s", exc)
            _x11_guard.install()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)
