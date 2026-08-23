"""BigScreenWidget — tableau de bord plein écran style ZEvent live."""

from __future__ import annotations

import logging
import math
import pathlib
import threading
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
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
    """Retourne un QPixmap en niveaux de gris à 30% d'opacité."""
    img = px.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    gray_px = QPixmap.fromImage(img)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setOpacity(0.30)
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


logger = logging.getLogger(__name__)

_AVATAR_MAX_BYTES = 2 * 1024 * 1024   # plafond de lecture pour un avatar


def _download_avatar(login: str, url: str) -> None:
    """Télécharge l'avatar depuis `url` et le stocke dans le cache disque.

    `login` et `url` viennent d'APIs tierces : le premier sert de nom de fichier,
    la seconde était passée à urlretrieve, qui accepte file:// et ftp://.
    """
    if not url:
        return
    import urllib.request
    cache_path = _AVATAR_CACHE_DIR / f"{login}.png"
    if cache_path.resolve().parent != _AVATAR_CACHE_DIR.resolve():
        logger.error("Avatar %r: chemin hors du cache, ignoré", login[:40])
        return
    if not url.lower().startswith("https://"):
        logger.error("Avatar %s: URL non https, ignorée", login)
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZLink/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = resp.read(_AVATAR_MAX_BYTES + 1)
        if len(payload) > _AVATAR_MAX_BYTES:
            logger.error("Avatar %s: réponse > %d octets, ignorée", login, _AVATAR_MAX_BYTES)
            return
        cache_path.write_bytes(payload)
    except Exception as exc:
        logger.debug("Avatar %s: téléchargement échoué — %s", login, exc)


# ---------------------------------------------------------------------------
# AvatarPixmapCache — chargement async centralisé
# ---------------------------------------------------------------------------

class _AvatarPixmapCache:
    """Charge les pixmaps ronds depuis le cache disque en background."""

    def __init__(self) -> None:
        self._cache: dict[str, QPixmap] = {}               # key → pixmap couleur
        self._gray: dict[str, QPixmap] = {}                # key → pixmap gris
        self._loading: dict[str, str] = {}                 # key → profile_url tenté
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
        # Enregistrer le callback quelle que soit la situation
        if callback is not None:
            self._pending.setdefault(key, []).append(callback)
        current_url = self._loading.get(key)  # None = jamais tenté, "" = tenté sans URL
        if current_url is None or (current_url == "" and profile_url):
            self._loading[key] = profile_url
            threading.Thread(
                target=self._load,
                args=(login, display, size, key, profile_url),
                daemon=True,
            ).start()
        return _initials_pixmap(login, display, size)

    def get_gray(self, login: str, display: str, size: int,
                 callback: "None | callable" = None,
                 profile_url: str = "") -> QPixmap:
        color = self.get(login, display, size, callback, profile_url)
        gkey = f"{login}@{size}:gray"
        if gkey in self._gray:
            return self._gray[gkey]
        self._gray[gkey] = _grayscale_pixmap(color, size)
        return self._gray[gkey]

    def _load(self, login: str, display: str, size: int,
              key: str, profile_url: str = "") -> None:
        px = _load_avatar_pixmap(login, size)
        if px is None and profile_url:
            _download_avatar(login, profile_url)
            px = _load_avatar_pixmap(login, size)
        if px is None:
            # Retry: 5s si l'URL a échoué, 2s si pas d'URL (le fichier peut arriver via bg)
            delay = 5.0 if profile_url else 2.0
            threading.Timer(delay, lambda: self._loading.pop(key, None)).start()
            return
        self._cache[key] = px
        self._gray.pop(f"{login}@{size}:gray", None)  # invalide le gris
        for cb in self._pending.pop(key, []):
            QTimer.singleShot(0, cb)

    def get_sq(self, login: str, display: str, size: int,
               callback: "None | callable" = None,
               profile_url: str = "") -> QPixmap:
        """Retourne un QPixmap carré (non rogné en cercle)."""
        key = f"{login}@{size}sq"
        if key in self._cache:
            return self._cache[key]
        if callback is not None:
            self._pending.setdefault(key, []).append(callback)
        current_url = self._loading.get(key)
        if current_url is None or (current_url == "" and profile_url):
            self._loading[key] = profile_url
            threading.Thread(
                target=self._load_sq,
                args=(login, display, size, key, profile_url),
                daemon=True,
            ).start()
        return _initials_square_pixmap(login, display, size)

    def get_gray_sq(self, login: str, display: str, size: int,
                    callback: "None | callable" = None,
                    profile_url: str = "") -> QPixmap:
        color = self.get_sq(login, display, size, callback, profile_url)
        gkey = f"{login}@{size}sq:gray"
        if gkey in self._gray:
            return self._gray[gkey]
        self._gray[gkey] = _grayscale_pixmap(color, size)
        return self._gray[gkey]

    def _load_sq(self, login: str, display: str, size: int,
                 key: str, profile_url: str = "") -> None:
        px = _load_square_avatar_pixmap(login, size)
        if px is None and profile_url:
            _download_avatar(login, profile_url)
            px = _load_square_avatar_pixmap(login, size)
        if px is None:
            delay = 5.0 if profile_url else 2.0
            threading.Timer(delay, lambda: self._loading.pop(key, None)).start()
            return
        self._cache[key] = px
        self._gray.pop(f"{login}@{size}sq:gray", None)
        for cb in self._pending.pop(key, []):
            QTimer.singleShot(0, cb)


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
        self._timer.start()

        import time
        self._last_ms = int(time.monotonic() * 1000)

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        # Uniquement les streamers en live, tri alphabétique
        self._streamers = sorted(
            [s for s in streamers if s.online],
            key=lambda s: s.twitch_login.lower(),
        )
        self.update()

    def _tick(self) -> None:
        import time
        now = int(time.monotonic() * 1000)
        dt = now - self._last_ms
        self._last_ms = now
        if not self._streamers:
            return
        total_w = len(self._streamers) * self._ITEM_W
        self._offset = (self._offset + self._SPEED * dt / 1000.0) % total_w
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
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
        name_font = QFont("Segoe UI", 11, QFont.Weight.Bold)
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
        game_font = QFont("Segoe UI", 9)
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
    _FPS_INTERVAL = 33  # ~30fps
    _SCROLL_PX = 1.5    # pixels défilés par frame

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._streamers: list[StreamerInfo] = []
        self._offset: float = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(self._FPS_INTERVAL)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        # tri alphabétique uniquement — pas de regroupement par état live
        self._streamers = sorted(streamers, key=lambda s: s.twitch_login.lower())
        self.update()

    def _tick(self) -> None:
        if not self._streamers:
            return
        w = self.width()
        if w == 0:
            return
        cell_w = w // self._COLS
        cell_h = cell_w + self._GAP
        n_rows = max(1, math.ceil(len(self._streamers) / self._COLS))
        total_h = n_rows * cell_h
        self._offset = (self._offset + self._SCROLL_PX) % total_h
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if not self._streamers:
            return

        painter = QPainter(self)
        # Aucun redimensionnement ni forme arrondie ici : les indices de rendu
        # lissé ne feraient que coûter du temps par image.

        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, QBrush(_BG))

        n = len(self._streamers)
        if n == 0:
            painter.end()
            return

        # Cellules carrées qui couvrent toute la largeur, images qui se touchent
        cell_w = w // self._COLS
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
        online: list[tuple[int, int, "QPixmap"]] = []
        offline: list[tuple[int, int, "QPixmap"]] = []

        for visible_row in range(rows_needed):
            cy = int(visible_row * cell_h - row_frac)
            if cy + cell_w < 0 or cy > h:
                continue
            data_row = (start_row + visible_row) % n_rows

            for col in range(self._COLS):
                idx = (data_row * self._COLS + col) % n
                s = self._streamers[idx]
                cx = col * cell_w
                purl = getattr(s, "profile_url", "")
                if s.online:
                    online.append((cx, cy, _avatar_cache.get_sq(
                        s.twitch_login, s.display, cell_w, self.update, purl
                    )))
                else:
                    offline.append((cx, cy, _avatar_cache.get_gray_sq(
                        s.twitch_login, s.display, cell_w, self.update, purl
                    )))

        painter.setOpacity(1.0)
        for cx, cy, px in online:
            painter.drawPixmap(cx, cy, px)
        painter.setOpacity(0.35)
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
        self.setStyleSheet("background: transparent;")

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

class _CagnotteCard(QFrame):
    """Overlay bas-gauche : cagnotte totale + barre + viewers."""

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

        section = QLabel("CAGNOTTE TOTALE")
        section.setFont(QFont("Consolas", 10))
        section.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        vl.addWidget(section)

        _odo_font = QFont("Consolas", 44, QFont.Weight.Bold)
        self._amount_odo = _OdometerWidget(_odo_font, self)
        vl.addWidget(self._amount_odo)

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
        self._viewers_lbl.setFont(QFont("Segoe UI", 14))
        self._viewers_lbl.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        vl.addWidget(self._viewers_lbl)

        self.adjustSize()

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
        section.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        self._vl.addWidget(section)

        self._content_w = QWidget()
        self._content_w.setStyleSheet("background: transparent;")
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


class _GoalRow(QWidget):
    def __init__(self, g: "GoalWithStreamer", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
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
        streamer_lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        streamer_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        info.addWidget(streamer_lbl)

        goal_lbl = QLabel(g.goal_name)
        goal_lbl.setFont(QFont("Segoe UI", 10))
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
        pct_lbl = QLabel(f"{g.pct:.0f}%")
        pct_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        pct_lbl.setStyleSheet("color: #00ff87; background: transparent; border: none;")
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
