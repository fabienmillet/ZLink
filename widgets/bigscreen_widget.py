# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""BigScreenWidget — tableau de bord plein écran style ZEvent live."""

from __future__ import annotations

import logging
import math
import os
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QObject,
    QRectF,
    QSize,
    Qt,
    QTime,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtGui import (
    QBitmap,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRegion,
)


if TYPE_CHECKING:
    from core.api_client import GlobalStats, GoalWithStreamer, StreamerInfo

_AVATAR_CACHE_DIR = pathlib.Path.home() / ".zlink" / "avatars"
_AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Fragments de feuille de style et police repetes.
_POLICE_UI = "Segoe UI"
_FOND_TRANSPARENT = "background: transparent;"
_TEXTE_VERT_SANS_BORDURE = "color: #00ff87; background: transparent; border: none;"

_GREEN = QColor("#00ff87")
_BG = QColor("#0a0a0a")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _circle_pixmap(px: QPixmap, size: int) -> QPixmap:
    """Rogne un QPixmap en cercle de `size` px."""
    scaled = px.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    src_x = (scaled.width() - size) // 2
    src_y = (scaled.height() - size) // 2
    cropped = scaled.copy(src_x, src_y, size, size)

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    mask = QBitmap(size, size)
    mask.fill(Qt.GlobalColor.color0)
    mp = QPainter(mask)
    mp.setRenderHint(QPainter.RenderHint.Antialiasing)
    mp.fillRect(0, 0, size, size, Qt.GlobalColor.color0)
    mp.setBrush(Qt.GlobalColor.color1)
    mp.drawEllipse(0, 0, size, size)
    mp.end()
    painter.setClipRegion(QRegion(mask))
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return result


def _load_avatar_pixmap(login: str, size: int) -> QPixmap | None:
    """Charge depuis le cache disque et retourne un QPixmap rond ou None."""
    cache_path = _AVATAR_CACHE_DIR / f"{login}.png"
    if cache_path.exists():
        px = QPixmap(str(cache_path))
        if not px.isNull():
            return _circle_pixmap(px, size)
    return None


def _initials_pixmap(login: str, display: str, size: int) -> QPixmap:
    """Fabrique un QPixmap rond avec les initiales en fallback."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor("#1a1a1a")))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size, size)
    text = (display[:1] if display else login[:1]).upper()
    font_size = max(8, size // 3)
    painter.setFont(QFont("Consolas", font_size, QFont.Weight.Bold))
    painter.setPen(QPen(QColor("#555555")))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return px


def _grayscale_pixmap(px: QPixmap, size: int) -> QPixmap:
    """Retourne un QPixmap en niveaux de gris, alpha préservé."""
    if size <= 0:
        return QPixmap()
    src = px.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    # Format_Grayscale8 DÉTRUIT le canal alpha : le fond transparent des logos
    # détourés devenait du noir pur, indiscernable du fond de la mosaïque. On
    # restitue donc l'alpha d'origine par composition DestinationIn.
    gray = src.convertToFormat(
        QImage.Format.Format_Grayscale8
    ).convertToFormat(QImage.Format.Format_ARGB32)
    mask = QPainter(gray)
    mask.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    mask.drawImage(0, 0, src)
    mask.end()

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    # 0.30 ici, cumulé au 0.55 du rendu, donnait 0.165 d'opacité effective :
    # les avatars sombres se confondaient avec le fond et passaient pour
    # « manquants ». On remonte, le rendu module le reste.
    painter.setOpacity(0.55)
    gray_px = QPixmap.fromImage(gray)
    if gray_px.size() != result.size():
        # Sans mise à l'échelle, une source plus petite laisserait le reste vide.
        gray_px = gray_px.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
    painter.drawPixmap(0, 0, gray_px)
    painter.end()
    return result


def _square_pixmap(px: QPixmap, size: int) -> QPixmap:
    """Rogne un QPixmap en carré de `size` px (sans distorsion)."""
    scaled = px.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    src_x = (scaled.width() - size) // 2
    src_y = (scaled.height() - size) // 2
    return scaled.copy(src_x, src_y, size, size)


def _load_square_avatar_pixmap(login: str, size: int) -> QPixmap | None:
    """Charge depuis le cache disque et retourne un QPixmap carré ou None."""
    cache_path = _AVATAR_CACHE_DIR / f"{login}.png"
    if cache_path.exists():
        px = QPixmap(str(cache_path))
        if not px.isNull():
            return _square_pixmap(px, size)
    return None


def _initials_square_pixmap(login: str, display: str, size: int) -> QPixmap:
    """Fabrique un QPixmap carré avec les initiales en fallback."""
    px = QPixmap(size, size)
    px.fill(QColor("#1a1a1a"))
    painter = QPainter(px)
    text = (display[:1] if display else login[:1]).upper()
    font_size = max(8, size // 3)
    painter.setFont(QFont("Consolas", font_size, QFont.Weight.Bold))
    painter.setPen(QPen(QColor("#333333")))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return px


from core import avatar_cache as _avatar_disk

logger = logging.getLogger(__name__)

# ── Instrumentation de cadence ───────────────────────────────────────────────
# Activée par ZLINK_PERF=1, sinon strictement inerte (un test de booléen par
# image). Sert à savoir si un widget dépasse son budget, ou si c'est la boucle
# d'événements qui est engorgée par autre chose — les deux ne se corrigent pas
# au même endroit.
_PERF = os.environ.get("ZLINK_PERF") == "1"


class _Cadence:
    """Durées de peinture et retards de réveil, résumés toutes les 5 s."""

    def __init__(self, nom: str, budget_ms: float) -> None:
        self._nom = nom
        self._budget = budget_ms
        self._peintures: list[float] = []
        self._retards: list[float] = []
        self._t_resume = time.perf_counter()
        self._attendu = 0.0

    def reveil(self) -> None:
        """Appelé au début du tick : mesure le retard du timer."""
        now = time.perf_counter()
        if self._attendu:
            self._retards.append((now - self._attendu) * 1000.0)
        self._attendu = now + self._budget / 1000.0

    def peinture(self, ms: float) -> None:
        self._peintures.append(ms)
        if time.perf_counter() - self._t_resume >= 5.0:
            self._resumer()

    def _resumer(self) -> None:
        def stats(xs: list[float]) -> str:
            if not xs:
                return "n/a"
            tri = sorted(xs)
            moy = sum(tri) / len(tri)
            return (f"moy {moy:5.2f}  med {tri[len(tri)//2]:5.2f}  "
                    f"p95 {tri[int(len(tri) * 0.95)]:6.2f}  max {tri[-1]:6.2f}")

        depasse = sum(1 for x in self._peintures if x > self._budget)
        logger.warning(
            "PERF %-9s %3d images en 5 s (%4.1f fps) | peinture ms: %s | "
            "retard du reveil ms: %s | %d image(s) au-dessus du budget de %.0f ms",
            self._nom, len(self._peintures), len(self._peintures) / 5.0,
            stats(self._peintures), stats(self._retards), depasse, self._budget,
        )
        self._peintures.clear()
        self._retards.clear()
        self._t_resume = time.perf_counter()


_CAD_MOSAIQUE = _Cadence("mosaique", 50.0)
_CAD_TICKER = _Cadence("ticker", 16.0)

def _download_avatar(login: str, url: str) -> None:
    """Délègue au cache partagé — voir core/avatar_cache.

    Cette fonction avait sa propre copie du téléchargement, concurrente de
    celle de data_manager : les deux tiraient la même image en parallèle.
    """
    _avatar_disk.download(login, url)


# ---------------------------------------------------------------------------
# AvatarPixmapCache — chargement async centralisé
# ---------------------------------------------------------------------------

# Pool borné pour le chargement des avatars. La version précédente créait un
# thread PAR CLÉ, depuis paintEvent : 367 threads en 1,9 s au démarrage, et
# jusqu'à 301 threads vivants en permanence quand des avatars sont
# introuvables (relance par threading.Timer sans fin).
_AVATAR_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="avatar")


#: Vrai une fois le pool fermé. Un rafraîchissement Qt déjà en file d'attente
#: peut encore demander un avatar APRÈS la fermeture : submit() lève alors
#: RuntimeError et la trace remonte jusqu'à l'utilisateur, pendant qu'on quitte.
_POOL_CLOSED = False


def shutdown_avatar_pool() -> None:
    """Ferme le pool sans attendre les téléchargements en cours.

    Ses threads sont NON-DAEMON : sans cet appel, l'interpréteur les joint à la
    sortie et un téléchargement lent retarderait la fermeture.
    """
    global _POOL_CLOSED
    _POOL_CLOSED = True
    _AVATAR_POOL.shutdown(wait=False, cancel_futures=True)


def _submit_avatar(fn, *args) -> bool:
    """Programme un chargement. Faux si le pool est fermé — on quitte."""
    if _POOL_CLOSED:
        return False
    try:
        _AVATAR_POOL.submit(fn, *args)
        return True
    except RuntimeError:
        # Course : la fermeture a eu lieu entre le test et l'envoi.
        return False


class _GuiDispatcher(QObject):
    """Rejoue un appelable sur le thread GUI.

    QTimer.singleShot(0, cb) appelé depuis un thread Python ordinaire crée le
    timer sur CE thread, qui n'a pas de boucle d'événements : le callback n'est
    jamais exécuté. Le pixmap arrivait donc bien en cache mémoire, mais aucun
    label n'en était informé — d'où les avatars restés en initiales.
    Un signal en connexion queued, lui, est délivré sur le thread d'affinité de
    l'objet, ici celui où le dispatcher a été construit (le thread GUI).
    """

    _call = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        try:
            fn()
        except RuntimeError:
            pass  # widget détruit entre-temps
        except Exception:
            # PyQt appelle qFatal() sur une exception non rattrapée dans un
            # slot : sans ce filet, un callback fautif abat l'application.
            logger.exception("Callback avatar en échec")

    def post(self, fn: object) -> None:
        self._call.emit(fn)


_gui_dispatcher: "_GuiDispatcher | None" = None


def _ensure_dispatcher() -> None:
    """Construit le dispatcher. À n'appeler QUE depuis le thread GUI."""
    global _gui_dispatcher
    if _gui_dispatcher is None:
        _gui_dispatcher = _GuiDispatcher()


def _post_to_gui(fn: object) -> None:
    """Fait exécuter `fn` sur le thread GUI, depuis n'importe quel thread."""
    d = _gui_dispatcher
    if d is not None:
        d.post(fn)
    else:
        # Surtout pas de QTimer.singleShot ici : depuis un thread sans boucle
        # d'événements il ne se déclenche jamais, ce qui est précisément le bug
        # que le dispatcher corrige. Mieux vaut le signaler que le taire.
        logger.error("Dispatcher GUI absent — callback avatar abandonné")


class _AvatarPixmapCache:
    """Charge les pixmaps ronds depuis le cache disque en background."""

    def __init__(self) -> None:
        # Placeholders « initiales » mémorisés : ils étaient recréés à CHAQUE
        # appel, donc leur identité changeait, donc le gris dérivé n'était
        # jamais mis en cache et _grayscale_pixmap repartait de zéro à chaque
        # image — 20 des 23 ms par frame de la mosaïque.
        self._placeholders: dict[str, QPixmap] = {}
        self._cache: dict[str, QPixmap] = {}               # key → pixmap couleur
        self._gray: dict[str, QPixmap] = {}                # key → pixmap gris
        # key -> (profile_url tente, instant avant lequel on ne retente pas).
        # Une expiration DATEE remplace le threading.Timer par cle : avec 300
        # avatars introuvables, ces minuteries faisaient a elles seules 300
        # threads vivants en permanence.
        self._loading: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, list] = {}                # key → liste de callbacks en attente

    def get(self, login: str, display: str, size: int,
            callback: "None | callable" = None,
            profile_url: str = "") -> QPixmap:
        """Retourne le pixmap si disponible, sinon déclenche le chargement.

        `callback()` sera appelé sur le main thread quand le pixmap est prêt.
        Plusieurs callbacks peuvent être enregistrés pour la même clé : tous
        seront appelés à la fin du chargement, même si un thread était déjà
        en cours (cas rebuild du RemoteMenu).
        """
        key = f"{login}@{size}"
        if key in self._cache:
            return self._cache[key]
        if callback is not None:
            lst = self._pending.setdefault(key, [])
            # Dédoublonnage : paintEvent réenregistre self.update à chaque frame
            # tant que l'avatar n'est pas résolu. Sans cela _pending enfle de
            # milliers d'entrées et leur réveil simultané fige le thread GUI.
            if callback not in lst:
                lst.append(callback)
            if key in self._cache:
                # Le chargement s'est terminé pendant l'enregistrement : notre
                # callback vient d'atterrir dans une liste déjà vidée et ne
                # serait jamais rappelé.
                for cb in self._pending.pop(key, []):
                    _post_to_gui(cb)
                return self._cache[key]
        entry = self._loading.get(key)   # None = jamais tenté
        now = time.monotonic()
        if entry is None or (entry[0] == "" and profile_url) or now >= entry[1]:
            self._loading[key] = (profile_url, float("inf"))
            _ensure_dispatcher()
            _submit_avatar(self._load, login, display, size, key, profile_url)
        return self._placeholder(f"{login}@{size}", login, display, size, False)

    def get_gray(self, login: str, display: str, size: int,
                 callback: "None | callable" = None,
                 profile_url: str = "") -> QPixmap:
        color = self.get(login, display, size, callback, profile_url)
        gkey = f"{login}@{size}:gray"
        if gkey in self._gray:
            return self._gray[gkey]
        if color is self._placeholders.get(f"{login}@{size}"):
            cached = self._gray.get(gkey + ":ph")
            if cached is not None:
                return cached
        gray = _grayscale_pixmap(color, size)
        # On compare l'IDENTITÉ, pas la présence de la clé : si le chargement
        # s'est terminé pendant le calcul du gris, la clé est présente alors que
        # `color` est encore le placeholder — tester la présence figerait les
        # initiales pour toujours.
        if self._cache.get(f"{login}@{size}") is color:
            self._gray[gkey] = gray
        elif color is self._placeholders.get(f"{login}@{size}"):
            # Gris du placeholder : mémorisé sous une clé distincte, purgée
            # par _load() quand le vrai avatar arrive.
            self._gray[gkey + ":ph"] = gray
        return gray

    def _placeholder(self, key: str, login: str, display: str,
                     size: int, square: bool) -> QPixmap:
        """Pixmap d'initiales stable pour une clé donnée."""
        px = self._placeholders.get(key)
        if px is None:
            px = (_initials_square_pixmap(login, display, size) if square
                  else _initials_pixmap(login, display, size))
            self._placeholders[key] = px
        return px

    def _load(self, login: str, display: str, size: int,
              key: str, profile_url: str = "") -> None:
        px = _load_avatar_pixmap(login, size)
        if px is None and profile_url:
            _download_avatar(login, profile_url)
            px = _load_avatar_pixmap(login, size)
        if px is None:
            # Retry: 5s si l'URL a échoué, 2s si pas d'URL (le fichier peut arriver via bg)
            # Echec : on autorise une nouvelle tentative apres un delai, sans
            # creer de thread — la date d'expiration est relue par get()/get_sq().
            delay = 5.0 if profile_url else 2.0
            if self._loading.get(key, ("", 0.0))[0] == profile_url:
                self._loading[key] = (profile_url, time.monotonic() + delay)
            return
        self._cache[key] = px
        self._gray.pop(f"{login}@{size}:gray", None)  # invalide le gris
        self._gray.pop(f"{login}@{size}:gray:ph", None)
        self._placeholders.pop(f"{login}@{size}", None)
        for cb in self._pending.pop(key, []):
            _post_to_gui(cb)

    def get_sq(self, login: str, display: str, size: int,
               callback: "None | callable" = None,
               profile_url: str = "") -> QPixmap:
        """Retourne un QPixmap carré (non rogné en cercle)."""
        key = f"{login}@{size}sq"
        if key in self._cache:
            return self._cache[key]
        if callback is not None:
            lst = self._pending.setdefault(key, [])
            if callback not in lst:  # cf. get() : anti-tempête de callbacks
                lst.append(callback)
            if key in self._cache:   # cf. get() : course enregistrement/drain
                for cb in self._pending.pop(key, []):
                    _post_to_gui(cb)
                return self._cache[key]
        entry = self._loading.get(key)
        now = time.monotonic()
        if entry is None or (entry[0] == "" and profile_url) or now >= entry[1]:
            self._loading[key] = (profile_url, float("inf"))
            _ensure_dispatcher()
            _submit_avatar(self._load_sq, login, display, size, key, profile_url)
        return self._placeholder(f"{login}@{size}sq", login, display, size, True)

    def get_gray_sq(self, login: str, display: str, size: int,
                    callback: "None | callable" = None,
                    profile_url: str = "") -> QPixmap:
        color = self.get_sq(login, display, size, callback, profile_url)
        gkey = f"{login}@{size}sq:gray"
        if gkey in self._gray:
            return self._gray[gkey]
        if color is self._placeholders.get(f"{login}@{size}sq"):
            cached = self._gray.get(gkey + ":ph")
            if cached is not None:
                return cached
        gray = _grayscale_pixmap(color, size)
        # Même course que dans get_gray : voir le commentaire là-bas.
        if self._cache.get(f"{login}@{size}sq") is color:
            self._gray[gkey] = gray
        elif color is self._placeholders.get(f"{login}@{size}sq"):
            self._gray[gkey + ":ph"] = gray
        return gray

    def _load_sq(self, login: str, display: str, size: int,
                 key: str, profile_url: str = "") -> None:
        px = _load_square_avatar_pixmap(login, size)
        if px is None and profile_url:
            _download_avatar(login, profile_url)
            px = _load_square_avatar_pixmap(login, size)
        if px is None:
            # Echec : on autorise une nouvelle tentative apres un delai, sans
            # creer de thread — la date d'expiration est relue par get()/get_sq().
            delay = 5.0 if profile_url else 2.0
            if self._loading.get(key, ("", 0.0))[0] == profile_url:
                self._loading[key] = (profile_url, time.monotonic() + delay)
            return
        self._cache[key] = px
        self._gray.pop(f"{login}@{size}sq:gray", None)
        self._gray.pop(f"{login}@{size}sq:gray:ph", None)
        self._placeholders.pop(f"{login}@{size}sq", None)
        for cb in self._pending.pop(key, []):
            _post_to_gui(cb)


_avatar_cache = _AvatarPixmapCache()


def load_avatar_into_label(
    label: "QLabel",
    login: str,
    display: str,
    size: int,
    profile_url: str,
) -> None:
    """Charge l'avatar de `login` dans `label` via le cache partagé.

    Si déjà en mémoire : application immédiate.
    Sinon : asynchrone — le callback met à jour le label quand le téléchargement termine.
    """
    key = f"{login}@{size}"

    def _apply() -> None:
        try:
            cached = _avatar_cache._cache.get(key)
            if cached is not None:
                label.setPixmap(cached)
        except RuntimeError:
            pass  # widget supprimé avant l'application

    # Cas 1 : déjà en cache mémoire → application immédiate
    if key in _avatar_cache._cache:
        try:
            label.setPixmap(_avatar_cache._cache[key])
        except RuntimeError:
            pass
        return

    # Cas 2 : pas en mémoire → chargement async (disque d'abord, téléchargement si absent)
    # _AvatarPixmapCache._load() vérifie le disque avant de télécharger → jamais de I/O
    # bloquant sur le thread principal
    _avatar_cache.get(login, display, size, _apply, profile_url)


# ---------------------------------------------------------------------------
# Composant 1 — Ticker scrollant horizontal (haut, 56px)
# ---------------------------------------------------------------------------

class _TickerWidget(QWidget):
    """Cartes horizontales défilantes — uniquement les streamers en live."""

    _ITEM_W = 240   # largeur d'une carte
    _ITEM_H = 56    # hauteur fixe
    _SPEED = 50     # px/s
    _AVATAR = 40    # taille de l'avatar

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._ITEM_H)
        self._streamers: list[StreamerInfo] = []  # seulement les live
        self._offset: float = 0.0
        self._last_ms: int = 0

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60fps
        self._timer.timeout.connect(self._tick)
        # Démarré par showEvent : inutile de tourner tant que le
        # widget n'est pas affiché.

        import time
        self._last_ms = int(time.monotonic() * 1000)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # 62 réveils par seconde pour un widget invisible.
        super().hideEvent(event)
        self._timer.stop()

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        # Uniquement les streamers en live, tri alphabétique
        self._streamers = sorted(
            [s for s in streamers if s.online],
            key=lambda s: s.twitch_login.lower(),
        )
        self.update()

    def _tick(self) -> None:
        if _PERF:
            _CAD_TICKER.reveil()
        now = int(time.monotonic() * 1000)
        dt = now - self._last_ms
        self._last_ms = now
        if not self._streamers:
            return
        total_w = len(self._streamers) * self._ITEM_W
        self._offset = (self._offset + self._SPEED * dt / 1000.0) % total_w
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        _t0 = time.perf_counter() if _PERF else 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = self.width()
        h = self.height()

        # Fond opaque
        painter.fillRect(0, 0, w, h, QColor("#0d0d0d"))

        # Séparateur bas
        painter.setPen(QPen(QColor("#1e1e1e")))
        painter.drawLine(0, h - 1, w, h - 1)

        if not self._streamers:
            painter.end()
            if _PERF:
                _CAD_TICKER.peinture((time.perf_counter() - _t0) * 1000.0)
            return

        n = len(self._streamers)
        total_w = n * self._ITEM_W
        reps = math.ceil((w + total_w) / total_w) + 1

        for rep in range(reps):
            base_x = rep * total_w - int(self._offset)
            for i, s in enumerate(self._streamers):
                x = base_x + i * self._ITEM_W
                if x + self._ITEM_W < 0 or x > w:
                    continue
                self._draw_card(painter, s, x, h)

        painter.end()
        if _PERF:
            _CAD_TICKER.peinture((time.perf_counter() - _t0) * 1000.0)

    def _draw_card(self, painter: QPainter, s: "StreamerInfo", x: int, h: int) -> None:
        size = self._AVATAR
        av_y = (h - size) // 2
        av_x = x + 8

        # Avatar cercle
        px = _avatar_cache.get(s.twitch_login, s.display, size, self.update,
                               getattr(s, "profile_url", ""))
        painter.setOpacity(1.0)
        painter.drawPixmap(av_x, av_y, px)

        # Colonne texte
        text_x = av_x + size + 8
        text_w = self._ITEM_W - size - 8 - 8 - 8  # marges gauche, gap, marge droite

        # Nom
        name_font = QFont(_POLICE_UI, 11, QFont.Weight.Bold)
        painter.setFont(name_font)
        painter.setPen(QPen(QColor("#ffffff")))
        name = QFontMetrics(name_font).elidedText(
            s.display, Qt.TextElideMode.ElideRight, text_w
        )
        painter.drawText(
            QRect(text_x, 6, text_w, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )

        # Jeu
        game_font = QFont(_POLICE_UI, 9)
        painter.setFont(game_font)
        painter.setPen(QPen(QColor("#888888")))
        game = s.game or "en live"
        game_text = f"joue à {game}" if s.game else "en live"
        game_text = QFontMetrics(game_font).elidedText(
            game_text, Qt.TextElideMode.ElideRight, text_w
        )
        painter.drawText(
            QRect(text_x, 28, text_w, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            game_text,
        )

        # Séparateur vertical droit
        painter.setPen(QPen(QColor("#1e1e1e")))
        painter.drawLine(x + self._ITEM_W - 1, 8, x + self._ITEM_W - 1, h - 8)


# ---------------------------------------------------------------------------
# Composant 2 — Fond avatars 3D défilant
# ---------------------------------------------------------------------------

class _BgAvatarsWidget(QWidget):
    """Grille d'avatars carrés défilant du bas vers le haut en boucle."""

    _COLS = 10          # colonnes
    _AVATAR_SIZE = 96   # taille du cache (px) — affiché dynamiquement
    _GAP = 2            # séparateur minimal entre images
    # 60 images par seconde demandées. La cadence RÉELLEMENT obtenue sera plus
    # basse : le vidage du backing store plein écran bloque ~14 ms par image
    # (mesuré), ce qui plafonne autour de 30 à 35 fps. Viser 60 laisse la
    # mosaïque prendre toute la marge disponible au lieu de s'auto-limiter,
    # et le défilement étant exprimé en px/s, sa vitesse ne bouge pas d'un
    # poste à l'autre.
    _FPS_INTERVAL = 16
    # En pixels PAR SECONDE, et non par image : la vitesse ne doit pas dépendre
    # de la cadence réellement obtenue. Avec l'ancien pas par image, la mosaïque
    # ralentissait dès que la machine était chargée — 37 px/s mesurés au lieu
    # des 45 attendus.
    _SCROLL_PX_S = 45.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._streamers: list[StreamerInfo] = []
        self._offset: float = 0.0
        self._last_tick: float = 0.0
        self._prewarm_idx: int = 0
        self._prewarm_cell: int = 0
        self._prewarm_gen: int = 0

        self._timer = QTimer(self)
        self._timer.setInterval(self._FPS_INTERVAL)
        self._timer.timeout.connect(self._tick)
        # Démarré par showEvent : inutile de tourner tant que le
        # widget n'est pas affiché.

    _PREWARM_BATCH = 24   # avatars demandés par salve

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        # tri alphabétique uniquement — pas de regroupement par état live
        self._streamers = sorted(streamers, key=lambda s: s.twitch_login.lower())
        # _prewarm_cell n'est PAS remis à zéro : la taille de cellule n'a pas
        # changé, seule la liste. La réinitialiser forcerait un balayage complet
        # à chaque rafraîchissement périodique.
        self._prewarm_idx = 0
        self._restart_prewarm()
        self.update()

    def showEvent(self, event) -> None:  # type: ignore[override]
        # Qt ne délivre pas resizeEvent à un widget caché : la vraie taille
        # n'est connue qu'ici.
        super().showEvent(event)
        self._timer.start()
        self._restart_prewarm()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # Le Big Screen est construit puis masqué : son timer à 30 fps tournait
        # pendant toute la vie de l'application sans rien afficher.
        super().hideEvent(event)
        self._timer.stop()

    def _restart_prewarm(self) -> None:
        """Invalide les chaînes de préchargement en cours et en lance une seule.

        Sans jeton de génération, chaque set_streamers et chaque resize
        démarrait une chaîne supplémentaire sans arrêter les précédentes : elles
        s'empilaient et multipliaient le débit de threads que _PREWARM_BATCH
        cherche justement à contenir.
        """
        self._prewarm_gen += 1
        self._prewarm(self._prewarm_gen)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        # Les pixmaps sont mis en cache par taille de cellule. Si la fenêtre
        # change de largeur après le préchargement (cas normal : les données
        # arrivent avant que la fenêtre ait sa taille finale), les clés
        # préchauffées ne sont plus celles demandées au rendu et chaque
        # cellule repart en chargement à la demande — d'où les rangées de
        # lettres. On relance donc le préchargement à la nouvelle taille.
        super().resizeEvent(event)
        if self.width() // self._COLS != self._prewarm_cell:
            self._prewarm_idx = 0
            self._restart_prewarm()

    def _prewarm(self, gen: int = 0) -> None:
        """Précharge les avatars par petites salves.

        Sans cela, une cellule ne demande son avatar qu'au moment d'être peinte
        et affiche ses initiales le temps du chargement : la mosaïque montrait
        des rangées de lettres au fur et à mesure du défilement. Les salves
        évitent de lancer un thread par streamer d'un seul coup.
        """
        if gen != self._prewarm_gen:
            return  # chaîne périmée, une plus récente a pris le relais
        if not self.isVisible():
            # Le Big Screen est construit puis caché : tant qu'il l'est, sa
            # taille est celle par défaut et préchauffer remplirait le cache de
            # pixmaps à une taille jamais affichée. showEvent réamorcera.
            return
        w = self.width()
        cell_w = w // self._COLS if w else 0
        if cell_w <= 0:
            # Pas encore dimensionné : on retente au prochain tour.
            QTimer.singleShot(200, lambda g=gen: self._prewarm(g))
            return
        # Après le calcul de cell_w, pour que le cas « pas encore dimensionné »
        # soit reprogrammé même sans streamers : sans quoi un préchauffage
        # démarré trop tôt s'arrêterait là et ne reprendrait jamais.
        if not self._streamers:
            return
        if cell_w != self._prewarm_cell:
            # Nouvelle taille de cellule : on repart du début.
            self._prewarm_cell = cell_w
            self._prewarm_idx = 0
        batch = self._streamers[self._prewarm_idx:self._prewarm_idx + self._PREWARM_BATCH]
        if not batch:
            return
        for s in batch:
            purl = getattr(s, "profile_url", "")
            if s.online:
                _avatar_cache.get_sq(s.twitch_login, s.display, cell_w, None, purl)
            else:
                _avatar_cache.get_gray_sq(s.twitch_login, s.display, cell_w, None, purl)
        self._prewarm_idx += len(batch)
        QTimer.singleShot(200, lambda g=gen: self._prewarm(g))

    def _tick(self) -> None:
        if _PERF:
            _CAD_MOSAIQUE.reveil()
        if not self._streamers:
            return
        w = self.width()
        if w == 0:
            return
        now = time.monotonic()
        # Premier tick, ou reprise après masquage : on prend une image nominale
        # plutôt qu'un delta de plusieurs secondes qui ferait sauter la mosaïque.
        dt = now - self._last_tick if self._last_tick else self._FPS_INTERVAL / 1000.0
        self._last_tick = now
        dt = min(dt, 0.25)

        cell_w = w // self._COLS
        cell_h = cell_w + self._GAP
        n_rows = max(1, math.ceil(len(self._streamers) / self._COLS))
        total_h = n_rows * cell_h
        self._offset = (self._offset + self._SCROLL_PX_S * dt) % total_h
        self.update()

    def _cellules_visibles(self, rows_needed: int, start_row: int, n_rows: int,
                           n: int, cell_w: int, cell_h: int, row_frac: float,
                           h: int) -> tuple:
        """(en ligne, hors ligne) : positions et pixmaps des cellules a peindre.

        Deux listes plutot qu'une : peindre les hors-ligne d'un bloc ne change
        l'opacite que deux fois par image, au lieu d'une fois par cellule.

        Les pixmaps sont demandes a la taille EXACTE de la cellule : le cache
        les stocke deja mis a l'echelle, donc drawPixmap fait une copie 1:1 au
        lieu de redimensionner chaque avatar a chaque image.
        """
        online: list[tuple[int, int, "QPixmap"]] = []
        offline: list[tuple[int, int, "QPixmap"]] = []
        for visible_row in range(rows_needed):
            cy = int(visible_row * cell_h - row_frac)
            if cy + cell_w < 0 or cy > h:
                continue
            data_row = (start_row + visible_row) % n_rows
            for col in range(self._COLS):
                s = self._streamers[(data_row * self._COLS + col) % n]
                purl = getattr(s, "profile_url", "")
                cible = online if s.online else offline
                lire = _avatar_cache.get_sq if s.online else _avatar_cache.get_gray_sq
                cible.append((col * cell_w, cy, lire(
                    s.twitch_login, s.display, cell_w, self.update, purl)))
        return online, offline

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if not self._streamers:
            return
        _t0 = time.perf_counter() if _PERF else 0.0

        painter = QPainter(self)
        # Aucun redimensionnement ni forme arrondie ici : les indices de rendu
        # lissé ne feraient que coûter du temps par image.

        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, QBrush(_BG))

        n = len(self._streamers)

        # Cellules carrées qui couvrent toute la largeur, images qui se touchent
        cell_w = w // self._COLS
        if cell_w <= 0:
            # 0 < w < _COLS : les transformations d'image renverraient un
            # pixmap nul, mis en cache comme un succès et jamais réparé.
            painter.end()
            return
        cell_h = cell_w + self._GAP
        n_rows = math.ceil(n / self._COLS)

        # Scroll vers le haut : offset augmente, les lignes montent
        row_frac = self._offset % cell_h
        start_row = int(self._offset / cell_h) % n_rows
        rows_needed = math.ceil(h / cell_h) + 2

        # Les pixmaps sont demandés à la taille exacte de la cellule : le cache
        # les stocke déjà mis à l'échelle, donc drawPixmap fait une copie 1:1
        # au lieu de redimensionner chaque avatar à chaque image.
        # Deux passes (en ligne puis hors ligne) pour ne changer l'opacité que
        # deux fois par image au lieu d'une fois par cellule.
        online, offline = self._cellules_visibles(
            rows_needed, start_row, n_rows, n, cell_w, cell_h, row_frac, h)

        painter.setOpacity(1.0)
        for cx, cy, px in online:
            painter.drawPixmap(cx, cy, px)
        # 0.35 sur un fond quasi noir rendait les avatars hors ligne
        # indiscernables du vide : on remonte juste assez pour les lire sans
        # qu'ils concurrencent les streamers en live.
        painter.setOpacity(0.85)
        for cx, cy, px in offline:
            painter.drawPixmap(cx, cy, px)
        painter.setOpacity(1.0)

        # Dégradé en haut pour fondu avec le ticker (56 px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, 80)
        grad.setColorAt(0.0, QColor(10, 10, 10, 220))
        grad.setColorAt(1.0, QColor(10, 10, 10, 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(0, 0, w, 80)

        painter.end()
        if _PERF:
            _CAD_MOSAIQUE.peinture((time.perf_counter() - _t0) * 1000.0)


# ---------------------------------------------------------------------------
# Composant 3a — Odometer (chiffres qui défilent comme un compteur)
# ---------------------------------------------------------------------------

class _Digit(QWidget):
    """Un seul chiffre animé — défile verticalement de 0..9 + espace/€."""

    _CHARS = "0123456789 €\u202f\u00a0,"

    def __init__(self, font: "QFont", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = font
        fm = QFontMetrics(font)
        self._cell_w = fm.horizontalAdvance("0") + 4
        self._cell_h = fm.height() + 4
        self.setFixedSize(self._cell_w, self._cell_h)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._current_char = " "
        self._from_char = " "
        self._offset: float = 0.0  # 0.0 = at rest, -1.0 = one cell up (next visible)
        self._anim: QPropertyAnimation | None = None

    # ── pyqtProperty pour l'animation ────────────────────────────────

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, v: float) -> None:
        self._offset = v
        self.update()

    anim_offset = pyqtProperty(float, _get_offset, _set_offset)

    # ── public ────────────────────────────────────────────────────────

    def set_char(self, ch: str, animate: bool = True) -> None:
        if ch == self._current_char:
            return
        self._from_char = self._current_char
        self._current_char = ch

        if not animate or self._from_char == " " or ch == " ":
            # Pas d'anim pour l'espace : apparition directe
            self._offset = 0.0
            self.update()
            return

        if self._anim is not None:
            self._anim.stop()
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"anim_offset", self)
        self._anim.setDuration(250)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(-1.0)  # fait monter: l'ancien sort par le haut, le nouveau entre par le bas
        self._anim.start()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font)
        w, h = self.width(), self.height()

        # Chiffre entrant (en-dessous, monte vers 0)
        if self._offset < 0.0:
            # from_char sort vers le haut
            y_from = int(self._offset * h)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(
                QRect(0, y_from, w, h),
                Qt.AlignmentFlag.AlignCenter,
                self._from_char,
            )
            # current_char entre par le bas
            y_cur = h + int(self._offset * h)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(
                QRect(0, y_cur, w, h),
                Qt.AlignmentFlag.AlignCenter,
                self._current_char,
            )
        else:
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(
                QRect(0, 0, w, h),
                Qt.AlignmentFlag.AlignCenter,
                self._current_char,
            )
        painter.end()


class _OdometerWidget(QWidget):
    """Affiche un montant formaté (ex: '4 781 597 €') avec animation chiffre par chiffre."""

    def __init__(self, font: "QFont", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = font
        self._digits: list[_Digit] = []
        self._current_text = ""
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setStyleSheet(_FOND_TRANSPARENT)

    def set_text(self, text: str, animate: bool = True) -> None:
        if text == self._current_text:
            return
        prev = self._current_text
        self._current_text = text

        # Aligne à droite : complète avec espaces à gauche si texte plus court
        old_len = len(prev)
        new_len = len(text)
        target_len = max(old_len, new_len)
        old_padded = prev.rjust(target_len)
        new_padded = text.rjust(target_len)

        # Crée ou recycle les _Digit widgets
        while len(self._digits) < target_len:
            d = _Digit(self._font, self)
            self._layout.addWidget(d)
            self._digits.append(d)

        # Cache les excédentaires
        for i, d in enumerate(self._digits):
            d.setVisible(i < target_len)

        fm = QFontMetrics(self._font)
        cell_h = fm.height() + 4
        self.setFixedHeight(cell_h)

        for i in range(target_len):
            self._digits[i].set_char(new_padded[i], animate=(new_padded[i] != old_padded[i]))


# ---------------------------------------------------------------------------
# Composant 3 — Card cagnotte
# ---------------------------------------------------------------------------

def _fmt_euros_compact(n: float) -> str:
    """Euros sans décimale, espace insécable en séparateur.

    Insécable pour que « 12 400 € » ne se coupe jamais en fin de ligne : la
    carte est étroite, et un montant scindé se lit comme deux nombres.
    """
    return f"{int(n):,} €".replace(",", " ")


class _CagnotteCard(QFrame):
    """Overlay bas-gauche : heure, cagnotte totale, vitesse de collecte, viewers.

    L'heure tient dans la place laissée libre à droite du titre. Sur un écran
    occupé de bout en bout par ZLink — grand écran, plein écran — la barre des
    tâches est rétractée et il n'y a plus une seule horloge à portée de regard,
    alors qu'un soir de ZEvent on regarde l'heure sans arrêt : les paliers, le
    programme, la relève.
    """

    _W = 380

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(self._W)
        self.setStyleSheet(
            "QFrame { background-color: #0d0d0d; "
            "border-radius: 12px; border: 1px solid #303030; }"
        )

        vl = QVBoxLayout(self)
        vl.setContentsMargins(24, 20, 24, 20)
        vl.setSpacing(8)

        ligne_titre = QHBoxLayout()
        ligne_titre.setContentsMargins(0, 0, 0, 0)
        ligne_titre.setSpacing(8)
        section = QLabel("CAGNOTTE TOTALE")
        section.setFont(QFont("Consolas", 10))
        section.setStyleSheet(_TEXTE_VERT_SANS_BORDURE)
        ligne_titre.addWidget(section)
        ligne_titre.addStretch()
        self._heure_lbl = QLabel("")
        self._heure_lbl.setFont(QFont("Consolas", 10))
        self._heure_lbl.setStyleSheet(
            "color: #777777; background: transparent; border: none;")
        ligne_titre.addWidget(self._heure_lbl)
        vl.addLayout(ligne_titre)

        _odo_font = QFont("Consolas", 44, QFont.Weight.Bold)
        self._amount_odo = _OdometerWidget(_odo_font, self)
        vl.addWidget(self._amount_odo)

        # Ce que la cagnotte gagne par heure. Le total dit où on en est, pas
        # si ça monte : c'est pourtant la question, un soir d'événement.
        self._rythme_lbl = QLabel("")
        self._rythme_lbl.setFont(QFont("Consolas", 11))
        self._rythme_lbl.setStyleSheet(
            "color: #00ff87; background: transparent; border: none;")
        self._rythme_lbl.hide()
        vl.addWidget(self._rythme_lbl)

        # Barre de progression
        self._progress_bar = _ProgressBar(self)
        self._progress_bar.setFixedHeight(10)
        vl.addWidget(self._progress_bar)

        self._pct_lbl = QLabel("")
        self._pct_lbl.setFont(QFont("Consolas", 11))
        self._pct_lbl.setStyleSheet("color: #555555; background: transparent; border: none;")
        vl.addWidget(self._pct_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: 1px solid #1a1a1a; background: transparent;")
        vl.addWidget(sep)

        self._viewers_lbl = QLabel("—")
        self._viewers_lbl.setFont(QFont(_POLICE_UI, 14))
        self._viewers_lbl.setStyleSheet(_TEXTE_VERT_SANS_BORDURE)
        vl.addWidget(self._viewers_lbl)

        # Une seconde, pas une minute : au tour de minute, une horloge qui se
        # rafraîchit à la minute a jusqu'à soixante secondes de retard, et on
        # la croit arrêtée. Le libellé n'est réécrit que s'il change.
        self._horloge = QTimer(self)
        self._horloge.setInterval(1000)
        self._horloge.timeout.connect(self.rafraichir_heure)
        self._horloge.start()
        self.rafraichir_heure()

        self.adjustSize()

    def rafraichir_heure(self, maintenant: "QTime | None" = None) -> None:
        """Met l'horloge à l'heure locale."""
        texte = (maintenant or QTime.currentTime()).toString("HH:mm")
        if texte != self._heure_lbl.text():
            self._heure_lbl.setText(texte)

    def update_rate(self, euros_par_heure: float | None) -> None:
        """Vitesse de collecte, ou rien tant qu'on ne peut pas la mesurer.

        Masquée plutôt que mise à zéro : « 0 €/h » affirme que rien ne rentre,
        alors qu'avant l'événement il n'y a simplement pas encore de série à
        comparer.
        """
        if not euros_par_heure or euros_par_heure <= 0:
            self._rythme_lbl.hide()
            return
        self._rythme_lbl.setText(f"+ {_fmt_euros_compact(euros_par_heure)} / h")
        self._rythme_lbl.show()

    def update_stats(self, stats: "GlobalStats") -> None:
        amount = stats.donation_formatted
        if amount and "€" not in amount:
            amount = amount + " €"
        self._amount_odo.set_text(amount if amount else "—")
        # Pas de goal global connu → barre cachée
        self._progress_bar.hide()
        self._pct_lbl.hide()

    def update_viewers(self, viewers: int) -> None:
        self._viewers_lbl.setText(f"● {viewers:,} viewers en live".replace(",", "\u202f"))

    def update_live_count(self, count: int) -> None:
        self._viewers_lbl.setText(
            f"● {count} streamer{'s' if count != 1 else ''} en live"
        )


class _ProgressBar(QWidget):
    """Barre de progression verte custom."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pct: float = 0.0

    def set_pct(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, pct))
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = h // 2
        # Fond
        painter.setBrush(QBrush(QColor("#1a1a1a")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, w, h, r, r)
        # Remplissage
        fill_w = int(w * self._pct / 100)
        if fill_w > 0:
            painter.setBrush(QBrush(_GREEN))
            painter.drawRoundedRect(0, 0, fill_w, h, r, r)
        painter.end()


# ---------------------------------------------------------------------------
# Composant 4 — Card objectifs
# ---------------------------------------------------------------------------

class _GoalsCard(QFrame):
    """Overlay bas-droite : objectifs proches de complétion."""

    _W = 360
    _MAX_GOALS = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(self._W)
        self.setStyleSheet(
            "QFrame { background-color: #0d0d0d; "
            "border-radius: 12px; border: 1px solid #303030; }"
        )
        self._vl = QVBoxLayout(self)
        self._vl.setContentsMargins(24, 20, 24, 20)
        self._vl.setSpacing(0)

        section = QLabel("OBJECTIFS PROCHES")
        section.setFont(QFont("Consolas", 10))
        section.setStyleSheet(_TEXTE_VERT_SANS_BORDURE)
        self._vl.addWidget(section)

        self._content_w = QWidget()
        self._content_w.setStyleSheet(_FOND_TRANSPARENT)
        self._content_vl = QVBoxLayout(self._content_w)
        self._content_vl.setContentsMargins(0, 8, 0, 0)
        self._content_vl.setSpacing(0)
        self._vl.addWidget(self._content_w)

        self.hide()

    def update_goals(self, goals: "list[GoalWithStreamer]") -> None:
        # Nettoyer
        while self._content_vl.count():
            item = self._content_vl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        shown = [g for g in goals if 90.0 <= g.pct <= 100.0][: self._MAX_GOALS]
        if not shown:
            self.hide()
            return

        for g in shown:
            self._content_vl.addWidget(_GoalRow(g))

        self.adjustSize()
        self.show()


def _distance_objectif(g: "GoalWithStreamer") -> str:
    """« plus que 40 € · 96% », ou le seul pourcentage quand il ne manque rien."""
    if g.reste <= 0:
        return f"{g.pourcent_affiche}%"
    return f"plus que {_fmt_euros_compact(math.ceil(g.reste))} · {g.pourcent_affiche}%"


class _GoalRow(QWidget):
    def __init__(self, g: "GoalWithStreamer", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_FOND_TRANSPARENT)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 10, 0, 6)
        vl.setSpacing(6)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: 1px solid #222222; background: transparent;")
        vl.addWidget(sep)

        # Ligne avatar + noms
        top = QHBoxLayout()
        top.setSpacing(10)
        top.setContentsMargins(0, 0, 0, 0)

        av = QLabel()
        av.setFixedSize(32, 32)
        av.setAlignment(Qt.AlignmentFlag.AlignCenter)
        av.setStyleSheet("border-radius: 16px; background: transparent;")

        def _refresh_av() -> None:
            try:
                av.setPixmap(_avatar_cache.get(g.streamer_login, g.streamer_display, 32))
            except RuntimeError:
                pass

        av.setPixmap(
            _avatar_cache.get(g.streamer_login, g.streamer_display, 32, _refresh_av, "")
        )
        top.addWidget(av, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(2)
        info.setContentsMargins(0, 0, 0, 0)

        streamer_lbl = QLabel(g.streamer_display)
        streamer_lbl.setTextFormat(Qt.TextFormat.PlainText)
        streamer_lbl.setFont(QFont(_POLICE_UI, 12, QFont.Weight.Bold))
        streamer_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        info.addWidget(streamer_lbl)

        goal_lbl = QLabel(g.goal_name)
        goal_lbl.setTextFormat(Qt.TextFormat.PlainText)
        goal_lbl.setFont(QFont(_POLICE_UI, 10))
        goal_lbl.setStyleSheet("color: #aaaaaa; background: transparent; border: none;")
        goal_lbl.setWordWrap(True)
        info.addWidget(goal_lbl)

        top.addLayout(info, stretch=1)
        vl.addLayout(top)

        # Barre + % sur la même ligne
        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        bar = _ProgressBar()
        bar.setFixedHeight(6)
        bar.set_pct(g.pct)
        bar_row.addWidget(bar, stretch=1)
        # Comme dans le panel : entre 90 et 100 %, c'est le montant restant qui
        # distingue deux objectifs, pas le pourcentage.
        pct_lbl = QLabel(_distance_objectif(g))
        pct_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        pct_lbl.setStyleSheet(_TEXTE_VERT_SANS_BORDURE)
        pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(pct_lbl)
        vl.addLayout(bar_row)


# ---------------------------------------------------------------------------
# BigScreenWidget — assemblage final
# ---------------------------------------------------------------------------

class BigScreenWidget(QWidget):
    """Vue tableau de bord plein écran — style ZEvent live."""

    close_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("background-color: #0a0a0a;")

        # ── Ticker (haut, 56px) ───────────────────────────────────────
        self._ticker = _TickerWidget(self)

        # ── Fond 3D (derrière tout) ───────────────────────────────────
        self._bg = _BgAvatarsWidget(self)

        # ── Cards overlay ────────────────────────────────────────────
        self._cagnotte_card = _CagnotteCard(self)
        self._goals_card = _GoalsCard(self)

        # ── Bouton fermer ─────────────────────────────────────────
        self._close_btn = QPushButton("✕", self)
        self._close_btn.setFixedSize(36, 36)
        self._close_btn.setToolTip("Fermer le Big Screen")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(0,0,0,160);
                color: #888888;
                border: 1px solid #2a2a2a;
                border-radius: 18px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(40,40,40,220);
                color: #ffffff;
                border-color: #444444;
            }
        """)
        self._close_btn.clicked.connect(self.close_requested)

        # Le ticker est au-dessus du fond
        self._ticker.raise_()
        self._cagnotte_card.raise_()
        self._goals_card.raise_()
        self._close_btn.raise_()

        # Gradient supérieur pour lisibilité du ticker
        self._ticker.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._ticker.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        w, h = self.width(), self.height()

        # Ticker
        self._ticker.setGeometry(0, 0, w, 56)

        # Fond (commence sous le ticker)
        self._bg.setGeometry(0, 56, w, h - 56)

        # Cards
        self._cagnotte_card.adjustSize()
        ch = self._cagnotte_card.height()
        self._cagnotte_card.move(40, h - ch - 40)

        self._goals_card.adjustSize()
        gh = self._goals_card.height()
        self._goals_card.move(w - self._goals_card.width() - 40, h - gh - 40)

        # Bouton fermer — coin haut-droite
        self._close_btn.move(w - 36 - 12, 12)

    # -- public API -----------------------------------------------------------

    def update_streamers(self, streamers: "list[StreamerInfo]") -> None:
        """Met à jour le fond, le ticker et le compteur live."""
        self._ticker.set_streamers(streamers)
        self._bg.set_streamers(streamers)
        live_count = sum(1 for s in streamers if s.online)
        self._cagnotte_card.update_live_count(live_count)

    def update_stats(self, stats: "GlobalStats") -> None:
        """Met à jour la cagnotte."""
        self._cagnotte_card.update_stats(stats)

    def update_history(self, history) -> None:
        """Vitesse de collecte, lue sur la série de l'édition en cours.

        `donation_rate` rend des euros par MINUTE, et None tant que deux
        relevés ne sont pas assez espacés — ou hors de la fenêtre de
        l'événement, où il n'y a pas encore de série. La carte masque alors
        la ligne, plutôt que d'annoncer zéro.
        """
        par_minute = history.donation_rate() if history is not None else None
        self._cagnotte_card.update_rate(
            par_minute * 60.0 if par_minute else None)

    def update_goals(self, goals: "list[GoalWithStreamer]") -> None:
        """Met à jour les objectifs proches."""
        self._goals_card.update_goals(goals)
        # Repositionner la card goals après resize éventuel
        if self.height() > 0:
            self._goals_card.adjustSize()
            gh = self._goals_card.height()
            self._goals_card.move(
                self.width() - self._goals_card.width() - 40,
                self.height() - gh - 40,
            )
