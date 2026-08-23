"""Fenêtre fullscreen — stream principal + overlay info (écran centre)."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBitmap,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPixmap,
    QRegion,
    QScreen,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView as _QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEnginePage as _QWebEnginePage
    _WEBENGINE_OK: bool = True
except ImportError:
    _QWebEngineView = None  # type: ignore[assignment]
    _QWebEnginePage = None  # type: ignore[assignment]
    _WEBENGINE_OK: bool = False


if _WEBENGINE_OK and _QWebEnginePage is not None:
    class _SilentPage(_QWebEnginePage):  # type: ignore[misc]
        """QWebEnginePage qui absorbe les erreurs JS de Twitch (GraphQL non auth, etc.)."""
        def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # type: ignore[override]
            pass  # supprimer tous les messages JS du chat embed

from widgets.mpv_widget import MpvWidget
from widgets.bigscreen_widget import load_avatar_into_label as _load_avatar_into_label
from core.ad_watcher import AdWatcher
from core.api_client import _DONATION_HOSTS, _safe_https_url
from core.paths import CONFIG_PATH

if TYPE_CHECKING:
    from core.api_client import StreamerInfo

logger = logging.getLogger(__name__)


def _load_setting(key: str, default: object) -> object:
    """Lit une préférence de lecture depuis config.json."""
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(key, default)
    except Exception as exc:
        logger.warning("Lecture de %s impossible : %s", CONFIG_PATH.name, exc)
    return default


def _save_settings(values: dict) -> None:
    """Fusionne des préférences dans config.json (lecture-modification-écriture)."""
    try:
        cfg = {}
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg.update(values)
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        os.chmod(CONFIG_PATH, 0o600)
    except Exception as exc:
        logger.error("Sauvegarde de %s impossible : %s", CONFIG_PATH.name, exc)

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

FULLSCREEN_STYLE = """
QMainWindow {
    background-color: #0a0a0a;
}
"""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REMOTE_MENU_WIDTH: int = 320

_AVATAR_CACHE_DIR = Path.home() / ".zlink" / "avatars"
_AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_viewers(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n) if n > 0 else ""


def _circle_pixmap(px: QPixmap, size: int) -> QPixmap:
    """Rogne un QPixmap en cercle de `size` px."""
    scaled = px.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
    # crop to center square
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


# ---------------------------------------------------------------------------
# AvatarLabel
# ---------------------------------------------------------------------------

class AvatarLabel(QLabel):
    """QLabel circulaire avec chargement async de l'avatar depuis profileUrl."""

    def __init__(self, login: str, initials: str, size: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._login = login
        self._size = size
        self.setFixedSize(size, size)
        self._show_initials(initials)

    def _show_initials(self, initials: str) -> None:
        self.setText(initials[:2].upper() if initials else "?")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font_size = max(8, self._size // 3)
        self.setFont(QFont("Consolas", font_size, QFont.Weight.Bold))
        self.setStyleSheet(
            f"background: #1a1a1a; color: #555555;"
            f" border-radius: {self._size // 2}px;"
            f" min-width: {self._size}px; max-width: {self._size}px;"
            f" min-height: {self._size}px; max-height: {self._size}px;"
        )

    def load_async(self, url: str) -> None:
        """Charge l'avatar via le cache partagé (bigscreen_widget._avatar_cache)."""
        _load_avatar_into_label(self, self._login, "", self._size, url)


# ---------------------------------------------------------------------------
# LiveDot  (point animé vert)
# ---------------------------------------------------------------------------

class LiveDot(QWidget):
    """Petit cercle vert clignotant indiquant un stream live."""

    def __init__(self, size: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._color = QColor("#00ff87")
        self._anim_phase: int = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1200)

    def _tick(self) -> None:
        self._anim_phase = 1 - self._anim_phase
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = 255 if self._anim_phase == 0 else 160
        c = QColor(self._color)
        c.setAlpha(alpha)
        painter.setBrush(QBrush(c))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, self.width(), self.height())


# ---------------------------------------------------------------------------
# RemoteItem (grande taille — sélectionnés)
# ---------------------------------------------------------------------------

class RemoteItemLarge(QWidget):
    """Item 90px pour les streamers sélectionnés."""

    clicked = pyqtSignal(str)  # twitch_login

    def __init__(
        self,
        login: str,
        display: str,
        game: str,
        title: str,
        viewers: int,
        profile_url: str,
        is_current: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._login = login
        self._is_current = is_current
        self._is_kb_focused = False
        self.setFixedHeight(90)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self._build(display, game, title, viewers, profile_url)
        self._refresh_style()

    def _build(self, display: str, game: str, title: str, viewers: int, profile_url: str) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 0, 12, 0)
        outer.setSpacing(10)

        # ── Avatar 52px avec live dot ──────────────────────────────────
        avatar_container = QWidget()
        avatar_container.setFixedSize(52, 52)
        avatar_container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        avatar_container.setStyleSheet("background: transparent;")

        initials = display[:1] if display else self._login[:1]
        self._avatar = AvatarLabel(self._login, initials, 52, avatar_container)
        self._avatar.move(0, 0)

        dot = LiveDot(8, avatar_container)
        dot.move(52 - 10, 52 - 10)  # bas-droite

        outer.addWidget(avatar_container)

        # ── Infos ──────────────────────────────────────────────────────
        info_col = QWidget()
        info_col.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info_col.setStyleSheet("background: transparent;")
        col = QVBoxLayout(info_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        name_color = "#00ff87" if self._is_current else "#ffffff"
        name_lbl = QLabel(display if display else login)
        name_lbl.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {name_color}; background: transparent;")
        col.addWidget(name_lbl)
        self._name_lbl = name_lbl

        game_lbl = QLabel(game if game else "—")
        game_lbl.setFont(QFont("Segoe UI", 11))
        game_lbl.setStyleSheet("color: #888888; background: transparent;")
        col.addWidget(game_lbl)

        title_font = QFont("Segoe UI", 10)
        title_lbl = QLabel()
        title_lbl.setFont(title_font)
        title_lbl.setStyleSheet("color: #555555; background: transparent;")
        title_lbl.setWordWrap(False)
        title_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        col.addWidget(title_lbl)
        self._title_lbl = title_lbl
        self._raw_title = title or ""
        if self._raw_title:
            # Largeur disponible : 320 - marges(24) - avatar(52) - spacing(10) - scrollbar(6)
            _avail = REMOTE_MENU_WIDTH - 92
            elided = QFontMetrics(title_font).elidedText(
                self._raw_title, Qt.TextElideMode.ElideRight, _avail
            )
            title_lbl.setText(elided)
            title_lbl.setToolTip(self._raw_title)

        v_text = f"{_fmt_viewers(viewers)} viewers" if viewers > 0 else ""
        v_lbl = QLabel(v_text)
        v_lbl.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        v_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        col.addWidget(v_lbl)

        outer.addWidget(info_col, stretch=1)

        # ── Bouton Watch (caché par défaut) ────────────────────────────
        self._watch_btn = QPushButton("▶")
        self._watch_btn.setFixedSize(32, 32)
        self._watch_btn.setFont(QFont("Segoe UI", 16))
        self._watch_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #00ff87;
                border: none;
            }
            QPushButton:hover { color: #ffffff; }
        """)
        self._watch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._watch_btn.hide()
        self._watch_btn.clicked.connect(lambda: self.clicked.emit(self._login))
        outer.addWidget(self._watch_btn)

        # Charger avatar async
        self._avatar.load_async(profile_url)

    def _refresh_style(self) -> None:
        if self._is_kb_focused:
            self.setStyleSheet(
                "RemoteItemLarge { background: #1a1a1a;"
                " border-left: 3px solid #00ff87; border-radius: 6px; }"
            )
        elif self._is_current:
            self.setStyleSheet(
                "RemoteItemLarge { background: #0d1f0d;"
                " border-left: 3px solid #00ff87; border-radius: 6px; }"
            )
        else:
            self.setStyleSheet(
                "RemoteItemLarge { background: transparent;"
                " border-left: 3px solid transparent; border-radius: 6px; }"
                "RemoteItemLarge:hover { background: #111111; }"
            )

    def set_keyboard_focus(self, focused: bool) -> None:
        self._is_kb_focused = focused
        self._refresh_style()

    def set_current(self, is_current: bool) -> None:
        self._is_current = is_current
        if hasattr(self, "_name_lbl"):
            self._name_lbl.setStyleSheet(
                f"color: {'#00ff87' if is_current else '#ffffff'}; background: transparent;"
            )
        self._refresh_style()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._watch_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._watch_btn.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._login)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# RemoteItemSmall  (non-sélectionnés — 56px)
# ---------------------------------------------------------------------------

class RemoteItemSmall(QWidget):
    """Item 56px pour les streamers non-sélectionnés."""

    clicked = pyqtSignal(str)  # twitch_login

    def __init__(
        self,
        login: str,
        display: str,
        game: str,
        viewers: int,
        profile_url: str,
        is_current: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._login = login
        self._is_current = is_current
        self._is_kb_focused = False
        self.setFixedHeight(56)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self._build(display, game, viewers, profile_url)
        self._refresh_style()

    def _build(self, display: str, game: str, viewers: int, profile_url: str) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 0, 12, 0)
        outer.setSpacing(10)

        # Avatar 36px
        initials = display[:1] if display else self._login[:1]
        self._avatar = AvatarLabel(self._login, initials, 36)
        outer.addWidget(self._avatar)

        # Nom + viewers
        info_col = QWidget()
        info_col.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info_col.setStyleSheet("background: transparent;")
        col = QVBoxLayout(info_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(6)
        name_lbl = QLabel(display if display else self._login)
        name_lbl.setFont(QFont("Segoe UI", 12))
        name_lbl.setStyleSheet("color: #aaaaaa; background: transparent;")
        row1.addWidget(name_lbl)
        v_text = _fmt_viewers(viewers)
        v_lbl = QLabel(v_text)
        v_lbl.setFont(QFont("Consolas", 11))
        v_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        row1.addWidget(v_lbl)
        row1.addStretch()
        col_widget1 = QWidget()
        col_widget1.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        col_widget1.setStyleSheet("background: transparent;")
        col_widget1.setLayout(row1)
        col.addWidget(col_widget1)

        if game:
            # Largeur disponible : 320 - marges(24) - avatar(36) - spacing(10) - scrollbar(6)
            _avail_g = REMOTE_MENU_WIDTH - 76
            _game_font = QFont("Segoe UI", 10)
            game_text = QFontMetrics(_game_font).elidedText(
                game, Qt.TextElideMode.ElideRight, _avail_g
            )
        else:
            _game_font = QFont("Segoe UI", 10)
            game_text = ""
        game_lbl = QLabel(game_text)
        game_lbl.setFont(_game_font)
        game_lbl.setStyleSheet("color: #555555; background: transparent;")
        if game:
            game_lbl.setToolTip(game)
        col.addWidget(game_lbl)

        outer.addWidget(info_col, stretch=1)

        # Bouton Watch
        self._watch_btn = QPushButton("▶")
        self._watch_btn.setFixedSize(28, 28)
        self._watch_btn.setFont(QFont("Segoe UI", 14))
        self._watch_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #00ff87;
                border: none;
            }
            QPushButton:hover { color: #ffffff; }
        """)
        self._watch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._watch_btn.hide()
        self._watch_btn.clicked.connect(lambda: self.clicked.emit(self._login))
        outer.addWidget(self._watch_btn)

        self._avatar.load_async(profile_url)

    def _refresh_style(self) -> None:
        if self._is_kb_focused:
            self.setStyleSheet(
                "RemoteItemSmall { background: #1a1a1a;"
                " border-left: 3px solid #00ff87; border-radius: 6px; }"
            )
        elif self._is_current:
            self.setStyleSheet(
                "RemoteItemSmall { background: #0d1f0d;"
                " border-left: 3px solid #00ff87; border-radius: 6px; }"
            )
        else:
            self.setStyleSheet(
                "RemoteItemSmall { background: transparent;"
                " border-left: 3px solid transparent; border-radius: 6px; }"
                "RemoteItemSmall:hover { background: #111111; }"
            )

    def set_keyboard_focus(self, focused: bool) -> None:
        self._is_kb_focused = focused
        self._refresh_style()

    def set_current(self, is_current: bool) -> None:
        self._is_current = is_current
        self._refresh_style()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._watch_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._watch_btn.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._login)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# _SectionSep  (séparateur de section)
# ---------------------------------------------------------------------------

def _make_section_sep(text: str) -> QWidget:
    """Séparateur horizontal avec texte centré (style — TEXTE —)."""
    w = QWidget()
    w.setFixedHeight(24)
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    w.setStyleSheet("background: transparent;")
    layout = QHBoxLayout(w)
    layout.setContentsMargins(12, 0, 12, 0)
    layout.setSpacing(6)

    def _line() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setFixedHeight(1)
        f.setStyleSheet("color: #1e1e1e; background: #1e1e1e;")
        return f

    layout.addWidget(_line())
    lbl = QLabel(text)
    lbl.setFont(QFont("Consolas", 9))
    lbl.setStyleSheet("color: #333333; background: transparent;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
    layout.addWidget(lbl)
    layout.addWidget(_line())
    return w


def _make_item_sep() -> QWidget:
    """Séparateur fin entre items."""
    w = QWidget()
    w.setFixedHeight(1)
    w.setStyleSheet("background-color: #1e1e1e; border: none;")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    return w


# ---------------------------------------------------------------------------
# RemoteItem
# ---------------------------------------------------------------------------
# RemoteMenu
# ---------------------------------------------------------------------------

class RemoteMenu(QWidget):
    """Menu latéral slide-in/out avec navigation clavier pour les streams.

    Deux sections :
      • SÉLECTIONNÉS  — grands items (90px), avatar 52px, titre + viewers
      • AUTRES LIVE   — petits items  (56px), avatar 36px, nom + viewers
    """

    stream_selected = pyqtSignal(str)  # twitch_login

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedWidth(REMOTE_MENU_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "RemoteMenu { background: rgba(10, 10, 10, 235);"
            " border-right: 1px solid #1e1e1e; }"
        )

        self._keyboard_idx: int = -1
        self._items: list[RemoteItemLarge | RemoteItemSmall] = []
        self._login_list: list[str] = []
        self._last_streamers: list = []   # list[StreamerInfo] at runtime
        self._last_selected: list[str] = []
        self._current_login: str = ""
        self._hiding: bool = False
        self._needs_rebuild: bool = True  # rebuild différé si menu caché

        self._build()

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.finished.connect(self._on_anim_finished)

        self.hide()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header 56px ───────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(56)
        header.setStyleSheet(
            "background: transparent; border-bottom: 1px solid #1e1e1e;"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(0)

        self._title_lbl = QLabel("STREAMS")
        self._title_lbl.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(
            "color: #00ff87; letter-spacing: 3px; background: transparent;"
        )
        hl.addWidget(self._title_lbl)

        hl.addStretch()

        self._count_lbl = QLabel("0 en live")
        self._count_lbl.setFont(QFont("Segoe UI", 11))
        self._count_lbl.setStyleSheet("color: #555555; background: transparent;")
        hl.addWidget(self._count_lbl)

        outer.addWidget(header)

        # ── ScrollArea ────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: #0a0a0a; width: 4px; border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: #333333; border-radius: 2px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 4, 0, 8)
        self._container_layout.setSpacing(0)
        self._container_layout.addStretch()

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

    # ── public API ────────────────────────────────────────────────────

    def update_streamers(
        self,
        streamers: list,   # list[StreamerInfo]
        selected_logins: list[str],
        current_login: str,
    ) -> None:
        self._last_streamers = list(streamers)
        self._last_selected = list(selected_logins)
        self._current_login = current_login
        if self.isVisible():
            self._rebuild()
            self._needs_rebuild = False
        else:
            # Menu caché : mémoriser et rebuild au prochain show_menu()
            self._needs_rebuild = True

    def set_current_login(self, login: str) -> None:
        """Met à jour le stream courant sans reconstruire toute la liste."""
        self._current_login = login
        # Mise à jour in-place : évite le full rebuild pour un simple changement
        for item in self._items:
            item.set_current(item._login == login)

    def select_previous(self) -> None:
        if not self._items:
            return
        self._set_kb_idx(max(0, self._keyboard_idx - 1))

    def select_next(self) -> None:
        if not self._items:
            return
        self._set_kb_idx(min(len(self._items) - 1, self._keyboard_idx + 1))

    def confirm_selection(self) -> None:
        if 0 <= self._keyboard_idx < len(self._login_list):
            login = self._login_list[self._keyboard_idx]
            self.stream_selected.emit(login)
            QTimer.singleShot(500, self.hide_menu)

    def show_menu(self) -> None:
        if self._needs_rebuild:
            self._rebuild()
            self._needs_rebuild = False
        h = self.parent().height() if self.parent() else 600
        self._hiding = False
        self.setGeometry(-REMOTE_MENU_WIDTH, 0, REMOTE_MENU_WIDTH, h)
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(QRect(-REMOTE_MENU_WIDTH, 0, REMOTE_MENU_WIDTH, h))
        self._anim.setEndValue(QRect(0, 0, REMOTE_MENU_WIDTH, h))
        self._anim.start()

    def hide_menu(self) -> None:
        if not self.isVisible():
            return
        h = self.parent().height() if self.parent() else 600
        self._hiding = True
        self._anim.stop()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(QRect(-REMOTE_MENU_WIDTH, 0, REMOTE_MENU_WIDTH, h))
        self._anim.start()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_menu()
        else:
            self.show_menu()

    # ── internals ─────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        # Supprimer les anciens items
        for item in self._items:
            item.deleteLater()
        self._items.clear()
        self._login_list.clear()

        # Vider le container (tout sauf le stretch final)
        while self._container_layout.count() > 1:
            child = self._container_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        sel_set = set(self._last_selected)
        live = [s for s in self._last_streamers if s.online]
        selected_live = [s for s in live if s.twitch_login in sel_set]
        other_live = [s for s in live if s.twitch_login not in sel_set]

        self._count_lbl.setText(f"{len(live)} en live")

        idx = 0

        # ── Section SÉLECTIONNÉS ──────────────────────────────────────
        if selected_live:
            self._container_layout.insertWidget(idx, _make_section_sep("SÉLECTIONNÉS"))
            idx += 1
            for i, s in enumerate(selected_live):
                item = self._make_large_item(s)
                self._container_layout.insertWidget(idx, item)
                idx += 1
                if i < len(selected_live) - 1:
                    self._container_layout.insertWidget(idx, _make_item_sep())
                    idx += 1

        # ── Section AUTRES LIVE ───────────────────────────────────────
        if other_live:
            self._container_layout.insertWidget(idx, _make_section_sep("AUTRES LIVE"))
            idx += 1
            for i, s in enumerate(other_live):
                item = self._make_small_item(s)
                self._container_layout.insertWidget(idx, item)
                idx += 1
                if i < len(other_live) - 1:
                    self._container_layout.insertWidget(idx, _make_item_sep())
                    idx += 1

        self._keyboard_idx = -1

    def _make_large_item(self, s: "StreamerInfo") -> RemoteItemLarge:
        item = RemoteItemLarge(
            login=s.twitch_login,
            display=s.display,
            game=s.game,
            title=getattr(s, "title", ""),
            viewers=s.viewers,
            profile_url=s.profile_url,
            is_current=(s.twitch_login == self._current_login),
        )
        item.clicked.connect(self._on_item_clicked)
        self._items.append(item)
        self._login_list.append(s.twitch_login)
        return item

    def _make_small_item(self, s: "StreamerInfo") -> RemoteItemSmall:
        item = RemoteItemSmall(
            login=s.twitch_login,
            display=s.display,
            game=s.game,
            viewers=s.viewers,
            profile_url=s.profile_url,
            is_current=(s.twitch_login == self._current_login),
        )
        item.clicked.connect(self._on_item_clicked)
        self._items.append(item)
        self._login_list.append(s.twitch_login)
        return item

    def _on_item_clicked(self, login: str) -> None:
        self.stream_selected.emit(login)
        QTimer.singleShot(300, self.hide_menu)

    def _set_kb_idx(self, idx: int) -> None:
        if 0 <= self._keyboard_idx < len(self._items):
            self._items[self._keyboard_idx].set_keyboard_focus(False)
        self._keyboard_idx = idx
        if 0 <= idx < len(self._items):
            self._items[idx].set_keyboard_focus(True)
            self._scroll.ensureWidgetVisible(self._items[idx])

    def _on_anim_finished(self) -> None:
        if self._hiding:
            self.hide()


# ---------------------------------------------------------------------------
# ChatPanel
# ---------------------------------------------------------------------------

class ChatPanel(QWidget):
    """Panneau chat Twitch via QWebEngineView, redimensionnable par drag."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._width: int = 350
        self._visible: bool = False
        self._login: str = ""
        self._drag_start_x: float = 0.0
        self._drag_start_width: int = 350
        self._build()
        self.hide()

    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._handle = QWidget(self)
        self._handle.setFixedWidth(4)
        self._handle.setStyleSheet("background: #333333;")
        self._handle.setCursor(Qt.CursorShape.SizeHorCursor)
        self._handle.installEventFilter(self)
        outer.addWidget(self._handle)

        if _WEBENGINE_OK and _QWebEngineView is not None:
            self._web: QWidget = _QWebEngineView()
            self._web.setPage(_SilentPage(self._web))  # type: ignore[arg-type]
            self._web.setStyleSheet("background: #0a0a0a;")
            self._web.loadFinished.connect(self._inject_chat_css)  # type: ignore[attr-defined]
        else:
            placeholder = QLabel("\U0001f4ac\nPyQt6-WebEngine\nnon install\u00e9")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #555555; background: #0a0a0a;")
            self._web = placeholder

        outer.addWidget(self._web)

    # ── public API ────────────────────────────────────────────────────

    def _inject_chat_css(self, _ok: bool = True) -> None:
        """Injecte du CSS pour masquer tous les éléments UI inutiles du chat Twitch."""
        if not _WEBENGINE_OK or not hasattr(self._web, "page"):
            return
        js = r"""
(function () {
    var css = `
        /* Zone de saisie et envoi de message */
        .chat-input, .chat-footer,
        .chat-input__textarea, .chat-input-buttons-container,
        .chat-input__buttons, .chat-input-wrapper,
        .chatter-input-container, .chat-room__input,
        [data-a-target="chat-input"],
        [data-test-selector="chat-input"],
        [data-test-selector="chat-input-buttons"],
        .thread-message__input-wrapper,
        /* Bannière cookies / RGPD */
        .consent-banner, [data-a-target="consent-banner"],
        .privacy-policy-overlay, .cookie-policy-banner,
        /* Prompts connexion */
        .chat-login-overlay, .anon-chat-login-prompt,
        [data-a-target="anonymous-chat-login-prompt"],
        [data-test-selector="anon-chat-notice"],
        .chat-room__content ~ div .tw-relative > .tw-flex,
        /* Points de chaîne / récompenses */
        .community-points-summary, .community-points-icon,
        [data-test-selector="community-points-summary"],
        /* Messages mis en valeur via récompenses (Clip to take, etc.) */
        .chat-line--is-highlighted,
        [data-test-selector="highlighted-chat-message"],
        .reward-redeemed-chat-card,
        .chat-line__message--highlighted,
        /* Indicateurs de salle (slow mode, emotes only…) */
        .room-state-indicator, .chat-room__state-indicators,
        /* Popup "Choisissez un pseudo" */
        .chat-settings, .chat-room__footer [data-test-selector],
        /* Bouton "Rejoindre la conversation" */
        [data-a-target="chat-join-button"],
        /* Header de la chatroom */
        .chat-room__header,
        /* Barre "Stream Chat" avec icônes */
        .stream-chat-header, .chat-shell__expanded-header,
        [data-test-selector="chat-room-component-layout"] > header,
        .Layout-sc-1xcs6mc-0.frkJAl, .chat-shell header
        { display: none !important; }

        /* Supprimer le padding bas réservé à la zone de saisie */
        .chat-room__content { padding-bottom: 0 !important; }

        /* Forcer le fond sombre */
        body, .chat-room, .chat-list--default {
            background-color: #0a0a0a !important;
        }
    `;
    if (!document.getElementById('zlink-chat-css')) {
        var style = document.createElement('style');
        style.id = 'zlink-chat-css';
        style.textContent = css;
        (document.head || document.documentElement).appendChild(style);
    }
})();
"""
        self._web.page().runJavaScript(js)  # type: ignore[attr-defined]

    def set_stream(self, login: str) -> None:
        self._login = login
        if self._visible and _WEBENGINE_OK and login:
            url = (
                f"https://www.twitch.tv/embed/{login}/chat"
                "?parent=localhost&darkpopout"
            )
            self._web.setUrl(QUrl(url))  # type: ignore[attr-defined]

    def show_chat(self) -> None:
        self._visible = True
        self.set_stream(self._login)
        self.show()
        self.raise_()
        self._update_geometry()

    def hide_chat(self) -> None:
        self._visible = False
        self.hide()

    def toggle(self) -> None:
        if self._visible:
            self.hide_chat()
        else:
            self.show_chat()

    def _update_geometry(self) -> None:
        p = self.parent()
        if p:
            self.setGeometry(p.width() - self._width, 0, self._width, p.height())

    # ── drag resize ───────────────────────────────────────────────────

    def eventFilter(self, obj: object, event: object) -> bool:  # type: ignore[override]
        if obj is self._handle:
            ev = event  # type: ignore[assignment]
            if ev.type() == QEvent.Type.MouseButtonPress:
                if ev.button() == Qt.MouseButton.LeftButton:
                    self._drag_start_x = ev.globalPosition().x()
                    self._drag_start_width = self._width
                    return True
            elif ev.type() == QEvent.Type.MouseMove:
                if ev.buttons() & Qt.MouseButton.LeftButton:
                    delta = int(self._drag_start_x - ev.globalPosition().x())
                    self._width = max(250, min(600, self._drag_start_width + delta))
                    self._update_geometry()
                    p = self.parent()
                    fs = p.parent() if p is not None else None
                    if fs is not None and hasattr(fs, "_update_mpv_geometry"):
                        fs._update_mpv_geometry()  # type: ignore[union-attr]
                    return True
        return super().eventFilter(obj, event)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ad-break banner + end-of-ad toast
# ---------------------------------------------------------------------------

class _AdBreakBanner(QWidget):
    """Bandeau semi-transparent affiché en bas du fullscreen lors d'une pub.

    Propose à l'utilisateur de recevoir une notification quand la pub se termine,
    ce qui permet de changer de flux entre-temps.
    """

    notify_requested = pyqtSignal(str)   # login à surveiller
    dismissed        = pyqtSignal()

    def __init__(self, login: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._login = login

        self.setStyleSheet(
            "QWidget { background: rgba(8,8,8,210);"
            " border-top: 1px solid rgba(255,80,0,120); }"
        )
        self.setFixedHeight(56)

        h = QHBoxLayout(self)
        h.setContentsMargins(16, 0, 12, 0)
        h.setSpacing(12)

        # Icône + texte
        icon = QLabel("📺")
        icon.setFont(QFont("Segoe UI", 14))
        icon.setStyleSheet("background: transparent; border: none;")
        h.addWidget(icon)

        self._msg = QLabel(f"<b>{html.escape(login)}</b> — Publicité en cours — 0:00")
        self._msg.setFont(QFont("Segoe UI Variable", 11))
        self._msg.setStyleSheet("color: #cccccc; background: transparent; border: none;")
        h.addWidget(self._msg, stretch=1)

        # Bouton "Me prévenir"
        self._notify_btn = QPushButton("🔔  Me prévenir")
        self._notify_btn.setFixedHeight(32)
        self._notify_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._notify_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,100,0,30);
                color: #ff9966;
                border: 1px solid rgba(255,100,0,120);
                border-radius: 4px;
                padding: 0 14px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: rgba(255,100,0,60);
                color: #ffffff;
                border-color: #ff6600;
            }
            QPushButton:pressed { background: rgba(255,100,0,90); }
        """)
        self._notify_btn.clicked.connect(self._on_notify)
        h.addWidget(self._notify_btn)

        # Bouton fermer
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #555555;"
            " border: none; font-size: 13px; }"
            "QPushButton:hover { color: #aaaaaa; }"
        )
        close_btn.clicked.connect(self._dismiss)
        h.addWidget(close_btn)

        # Minuteur temps écoulé
        self._elapsed = 0
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()

        # Apparition depuis le bas
        self._show_animated(parent)

    def _tick(self) -> None:
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        self._msg.setText(
            f"<b>{html.escape(self._login)}</b> — Publicité en cours — {m}:{s:02d}"
        )

    def _show_animated(self, parent: QWidget) -> None:
        self.move(0, parent.height())
        self.resize(parent.width(), 56)
        self.show()
        self.raise_()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(250)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.pos())
        anim.setEndValue(self.parent().rect().bottomLeft() - self.rect().bottomLeft())  # type: ignore[union-attr]
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def reposition(self, parent_width: int, parent_height: int) -> None:
        self.setGeometry(0, parent_height - 56, parent_width, 56)

    def mark_notifying(self) -> None:
        """Change l'état du bouton pour confirmer l'inscription."""
        self._notify_btn.setText("✓  Notification activée")
        self._notify_btn.setEnabled(False)
        self._notify_btn.setStyleSheet(
            "QPushButton { background: rgba(0,200,100,25); color: #00ff87;"
            " border: 1px solid rgba(0,200,100,80); border-radius: 4px;"
            " padding: 0 14px; font-size: 12px; }"
        )

    def dismiss_animated(self) -> None:
        """Fait glisser le banner vers le bas puis le ferme."""
        self._ticker.stop()
        if not self.isVisible():
            return
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.setStartValue(self.pos())
        target = self.pos()
        target.setY(target.y() + 60)
        anim.setEndValue(target)
        anim.finished.connect(self.close)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _on_notify(self) -> None:
        self.notify_requested.emit(self._login)
        self.mark_notifying()

    def _dismiss(self) -> None:
        self.dismissed.emit()
        self.dismiss_animated()


class _AdEndToast(QWidget):
    """Toast 'Pub terminée' affiché en bas-droite quand une pub surveillée se termine."""

    switch_requested = pyqtSignal(str)  # login

    def __init__(self, login: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._login = login

        w, h = 300, 64
        self.setFixedSize(w, h)
        self.setStyleSheet(
            "QWidget { background: rgba(8,8,8,220);"
            " border: 1px solid #00ff87; border-radius: 6px; }"
        )

        vl = QVBoxLayout(self)
        vl.setContentsMargins(12, 6, 12, 6)
        vl.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        dot = QLabel("●")
        dot.setFont(QFont("Segoe UI", 9))
        dot.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        top.addWidget(dot)
        title = QLabel(f"<b>{html.escape(login)}</b> — Pub terminée !")
        title.setFont(QFont("Segoe UI Variable", 11))
        title.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top.addWidget(title, stretch=1)
        vl.addLayout(top)

        switch_btn = QPushButton("Regarder maintenant →")
        switch_btn.setFixedHeight(22)
        switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        switch_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #00ff87;"
            " border: none; font-size: 11px; text-align: left; padding: 0; }"
            "QPushButton:hover { color: #ffffff; }"
        )
        switch_btn.clicked.connect(lambda: self.switch_requested.emit(self._login))
        vl.addWidget(switch_btn)

        # Position bas-droite, auto-dismiss 8s
        self.move(parent.width() - w - 16, parent.height() - h - 16)
        self.show()
        self.raise_()
        QTimer.singleShot(8000, self._fade_out)

    def _fade_out(self) -> None:
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(600)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


# ---------------------------------------------------------------------------
# Toast HypeWatcher (discret, haut-droite)
# ---------------------------------------------------------------------------

_FS_TOAST_W = 260
_FS_TOAST_H = 36


class _FsHypeToast(QWidget):
    """Toast discret affiché en haut à droite de la FullscreenWindow."""

    def __init__(
        self, login: str, label: str, score: float, color: str, parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFixedSize(_FS_TOAST_W, _FS_TOAST_H)
        self.setStyleSheet(
            f"QWidget {{ background: rgba(0,0,0,180); "
            f"border-left: 3px solid {color}; border-radius: 4px; }}"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(6)

        name_lbl = QLabel(login or "?")
        name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        name_lbl.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        name_lbl.setMaximumWidth(90)
        h.addWidget(name_lbl)

        sep = QLabel("·")
        sep.setFont(QFont("Segoe UI Variable", 10))
        sep.setStyleSheet("color: #555555; background: transparent; border: none;")
        h.addWidget(sep)

        lbl = QLabel(label)
        # Texte produit par le LLM à partir du chat : texte brut obligatoire.
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setFont(QFont("Segoe UI Variable", 10))
        lbl.setStyleSheet("color: #cccccc; background: transparent; border: none;")
        h.addWidget(lbl, stretch=1)

        # Position en haut à droite
        self.move(parent.width() - _FS_TOAST_W - 8, 8)
        QTimer.singleShot(4000, self._fade_out)

    def _fade_out(self) -> None:
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(600)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start()


# ---------------------------------------------------------------------------
# FullscreenWindow
# ---------------------------------------------------------------------------

class FullscreenWindow(QMainWindow):
    """Fenêtre plein écran pour le stream principal.

    Overlay en bas (nom, jeu, viewers) visible au mouvement souris,
    masqué avec fade-out après 2 s d'inactivité.
    """

    stream_change_requested = pyqtSignal(str)
    stream_changed = pyqtSignal(str)  # emis quand le stream actif change (Bug 3)

    def __init__(self, screen: QScreen, show_on_init: bool = True, clip_config: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target_screen = screen
        self._clip_config: dict = clip_config or {}
        self.setWindowTitle("ZLink — Fullscreen")
        self.setStyleSheet(FULLSCREEN_STYLE)
        self.setMouseTracking(True)

        self._current_login: str = ""
        self._current_game: str = ""
        self._current_viewers: int = 0
        self._current_donation_url: str = ""
        self._loading: bool = False
        self._pip_active: bool = False

        # Ad-break watcher
        self._ad_watcher = AdWatcher(self)
        self._ad_watcher.ad_detected.connect(self._on_ad_detected)
        self._ad_watcher.ad_ended.connect(self._on_ad_ended)
        self._ad_banner: _AdBreakBanner | None = None
        self._ad_notify_logins: set[str] = set()
        self._ad_active: bool = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self._build()
        if show_on_init:
            self._move_to_screen(screen)

    # ── layout ────────────────────────────────────────────────────────

    def _build(self) -> None:
        central = QWidget()
        central.setStyleSheet("background-color: #0a0a0a;")
        central.setMouseTracking(True)

        # MPV remplit tout le central (position absolue)

        # ── QStackedWidget : index 0 = état vide, index 1 = MPV ────────────
        self._stack = QStackedWidget(central)
        self._stack.setMouseTracking(True)

        # ── État vide (logo ZLink) ── index 0 ───────────────────────────────
        self._empty = QWidget()
        self._empty.setStyleSheet("background-color: #0a0a0a;")
        el = QVBoxLayout(self._empty)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(0)
        el.addStretch()

        logo = QLabel("ZLink")
        logo.setFont(QFont("Consolas", 48, QFont.Weight.Bold))
        logo.setStyleSheet("color: #00ff87; background: transparent;")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        el.addWidget(logo)

        el.addSpacing(12)

        self._hint_lbl = QLabel("Sélectionnez un stream dans la grille")
        self._hint_lbl.setFont(QFont("Segoe UI", 16))
        self._hint_lbl.setStyleSheet("color: #555555; background: transparent;")
        self._hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        el.addWidget(self._hint_lbl)

        el.addStretch()
        self._stack.addWidget(self._empty)   # index 0 — état vide

        # ── Zone vidéo MPV ── index 1 ────────────────────────────────────────
        _buf = max(self._clip_config.get("duration_secs", 60) + 30, 90)
        self._mpv = MpvWidget(clip_buffer_secs=_buf)
        self._mpv.setMouseTracking(True)
        self._mpv.installEventFilter(self)
        self._stack.addWidget(self._mpv)     # index 1 — MPV
        self._stack.setCurrentIndex(0)       # démarrer en état vide

        # Chat panel (droite, position absolue)
        self._chat_panel = ChatPanel(central)

        # Overlay flotte par-dessus le MPV (position absolue, parent = central)

        # ── Overlay bar 56 px ────────────────────────────────────────
        self._overlay = QWidget(central)
        self._overlay.setFixedHeight(56)
        self._overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 217); border-top: 1px solid #222222;",
        )

        # Opacity effect for fade in/out
        self._opacity_fx = QGraphicsOpacityEffect(self._overlay)
        self._opacity_fx.setOpacity(0.0)
        self._overlay.setGraphicsEffect(self._opacity_fx)

        self._fade_anim = QPropertyAnimation(self._opacity_fx, b"opacity")
        self._fade_anim.setDuration(200)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.finished.connect(self._on_fade_finished)

        # Timer d'auto-hide : cache l'overlay 3s après le dernier mouvement souris
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._start_fade_out)

        ol = QHBoxLayout(self._overlay)
        ol.setContentsMargins(20, 0, 20, 0)
        ol.setSpacing(0)

        # Left: name · game
        self._ov_info = QLabel()
        self._ov_info.setFont(QFont("Segoe UI", 15))
        self._ov_info.setStyleSheet("color: #ffffff; background: transparent;")
        ol.addWidget(self._ov_info)

        ol.addStretch()

        # Right: viewers
        self._ov_viewers = QLabel()
        self._ov_viewers.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        self._ov_viewers.setStyleSheet("color: #00ff87; background: transparent;")
        self._ov_viewers.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        ol.addWidget(self._ov_viewers)

        ol.addSpacing(16)

        # ── Volume ───────────────────────────────────────────────
        self._volume = int(_load_setting("volume", 60))
        self._muted = bool(_load_setting("muted", False))

        self._vol_btn = QPushButton()
        self._vol_btn.setFixedWidth(32)
        self._vol_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888888; border: none;"
            " font-size: 15px; }"
            "QPushButton:hover { color: #ffffff; }"
        )
        self._vol_btn.setToolTip("Couper le son")
        self._vol_btn.clicked.connect(self._toggle_mute)
        ol.addWidget(self._vol_btn)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(self._volume)
        self._vol_slider.setFixedWidth(110)
        self._vol_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #333333; height: 4px; border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #00ff87; height: 4px; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff; width: 12px; height: 12px;
                margin: -4px 0; border-radius: 6px;
            }
            QSlider::handle:horizontal:hover { background: #00ff87; }
        """)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        ol.addWidget(self._vol_slider)

        # Écriture différée : éviter un accès disque à chaque cran du curseur.
        self._vol_save_timer = QTimer(self)
        self._vol_save_timer.setSingleShot(True)
        self._vol_save_timer.setInterval(1500)
        self._vol_save_timer.timeout.connect(self._persist_volume)

        ol.addSpacing(16)

        # Bouton chat
        self._chat_btn = QPushButton("\U0001f4ac Chat")
        self._chat_btn.setCheckable(True)
        self._chat_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #888888;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QPushButton:checked {
                color: #00ff87;
                border-color: #00ff87;
            }
            QPushButton:hover { color: #ffffff; }
        """)
        self._chat_btn.clicked.connect(self._toggle_chat)
        ol.addWidget(self._chat_btn)

        ol.addSpacing(8)

        # Bouton donation
        self._donate_btn = QPushButton("\u2665 Faire un don")
        self._donate_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #888888;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                color: #00ff87;
                border-color: #00ff87;
            }
        """)
        self._donate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._donate_btn.clicked.connect(self._open_donate_view)
        self._donate_btn.hide()  # visible seulement quand donation_url est dispo
        ol.addWidget(self._donate_btn)

        ol.addSpacing(8)

        # Bouton clip 60s
        self._clip_btn = QPushButton("⏺ Clip")
        self._clip_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #888888;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QPushButton:hover { color: #ff4444; border-color: #ff4444; }
        """)
        self._clip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clip_btn.clicked.connect(self._save_clip)
        ol.addWidget(self._clip_btn)

        self._remote_menu = RemoteMenu(central)
        self._remote_menu.stream_selected.connect(self._on_remote_stream_selected)

        # Bouton toggle menu télécommande (toujours visible, haut gauche)
        self._remote_btn = QPushButton("\u2630", central)
        self._remote_btn.setFixedSize(32, 32)
        self._remote_btn.setStyleSheet("""
            QPushButton {
                background: rgba(0, 0, 0, 128);
                color: #ffffff;
                border: none;
                border-radius: 4px;
                font-size: 18px;
            }
            QPushButton:hover { background: rgba(0, 0, 0, 200); }
        """)
        self._remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remote_btn.clicked.connect(self._toggle_remote_menu)

        # Opacity effect pour le bouton télécommande (synchro avec overlay)
        self._btn_opacity_fx = QGraphicsOpacityEffect(self._remote_btn)
        self._btn_opacity_fx.setOpacity(0.0)
        self._remote_btn.setGraphicsEffect(self._btn_opacity_fx)

        # Z-order : stack au fond, remote_btn au premier plan
        self._stack.lower()
        self._chat_panel.raise_()
        self._overlay.raise_()
        self._remote_menu.raise_()
        self._remote_btn.raise_()

        # ── Vue donation (WebEngine) + bouton fermer ─────────────────
        if _WEBENGINE_OK and _QWebEngineView is not None:
            self._donate_view: QWidget = _QWebEngineView(central)
        else:
            # Fallback : label d'avertissement si WebEngine absent
            self._donate_view = QLabel(
                "PyQt6-WebEngine non disponible.\u00a0Ouvrez le lien manuellement.",
                central,
            )
            self._donate_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._donate_view.setStyleSheet("color: #888888; background: #0a0a0a;")
        self._donate_view.hide()

        self._donate_close_btn = QPushButton("\u2715  Fermer le don", central)
        self._donate_close_btn.setFixedHeight(36)
        self._donate_close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(0,0,0,180);
                color: #cccccc;
                border: 1px solid #444444;
                border-radius: 4px;
                padding: 0 16px;
                font-size: 13px;
            }
            QPushButton:hover { color: #ffffff; border-color: #888888; }
        """)
        self._donate_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._donate_close_btn.clicked.connect(self._close_donate_view)
        self._donate_close_btn.hide()
        self._donate_close_btn.raise_()

        self._overlay.hide()  # initialement caché — s'affiche au mouvement souris
        self._remote_btn.hide()  # idem

        self.setCentralWidget(central)

    def _move_to_screen(self, screen: QScreen) -> None:
        g = screen.geometry()
        self.setGeometry(g)
        self.show()  # crée le handle natif à la bonne position
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.showFullScreen()
        logger.info("Fullscreen ouverte sur %s (%dx%d)", screen.name(), g.width(), g.height())

    # ── public API ────────────────────────────────────────────────────

    def set_stream(
        self, twitch_login: str, game: str = "", viewers: int = 0, donation_url: str = ""
    ) -> None:
        """Appelé quand un stream est sélectionné depuis la grille.

        Met à jour l'overlay immédiatement (le stream MPV démarre
        via on_stream_ready() une fois l'URL résolue par StreamManager).
        """
        self._ad_watcher.unwatch(self._current_login)
        self._dismiss_ad_banner()
        self._ad_active = False
        self._current_login = twitch_login
        self._current_game = game
        self._current_viewers = viewers
        self._current_donation_url = donation_url
        self._loading = True

        # Referme la vue donation si elle était ouverte
        if self._pip_active:
            self._close_donate_view()

        # Bouton donation : visible seulement si l'URL est disponible
        self._donate_btn.setVisible(bool(donation_url))

        # login et game viennent d'APIs tierces : échappés avant interpolation
        # dans du texte riche.
        parts = [f"<b>{html.escape(twitch_login)}</b>"]
        if game:
            parts.append(
                f"<span style='color:#888888;'> · {html.escape(game)}</span>"
            )
        self._ov_info.setText("".join(parts))
        self._ov_viewers.setText(
            f"{self._fmt_viewers(viewers)} viewers" if viewers else "",
        )

        self._hint_lbl.setText("Chargement…")
        self._hint_lbl.setStyleSheet("color: #888888;")
        self._stack.setCurrentIndex(0)  # montrer l'état de chargement
        self._show_overlay()
        self._chat_panel.set_stream(twitch_login)
        self._remote_menu.set_current_login(twitch_login)
        self.stream_changed.emit(twitch_login)

    def on_stream_ready(self, login: str, url: str) -> None:
        """Signalé par StreamManager quand l'URL streamlink est résolue."""
        if login != self._current_login:
            return
        self._loading = False
        self._stack.setCurrentIndex(1)  # afficher le MPV
        self._mpv.play(url)
        self._ad_watcher.watch(login, url)
        logger.info("Fullscreen: lecture MPV démarrée pour %s", login)

    def on_stream_error(self, login: str, msg: str) -> None:
        """Signalé par StreamManager en cas d'échec streamlink."""
        if login != self._current_login:
            return
        self._loading = False
        self._hint_lbl.setText(f"⚠️ {msg}")
        self._hint_lbl.setStyleSheet("color: #ff4444;")
        self._stack.setCurrentIndex(0)  # retour à l'état vide (affiche le message d'erreur)
        logger.error("Fullscreen: erreur stream %s — %s", login, msg)

    def on_stream_stopped(self, login: str) -> None:
        """Signalé par StreamManager quand le stream est arrêté."""
        if login != self._current_login:
            return
        self._mpv.stop()

    def clear_stream(self) -> None:
        if self._pip_active:
            self._close_donate_view()
        self._ad_watcher.unwatch(self._current_login)
        self._dismiss_ad_banner()
        self._ad_active = False
        self._mpv.stop()
        self._current_login = ""
        self._current_game = ""
        self._current_viewers = 0
        self._current_donation_url = ""
        self._loading = False
        self._donate_btn.hide()
        self._hint_lbl.setText("Sélectionnez un stream dans la grille")
        self._hint_lbl.setStyleSheet("color: #555555;")
        self._hide_overlay()
        self._stack.setCurrentIndex(0)  # afficher l'état vide
        self._remote_menu.set_current_login("")

    @property
    def current_login(self) -> str:
        return self._current_login

    def update_viewers(self, viewers: int) -> None:
        self._current_viewers = viewers
        self._ov_viewers.setText(
            f"{self._fmt_viewers(viewers)} viewers" if viewers else "",
        )

    # ── vue donation (PiP) ───────────────────────────────────────────

    def _open_donate_view(self) -> None:
        """Passe en mode PiP : stream en miniature, vue web de donation en plein écran."""
        if not self._current_donation_url:
            return
        # macOS : QWebEngineView cohabite mal avec la surface OpenGL du lecteur
        # (vue vide, artefacts de composition). Le navigateur système est aussi
        # plus sûr : l'utilisateur voit l'URL réelle avant de payer.
        if sys.platform == "darwin":
            url = _safe_https_url(self._current_donation_url, _DONATION_HOSTS)
            if not url:
                logger.error(
                    "Don annulé : URL hors allowlist (%s)",
                    self._current_donation_url[:60],
                )
                return
            logger.info("Ouverture de la page de don dans le navigateur système")
            QDesktopServices.openUrl(QUrl(url))
            return

        self._pip_active = True
        self._hide_overlay()  # masque la barre overlay
        # Revalidation avant ouverture : la page est affichée sans barre d'adresse
        # et l'utilisateur y saisit des coordonnées de paiement.
        url = _safe_https_url(self._current_donation_url, _DONATION_HOSTS)
        if not url:
            logger.error(
                "Vue don annulée : URL hors allowlist (%s)",
                self._current_donation_url[:60],
            )
            self._pip_active = False
            self._show_overlay()
            return
        if _WEBENGINE_OK and hasattr(self._donate_view, "load"):
            self._donate_view.load(QUrl(url))  # type: ignore[attr-defined]
        self._donate_view.show()
        self._donate_close_btn.show()
        self._update_mpv_geometry()

    def _close_donate_view(self) -> None:
        """Quitte le mode PiP et restaure le stream en plein écran."""
        self._pip_active = False
        self._donate_view.hide()
        self._donate_close_btn.hide()
        self._update_mpv_geometry()

    # ── overlay animation ─────────────────────────────────────────────

    def _show_overlay(self) -> None:
        """Affiche l'overlay + bouton télécommande et (re)démarre le timer d'auto-hide."""
        if not self._current_login or self._pip_active or self._ad_active:
            return
        self._overlay.show()
        self._overlay.raise_()
        self._remote_btn.show()
        self._remote_btn.raise_()
        if self._opacity_fx.opacity() < 1.0:
            self._fade_anim.stop()
            self._fade_anim.setStartValue(self._opacity_fx.opacity())
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
            self._btn_opacity_fx.setOpacity(1.0)
        # Relance le compteur 3s à chaque mouvement
        self._hide_timer.start()

    def _start_fade_out(self) -> None:
        """Déclenche le fondu de disparition (appelé par le timer)."""
        if not self._overlay.isVisible():
            return
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity_fx.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_fade_finished(self) -> None:
        """Cache le widget une fois le fondu terminé."""
        if self._opacity_fx.opacity() == 0.0:
            self._overlay.hide()
            self._btn_opacity_fx.setOpacity(0.0)
            self._remote_btn.hide()

    def _hide_overlay(self) -> None:
        """Cache l'overlay + bouton immédiatement (appelé par clear_stream)."""
        self._hide_timer.stop()
        self._fade_anim.stop()
        self._opacity_fx.setOpacity(0.0)
        self._overlay.hide()
        self._btn_opacity_fx.setOpacity(0.0)
        self._remote_btn.hide()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._show_overlay()  # re-affiche si masqué (ex: après clear_stream)
        super().mouseMoveEvent(event)

    # ── resize ────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._update_mpv_geometry()
        self._remote_btn.move(8, 8)
        self._remote_btn.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._update_mpv_geometry()

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        """Propager les mouvements souris depuis MpvWidget vers la fenêtre."""
        if obj is self._mpv and event.type() == QEvent.Type.MouseMove:
            self._show_overlay()
        return super().eventFilter(obj, event)

    # ── helpers ───────────────────────────────────────────────────────

    # ── ad-break notifications ────────────────────────────────────────

    def _on_ad_detected(self, login: str) -> None:
        """Pub détectée sur le stream en cours → afficher le bandeau."""
        if login != self._current_login:
            return
        self._ad_active = True
        self._hide_overlay()  # masque l'overlay immédiatement
        if self._ad_banner is not None:
            return  # déjà affiché
        cw = self.centralWidget()
        if cw is None:
            return
        self._ad_banner = _AdBreakBanner(login, cw)
        self._ad_banner.notify_requested.connect(self._on_ad_notify_requested)
        self._ad_banner.dismissed.connect(self._dismiss_ad_banner)
        self._ad_banner.reposition(cw.width(), cw.height())

    def _on_ad_ended(self, login: str) -> None:
        """Pub terminée — dismiss le bandeau si présent, notifie si demandé."""
        if login == self._current_login:
            self._ad_active = False
            self._dismiss_ad_banner()
        if login in self._ad_notify_logins:
            self._ad_notify_logins.discard(login)
            cw = self.centralWidget()
            if cw is None:
                return
            toast = _AdEndToast(login, cw)
            toast.switch_requested.connect(self.stream_change_requested)
            toast.show()
            toast.raise_()

    def _on_ad_notify_requested(self, login: str) -> None:
        """L'utilisateur a demandé à être notifié quand la pub est terminée."""
        self._ad_notify_logins.add(login)

    def _dismiss_ad_banner(self) -> None:
        if self._ad_banner is not None:
            self._ad_banner.dismiss_animated()
            self._ad_banner = None

    # ── hype toast ────────────────────────────────────────────────────

    def show_hype_alert(self, login: str, label: str, score: float, color: str) -> None:
        """Affiche un toast discret en haut à droite (appelé depuis GridWindow)."""
        cw = self.centralWidget()
        if cw is None:
            return
        toast = _FsHypeToast(login, label, score, color, cw)
        toast.show()
        toast.raise_()

    @staticmethod
    def _fmt_viewers(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n) if n > 0 else ""

    def _save_clip(self) -> None:
        """Sauvegarde les N dernières secondes du stream fullscreen selon la config clips."""
        secs   = self._clip_config.get("duration_secs", 60)
        folder = self._clip_config.get("directory", "")
        path   = self._mpv.save_clip(secs, folder)
        original = "⏺ Clip"
        if path:
            self._clip_btn.setText("✓ Clip sauvé !")
        else:
            self._clip_btn.setText("✗ Erreur")
        QTimer.singleShot(3000, lambda: self._clip_btn.setText(original))

    # ── volume ─────────────────────────────────────────────────────

    def _apply_volume(self) -> None:
        """Répercute volume et mute sur MPV et rafraîchit l'icône."""
        self._mpv.set_volume(0 if self._muted else self._volume)
        self._mpv.set_mute(self._muted)
        if self._muted or self._volume == 0:
            icon, tip = "\U0001f507", "Rétablir le son"
        elif self._volume < 50:
            icon, tip = "\U0001f509", "Couper le son"
        else:
            icon, tip = "\U0001f50a", "Couper le son"
        self._vol_btn.setText(icon)
        self._vol_btn.setToolTip(tip)
        self._vol_slider.setToolTip(f"Volume : {self._volume} %")

    def _on_volume_changed(self, value: int) -> None:
        self._volume = int(value)
        if self._volume > 0:
            self._muted = False
        self._apply_volume()
        self._vol_save_timer.start()

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._apply_volume()
        self._vol_save_timer.start()

    def set_volume(self, value: int) -> None:
        """Règle le volume depuis l'extérieur (clavier, télécommande)."""
        self._vol_slider.setValue(max(0, min(100, int(value))))

    def _persist_volume(self) -> None:
        _save_settings({"volume": self._volume, "muted": self._muted})

    def set_clip_config(self, cfg: dict) -> None:
        """Met à jour la config clips (appelé depuis main.py sur settings_changed)."""
        self._clip_config = cfg.get("clips", {})

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Up:
            self._remote_menu.select_previous()
        elif key == Qt.Key.Key_Down:
            self._remote_menu.select_next()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Space):
            self._remote_menu.confirm_selection()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.set_volume(self._volume + 5)
            self._show_overlay()
        elif key == Qt.Key.Key_Minus:
            self.set_volume(self._volume - 5)
            self._show_overlay()
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
            self._show_overlay()
        elif key == Qt.Key.Key_Escape:
            if self._remote_menu.isVisible():
                self._remote_menu.hide_menu()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    # ── new features ───────────────────────────────────────────────

    def _update_mpv_geometry(self) -> None:
        """Recalcule les géométries absolues selon la visibilité du chat / mode PiP."""
        w, h = self.width(), self.height()
        chat_w = self._chat_panel._width if self._chat_panel._visible else 0
        video_w = w - chat_w

        if self._pip_active:
            _PIP_W, _PIP_H, _MARGIN = 320, 180, 16
            pip_x = video_w - _PIP_W - _MARGIN
            pip_y = h - _PIP_H - _MARGIN
            self._stack.setGeometry(pip_x, pip_y, _PIP_W, _PIP_H)
            self._donate_view.setGeometry(0, 0, w, h)
            # Bouton fermer en haut à droite de la fenêtre
            btn_w = self._donate_close_btn.sizeHint().width()
            self._donate_close_btn.move(w - btn_w - 16, 16)
            self._stack.raise_()           # PiP au premier plan
            self._donate_close_btn.raise_()
        else:
            self._stack.setGeometry(0, 0, video_w, h)
            self._overlay.setGeometry(0, h - 56, video_w, 56)

            self._remote_btn.move(8, 8)
            self._remote_btn.raise_()

            if self._remote_menu.isVisible():
                self._remote_menu.setGeometry(
                    self._remote_menu.x(), 0, REMOTE_MENU_WIDTH, h
                )

            if self._chat_panel._visible:
                self._chat_panel._update_geometry()

        if self._ad_banner is not None:
            self._ad_banner.reposition(w, h)

        # Backend de rendu (macOS) : la surface OpenGL doit être repeinte après
        # tout changement de géométrie, sinon elle affiche l'ancienne frame.
        if self._mpv.uses_render_backend:
            self._mpv.update()

    def update_remote_menu(
        self,
        streamers: list[StreamerInfo],
        selected_logins: list[str],
    ) -> None:
        """Met à jour la liste du menu télécommande."""
        self._remote_menu.update_streamers(
            streamers, selected_logins, self._current_login
        )

    def _toggle_remote_menu(self) -> None:
        """Bascule le menu télécommande et maintient l'overlay visible à l'ouverture."""
        if self._remote_menu.isVisible():
            self._remote_menu.hide_menu()
        else:
            self._show_overlay()  # montre l'overlay avant que le menu slide-in
            self._remote_menu.show_menu()

    def _on_remote_stream_selected(self, login: str) -> None:
        """Relaye la sélection du menu télécommande via stream_change_requested."""
        self.stream_change_requested.emit(login)

    def _toggle_chat(self) -> None:
        self._chat_panel.toggle()
        self._chat_btn.setChecked(self._chat_panel._visible)
        self._update_mpv_geometry()
