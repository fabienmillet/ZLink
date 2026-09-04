# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
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

from PyQt6.QtCore import (Qt, QPoint, QPropertyAnimation, QRectF, QSize, QTimer,
                          pyqtSignal, QMimeData)
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen, QDrag
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import threading
import time

from core import favorites
from core.paths import CLIPS_DEFAUT
from core.replay_hd import REPLAY_SECS
from core.stream_manager import QUALITY_GRID
from widgets.mpv_widget import MpvWidget

try:
    import qtawesome as qta
    _QTA_OK = True
except Exception:  # noqa: BLE001
    # Pas seulement ImportError : qtawesome charge des polices et peut
    # échouer autrement. Un except trop étroit laissait _QTA_OK non
    # défini, et le démarrage plantait par NameError une fois sur six.
    qta = None  # type: ignore[assignment]
    _QTA_OK = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------

# La LARGEUR de bordure est identique dans tous les états, seule la couleur
# change. Une largeur variable modifiait le rectangle de contenu, donc
# déplaçait et redimensionnait la fenêtre X11 native de mpv à chaque
# changement d'état — six fois en 1,3 s pendant une alerte hype, d'où les
# sautes d'image et les zones noires quand un flux s'interrompt.
# Police de l'UI, repetee sur toutes les etiquettes de cellule.
_POLICE_UI = "Segoe UI"

CELL_NORMAL = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 2px solid #222222;
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
    border: 2px solid #1e1e1e;
}
"""

CELL_EMPTY = """
QFrame#streamCell {
    background-color: #0a0a0a;
    border: 2px solid #141414;
}
"""

# ---------------------------------------------------------------------------
# HypeToast — superposition éphémère signalant un moment fort
# ---------------------------------------------------------------------------

_TOAST_W = 270
_TOAST_H = 44


class _HypeToast(QWidget):
    """Notification flottante affichée sur la GridWidget (top-right, 4 s)."""

    clip_requested = pyqtSignal(str)     # login
    replay_requested = pyqtSignal(str)   # login

    def __init__(
        self, label: str, score: float, color: str, parent: QWidget,
        login: str = "", can_clip: bool = False,
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
        lbl.setFont(QFont(_POLICE_UI, 11))
        lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        h.addWidget(lbl, stretch=1)

        # Le bouton doit être sur l'alerte elle-même : le moment est déjà en
        # train de passer, et aller chercher la cellule dans la grille prend
        # plus de temps qu'il n'en reste dans le tampon.
        self._login = login
        if can_clip and login:
            keep = QPushButton()
            keep.setFixedSize(24, 24)
            keep.setCursor(Qt.CursorShape.PointingHandCursor)
            keep.setToolTip("Garder ce moment")
            if _QTA_OK:
                keep.setIcon(qta.icon("mdi6.content-save-outline", color=color))
                keep.setIconSize(QSize(15, 15))
            else:
                keep.setText("\u25cf")
            keep.setStyleSheet(
                "QPushButton { background: transparent; border: none; }"
                "QPushButton:hover { background: #262626; border-radius: 4px; }"
            )
            keep.clicked.connect(self._on_keep)
            h.addWidget(keep)

            replay = QPushButton()
            replay.setFixedSize(24, 24)
            replay.setCursor(Qt.CursorShape.PointingHandCursor)
            replay.setToolTip("Revoir ce moment")
            if _QTA_OK:
                replay.setIcon(qta.icon("mdi6.replay", color=color))
                replay.setIconSize(QSize(15, 15))
            else:
                replay.setText("\u21ba")
            replay.setStyleSheet(
                "QPushButton { background: transparent; border: none; }"
                "QPushButton:hover { background: #262626; border-radius: 4px; }"
            )
            replay.clicked.connect(self._on_replay)
            h.addWidget(replay)
        self.move(parent.width() - _TOAST_W - 12, 12)
        QTimer.singleShot(4000, self._start_fade)

    def _on_keep(self) -> None:
        self.clip_requested.emit(self._login)
        # Retour visuel : le toast s'efface, le clip est parti.
        self._start_fade()

    def _on_replay(self) -> None:
        self.replay_requested.emit(self._login)
        self._start_fade()

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

#: Icônes de l'état sonore d'une cellule — la pastille « épinglé » et les
#: deux entrées du menu contextuel doivent désigner la même chose.
_ICONE_SON = "mdi6.volume-high"
_ICONE_MUET = "mdi6.volume-off"

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
            font = QFont(_POLICE_UI, font_size)
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
    audio_pin_requested = pyqtSignal(str)
    clip_requested      = pyqtSignal(str)  # login dont on veut garder le moment
    replay_requested    = pyqtSignal(str)  # login dont on veut revoir le moment
    # Le flux s'est arrêté de lui-même (fin de live, coupure) : la cellule
    # demande sa libération pour qu'un autre streamer prenne la place.
    stream_ended = pyqtSignal(str)  # login de la cellule libérée

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
        self._mix_muted: bool = False     # coupure demandée depuis la console
        self._clip_secs: int = 0      # 0 = clips désactivés sur cette cellule
        self._low_latency: bool = False
        self._pulse_gen: int = 0      # jeton d'annulation des pulsations
        self._attente_image: QTimer | None = None
        # Compté hors de start_stream, qui remet _end_retried à zéro à chaque
        # relance : sans compteur séparé, une reprise en rappelait une autre et
        # une chaîne morte se relançait toutes les quatre secondes sans fin.
        self._echecs_demarrage: int = 0

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

        # U+1F50A ne rend AUCUN glyphe hors Windows : l'indicateur d'épinglage
        # occupait zéro pixel et rien ne signalait la cellule sonore.
        self._pin_lbl = QLabel()
        self._pin_lbl.setFixedSize(14, 14)
        if _QTA_OK:
            self._pin_lbl.setPixmap(
                qta.icon(_ICONE_SON, color="#00ff87").pixmap(14, 14)
            )
        else:
            self._pin_lbl.setText("\u25cf")   # puce pleine, présente partout
            self._pin_lbl.setFont(QFont(_POLICE_UI, 9))
        self._pin_lbl.setStyleSheet("color: #00ff87; background: transparent; border: none;")
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
        if twitch_login != prev_login:
            self._echecs_demarrage = 0    # nouvelle chaîne, nouveau crédit
        self._twitch_login = twitch_login
        self._is_online = online
        self._viewers = viewers or 0

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
            # clip_buffer_secs=0 : pas de tampon arrière tant que les clips
            # depuis la grille ne sont pas demandés (voir set_clip_buffer).
            self._mpv = MpvWidget(self._video_stack, grid_mode=True,
                                  clip_buffer_secs=self._clip_secs,
                                  low_latency=self._low_latency)
            if self._clip_secs:
                self._mpv.set_clip_buffer(self._clip_secs)
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
            self._mpv.playback_started.connect(self._sur_premiere_image)
            self._mpv.playback_ended.connect(self._on_playback_ended)
            self._mpv.resolution_failed.connect(self._sur_echec_resolution)
            self._mpv.playback_requested.connect(self._armer_attente)
            # Re-appliquer l'état pin audio (évite un MPV muted sur une cellule épinglée)
            if self._audio_pinned:
                self._mpv.set_mute(False)
        else:
            self._video_stack.setCurrentWidget(self._mpv)
        return self._mpv

    def update_info(self, streamer: object) -> None:
        """Met à jour viewers/name sans toucher au stream MPV en cours."""
        viewers = int(getattr(streamer, "viewers", 0))
        self._viewers = viewers   # sert au classement de la grille
        self._viewers_lbl.setText(_format_viewers(viewers))

    def set_clip_buffer(self, secs: int) -> None:
        """Active (secs > 0) ou coupe (0) la conservation des dernières secondes."""
        secs = max(0, int(secs))
        if secs == self._clip_secs:
            return
        self._clip_secs = secs
        if self._mpv is not None:
            self._mpv.set_clip_buffer(secs)

    def set_low_latency(self, on: bool) -> None:
        """Retard minimal sur le direct pour cette cellule.

        Retenu même sans lecteur : les cellules sont créées à la demande, et
        `_ensure_mpv` passe la valeur au constructeur.
        """
        self._low_latency = bool(on)
        if self._mpv is not None:
            self._mpv.set_low_latency(self._low_latency)

    def save_clip(self, secs: int, directory: str) -> str | None:
        """Écrit les dernières secondes de CETTE cellule. None si indisponible."""
        if self._mpv is None or not self._clip_secs:
            return None
        return self._mpv.save_clip(secs, directory)

    def set_volume(self, volume: int) -> None:
        """Volume de cette cellule, 0-100. Sans effet si elle n'est pas épinglée."""
        if self._mpv is not None and self._audio_pinned:
            self._mpv.set_volume(volume)

    def set_mix_muted(self, muted: bool) -> None:
        """Coupe cette cellule depuis la console, sans toucher à son volume."""
        self._mix_muted = bool(muted)
        self._apply_audio_state()

    def set_audio_pinned(self, pinned: bool) -> None:
        """Active ou désactive l'épinglement audio sur cette cellule."""
        self._audio_pinned = pinned
        self._pin_lbl.setVisible(pinned)
        self._apply_audio_state()

    def _apply_audio_state(self) -> None:
        """Une seule vérité pour le silence : épinglage ET coupure console.

        Couper depuis la console en descendant le volume à zéro paraissait
        suffire, mais le volume est réécrit dès que la console se reconstruit —
        le silence se levait tout seul. La coupure passe donc par le mute de
        mpv, indépendant du volume, et le volume garde sa propre valeur.
        """
        if self._mpv is None:
            return
        self._mpv.set_mute(not self._audio_pinned or self._mix_muted)

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
        self._viewers = 0   # relayé au HypeWatcher comme 3e signal
        self._press_pos = None      # amorce du glisser-déposer
        self._dragging = False
        self._end_retried = False   # une reprise autorisée par flux
        self.setAcceptDrops(True)
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

    def start_stream(self, quality: str = QUALITY_GRID) -> None:
        """Lance la lecture MPV si le streamer est live et pas déjà en cours."""
        if self._twitch_login and self._is_online and self._twitch_login != self._streaming_login:
            self._streaming_login = self._twitch_login
            self._end_retried = False
            self._overlay.show_overlay(self._twitch_login)
            QTimer.singleShot(200, lambda: self._ensure_mpv().play_stream(self._twitch_login, quality))

    _END_RETRY_MS = 4000   # première reprise après une fin de flux
    #: Attentes entre deux tentatives, en millisecondes. La cellule n'est rendue
    #: qu'une fois cette liste épuisée : quatre essais étalés sur une minute.
    _RELANCES_MS = (4000, 12000, 30000, 60000)
    #: Délai accordé à mpv pour sortir sa première image APRÈS que l'URL lui a
    #: été remise. Compté à partir de là et non du clic : la résolution passe
    #: par un sémaphore à trois places, et la vingtième cellule d'une grille
    #: attend son tour plusieurs dizaines de secondes sans que rien n'aille mal.
    _FIRST_FRAME_MS = 20000

    def _armer_attente(self, login: str = "") -> None:
        """Borne l'attente de la première image : au-delà, le flux est mort.

        Un anneau qui tourne indéfiniment est le pire des états : il affirme
        qu'il se passe quelque chose. Une URL résolue puis muette — flux coupé
        côté Twitch, CDN qui ne répond plus — n'émet aucun événement mpv, donc
        rien d'autre ne viendrait interrompre l'attente.
        """
        if login and login != self._twitch_login:
            return
        self._desarmer_attente()
        minuterie = QTimer(self)
        minuterie.setSingleShot(True)
        minuterie.setInterval(self._FIRST_FRAME_MS)
        minuterie.timeout.connect(self._sur_attente_expiree)
        self._attente_image = minuterie
        minuterie.start()

    def _sur_premiere_image(self) -> None:
        """mpv a sorti une image : l'attente s'arrête et l'ardoise est effacée."""
        self._desarmer_attente()
        self._echecs_demarrage = 0

    def _desarmer_attente(self) -> None:
        """Retire la borne d'attente. Idempotent. Ne touche pas au compteur."""
        minuterie, self._attente_image = self._attente_image, None
        if minuterie is not None:
            minuterie.stop()
            minuterie.deleteLater()

    def _sur_attente_expiree(self) -> None:
        self._attente_image = None
        if not self._streaming_login:
            return
        logger.warning("Cellule %s : aucune image après %d s",
                       self._twitch_login, self._FIRST_FRAME_MS // 1000)
        self._abandonner_demarrage()

    def _sur_echec_resolution(self, login: str) -> None:
        """streamlink n'a rien rendu pour ce streamer."""
        if login and login != self._twitch_login:
            return
        logger.warning("Cellule %s : flux non résolu", login or self._twitch_login)
        self._abandonner_demarrage()

    def _abandonner_demarrage(self) -> None:
        """Retente une fois, puis rend la cellule plutôt que de la laisser tourner.

        Même politique que pour un flux interrompu : le premier échec est
        souvent passager, le second ne l'est plus.
        """
        login = self._twitch_login
        if not login:
            return
        self._desarmer_attente()
        self._echecs_demarrage += 1
        self._streaming_login = ""
        if self._mpv is not None:
            self._mpv.stop()
        if self._echecs_demarrage < len(self._RELANCES_MS):
            # Attente croissante : un échec isolé vient presque toujours d'un
            # hoquet passager côté Twitch, et réessayer aussitôt le reproduit.
            # Abandonner au deuxième essai faisait défiler les cellules —
            # libérée, remplacée, échouée à nouveau — sans laisser au flux le
            # temps de revenir.
            attente = self._RELANCES_MS[self._echecs_demarrage - 1]
            logger.info("Cellule %s : nouvel essai dans %d s (%d/%d)",
                        login, attente // 1000, self._echecs_demarrage,
                        len(self._RELANCES_MS))
            QTimer.singleShot(attente, lambda lg=login: self._retry_stream(lg))
            return
        logger.info("Cellule %s : flux injoignable, libération", login)
        self._overlay.hide_overlay()
        self.stream_ended.emit(login)

    def _on_playback_ended(self) -> None:
        """mpv est repassé au repos : coupure passagère, ou live terminé.

        On retente une fois — un flux Twitch hoquette régulièrement — puis on
        libère la cellule si ça ne repart pas.
        """
        if not self._twitch_login or not self._streaming_login:
            return
        if not self._end_retried:
            self._end_retried = True
            login = self._twitch_login
            logger.info("Cellule %s : flux interrompu, nouvelle tentative", login)
            self._streaming_login = ""
            QTimer.singleShot(
                self._END_RETRY_MS,
                lambda lg=login: self._retry_stream(lg),
            )
            return
        logger.info("Cellule %s : flux terminé, libération", self._twitch_login)
        self.stream_ended.emit(self._twitch_login)

    def _retry_stream(self, login: str) -> None:
        """Relance si la cellule affiche toujours le même streamer.

        En reprenant la qualité courante de la grille : sans elle, la cellule
        repartait sur le défaut codé en dur et une cellule qui hoquette dans une
        grille de quatre flux retombait silencieusement en 160p sans jamais
        remonter.
        """
        if self._twitch_login != login or not self._is_online:
            return
        grid = self._grid()
        quality = getattr(grid, "_grid_quality", None)
        if quality:
            self.start_stream(quality)
        else:
            self.start_stream()

    def stop_stream(self) -> None:
        """Arrête la lecture MPV et réinitialise l'état de streaming."""
        self._desarmer_attente()
        self._streaming_login = ""
        if self._mpv is not None:
            self._mpv.stop()
        self._overlay.hide_overlay()

    _PULSE_HALF_MS = 220        # moitié d'un cycle allumé/éteint

    def pulse_hype(self, color: str = "#ff6b00", pulses: int = 3,
                   seconds: float | None = None) -> None:
        """Anime le contour de la cellule. `seconds` prime sur `pulses`.

        Un jeton de génération accompagne la chaîne de minuteries : une
        nouvelle pulsation annule la précédente. Sans lui, deux animations
        lancées coup sur coup — une alerte de chat puis un objectif atteint —
        s'entrelaçaient, et le contour clignotait à contretemps bien après la
        fin des deux.
        """
        if seconds is not None:
            pulses = max(1, round(seconds * 1000 / (2 * self._PULSE_HALF_MS)))
        self._pulse_gen += 1
        self._pulse_on(color, pulses, self._pulse_gen)

    def _pulse_on(self, color: str, left: int, gen: int) -> None:
        if gen != self._pulse_gen:
            return
        if left <= 0:
            self._refresh_style()
            return
        # 2px comme tous les autres états : la géométrie ne bouge pas.
        self.setStyleSheet(
            f"QFrame#streamCell {{ background-color: #120500; border: 2px solid {color}; }}"
        )
        QTimer.singleShot(self._PULSE_HALF_MS,
                          lambda: self._pulse_off(color, left, gen))

    def _pulse_off(self, color: str, left: int, gen: int) -> None:
        if gen != self._pulse_gen:
            return
        self._refresh_style()
        QTimer.singleShot(self._PULSE_HALF_MS,
                          lambda: self._pulse_on(color, left - 1, gen))

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

    # -- glisser-déposer -------------------------------------------------------

    _MIME = "application/x-zlink-cell"

    def _grid(self) -> object | None:
        """Le GridWidget parent, s'il expose le déplacement manuel."""
        w = self.parent()
        while w is not None and not hasattr(w, "move_cell"):
            w = w.parent()
        return w

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            # On mémorise l'appui sans rien émettre : le clic ne part qu'au
            # relâchement, sinon amorcer un glissement basculerait aussi en
            # plein écran.
            self._press_pos = event.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        grid = self._grid()
        if (
            not (event.buttons() & Qt.MouseButton.LeftButton)
            or self._press_pos is None
            or self._dragging
            or not self._twitch_login
            or grid is None
            or not getattr(grid, "is_draggable", lambda: False)()
        ):
            super().mouseMoveEvent(event)
            return
        if ((event.pos() - self._press_pos).manhattanLength()
                < QApplication.startDragDistance()):
            super().mouseMoveEvent(event)
            return
        self._dragging = True
        self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
        mime = QMimeData()
        mime.setData(self._MIME, self._twitch_login.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.grab().scaled(
            160, 90, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        drag.setPixmap(pm)
        # Sans point d'accroche, la vignette colle son coin supérieur gauche au
        # curseur : elle masque la case visée et on ne sait plus ce qu'on
        # désigne. On la place sous le point réellement saisi, à l'échelle de la
        # réduction.
        if self.width() > 0 and self.height() > 0:
            drag.setHotSpot(QPoint(
                int(self._press_pos.x() * pm.width() / self.width()),
                int(self._press_pos.y() * pm.height() / self.height()),
            ))
        drag.exec(Qt.DropAction.MoveAction)
        self.setCursor(QCursor(
            Qt.CursorShape.OpenHandCursor if self._is_online
            else Qt.CursorShape.ArrowCursor
        ))

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._dragging
            and self._twitch_login
            and self._is_online
        ):
            self.clicked.emit(self._twitch_login)
        self._press_pos = None
        self._dragging = False
        super().mouseReleaseEvent(event)

    def _set_drop_target(self, on: bool) -> None:
        """Souligne la cellule survolée pendant un glissement.

        Sans ce retour, rien n'indiquait où la cellule allait atterrir : on
        déplaçait à l'aveugle. La LARGEUR de bordure reste à 2px comme partout,
        pour ne pas déplacer la fenêtre X11 de mpv (cf. CELL_NORMAL).
        """
        if on:
            self.setStyleSheet(
                "QFrame#streamCell { background-color: #0a1a12; "
                "border: 2px dashed #00ff87; }"
            )
        else:
            self._refresh_style()

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(self._MIME):
            src = bytes(event.mimeData().data(self._MIME)).decode("utf-8")
            if src != self._twitch_login:
                self._set_drop_target(True)
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(self._MIME):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self._set_drop_target(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self._set_drop_target(False)
        if not event.mimeData().hasFormat(self._MIME):
            return
        source = bytes(event.mimeData().data(self._MIME)).decode("utf-8")
        grid = self._grid()
        if grid is not None and source and source != self._twitch_login:
            grid.move_cell(source, self._twitch_login)  # type: ignore[attr-defined]
        event.acceptProposedAction()

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if not self._twitch_login or not self._is_online:
            return
        from PyQt6.QtWidgets import QMenu
        from core.ui_theme import MENU_QSS
        menu = QMenu(self)
        # Sans style explicite, le menu prend la palette du bureau : sous un
        # thème sombre, le fond devenait noir et le texte le restait — les
        # entrées n'affichaient plus que leur icône.
        menu.setStyleSheet(MENU_QSS)
        if self._is_active:
            # Cette chaîne est celle du plein écran : proposer d'épingler son
            # audio laisserait croire à une action, pour un doublon inaudible.
            deja = self._ajouter_action(
                menu, "Audio déjà joué en plein écran", _ICONE_SON,
                "#555555",
            )
            deja.setEnabled(False)
        else:
            epingle = self._audio_pinned
            act = self._ajouter_action(
                menu,
                "Couper l'audio" if epingle else "Épingler l'audio",
                _ICONE_MUET if epingle else _ICONE_SON,
                "#e0e0e0",
            )
            act.triggered.connect(
                lambda: self.audio_pin_requested.emit(self._twitch_login))

        fav = favorites.is_favorite(self._twitch_login)
        fav_act = self._ajouter_action(
            menu,
            "Retirer des favoris" if fav else "Mettre en favori",
            "mdi6.star-off" if fav else "mdi6.star",
            "#f5c518",
        )
        fav_act.triggered.connect(self._toggle_favorite)

        self._ajouter_actions_clip(menu)
        menu.exec(event.globalPos())

    @staticmethod
    def _ajouter_action(menu, libelle: str, icone: str, couleur: str):
        """Entrée de menu avec son icône, quand qtawesome est disponible.

        Pas d'emoji dans les libellés : U+1F507/U+1F50A ne rendent aucun glyphe
        hors Windows, l'entrée apparaîtrait amputée de son icône. D'où une icône
        qtawesome, ou rien.
        """
        act = menu.addAction(libelle)
        if _QTA_OK:
            act.setIcon(qta.icon(icone, color=couleur))
        return act

    def _ajouter_actions_clip(self, menu) -> None:
        """Garder et revoir les dernières secondes, si les clips sont activés."""
        if not self._clip_secs:
            return
        menu.addSeparator()
        clip_act = self._ajouter_action(
            menu, f"Garder les {REPLAY_SECS} dernières secondes",
            "mdi6.content-save-outline", "#e0e0e0",
        )
        clip_act.triggered.connect(
            lambda: self.clip_requested.emit(self._twitch_login))
        replay_act = self._ajouter_action(
            menu, f"Revoir les {REPLAY_SECS} dernières secondes",
            "mdi6.replay", "#ff6b00",
        )
        replay_act.triggered.connect(
            lambda: self.replay_requested.emit(self._twitch_login))

    def _toggle_favorite(self) -> None:
        favorites.toggle(self._twitch_login)
        self._refresh_style()
        grid = self._grid()
        if grid is not None:
            grid._reposition_cells()  # type: ignore[attr-defined]


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
        _cf = QFont(_POLICE_UI, 11); _cf.setBold(True); check.setFont(_cf)
        check.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        top.addWidget(check)
        title = QLabel(f"<b>{html.escape(login)}</b> — Objectif accompli !")
        title.setFont(QFont(_POLICE_UI, 10))
        title.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top.addWidget(title, stretch=1)
        vl.addLayout(top)

        name_lbl = QLabel()
        # Le nom d'un objectif est écrit par le streamer : sans ce format, une
        # balise y serait rendue, et une « image » distante déclencherait une
        # requête depuis la machine de l'utilisateur.
        name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        name_lbl.setFont(QFont(_POLICE_UI, 9))
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
    #: Chaînes dont l'audio est épinglé, dans l'ordre de la grille.
    audio_pins_changed = pyqtSignal(list)
    #: (login, chemin|"") — un clip vient d'être demandé ; chemin vide = échec.
    clip_saved = pyqtSignal(str, str)
    #: Interne : le fil de sauvegarde a fini. (login, chemin ou vide)
    _clip_ecrit = pyqtSignal(str, str)
    #: (login, chemin, secondes) — rejouer ce moment en grand.
    replay_requested = pyqtSignal(str, str, int)

    MAX_CELLS: int = 25

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #000000;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._cells: list[StreamCell] = []
        self._active_login: str = ""
        self._cell_map: dict[str, StreamCell] = {}
        # Queued par construction : le fil de sauvegarde emet depuis
        # un thread, l'interface doit etre notifiee sur le sien.
        self._clip_ecrit.connect(self.clip_saved)
        self._first_load: bool = True
        self._max_active_streams: int = self.MAX_CELLS
        self._grid_quality: str = QUALITY_GRID
        self._last_streamers: list = []
        self._last_selected: list[str] = []
        self._insertion_order: list[str] = []
        self._applied_order: list[str] = []
        self._last_reorder: float = 0.0
        # Flux dont mpv a constaté la fin, alors que l'API les annonce encore en
        # direct. On les écarte de la grille le temps que l'API se mette à jour,
        # sinon le replacement les réinstallerait aussitôt dans leur cellule.
        self._ended: dict[str, float] = {}
        # "viewers" : la grille se réordonne seule. "manual" : l'ordre est celui
        # que l'utilisateur a posé au glisser-déposer et rien ne le bouge.
        self._sort_mode: str = "viewers"
        # Épinglage MULTIPLE : la liste affichée en plein écran n'aurait aucun
        # sens avec une seule entrée possible, et suivre deux commentaires à la
        # fois est un usage courant en régie.
        self._audio_pinned_logins: set[str] = set()
        self._clip_secs: int = 60
        self._low_latency: bool = False
        self._clip_dir: str = ""
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
            cell.clip_requested.connect(self.save_clip)
            cell.replay_requested.connect(self.request_replay)
            cell.stream_ended.connect(self._on_stream_ended)
            cell.hide()
            self._cells.append(cell)

    # Réordonner déplace les fenêtres X11 natives de mpv : à faire rarement,
    # sinon la vidéo saute à chaque rafraîchissement des viewers.
    _REORDER_MIN_INTERVAL_S = 20.0
    # Au-delà, on retente : le streamer a pu relancer son direct.
    _ENDED_COOLDOWN_S = 300.0

    #: Dispositions acceptées, cf. set_sort_mode().
    SORT_MODES = ("viewers", "manual", "favorites")

    def is_draggable(self) -> bool:
        """Vrai si l'utilisateur peut réordonner la grille à la souris."""
        return self._sort_mode in ("manual", "favorites")

    def set_sort_mode(self, mode: str) -> None:
        """Choisit la disposition de la grille.

        - « viewers »   : tri automatique par audience, favoris en tête.
        - « manual »    : ordre entièrement libre, au glisser-déposer. Les
          favoris n'y sont PAS remontés — sans quoi ils annuleraient une partie
          des déplacements et le mode ne serait plus vraiment manuel.
        - « favorites » : favoris en tête, le reste au glisser-déposer.
        """
        mode = mode if mode in self.SORT_MODES else "viewers"
        if mode == self._sort_mode:
            return
        self._sort_mode = mode
        # Le curseur « main ouverte » est la seule indication qu'une cellule est
        # saisissable : sans lui, rien ne le laisse deviner.
        shape = (Qt.CursorShape.OpenHandCursor if self.is_draggable()
                 else Qt.CursorShape.PointingHandCursor)
        for c in self._cells:
            if c.twitch_login and c.is_online:
                c.setCursor(QCursor(shape))
        # Un changement de mode doit s'appliquer tout de suite : sans cela
        # l'hystérésis anti-clignotement retenait le nouveau classement.
        self._last_reorder = 0.0
        self._reposition_cells()

    def _on_stream_ended(self, login: str) -> None:
        """Libère la cellule d'un flux terminé et laisse la place à un autre.

        L'API met souvent plusieurs minutes à basculer un streamer en
        « offline » : sans ça, la cellule restait noire tout ce temps alors que
        d'autres streamers en direct attendaient une place.
        """
        for cell in self._cells:
            if cell.twitch_login == login:
                cell.stop_stream()
                cell.set_empty()
                break
        self._ended[login] = time.monotonic()
        self._applied_order = [lg for lg in self._applied_order if lg != login]
        self._insertion_order = [lg for lg in self._insertion_order if lg != login]
        self._cell_map.pop(login, None)
        # On rejoue le placement : un streamer live hors grille peut monter.
        if self._last_streamers:
            self.update_streamers(self._last_streamers, self._last_selected)
        else:
            self._reposition_cells()

    def _ordered_for_display(self, active: list["StreamCell"]) -> list["StreamCell"]:
        """Classe les cellules par viewers décroissants, avec hystérésis.

        Le nouvel ordre n'est adopté que s'il diffère de celui appliqué ET
        qu'un délai minimal s'est écoulé : deux streams aux audiences proches
        échangeraient sinon leur place à chaque sondage.
        """
        # Les favoris passent devant dans les deux modes : c'est tout l'intérêt
        # de les marquer.
        def _fav_first(cells: list["StreamCell"]) -> list["StreamCell"]:
            favs = favorites.get()
            return sorted(cells, key=lambda c: c.twitch_login.lower() not in favs)

        if self._sort_mode in ("manual", "favorites"):
            # L'ordre voulu par l'utilisateur d'abord ; les cellules qu'il n'a
            # jamais placées (nouvelles arrivées) viennent ensuite.
            rank = {lg: i for i, lg in enumerate(self._applied_order)}
            ordered = sorted(active, key=lambda c: rank.get(c.twitch_login, 10_000))
            if self._sort_mode == "favorites":
                ordered = _fav_first(ordered)
            self._applied_order = [c.twitch_login for c in ordered]
            return ordered

        by_viewers = _fav_first(
            sorted(active, key=lambda c: -getattr(c, "_viewers", 0))
        )
        wanted = [c.twitch_login for c in by_viewers]
        if wanted == self._applied_order:
            return by_viewers

        now = time.monotonic()
        settled = set(self._applied_order) == set(wanted)
        if settled and (now - self._last_reorder) < self._REORDER_MIN_INTERVAL_S:
            # Mêmes streams, seul le classement bouge : on attend.
            order = {lg: i for i, lg in enumerate(self._applied_order)}
            return sorted(active, key=lambda c: order.get(c.twitch_login, 1_000))

        self._applied_order = wanted
        self._last_reorder = now
        return by_viewers

    def move_cell(self, login: str, target: str) -> None:
        """Place `login` dans la case occupée par `target`.

        `target` vide = déplacement en fin de grille.

        L'index de destination est relevé AVANT le retrait de la cellule
        déplacée, et c'est tout l'enjeu : en le calculant après, chaque cellule
        située à droite de la source remontait d'un cran, et le dépôt atterrissait
        systématiquement une case trop à gauche dès qu'on glissait vers la
        droite. Vers la gauche, le retrait ne décalait rien et la position était
        juste — d'où un défaut qui ne se voyait que dans un sens.
        """
        order = list(self._applied_order)
        if login not in order:
            order.append(login)
        if target and target in order and target != login:
            dst = order.index(target)
            order.remove(login)
            order.insert(dst, login)
        else:
            order.remove(login)
            order.append(login)
        self._applied_order = order
        self._last_reorder = time.monotonic()
        self._reposition_cells()

    def _reposition_cells(self) -> None:
        """Recalcule et applique la géométrie de chaque cellule active.

        Algorithme :
        - N streams actifs → _compute_grid_dims(N) → (rows, cols)
        - Chaque rangée est divisée en len(rangée) cellules égales
        - La dernière rangée (incomplète) reçoit des cellules plus larges
        - AUCUNE cellule noire : les cellules vides sont masquées
        """
        active = [c for c in self._cells if c.twitch_login]
        active = self._ordered_for_display(active)
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
        if self._max_active_streams < old:
            active = [c for c in self._cells if c.twitch_login]
            # La valeur BORNEE, pas le n brut : avec un plafond negatif bricole
            # dans config.json, active[-2:] ne vidait que les deux DERNIERES
            # cellules et la grille restait tres au-dessus de son plafond.
            for cell in active[self._max_active_streams:]:
                cell.stop_stream()
                cell.set_empty()

    def set_active_stream(self, twitch_login: str | None) -> None:
        """Met à jour le contour vert de la cellule active (stream en fullscreen).

        Passe par `set_active` plutôt que de peindre les cellules elle-même :
        les deux routes existaient en parallèle, et celle-ci ne mettait pas
        `_active_login` à jour — la grille croyait donc encore active la
        dernière cellule CLIQUÉE, alors que le plein écran avait changé par le
        clavier, la télécommande ou la palette.
        """
        self.set_active(twitch_login or "")

    def set_quality_provider(self, provider: "Callable[[int], str]") -> None:
        """Installe la fonction qui décide de la qualité selon le nombre de flux."""
        self._quality_provider = provider

    def restart_all_streams(self, quality: str) -> None:
        """Relancer tous les streams actifs avec la nouvelle qualité.

        Sans effet si la qualité demandée est déjà celle en place : une relance
        coupe et recharge CHAQUE cellule (résolution streamlink comprise, une
        dizaine de secondes de noir), coût inacceptable pour un changement qui
        n'en est pas un.
        """
        if quality == self._grid_quality:
            logger.debug("GridWidget: qualité déjà %s — relance inutile", quality)
            return
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
            cell.set_active(bool(twitch_login)
                            and cell.twitch_login == twitch_login)
        # Le plein écran porte déjà le son de cette chaîne : la garder épinglée
        # la faisait entendre DEUX FOIS, la cellule et le grand écran jouant le
        # même flux avec quelques secondes d'écart.
        if twitch_login:
            self.unpin_audio(twitch_login)

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
            self._vider_grille()
            return

        live = self._flux_en_direct(all_s, set(selected_logins))
        if live is None:
            return

        # Décider qui est dans la grille (par viewers) → pour le cut
        kept_logins = {s.twitch_login for s in live[: self._max_active_streams]}  # type: ignore[union-attr]
        to_show_map: dict[str, object] = {
            s.twitch_login: s  # type: ignore[union-attr]
            for s in live if s.twitch_login in kept_logins  # type: ignore[union-attr]
        }
        # Qualité décidée avant de peupler : en mode adaptatif elle dépend du
        # nombre de flux qui vont effectivement tourner.
        if self._quality_provider is not None:
            self._grid_quality = self._quality_provider(len(kept_logins))

        self._appliquer_cellules(to_show_map, kept_logins, self._grid_quality)
        self._reposition_cells()
        self._first_load = False
        self._emit_active_count()

    def _vider_grille(self) -> None:
        """Plus aucune sélection : toutes les cellules s'arrêtent et se vident."""
        for cell in self._cells:
            cell.stop_stream()
            cell.set_empty()
        self._reposition_cells()
        self._cell_map = {}

    def _flux_en_direct(self, all_s: list, sel_set: set) -> list | None:
        """Sélectionnés ET en ligne, triés par audience décroissante.

        Renvoie None si la liste reçue n'est pas exploitable — l'appelant
        s'abstient alors de toucher à la grille plutôt que de la vider.
        """
        try:
            live = sorted(
                [s for s in all_s  # type: ignore[union-attr]
                 if getattr(s, "twitch_login", None) in sel_set
                 and getattr(s, "online", False)],
                key=lambda s: -(getattr(s, "viewers", 0)),
            )
        except Exception:
            return None

        # Écarter les flux dont on a constaté la fin, tant que le délai court
        # et que l'API n'a pas confirmé leur passage hors ligne.
        now_m = time.monotonic()
        self._ended = {
            lg: t for lg, t in self._ended.items()
            if now_m - t < self._ENDED_COOLDOWN_S
        }
        still_live = {s.twitch_login for s in live}  # type: ignore[union-attr]
        # Un streamer passé offline puis revenu n'a plus à être écarté.
        self._ended = {lg: t for lg, t in self._ended.items() if lg in still_live}
        return [s for s in live if s.twitch_login not in self._ended]  # type: ignore[union-attr]

    def _appliquer_cellules(self, to_show_map: dict, new_logins_set: set,
                            quality: str) -> None:
        """Retire les partis, rafraîchit les présents, place les nouveaux.

        Les cellules ne bougent jamais : un arrivant prend une case libre plutôt
        que de décaler tout le monde.
        """
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

    def set_clip_config(self, cfg: dict) -> None:
        """Applique la configuration des clips à toutes les cellules."""
        clips = (cfg or {}).get("clips") or {}
        enabled = bool(clips.get("grid_enabled", True))
        self._clip_secs = max(10, int(clips.get("duration_secs", 60) or 60))
        # Le repli est explicite : passer une chaîne vide plus bas laissait
        # la reprise en pleine qualité écrire dans le dossier temporaire.
        self._clip_dir = str(clips.get("directory") or "") or str(CLIPS_DEFAUT)
        for cell in self._cells:
            cell.set_clip_buffer(self._clip_secs if enabled else 0)
        logger.info("Clips depuis la grille : %s (%d s)",
                    "activés" if enabled else "désactivés", self._clip_secs)

    def set_low_latency(self, on: bool) -> None:
        """Applique la basse latence à toutes les cellules, actuelles et à venir."""
        self._low_latency = bool(on)
        for cell in self._cells:
            cell.set_low_latency(self._low_latency)
        logger.info("Basse latence dans la grille : %s",
                    "activée" if self._low_latency else "désactivée")

    def save_clip(self, login: str) -> bool:
        """Lance la sauvegarde du moment en cours de `login`.

        Rend True quand la demande est partie — le chemin n'existe pas encore.
        C'est la seule réponse honnête d'un travail asynchrone, et le plafond
        horaire des clips automatiques compte les demandes, pas les fichiers.

        Le clip est repris chez Twitch en pleine qualité, comme le replay : une
        cellule joue en 360p, et garder soixante secondes de 360p vaut moins que
        trente secondes de 1080p — un clip, on le conserve.

        Le téléchargement tourne sur un fil séparé pour ne pas figer la grille ;
        `clip_saved` est émis à la fin, comme avant, et le tampon local de la
        cellule sert de repli.
        """
        cell = self._cell_map.get(login)
        if cell is None:
            logger.warning("Clip de %s impossible : cellule absente", login)
            self.clip_saved.emit(login, "")
            return False
        self._lancer_clip(login, cell)
        return True

    def _lancer_clip(self, login: str, cell: "StreamCell") -> None:
        """Démarre le fil de sauvegarde.

        Isolé pour lui-même : c'est la seule ligne qui sorte du fil graphique,
        et un test qui exerce la sauvegarde n'a pas à partir sur le réseau.
        """
        threading.Thread(target=self._ecrire_clip, args=(login, cell),
                         daemon=True, name=f"clip-{login}").start()

    def _ecrire_clip(self, login: str, cell: "StreamCell") -> None:
        """Fil de sauvegarde : pleine qualité si possible, tampon local sinon."""
        from core.replay_hd import recuperer

        path = ""
        try:
            path, _obtenue = recuperer(login, REPLAY_SECS, self._clip_dir,
                                       prefixe="clip")
        except Exception:  # le repli reste disponible  # noqa: BLE001
            logger.exception("Clip de %s : reprise en pleine qualité impossible",
                             login)
        if not path:
            path = cell.save_clip(REPLAY_SECS, self._clip_dir) or ""
            if path:
                logger.info("Clip de %s repris du tampon local (360p)", login)
        if path:
            logger.info("Clip de %s : %s", login, path)
        else:
            logger.warning("Clip de %s impossible (tampon coupé ?)", login)
        self._clip_ecrit.emit(login, path)

    def request_replay(self, login: str) -> None:
        """Extrait le moment de `login` dans un fichier temporaire et le signale.

        Le fichier va dans le répertoire temporaire du système, pas dans le
        dossier des clips : un replay est jetable, il n'a pas à se mêler aux
        moments qu'on a choisi de garder.
        """
        import tempfile
        cell = self._cell_map.get(login)
        if cell is None:
            return
        path = cell.save_clip(REPLAY_SECS, tempfile.gettempdir())
        if path:
            self.replay_requested.emit(login, path, REPLAY_SECS)
        else:
            logger.warning("Replay de %s impossible (tampon coupé ?)", login)

    def _on_audio_pin_requested(self, login: str) -> None:
        """Ajoute ou retire une chaîne des audios épinglés.

        Épingler la chaîne du plein écran n'a pas de sens : son son y passe
        déjà, et la cellule le rejouerait par-dessus avec le décalage des deux
        lecteurs.
        """
        if not login:
            return
        if login == self._active_login:
            logger.info("Audio de %s non épinglé : déjà joué en plein écran",
                        login)
            return
        if login in self._audio_pinned_logins:
            self._audio_pinned_logins.discard(login)
        else:
            self._audio_pinned_logins.add(login)
        self._apply_audio_pin()

    def pinned_audio_logins(self) -> list[str]:
        """Chaînes épinglées, dans l'ordre où elles apparaissent dans la grille."""
        seen = [c.twitch_login for c in self._ordered_for_display(
            [c for c in self._cells if c.twitch_login])]
        return [lg for lg in seen if lg in self._audio_pinned_logins]

    def _apply_audio_pin(self) -> None:
        """Synchronise mute/unmute sur toutes les cellules et signale la liste."""
        present = {c.twitch_login for c in self._cells if c.twitch_login}
        # Une chaîne qui a quitté la grille ne peut plus être entendue : garder
        # son épingle afficherait une entrée sans son dans la liste.
        stale = self._audio_pinned_logins - present
        if stale:
            self._audio_pinned_logins -= stale
        for cell in self._cells:
            cell.set_audio_pinned(cell.twitch_login in self._audio_pinned_logins)
        self.audio_pins_changed.emit(self.pinned_audio_logins())

    def unpin_audio(self, login: str) -> None:
        """Retire une chaîne des audios épinglés. Sans effet si absente."""
        if login in self._audio_pinned_logins:
            self._audio_pinned_logins.discard(login)
            self._apply_audio_pin()

    def set_cell_volume(self, login: str, volume: int) -> None:
        """Règle le volume d'une cellule épinglée (console de mixage)."""
        cell = self._cell_map.get(login)
        if cell is not None:
            cell.set_volume(volume)

    def set_cell_muted(self, login: str, muted: bool) -> None:
        """Coupe ou rétablit une cellule depuis la console."""
        cell = self._cell_map.get(login)
        if cell is not None:
            cell.set_mix_muted(muted)

    def pulse_cell(self, login: str, color: str = "#f5c518",
                   seconds: float = 6.0) -> None:
        """Fait pulser la cellule d'une chaîne, si elle est affichée."""
        cell = self._cell_map.get(login)
        if cell is not None:
            cell.pulse_hype(color, seconds=seconds)

    def pulse_all(self, color: str = "#f5c518", pulses: int = 4) -> None:
        """Fait pulser TOUTES les cellules occupées.

        Un palier de cagnotte n'appartient à personne en particulier : le
        signaler sur une seule cellule serait trompeur.
        """
        for cell in self._cells:
            if cell.twitch_login:
                cell.pulse_hype(color, pulses=pulses)

    def goal_achieved_flash(self, login: str, goal_name: str) -> None:
        """Signale un objectif accompli sur la grille, par le liseré SEUL.

        Plus aucun toast ici. La grille est la seule fenêtre entièrement faite
        de vidéo : tout ce qu'on y pose recouvre un flux. Le liseré vert, lui,
        ne masque rien et désigne QUI — ce que le toast disait moins bien.

        L'objectif est annoncé ailleurs, deux fois plutôt qu'une : un bandeau
        sur le direct, là où l'on regarde, et une entrée dans le fil de
        l'Accueil, qui reste consultable après coup. Une chaîne absente de la
        grille n'y perd donc rien.
        """
        cell = self._cell_map.get(login)
        if cell is not None:
            # Dix secondes : un objectif atteint doit rester visible le temps
            # qu'on lève les yeux vers la grille, pas le temps d'un battement.
            cell.pulse_hype("#00ff87", seconds=10.0)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Arrête tous les streams MPV avant de fermer."""
        for cell in self._cells:
            cell.stop_stream()
        super().closeEvent(event)

    def show_hype_toast(
        self, cell_idx: int, label: str, score: float, color: str = "#ff6b00",
    ) -> None:
        """Affiche un toast éphémère en haut à droite de la grille."""
        login = ""
        if 0 <= cell_idx < len(self._cells):
            login = self._cells[cell_idx].twitch_login
        toast = _HypeToast(label, score, color, self, login,
                           can_clip=bool(self._clip_secs and login))
        toast.clip_requested.connect(self.save_clip)
        toast.replay_requested.connect(self.request_replay)
        toast.show()
        toast.raise_()

    # -- test data -------------------------------------------------------------

    def _load_test_data(self) -> None:
        data: list[dict[str, object]] = []
        for s in _TEST_STREAMERS:
            v = s["viewers"]
            # Bruit cosmetique sur des donnees de test : aucun enjeu de
            # securite, secrets.randbelow serait deplace ici.
            jitter = random.randint(-200, 200) if isinstance(v, int) and v > 0 else 0  # NOSONAR
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
