"""Widget MPV réutilisable — embed libmpv dans un QWidget via wid (HWND)."""

from __future__ import annotations

import logging
import os
import pathlib
import locale
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import mpv as _mpv_type

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QOpenGLContext
from PyQt6.QtWidgets import QWidget

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

logger = logging.getLogger(__name__)

if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
    logger.warning(
        "Session Wayland détectée : mpv n'implémente --wid que sur X11, la vidéo "
        "s'ouvrirait dans des fenêtres séparées. Lancer avec QT_QPA_PLATFORM=xcb "
        "pour passer par XWayland."
    )

# Import paresseux : mpv.py lève OSError au module-level si libmpv-2.dll est absent.
# Sur Windows, ctypes.util.find_library cherche dans os.environ["PATH"].
# os.add_dll_directory() est aussi nécessaire pour Python 3.8+.
_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
_path_entries = os.environ.get("PATH", "").split(os.pathsep)
if _PROJECT_ROOT not in _path_entries:
    os.environ["PATH"] = _PROJECT_ROOT + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(_PROJECT_ROOT)  # type: ignore[attr-defined]
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
    # Interne : libmpv signale une nouvelle frame depuis son thread de rendu ;
    # la connexion queued ramène le repaint sur le thread GUI.
    _frame_ready = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, grid_mode: bool = False, clip_buffer_secs: int = 90) -> None:
        super().__init__(parent)

        self._grid_mode = grid_mode
        self._player: "_mpv_type.MPV | None" = None
        self._stop_flag: threading.Event | None = None
        self._time_pos_started: bool = False
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

        mpv_kwargs: dict[str, object] = {
            "ytdl": False,
            "osc": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
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
                # X11 : mpv gère --wid nativement (embed sans composition Qt).
                # hwdec auto-safe couvre vaapi (AMD/Intel) et nvdec.
                mpv_kwargs.update(
                    wid=str(wid), hwdec="auto-safe", gpu_api="auto", vo="gpu",
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
                # Filtre audio silencieux : décode l'audio sans le jouer,
                # expose le niveau RMS via la propriété af-metadata.
                af="lavfi=[astats=metadata=1:reset=1:length=0.5]",
            )
        else:
            # Fullscreen : cache configurable pour permettre la sauvegarde de clips
            _buf = max(clip_buffer_secs + 30, 90)
            mpv_kwargs.update(
                demuxer_max_bytes="200MiB",
                demuxer_readahead_secs=_buf,
            )

        try:
            self._player = _mpv_module.MPV(**mpv_kwargs)  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("MpvWidget: impossible d'initialiser MPV — %s", exc)
            self._player = None

        if self._player is not None:
            def _on_time_pos(name: str, value: object) -> None:  # noqa: ANN001
                if value is not None and float(value) > 0 and not self._time_pos_started:
                    self._time_pos_started = True
                    self.playback_started.emit()
            self._player.observe_property("time-pos", _on_time_pos)

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

    def play_stream(self, twitch_login: str, quality: str = "360p,worst") -> None:  # noqa: D401
        """Résout l'URL streamlink en arrière-plan puis lance la lecture.

        Prévu pour les cellules de grille. Non bloquant.
        """
        self.stop()  # annule toute résolution en cours
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
                         safe_quality(quality, "360p,worst"),
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
                    logger.info("MpvWidget: grille — lecture démarrée pour %s", twitch_login)
            except FileNotFoundError:
                logger.error("play_stream: streamlink introuvable (%s)", _STREAMLINK)
            except subprocess.TimeoutExpired:
                logger.error("play_stream(%s): timeout streamlink", twitch_login)
            except Exception as exc:
                logger.error("play_stream(%s): %s", twitch_login, exc)

        threading.Thread(target=_worker, daemon=True).start()

    def stop(self) -> None:
        """Stoppe la lecture et annule toute résolution streamlink en cours."""
        self._time_pos_started = False
        if self._stop_flag is not None:
            self._stop_flag.set()
            self._stop_flag = None
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    def set_mute(self, muted: bool) -> None:
        if self._player is not None:
            self._player.mute = muted

    def set_volume(self, volume: int) -> None:
        """Volume entre 0 et 100."""
        if self._player is not None:
            self._player.volume = max(0, min(100, volume))

    def save_clip(self, secs: int = 60, directory: str = "") -> str | None:
        """Sauvegarde les dernières `secs` secondes dans `directory` (défaut ~/Videos/ZLink/).

        Utilise dump-cache MPV — nécessite demuxer_readahead_secs >= secs.
        Non bloquant : MPV écrit le fichier en arrière-plan.
        Retourne le chemin du fichier créé, ou None en cas d'échec.
        """
        if self._player is None or self._grid_mode:
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

    def get_audio_rms_db(self) -> float | None:
        """Retourne le niveau RMS audio en dBFS lu via le filtre astats.

        Disponible uniquement en grid_mode (filtre astats activé).
        Retourne None si non disponible (pas de filtre, pas de lecture, silence pur).
        Thread-safe — libmpv gère l'accès concurrent aux propriétés.
        """
        if not self._grid_mode or self._player is None:
            return None
        try:
            meta: dict | None = self._player["af-metadata"]
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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # Ordre important : le contexte de rendu référence le player.
        if self._render_ctx is not None:
            try:
                self._render_ctx.free()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("MpvWidget: libération du contexte de rendu — %s", exc)
            self._render_ctx = None
        if self._player is not None:
            self._player.terminate()
        super().closeEvent(event)
