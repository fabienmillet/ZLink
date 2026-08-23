"""Widget grille 5×5 réutilisable — miniatures de streams.

Utilisé dans :
- windows/grid.py  (standalone, mode triple et dual)
"""

from __future__ import annotations

import html
import logging
import math
import random

from typing import Callable

from PyQt6.QtCore import Qt, QPropertyAnimation, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from widgets.mpv_widget import MpvWidget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------

CELL_NORMAL = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 1px solid #222222;
}
QFrame#streamCell:hover {
    background-color: #1a1a1a;
}
"""

CELL_ACTIVE = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 2px solid #00ff87;
}
QFrame#streamCell:hover {
    background-color: #1a1a1a;
}
"""

CELL_OFFLINE = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 1px solid #222222;
}
"""

CELL_EMPTY = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 1px solid #1a1a1a;
}
"""

# ---------------------------------------------------------------------------
# HypeToast — superposition éphémère signalant un moment fort
# ---------------------------------------------------------------------------

_TOAST_W = 270
_TOAST_H = 44


class _HypeToast(QWidget):
    """Notification flottante affichée sur la GridWidget (top-right, 4 s)."""

    def __init__(
        self, label: str, score: float, color: str, parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFixedSize(_TOAST_W, _TOAST_H)
        self.setStyleSheet(
            f"QWidget {{ background: rgba(10,10,10,230); "
            f"border: 1px solid {color}; border-radius: 6px; }}"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(6)
        pct = QLabel(f"{score:.0%}")
        _pf = QFont("Consolas", 12); _pf.setBold(True); pct.setFont(_pf)
        pct.setStyleSheet(
            f"color: {color}; background: transparent; border: none;"
        )
        pct.setFixedWidth(40)
        h.addWidget(pct)
        lbl = QLabel(label)
        # Le label vient d'un LLM nourri par le chat Twitter/Twitch : jamais de
        # texte riche, sinon un spectateur peut injecter du balisage dans l'overlay.
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setFont(QFont("Segoe UI", 11))
        lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        h.addWidget(lbl, stretch=1)
        self.move(parent.width() - _TOAST_W - 12, 12)
        QTimer.singleShot(4000, self._start_fade)

    def _start_fade(self) -> None:
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(600)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_STREAMERS: list[dict[str, object]] = [
    {"login": "zerator", "viewers": 42_350, "online": True},
    {"login": "domingo", "viewers": 38_200, "online": True},
    {"login": "antoinedaniel", "viewers": 22_100, "online": True},
    {"login": "mistermv", "viewers": 18_700, "online": True},
    {"login": "joyca", "viewers": 15_400, "online": True},
    {"login": "etoiles", "viewers": 6_900, "online": True},
    {"login": "bagherajones", "viewers": 11_800, "online": True},
    {"login": "ponce", "viewers": 11_200, "online": True},
    {"login": "moman", "viewers": 7_600, "online": True},
    {"login": "lapi", "viewers": 5_300, "online": True},
    {"login": "avamind", "viewers": 4_800, "online": True},
    {"login": "jltomy", "viewers": 4_200, "online": True},
    {"login": "mastu", "viewers": 3_900, "online": True},
    {"login": "helydia", "viewers": 3_400, "online": True},
    {"login": "hortyunderscore", "viewers": 2_800, "online": True},
    {"login": "shisheyu", "viewers": 2_100, "online": True},
    {"login": "samueletienne", "viewers": 8_400, "online": True},
    {"login": "sylvainlyve", "viewers": 1_600, "online": True},
    {"login": "joycamq", "viewers": 1_200, "online": True},
    {"login": "nico_la", "viewers": 900, "online": True},
]


def _format_viewers(count: int) -> str:
    """12345 → '12.3k', 450 → '450', 1200000 → '1.2M'."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _compute_grid_dims(n: int) -> tuple[int, int]:
    """Calcule (rows, cols) optimal pour n streams sur écran 16:9.

    Trois critères pondérés :
      1. Slots gaspillés    → cells noires impossibles (weight=3)
      2. Disproportion      → dernière rangée pas trop large vs les autres (weight=4)
      3. Ratio AR cellule   → viser des cellules 16:9 sur écran 16:9 (weight=n)

    La disproportion mesure combien les cellules de la dernière rangée
    sont plus larges que les autres : cols/last_row_cells.
    """
    n = max(1, min(n, 25))
    if n == 1:
        return (1, 1)
    best_cols, best_score = 1, float("inf")
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        wasted = rows * cols - n
        last_cells = n - (rows - 1) * cols
        disparity = cols / last_cells if last_cells < cols else 1.0
        # Cellule 16:9 sur écran 16:9 ↔ rows == cols
        ar_penalty = (math.log(rows / cols)) ** 2 * n
        score = wasted * 3 + (disparity - 1) * 4 + ar_penalty
        # En cas d'égalité, préférer plus de colonnes (paysage)
        if score < best_score - 1e-9 or (abs(score - best_score) < 1e-9 and cols > best_cols):
            best_score = score
            best_cols = cols
    return (math.ceil(n / best_cols), best_cols)


def _distribute(total: int, n: int, gutter: int = 2) -> list[tuple[int, int]]:
    """Répartit `total` pixels en `n` segments avec `gutter` px d'écart.

    Retourne une liste de (offset, size) en pixels entiers exacts.
    L'excédent de l'arrondi est distribué sur les premiers segments.
    """
    if n <= 0:
        return []
    space = total - gutter * (n - 1)
    base = space // n
    extra = space - base * n
    result: list[tuple[int, int]] = []
    pos = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        result.append((pos, size))
        pos += size + gutter
    return result


# ---------------------------------------------------------------------------
# LoadingOverlay
# ---------------------------------------------------------------------------

_SPINNER_SEGMENTS = 12   # segments forming the ~100° gradient tail
_SPINNER_SPAN    = 9     # degrees per segment  (12 × 9 ≈ 108°)
_SPINNER_TICK    = 16    # ms per frame (~60 fps)
_SPINNER_STEP    = 4     # degrees of rotation per frame
_BAR_H = 28              # info-bar height — must match StreamCell._build()


class LoadingOverlay(QWidget):
    """Overlay chargement — anneau track + arc tournant avec point lumineux.

    Parent must be the StreamCell. The caller is responsible for calling
    setGeometry() whenever the cell resizes (see StreamCell.resizeEvent).
    The overlay is transparent to mouse events so clicks pass through.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._angle: int = 0
        self._login: str = ""
        self._timer = QTimer(self)
        self._timer.setInterval(_SPINNER_TICK)
        self._timer.timeout.connect(self._tick)
        self.hide()

    # -- public ---------------------------------------------------------------

    def show_overlay(self, login: str = "") -> None:
        """Show the overlay and start the spinner animation."""
        self._angle = 0
        self._login = login
        self.show()
        self.raise_()
        self._timer.start()

    def hide_overlay(self) -> None:
        """Stop the animation and hide the overlay."""
        self._timer.stop()
        self.hide()

    # -- internal -------------------------------------------------------------

    def _tick(self) -> None:
        self._angle = (self._angle + _SPINNER_STEP) % 360
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # --- fond sombre subtil ----------------------------------------------
        painter.fillRect(0, 0, w, h, QColor(0, 0, 0, 180))

        # --- géométrie -------------------------------------------------------
        radius = max(14, min(w // 8, h // 6, 26))
        pen_w = 1.5
        cx = w // 2
        cy = h // 2 - int(radius * 0.2)
        arc_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # --- anneau track (fond de l'arc) ------------------------------------
        track_pen = QPen(QColor(255, 255, 255, 15))
        track_pen.setWidthF(pen_w)
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(arc_rect)

        # --- arc tournant épuré ----------------------------------------------
        arc_pen = QPen(QColor(0, 255, 135))
        arc_pen.setWidthF(pen_w)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        painter.drawArc(arc_rect, int(-self._angle * 16), int(-100 * 16))

        # --- login du channel ------------------------------------------------
        if self._login:
            font_size = max(7, min(9, w // 22))
            font = QFont("Segoe UI", font_size)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110.0)
            painter.setFont(font)
            painter.setPen(QColor(160, 160, 160))
            text_rect = QRectF(4, cy + radius + 10, w - 8, 16)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                self._login.upper(),
            )

        painter.end()


# ---------------------------------------------------------------------------
# StreamCell
# ---------------------------------------------------------------------------

class StreamCell(QFrame):
    """Cellule individuelle de la grille 5×5."""

    clicked            = pyqtSignal(str)  # twitch_login
    audio_pin_requested = pyqtSignal(str)  # login à épingler (toggle)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("streamCell")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._twitch_login: str = ""
        self._is_active: bool = False
        self._is_online: bool = False
        self._streaming_login: str = ""  # login du stream actuellement lancé
        self._audio_pinned: bool = False

        self._build()
        self.set_empty()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # MpvWidget créé paresseusement au 1er start_stream() pour éviter
        # d'instancier 24 players MPV au démarrage.
        self._mpv: MpvWidget | None = None
        self._video_stack = QStackedWidget()
        self._video_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        _ph = QWidget()
        _ph.setStyleSheet("background: #000000;")
        self._video_stack.addWidget(_ph)  # index 0 — placeholder
        root.addWidget(self._video_stack, stretch=1)

        # Barre info — 28 px
        bar = QWidget()
        bar.setFixedHeight(28)
        bar.setStyleSheet("background-color: rgba(0, 0, 0, 0.7);")

        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(0)

        self._name_lbl = QLabel()
        self._name_lbl.setFont(QFont("Consolas", 10))
        self._name_lbl.setStyleSheet("color: #ffffff; background: transparent;")
        self._name_lbl.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        )
        h.addWidget(self._name_lbl)
        h.addStretch()

        self._pin_lbl = QLabel("🔊")
        self._pin_lbl.setFont(QFont("Segoe UI", 8))
        self._pin_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        self._pin_lbl.hide()
        h.addWidget(self._pin_lbl)
        h.addSpacing(4)

        self._viewers_lbl = QLabel()
        _vf = QFont("Consolas", 10); _vf.setBold(True); self._viewers_lbl.setFont(_vf)
        self._viewers_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        self._viewers_lbl.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        h.addWidget(self._viewers_lbl)

        root.addWidget(bar)

        # Loading overlay — floats above _video_stack, hidden by default.
        # Geometry is set in resizeEvent once the layout has been applied.
        self._overlay = LoadingOverlay(self)

    # -- public ----------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Position the overlay over the video area (full width, minus the info bar).
        video_h = max(0, self.height() - _BAR_H)
        self._overlay.setGeometry(0, 0, self.width(), video_h)
        self._overlay.raise_()

    def set_stream(
        self, twitch_login: str, viewers: int, *, online: bool = True,
    ) -> None:
        prev_login = self._twitch_login
        was_online = self._is_online
        self._twitch_login = twitch_login
        self._is_online = online

        if online:
            self._name_lbl.setText(twitch_login)
            self._name_lbl.setStyleSheet("color: #ffffff; background: transparent;")
            self._viewers_lbl.setText(_format_viewers(viewers))
            self._viewers_lbl.show()
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            # Login changé ou streamer revenu en ligne → autoriser le redémarrage
            if twitch_login != prev_login or not was_online:
                self._streaming_login = ""
        else:
            self._name_lbl.setText(twitch_login)
            self._name_lbl.setStyleSheet("color: #555555; background: transparent;")
            self._viewers_lbl.hide()
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            if was_online:
                self.stop_stream()
        self._refresh_style()

    def _ensure_mpv(self) -> MpvWidget:
        """Crée l'instance MpvWidget à la demande (lazy init)."""
        if self._mpv is None:
            self._mpv = MpvWidget(self._video_stack, grid_mode=True)
            self._mpv.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
            )
            self._video_stack.addWidget(self._mpv)
            self._video_stack.setCurrentWidget(self._mpv)
            # Forcer le rendu du widget natif avant que MPV s'y attache
            self._mpv.show()
            self._mpv.repaint()
            # Hide the overlay as soon as MPV reports actual playback.
            self._mpv.playback_started.connect(self._overlay.hide_overlay)
            # Re-appliquer l'état pin audio (évite un MPV muted sur une cellule épinglée)
            if self._audio_pinned:
                self._mpv.set_mute(False)
        else:
            self._video_stack.setCurrentWidget(self._mpv)
        return self._mpv

    def update_info(self, streamer: object) -> None:
        """Met à jour viewers/name sans toucher au stream MPV en cours."""
        viewers = int(getattr(streamer, "viewers", 0))
        self._viewers_lbl.setText(_format_viewers(viewers))

    def set_audio_pinned(self, pinned: bool) -> None:
        """Active ou désactive l'épinglement audio sur cette cellule."""
        self._audio_pinned = pinned
        self._pin_lbl.setVisible(pinned)
        if self._mpv is not None:
            self._mpv.set_mute(not pinned)

    def set_streamer(self, streamer: object) -> None:
        """Assigne un nouveau streamer (réinitialise _streaming_login)."""
        login = str(getattr(streamer, "twitch_login", ""))
        viewers = int(getattr(streamer, "viewers", 0))
        self.set_stream(login, viewers, online=True)

    def set_placeholder(self) -> None:
        """Alias pour set_empty (compatibilité appels futurs)."""
        self.set_empty()

    def set_empty(self) -> None:
        self._twitch_login = ""
        self._is_online = False
        self._is_active = False
        self._streaming_login = ""
        if self._mpv is not None:
            self._mpv.stop()
        self._overlay.hide_overlay()
        self._name_lbl.setText("")
        self._viewers_lbl.setText("")
        self._viewers_lbl.hide()
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self.setStyleSheet(CELL_EMPTY)

    def set_active(self, active: bool) -> None:
        self._is_active = active
        self._refresh_style()

    @property
    def twitch_login(self) -> str:
        return self._twitch_login

    @property
    def is_online(self) -> bool:
        return self._is_online

    @property
    def is_streaming(self) -> bool:
        """True si un stream MPV est actuellement en lecture."""
        return bool(self._streaming_login) and self._streaming_login == self._twitch_login

    def start_stream(self, quality: str = "360p,worst") -> None:
        """Lance la lecture MPV si le streamer est live et pas déjà en cours."""
        if self._twitch_login and self._is_online and self._twitch_login != self._streaming_login:
            self._streaming_login = self._twitch_login
            self._overlay.show_overlay(self._twitch_login)
            QTimer.singleShot(200, lambda: self._ensure_mpv().play_stream(self._twitch_login, quality))

    def stop_stream(self) -> None:
        """Arrête la lecture MPV et réinitialise l'état de streaming."""
        self._streaming_login = ""
        if self._mpv is not None:
            self._mpv.stop()
        self._overlay.hide_overlay()

    def pulse_hype(self, color: str = "#ff6b00", pulses: int = 3) -> None:
        """Anime le contour de la cellule pour signaler un moment fort."""
        if pulses <= 0:
            self._refresh_style()
            return
        self.setStyleSheet(
            f"QFrame#streamCell {{ background-color: #120500; border: 3px solid {color}; }}"
        )
        QTimer.singleShot(220, lambda: self._pulse_off(color, pulses))

    def _pulse_off(self, color: str, pulses: int) -> None:
        self._refresh_style()
        QTimer.singleShot(220, lambda: self.pulse_hype(color, pulses - 1))

    # -- internal --------------------------------------------------------------

    def _refresh_style(self) -> None:
        if not self._twitch_login:
            self.setStyleSheet(CELL_EMPTY)
        elif self._is_active:
            self.setStyleSheet(CELL_ACTIVE)
        elif not self._is_online:
            self.setStyleSheet(CELL_OFFLINE)
        else:
            self.setStyleSheet(CELL_NORMAL)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._twitch_login
            and self._is_online
        ):
            self.clicked.emit(self._twitch_login)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if not self._twitch_login or not self._is_online:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        if self._audio_pinned:
            act = menu.addAction("🔇 Couper l'audio")
        else:
            act = menu.addAction("🔊 Épingler l'audio")
        act.triggered.connect(lambda: self.audio_pin_requested.emit(self._twitch_login))
        menu.exec(event.globalPos())


# ---------------------------------------------------------------------------
# GoalAchievedToast — notification objectif accompli
# ---------------------------------------------------------------------------

class _GoalAchievedToast(QWidget):
    """Toast vert affiché sur la grille quand un objectif de don est accompli."""

    def __init__(self, login: str, goal_name: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        w, h = 270, 52
        self.setFixedSize(w, h)
        self.setStyleSheet(
            "QWidget { background: rgba(0,20,10,230);"
            " border: 1px solid #00ff87; border-radius: 6px; }"
        )
        vl = QVBoxLayout(self)
        vl.setContentsMargins(10, 6, 10, 6)
        vl.setSpacing(2)

        top = QHBoxLayout()
        check = QLabel("✓")
        _cf = QFont("Segoe UI", 11); _cf.setBold(True); check.setFont(_cf)
        check.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        top.addWidget(check)
        title = QLabel(f"<b>{html.escape(login)}</b> — Objectif accompli !")
        title.setFont(QFont("Segoe UI", 10))
        title.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top.addWidget(title, stretch=1)
        vl.addLayout(top)

        name_lbl = QLabel()
        name_lbl.setFont(QFont("Segoe UI", 9))
        name_lbl.setStyleSheet("color: #aaaaaa; background: transparent; border: none;")
        fm = QFontMetrics(name_lbl.font())
        name_lbl.setText(fm.elidedText(goal_name, Qt.TextElideMode.ElideRight, w - 24))
        vl.addWidget(name_lbl)

        self.move(parent.width() - w - 12, parent.height() - h - 12)
        self.show()
        self.raise_()
        QTimer.singleShot(5000, self._start_fade)

    def _start_fade(self) -> None:
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(600)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start()


# ---------------------------------------------------------------------------
# GridWidget — widget réutilisable 5×5
# ---------------------------------------------------------------------------

class GridWidget(QWidget):
    """Grille adaptive de cellules de stream — réutilisable."""

    stream_selected = pyqtSignal(str)  # twitch_login
    active_streams_changed = pyqtSignal(int)  # nombre de flux joués (qualité adaptative)

    MAX_CELLS: int = 25

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #000000;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._cells: list[StreamCell] = []
        self._active_login: str = ""
        self._cell_map: dict[str, StreamCell] = {}
        self._first_load: bool = True
        self._max_active_streams: int = self.MAX_CELLS
        self._grid_quality: str = "360p,worst"
        self._last_streamers: list = []
        self._last_selected: list[str] = []
        self._insertion_order: list[str] = []
        self._audio_pinned_login: str = ""
        self._last_active_count: int = -1
        # Renseigné par main.py : count → qualité (mode adaptatif). Sans lui, la
        # grille garde la qualité fixe reçue via restart_all_streams().
        self._quality_provider: "Callable[[int], str] | None" = None

        self._build()

    def _build(self) -> None:
        # Pas de layout Qt : positions gérées manuellement via setGeometry + resizeEvent.
        # Chaque StreamCell est enfant direct de GridWidget.
        for _ in range(self.MAX_CELLS):
            cell = StreamCell(self)
            cell.clicked.connect(self._on_cell_clicked)
            cell.audio_pin_requested.connect(self._on_audio_pin_requested)
            cell.hide()
            self._cells.append(cell)

    def _reposition_cells(self) -> None:
        """Recalcule et applique la géométrie de chaque cellule active.

        Algorithme :
        - N streams actifs → _compute_grid_dims(N) → (rows, cols)
        - Chaque rangée est divisée en len(rangée) cellules égales
        - La dernière rangée (incomplète) reçoit des cellules plus larges
        - AUCUNE cellule noire : les cellules vides sont masquées
        """
        active = [c for c in self._cells if c.twitch_login]
        n = len(active)

        # Maj visibilité
        for cell in self._cells:
            if cell.twitch_login:
                cell.show()
            else:
                cell.hide()

        W, H = self.width(), self.height()
        if n == 0 or W <= 0 or H <= 0:
            return

        rows, cols = _compute_grid_dims(n)
        gutter = 2 if n > 1 else 0
        row_ys = _distribute(H, rows, gutter)

        for row_idx in range(rows):
            row_start = row_idx * cols
            row_end   = min(row_start + cols, n)
            row_cells = active[row_start:row_end]
            if not row_cells:
                break
            y, cell_h = row_ys[row_idx]
            col_xs = _distribute(W, len(row_cells), gutter)
            for i, cell in enumerate(row_cells):
                x, cell_w = col_xs[i]
                cell.setGeometry(x, y, cell_w, cell_h)

        logger.debug("GridWidget repositionné : %d×%d pour %d streams", rows, cols, n)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._reposition_cells()


    # -- public API ------------------------------------------------------------

    def set_max_streams(self, n: int) -> None:
        """Met à jour le nombre maximum de streams actifs dans la grille."""
        old = self._max_active_streams
        self._max_active_streams = max(1, min(n, self.MAX_CELLS))
        if n < old:
            active = [c for c in self._cells if c.twitch_login]
            for cell in active[n:]:
                cell.stop_stream()
                cell.set_empty()

    def set_active_stream(self, twitch_login: str | None) -> None:
        """Met à jour le contour vert de la cellule active (stream en fullscreen)."""
        for cell in self._cells:
            cell.set_active(bool(twitch_login) and cell.twitch_login == twitch_login)

    def set_quality_provider(self, provider: "Callable[[int], str]") -> None:
        """Installe la fonction qui décide de la qualité selon le nombre de flux."""
        self._quality_provider = provider

    def restart_all_streams(self, quality: str) -> None:
        """Relancer tous les streams actifs avec la nouvelle qualité."""
        self._grid_quality = quality
        logger.info("GridWidget: relance tous les streams actifs avec qualité %s", quality)
        for cell in self._cells:
            if cell.twitch_login and cell.is_online:
                cell.stop_stream()
                cell.start_stream(quality)

    def set_streams(self, streams: list[dict[str, object]]) -> None:
        """Mets à jour toutes les cellules.

        Chaque dict attend : login (str), viewers (int), online (bool).
        """
        self._cell_map.clear()
        for i, cell in enumerate(self._cells):
            if i < len(streams):
                s = streams[i]
                login = str(s.get("login", ""))
                viewers = int(s.get("viewers", 0))
                online = bool(s.get("online", True))
                cell.set_stream(login, viewers, online=online)
                if login:
                    self._cell_map[login] = cell
            else:
                cell.set_empty()
        # Re-appliquer le stream actif
        if self._active_login:
            self.set_active(self._active_login)
        self._reposition_cells()

    def set_active(self, twitch_login: str) -> None:
        """Marque la cellule active (stream en fullscreen)."""
        self._active_login = twitch_login
        for cell in self._cells:
            cell.set_active(cell.twitch_login == twitch_login)

    def update_streamers(
        self,
        streamers: object,
        selected_logins: list[str] | None = None,
    ) -> None:
        """Met à jour la grille depuis une liste de StreamerInfo.

        Seuls les streamers sélectionnés ET live sont affichés,
        triés par viewers décroissants, limités à self._max_active_streams.
        Les cellules ne bougent jamais : on ajoute dans les vides, on vide les partis.
        """
        try:
            all_s = list(streamers)  # type: ignore[arg-type]
        except Exception:
            return

        self._last_streamers = all_s
        self._last_selected = list(selected_logins) if selected_logins else []

        if not selected_logins:
            for cell in self._cells:
                cell.stop_stream()
                cell.set_empty()
            self._reposition_cells()
            self._cell_map = {}
            return

        sel_set = set(selected_logins)
        try:
            live = sorted(
                [s for s in all_s  # type: ignore[union-attr]
                 if getattr(s, "twitch_login", None) in sel_set
                 and getattr(s, "online", False)],
                key=lambda s: -(getattr(s, "viewers", 0)),
            )
        except Exception:
            return

        # Décider qui est dans la grille (par viewers) → pour le cut
        kept_logins = {s.twitch_login for s in live[: self._max_active_streams]}  # type: ignore[union-attr]
        to_show_map: dict[str, object] = {
            s.twitch_login: s  # type: ignore[union-attr]
            for s in live if s.twitch_login in kept_logins  # type: ignore[union-attr]
        }
        new_logins_set = kept_logins
        # Qualité décidée avant de peupler : en mode adaptatif elle dépend du
        # nombre de flux qui vont effectivement tourner.
        if self._quality_provider is not None:
            self._grid_quality = self._quality_provider(len(kept_logins))
        quality = self._grid_quality

        # Étape 1 — retirer les streamers qui ne sont plus sélectionnés
        for cell in self._cells:
            if cell.twitch_login and cell.twitch_login not in new_logins_set:
                cell.stop_stream()
                cell.set_empty()
        self._insertion_order = [
            lg for lg in self._insertion_order if lg in new_logins_set
        ]

        # Étape 2 — mettre à jour viewers des streamers déjà présents
        existing: dict[str, StreamCell] = {
            cell.twitch_login: cell
            for cell in self._cells if cell.twitch_login
        }
        for login, cell in existing.items():
            if login in to_show_map:
                cell.update_info(to_show_map[login])

        # Étape 3 — ajouter les nouveaux dans les cellules libres
        truly_new = [
            to_show_map[lg] for lg in to_show_map if lg not in existing
        ]
        free_cells = [cell for cell in self._cells if not cell.twitch_login]
        for streamer, cell in zip(truly_new, free_cells):
            cell.set_streamer(streamer)
            cell.start_stream(quality)
            self._insertion_order.append(streamer.twitch_login)  # type: ignore[union-attr]

        self._cell_map = {
            cell.twitch_login: cell
            for cell in self._cells if cell.twitch_login
        }
        self._reposition_cells()
        self._first_load = False
        self._emit_active_count()

    def _emit_active_count(self) -> None:
        """Publie le nombre de cellules en lecture (pilote la qualité adaptative)."""
        count = sum(1 for cell in self._cells if cell.twitch_login and cell.is_online)
        if count != self._last_active_count:
            self._last_active_count = count
            self.active_streams_changed.emit(count)

    def refresh_viewers(self, streamers: list) -> None:
        """Met à jour viewers + online status sans toucher aux positions."""
        streamer_map = {
            getattr(s, "twitch_login", ""): s
            for s in streamers
        }
        for cell in self._cells:
            if cell.twitch_login and cell.twitch_login in streamer_map:
                s = streamer_map[cell.twitch_login]
                cell.update_info(s)
                # Si le streamer est passé offline, arrêter le stream
                if not getattr(s, "online", False) and cell.is_streaming:
                    cell.stop_stream()

    def update_cell(self, twitch_login: str, viewers: int) -> None:
        """Met à jour les viewers d'une cellule sans tout redessiner."""
        cell = self._cell_map.get(twitch_login)
        if cell is not None:
            cell.set_stream(twitch_login, viewers, online=True)

    # -- slots -----------------------------------------------------------------

    def _on_cell_clicked(self, twitch_login: str) -> None:
        logger.info("Grid: stream sélectionné → %s", twitch_login)
        self.set_active(twitch_login)
        self.stream_selected.emit(twitch_login)

    def _on_audio_pin_requested(self, login: str) -> None:
        """Toggle l'épinglement audio sur une cellule."""
        if self._audio_pinned_login == login:
            self._audio_pinned_login = ""  # unpin
        else:
            self._audio_pinned_login = login
        self._apply_audio_pin()

    def _apply_audio_pin(self) -> None:
        """Synchronise l'état mute/unmute de toutes les cellules selon le pin actif."""
        for cell in self._cells:
            pinned = bool(self._audio_pinned_login) and cell.twitch_login == self._audio_pinned_login
            cell.set_audio_pinned(pinned)

    def goal_achieved_flash(self, login: str, goal_name: str) -> None:
        """Pulse la cellule en vert et affiche un toast quand un objectif est accompli."""
        cell = self._cell_map.get(login)
        if cell is not None:
            cell.pulse_hype("#00ff87", pulses=5)
        toast = _GoalAchievedToast(login, goal_name, self)
        toast.show()
        toast.raise_()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Arrête tous les streams MPV avant de fermer."""
        for cell in self._cells:
            cell.stop_stream()
        super().closeEvent(event)

    def show_hype_toast(
        self, cell_idx: int, label: str, score: float, color: str = "#ff6b00",
    ) -> None:
        """Affiche un toast éphémère en haut à droite de la grille."""
        toast = _HypeToast(label, score, color, self)
        toast.show()
        toast.raise_()

    # -- test data -------------------------------------------------------------

    def _load_test_data(self) -> None:
        data: list[dict[str, object]] = []
        for s in _TEST_STREAMERS:
            v = s["viewers"]
            jitter = random.randint(-200, 200) if isinstance(v, int) and v > 0 else 0
            data.append({
                "login": s["login"],
                "viewers": max(0, int(v) + jitter) if isinstance(v, int) else 0,  # type: ignore[arg-type]
                "online": s["online"],
            })
        # 5 placeholders vides pour compléter la grille
        for _ in range(self.MAX_CELLS - len(data)):
            data.append({"login": "", "viewers": 0, "online": False})
        self.set_streams(data)
        self.set_active("zerator")
