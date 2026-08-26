# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fenêtre fullscreen — stream principal + overlay info (écran centre)."""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import tempfile
import threading
import pathlib
import time
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
    QSize,)
from PyQt6.QtGui import (
    QBitmap,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QRegion,
    QScreen,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
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

try:
    import qtawesome as qta
    _QTA_OK = True
except Exception:  # noqa: BLE001
    # Pas seulement ImportError : qtawesome charge des polices et peut
    # échouer autrement. Un except trop étroit laissait _QTA_OK non
    # défini, et le démarrage plantait par NameError une fois sur six.
    qta = None  # type: ignore[assignment]
    _QTA_OK = False

from widgets.mpv_widget import MpvWidget
from widgets.bigscreen_widget import load_avatar_into_label as _load_avatar_into_label
from core.ad_watcher import AdWatcher
from core.api_client import _DONATION_HOSTS, _safe_https_url
from core.paths import CONFIG_PATH
from core.win_foreground import ceder_premier_plan, remonter_navigateur
from widgets.command_palette import CommandPalette
from core.win_fullscreen import mark_fullscreen

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
    except Exception:
        logger.exception("Sauvegarde de %s impossible", CONFIG_PATH.name)

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

# Fragments de feuille de style et libellés répétés. Regroupés ici pour n'avoir
# qu'un seul endroit à toucher quand la charte change.
_FOND_TRANSPARENT = "background: transparent;"
_FOND_TRANSPARENT_SANS_BORDURE = "background: transparent; border: none;"
_TEXTE_GRIS = "color: #555555; background: transparent;"
_TEXTE_VERT = "color: #00ff87; background: transparent;"
_POLICE_UI = "Segoe UI"
_POLICE_UI_VARIABLE = "Segoe UI Variable"
_ICONE_VOLUME = "mdi6.volume-high"
_LIBELLE_COUPER_SON = "Couper le son"


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

def _infobulle(texte: str) -> str:
    """Infobulle sûre : Qt y rend le texte riche, une balise y serait active."""
    return "<qt>" + html.escape(str(texte)) + "</qt>"


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
        avatar_container.setStyleSheet(_FOND_TRANSPARENT)

        initials = display[:1] if display else self._login[:1]
        self._avatar = AvatarLabel(self._login, initials, 52, avatar_container)
        self._avatar.move(0, 0)

        dot = LiveDot(8, avatar_container)
        dot.move(52 - 10, 52 - 10)  # bas-droite

        outer.addWidget(avatar_container)

        # ── Infos ──────────────────────────────────────────────────────
        info_col = QWidget()
        info_col.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info_col.setStyleSheet(_FOND_TRANSPARENT)
        col = QVBoxLayout(info_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        name_color = "#00ff87" if self._is_current else "#ffffff"
        # _build ne reçoit pas le login : il est porté par l'instance. Sans
        # cela, un streamer sans nom d'affichage levait un NameError.
        name_lbl = QLabel(display if display else self._login)
        name_lbl.setFont(QFont(_POLICE_UI, 14, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {name_color}; background: transparent;")
        col.addWidget(name_lbl)
        self._name_lbl = name_lbl

        game_lbl = QLabel(game if game else "—")
        game_lbl.setFont(QFont(_POLICE_UI, 11))
        game_lbl.setStyleSheet("color: #888888; background: transparent;")
        col.addWidget(game_lbl)

        title_font = QFont(_POLICE_UI, 10)
        title_lbl = QLabel()
        title_lbl.setFont(title_font)
        title_lbl.setStyleSheet(_TEXTE_GRIS)
        title_lbl.setWordWrap(False)
        title_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        title_lbl.setTextFormat(Qt.TextFormat.PlainText)
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
            title_lbl.setToolTip(_infobulle(self._raw_title))

        v_text = f"{_fmt_viewers(viewers)} viewers" if viewers > 0 else ""
        v_lbl = QLabel(v_text)
        v_lbl.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        v_lbl.setStyleSheet(_TEXTE_VERT)
        col.addWidget(v_lbl)

        outer.addWidget(info_col, stretch=1)

        # ── Bouton Watch (caché par défaut) ────────────────────────────
        self._watch_btn = QPushButton("▶")
        self._watch_btn.setFixedSize(32, 32)
        self._watch_btn.setFont(QFont(_POLICE_UI, 16))
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
        info_col.setStyleSheet(_FOND_TRANSPARENT)
        col = QVBoxLayout(info_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(6)
        name_lbl = QLabel(display if display else self._login)
        name_lbl.setFont(QFont(_POLICE_UI, 12))
        name_lbl.setStyleSheet("color: #aaaaaa; background: transparent;")
        row1.addWidget(name_lbl)
        v_text = _fmt_viewers(viewers)
        v_lbl = QLabel(v_text)
        v_lbl.setFont(QFont("Consolas", 11))
        v_lbl.setStyleSheet(_TEXTE_VERT)
        row1.addWidget(v_lbl)
        row1.addStretch()
        col_widget1 = QWidget()
        col_widget1.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        col_widget1.setStyleSheet(_FOND_TRANSPARENT)
        col_widget1.setLayout(row1)
        col.addWidget(col_widget1)

        if game:
            # Largeur disponible : 320 - marges(24) - avatar(36) - spacing(10) - scrollbar(6)
            _avail_g = REMOTE_MENU_WIDTH - 76
            _game_font = QFont(_POLICE_UI, 10)
            game_text = QFontMetrics(_game_font).elidedText(
                game, Qt.TextElideMode.ElideRight, _avail_g
            )
        else:
            _game_font = QFont(_POLICE_UI, 10)
            game_text = ""
        game_lbl = QLabel(game_text)
        game_lbl.setTextFormat(Qt.TextFormat.PlainText)
        game_lbl.setFont(_game_font)
        game_lbl.setStyleSheet(_TEXTE_GRIS)
        if game:
            game_lbl.setToolTip(_infobulle(game))
        col.addWidget(game_lbl)

        outer.addWidget(info_col, stretch=1)

        # Bouton Watch
        self._watch_btn = QPushButton("▶")
        self._watch_btn.setFixedSize(28, 28)
        self._watch_btn.setFont(QFont(_POLICE_UI, 14))
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
    w.setStyleSheet(_FOND_TRANSPARENT)
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
        self._count_lbl.setFont(QFont(_POLICE_UI, 11))
        self._count_lbl.setStyleSheet(_TEXTE_GRIS)
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
        self._container.setStyleSheet(_FOND_TRANSPARENT)
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

    def _vider(self) -> None:
        """Retire les items et tout le contenu du container, sauf le stretch."""
        for item in self._items:
            item.deleteLater()
        self._items.clear()
        self._login_list.clear()
        while self._container_layout.count() > 1:
            child = self._container_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _inserer_section(self, idx: int, titre: str, streamers: list, fabrique) -> int:
        """Insère un titre de section puis ses items, séparateurs compris.

        Renvoie l'index d'insertion suivant. Une section vide ne pose ni titre
        ni séparateur, et rend l'index inchangé.

        `fabrique` construit l'item ET l'enregistre dans _items / _login_list :
        c'est elle qui décide de la taille (grande ou petite ligne).
        """
        if not streamers:
            return idx
        self._container_layout.insertWidget(idx, _make_section_sep(titre))
        idx += 1
        dernier = len(streamers) - 1
        for i, s in enumerate(streamers):
            self._container_layout.insertWidget(idx, fabrique(s))
            idx += 1
            if i < dernier:
                self._container_layout.insertWidget(idx, _make_item_sep())
                idx += 1
        return idx

    def _rebuild(self) -> None:
        self._vider()

        sel_set = set(self._last_selected)
        live = [s for s in self._last_streamers if s.online]
        selected_live = [s for s in live if s.twitch_login in sel_set]
        other_live = [s for s in live if s.twitch_login not in sel_set]

        self._count_lbl.setText(f"{len(live)} en live")

        idx = self._inserer_section(0, "SÉLECTIONNÉS", selected_live,
                                    self._make_large_item)
        self._inserer_section(idx, "AUTRES LIVE", other_live,
                              self._make_small_item)

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
        if obj is not self._handle:
            return super().eventFilter(obj, event)  # type: ignore[arg-type]
        ev = event  # type: ignore[assignment]
        if (ev.type() == QEvent.Type.MouseButtonPress
                and ev.button() == Qt.MouseButton.LeftButton):
            self._drag_start_x = ev.globalPosition().x()
            self._drag_start_width = self._width
            return True
        if (ev.type() == QEvent.Type.MouseMove
                and ev.buttons() & Qt.MouseButton.LeftButton):
            self._redimensionner(ev.globalPosition().x())
            return True
        # Bouton autre que le gauche, ou survol sans glisser : l'événement
        # continue son chemin, comme avant.
        return super().eventFilter(obj, event)  # type: ignore[arg-type]

    def _redimensionner(self, x_souris: float) -> None:
        """Nouvelle largeur du panneau pendant un glisser, bornée à 250-600 px.

        Le plein écran doit recaler sa vidéo dans la foulée : sans ça, l'image
        garde l'ancienne largeur jusqu'au prochain redimensionnement.
        """
        delta = int(self._drag_start_x - x_souris)
        self._width = max(250, min(600, self._drag_start_width + delta))
        self._update_geometry()
        p = self.parent()
        fs = p.parent() if p is not None else None
        if fs is not None and hasattr(fs, "_update_mpv_geometry"):
            fs._update_mpv_geometry()  # type: ignore[union-attr]


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
        icon.setFont(QFont(_POLICE_UI, 14))
        icon.setStyleSheet(_FOND_TRANSPARENT_SANS_BORDURE)
        h.addWidget(icon)

        self._msg = QLabel(f"<b>{html.escape(login)}</b> — Publicité en cours — 0:00")
        self._msg.setFont(QFont(_POLICE_UI_VARIABLE, 11))
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
        dot.setFont(QFont(_POLICE_UI, 9))
        dot.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        top.addWidget(dot)
        title = QLabel(f"<b>{html.escape(login)}</b> — Pub terminée !")
        title.setFont(QFont(_POLICE_UI_VARIABLE, 11))
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
# Toast « favori en direct »
# ---------------------------------------------------------------------------

def ouvrir_page_de_don(url: str) -> bool:
    """Ouvre une page de don dans le NAVIGATEUR, après contrôle de l'hôte.

    Hors de l'application, volontairement : la vue web intégrée n'a pas de
    barre d'adresse, alors que l'utilisateur va y saisir des coordonnées de
    paiement. Dans son navigateur il voit l'URL réelle et son cadenas.
    """
    if not url:
        return False
    sur = _safe_https_url(url, _DONATION_HOSTS)
    if not sur:
        logger.error("Don annulé : URL hors allowlist (%s)", str(url)[:60])
        return False
    # Le verrou de premier plan réserve SetForegroundWindow au processus qui
    # détient déjà le premier plan : sans cette cession, le navigateur ne peut
    # que faire clignoter son bouton dans la barre des tâches.
    ceder_premier_plan()
    QDesktopServices.openUrl(QUrl(sur))
    # Deux tentatives : le délai entre le clic et l'apparition de l'onglet
    # dépend de la machine et du navigateur.
    for delai in (400, 1200):
        QTimer.singleShot(delai, remonter_navigateur)
    return True


class _FavoriteLiveToast(QWidget):
    """Annonce en haut à droite qu'un favori vient de lancer son direct.

    Une bascule automatique serait insupportable : elle vous arracherait à ce
    que vous regardez au pire moment. Le toast propose, il n'impose pas — et la
    barre qui se vide dit combien de temps il reste pour décider, plutôt que de
    disparaître sans prévenir.
    """

    switch_requested = pyqtSignal(str)   # login

    _W, _H = 320, 76
    _LIFE_MS = 12_000            # durée avant disparition si on ne clique pas
    _TICK_MS = 50

    def __init__(self, login: str, display: str, parent: QWidget,
                 top_offset: int = 0) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._login = login
        self.setFixedSize(self._W, self._H)
        self.setStyleSheet(
            "QWidget#favToast { background: rgba(8,8,8,228);"
            " border: 1px solid #f5c518; border-radius: 6px; }"
        )
        self.setObjectName("favToast")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QWidget()
        body.setStyleSheet(_FOND_TRANSPARENT)
        hl = QHBoxLayout(body)
        hl.setContentsMargins(10, 8, 12, 4)
        hl.setSpacing(10)

        av = AvatarLabel(login, (display or login)[:2], 34, body)
        av.load_async("")
        hl.addWidget(av)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        star = QLabel()
        star.setFixedSize(12, 12)
        if _QTA_OK:
            star.setPixmap(qta.icon("mdi6.star", color="#f5c518").pixmap(12, 12))
        star.setStyleSheet(_FOND_TRANSPARENT_SANS_BORDURE)
        top.addWidget(star)
        title = QLabel(display or login)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setFont(QFont(_POLICE_UI_VARIABLE, 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top.addWidget(title, stretch=1)
        col.addLayout(top)
        self._sub = QLabel("vient de passer en direct")
        self._sub.setFont(QFont(_POLICE_UI_VARIABLE, 9))
        self._sub.setStyleSheet("color: #9a9a9a; background: transparent; border: none;")
        col.addWidget(self._sub)
        hl.addLayout(col, stretch=1)

        watch = QPushButton("Regarder")
        watch.setFixedHeight(26)
        watch.setCursor(Qt.CursorShape.PointingHandCursor)
        watch.setStyleSheet(
            "QPushButton { background: #f5c518; color: #1a1400; border: none;"
            " border-radius: 5px; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #ffd84d; }"
        )
        watch.clicked.connect(self._on_watch)
        self._boutons = QHBoxLayout()
        self._boutons.setContentsMargins(0, 0, 0, 0)
        self._boutons.setSpacing(6)
        self._boutons.addWidget(watch, 0, Qt.AlignmentFlag.AlignVCenter)
        hl.addLayout(self._boutons)
        root.addWidget(body, stretch=1)

        # Barre de temps restant : 3 px sur toute la largeur, en bas.
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(3)
        self._bar.setRange(0, self._LIFE_MS)
        self._bar.setValue(self._LIFE_MS)
        self._bar.setStyleSheet(
            "QProgressBar { background: #2a2a2a; border: none;"
            " border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }"
            "QProgressBar::chunk { background: #f5c518; }"
        )
        root.addWidget(self._bar)

        self._left = self._LIFE_MS
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.move(parent.width() - self._W - 16, 16 + top_offset)
        self.show()
        self.raise_()

    def set_message(self, texte: str, couleur: str = "") -> None:
        """Réutilise le toast pour une autre annonce que « en direct »."""
        self._sub.setTextFormat(Qt.TextFormat.PlainText)
        self._sub.setText(texte)
        if couleur:
            self.setStyleSheet(
                "QWidget#favToast { background: rgba(8,8,8,228);"
                f" border: 1px solid {couleur}; border-radius: 6px; }}"
            )
            self._bar.setStyleSheet(
                "QProgressBar { background: #2a2a2a; border: none;"
                " border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }"
                f"QProgressBar::chunk {{ background: {couleur}; }}"
            )

    def _tick(self) -> None:
        self._left -= self._TICK_MS
        if self._left <= 0:
            self._timer.stop()
            self._fade_out()
            return
        self._bar.setValue(self._left)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        # Le décompte s'arrête sous le curseur : viser un bouton qui s'échappe
        # est le meilleur moyen de rater ce qu'on voulait.
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._left > 0:
            self._timer.start()
        super().leaveEvent(event)

    def add_donate_button(self, url: str) -> None:
        """Ajoute un bouton « Donner » à côté de « Regarder »."""
        self._donate_url = url
        b = QPushButton("Donner")
        b.setFixedHeight(26)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton { background: transparent; color: #00ff87;"
            " border: 1px solid #00ff87; border-radius: 5px;"
            " font-weight: bold; padding: 0 10px; }"
            "QPushButton:hover { background: #0f1a14; }"
        )
        b.clicked.connect(self._on_donate)
        self._boutons.insertWidget(0, b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedWidth(self._W + 74)
        parent = self.parentWidget()
        if parent is not None:
            self.move(parent.width() - self.width() - 16, self.y())

    def _on_donate(self) -> None:
        self._timer.stop()
        ouvrir_page_de_don(getattr(self, "_donate_url", ""))
        self.close()

    def _on_watch(self) -> None:
        self._timer.stop()
        self.switch_requested.emit(self._login)
        self.close()

    def _fade_out(self) -> None:
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(500)
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
        name_lbl.setFont(QFont(_POLICE_UI_VARIABLE, 10, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        name_lbl.setMaximumWidth(90)
        h.addWidget(name_lbl)

        sep = QLabel("·")
        sep.setFont(QFont(_POLICE_UI_VARIABLE, 10))
        sep.setStyleSheet("color: #555555; background: transparent; border: none;")
        h.addWidget(sep)

        lbl = QLabel(label)
        # Texte produit par le LLM à partir du chat : texte brut obligatoire.
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setFont(QFont(_POLICE_UI_VARIABLE, 10))
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
# PinnedAudioOverlay — chaînes dont l'audio est épinglé
# ---------------------------------------------------------------------------

_PIN_ROW_H = 30
_PIN_OV_W = 224
_PIN_HEAD_H = 14         # bandeau « AUDIO ÉPINGLÉ »
_PIN_MARGIN_T = 8
_PIN_MARGIN_B = 10
_PIN_GAP = 6             # entre le bandeau et la liste
_PIN_ROW_GAP = 4         # entre deux lignes


class PinnedAudioOverlay(QWidget):
    """Liste, en haut à droite, des chaînes dont l'audio est épinglé.

    Le son de la grille vient de cellules qu'on ne regarde pas forcément :
    l'épinglage restait invisible depuis le plein écran, et on ne savait pas
    ce qu'on entendait. Chaque entrée porte la photo de profil et le nom de la
    chaîne, empilées comme une liste de participants.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("pinnedAudio")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Purement informatif : sans cela l'overlay avalerait les clics destinés
        # à la vidéo qu'il recouvre.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "QWidget#pinnedAudio { background: rgba(10,10,10,190); "
            "border: 1px solid #2a2a2a; border-radius: 8px; }"
        )
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(10, _PIN_MARGIN_T, 12, _PIN_MARGIN_B)
        self._v.setSpacing(_PIN_GAP)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        icon = QLabel()
        icon.setFixedSize(12, 12)
        if _QTA_OK:
            icon.setPixmap(qta.icon(_ICONE_VOLUME, color="#00ff87").pixmap(12, 12))
        icon.setStyleSheet(_FOND_TRANSPARENT_SANS_BORDURE)
        head.addWidget(icon)
        title = QLabel("AUDIO ÉPINGLÉ")
        title.setFont(QFont(_POLICE_UI_VARIABLE, 7, QFont.Weight.Bold))
        title.setStyleSheet(
            "color: #00ff87; background: transparent; border: none; "
            "letter-spacing: 1px;"
        )
        head.addWidget(title)
        head.addStretch()
        self._v.addLayout(head)

        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(_PIN_ROW_GAP)
        self._v.addLayout(self._rows)

        self._shown: list[str] = []
        self._muted: set[str] = set()
        self._elements: dict[str, tuple[QLabel, QLabel]] = {}
        self.setFixedWidth(_PIN_OV_W)
        self.hide()

    # -- API -------------------------------------------------------------

    def set_logins(self, logins: list[str]) -> None:
        """Remplace la liste affichée. Sans effet si elle n'a pas changé."""
        logins = [str(lg) for lg in logins if lg]
        if logins == self._shown:
            return
        self._shown = logins
        while self._rows.count():
            item = self._rows.takeAt(0)
            w = item.widget()
            if w is not None:
                # setParent(None) avant deleteLater : le seul deleteLater laisse
                # le widget se peindre jusqu'au retour à la boucle d'événements.
                w.hide()   # avant de détacher : détaché et visible = une fenêtre
                w.setParent(None)
                w.deleteLater()
        self._elements.clear()
        for lg in logins:
            self._rows.addWidget(self._make_row(lg))
        self._apply_muted()
        self.setVisible(bool(logins))
        # Hauteur calculée, pas mesurée : sizeHint() interrogé dans la foulée
        # renvoie encore celle du bandeau seul — les lignes qu'on vient
        # d'ajouter ne sont prises en compte qu'au tour de boucle suivant. La
        # boîte se figeait alors à 31 px et tronquait toute la liste.
        n = len(logins)
        height = (_PIN_MARGIN_T + _PIN_HEAD_H + _PIN_GAP
                  + n * _PIN_ROW_H + max(0, n - 1) * _PIN_ROW_GAP
                  + _PIN_MARGIN_B)
        self.setFixedSize(_PIN_OV_W, height)
        parent = self.parentWidget()
        if parent is not None:
            self.reposition(parent.width(), parent.height())

    def reposition(self, w: int, h: int) -> None:
        self.move(max(0, w - self.width() - 8), 8)

    def set_muted(self, logins: set) -> None:
        """Grise les chaînes coupées depuis la console.

        Sans cela, la liste affirme qu'on entend une chaîne qui est en fait
        silencieuse — exactement l'information qu'elle est censée donner.
        """
        self._muted = {str(x) for x in logins}
        self._apply_muted()

    def _apply_muted(self) -> None:
        """Applique l'état de coupure aux lignes ACTUELLES.

        Pas de raccourci « rien n'a changé » : les lignes sont recréées à
        chaque reconstruction de la liste, et un état inchangé devait quand
        même être repeint sur des widgets neufs.
        """
        coupees = self._muted
        for login, (icone, nom) in self._elements.items():
            mute = login in coupees
            nom.setStyleSheet(
                f"color: {'#5a5a5a' if mute else '#e8e8e8'};"
                " background: transparent; border: none;")
            if _QTA_OK:
                icone.setPixmap(qta.icon(
                    "mdi6.volume-off" if mute else _ICONE_VOLUME,
                    color="#ff4444" if mute else "#00ff87").pixmap(12, 12))
            icone.setVisible(True)

    def height_hint(self) -> int:
        """Hauteur occupée, 0 si masqué — pour décaler ce qui vient dessous."""
        return self.height() if self.isVisible() else 0

    # -- interne ---------------------------------------------------------

    def _make_row(self, login: str) -> QWidget:
        row = QWidget()
        row.setFixedHeight(_PIN_ROW_H)
        row.setStyleSheet(_FOND_TRANSPARENT)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        av = AvatarLabel(login, login[:2], 24, row)
        av.load_async("")
        h.addWidget(av)

        etat = QLabel()
        etat.setFixedSize(12, 12)
        etat.setStyleSheet(_FOND_TRANSPARENT_SANS_BORDURE)
        if _QTA_OK:
            etat.setPixmap(qta.icon(_ICONE_VOLUME,
                                    color="#00ff87").pixmap(12, 12))
        h.addWidget(etat)

        name = QLabel(login)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setFont(QFont(_POLICE_UI_VARIABLE, 10, QFont.Weight.Bold))
        name.setStyleSheet("color: #e8e8e8; background: transparent; border: none;")
        fm = QFontMetrics(name.font())
        name.setText(fm.elidedText(login, Qt.TextElideMode.ElideRight, _PIN_OV_W - 60))
        h.addWidget(name, stretch=1)
        self._elements[login] = (etat, name)
        return row


# ---------------------------------------------------------------------------
# Replay — le direct passe en incrustation, l'action repasse en grand
# ---------------------------------------------------------------------------

_REPLAY_PIP_W, _REPLAY_PIP_H, _REPLAY_MARGIN = 320, 180, 16
#: Attente maximale de fin d'écriture du fichier par dump-cache.
_REPLAY_DUMP_TIMEOUT_S = 6.0


class _PetitAnneau(QWidget):
    """Anneau qui tourne, seize pixels. Rien d'autre.

    Un texte figé « Reprise en cours » n'apprend rien : il est indiscernable
    d'une application bloquée. Ce qui tourne dit que quelque chose avance.
    """

    _PAS = 12          # degrés par image
    _TICK = 33         # ms — 30 images par seconde suffisent pour un anneau
    _ARC = 100         # ouverture de l'arc, en degrés

    def __init__(self, taille: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(taille, taille)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._angle = 0
        self._minuterie = QTimer(self)
        self._minuterie.setInterval(self._TICK)
        self._minuterie.timeout.connect(self._tourner)
        self._minuterie.start()

    def _tourner(self) -> None:
        self._angle = (self._angle + self._PAS) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        marge = 2
        boite = self.rect().adjusted(marge, marge, -marge, -marge)
        # Qt compte en seizièmes de degré, et dans le sens trigonométrique.
        stylo = QPen(QColor(255, 107, 0, 60), 2)
        p.setPen(stylo)
        p.drawArc(boite, 0, 360 * 16)
        stylo.setColor(QColor("#ff6b00"))
        p.setPen(stylo)
        p.drawArc(boite, -self._angle * 16, self._ARC * 16)
        p.end()


class _ReplayLoader(QWidget):
    """Bandeau d'attente pendant qu'on reprend le moment chez Twitch.

    La reprise en pleine qualité demande quelques secondes de réseau : sans
    ce bandeau, le clic sur « Revoir les dernières secondes » ne produisait
    rien de visible, et on le refaisait en croyant l'avoir manqué.

    Cliquable pour renoncer : une attente qu'on ne peut pas interrompre est
    une attente qu'on subit.
    """

    annulation_demandee = pyqtSignal()

    def __init__(self, login: str, secs: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QWidget { background: rgba(10,10,10,200); border: 1px solid #ff6b00;"
            " border-radius: 6px; }"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(10)
        anneau = _PetitAnneau(16, self)
        anneau.setStyleSheet("background: transparent; border: none;")
        h.addWidget(anneau)
        quoi = QLabel(f"Reprise des {secs} dernières secondes de {login}…")
        quoi.setTextFormat(Qt.TextFormat.PlainText)
        quoi.setFont(QFont(_POLICE_UI_VARIABLE, 10))
        quoi.setStyleSheet("color: #e0e0e0; background: transparent; border: none;")
        h.addWidget(quoi)
        renoncer = QLabel("✕  Échap")
        renoncer.setFont(QFont(_POLICE_UI_VARIABLE, 10, QFont.Weight.Bold))
        renoncer.setStyleSheet("color: #888888; background: transparent; border: none;")
        h.addWidget(renoncer)
        self.adjustSize()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.annulation_demandee.emit()
        event.accept()


class _ReplayProgress(QWidget):
    """Fine barre de progression du replay, collée en haut de l'écran.

    Trois pixels : assez pour situer où l'on en est, assez peu pour ne rien
    voler à l'image. Pas de graduation ni de texte — c'est un repère, pas un
    lecteur.

    La progression est FOURNIE par l'appelant, jamais calculée à partir de la
    durée du fichier : un MP4 fragmenté repris chez Twitch annonce la durée
    depuis le début du direct, soit des heures, et le rapport serait à 99 %
    dès la première image.
    """

    HAUTEUR = 3

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(self.HAUTEUR)
        self._ratio = 0.0

    def set_ratio(self, ratio: float) -> None:
        borne = max(0.0, min(1.0, ratio))
        if abs(borne - self._ratio) < 0.001:
            return          # inutile de repeindre pour un dixième de pixel
        self._ratio = borne
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        w = self.width()
        p.fillRect(0, 0, w, self.height(), QColor(255, 107, 0, 60))
        p.fillRect(0, 0, int(w * self._ratio), self.height(), QColor("#ff6b00"))
        p.end()


class _ReplayBadge(QWidget):
    """Bandeau « REPLAY » posé sur la vidéo rejouée.

    Cliquable : Échap ferme aussi le replay, mais rien ne le disait — et une
    sortie qu'on ne devine pas revient à ne pas en avoir.
    """

    fermeture_demandee = pyqtSignal()

    def __init__(self, login: str, secs: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QWidget { background: rgba(10,10,10,200); border: 1px solid #ff6b00;"
            " border-radius: 6px; }"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(8)
        dot = QLabel("REPLAY")
        dot.setFont(QFont(_POLICE_UI_VARIABLE, 10, QFont.Weight.Bold))
        dot.setStyleSheet("color: #ff6b00; background: transparent; border: none;"
                          " letter-spacing: 2px;")
        h.addWidget(dot)
        who = QLabel(f"{login} · {secs} dernières secondes")
        who.setTextFormat(Qt.TextFormat.PlainText)
        who.setFont(QFont(_POLICE_UI_VARIABLE, 10))
        who.setStyleSheet("color: #e0e0e0; background: transparent; border: none;")
        h.addWidget(who)
        sortie = QLabel("✕  Échap")
        sortie.setFont(QFont(_POLICE_UI_VARIABLE, 10, QFont.Weight.Bold))
        sortie.setStyleSheet("color: #888888; background: transparent; border: none;")
        h.addWidget(sortie)
        self.adjustSize()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.fermeture_demandee.emit()
        event.accept()


# ---------------------------------------------------------------------------
# FullscreenWindow
# ---------------------------------------------------------------------------

class FullscreenWindow(QMainWindow):
    """Fenêtre plein écran pour le stream principal.

    Overlay en bas (nom, jeu, viewers) visible au mouvement souris,
    masqué avec fade-out après 2 s d'inactivité.
    """

    stream_change_requested = pyqtSignal(str)
    #: Index (0-based) d'une cellule de la grille, demandé au clavier.
    slot_requested          = pyqtSignal(int)
    #: -1 / +1 — stream précédent ou suivant dans l'ordre de la grille.
    neighbour_requested     = pyqtSignal(int)
    stream_changed = pyqtSignal(str)  # emis quand le stream actif change (Bug 3)
    #: Volume et coupure du direct, APRÈS application — quelle que soit
    #: l'origine du changement : curseur de l'overlay, touches +/-/M, ou
    #: console de mixage. Sans ce retour, régler le son en plein écran laissait
    #: la tranche du mixer sur sa valeur d'avant.
    volume_changed = pyqtSignal(int)
    mute_changed = pyqtSignal(bool)
    #: Interne : le fil de reprise HD a fini. (chemin, secondes obtenues)
    _replay_hd_pret = pyqtSignal(str, float)
    #: Ctrl+Entrée dans la palette : ajouter la chaîne à la grille. Le plein
    #: écran ne tient pas la sélection, la demande remonte à main.py.
    grid_add_requested = pyqtSignal(str)

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
        # Replay : le direct passe en incrustation, l'action repasse en grand.
        self._replay_active: bool = False
        self._replay_player: MpvWidget | None = None
        self._replay_badge: QWidget | None = None
        self._replay_progress: QWidget | None = None
        self._replay_loader: QWidget | None = None
        #: Vrai entre une demande de replay et son abandon : le fil de reprise
        #: peut encore rendre un fichier, il ne doit plus être joué.
        self._replay_annule: bool = False
        self._replay_suivi: QTimer | None = None
        #: Position de la premiere image lue, en secondes. Un MP4 fragmente
        #: repris chez Twitch demarre a l'horodatage du direct : seule la
        #: distance a cette origine a un sens.
        self._replay_origine: float | None = None
        self._replay_path: str = ""
        self._replay_login: str = ""
        self._replay_secs: int = 30
        self._replay_size: int = -1
        self._replay_deadline: float = 0.0
        # Queued par construction : le fil de reprise emet depuis un
        # thread, la lecture doit repartir sur le fil graphique.
        self._replay_hd_pret.connect(self._sur_replay_hd)

        # Ad-break watcher
        self._ad_watcher = AdWatcher(self)
        self._ad_watcher.ad_detected.connect(self._on_ad_detected)
        self._ad_watcher.ad_ended.connect(self._on_ad_ended)
        self._ad_banner: _AdBreakBanner | None = None
        self._ad_notify_logins: set[str] = set()
        self._ad_active: bool = False
        # Table des touches, construite à la première frappe.
        self._touches: dict | None = None

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
        logo.setStyleSheet(_TEXTE_VERT)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        el.addWidget(logo)

        el.addSpacing(12)

        self._hint_lbl = QLabel("Sélectionnez un stream dans la grille")
        self._hint_lbl.setFont(QFont(_POLICE_UI, 16))
        self._hint_lbl.setStyleSheet(_TEXTE_GRIS)
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

        # La barre est en trois blocs : gauche et droite reçoivent la MÊME
        # part d'espace (stretch 1), ce qui place mathématiquement le bloc
        # central au milieu de la barre quelle que soit sa largeur.
        ol = QHBoxLayout(self._overlay)
        ol.setContentsMargins(20, 0, 20, 0)
        ol.setSpacing(0)

        _left = QWidget()
        _left.setStyleSheet(_FOND_TRANSPARENT)
        ll = QHBoxLayout(_left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        # Nom · jeu en premier, le volume vient après.
        self._ov_info = QLabel()
        self._ov_info.setFont(QFont(_POLICE_UI, 15))
        self._ov_info.setStyleSheet("color: #ffffff; background: transparent;")
        ll.addWidget(self._ov_info)
        ll.addSpacing(16)

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
        self._vol_btn.setToolTip(_LIBELLE_COUPER_SON)
        self._vol_btn.clicked.connect(self._toggle_mute)
        ll.addWidget(self._vol_btn)

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
        ll.addWidget(self._vol_slider)

        # Écriture différée : éviter un accès disque à chaque cran du curseur.
        self._vol_save_timer = QTimer(self)
        self._vol_save_timer.setSingleShot(True)
        self._vol_save_timer.setInterval(1500)
        self._vol_save_timer.timeout.connect(self._persist_volume)

        ll.addStretch()
        ol.addWidget(_left, stretch=1)

        # Centre : viewers
        self._ov_viewers = QLabel()
        self._ov_viewers.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        self._ov_viewers.setStyleSheet(_TEXTE_VERT)
        self._ov_viewers.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ol.addWidget(self._ov_viewers)

        _right = QWidget()
        _right.setStyleSheet(_FOND_TRANSPARENT)
        rl = QHBoxLayout(_right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addStretch()

        # Bouton chat
        self._chat_btn = QPushButton("Chat")
        if _QTA_OK:
            # U+1F4AC ne rend aucun glyphe hors Windows : le bouton Chat
            # apparaissait sans icône. On passe à qtawesome comme ailleurs.
            self._chat_btn.setIcon(qta.icon("mdi6.message-text-outline", color="#888888",
                                        color_active="#ffffff"))
            self._chat_btn.setIconSize(QSize(14, 14))
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
        rl.addWidget(self._chat_btn)

        rl.addSpacing(8)

        # Bouton donation
        self._donate_btn = QPushButton("Faire un don")
        if _QTA_OK:
            # U+1F4AC ne rend aucun glyphe hors Windows : le bouton Chat
            # apparaissait sans icône. On passe à qtawesome comme ailleurs.
            self._donate_btn.setIcon(qta.icon("mdi6.heart-outline", color="#888888",
                                        color_active="#ffffff"))
            self._donate_btn.setIconSize(QSize(14, 14))
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
        rl.addWidget(self._donate_btn)

        rl.addSpacing(8)

        # Bouton clip 60s
        self._clip_btn = QPushButton("Clip")
        if _QTA_OK:
            # U+1F4AC ne rend aucun glyphe hors Windows : le bouton Chat
            # apparaissait sans icône. On passe à qtawesome comme ailleurs.
            self._clip_btn.setIcon(qta.icon("mdi6.record-circle-outline", color="#888888",
                                        color_active="#ffffff"))
            self._clip_btn.setIconSize(QSize(14, 14))
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
        rl.addWidget(self._clip_btn)
        ol.addWidget(_right, stretch=1)

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

        self._pinned_muted: set[str] = set()
        self._pinned_audio = PinnedAudioOverlay(central)

        # Z-order : stack au fond, remote_btn au premier plan
        self._stack.lower()
        self._chat_panel.raise_()
        self._overlay.raise_()
        self._pinned_audio.raise_()
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

        # Palette de commandes (Ctrl+K). Pas d'onglets ici : en plein écran on
        # cherche une chaîne ou une action, il n'y a rien d'autre à atteindre.
        self._palette = CommandPalette(central, [])
        self._palette.stream_requested.connect(self.stream_change_requested)
        self._palette.grid_requested.connect(self.grid_add_requested)
        self._palette.action_requested.connect(self.run_action)
        # QShortcut plutôt que keyPressEvent : la frappe arrive d'abord au
        # widget qui a le focus, et la vidéo incrustée est une fenêtre native
        # qui ne fait pas remonter les touches jusqu'ici. Un raccourci de
        # portée FENÊTRE se déclenche quel que soit l'enfant qui a la main.
        raccourci = QShortcut(QKeySequence("Ctrl+K"), self)
        raccourci.setContext(Qt.ShortcutContext.WindowShortcut)
        raccourci.activated.connect(self._palette.open)

        self.setCentralWidget(central)

    def set_streamers(self, streamers: list) -> None:
        """Alimente la palette de commandes."""
        self._palette.set_streamers(streamers)

    def _move_to_screen(self, screen: QScreen) -> None:
        g = screen.geometry()
        self.setGeometry(g)
        self.show()  # crée le handle natif à la bonne position
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.showFullScreen()
        mark_fullscreen(self)
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
        # Le volume et la coupure sont ceux de la FENÊTRE, pas du flux : les
        # réimposer, sinon changer de streamer remettait le son à fond alors
        # que la barre affichait toujours le réglage précédent.
        self._apply_volume()
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
        """Ouvre la page de don dans le navigateur par défaut.

        Volontairement hors de l'application : la vue web intégrée n'a pas de
        barre d'adresse, alors que l'utilisateur va y saisir des coordonnées de
        paiement. Dans le navigateur il voit l'URL réelle et son cadenas.
        """
        # Une SEULE route vers le navigateur. Ce bouton avait sa propre copie
        # du contrôle d'allowlist et de la cession de premier plan : la copie
        # n'a pas suivi quand on a découvert que c'était l'ordre Z, et non le
        # focus, qui cachait la page — le bouton du plein écran restait donc
        # cassé alors que celui du panel était réparé.
        ouvrir_page_de_don(self._current_donation_url)

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
        # Seuil plutôt qu'égalité : l'interpolation de QPropertyAnimation
        # n'est pas tenue d'atterrir sur 0.0 au bit près.
        if self._opacity_fx.opacity() <= 0.001:
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

    # ── replay ────────────────────────────────────────────────────────

    def _meilleur_tampon(self, login: str, fourni: str) -> str:
        """Chemin du meilleur enregistrement disponible pour ce moment.

        La grille joue en 360p — c'est le prix de vingt-cinq flux simultanés —
        et son `dump-cache` ne peut restituer que ça : le tampon contient les
        octets réellement reçus, pas une source à ré-encoder. Un replay de
        cellule est donc en 360p, quoi qu'on fasse ensuite.

        Mais le plein écran, lui, joue la même chaîne en `best`. Quand c'est
        de celle-là qu'on veut revoir le moment, son tampon est disponible et
        bien meilleur. On le préfère, et on retombe sur le fichier fourni par
        la grille s'il n'a rien à offrir.
        """
        if not login or login != self.current_login or self._mpv is None:
            return fourni
        try:
            meilleur = self._mpv.save_clip(
                self._replay_secs_demandes(), tempfile.gettempdir())
        except Exception:      # noqa: BLE001 — on retombe sur la grille
            logger.exception("Replay : tampon du plein écran illisible")
            return fourni
        if not meilleur:
            logger.debug("Replay : tampon du plein écran vide, repli sur la grille")
            return fourni
        logger.info("Replay de %s pris sur le plein écran (qualité de lecture)",
                    login)
        return meilleur

    @staticmethod
    def _replay_secs_demandes() -> int:
        """Durée d'un replay : celle que la source peut réellement fournir.

        Volontairement décorrélée de la durée des clips : un clip vient du
        tampon local et peut être long, un replay est plafonné par la fenêtre
        que Twitch garde en ligne.
        """
        from core.replay_hd import REPLAY_SECS
        return REPLAY_SECS

    def start_replay(self, login: str, path: str, secs: int = 30) -> None:
        """Rejoue `path` en grand, le direct passant en incrustation.

        Le direct ne peut pas se rembobiner lui-même : reculer dans son cache
        le mettrait en pause et ferait décrocher le flux. Le moment est donc
        rejoué par un SECOND lecteur, tandis que le direct continue, réduit
        dans un coin.
        """
        cw = self.centralWidget()
        if cw is None or self._replay_active:
            return
        self._replay_login = login
        self._replay_secs = secs
        self._replay_annule = False
        # Montré tout de suite : le chemin le plus court passe encore par une
        # attente de fichier, et le plus long par plusieurs secondes de réseau.
        self._montrer_chargeur(login, secs)

        local = self._meilleur_tampon(login, path)
        if local != path:
            # La chaîne est celle du plein écran : son tampon est déjà en
            # pleine qualité, et disponible tout de suite. Rien à télécharger.
            self._engager_replay(local)
            return
        # Sinon le seul tampon local est celui de la grille, donc en 360p :
        # on tente de reprendre le moment chez Twitch, en pleine qualité.
        self._reprendre_chez_twitch(login, path, secs)

    def _montrer_chargeur(self, login: str, secs: int) -> None:
        """Pose le bandeau d'attente à la place qu'occupera le badge REPLAY."""
        cw = self.centralWidget()
        if cw is None:
            return
        self._cacher_chargeur()
        chargeur = _ReplayLoader(login, secs, cw)
        chargeur.annulation_demandee.connect(self._annuler_replay)
        self._replay_loader = chargeur
        self._placer_chargeur()
        chargeur.show()
        chargeur.raise_()

    def _placer_chargeur(self) -> None:
        """Centre le bandeau sur la zone vidéo, à la hauteur du badge REPLAY."""
        chargeur = self._replay_loader
        if chargeur is None:
            return
        chat_w = self._chat_panel._width if self._chat_panel._visible else 0
        video_w = self.width() - chat_w
        chargeur.move((video_w - chargeur.width()) // 2, 24)

    def _cacher_chargeur(self) -> None:
        """Retire le bandeau d'attente. Idempotent."""
        chargeur, self._replay_loader = self._replay_loader, None
        if chargeur is not None:
            chargeur.hide()   # avant de détacher : détaché et visible = une fenêtre
            chargeur.setParent(None)
            chargeur.deleteLater()

    def _annuler_replay(self) -> None:
        """Renonce à la reprise en cours, avant qu'elle n'ait commencé.

        Le fil de téléchargement, lui, va jusqu'au bout — l'interrompre au
        milieu laisserait un fichier tronqué. Il rendra son verdict dans le
        vide, et le fichier sera supprimé comme n'importe quel temporaire.
        """
        if self._replay_active:
            return
        self._replay_annule = True
        attente = getattr(self, "_replay_wait", None)
        if attente is not None:
            attente.stop()
        self._cacher_chargeur()
        self._cleanup_replay()
        logger.info("Replay abandonné à la demande de l'utilisateur")

    def _reprendre_chez_twitch(self, login: str, repli: str, secs: int) -> None:
        """Retélécharge le moment en pleine qualité, sans bloquer l'interface.

        Quelques secondes de réseau : un fil séparé, et l'interface continue de
        répondre. Le fichier de la grille sert de repli, pour qu'un direct
        terminé ou une panne réseau ne laissent pas l'utilisateur sans rien.
        """
        from core.replay_hd import recuperer

        def _travail() -> None:
            chemin, obtenue = "", 0.0
            try:
                chemin, obtenue = recuperer(login, secs)
            except Exception:      # noqa: BLE001 — le repli reste disponible
                logger.exception("Replay HD : reprise impossible")
            self._replay_hd_pret.emit(chemin or repli, float(obtenue or secs))

        threading.Thread(target=_travail, daemon=True,
                         name=f"replay-hd-{login}").start()

    def _sur_replay_hd(self, chemin: str, secondes: float) -> None:
        """Le fil de reprise a rendu son verdict — on joue ce qu'on a."""
        if self._replay_annule:
            self._replay_path = chemin
            self._cleanup_replay()      # supprime le fichier devenu inutile
            return
        if self._replay_active:
            return
        if not chemin:
            # Ni reprise HD ni repli : rien ne viendra, l'attente s'arrête ici.
            logger.warning("Replay : aucune source, ni Twitch ni tampon local")
            self._cacher_chargeur()
            return
        self._replay_secs = int(secondes) or self._replay_secs
        self._engager_replay(chemin)

    def _engager_replay(self, path: str) -> None:
        """Attend que le fichier soit complet, puis lance la lecture."""
        if not path:
            return
        self._replay_path = path
        # dump-cache écrit en arrière-plan et mpv ne signale pas la fin : on
        # attend que le fichier cesse de grossir, sinon on lit un tronçon.
        self._replay_size = -1
        self._replay_deadline = time.monotonic() + _REPLAY_DUMP_TIMEOUT_S
        self._replay_wait = QTimer(self)
        self._replay_wait.setInterval(200)
        self._replay_wait.timeout.connect(self._check_replay_file)
        self._replay_wait.start()
        logger.info("Replay demandé : %s (%s)", self._replay_login, path)

    def _check_replay_file(self) -> None:
        if self._replay_annule:
            self._replay_wait.stop()
            return
        try:
            size = pathlib.Path(self._replay_path).stat().st_size
        except OSError:
            size = 0
        if size > 0 and size == self._replay_size:
            self._replay_wait.stop()
            self._begin_replay()
            return
        self._replay_size = size
        if time.monotonic() > self._replay_deadline:
            self._replay_wait.stop()
            if size > 0:
                # Fichier partiel mais lisible : mieux vaut un replay tronqué
                # que rien du tout.
                logger.warning("Replay : écriture non terminée, lecture partielle")
                self._begin_replay()
            else:
                logger.warning("Replay abandonné : aucun fichier produit")
                # Le bandeau d'attente doit partir avec l'attente : le laisser
                # tourner sur un replay qui n'arrivera jamais serait pire que
                # de n'avoir rien montré.
                self._cacher_chargeur()
                self._cleanup_replay()

    def _begin_replay(self) -> None:
        cw = self.centralWidget()
        if cw is None:
            return
        self._cacher_chargeur()
        self._replay_active = True
        self._replay_player = MpvWidget(cw)
        self._replay_player.playback_ended.connect(self.stop_replay)
        self._replay_badge = _ReplayBadge(self._replay_login, self._replay_secs, cw)
        self._replay_badge.fermeture_demandee.connect(self.stop_replay)
        self._replay_progress = _ReplayProgress(cw)
        self._replay_origine = None
        # Le direct se tait pendant le replay : deux sources simultanées ne
        # s'écoutent pas, et c'est l'action qu'on veut entendre.
        self._mpv.set_mute(True)
        self._update_mpv_geometry()
        self._replay_player.show()
        self._replay_badge.show()
        self._replay_badge.raise_()
        self._replay_progress.show()
        self._replay_progress.raise_()
        self._replay_player.play(self._replay_path)
        # 200 ms : la barre avance d'un demi-pour-cent par pas sur trente
        # secondes, assez fin pour paraître continu sans réveiller l'interface
        # soixante fois par seconde pour trois pixels.
        self._replay_suivi = QTimer(self)
        self._replay_suivi.setInterval(200)
        self._replay_suivi.timeout.connect(self._suivre_progression)
        self._replay_suivi.start()

    def _suivre_progression(self) -> None:
        """Avance la barre selon la distance parcourue depuis la première image.

        La durée annoncée par le fichier est inutilisable : un fragment repris
        chez Twitch porte l'horodatage du direct, soit des heures. On mesure
        donc un écart, jamais un rapport à `duration`.
        """
        joueur, barre = self._replay_player, self._replay_progress
        if joueur is None or barre is None:
            return
        pos = joueur.position()
        if pos is None:
            return
        if self._replay_origine is None:
            self._replay_origine = pos
        span = max(1.0, float(self._replay_secs))
        barre.set_ratio((pos - self._replay_origine) / span)

    def stop_replay(self) -> None:
        """Referme le replay et rend l'écran au direct."""
        if not self._replay_active:
            return
        self._replay_active = False
        self._cacher_chargeur()
        player, self._replay_player = self._replay_player, None
        if player is not None:
            player.shutdown()
            player.hide()   # avant de détacher : détaché et visible = une fenêtre
            player.setParent(None)
            player.deleteLater()
        suivi, self._replay_suivi = self._replay_suivi, None
        if suivi is not None:
            suivi.stop()
            suivi.deleteLater()
        self._replay_origine = None
        for attr in ("_replay_badge", "_replay_progress"):
            widget = getattr(self, attr)
            setattr(self, attr, None)
            if widget is not None:
                widget.hide()   # avant de détacher : détaché et visible = une fenêtre
                widget.setParent(None)
                widget.deleteLater()
        self._apply_volume()          # rétablit le son du direct
        self._update_mpv_geometry()
        self._cleanup_replay()

    def _cleanup_replay(self) -> None:
        """Supprime le fichier temporaire du replay."""
        path, self._replay_path = self._replay_path, ""
        if path:
            try:
                pathlib.Path(path).unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Replay : fichier temporaire non supprimé — %s", exc)

    def run_action(self, cle: str) -> None:
        """Exécute une action demandée depuis la palette du panel."""
        if cle == "clip":
            self._save_clip()
        elif cle == "replay":
            self._replay_current()

    def _replay_current(self) -> None:
        """Rejoue les dernières secondes du direct affiché."""
        if self._replay_active or not self._current_login:
            self.stop_replay()
            return
        import tempfile
        secs = int(self._clip_config.get("duration_secs", 30) or 30)
        path = self._mpv.save_clip(secs, tempfile.gettempdir())
        if path:
            self.start_replay(self._current_login, path, secs)

    def _toggle_favorite_current(self) -> None:
        """Bascule le favori du direct affiché."""
        if not self._current_login:
            return
        from core import favorites
        etat = favorites.toggle(self._current_login)
        self._hint_lbl.setText(
            "Ajouté aux favoris" if etat else "Retiré des favoris")
        self._hint_lbl.setStyleSheet("color: #f5c518;")

    def show_show_started(self, login: str, nom: str) -> None:
        """Propose de basculer sur le show du programme qui vient de démarrer."""
        cw = self.centralWidget()
        if cw is None or not login or login == self._current_login:
            return
        toast = _FavoriteLiveToast(login, nom or login, cw,
                                   top_offset=self._pinned_audio.height_hint())
        toast.set_message(f"commence maintenant · {login}", "#a855f7")
        toast.switch_requested.connect(self.stream_change_requested)

    def show_goal_imminent(self, login: str, display: str, goal: str,
                           reste: float, donation_url: str = "") -> None:
        """Annonce qu'un objectif est sur le point de tomber."""
        cw = self.centralWidget()
        if cw is None or not login:
            return
        montant = f"{reste:,.0f} €".replace(",", "\u202f")
        toast = _FavoriteLiveToast(login, display or login, cw,
                                   top_offset=self._pinned_audio.height_hint())
        toast.set_message(f"plus que {montant} — « {goal} »", "#00ff87")
        toast.switch_requested.connect(self.stream_change_requested)
        # Le geste utile n'est pas seulement de regarder : c'est de faire
        # tomber l'objectif. Sans ce bouton, l'alerte ne mène nulle part.
        if donation_url:
            toast.add_donate_button(donation_url)

    def show_top_entry(self, login: str, display: str, viewers: int,
                       rang: int) -> None:
        """Une chaîne qu'on n'affiche pas vient d'entrer dans les plus grosses."""
        cw = self.centralWidget()
        if cw is None or not login:
            return
        toast = _FavoriteLiveToast(login, display or login, cw,
                                   top_offset=self._pinned_audio.height_hint())
        toast.set_message(
            f"n°{rang} des audiences · " + f"{viewers:,}".replace(",", "\u202f")
            + " viewers", "#38bdf8")
        toast.switch_requested.connect(self.stream_change_requested)

    def show_raid(self, source: str, cible: str, viewers: int) -> None:
        """Annonce un raid arrivé sur une chaîne de la grille."""
        cw = self.centralWidget()
        if cw is None or not cible:
            return
        detail = (f"raid de {source}"
                  + (f" · {viewers:,}".replace(",", "\u202f") if viewers else ""))
        toast = _FsHypeToast(cible, detail, 1.0, "#a855f7", cw)
        off = self._pinned_audio.height_hint()
        if off:
            toast.move(toast.x(), 8 + off + 6)
        toast.show()
        toast.raise_()

    def show_big_donation(self, login: str, display: str, amount: float,
                          nature: str = "don") -> None:
        """Signale qu'une chaîne vient de recevoir une somme notable."""
        cw = self.centralWidget()
        if cw is None or not login:
            return
        montant = f"{amount:,.0f} €".replace(",", "\u202f")
        texte = (f"bombardement · +{montant}" if nature == "bombardement"
                 else f"+{montant}")
        toast = _FsHypeToast(display or login, texte, 1.0, "#f5c518", cw)
        off = self._pinned_audio.height_hint()
        if off:
            toast.move(toast.x(), 8 + off + 6)
        toast.show()
        toast.raise_()

    def show_milestone(self, amount: float, label: str) -> None:
        """Annonce un palier de cagnotte, là où l'utilisateur regarde."""
        cw = self.centralWidget()
        if cw is None or not label:
            return
        toast = _FsHypeToast("Cagnotte", f"{label} franchis !", 1.0,
                             "#f5c518", cw)
        off = self._pinned_audio.height_hint()
        if off:
            toast.move(toast.x(), 8 + off + 6)
        toast.show()
        toast.raise_()

    def show_resource_alert(self, ressource: str, total: float,
                            part: float) -> None:
        """Prévient que le poste sature, là où l'utilisateur regarde.

        Orange plutôt que rouge : rien n'est cassé, mais l'image va se dégrader
        si le nombre de flux ne baisse pas.
        """
        cw = self.centralWidget()
        if cw is None:
            return
        from core.resource_watch import LIBELLES
        toast = _FsHypeToast(
            "Poste saturé",
            f"{LIBELLES.get(ressource, ressource)} à {total:.0f} % — "
            "réduisez le nombre de flux",
            1.0, "#ff6b00", cw)
        off = self._pinned_audio.height_hint()
        if off:
            toast.move(toast.x(), 8 + off + 6)
        toast.show()
        toast.raise_()

    def show_favorite_live(self, login: str, display: str = "") -> None:
        """Annonce qu'un favori vient de lancer son direct."""
        cw = self.centralWidget()
        if cw is None or not login:
            return
        # Déjà en train de le regarder : l'annoncer n'apprendrait rien.
        if login == self._current_login:
            return
        # Sous la liste des audios épinglés, qui occupe le même coin.
        toast = _FavoriteLiveToast(login, display or login, cw,
                                   top_offset=self._pinned_audio.height_hint())
        toast.switch_requested.connect(self.stream_change_requested)

    def set_pinned_audio(self, logins: list) -> None:
        """Met à jour la liste des chaînes dont l'audio est épinglé."""
        self._pinned_audio.set_logins([str(lg) for lg in logins])
        self._pinned_audio.set_muted(self._pinned_muted)
        self._pinned_audio.raise_()

    def set_pinned_muted(self, login: str, muted: bool) -> None:
        """Reflète dans la liste qu'une chaîne a été coupée depuis la console."""
        if muted:
            self._pinned_muted.add(str(login))
        else:
            self._pinned_muted.discard(str(login))
        self._pinned_audio.set_muted(self._pinned_muted)

    def show_hype_alert(
        self, login: str, label: str, score: float, color: str, excerpt: str = "",
    ) -> None:
        """Affiche un toast discret en haut à droite (appelé depuis GridWindow)."""
        cw = self.centralWidget()
        if cw is None:
            return
        toast = _FsHypeToast(login, label, score, color, cw)
        # Les deux occupent le coin haut-droit : décaler le toast sous la liste
        # des audios épinglés, sinon il la recouvre.
        off = self._pinned_audio.height_hint()
        if off:
            toast.move(toast.x(), 8 + off + 6)
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
        # Le libellé rendu après coup ne doit pas réintroduire de pictogramme :
        # le bouton porte déjà une icône qtawesome, et U+23FA s'affichait en
        # plus, sous forme de carré bleu faute de police d'emoji.
        original = "Clip"
        if path:
            self._clip_btn.setText("✓ Clip sauvé !")
        else:
            self._clip_btn.setText("✗ Erreur")
        QTimer.singleShot(3000, lambda: self._clip_btn.setText(original))

    # ── volume ─────────────────────────────────────────────────────

    def _apply_volume(self) -> None:
        """Répercute volume et mute sur MPV, l'icône, et la console."""
        self._mpv.set_volume(0 if self._muted else self._volume)
        self.volume_changed.emit(self._volume)
        self.mute_changed.emit(self._muted)
        self._mpv.set_mute(self._muted)
        if self._muted or self._volume == 0:
            icon, tip = "\U0001f507", "Rétablir le son"
        elif self._volume < 50:
            icon, tip = "\U0001f509", _LIBELLE_COUPER_SON
        else:
            icon, tip = "\U0001f50a", _LIBELLE_COUPER_SON
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

    def set_muted(self, muted: bool) -> None:
        """Coupe ou rétablit le direct, depuis la console de mixage."""
        if self._muted == bool(muted):
            return
        self._muted = bool(muted)
        self._apply_volume()

    def set_volume(self, value: int) -> None:
        """Règle le volume depuis l'extérieur (clavier, télécommande)."""
        self._vol_slider.setValue(max(0, min(100, int(value))))

    def _persist_volume(self) -> None:
        _save_settings({"volume": self._volume, "muted": self._muted})

    def set_clip_config(self, cfg: dict) -> None:
        """Met à jour la config clips (appelé depuis main.py sur settings_changed)."""
        self._clip_config = cfg.get("clips", {})

    def _carte_des_touches(self) -> dict:
        """Touche → (action, faut-il rappeler l'overlay ensuite).

        Une table plutôt qu'une chaîne de elif : ce qu'on vient lire ici, c'est
        la correspondance touche/action, et elle se lit d'un coup d'œil.
        Construite à la première frappe puis mémorisée — les actions sont des
        méthodes liées, elles n'existent pas avant l'instance.

        « C » garde le moment en cours : le geste le plus fréquent en régie, et
        il fallait autrefois viser un bouton dans un overlay masqué.
        """
        if self._touches is None:
            K = Qt.Key
            self._touches = {
                K.Key_Up:     (self._remote_menu.select_previous, False),
                K.Key_Down:   (self._remote_menu.select_next, False),
                K.Key_Return: (self._remote_menu.confirm_selection, False),
                K.Key_Space:  (self._remote_menu.confirm_selection, False),
                K.Key_Plus:   (lambda: self.set_volume(self._volume + 5), True),
                K.Key_Equal:  (lambda: self.set_volume(self._volume + 5), True),
                K.Key_Minus:  (lambda: self.set_volume(self._volume - 5), True),
                K.Key_M:      (self._toggle_mute, True),
                K.Key_C:      (self._save_clip, True),
                K.Key_R:      (self._replay_current, False),
                K.Key_F:      (self._toggle_favorite_current, True),
                K.Key_Left:   (lambda: self.neighbour_requested.emit(-1), False),
                K.Key_Right:  (lambda: self.neighbour_requested.emit(1), False),
            }
        return self._touches

    def _echapper(self) -> None:
        """Échap : coupe le replay d'abord, puis le menu, puis ferme.

        Le replay passe avant : c'est l'état le plus transitoire, et le plus
        susceptible d'être interrompu.
        """
        if self._replay_active:
            self.stop_replay()
        elif self._replay_loader is not None:
            self._annuler_replay()
        elif self._remote_menu.isVisible():
            self._close_remote_menu()
        else:
            self.close()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._palette.isVisible():
                self._palette.hide()
                return
            self._echapper()
            return
        # Les chiffres désignent une cellule de la grille : une plage, donc hors
        # table.
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            self.slot_requested.emit(key - Qt.Key.Key_1)
            return
        entree = self._carte_des_touches().get(key)
        if entree is None:
            super().keyPressEvent(event)
            return
        action, montrer_overlay = entree
        action()
        if montrer_overlay:
            self._show_overlay()

    # ── new features ───────────────────────────────────────────────

    def _update_mpv_geometry(self) -> None:
        """Recalcule les géométries absolues selon la visibilité du chat / mode PiP.

        Trois dispositions s'excluent — replay, page de don, direct seul — et
        chacune a sa méthode. Les avoir toutes ici en faisait une fonction dont
        on ne lisait plus la branche qui nous intéressait.
        """
        w, h = self.width(), self.height()
        chat_w = self._chat_panel._width if self._chat_panel._visible else 0
        video_w = w - chat_w

        if self._replay_active:
            self._geometrie_replay(video_w, h)
        elif self._pip_active:
            self._geometrie_donation(w, h, video_w)
        else:
            self._geometrie_direct(w, h, video_w)

        # Hors des branches : le bandeau d'attente est posé AVANT que le
        # replay soit actif, et doit suivre la fenêtre pendant ce temps-là.
        self._placer_chargeur()

        if self._ad_banner is not None:
            self._ad_banner.reposition(w, h)

        # Backend de rendu (macOS) : la surface OpenGL doit être repeinte après
        # tout changement de géométrie, sinon elle affiche l'ancienne frame.
        if self._mpv.uses_render_backend:
            self._mpv.update()

    def _geometrie_replay(self, video_w: int, h: int) -> None:
        """Le replay prend toute la zone vidéo, le direct passe en incrustation.

        C'est l'action qu'on veut voir en grand ; le direct ne sert plus qu'à
        ne pas perdre le fil.
        """
        pip_x = video_w - _REPLAY_PIP_W - _REPLAY_MARGIN
        pip_y = h - _REPLAY_PIP_H - _REPLAY_MARGIN
        if self._replay_player is not None:
            self._replay_player.setGeometry(0, 0, video_w, h)
            self._replay_player.lower()
        self._stack.setGeometry(pip_x, pip_y, _REPLAY_PIP_W, _REPLAY_PIP_H)
        self._stack.raise_()
        if self._replay_badge is not None:
            self._replay_badge.move(
                (video_w - self._replay_badge.width()) // 2, 24)
            self._replay_badge.raise_()
        if self._replay_progress is not None:
            # Collée au bord haut de la zone vidéo : le chat, quand il est
            # ouvert, ne rejoue pas et n'a pas à porter la barre.
            self._replay_progress.setGeometry(
                0, 0, video_w, _ReplayProgress.HAUTEUR)
            self._replay_progress.raise_()
        self._overlay.hide()

    def _geometrie_donation(self, w: int, h: int, video_w: int) -> None:
        """La page de don occupe l'écran, le direct se réduit dans un coin."""
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

    def _geometrie_direct(self, _w: int, h: int, video_w: int) -> None:
        """Le cas ordinaire : le direct en grand, ses surcouches par-dessus."""
        self._stack.setGeometry(0, 0, video_w, h)
        self._overlay.setGeometry(0, h - 56, video_w, 56)
        self._remote_btn.move(8, 8)
        self._remote_btn.raise_()
        self._pinned_audio.reposition(video_w, h)
        self._pinned_audio.raise_()
        if self._remote_menu.isVisible():
            self._remote_menu.setGeometry(
                self._remote_menu.x(), 0, REMOTE_MENU_WIDTH, h)
        if self._chat_panel._visible:
            self._chat_panel._update_geometry()

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
            self._close_remote_menu()
        else:
            self._show_overlay()  # montre l'overlay avant que le menu slide-in
            self._remote_menu.show_menu()
            # Le bouton est à (8, 8), donc DANS l'emprise du menu (0..320) une
            # fois ouvert — et raise_() le posait par-dessus son titre.
            self._remote_btn.hide()

    def _close_remote_menu(self) -> None:
        """Referme le menu et rend la main au bouton."""
        self._remote_menu.hide_menu()
        self._remote_btn.show()
        self._remote_btn.raise_()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        """Un clic hors du menu le referme."""
        if (self._remote_menu.isVisible()
                and not self._remote_menu.geometry().contains(event.pos())):
            self._close_remote_menu()
            return
        super().mousePressEvent(event)

    def _on_remote_stream_selected(self, login: str) -> None:
        """Relaye la sélection du menu télécommande via stream_change_requested."""
        self.stream_change_requested.emit(login)

    def _toggle_chat(self) -> None:
        self._chat_panel.toggle()
        self._chat_btn.setChecked(self._chat_panel._visible)
        self._update_mpv_geometry()
