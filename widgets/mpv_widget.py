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
import ctypes
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

from core.stream_manager import QUALITY_GRID, safe_quality
from core.sous_processus import sans_fenetre

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


def _hwdec_linux() -> str:
    """Priorité de décodage matériel sous Linux.

    `auto-safe` fait essayer nvdec à mpv, donc un `dlopen("libcuda.so.1")` qui
    échoue sur toute machine AMD ou Intel. Le message « Cannot load
    libcuda.so.1 » que ffmpeg écrit alors part DIRECTEMENT sur la sortie
    d'erreur, hors du journal de mpv : vérifié, ni `really-quiet` ni
    `msg-level=ffmpeg=no` ne l'attrapent. Une ligne par cellule, vingt-cinq
    cellules, et de nouveau à chaque reprise.

    Et pour rien : sur ces machines mpv retenait `vaapi` de toute façon, ce
    que la liste ci-dessous demande directement. `no` ferme la liste — sans
    lui, une machine sans VA-API du tout n'aurait plus de repli logiciel.

    nvdec n'est écarté que quand la bibliothèque est RÉELLEMENT absente : sur
    une machine NVIDIA, `auto-safe` reste le bon choix, et c'est le même
    dlopen que ffmpeg qui tranche.
    """
    try:
        ctypes.CDLL("libcuda.so.1")
    except OSError:
        return "vaapi,vaapi-copy,no"
    return "auto-safe"


#: Résolu une fois : vingt-cinq cellules ne doivent pas rouvrir la question.
_HWDEC_LINUX = _hwdec_linux() if sys.platform.startswith("linux") else "auto-safe"

#: Option ffmpeg qui rapproche le démarrage du bord du direct. Le démuxeur HLS
#: s'ouvre par défaut trois segments en arrière (`live_start_index=-3`), soit
#: six secondes de retard sur les segments Twitch, qui durent deux secondes.
#:
#: DEUX, et non un. `-1` démarre sur le dernier segment publié — celui que
#: Twitch est encore en train d'écrire : essayé, le flux part trop tôt et
#: bafouille. Le segment gardé en réserve est le coussin qui absorbe les
#: à-coups du réseau ; il coûte deux secondes et rend les deux autres.
#:
#: Ce n'est PAS `--twitch-low-latency`. Ce drapeau ne règle que le lecteur HLS
#: de streamlink (TwitchHLSStreamReader), et ZLink n'appelle streamlink qu'avec
#: `--stream-url` : il imprime l'URL de la playlist puis s'arrête, c'est mpv
#: qui lit le flux. Le drapeau n'aurait donc rien changé. Les segments
#: `EXT-X-TWITCH-PREFETCH`, eux, restent hors de portée — ffmpeg ignore ces
#: balises propres à Twitch.
_LATENCE_BASSE = "live_start_index=-2"

_STREAMLINK = _find_streamlink()
# Limite de concurrence globale pour streamlink (évite les timeouts quand la grille charge 20 streams d'un coup)
_STREAMLINK_SEMAPHORE = threading.Semaphore(3)

#: Qualité demandée par défaut pour une cellule de grille, et repli quand la
#: valeur configurée est refusée. Empruntée à stream_manager plutôt que
#: recopiée : deux échelles qui divergent, c'est une moitié des chaînes qui
#: échoue sans qu'on sache laquelle des deux est en cause.
QUALITE_GRILLE = QUALITY_GRID

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


#: Dernier horodatage rendu, et le rang de sa réutilisation. Deux dump-cache
#: peuvent tomber dans la même milliseconde — le replay du plein écran en
#: lance justement deux coup sur coup.
_dernier_horodatage: tuple[str, int] = ("", 0)


def _horodatage_de_clip() -> str:
    """Un horodatage lisible, et unique même répété dans la milliseconde.

    Le nom ne tenait qu'à la seconde : le second dump visait le MÊME fichier
    et écrasait le premier pendant que mpv y écrivait encore. Le replay lisait
    alors un tronçon corrompu, ou repartait chercher chez Twitch en croyant
    qu'aucun tampon local ne valait mieux.
    """
    global _dernier_horodatage

    import datetime

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    precedent, rang = _dernier_horodatage
    rang = rang + 1 if ts == precedent else 0
    _dernier_horodatage = (ts, rang)
    return ts if rang == 0 else f"{ts}-{rang}"


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
    # La résolution streamlink a échoué : ni URL, ni lecture à attendre. Sans
    # ce signal, l'appelant ne distinguait pas une résolution qui piétine d'une
    # résolution morte, et la cellule tournait indéfiniment sur son anneau de
    # chargement.
    resolution_failed = pyqtSignal(str)      # twitch_login
    # Une URL a été obtenue et remise à mpv : à partir d'ici, et seulement
    # ici, il est légitime d'attendre une première image dans un délai borné.
    playback_requested = pyqtSignal(str)     # twitch_login
    # Interne : libmpv signale une nouvelle frame depuis son thread de rendu ;
    # la connexion queued ramène le repaint sur le thread GUI.
    _frame_ready = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *,
                 grid_mode: bool = False, clip_buffer_secs: int = 90,
                 low_latency: bool = False) -> None:
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
        self._low_latency: bool = bool(low_latency)

        if not _MPV_AVAILABLE:
            return

        self._garantir_locale_c()

        # État audio VOULU, indépendant du lecteur : il survit à un changement
        # de flux, à une relance et à un changement de qualité.
        self._want_volume: int = 100
        self._want_muted: bool = grid_mode      # une cellule démarre muette

        mpv_kwargs = self._options_de_base()
        if not self._appliquer_options_affichage(mpv_kwargs, grid_mode):
            return
        self._appliquer_options_tampon(mpv_kwargs, grid_mode, clip_buffer_secs)
        self._appliquer_options_latence(mpv_kwargs)

        self._appliquer_options_journal(mpv_kwargs)

        self._creer_lecteur(mpv_kwargs)

        self._brancher_observateurs()

    @staticmethod
    def _garantir_locale_c() -> None:
        """LC_NUMERIC=C, sans quoi mpv_create() part en segfault.

        Normalement déjà réglé par main.py ; garanti ici pour les widgets
        créés hors de ce chemin (tests, scripts).
        """
        # libmpv exige LC_NUMERIC="C" : avec une locale à virgule décimale, mpv_create()
        # part en segfault. Normalement déjà réglé dans main.py, on le garantit ici pour
        # les widgets créés hors de ce chemin (tests, scripts).
        if locale.getlocale(locale.LC_NUMERIC)[0] not in (None, "C", "POSIX"):
            logger.warning(
                "LC_NUMERIC=%s incompatible avec libmpv — forcé à C",
                locale.getlocale(locale.LC_NUMERIC)[0],
            )
            locale.setlocale(locale.LC_NUMERIC, "C")

    @staticmethod
    def _options_de_base() -> dict:
        """Options communes à tous les lecteurs, avant spécialisation."""
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
            # Corollaire de la ligne au-dessus, et pas un réglage de confort.
            # mpv cache le pointeur au bout d'une seconde d'immobilité
            # (cursor-autohide vaut 1000 par défaut) et ne le réaffiche qu'en
            # voyant la souris bouger — or `input_cursor: False` lui retire
            # justement ces événements. Le pointeur disparaissait donc sur la
            # vidéo une seconde après l'avoir lâchée, et ne revenait JAMAIS.
            #
            # C'est ZLink qui décide de ce qui se montre par-dessus le flux :
            # la barre du bas apparaît au mouvement de souris, et viser ses
            # boutons sans voir le pointeur n'est pas possible.
            "cursor_autohide": "no",
            "really_quiet": True,
        }
        return mpv_kwargs

    def _appliquer_options_affichage(self, mpv_kwargs: dict,
                                     grid_mode: bool) -> bool:
        """Choix du backend d'affichage : rendu libmpv ou embarquement natif.

        Renvoie False quand la plateforme ne permet aucun affichage sûr —
        l'appelant laisse alors le widget inerte plutôt que de risquer un
        BadWindow fatal.
        """
        if _RENDER_API:
            self._appliquer_rendu_libmpv(mpv_kwargs, grid_mode)
            return True
        return self._appliquer_embarquement(mpv_kwargs, grid_mode)

    def _appliquer_rendu_libmpv(self, mpv_kwargs: dict, grid_mode: bool) -> None:
        """macOS : c'est nous qui dessinons les images dans le FBO du widget.

        mpv n'implémente --wid que sur X11, win32 et Android ; ailleurs il
        ouvrirait sa propre fenêtre.
        """
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

    def _appliquer_embarquement(self, mpv_kwargs: dict, grid_mode: bool) -> bool:
        """X11 ou Windows : mpv dessine lui-même dans notre fenêtre native.

        Renvoie False quand la plateforme Qt ne fournit pas de fenêtre X11
        exploitable — le widget reste alors inerte.
        """
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
                return False
            # X11 : mpv gère --wid nativement (embed sans composition Qt).
            # hwdec : auto-safe sur machine NVIDIA, VA-API seule ailleurs —
            # voir _hwdec_linux(), qui existe pour une raison bruyante.
            # gpu-context doit être EXPLICITE : en autodétection, mpv voit
            # WAYLAND_DISPLAY dans l'environnement et choisit son backend
            # Wayland même quand Qt tourne sous XWayland. Il ouvre alors sa
            # propre fenêtre au lieu de se greffer sur le wid X11 — c'est la
            # cause des flux qui s'affichent hors de l'application.
            mpv_kwargs.update(
                wid=str(wid), hwdec=_HWDEC_LINUX, gpu_api="auto", vo="gpu",
                gpu_context="x11egl",
            )
        else:
            # Windows : HWND + décodage D3D11 (plateforme cible).
            mpv_kwargs.update(
                wid=str(wid), hwdec="d3d11va", gpu_api="d3d11", vo="gpu",
            )
        return True

    @staticmethod
    def _appliquer_options_tampon(mpv_kwargs: dict, grid_mode: bool,
                                  clip_buffer_secs: int) -> None:
        """Taille des tampons du démuxeur, très différente selon l'usage."""
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

    def _appliquer_options_latence(self, mpv_kwargs: dict) -> None:
        """Démarrage au bord du direct, quand la basse latence est demandée.

        Rien à poser dans le cas contraire : l'absence de `demuxer-lavf-o`
        laisse ffmpeg sur son `live_start_index=-3`, la marge d'origine.
        """
        if self._low_latency:
            mpv_kwargs["demuxer_lavf_o"] = _LATENCE_BASSE

    def _appliquer_options_journal(self, mpv_kwargs: dict) -> None:
        """Journal mpv optionnel, activé par ZLINK_MPV_LOG=1."""
        # Journal mpv optionnel : ZLINK_MPV_LOG=1 écrit les messages de chaque
        # player dans ~/.zlink/mpv.log. Sert à voir ce que faisait mpv juste
        # avant une erreur X « BadWindow », que le message X seul ne situe pas.
        if os.environ.get("ZLINK_MPV_LOG") == "1":
            mpv_kwargs["log_handler"] = _mpv_log_handler(self)
            # « v » suffit pour le cycle de vie fenêtre/VO ; « debug » ajoutait
            # surtout le détail des segments HLS.
            mpv_kwargs["loglevel"] = "v"
            mpv_kwargs.pop("really_quiet", None)

    def _creer_lecteur(self, mpv_kwargs: dict) -> None:
        """Instancie MPV et reprend la main sur le gestionnaire d'erreur X."""
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
        except Exception:
            logger.exception("MpvWidget: impossible d'initialiser MPV")
            self._player = None

    def _brancher_observateurs(self) -> None:
        """Suivi de time-pos et idle-active : début et fin de lecture réelle."""
        if self._player is None:
            return
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
        except Exception:
            logger.exception("MpvWidget: contexte de rendu indisponible")
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
        except Exception:
            logger.exception("MpvWidget.paintGL")

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

    def play_stream(self, twitch_login: str,
                    quality: str = QUALITE_GRILLE) -> None:  # noqa: D401
        """Résout l'URL streamlink en arrière-plan puis lance la lecture.

        Prévu pour les cellules de grille. Non bloquant.
        """
        self.stop()  # annule toute résolution en cours
        # Nouveau flux : on remet l'observateur pour en détecter la 1re image.
        self._time_pos_started = False
        self._observe_time_pos()
        if self._player is None:
            logger.warning("MpvWidget.play_stream: MPV non disponible")
            self.resolution_failed.emit(twitch_login)
            return

        if not _STREAMLINK:
            logger.error("play_stream(%s): streamlink introuvable", twitch_login)
            self.resolution_failed.emit(twitch_login)
            return

        stop_flag = threading.Event()
        self._stop_flag = stop_flag
        threading.Thread(
            target=self._resoudre_et_jouer,
            args=(twitch_login, quality, stop_flag, self._player),
            daemon=True,
        ).start()

    def _resoudre_et_jouer(self, twitch_login: str, quality: str,
                           stop_flag: threading.Event, player) -> None:
        """Thread : résout l'URL puis lance la lecture sur `player`.

        Le lecteur est passé en argument plutôt que relu sur self : la cellule
        peut être démontée pendant la résolution, et on jouerait alors sur un
        lecteur déjà remplacé.
        """
        try:
            url = self._url_streamlink(twitch_login, quality, stop_flag)
            if stop_flag.is_set():
                return          # cellule réaffectée : ni échec ni lecture
            if url is None:
                self.resolution_failed.emit(twitch_login)
                return
            self._time_pos_started = False
            player.play(url)
            self._reapply_audio()
            self.playback_requested.emit(twitch_login)
            logger.info("MpvWidget: grille — lecture démarrée pour %s", twitch_login)
        except FileNotFoundError:
            logger.error("play_stream: streamlink introuvable (%s)", _STREAMLINK)
            self.resolution_failed.emit(twitch_login)
        except subprocess.TimeoutExpired:
            logger.error("play_stream(%s): timeout streamlink", twitch_login)
            self.resolution_failed.emit(twitch_login)
        except Exception:
            logger.exception("play_stream(%s)", twitch_login)
            self.resolution_failed.emit(twitch_login)

    @staticmethod
    def _url_streamlink(twitch_login: str, quality: str,
                        stop_flag: threading.Event) -> str | None:
        """URL du flux résolue par streamlink, ou None si annulé ou en échec.

        Le drapeau d'annulation est relu avant ET après l'appel : la résolution
        peut durer plusieurs secondes, et une cellule qui change de chaîne
        entre-temps ne doit pas voir l'ancien flux démarrer.
        """
        with _STREAMLINK_SEMAPHORE:
            if stop_flag.is_set():
                return None
            result = subprocess.run(
                [_STREAMLINK, f"twitch.tv/{twitch_login}",
                 safe_quality(quality, QUALITE_GRILLE),
                 "--stream-url", "--twitch-disable-ads"],
                capture_output=True, text=True, timeout=25,
                **sans_fenetre(),
            )
        if stop_flag.is_set():
            return None
        url = result.stdout.strip()
        if result.returncode != 0 or not url:
            # Le code de retour seul ne dit rien quand streamlink sort en
            # silence : on joint la QUALITÉ demandée — une qualité absente de
            # la chaîne est le motif d'échec le plus courant — et les deux
            # flux, stdout compris, puisque l'erreur y atterrit parfois.
            logger.error(
                "play_stream(%s): rc=%d qualite=%r"
                " | stderr: %s | stdout: %s",
                twitch_login, result.returncode,
                safe_quality(quality, QUALITE_GRILLE),
                (result.stderr or "").strip()[:300] or "(vide)",
                (result.stdout or "").strip()[:200] or "(vide)",
            )
            return None
        return url

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

    # ── transport : position, pause, recherche ──────────────────────────
    #
    # Réservé aux médias de durée CONNUE — un clip, un replay enregistré. Un
    # flux Twitch repris en direct annonce la durée depuis le début de
    # l'émission, soit des heures : une barre de progression bâtie dessus
    # afficherait 99 % dès la première image.

    # `position()` existe déjà plus bas, et rend None quand mpv ne sait pas
    # encore : on ne la redéfinit pas ici. Une seconde définition, plus haut
    # dans la classe, était silencieusement écrasée par la première — le
    # lecteur de clips recevait donc None là où il attendait un flottant.

    def duree(self) -> float:
        """Durée du média, en secondes. Zéro tant qu'elle n'est pas connue."""
        return self._propriete("duration")

    def _propriete(self, nom: str) -> float:
        """Une propriété numérique de mpv, ou zéro.

        mpv lève dès que la propriété n'est pas encore disponible — entre le
        `play()` et la première image, elles le sont toutes. Un lecteur qui
        remonterait l'exception ne survivrait pas à sa propre ouverture.
        """
        if self._player is None:
            return 0.0
        try:
            valeur = getattr(self._player, nom.replace("-", "_"))
        except Exception:                                  # noqa: BLE001
            return 0.0
        try:
            return max(0.0, float(valeur))
        except (TypeError, ValueError):
            return 0.0

    def chercher(self, secondes: float) -> None:
        """Se place à cet instant, en absolu."""
        if self._player is None:
            return
        try:
            self._player.seek(max(0.0, float(secondes)), reference="absolute")
        except Exception as exc:                           # noqa: BLE001
            logger.debug("MpvWidget: recherche impossible — %s", exc)

    def set_pause(self, en_pause: bool) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = bool(en_pause)
        except Exception as exc:                           # noqa: BLE001
            logger.debug("MpvWidget: pause impossible — %s", exc)

    def en_pause(self) -> bool:
        if self._player is None:
            return False
        try:
            return bool(self._player.pause)
        except Exception:                                  # noqa: BLE001
            return False

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
            from core.paths import CLIPS_DEFAUT
            clip_dir = pathlib.Path(directory) if directory else CLIPS_DEFAUT
            clip_dir.mkdir(parents=True, exist_ok=True)
            output = clip_dir / f"clip_{_horodatage_de_clip()}.ts"
            self._player.command("dump-cache", start, end, str(output))
            logger.info("Clip sauvegardé : %s", output)
            return str(output)
        except Exception:
            logger.exception("save_clip")
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

    def set_low_latency(self, on: bool) -> None:
        """Active ou coupe le démarrage au bord du direct.

        Contrairement au tampon de clip, `demuxer-lavf-o` n'est lu qu'à
        L'OUVERTURE d'un flux : le changement ne touche pas la lecture en
        cours, il prend effet à la suivante — reprise, changement de chaîne
        ou de qualité. Les cellules de grille non encore créées, elles,
        reçoivent la valeur par le constructeur.
        """
        on = bool(on)
        if on == self._low_latency:
            return
        self._low_latency = on
        if self._player is None:
            return
        try:
            self._player.demuxer_lavf_o = _LATENCE_BASSE if on else ""
        except Exception as exc:      # noqa: BLE001 — réglage d'agrément
            logger.debug("MpvWidget: basse latence non appliquée — %s", exc)

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

    def position(self) -> float | None:
        """Position de lecture en secondes, ou None si indisponible.

        Sur un MP4 fragmenté repris chez Twitch, cette valeur part de
        l'horodatage ABSOLU du direct (des dizaines de milliers de secondes) :
        elle ne vaut que comparée à elle-même, jamais rapportée à `duration`.
        """
        if self._player is None:
            return None
        try:
            pos = self._player.time_pos
        except Exception:      # noqa: BLE001 — lecture en cours de démontage
            return None
        return float(pos) if pos is not None else None

    def restant(self) -> float | None:
        """Secondes de lecture restantes, ou None si indisponible.

        `duration` ne peut pas servir ici : sur un fragment repris chez Twitch
        il porte l'horodatage absolu du direct. `time-remaining` est un ÉCART,
        juste dans les deux cas — c'est lui qui, ajouté à la position, donne
        la vraie longueur du morceau.
        """
        if self._player is None:
            return None
        try:
            reste = self._player.time_remaining
        except Exception:      # noqa: BLE001 — lecture en cours de démontage
            return None
        return float(reste) if reste is not None else None

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
