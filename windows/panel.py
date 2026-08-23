"""Fenêtre panel — stats, programme, donation goals, grille optionnelle."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QCursor, QFont, QFontMetrics, QLinearGradient, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QRegion, QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
import pyqtgraph as pg
try:
    import qtawesome as qta
    _QTA_OK = True
except ImportError:
    _QTA_OK = False
pg.setConfigOptions(background="#111111", foreground="#888888", antialias=True)

_CHARTJS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
# Empreinte de chart.js 4.4.0 (dist/chart.umd.min.js, 205 222 octets).
_CHARTJS_SHA256 = "0e2326c6868072bec1592760c6729043caeea2960a2b46cee6a2192aac6abff0"
_CHARTJS_PATH = Path.home() / ".zlink" / "chart.min.js"
_CHARTJS_MAX_BYTES = 4 * 1024 * 1024


def _ensure_chartjs() -> None:
    """Télécharge Chart.js une fois et vérifie son empreinte à chaque démarrage.

    Le script est inliné dans une page chargée en origine file:// : un fichier
    altéré (CDN compromis, écriture par un autre processus dans ~/.zlink)
    s'exécuterait dans le QWebEngineView.
    """
    log = logging.getLogger(__name__)
    if _CHARTJS_PATH.exists():
        if hashlib.sha256(_CHARTJS_PATH.read_bytes()).hexdigest() == _CHARTJS_SHA256:
            return
        log.error("Chart.js: empreinte locale inattendue — fichier écarté")
        _CHARTJS_PATH.unlink(missing_ok=True)
    try:
        import urllib.request
        with urllib.request.urlopen(_CHARTJS_URL, timeout=15) as resp:
            payload = resp.read(_CHARTJS_MAX_BYTES + 1)
        if len(payload) > _CHARTJS_MAX_BYTES:
            log.error("Chart.js: réponse trop volumineuse, rejetée")
            return
        digest = hashlib.sha256(payload).hexdigest()
        if digest != _CHARTJS_SHA256:
            log.error("Chart.js: empreinte %s… inattendue — téléchargement rejeté", digest[:12])
            return
        _CHARTJS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CHARTJS_PATH.write_bytes(payload)
    except Exception as e:
        log.warning("Chart.js download failed: %s", e)


threading.Thread(target=_ensure_chartjs, daemon=True).start()

from core.api_client import (
    DonationGoal,
    EventItem,
    GlobalStats,
    GoalWithStreamer,
    StreamerInfo,
    fetch_donation_goals,
)
from core.gemini_client import GeminiClient
from core.history_store import HistoryStore
from typing import TYPE_CHECKING
from widgets.bigscreen_widget import BigScreenWidget

if TYPE_CHECKING:
    from windows.grid import GridWindow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

PANEL_STYLE = """
QMainWindow, QWidget#central {
    background-color: #0a0a0a;
}

/* Header */
QWidget#header {
    background-color: #111111;
    border-bottom: 1px solid #222222;
}

/* Tab bar */
QWidget#tabBar {
    background-color: #0a0a0a;
}

/* Scroll areas */
QScrollArea {
    background-color: #0a0a0a;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background-color: #0a0a0a;
}
QScrollBar:vertical {
    background-color: #111111;
    width: 6px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #333333;
    min-height: 30px;
    border-radius: 3px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* Cards */
QFrame#card {
    background-color: #111111;
    border: 1px solid #222222;
    border-radius: 4px;
}

/* Streamer row */
QFrame#streamerRow {
    background-color: #0a0a0a;
    border: none;
    border-radius: 0;
}
QFrame#streamerRow:hover {
    background-color: #111111;
}

/* Event row */
QFrame#eventRow {
    background-color: #0a0a0a;
    border-bottom: 1px solid #222222;
}

/* Watch button */
QPushButton#watchBtn {
    background-color: transparent;
    color: #00ff87;
    border: 1px solid #00ff87;
    border-radius: 4px;
    padding: 2px 8px;
    font-family: "Segoe UI Variable";
    font-size: 11px;
}
QPushButton#watchBtn:hover {
    background-color: #00ff87;
    color: #0a0a0a;
}

/* Combo box (Goals tab) */
QComboBox {
    background-color: #111111;
    color: #ffffff;
    border: 1px solid #333333;
    border-radius: 4px;
    padding: 4px 8px;
    font-family: "Segoe UI Variable";
    font-size: 11px;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #111111;
    color: #ffffff;
    border: 1px solid #333333;
    selection-background-color: #222222;
}

/* Placeholder */
QLabel#placeholder {
    color: #555555;
    font-family: "Segoe UI Variable";
    font-size: 14px;
}

/* Checkbox (onglet Streamers) */
QCheckBox {
    spacing: 6px;
    color: #ffffff;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    background-color: #111111;
    border: 1px solid #333333;
    border-radius: 3px;
}
QCheckBox::indicator:checked {
    background-color: #00ff87;
    border-color: #00ff87;
}
QCheckBox::indicator:hover {
    border-color: #00ff87;
}
"""

# ---------------------------------------------------------------------------
# Shared style fragments & repeated literals
# ---------------------------------------------------------------------------
_WAIT_MSG        = "\ud83d\udc9a ZEvent 2026 \u2014 En attente de donn\u00e9es\u2026"
_GAME_JC         = "Just Chatting"
_FONT_SEGOE      = "Segoe UI Variable"  # UI générale (labels, boutons, titres)
_FONT_MONO       = "Cascadia Code"      # données chiffrées (viewers, stats, axes)
_SS_WHITE        = "color: #ffffff;"
_SS_GREY         = "color: #555555;"
_SS_MUTED        = "color: #888888;"
_SS_GREEN        = "color: #00ff87;"
_SS_GREEN_SPACED = "color: #00ff87; letter-spacing: 1px;"
_SS_WHITE_CLEAR  = "color: #ffffff; border: none; background: transparent;"
_SS_GREEN_CLEAR  = "color: #00ff87; border: none; background: transparent;"
_SS_GREY_CLEAR   = "color: #555555; border: none; background: transparent;"
_SS_BG_DARK      = "background-color: #0a0a0a;"


def _bold_font(family: str, size: int) -> "QFont":
    """Retourne QFont(family, size) avec Bold — évite le constructeur 3-args de PyQt6."""
    f = QFont(family, size)
    f.setBold(True)
    return f

# ---------------------------------------------------------------------------
# Test data (initial state avant le premier poll)
# ---------------------------------------------------------------------------

_TEST_STREAMERS: list[StreamerInfo] = [
    StreamerInfo("zerator", "ZeratoR", True, "Minecraft", "LAN", 42_350, 694_000.0, "694 000 €", ""),
    StreamerInfo("domingo", "Domingo", True, _GAME_JC, "LAN", 38_200, 1_200_000.0, "1 200 000 €", ""),
    StreamerInfo("antoinedaniel", "Antoine Daniel", True, "Gartic Phone", "LAN", 22_100, 533_000.0, "533 000 €", ""),
    StreamerInfo("mistermv", "MisterMV", True, "Balatro", "LAN", 18_700, 325_000.0, "325 000 €", ""),
    StreamerInfo("joyca", "Joyca", True, "GeoGuessr", "LAN", 15_400, 278_000.0, "278 000 €", ""),
    StreamerInfo("squeezie", "Squeezie", True, "Fortnite", "LAN", 54_800, 289_000.0, "289 000 €", ""),
    StreamerInfo("samueletienne", "Samuel Etienne", True, _GAME_JC, "LAN", 8_400, 464_000.0, "464 000 €", ""),
    StreamerInfo("bagherajones", "Baghera Jones", True, "Minecraft", "LAN", 11_800, 198_000.0, "198 000 €", ""),
    StreamerInfo("ponce", "Ponce", True, "Trackmania", "LAN", 11_200, 187_000.0, "187 000 €", ""),
    StreamerInfo("etoiles", "Etoiles", True, "Genshin Impact", "Online", 6_900, 143_000.0, "143 000 €", ""),
    StreamerInfo("moman", "MoMaN", True, _GAME_JC, "LAN", 7_600, 112_000.0, "112 000 €", ""),
    StreamerInfo("lapi", "Lapi", True, "Valorant", "Online", 5_300, 89_000.0, "89 000 €", ""),
    StreamerInfo("avamind", "Avamind", True, _GAME_JC, "Online", 4_800, 76_000.0, "76 000 €", ""),
    StreamerInfo("mastu", "Mastu", True, "League of Legends", "LAN", 3_900, 65_000.0, "65 000 €", ""),
    StreamerInfo("helydia", "Helydia", True, "Art", "Online", 3_400, 54_000.0, "54 000 €", ""),
    StreamerInfo("deujna", "Deujna", False, _GAME_JC, "LAN", 0, 45_000.0, "45 000 €", ""),
    StreamerInfo("chelxie", "Chelxie", False, "Art", "Online", 0, 23_000.0, "23 000 €", ""),
]

_TEST_STATS = GlobalStats(
    donation_total=16_182_382.0,
    donation_formatted="16 182 382 €",
    viewers_total=sum(s.viewers for s in _TEST_STREAMERS),
    website_mode="offline",
)

_TEST_EVENTS: list[EventItem] = [
    EventItem("", "Ouverture du ZEvent 2026", "2026-09-03", "10:00", "11:00", "",
              host_uuids=["zerator"], participant_uuids=["zerator", "domingo"]),
    EventItem("", "Tournoi Trackmania", "2026-09-03", "11:00", "13:00", "",
              host_uuids=["ponce"], participant_uuids=["ponce", "mistermv"]),
    EventItem("", "Course Minecraft", "2026-09-03", "14:00", "16:00", "",
              host_uuids=["zerator"], participant_uuids=["zerator", "joyca", "etoiles"]),
    EventItem("", "Blind Test Musical", "2026-09-03", "16:00", "18:00", "",
              host_uuids=["antoinedaniel"], participant_uuids=["antoinedaniel", "squeezie"]),
    EventItem("", "GeoGuessr Battle Royale", "2026-09-03", "18:00", "20:00", "",
              host_uuids=["joyca"], participant_uuids=["joyca", "domingo"]),
    EventItem("", "Show principal — Karaoké", "2026-09-03", "20:00", "23:00", "",
              host_uuids=["zerator"], participant_uuids=[]),
    EventItem("", "Session chill nocturne", "2026-09-03", "23:00", "02:00", "",
              host_uuids=["ponce"], participant_uuids=["ponce"]),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_euros(n: float) -> str:
    return f"{int(n):,} €".replace(",", "\u00a0")


def _fmt_viewers(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _clear_layout(layout) -> None:  # type: ignore[type-arg]
    """Supprime tous les widgets d'un layout."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()



# ---------------------------------------------------------------------------
# _EuroAxisItem — axe Y formaté en euros
# ---------------------------------------------------------------------------

class _EuroAxisItem(pg.AxisItem):
    """AxisItem pyqtgraph formaté en euros (16.2M, 500k…). Sans notation scientifique."""

    def tickStrings(self, values: list, scale: float, spacing: float) -> list[str]:  # type: ignore[override]
        result: list[str] = []
        for v in values:
            fv = float(v)
            if fv >= 1_000_000:
                result.append(f"{fv / 1_000_000:.1f}M")
            elif fv >= 1_000:
                result.append(f"{fv / 1_000:.0f}k")
            elif fv > 0:
                result.append(str(int(fv)))
            else:
                result.append("")
        return result


_JOURS_COURT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
_PARIS_OFFSET = timedelta(hours=2)  # UTC+2 (CEST) pendant l'event


class DateAxisItem(pg.AxisItem):
    """AxisItem pyqtgraph qui affiche les timestamps Unix en jour + heure.

    spacing > 7200 (> 2h) → "Ven 09h" / "Sam 12h"…
    spacing ≤ 7200       → "09h30"
    """

    def tickStrings(self, values: list, scale: float, spacing: float) -> list[str]:  # type: ignore[override]
        from datetime import timezone as _tz
        _epoch = datetime(1970, 1, 1, tzinfo=_tz.utc)
        result: list[str] = []
        for v in values:
            try:
                dt_utc = _epoch + timedelta(seconds=float(v))
                dt_paris = dt_utc + _PARIS_OFFSET
                if spacing > 7200:
                    result.append(f"{_JOURS_COURT[dt_paris.weekday()]} {dt_paris.strftime('%Hh')}")
                else:
                    result.append(dt_paris.strftime("%Hh%M"))
            except Exception:
                result.append("")
        return result


# ---------------------------------------------------------------------------
# Tab: Accueil — layout fixe 5 zones
# ---------------------------------------------------------------------------

# ── helper ────────────────────────────────────────────────────────────────

def _make_round_pixmap(pixmap: QPixmap, size: int = 40) -> QPixmap:
    """Rogne un QPixmap en cercle à la dimension donnée."""
    scaled = pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setClipRegion(QRegion(0, 0, size, size, QRegion.RegionType.Ellipse))
    p.drawPixmap(0, 0, scaled)
    p.end()
    return out


# ── Bandeau Gemini ────────────────────────────────────────────────────────────

class _AccueilGeminiBanner(QWidget):
    """Bande 36px fond #0d0d0d — message statique avec transition fade 300ms."""

    _ROTATE_MS = 30_000   # rotation automatique toutes les 30s
    _FADE_MS   = 300      # durée fade in / fade out

    # Templates fallback locaux
    _TEMPLATES = [
        "💚 {donation} récoltés pour la cause — merci à tous",
        "🏆 {live_count} streamers en live simultanément ce soir",
        "📡 {viewers} viewers connectés en ce moment",
        "💚 {donation} et on continue — vous êtes incroyables",
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setStyleSheet("background-color: #0d0d0d; border-bottom: 1px solid #1e1e1e;")
        self._gemini = GeminiClient()
        self._context: dict = {}
        self._messages: list[str] = [_WAIT_MSG]
        self._msg_index: int = 0
        self._fading_out: bool = False
        self._FONT = QFont(_FONT_MONO, 12)

        # Label centrable
        self._label = QLabel()
        # Messages générés par le LLM : texte brut, jamais de balisage interprété.
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setFont(self._FONT)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "color: #ffffff; background: transparent; border: none; padding: 0 12px;"
        )
        self._label.setText(self._messages[0])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._label)

        # Effet d'opacité
        self._opacity = QGraphicsOpacityEffect(self._label)
        self._opacity.setOpacity(1.0)
        self._label.setGraphicsEffect(self._opacity)

        # Animation fade
        self._anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.finished.connect(self._on_anim_finished)

        # Timer rotation
        self._rotate_timer = QTimer(self)
        self._rotate_timer.setInterval(self._ROTATE_MS)
        self._rotate_timer.timeout.connect(self._start_fade_out)
        self._rotate_timer.start()

        # Gemini refresh toutes les 5 min
        self._last_ai_call: float = 0.0
        self._gemini_timer = QTimer(self)
        self._gemini_timer.setInterval(5 * 60 * 1000)
        self._gemini_timer.timeout.connect(self._refresh_gemini)
        self._gemini_timer.start()

        self._refresh_gemini()

    # -- animation -----------------------------------------------------------

    def _start_fade_out(self) -> None:
        if self._anim.state() == QPropertyAnimation.State.Running:
            return
        self._fading_out = True
        self._anim.stop()
        self._anim.setDuration(self._FADE_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if self._fading_out:
            # Swap message
            self._msg_index = (self._msg_index + 1) % len(self._messages)
            self._label.setText(self._messages[self._msg_index])
            self._fading_out = False
            self._anim.setDuration(self._FADE_MS)
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.start()

    def _show_text_now(self, text: str) -> None:
        """Injecte un message urgent : fade out immédiat, puis affiche le texte."""
        if text not in self._messages:
            self._messages.insert(self._msg_index + 1, text)
        self._start_fade_out()
        self._rotate_timer.start()   # reset le timer de rotation

    # -- fallback ------------------------------------------------------------

    def _build_fallback_messages(self) -> list[str]:
        ctx = self._context
        msgs: list[str] = []
        for tpl in self._TEMPLATES:
            try:
                msgs.append(tpl.format(**ctx))
            except KeyError:
                pass
        return msgs or [_WAIT_MSG]

    # -- public API ----------------------------------------------------------

    def set_context(self, context: dict) -> None:
        old_donation = float(self._context.get("_raw_donation", 0))
        new_donation = float(context.get("_raw_donation", 0))
        old_live = self._context.get("live_count", 0)
        new_live = context.get("live_count", 0)
        self._context = context

        milestone_hit = int(new_donation) // 500_000 > int(old_donation) // 500_000
        live_changed = new_live != old_live

        # Mettre à jour les messages fallback avec les nouvelles données
        self._messages = self._build_fallback_messages()
        if self._msg_index >= len(self._messages):
            self._msg_index = 0

        if milestone_hit or live_changed:
            self._refresh_gemini(urgent=True)

    def trigger_refresh(self) -> None:
        self._refresh_gemini(urgent=True)

    # -- Gemini --------------------------------------------------------------

    _AI_COOLDOWN_S: float = 600.0        # 10 min entre deux appels programmés
    _AI_URGENT_COOLDOWN_S: float = 300.0   # 5 min entre deux appels urgents

    def _refresh_gemini(self, urgent: bool = False) -> None:
        import threading, time as _t
        now = _t.time()
        cooldown = self._AI_URGENT_COOLDOWN_S if urgent else self._AI_COOLDOWN_S
        if now - self._last_ai_call < cooldown:
            logger.debug("GeminiBanner: skip AI call (cooldown %.0fs)", cooldown - (now - self._last_ai_call))
            return
        self._last_ai_call = now
        ctx = dict(self._context)

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                text = loop.run_until_complete(
                    self._gemini.generate_announcement(ctx)
                )
            except Exception as exc:
                logger.debug("GeminiBanner Gemini: %s", exc)
                text = None
            finally:
                loop.close()
            if text:
                if urgent:
                    QTimer.singleShot(0, lambda t=text: self._show_text_now(t))
                else:
                    QTimer.singleShot(0, lambda t=text: self._messages.__setitem__(
                        slice(None), self._build_fallback_messages() + [t]
                    ))
            else:
                QTimer.singleShot(0, lambda: setattr(
                    self, "_messages", self._build_fallback_messages()
                ))

        threading.Thread(target=_worker, daemon=True).start()


# ── Player card top 3 ──────────────────────────────────────────────────────

class _AccueilPlayerCard(QFrame):
    """Card streamer top 3 — hauteur imposée par le parent à 150px."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame { background-color: #111111; border: 1px solid #1e1e1e; "
            "border-radius: 6px; }"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(6)

        # Ligne 1 — avatar + nom/jeu + rang
        header = QHBoxLayout()
        header.setSpacing(10)
        self._avatar = QLabel()
        self._avatar.setFixedSize(40, 40)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar.setStyleSheet(
            "background-color: #333333; border-radius: 20px; "
            "font-family: 'Segoe UI Variable'; font-size: 14px; font-weight: bold; "
            "color: #ffffff; border: none;"
        )
        header.addWidget(self._avatar)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self._name_lbl = QLabel("—")
        self._name_lbl.setFont(_bold_font(_FONT_SEGOE, 13))
        self._name_lbl.setStyleSheet(_SS_WHITE_CLEAR)
        name_col.addWidget(self._name_lbl)
        self._game_lbl = QLabel("")
        self._game_lbl.setFont(QFont(_FONT_SEGOE, 11))
        self._game_lbl.setStyleSheet("color: #666666; border: none; background: transparent;")
        name_col.addWidget(self._game_lbl)
        header.addLayout(name_col, stretch=1)

        self._rank_lbl = QLabel("#—")
        self._rank_lbl.setFont(_bold_font(_FONT_MONO, 18))
        self._rank_lbl.setStyleSheet(_SS_GREEN_CLEAR)
        self._rank_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        header.addWidget(self._rank_lbl)
        root.addLayout(header)

        # Ligne 2 — barre viewers (QProgressBar 3px)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(3)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            "QProgressBar { background-color: #222222; border: none; border-radius: 1px; }"
            "QProgressBar::chunk { background-color: #00ff87; border-radius: 1px; }"
        )
        root.addWidget(self._bar)

        # Ligne 3 — viewers
        self._viewers_lbl = QLabel("0 viewers")
        self._viewers_lbl.setFont(QFont(_FONT_MONO, 12))
        self._viewers_lbl.setStyleSheet(_SS_GREEN_CLEAR)
        root.addWidget(self._viewers_lbl)

        # Ligne 4 — cagnotte
        self._donation_lbl = QLabel("")
        self._donation_lbl.setFont(QFont(_FONT_MONO, 11))
        self._donation_lbl.setStyleSheet("color: #888888; border: none; background: transparent;")
        root.addWidget(self._donation_lbl)

    def set_streamer(
        self, streamer: StreamerInfo, rank: int, max_viewers: int
    ) -> None:
        self._name_lbl.setText(streamer.display[:20])
        self._game_lbl.setText(streamer.game or "")
        self._rank_lbl.setText(f"#{rank}")
        self._viewers_lbl.setText(f"{_fmt_viewers(streamer.viewers)} viewers")
        self._donation_lbl.setText(streamer.donation_formatted)
        ratio = int(streamer.viewers * 100 / max_viewers) if max_viewers > 0 else 0
        self._bar.setValue(ratio)
        self._set_initials(streamer.twitch_login)
        if streamer.profile_url:
            from widgets.bigscreen_widget import load_avatar_into_label as _load_av
            _load_av(self._avatar, streamer.twitch_login, streamer.display, 40, streamer.profile_url)

    def _set_initials(self, login: str) -> None:
        self._avatar.setPixmap(QPixmap())
        self._avatar.setText(login[:2].upper())


# ── Timeline ───────────────────────────────────────────────────────────────

class _AccueilTimeline(QWidget):
    """Timeline 110px — 8h autour de maintenant, paintEvent, refresh 1s."""

    event_clicked = pyqtSignal(object)  # EventItem

    _CARD_TOP    = 8
    _CARD_H      = 36
    _LABEL_H     = 14  # hauteur zone heure sous la ligne de base
    _BASELINE_Y  = 58

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._events: list[EventItem] = []
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def set_events(self, events: list[EventItem]) -> None:
        self._events = events
        self.update()

    def _hit_event(self, mouse_x: int) -> "EventItem | None":
        import time as _t
        now = _t.time()
        w = self.width()
        px_per_sec = w / (8 * 3600)
        cx = w // 2
        for ev in self._events:
            s_ts = ev.start_ts if ev.start_ts else self._parse_ts(ev.day, ev.start_local)
            e_ts = ev.end_ts   if ev.end_ts   else self._parse_ts(ev.day, ev.end_local)
            if s_ts is None or e_ts is None:
                continue
            start_x = cx + int((s_ts - now) * px_per_sec)
            ev_w    = max(int((e_ts - s_ts) * px_per_sec), 60)
            if start_x <= mouse_x <= start_x + ev_w:
                return ev
        return None

    def mouseMoveEvent(self, _event: QMouseEvent) -> None:  # type: ignore[override]
        ev = self._hit_event(_event.pos().x())
        self.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor) if ev
            else QCursor(Qt.CursorShape.ArrowCursor)
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        ev = self._hit_event(event.pos().x())
        if ev is not None:
            self.event_clicked.emit(ev)

    def _draw_tick_marks(
        self, p: QPainter, cx: int, w: int, now: float, px_per_sec: float
    ) -> None:
        for delta_h in range(-4, 5):
            x = cx + int(delta_h * 3600 * px_per_sec)
            if 0 <= x <= w:
                # petit tick
                p.setPen(QPen(QColor("#2a2a2a"), 1))
                p.drawLine(x, self._BASELINE_Y - 4, x, self._BASELINE_Y + 4)
                dt = datetime.fromtimestamp(now + delta_h * 3600, tz=timezone.utc) + timedelta(hours=2)
                p.setPen(QPen(QColor("#444444")))
                p.setFont(QFont(_FONT_SEGOE, 9))
                p.drawText(x - 14, self._BASELINE_Y + 6, 28, self._LABEL_H,
                           Qt.AlignmentFlag.AlignCenter, dt.strftime("%Hh"))

    _EVENT_COLORS: dict[str, str] = {
        "concert":  "#7c3aed",
        "karaok":   "#7c3aed",
        "tournoi":  "#d97706",
        "trackman": "#d97706",
        "minecraft":"#16a34a",
        "géo":      "#0891b2",
        "geo":      "#0891b2",
        "blind":    "#db2777",
        "interview":"#64748b",
        "show":     "#7c3aed",
        "cours":    "#059669",
    }

    @staticmethod
    def _event_color(name: str) -> str:
        name_lower = name.lower()
        for k, v in _AccueilTimeline._EVENT_COLORS.items():
            if k in name_lower:
                return v
        return "#2563eb"

    def _draw_event_cards(
        self, p: QPainter, cx: int, w: int, now: float, px_per_sec: float
    ) -> None:
        T = self._CARD_TOP
        H = self._CARD_H
        R = 4  # border-radius

        for ev in self._events:
            # Prefer pre-computed timestamps (set by mock + real API); fall back to parsing
            start_ts: float | None = ev.start_ts if ev.start_ts else self._parse_ts(ev.day, ev.start_local)
            end_ts:   float | None = ev.end_ts   if ev.end_ts   else self._parse_ts(ev.day, ev.end_local)
            if start_ts is None or end_ts is None:
                continue

            start_x = cx + int((start_ts - now) * px_per_sec)
            ev_w    = max(int((end_ts - start_ts) * px_per_sec), 60)
            if start_x > w or start_x + ev_w < 0:
                continue

            is_past    = end_ts < now
            is_current = start_ts <= now <= end_ts
            color_hex  = self._event_color(ev.name or "")
            color      = QColor(color_hex)

            card_rect = QRectF(start_x + 1, T, ev_w - 2, H)

            if is_past:
                # passé : fond très sombre, texte grisé
                bg = QColor("#111111")
                border = QColor("#2a2a2a")
                text_col = QColor("#555555")
                time_col = QColor("#3a3a3a")
                accent_opacity = 0
            elif is_current:
                # en cours : fond coloré semi-transparent + bordure vive + glow
                bg = QColor(color_hex + "28")
                border = color
                text_col = QColor("#ffffff")
                time_col = QColor(color_hex)
                accent_opacity = 255
            else:
                # futur : fond légèrement visible, bordure colorée
                bg = QColor("#181818")
                border = QColor(color_hex + "bb")
                text_col = QColor("#dddddd")
                time_col = QColor(color_hex)
                accent_opacity = 200

            # Fond arrondi
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bg))
            p.drawRoundedRect(card_rect, R, R)

            # Bordure
            p.setPen(QPen(border, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5), R, R)

            # Bande colorée gauche (accent) — seulement si pas passé
            if accent_opacity > 0 and ev_w > 8:
                accent_color = QColor(color)
                accent_color.setAlpha(accent_opacity)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(accent_color))
                p.drawRoundedRect(QRectF(start_x + 1, T, 3, H), 2, 2)

            # ── Texte : heure de début (petite) + nom ────────────────
            # Clip text origin so it never drifts left of the widget when
            # the card overflows the left edge (start_x < 0).
            text_x = max(start_x + 7, 4)
            visible_w = start_x + ev_w - text_x - 6  # remaining width inside card
            inner = QRectF(text_x, T, max(visible_w, 0), H)

            if not is_past and visible_w > 50:
                # Heure en haut
                p.setFont(QFont(_FONT_SEGOE, 8))
                p.setPen(QPen(time_col))
                time_str = _fmt_time_fr(ev.start_local) if ev.start_local else ""
                p.drawText(inner.adjusted(0, 1, 0, 0).toRect(),
                           Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                           time_str)
                # Nom en dessous
                _nf = QFont(_FONT_SEGOE, 9)
                _nf.setBold(is_current)
                p.setFont(_nf)
                p.setPen(QPen(text_col))
                fm = QFontMetrics(p.font())
                max_w = int(visible_w)
                name = fm.elidedText(ev.name or "", Qt.TextElideMode.ElideRight, max_w)
                p.drawText(inner.adjusted(0, 13, 0, 0).toRect(),
                           Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                           name)
            elif visible_w > 30:
                # Pas assez large : juste le nom centré
                p.setFont(QFont(_FONT_SEGOE, 9))
                p.setPen(QPen(text_col))
                fm = QFontMetrics(p.font())
                name = fm.elidedText(ev.name or "", Qt.TextElideMode.ElideRight, int(visible_w))
                p.drawText(inner.toRect(),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           name)

            # ── Indicateur "EN COURS" — petit point pulsé ────────────
            if is_current:
                dot_x = start_x + ev_w - 10
                dot_y = T + H // 2
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(color))
                p.drawEllipse(QRectF(dot_x - 3, dot_y - 3, 6, 6))

    def paintEvent(self, _event: QPaintEvent) -> None:  # type: ignore[override]
        import time as _time
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QPolygon

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = _time.time()
        w, h = self.width(), self.height()
        px_per_sec: float = w / (8 * 3600)
        cx: int = w // 2

        # Fond
        p.fillRect(0, 0, w, h, QColor("#0a0a0a"))

        # Dégradé horizontal discret de transparence sur les bords
        for side_x, direction in ((0, 1), (w, -1)):
            grad = QLinearGradient(side_x, 0, side_x + direction * 80, 0)
            grad.setColorAt(0.0, QColor("#0a0a0a"))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(
                side_x if direction == 1 else side_x - 80,
                0, 80, h, QBrush(grad)
            )

        self._draw_event_cards(p, cx, w, now, px_per_sec)

        # Ligne de base fine
        p.setPen(QPen(QColor("#1e1e1e"), 1))
        p.drawLine(0, self._BASELINE_Y, w, self._BASELINE_Y)

        self._draw_tick_marks(p, cx, w, now, px_per_sec)

        # Ligne "maintenant" — trait fin pointillé + triangle
        pen_now = QPen(QColor("#00ff87"), 1)
        pen_now.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen_now)
        p.drawLine(cx, self._CARD_TOP, cx, self._BASELINE_Y)

        p.setBrush(QBrush(QColor("#00ff87")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygon([QPoint(cx - 5, 0), QPoint(cx + 5, 0), QPoint(cx, 7)]))
        p.end()

    @staticmethod
    def _parse_ts(day: str, local_time: str) -> float | None:
        """Parse a HH:MM time in UTC+2 to a Unix timestamp."""
        if not day or not local_time:
            return None
        try:
            dt = datetime.strptime(f"{day} {local_time}", "%Y-%m-%d %H:%M")
            # Attach explicit UTC+2 tzinfo – avoids the double-subtract bug
            # that occurred when naive .timestamp() already used local tz.
            return dt.replace(tzinfo=timezone(timedelta(hours=2))).timestamp()
        except ValueError:
            return None


# ── Ticker ────────────────────────────────────────────────────────────────

class _AccueilTicker(QWidget):
    """Bande 36px fond #0d0d0d — défilement seamless via QScrollArea + QLabel doublé."""

    _SEP = "     ·     "

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self._FONT = QFont(_FONT_MONO, 11)
        self._content: str = _WAIT_MSG
        self._text_width: int = 0
        self._scroll_pos: int = 0

        self._label = QLabel()
        self._label.setFont(self._FONT)
        self._label.setStyleSheet("color: #cccccc; background: transparent; border: none; padding: 0px;")
        self._label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        self._scroll = QScrollArea(self)
        self._scroll.setWidget(self._label)
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(36)
        self._scroll.setStyleSheet(
            "QScrollArea { background: #0d0d0d; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #0d0d0d; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)

        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._set_content(self._content)

    def _set_content(self, text: str) -> None:
        self._content = text
        doubled = text + self._SEP + text
        self._label.setText(doubled)
        fm = QFontMetrics(self._FONT)
        self._text_width = fm.horizontalAdvance(text + self._SEP)
        self._label.setFixedWidth(self._text_width * 2)
        self._label.setFixedHeight(36)
        self._scroll_pos = 0
        self._scroll.horizontalScrollBar().setValue(0)

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        parts: list[str] = []
        for s in streamers:
            dot = "🟢" if (s.location or "").upper() == "LAN" else "🔵"
            game = f"  {s.game}" if s.game else ""
            parts.append(f"{dot} {s.display}{game}")
        text = (
            "   ·   ".join(parts)
            if parts
            else _WAIT_MSG
        )
        if text != self._content:
            self._set_content(text)

    def _tick(self) -> None:
        self._scroll_pos += 2
        if self._scroll_pos >= self._text_width:
            self._scroll_pos = 0
        self._scroll.horizontalScrollBar().setValue(self._scroll_pos)


# ── Accueil — liste streamers live ───────────────────────────────────────────────────────

class _AccueilStreamerItem(QWidget):
    """Ligne 36px — un streamer live dans la liste centrale."""

    def __init__(self, s: StreamerInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._login = s.twitch_login
        self.setFixedHeight(36)
        self.setStyleSheet("background-color: transparent;")
        self._avatar: QLabel
        self._v_lbl: QLabel
        self._name_lbl: QLabel
        self._build(s)

    def _build(self, s: StreamerInfo) -> None:
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(6)

        # Dot couleur selon location
        color = "#00ff87" if (s.location or "").upper() == "LAN" else "#3b82f6"
        dot = QLabel("●")
        dot.setFixedSize(10, 36)
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setStyleSheet(f"color: {color}; font-size: 8px; background: transparent;")
        h.addWidget(dot)

        # Avatar 28x28 avec initiales en fallback
        self._avatar = QLabel()
        self._avatar.setFixedSize(28, 28)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar.setStyleSheet(
            "background-color: #333333; border-radius: 14px; "
            "font-family: 'Segoe UI Variable'; font-size: 9px; font-weight: bold; color: #ffffff;"
        )
        self._avatar.setText(s.twitch_login[:2].upper())
        h.addWidget(self._avatar)
        if s.profile_url:
            from widgets.bigscreen_widget import load_avatar_into_label as _load_av
            _load_av(self._avatar, s.twitch_login, s.display, 28, s.profile_url)

        # Nom · jeu sur une ligne
        game = (s.game or "")[:20] + ("…" if len(s.game or "") > 20 else "")
        full = f"{s.display}  ·  {game}" if game else s.display
        self._name_lbl = QLabel(full)
        self._name_lbl.setFont(QFont(_FONT_SEGOE, 12))
        self._name_lbl.setStyleSheet("color: #aaaaaa; background: transparent;")
        h.addWidget(self._name_lbl, stretch=1)

        # Viewers
        self._v_lbl = QLabel(_fmt_viewers(s.viewers))
        self._v_lbl.setFont(_bold_font(_FONT_SEGOE, 12))
        self._v_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        h.addWidget(self._v_lbl)

    def patch(self, s: StreamerInfo) -> None:
        """Met à jour viewers (et jeu) sans reconstruire le widget."""
        self._v_lbl.setText(_fmt_viewers(s.viewers))
        game = (s.game or "")[:20] + ("…" if len(s.game or "") > 20 else "")
        full = f"{s.display}  ·  {game}" if game else s.display
        self._name_lbl.setText(full)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.setStyleSheet("background-color: #111111; border-radius: 4px;")

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.setStyleSheet("background-color: transparent;")




class _AccueilStreamersList(QWidget):
    """Colonne gauche 60% — liste scrollable des streamers live, triés par viewers."""

    stream_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_SS_BG_DARK)
        self._item_map: dict[str, _AccueilStreamerItem] = {}
        self._prev_live: frozenset[str] = frozenset()
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 0, 4, 0)
        root.setSpacing(0)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 4, 0, 4)
        hdr.setSpacing(0)
        title = QLabel("EN LIVE")
        title.setFont(_bold_font(_FONT_SEGOE, 10))
        title.setStyleSheet("color: #00ff87; letter-spacing: 2px; background: transparent;")
        hdr.addWidget(title)
        self._count_lbl = QLabel("")
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet("color: #555555; background: transparent;")
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._count_lbl, stretch=1)
        root.addLayout(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: #0a0a0a; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #0a0a0a; }"
        )
        content = QWidget()
        content.setStyleSheet("background: #0a0a0a;")
        self._list_layout = QVBoxLayout(content)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def update_streamers(self, streamers: list[StreamerInfo]) -> None:
        live = sorted([s for s in streamers if s.online], key=lambda s: -s.viewers)
        live_logins = frozenset(s.twitch_login for s in live)
        self._count_lbl.setText(f"{len(live)} streamers")

        if live_logins == self._prev_live and self._item_map:
            # Même ensemble de streamers — patch en place, aucun blink
            for s in live:
                item = self._item_map.get(s.twitch_login)
                if item is not None:
                    item.patch(s)
            return

        # Structure changée (arrivée/depart streamer) — rebuild complet
        self._prev_live = live_logins
        _clear_layout(self._list_layout)
        self._item_map.clear()
        for s in live:
            item = _AccueilStreamerItem(s)
            self._item_map[s.twitch_login] = item
            self._list_layout.addWidget(item)
        self._list_layout.addStretch()


# ── Accueil — goals proches d'atteinte ───────────────────────────────────────

class _AccueilGoalItem(QWidget):
    """Item 52px — un goal proche de son seuil."""

    def __init__(self, goal: GoalWithStreamer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(52)
        self._build(goal)

    def _build(self, g: GoalWithStreamer) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(4)

        # Ligne 1 : streamer + nom goal + pct
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        streamer_lbl = QLabel(g.streamer_display[:16])
        streamer_lbl.setFont(_bold_font(_FONT_SEGOE, 11))
        streamer_lbl.setStyleSheet("color: #ffffff; background: transparent;")
        row1.addWidget(streamer_lbl)
        goal_name = g.goal_name[:30] + ("…" if len(g.goal_name) > 30 else "")
        goal_lbl = QLabel(goal_name)
        goal_lbl.setFont(QFont(_FONT_SEGOE, 11))
        goal_lbl.setStyleSheet("color: #888888; background: transparent;")
        row1.addWidget(goal_lbl, stretch=1)
        pct_lbl = QLabel(f"{g.pct:.0f}%")
        pct_lbl.setFont(QFont(_FONT_SEGOE, 11))
        pct_lbl.setStyleSheet("color: #00ff87; background: transparent;")
        pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(pct_lbl)
        root.addLayout(row1)

        # Ligne 2 : barre de progression
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(g.pct))
        bar.setFixedHeight(4)
        bar.setTextVisible(False)
        chunk_color = "#ff6b00" if g.pct >= 99 else "#00ff87"
        bar.setStyleSheet(
            f"QProgressBar {{ background-color: #222222; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background-color: {chunk_color}; border-radius: 2px; }}"
        )
        root.addWidget(bar)


class _AccueilGoalsWidget(QWidget):
    """Colonne droite 40% — goals proches d'atteinte (pct \u2265 90%)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #0a0a0a; border-left: 1px solid #1a1a1a;")
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 0, 12, 0)
        root.setSpacing(0)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 4, 0, 4)
        hdr.setSpacing(0)
        title = QLabel("OBJECTIFS PROCHES")
        title.setFont(_bold_font(_FONT_SEGOE, 10))
        title.setStyleSheet("color: #00ff87; letter-spacing: 2px; background: transparent;")
        hdr.addWidget(title)
        self._count_lbl = QLabel("")
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet("color: #555555; background: transparent;")
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._count_lbl, stretch=1)
        root.addLayout(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: #0a0a0a; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #0a0a0a; }"
        )
        self._content = QWidget()
        self._content.setStyleSheet("background: #0a0a0a;")
        self._goals_layout = QVBoxLayout(self._content)
        self._goals_layout.setContentsMargins(0, 0, 0, 0)
        self._goals_layout.setSpacing(0)
        self._goals_layout.addStretch()
        emp = QLabel("Aucun objectif proche\npour l'instant")
        emp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        emp.setFont(QFont(_FONT_SEGOE, 12))
        emp.setStyleSheet("color: #444444; background: transparent;")
        self._goals_layout.addWidget(emp)
        self._goals_layout.addStretch()
        scroll.setWidget(self._content)
        root.addWidget(scroll, stretch=1)

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        to_show = sorted(
            [g for g in goals if g.pct >= 90.0 and not g.accomplished],
            key=lambda g: -g.pct,
        )
        self._count_lbl.setText(f"{len(to_show)} goals \xe0 90%+" if to_show else "")
        _clear_layout(self._goals_layout)
        if not to_show:
            self._goals_layout.addStretch()
            emp = QLabel("Aucun objectif proche\npour l'instant")
            emp.setAlignment(Qt.AlignmentFlag.AlignCenter)
            emp.setFont(QFont(_FONT_SEGOE, 12))
            emp.setStyleSheet("color: #444444; background: transparent;")
            self._goals_layout.addWidget(emp)
            self._goals_layout.addStretch()
            return
        for i, g in enumerate(to_show):
            self._goals_layout.addWidget(_AccueilGoalItem(g))
            if i < len(to_show) - 1:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet("background-color: #1e1e1e;")
                self._goals_layout.addWidget(sep)
        self._goals_layout.addStretch()


# ── Accueil Tab ───────────────────────────────────────────────────────────

class _AccueilTab(QWidget):
    stream_selected = pyqtSignal(str)
    add_to_grid     = pyqtSignal(str)  # twitch_login

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prev_viewers: int = 0
        self._prev_live: set[str] = set()
        self._prev_donation: float = 0.0
        self._initialized: bool = False
        self._events: list[EventItem] = []
        self._uuid_to_login: dict[str, str] = {}  # gdoc_id → twitch_login
        self._all_logins: set[str] = set()        # logins connus
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Bandeau Gemini — 36px fixe, pleine largeur
        self._banner = _AccueilGeminiBanner()
        root.addWidget(self._banner)

        # 2. Cards stats — 90px fixe
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(12, 8, 12, 0)
        cards_row.setSpacing(8)
        self._card_donation, self._amt_lbl = self._make_card_donation()
        self._card_viewers, self._viewers_lbl, self._trend_lbl = self._make_card_viewers()
        self._card_live, self._live_count_lbl = self._make_card_live()
        cards_row.addWidget(self._card_donation)
        cards_row.addWidget(self._card_viewers)
        cards_row.addWidget(self._card_live)
        cards_wrap = QWidget()
        cards_wrap.setFixedHeight(90)
        cards_wrap.setLayout(cards_row)
        root.addWidget(cards_wrap)

        # 3. Player cards top 3 — 150px fixe
        top3_row = QHBoxLayout()
        top3_row.setContentsMargins(12, 8, 12, 0)
        top3_row.setSpacing(8)
        self._player_cards: list[_AccueilPlayerCard] = []
        for _ in range(3):
            pc = _AccueilPlayerCard()
            top3_row.addWidget(pc)
            self._player_cards.append(pc)
        top3_wrap = QWidget()
        top3_wrap.setFixedHeight(150)
        top3_wrap.setLayout(top3_row)
        root.addWidget(top3_wrap)

        # 4. Section centrale — prend l'espace restant (pas de fixedHeight)
        central_widget = QWidget()
        central_layout = QHBoxLayout(central_widget)
        central_layout.setContentsMargins(0, 8, 0, 0)
        central_layout.setSpacing(0)
        self._streamers_list = _AccueilStreamersList()
        self._streamers_list.stream_selected.connect(self.stream_selected)
        self._goals_widget = _AccueilGoalsWidget()
        central_layout.addWidget(self._streamers_list, stretch=6)
        central_layout.addWidget(self._goals_widget, stretch=4)
        root.addWidget(central_widget, stretch=1)

        # 5. Timeline — 110px fixe
        tl_row = QHBoxLayout()
        tl_row.setContentsMargins(12, 8, 12, 0)
        tl_row.setSpacing(0)
        self._timeline = _AccueilTimeline()
        self._timeline.event_clicked.connect(self._on_timeline_click)
        tl_row.addWidget(self._timeline)
        tl_wrap = QWidget()
        tl_wrap.setFixedHeight(110)
        tl_wrap.setLayout(tl_row)
        root.addWidget(tl_wrap)

        # 6. Ticker — 36px fixe, pleine largeur
        self._ticker = _AccueilTicker()
        root.addWidget(self._ticker)

    # -- card builders --------------------------------------------------------

    @staticmethod
    def _base_card() -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background-color: #111111; border: 1px solid #1e1e1e; "
            "border-radius: 6px; }"
        )
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return card

    def _make_card_donation(self) -> tuple[QFrame, QLabel]:
        card = self._base_card()
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 0, 14, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        amt = QLabel("0 €")
        amt.setFont(_bold_font(_FONT_MONO, 22))
        amt.setStyleSheet(_SS_WHITE_CLEAR)
        vl.addWidget(amt)
        sub = QLabel("cagnotte totale")
        sub.setFont(QFont(_FONT_SEGOE, 10))
        sub.setStyleSheet(_SS_GREY_CLEAR)
        vl.addWidget(sub)
        return card, amt

    def _make_card_viewers(self) -> tuple[QFrame, QLabel, QLabel]:
        card = self._base_card()
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 0, 14, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        row = QHBoxLayout()
        row.setSpacing(6)
        viewers = QLabel("0")
        viewers.setFont(_bold_font(_FONT_MONO, 22))
        viewers.setStyleSheet(_SS_GREEN_CLEAR)
        row.addWidget(viewers)
        trend = QLabel("")
        trend.setFont(_bold_font(_FONT_MONO, 18))
        trend.setStyleSheet("border: none; background: transparent;")
        row.addWidget(trend)
        row.addStretch()
        vl.addLayout(row)
        sub = QLabel("viewers")
        sub.setFont(QFont(_FONT_SEGOE, 10))
        sub.setStyleSheet(_SS_GREY_CLEAR)
        vl.addWidget(sub)
        return card, viewers, trend

    def _make_card_live(self) -> tuple[QFrame, QLabel]:
        card = self._base_card()
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 0, 14, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        cnt = QLabel("0 / 0")
        cnt.setFont(_bold_font(_FONT_MONO, 22))
        cnt.setStyleSheet(_SS_WHITE_CLEAR)
        vl.addWidget(cnt)
        sub = QLabel("streamers en live")
        sub.setFont(QFont(_FONT_SEGOE, 10))
        sub.setStyleSheet(_SS_GREY_CLEAR)
        vl.addWidget(sub)
        return card, cnt

    # -- public API -----------------------------------------------------------

    def refresh(self, streamers: list[StreamerInfo], stats: GlobalStats) -> None:
        # Card cagnotte
        self._amt_lbl.setText(stats.donation_formatted)

        # Card viewers + tendance
        cur_v = stats.viewers_total
        if cur_v > self._prev_viewers:
            self._trend_lbl.setText("▲")
            self._trend_lbl.setStyleSheet(_SS_GREEN_CLEAR)
        elif cur_v < self._prev_viewers:
            self._trend_lbl.setText("▼")
            self._trend_lbl.setStyleSheet("color: #ff4444; border: none; background: transparent;")
        else:
            self._trend_lbl.setText("")
        self._viewers_lbl.setText(_fmt_viewers(cur_v))
        self._prev_viewers = cur_v

        # Card live
        nb_live = sum(1 for s in streamers if s.online)
        nb_total = len(streamers)
        self._live_count_lbl.setText(f"{nb_live} / {nb_total}")

        # Player cards top 3 par viewers
        top3 = sorted([s for s in streamers if s.online], key=lambda s: -s.viewers)[:3]
        max_v = top3[0].viewers if top3 else 1
        for i, pc in enumerate(self._player_cards):
            if i < len(top3):
                pc.set_streamer(top3[i], i + 1, max_v)
                pc.setVisible(True)
            else:
                pc.setVisible(False)

        # Ticker — streamers live en rotation
        ticker_live = sorted(
            [s for s in streamers if s.online], key=lambda s: -s.viewers
        )[:15]
        self._ticker.set_streamers(ticker_live)

        # Liste streamers live (section centrale)
        self._streamers_list.update_streamers(streamers)

        # Contexte Gemini
        self._banner.set_context({
            "year": 2025,
            "donation": stats.donation_formatted,
            "viewers": _fmt_viewers(stats.viewers_total),
            "live_count": nb_live,
            "total_count": nb_total,
            "_raw_donation": stats.donation_total,
        })

        self._prev_live = {s.twitch_login for s in streamers if s.online}
        self._prev_donation = stats.donation_total
        self._initialized = True
        # Rebuild UUID map for timeline click resolution
        self._uuid_to_login = {s.gdoc_id: s.twitch_login for s in streamers if s.gdoc_id}
        self._all_logins = {s.twitch_login for s in streamers}

    def _on_timeline_click(self, ev: EventItem) -> None:
        """Clic sur un event : popup pour ouvrir en fullscreen ou ajouter à la grille."""
        # Résoudre le login du présentateur
        login: str | None = None
        for uid in (ev.host_uuids or []):
            if uid in self._uuid_to_login:
                login = self._uuid_to_login[uid]
                break
            if uid in self._all_logins:
                login = uid
                break
        if not login:
            return  # pas de présentateur résolvable

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #111111; border: 1px solid #2a2a2a; color: #cccccc; "
            "font-family: 'Segoe UI Variable'; font-size: 11px; padding: 4px 0; }"
            "QMenu::item { padding: 6px 20px; }"
            "QMenu::item:selected { background: #1e1e1e; color: #ffffff; }"
            "QMenu::separator { height: 1px; background: #2a2a2a; margin: 2px 0; }"
        )
        host_act = QAction(f"{ev.name or 'Événement'}  —  {login}", menu)
        host_act.setEnabled(False)
        menu.addAction(host_act)
        menu.addSeparator()
        act_fs   = menu.addAction("▶  Charger en fullscreen")
        act_grid = menu.addAction("⊞  Ajouter à la grille")
        chosen = menu.exec(QCursor.pos())
        if chosen == act_fs:
            self.stream_selected.emit(login)
        elif chosen == act_grid:
            self.add_to_grid.emit(login)

    def update_events(self, events: list[EventItem]) -> None:
        self._events = events
        self._timeline.set_events(events)

    def update_history(self, history: HistoryStore) -> None:
        ts, _ = history.get_donation_series()
        if ts:
            logger.debug(
                "AccueilTab.update_history: %d points, premier ts UTC = %s",
                len(ts),
                datetime.fromtimestamp(ts[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        self._goals_widget.update_goals(goals)


# ---------------------------------------------------------------------------
# Tab: Programme
# ---------------------------------------------------------------------------

_JOURS_FR = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "SAMEDI", "DIMANCHE"]
_MOIS_FR = ["", "JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET",
            "AOÛT", "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE"]


def _day_header_fr(day_str: str) -> str:
    """'2025-09-05' → 'VENDREDI 5 SEPTEMBRE'."""
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d")
        return f"{_JOURS_FR[dt.weekday()]} {dt.day} {_MOIS_FR[dt.month]}"
    except ValueError:
        return day_str


def _fmt_time_fr(hhmm: str) -> str:
    """'14:30' → '14h30'."""
    if ":" in hhmm:
        h, m = hhmm.split(":", 1)
        return f"{h}h{m}"
    return hhmm


def _fmt_duration(start: str, end: str) -> str:
    """'14:30', '17:00' → '2h30'."""
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        delta = (eh * 60 + em) - (sh * 60 + sm)
        if delta <= 0:
            delta += 24 * 60
        if delta >= 60:
            h, m = divmod(delta, 60)
            return f"{h}h{m:02d}" if m else f"{h}h"
        return f"{delta}min"
    except (ValueError, AttributeError):
        return ""


_PROG_DAY_LABELS: dict[str, str] = {
    "2025-09-05": "Vendredi 5",
    "2025-09-06": "Samedi 6",
    "2025-09-07": "Dimanche 7",
}
_PROG_DAYS_ORDERED: list[str] = ["2025-09-05", "2025-09-06", "2025-09-07"]

_BTN_INACTIVE = (
    "QPushButton { background: transparent; color: #666666; "
    "border: 1px solid #222222; border-radius: 4px; padding: 6px 16px; }"
    "QPushButton:hover { color: #888888; border-color: #444444; }"
)
_BTN_ACTIVE = (
    "QPushButton { background: #111111; color: #00ff87; "
    "border: 1px solid #00ff87; border-radius: 4px; padding: 6px 16px; }"
)

# ── Chip de participant ────────────────────────────────────────────────────

def _make_chip(name: str) -> QLabel:
    """Petit badge arrondi avec le nom du participant."""
    lbl = QLabel(name)
    lbl.setFont(QFont(_FONT_SEGOE, 10))
    lbl.setStyleSheet(
        "color: #cccccc; background: #1e1e1e; border: 1px solid #2a2a2a; "
        "border-radius: 10px; padding: 1px 8px;"
    )
    lbl.setFixedHeight(20)
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


# ── Popup : liste complète des participants ────────────────────────────────

class _ParticipantsDialog(QDialog):
    """Popup modale listant tous les participants d'un événement."""

    def __init__(self, event_name: str, participants: list[str],
                 hosts: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(event_name)
        self.setModal(True)
        self.setMinimumWidth(360)
        self.setStyleSheet(
            "QDialog { background: #111111; }"
            "QLabel { color: #ffffff; background: transparent; border: none; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        title = QLabel(event_name)
        title.setFont(_bold_font(_FONT_SEGOE, 14))
        title.setWordWrap(True)
        lay.addWidget(title)

        if hosts:
            host_lbl = QLabel("Hôtes")
            host_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
            host_lbl.setStyleSheet("color: #555555; letter-spacing: 1px;")
            lay.addWidget(host_lbl)
            hw = QWidget()
            hw.setStyleSheet("background: transparent;")
            hfl = _FlowLayout(hw, h_spacing=6, v_spacing=6)
            for name in hosts:
                chip = _make_chip(name)
                chip.setStyleSheet(
                    "color: #00ff87; background: #0d1f16; "
                    "border: 1px solid #00ff87; border-radius: 10px; padding: 1px 8px;"
                )
                hfl.addWidget(chip)
            lay.addWidget(hw)

        if participants:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("border: none; border-top: 1px solid #222222;")
            lay.addWidget(sep)

            part_lbl = QLabel("Participants")
            part_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
            part_lbl.setStyleSheet("color: #555555; letter-spacing: 1px;")
            lay.addWidget(part_lbl)

            pw = QWidget()
            pw.setStyleSheet("background: transparent;")
            pfl = _FlowLayout(pw, h_spacing=6, v_spacing=6)
            for name in participants:
                pfl.addWidget(_make_chip(name))
            lay.addWidget(pw)

        close_btn = QPushButton("Fermer")
        close_btn.setFont(QFont(_FONT_SEGOE, 11))
        close_btn.setStyleSheet(
            "QPushButton { background: #1e1e1e; color: #888888; border: 1px solid #2a2a2a; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #222222; color: #ffffff; }"
        )
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)


# ── Layout de flux (wrap) pour chips ──────────────────────────────────────

class _FlowLayout(QHBoxLayout):
    """Mini flow-layout horizontal avec retour à la ligne via QGridLayout sous-jacent."""

    def __init__(self, parent: QWidget, h_spacing: int = 4, v_spacing: int = 4) -> None:
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(0)
        self._widgets: list[QWidget] = []
        self._h = h_spacing
        self._v = v_spacing
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(h_spacing)
        self._grid.setVerticalSpacing(v_spacing)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self.addLayout(self._grid)
        self._row = 0
        self._col = 0
        self._max_col = 4

    def addWidget(self, w: QWidget) -> None:  # type: ignore[override]
        self._grid.addWidget(w, self._row, self._col)
        self._col += 1
        if self._col >= self._max_col:
            self._col = 0
            self._row += 1


# ── Toast de rappel ────────────────────────────────────────────────────────

class _ReminderToast(QWidget):
    """Toast flottant « Rappel : Nom événement commence dans X min »."""

    def __init__(self, message: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        bell = QLabel("🔔")
        bell.setFont(QFont(_FONT_SEGOE, 14))
        bell.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(bell)

        txt = QLabel(message)
        txt.setFont(QFont(_FONT_SEGOE, 12))
        txt.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        txt.setWordWrap(True)
        lay.addWidget(txt, stretch=1)

        close = QPushButton("✕")
        close.setFont(QFont(_FONT_SEGOE, 10))
        close.setFixedSize(20, 20)
        close.setStyleSheet(
            "QPushButton { background: transparent; color: #888888; border: none; }"
            "QPushButton:hover { color: #ffffff; }"
        )
        close.clicked.connect(self.close)
        lay.addWidget(close)

        self.setStyleSheet(
            "QWidget { background: #1a2a1f; border: 1px solid #00ff87; border-radius: 8px; }"
        )
        self.setFixedWidth(340)
        self.adjustSize()

        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._eff.setOpacity(1.0)

        QTimer.singleShot(6000, self._fade_out)

    def _fade_out(self) -> None:
        anim = QPropertyAnimation(self._eff, b"opacity", self)
        anim.setDuration(400)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start()

    def show_near(self, parent: QWidget) -> None:
        self.show()
        pr = parent.rect()
        pg = parent.mapToGlobal(pr.bottomRight())
        self.move(pg.x() - self.width() - 16, pg.y() - self.height() - 16)


# ── Onglet Programme ───────────────────────────────────────────────────────

class _ProgrammeTab(QWidget):

    # Émis quand un rappel est déclenché (event_name, message)
    reminder_triggered = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._events_layout: QVBoxLayout
        self._gdoc_display: dict[str, str] = {}   # gdoc_id → display_name
        self._events: list[EventItem] = []
        self._day_btns: dict[str, QPushButton] = {}
        self._subscribed_ids: set[str] = set()     # event ids avec rappel activé
        self._reminded_ids: set[str] = set()       # rappels déjà déclenchés
        _today = datetime.now().strftime("%Y-%m-%d")
        self._current_day: str = _today if _today in _PROG_DAYS_ORDERED else _PROG_DAYS_ORDERED[0]
        self._build()

        # Timer de vérification des rappels (toutes les 30s)
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(30_000)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._day_lbl = QLabel("ZEvent 2026")
        self._day_lbl.setFont(_bold_font(_FONT_SEGOE, 13))
        self._day_lbl.setStyleSheet(_SS_WHITE)
        root.addWidget(self._day_lbl)

        tz = QLabel("Heures UTC+2 (Paris)")
        tz.setFont(QFont(_FONT_SEGOE, 10))
        tz.setStyleSheet(_SS_GREY)
        root.addWidget(tz)
        root.addSpacing(4)

        # ── Sélecteur de jour ─────────────────────────────────────────
        self._btn_bar = QHBoxLayout()
        self._btn_bar.setSpacing(6)
        self._btn_bar_stretch = None  # QSpacerItem ajouté dynamiquement
        root.addLayout(self._btn_bar)
        root.addSpacing(4)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._events_layout = QVBoxLayout(self._content)
        self._events_layout.setContentsMargins(0, 0, 4, 0)
        self._events_layout.setSpacing(8)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, stretch=1)

        self._update_btn_styles()
        # evenements chargés après le premier poll

    # -- day bar --------------------------------------------------------------

    @staticmethod
    def _short_day_label(day_str: str) -> str:
        """'2025-09-05' → 'Vendredi 5'  (via _PROG_DAY_LABELS, sinon calculé)."""
        if day_str in _PROG_DAY_LABELS:
            return _PROG_DAY_LABELS[day_str]
        try:
            dt = datetime.strptime(day_str, "%Y-%m-%d")
            _JOURS_SHORT = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
            return f"{_JOURS_SHORT[dt.weekday()]} {dt.day}"
        except ValueError:
            return day_str

    def _rebuild_day_buttons(self, days: list[str]) -> None:
        """Recrée les boutons de jour si la liste de jours a changé."""
        # Vider
        while self._btn_bar.count():
            item = self._btn_bar.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._day_btns.clear()
        for day in days:
            btn = QPushButton(self._short_day_label(day))
            btn.setFont(QFont(_FONT_SEGOE, 11))
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _checked, d=day: self._select_day(d))
            self._day_btns[day] = btn
            self._btn_bar.addWidget(btn)
        self._btn_bar.addStretch()

    # -- public ---------------------------------------------------------------

    def set_gdoc_map(self, gdoc_id_to_display: dict[str, str]) -> None:
        self._gdoc_display = gdoc_id_to_display
        if self._events:
            self._render_current_day()

    def update_events(self, events: list[EventItem]) -> None:
        self._events = events
        # Recalcule les jours disponibles depuis les événements reçus
        days = sorted({ev.day for ev in events if ev.day})
        if not days:
            days = _PROG_DAYS_ORDERED
        if list(self._day_btns.keys()) != days:
            self._rebuild_day_buttons(days)
            # Essayer de rester sur le jour actuel, sinon prendre le premier
            if self._current_day not in days:
                today = datetime.now().strftime("%Y-%m-%d")
                self._current_day = today if today in days else days[0]
            self._update_btn_styles()
        self._render_current_day()

    # -- internal -------------------------------------------------------------

    def _select_day(self, day: str) -> None:
        self._current_day = day
        self._update_btn_styles()
        self._render_current_day()

    def _update_btn_styles(self) -> None:
        for day, btn in self._day_btns.items():
            btn.setStyleSheet(_BTN_ACTIVE if day == self._current_day else _BTN_INACTIVE)

    def _render_current_day(self) -> None:
        _clear_layout(self._events_layout)
        day_events = sorted(
            [ev for ev in self._events if ev.day == self._current_day],
            key=lambda e: e.start_local,
        )
        if not day_events:
            ph = QLabel("Aucun événement pour ce jour")
            ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ph.setStyleSheet("color: #555555; font-family: 'Segoe UI Variable';")
            self._events_layout.addWidget(ph)
            self._events_layout.addStretch(1)
            return
        # Paires de cards côte à côte dans des rows QHBoxLayout indépendantes
        for i in range(0, len(day_events), 2):
            row_w = QWidget()
            row_w.setStyleSheet("background: transparent; border: none;")
            row_hl = QHBoxLayout(row_w)
            row_hl.setContentsMargins(0, 0, 0, 0)
            row_hl.setSpacing(8)
            row_hl.addWidget(self._event_card(day_events[i]), stretch=1)
            if i + 1 < len(day_events):
                row_hl.addWidget(self._event_card(day_events[i + 1]), stretch=1)
            else:
                # Dernière card seule : spacer pour garder la largeur 50%
                spacer_w = QWidget()
                spacer_w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                row_hl.addWidget(spacer_w, stretch=1)
            self._events_layout.addWidget(row_w)
        self._events_layout.addStretch(1)

    def _resolve(self, uuids: list[str], names: dict[str, str] | None = None) -> list[str]:
        """Résout des streamer_id en noms : noms portés par le show d'abord,
        puis mapping global, sinon uuid tronqué."""
        names = names or {}
        resolved = [
            names.get(uid) or self._gdoc_display[uid]
            for uid in uuids
            if uid in names or uid in self._gdoc_display
        ]
        # fallback : si pas encore de mapping, afficher l'uuid brut en court
        if not resolved and uuids:
            resolved = [uid[:12] + "…" if len(uid) > 12 else uid for uid in uuids]
        return resolved

    def _event_key(self, ev: EventItem) -> str:
        return ev.id if ev.id else f"{ev.day}_{ev.start_local}_{ev.name}"

    # ── Card ─────────────────────────────────────────────────────────

    def _event_card(self, ev: EventItem) -> QFrame:
        key = self._event_key(ev)
        hosts_names = self._resolve(ev.host_uuids, ev.names)
        parts_names = self._resolve(ev.participant_uuids, ev.names)
        is_subscribed = key in self._subscribed_ids

        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #111111; border: 1px solid #1e1e1e; border-radius: 6px; }"
            "QFrame:hover { border-color: #2a2a2a; }"
        )
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        # Barre verticale colorée à gauche
        outer = QHBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        accent = QFrame()
        accent.setFixedWidth(3)
        accent.setStyleSheet("background: #00ff87; border-radius: 2px; border: none;")
        accent.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        outer.addWidget(accent)

        inner = QWidget()
        inner.setStyleSheet("background: transparent; border: none;")
        cl = QVBoxLayout(inner)
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(6)
        outer.addWidget(inner, stretch=1)

        # ── Ligne 1 : heure + nom + durée + bell ─────────────────────
        line1 = QHBoxLayout()
        line1.setSpacing(6)

        time_lbl = QLabel(_fmt_time_fr(ev.start_local) if ev.start_local else "—")
        time_lbl.setFixedWidth(54)
        time_lbl.setFont(_bold_font(_FONT_SEGOE, 11))
        time_lbl.setStyleSheet("color: #00ff87; background: transparent; border: none;")
        line1.addWidget(time_lbl)

        name_lbl = QLabel(ev.name or "—")
        name_lbl.setFont(_bold_font(_FONT_SEGOE, 13))
        name_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        name_lbl.setWordWrap(True)
        line1.addWidget(name_lbl, stretch=1)

        dur = _fmt_duration(ev.start_local, ev.end_local)
        if dur:
            dur_lbl = QLabel(dur)
            dur_lbl.setFont(QFont(_FONT_SEGOE, 10))
            dur_lbl.setStyleSheet("color: #444444; background: transparent; border: none;")
            dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            line1.addWidget(dur_lbl)

        # Bouton rappel (🔔)
        bell_btn = QPushButton("🔔" if is_subscribed else "🔕")
        bell_btn.setFixedSize(26, 26)
        bell_btn.setFont(QFont(_FONT_SEGOE, 11))
        bell_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        bell_btn.setToolTip("Désactiver le rappel" if is_subscribed else "Me rappeler 5 min avant")
        bell_btn.setStyleSheet(
            f"QPushButton {{ background: {'#0d1f16' if is_subscribed else 'transparent'}; "
            f"border: 1px solid {'#00ff87' if is_subscribed else '#333333'}; border-radius: 4px; }}"
            "QPushButton:hover { border-color: #00ff87; background: #0d1f16; }"
        )
        bell_btn.clicked.connect(lambda _, k=key, b=bell_btn, e=ev: self._toggle_reminder(k, b, e))
        line1.addWidget(bell_btn)
        cl.addLayout(line1)

        # ── Ligne 2 : hôtes (chips) ───────────────────────────────────
        if hosts_names:
            hosts_row = QHBoxLayout()
            hosts_row.setSpacing(4)
            host_icon = QLabel("🎙")
            host_icon.setFont(QFont(_FONT_SEGOE, 10))
            host_icon.setStyleSheet("background: transparent; border: none;")
            hosts_row.addWidget(host_icon)
            for name in hosts_names[:3]:
                chip = _make_chip(name)
                chip.setStyleSheet(
                    "color: #00ff87; background: #0d1f16; border: 1px solid #1a3328; "
                    "border-radius: 10px; padding: 1px 8px;"
                )
                hosts_row.addWidget(chip)
            extra_h = len(hosts_names) - 3
            if extra_h > 0:
                more = QPushButton(f"+{extra_h}")
                more.setFont(QFont(_FONT_SEGOE, 10))
                more.setFixedHeight(20)
                more.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                more.setStyleSheet(
                    "QPushButton { color: #00ff87; background: transparent; border: none; "
                    "text-decoration: underline; padding: 0 2px; }"
                )
                more.clicked.connect(
                    lambda _, e=ev, h=hosts_names, p=parts_names: self._open_participants_popup(e, h, p)
                )
                hosts_row.addWidget(more)
            hosts_row.addStretch()
            cl.addLayout(hosts_row)

        # ── Ligne 3 : participants (chips) ────────────────────────────
        if parts_names:
            parts_row = QHBoxLayout()
            parts_row.setSpacing(4)
            CHIPS_SHOWN = 3
            for name in parts_names[:CHIPS_SHOWN]:
                parts_row.addWidget(_make_chip(name))
            extra_p = len(parts_names) - CHIPS_SHOWN
            if extra_p > 0:
                more = QPushButton(f"voir les {extra_p} autres…")
                more.setFont(QFont(_FONT_SEGOE, 10))
                more.setFixedHeight(20)
                more.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                more.setStyleSheet(
                    "QPushButton { color: #555555; background: transparent; border: none; "
                    "text-decoration: underline; padding: 0 2px; }"
                    "QPushButton:hover { color: #888888; }"
                )
                more.clicked.connect(
                    lambda _, e=ev, h=hosts_names, p=parts_names: self._open_participants_popup(e, h, p)
                )
                parts_row.addWidget(more)
            parts_row.addStretch()
            cl.addLayout(parts_row)

        if not hosts_names and not parts_names:
            na_lbl = QLabel("Participants non disponibles")
            na_lbl.setFont(QFont(_FONT_SEGOE, 10))
            na_lbl.setStyleSheet("color: #444444; background: transparent; border: none;")
            cl.addWidget(na_lbl)

        return card

    # ── Popup participants ────────────────────────────────────────────

    def _open_participants_popup(self, ev: EventItem,
                                  hosts: list[str], parts: list[str]) -> None:
        dlg = _ParticipantsDialog(ev.name or "Événement", parts, hosts, parent=self)
        dlg.exec()

    # ── Système de rappels ────────────────────────────────────────────

    def _toggle_reminder(self, key: str, btn: QPushButton, ev: EventItem) -> None:
        if key in self._subscribed_ids:
            self._subscribed_ids.discard(key)
            btn.setText("🔕")
            btn.setToolTip("Me rappeler 5 min avant")
            btn.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid #333333; border-radius: 4px; }"
                "QPushButton:hover { border-color: #00ff87; background: #0d1f16; }"
            )
        else:
            self._subscribed_ids.add(key)
            btn.setText("🔔")
            btn.setToolTip("Désactiver le rappel")
            btn.setStyleSheet(
                "QPushButton { background: #0d1f16; border: 1px solid #00ff87; border-radius: 4px; }"
                "QPushButton:hover { border-color: #00ff87; background: #0d1f16; }"
            )

    def _check_reminders(self) -> None:
        """Vérifie si un événement souscrit commence dans les 5 prochaines minutes."""
        import time as _time
        now = _time.time()
        for ev in self._events:
            key = self._event_key(ev)
            if key not in self._subscribed_ids:
                continue
            if key in self._reminded_ids:
                continue
            if ev.start_ts <= 0:
                continue
            delta = ev.start_ts - now
            if -60 <= delta <= 300:  # entre -1 min et +5 min
                self._reminded_ids.add(key)
                time_str = _fmt_time_fr(ev.start_local)
                if delta > 60:
                    mins = int(delta / 60)
                    msg = f"« {ev.name} » commence dans {mins} min ({time_str})"
                elif delta >= 0:
                    msg = f"« {ev.name} » commence maintenant ! ({time_str})"
                else:
                    msg = f"« {ev.name} » a commencé à {time_str}"
                self.reminder_triggered.emit(ev.name or "", msg)
                toast = _ReminderToast(msg, self)
                toast.show_near(self)


# ---------------------------------------------------------------------------
# Tab: Goals — lazy-load via API
# ---------------------------------------------------------------------------

def _run_coro(coro):  # type: ignore[no-untyped-def]
    """Exécute une coroutine dans une nouvelle event loop (safe depuis un thread Qt)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _GoalsTab(QWidget):
    _sig_goals = pyqtSignal(str, list)  # login, goals — cross-thread

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._streamers: list[StreamerInfo] = []
        self._cache: dict[str, list[DonationGoal]] = {}  # login → goals
        self._pending_login: str = ""
        self._sig_goals.connect(self._on_goals_arrived)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Sélecteur streamer
        hdr = QHBoxLayout()
        lbl = QLabel("Streamer :")
        lbl.setFont(QFont(_FONT_SEGOE, 11))
        lbl.setStyleSheet(_SS_MUTED)
        hdr.addWidget(lbl)

        self._combo = QComboBox()
        self._combo.setFont(QFont(_FONT_SEGOE, 11))
        self._combo.setMinimumHeight(30)
        self._combo.currentIndexChanged.connect(self._on_streamer_changed)
        hdr.addWidget(self._combo, stretch=1)
        root.addLayout(hdr)

        # Zone d'affichage des objectifs
        self._goals_scroll = QScrollArea()
        self._goals_scroll.setWidgetResizable(True)
        self._goals_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._goals_content = QWidget()
        self._goals_layout = QVBoxLayout(self._goals_content)
        self._goals_layout.setContentsMargins(0, 0, 2, 0)
        self._goals_layout.setSpacing(0)
        self._goals_scroll.setWidget(self._goals_content)
        root.addWidget(self._goals_scroll, stretch=1)

        # Placeholder initial
        ph = QLabel("Sélectionner un streamer pour voir ses objectifs")
        ph.setObjectName("placeholder")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setWordWrap(True)
        self._goals_layout.addStretch()
        self._goals_layout.addWidget(ph)
        self._goals_layout.addStretch()

    # -- public ---------------------------------------------------------------

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        """Met à jour la liste des streamers dans le combo."""
        current = self._combo.currentText()

        self._streamers = streamers
        self._combo.blockSignals(True)
        self._combo.clear()
        for s in sorted(streamers, key=lambda x: x.display.lower()):
            self._combo.addItem(s.display, userData=s)
        idx = self._combo.findText(current)
        self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._combo.blockSignals(False)

        if self._combo.count() > 0:
            self._on_streamer_changed(self._combo.currentIndex())

    # -- slots ----------------------------------------------------------------

    def seed_cache(self, cache: dict) -> None:
        """Pré-remplit le cache local depuis le prefetch DataManager (top N streamers)."""
        self._cache.update(cache)

    def _on_streamer_changed(self, idx: int) -> None:
        if idx < 0:
            return
        s: StreamerInfo | None = self._combo.itemData(idx)
        if s is None or not s.participation_id:
            self._show_placeholder("Aucune participation evenmorestats pour ce streamer")
            return
        self._load_goals(s.participation_id, s.twitch_login)

    # -- internal -------------------------------------------------------------

    def _load_goals(self, participation_id: str, login: str) -> None:
        """Affiche les goals depuis le cache si dispo, sinon fetch en background."""
        if login in self._cache:
            self._show_goals(self._cache[login])
            return
        self._pending_login = login
        self._show_placeholder("Chargement…")
        threading.Thread(
            target=self._do_fetch, args=(participation_id, login), daemon=True
        ).start()

    def _do_fetch(self, participation_id: str, login: str) -> None:
        """Worker thread — fetch les goals et émet le signal cross-thread."""
        try:
            goals = _run_coro(fetch_donation_goals(participation_id))
        except Exception as exc:
            logger.error("_do_fetch(%s): %s", participation_id, exc)
            goals = []
        self._sig_goals.emit(login, goals)

    def _on_goals_arrived(self, login: str, goals: list) -> None:
        """Slot main-thread — met en cache et affiche si le streamer est encore sélectionné."""
        self._cache[login] = goals
        current: StreamerInfo | None = self._combo.currentData()
        if current is not None and current.twitch_login == login:
            self._show_goals(goals)

    def _show_placeholder(self, text: str) -> None:
        _clear_layout(self._goals_layout)
        ph = QLabel(text)
        ph.setObjectName("placeholder")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setWordWrap(True)
        self._goals_layout.addStretch()
        self._goals_layout.addWidget(ph)
        self._goals_layout.addStretch()

    def _show_goals(self, goals: list[DonationGoal]) -> None:
        _clear_layout(self._goals_layout)
        if not goals:
            self._show_placeholder("Aucun objectif trouvé")
            return

        accomplished = [g for g in goals if g.accomplished]
        pending = [g for g in goals if not g.accomplished]

        if pending:
            hdr = QLabel(f"À ACCOMPLIR ({len(pending)})")
            hdr.setFont(_bold_font(_FONT_SEGOE, 10))
            hdr.setStyleSheet("color: #888888; letter-spacing: 1px; padding: 4px 0;")
            self._goals_layout.addWidget(hdr)
            for g in pending:
                self._goals_layout.addWidget(self._goal_row(g))

        if accomplished:
            hdr2 = QLabel(f"ACCOMPLIS ({len(accomplished)})")
            hdr2.setFont(_bold_font(_FONT_SEGOE, 10))
            hdr2.setStyleSheet("color: #00ff87; letter-spacing: 1px; padding: 8px 0 4px 0;")
            self._goals_layout.addWidget(hdr2)
            for g in accomplished:
                self._goals_layout.addWidget(self._goal_row(g))

        self._goals_layout.addStretch()

    def _goal_row(self, g: DonationGoal) -> QFrame:
        row = QFrame()
        row.setObjectName("eventRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(12)

        status = QLabel("✓" if g.accomplished else "○")
        status.setFont(_bold_font(_FONT_SEGOE, 14))
        status.setStyleSheet(
            _SS_GREEN if g.accomplished else _SS_GREY
        )
        status.setFixedWidth(20)
        h.addWidget(status)

        info = QVBoxLayout()
        info.setSpacing(2)
        nm = QLabel(g.name)
        nm.setFont(QFont(_FONT_SEGOE, 12))
        nm.setStyleSheet(
            _SS_MUTED if g.accomplished else _SS_WHITE
        )
        nm.setWordWrap(True)
        info.addWidget(nm)

        amt_str = f"{g.amount:,.0f} €".replace(",", "\u00a0")
        amt = QLabel(amt_str)
        amt.setFont(_bold_font(_FONT_MONO, 11))
        amt.setStyleSheet(
            _SS_GREEN if g.accomplished else _SS_MUTED
        )
        info.addWidget(amt)
        h.addLayout(info, stretch=1)

        return row


# ---------------------------------------------------------------------------
# Tab: Streamers — sélection pour la grille (vue en cartes)
# ---------------------------------------------------------------------------

# ── Constantes visuelles ─────────────────────────────────────────────────

_CARD_W = 220          # largeur de référence pour le texte elide
_CARD_H = 168          # hauteur fixe des cartes
_AVATAR_SZ = 56        # taille avatar circulaire

_COL_LAN        = "#d97706"   # ambre — badge LAN
_COL_ONLINE     = "#818cf8"   # indigo clair — badge Online (texte)
_COL_ONLINE_BG  = "#312e81"   # indigo sombre — fond badge Online
_COL_SEL        = "#00ff87"   # vert — bordure sélection


# ── StreamerCard ─────────────────────────────────────────────────────────

class _StreamerCard(QFrame):
    """Carte unique d'un streamer.

    État sélectionné : bordure verte 2 px + numéro de slot en coin haut-gauche.
    État offline     : avatar grisé, non cliquable.
    """

    toggled = pyqtSignal(str, bool)   # login, selected

    def __init__(self, s: StreamerInfo, slot: int | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._login = s.twitch_login
        self._online = s.online
        self._slot = slot           # None = pas sélectionné
        self._avatar_lbl: QLabel

        self.setFixedHeight(_CARD_H)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if self._online:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        self._apply_style()
        self._build(s)

    # -- construction ----------------------------------------------------------

    def _build(self, s: StreamerInfo) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 10)
        root.setSpacing(4)

        # ── Ligne avatar + badges ─────────────────────────────────
        av_row = QHBoxLayout()
        av_row.setSpacing(0)
        av_row.setContentsMargins(0, 0, 0, 0)

        # Conteneur avatar + badge viewers superposés via positions absolues
        av_container = QWidget()
        av_container.setFixedSize(_AVATAR_SZ + 4, _AVATAR_SZ + 4)
        av_container.setStyleSheet("background: transparent;")

        self._avatar_lbl = QLabel(av_container)
        self._avatar_lbl.setFixedSize(_AVATAR_SZ, _AVATAR_SZ)
        self._avatar_lbl.move(2, 2)
        self._avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar_lbl.setStyleSheet(
            f"border-radius: {_AVATAR_SZ // 2}px; "
            "background-color: #222222; "
            "font-family: 'Segoe UI Variable'; font-size: 15px; font-weight: bold; "
            + ("color: #555555;" if not s.online else "color: #00ff87;")
        )
        self._avatar_lbl.setText(s.twitch_login[:2].upper())

        from widgets.bigscreen_widget import load_avatar_into_label as _load_av
        _load_av(self._avatar_lbl, s.twitch_login, s.display, _AVATAR_SZ,
                 getattr(s, "profile_url", ""))

        if not s.online:
            _eff = QGraphicsOpacityEffect(av_container)
            _eff.setOpacity(0.35)
            av_container.setGraphicsEffect(_eff)

        # Badge viewers (coin bas-droite de l'avatar)
        if s.online and s.viewers:
            vbadge = QLabel(_fmt_viewers(s.viewers), av_container)
            vbadge.setFont(_bold_font(_FONT_MONO, 7))
            vbadge.setStyleSheet(
                "background-color: rgba(0,0,0,210); color: #00ff87; "
                "border-radius: 6px; padding: 1px 4px; border: none;"
            )
            vbadge.adjustSize()
            vbadge.move(_AVATAR_SZ + 4 - vbadge.width(), _AVATAR_SZ + 4 - vbadge.height())
            self._viewers_badge = vbadge
        else:
            self._viewers_badge = None

        av_row.addWidget(av_container)
        av_row.addStretch()

        # Badge type LAN / Online (coin haut-droit)
        loc = (s.location or "").upper()
        if loc == "LAN":
            badge_css = (
                f"background-color: #451a03; color: {_COL_LAN}; "
                f"border: 1px solid {_COL_LAN}; border-radius: 6px; padding: 0px 5px;"
            )
            badge_text = "LAN"
        else:
            badge_css = (
                f"background-color: {_COL_ONLINE_BG}; color: {_COL_ONLINE}; "
                f"border: 1px solid {_COL_ONLINE}55; border-radius: 6px; padding: 0px 5px;"
            )
            badge_text = "Online"

        type_badge = QLabel(badge_text)
        type_badge.setFont(_bold_font(_FONT_SEGOE, 8))
        type_badge.setStyleSheet(badge_css)
        type_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        av_row.addWidget(type_badge, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(av_row)
        root.addSpacing(14)

        # ── Nom ───────────────────────────────────────────────────
        name_lbl = QLabel(s.display)
        name_color = "#e8e8e8" if s.online else "#505050"
        name_lbl.setFont(_bold_font(_FONT_SEGOE, 12))
        name_lbl.setStyleSheet(f"color: {name_color}; background: transparent; border: none;")
        fm = QFontMetrics(name_lbl.font())
        name_lbl.setText(fm.elidedText(s.display, Qt.TextElideMode.ElideRight, _CARD_W))
        root.addWidget(name_lbl)

        # ── Jeu ───────────────────────────────────────────────────
        if s.game:
            game_lbl = QLabel(s.game)
            game_lbl.setFont(QFont(_FONT_SEGOE, 10))
            game_color = "#888888" if s.online else "#3a3a3a"
            game_lbl.setStyleSheet(f"color: {game_color}; background: transparent; border: none;")
            fm2 = QFontMetrics(game_lbl.font())
            game_lbl.setText(fm2.elidedText(s.game, Qt.TextElideMode.ElideRight, _CARD_W))
            root.addWidget(game_lbl)

        # ── Cagnotte ──────────────────────────────────────────────
        if s.donation > 0:
            don_lbl = QLabel(f"\u2665 {s.donation_formatted}")
            don_lbl.setFont(QFont(_FONT_SEGOE, 9))
            don_color = "#3d9970" if s.online else "#2a2a2a"
            don_lbl.setStyleSheet(f"color: {don_color}; background: transparent; border: none;")
            root.addWidget(don_lbl)

        root.addStretch()

        # ── Badge slot (cercle vert, overlay absolu coin haut-gauche) ─
        self._slot_lbl = QLabel(self)
        self._slot_lbl.setFixedSize(24, 24)
        self._slot_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
        self._slot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._slot_lbl.setStyleSheet(
            "background-color: #00ff87; color: #000000; "
            "border-radius: 12px; border: none;"
        )
        self._slot_lbl.move(8, 8)
        self._update_slot_badge()

    def _apply_style(self) -> None:
        if self._slot is not None:
            self.setStyleSheet(
                f"QFrame {{"
                f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
                f" stop:0 rgba(0,255,135,24), stop:0.45 #131313, stop:1 #111111);"
                f"border: 2px solid {_COL_SEL};"
                f"border-radius: 8px;"
                f"}}"
            )
        elif self._online:
            self.setStyleSheet(
                "QFrame { background-color: #111111; border: 1px solid #282828; "
                "border-radius: 8px; }"
                "QFrame:hover { background-color: #181818; border-color: #3a3a3a; }"
            )
        else:
            self.setStyleSheet(
                "QFrame { background-color: #0c0c0c; border: 1px solid #1a1a1a; "
                "border-radius: 8px; }"
            )

    def _update_slot_badge(self) -> None:
        if self._slot is not None:
            self._slot_lbl.setText(str(self._slot))
            self._slot_lbl.show()
        else:
            self._slot_lbl.hide()

    # -- public API ------------------------------------------------------------

    def set_slot(self, slot: int | None) -> None:
        self._slot = slot
        self._apply_style()
        self._update_slot_badge()

    def update_viewers(self, viewers: int) -> None:
        if self._viewers_badge is not None:
            self._viewers_badge.setText(_fmt_viewers(viewers))
            self._viewers_badge.adjustSize()

    # -- events ----------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._online:
            self.toggled.emit(self._login, self._slot is None)
        super().mousePressEvent(event)


# ── Section header ────────────────────────────────────────────────────────

class _SectionHeader(QWidget):
    """Séparateur de section avec titre et compteur."""

    def __init__(self, title: str, count: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        root = QHBoxLayout(self)
        root.setContentsMargins(2, 0, 4, 0)
        root.setSpacing(8)

        self._title_lbl = QLabel(title)
        self._title_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
        self._title_lbl.setStyleSheet("color: #666666; letter-spacing: 2px; background: transparent;")
        root.addWidget(self._title_lbl)

        self._count_lbl = QLabel(str(count))
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet("color: #444444; background: transparent;")
        root.addWidget(self._count_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: none; background-color: #1e1e1e;")
        sep.setFixedHeight(1)
        root.addWidget(sep, stretch=1)

    def set_count(self, count: int) -> None:
        self._count_lbl.setText(str(count))


# ── CardsGrid — disposition en grille fluide ──────────────────────────────

class _CardsGrid(QWidget):
    """Conteneur de cartes organisées en grille N colonnes, fluide."""

    _COLS = 4
    _GAP  = 10

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 2, 0, 6)
        self._layout.setSpacing(self._GAP)
        for c in range(self._COLS):
            self._layout.setColumnStretch(c, 1)

    def populate(self, cards: list["_StreamerCard"]) -> None:
        # Vider sans détruire (les cartes sont gérées par _StreamersTab)
        while self._layout.count():
            self._layout.takeAt(0)
        for i, card in enumerate(cards):
            row, col = divmod(i, self._COLS)
            self._layout.addWidget(card, row, col)
        # Stretch final pour coller les cartes en haut
        self._layout.setRowStretch(
            (len(cards) - 1) // self._COLS + 1 if cards else 0, 1
        )


# ── _StreamersTab ─────────────────────────────────────────────────────────

class _StreamersTab(QWidget):
    """Onglet Streamers — vue en cartes interactives pour sélectionner la grille."""

    grid_selection_changed = pyqtSignal(list)  # list[str] twitch_logins

    MAX_SELECTED: int = 25

    # Modes de tri
    _SORT_VIEWERS = 0
    _SORT_ALPHA   = 1
    _SORT_DONATION = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._streamers: list[StreamerInfo] = []
        # Ordre d'insertion maintenu (list), pas set, pour les numéros de slot
        self._selected: list[str] = []
        # Références aux cartes créées (login → card)
        self._card_map: dict[str, _StreamerCard] = {}
        self._sort_mode: int = self._SORT_VIEWERS
        # Empreinte structurelle : {(login, online)} — évite un rebuild inutile
        self._prev_structure: frozenset[tuple[str, bool]] = frozenset()
        self._build()

    # -- construction ----------------------------------------------------------

    def _build(self) -> None:
        self.setStyleSheet(_SS_BG_DARK)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        # ── Toolbar ──────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        sort_lbl = QLabel("Tri :")
        sort_lbl.setFont(QFont(_FONT_SEGOE, 10))
        sort_lbl.setStyleSheet(_SS_GREY_CLEAR)
        toolbar.addWidget(sort_lbl)

        self._sort_combo = QComboBox()
        self._sort_combo.addItem("Viewers ↓", self._SORT_VIEWERS)
        self._sort_combo.addItem("Alphabétique", self._SORT_ALPHA)
        self._sort_combo.addItem("Cagnotte ↓", self._SORT_DONATION)
        self._sort_combo.setFont(QFont(_FONT_SEGOE, 10))
        self._sort_combo.setMinimumHeight(26)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        toolbar.addWidget(self._sort_combo)

        toolbar.addStretch()

        self._counter_lbl = QLabel(f"Grille : 0 / {self.MAX_SELECTED}")
        self._counter_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
        self._counter_lbl.setStyleSheet(_SS_GREY_CLEAR)
        toolbar.addWidget(self._counter_lbl)

        toolbar.addSpacing(8)

        btn_all = QPushButton("✓ Tous")
        btn_all.setObjectName("watchBtn")
        btn_all.setFont(QFont(_FONT_SEGOE, 9))
        btn_all.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_all.clicked.connect(self._select_all)
        toolbar.addWidget(btn_all)

        btn_none = QPushButton("✗ Aucun")
        btn_none.setObjectName("watchBtn")
        btn_none.setFont(QFont(_FONT_SEGOE, 9))
        btn_none.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_none.clicked.connect(self._deselect_all)
        toolbar.addWidget(btn_none)

        root.addLayout(toolbar)

        # Séparateur
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #1e1e1e;")
        root.addWidget(sep)

        # ── Zone scrollable ───────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background: #0a0a0a; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #0a0a0a; }"
        )

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: #0a0a0a;")
        self._scroll_vl = QVBoxLayout(self._scroll_content)
        self._scroll_vl.setContentsMargins(0, 4, 0, 8)
        self._scroll_vl.setSpacing(4)
        self._scroll_vl.addStretch()

        self._scroll.setWidget(self._scroll_content)

        # ── Loader (montré pendant la reconstruction des cartes) ───────────────
        self._loader_lbl = QLabel("Chargement\u2026")
        self._loader_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loader_lbl.setFont(QFont(_FONT_SEGOE, 11))
        self._loader_lbl.setStyleSheet("color: #444444; background: #0a0a0a; border: none;")
        self._loader_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        # QStackedWidget alterne entre loader (0) et scroll (1)
        self._content_stack = QStackedWidget()
        self._content_stack.addWidget(self._loader_lbl)  # index 0
        self._content_stack.addWidget(self._scroll)      # index 1
        self._content_stack.setCurrentIndex(1)
        root.addWidget(self._content_stack, stretch=1)

        # Grilles par section (créées une fois, repeuplées)
        self._grid_lan    = _CardsGrid()
        self._grid_online = _CardsGrid()
        self._grid_off    = _CardsGrid()

        self._hdr_lan    = _SectionHeader("LAN")
        self._hdr_online = _SectionHeader("EN LIGNE")
        self._hdr_off    = _SectionHeader("HORS LIGNE")

    # -- public ---------------------------------------------------------------

    def refresh(self, streamers: list[StreamerInfo], selected_logins: list[str]) -> None:
        """Met à jour les cartes — rebuild complet si la structure change, patch sinon."""
        online_logins = {s.twitch_login for s in streamers if s.online}
        self._selected = [lg for lg in selected_logins if lg in online_logins]

        new_structure = frozenset(
            (s.twitch_login, s.online) for s in streamers
        )
        if new_structure == self._prev_structure and self._card_map:
            # Uniquement les viewers ont changé — mise à jour en place, aucun freeze
            self._streamers = streamers
            self._patch_viewers(streamers)
            self._update_counter()
        else:
            # Structure différente — rebuild avec loader
            self._prev_structure = new_structure
            self._streamers = streamers
            self._content_stack.setCurrentIndex(0)  # affiche "Chargement…"
            QTimer.singleShot(0, self._deferred_rebuild)

    def set_max_streams(self, n: int) -> None:
        """Propage le maximum de streams autorisés (depuis settings)."""
        self.MAX_SELECTED = max(1, n)
        self._update_counter()

    # -- fast-path & deferred rebuild -----------------------------------------

    def _patch_viewers(self, streamers: list[StreamerInfo]) -> None:
        """Met à jour les badges viewers sans reconstruire les cartes."""
        for s in streamers:
            card = self._card_map.get(s.twitch_login)
            if card is not None:
                card.update_viewers(s.viewers)

    def _deferred_rebuild(self) -> None:
        """Reconstruit toutes les cartes (appelé après un tick QTimer)."""
        self._rebuild_cards()
        self._update_counter()
        self._content_stack.setCurrentIndex(1)  # repasse en vue scroll

    # -- internal -------------------------------------------------------------

    def _sorted_streamers(self, items: list[StreamerInfo]) -> list[StreamerInfo]:
        if self._sort_mode == self._SORT_ALPHA:
            return sorted(items, key=lambda s: s.display.lower())
        elif self._sort_mode == self._SORT_DONATION:
            return sorted(items, key=lambda s: -s.donation)
        else:  # SORT_VIEWERS
            return sorted(items, key=lambda s: -s.viewers)

    def _rebuild_cards(self) -> None:
        """Détruit toutes les cartes et les reconstruit selon le tri actuel."""
        # Supprimer les anciennes cartes de leur conteneur
        for card in self._card_map.values():
            card.setParent(None)  # type: ignore[arg-type]
            card.deleteLater()
        self._card_map.clear()

        # Supprimer tout du scroll_vl (ne pas détruire les grilles/headers — réutilisés)
        while self._scroll_vl.count():
            self._scroll_vl.takeAt(0)

        sel_set = set(self._selected)

        lan_streamers    = self._sorted_streamers([s for s in self._streamers if s.online and (s.location or "").upper() == "LAN"])
        online_streamers = self._sorted_streamers([s for s in self._streamers if s.online and (s.location or "").upper() != "LAN"])
        off_streamers    = self._sorted_streamers([s for s in self._streamers if not s.online])

        def _make_cards(items: list[StreamerInfo]) -> list[_StreamerCard]:
            cards: list[_StreamerCard] = []
            for s in items:
                slot: int | None = None
                if s.twitch_login in sel_set:
                    slot = self._selected.index(s.twitch_login) + 1
                card = _StreamerCard(s, slot)
                card.toggled.connect(self._on_card_toggled)
                self._card_map[s.twitch_login] = card
                cards.append(card)
            return cards

        # Section LAN
        if lan_streamers:
            self._hdr_lan.set_count(len(lan_streamers))
            self._scroll_vl.addWidget(self._hdr_lan)
            cards_lan = _make_cards(lan_streamers)
            self._grid_lan.populate(cards_lan)
            self._scroll_vl.addWidget(self._grid_lan)

        # Section Online
        if online_streamers:
            self._hdr_online.set_count(len(online_streamers))
            self._scroll_vl.addWidget(self._hdr_online)
            cards_online = _make_cards(online_streamers)
            self._grid_online.populate(cards_online)
            self._scroll_vl.addWidget(self._grid_online)

        # Section Hors ligne
        if off_streamers:
            self._hdr_off.set_count(len(off_streamers))
            self._scroll_vl.addWidget(self._hdr_off)
            cards_off = _make_cards(off_streamers)
            self._grid_off.populate(cards_off)
            self._scroll_vl.addWidget(self._grid_off)

        self._scroll_vl.addStretch()

    def _renumber_slots(self) -> None:
        """Resynchronise les numéros de slot sur toutes les cartes."""
        sel_set = set(self._selected)
        for login, card in self._card_map.items():
            if login in sel_set:
                card.set_slot(self._selected.index(login) + 1)
            else:
                card.set_slot(None)

    def _update_counter(self) -> None:
        n = len(self._selected)
        color = "#ff4444" if n >= self.MAX_SELECTED else "#888888"
        self._counter_lbl.setText(f"Grille : {n} / {self.MAX_SELECTED}")
        self._counter_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")

    # -- slots ----------------------------------------------------------------

    def _on_card_toggled(self, login: str, add: bool) -> None:
        if add:
            if len(self._selected) >= self.MAX_SELECTED:
                # Flash rouge sur le compteur pour signaler le refus
                self._counter_lbl.setStyleSheet("color: #ff4444; background: transparent; border: none;")
                QTimer.singleShot(1000, self._update_counter)
                return
            if login not in self._selected:
                self._selected.append(login)
        else:
            if login in self._selected:
                self._selected.remove(login)
        self._renumber_slots()
        self._update_counter()
        self.grid_selection_changed.emit(list(self._selected))

    def _on_sort_changed(self, idx: int) -> None:
        self._sort_mode = self._sort_combo.itemData(idx)
        self._rebuild_cards()

    def _select_all(self) -> None:
        live_logins = [
            s.twitch_login
            for s in self._sorted_streamers([s for s in self._streamers if s.online])
        ]
        self._selected = live_logins[: self.MAX_SELECTED]
        self._rebuild_cards()
        self._update_counter()
        self.grid_selection_changed.emit(list(self._selected))

    def _deselect_all(self) -> None:
        self._selected.clear()
        self._renumber_slots()
        self._update_counter()
        self.grid_selection_changed.emit([])

    def add_login(self, login: str) -> None:
        """Ajoute un login à la sélection grille si possible (depuis timeline)."""
        if login in self._selected:
            return
        if len(self._selected) >= self.MAX_SELECTED:
            return
        online = {s.twitch_login for s in self._streamers if s.online}
        if login not in online:
            return
        self._selected.append(login)
        self._renumber_slots()
        self._update_counter()
        self.grid_selection_changed.emit(list(self._selected))


# ---------------------------------------------------------------------------
# Stats helpers — axes personnalisés + widget LAN/Online
# ---------------------------------------------------------------------------

JOURS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
PARIS = timedelta(hours=2)


class LanOnlineWidget(QWidget):
    """Barres verticales LAN vs Online (viewers + count) — dessin custom QPainter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lan_count: int = 0
        self._online_count: int = 0
        self._lan_viewers: int = 0
        self._online_viewers: int = 0
        self.setFixedHeight(200)

    def update_data(self, streamers: list[StreamerInfo]) -> None:
        lan = [s for s in streamers if s.online and s.location == "LAN"]
        online = [s for s in streamers if s.online and s.location != "LAN"]
        self._lan_count = len(lan)
        self._online_count = len(online)
        self._lan_viewers = sum(s.viewers for s in lan)
        self._online_viewers = sum(s.viewers for s in online)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#0f0f0f"))

        max_v = max(self._lan_viewers, self._online_viewers, 1)
        bar_w = min(w // 3, 120)
        bar_max_h = h - 60

        # LAN bar
        lan_h = int(self._lan_viewers / max_v * bar_max_h)
        lan_x = w // 4 - bar_w // 2
        p.fillRect(lan_x, h - 40 - lan_h, bar_w, lan_h, QColor("#00ff87"))
        p.setPen(QPen(QColor("#ffffff")))
        p.setFont(QFont(_FONT_SEGOE, 10))
        p.drawText(lan_x, h - 40 - lan_h - 5, f"{self._lan_count} LAN")
        lan_v_str = f"{self._lan_viewers // 1000}k" if self._lan_viewers > 999 else str(self._lan_viewers)
        p.setPen(QPen(QColor("#00ff87")))
        p.drawText(lan_x, h - 25, lan_v_str)
        p.setPen(QPen(QColor("#888888")))
        p.drawText(lan_x, h - 10, "viewers")

        # Online bar
        online_h = int(self._online_viewers / max_v * bar_max_h)
        online_x = 3 * w // 4 - bar_w // 2
        p.fillRect(online_x, h - 40 - online_h, bar_w, online_h, QColor("#3b82f6"))
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(online_x, h - 40 - online_h - 5, f"{self._online_count} Online")
        online_v_str = f"{self._online_viewers // 1000}k" if self._online_viewers > 999 else str(self._online_viewers)
        p.setPen(QPen(QColor("#3b82f6")))
        p.drawText(online_x, h - 25, online_v_str)
        p.setPen(QPen(QColor("#888888")))
        p.drawText(online_x, h - 10, "viewers")


# ---------------------------------------------------------------------------
# Tab: Stats — 5 graphes en layout 2 colonnes
# ---------------------------------------------------------------------------

class _StatsTab(QWidget):
    """Onglet Stats — évolution cagnotte, viewers (Chart.js), LAN/Online, ranking, top viewers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._g5_bars: pg.BarGraphItem | None = None
        self._charts_view: QWebEngineView | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Row 0 — Chart.js (cagnotte + viewers)
        self._charts_view = QWebEngineView()
        self._charts_view.setFixedHeight(420)
        self._charts_view.setHtml(self._build_charts_html([], [], [], []))
        root.addWidget(self._charts_view, stretch=2)

        # Row 1 — LAN/Online + Top viewers (fixed height)
        middle = QWidget()
        middle.setFixedHeight(220)
        hbox = QHBoxLayout(middle)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(8)
        self._lan_online = LanOnlineWidget()
        hbox.addWidget(self._lan_online)
        hbox.addWidget(self._build_g5_top_viewers())
        root.addWidget(middle)

        # Row 2 — Ranking
        root.addWidget(self._build_g4_ranking(), stretch=1)

    # -- helpers --------------------------------------------------------------

    def _make_frame(self) -> QFrame:
        f = QFrame()
        f.setObjectName("card")
        return f

    # -- chart HTML builder ---------------------------------------------------

    def _build_charts_html(
        self,
        ts_don: list[float],
        val_don: list[float],
        ts_view: list[float],
        val_view: list[float],
    ) -> str:
        def fmt(ts: float) -> str:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc) + PARIS
            return f"{JOURS_FR[dt.weekday()]} {dt.hour:02d}h"

        labels_don = json.dumps([fmt(ts) for ts in ts_don])
        data_don = json.dumps([round(v) for v in val_don])
        labels_view = json.dumps([fmt(ts) for ts in ts_view])
        data_view = json.dumps([round(v) for v in val_view])

        # Pas de repli sur le CDN distant : seul le fichier local, dont
        # l'empreinte a été vérifiée au démarrage, est inliné.
        if _CHARTJS_PATH.exists():
            chart_js_content = _CHARTJS_PATH.read_text(encoding="utf-8")
            script_tag = f"<script>{chart_js_content}</script>"
        else:
            logger.warning("Chart.js absent — graphique non rendu")
            script_tag = ""

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0f0f0f; color: #888; font-family: 'Segoe UI Variable', sans-serif; overflow: hidden; }}
  .chart-wrap {{ width: 100%; padding: 8px 12px 4px; }}
  .chart-title {{ font-size: 11px; letter-spacing: 1px; margin-bottom: 4px; }}
  .don-title {{ color: #00ff87; }}
  .view-title {{ color: #38bdf8; }}
  canvas {{ height: 180px !important; max-height: 180px; }}
</style></head>
<body>
<div class="chart-wrap">
  <div class="chart-title don-title">ÉVOLUTION CAGNOTTE 72h</div>
  <canvas id="cagnotteChart" height="160"></canvas>
</div>
<div class="chart-wrap">
  <div class="chart-title view-title">VIEWERS TOTAUX 72h</div>
  <canvas id="viewersChart" height="160"></canvas>
</div>
{script_tag}
<script>
const BASE = {{
  responsive: true,
  maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{
    legend: {{ display: false }},
    tooltip: {{
      backgroundColor: '#1a1a1a', borderColor: '#333', borderWidth: 1,
      titleColor: '#888', bodyColor: '#fff',
    }},
  }},
  scales: {{
    x: {{
      ticks: {{ color: '#666', font: {{ family: 'Cascadia Code', size: 9 }}, maxRotation: 0 }},
      grid: {{ color: '#1a1a1a' }},
      border: {{ color: '#333' }},
    }},
    y: {{
      ticks: {{ color: '#666', font: {{ family: 'Cascadia Code', size: 9 }} }},
      grid: {{ color: '#1a1a1a' }},
      border: {{ color: '#333' }},
    }},
  }},
}};

new Chart(document.getElementById('cagnotteChart'), {{
  type: 'line',
  data: {{
    labels: {labels_don},
    datasets: [{{
      data: {data_don},
      borderColor: '#00ff87', backgroundColor: 'rgba(0,255,135,0.08)',
      borderWidth: 2, pointRadius: 0, fill: true, tension: 0.1,
    }}],
  }},
  options: {{
    ...BASE,
    scales: {{ ...BASE.scales, y: {{ ...BASE.scales.y,
      ticks: {{ ...BASE.scales.y.ticks,
        callback: v => v >= 1e6 ? (v/1e6).toFixed(1)+'M€' : v >= 1000 ? (v/1000).toFixed(0)+'k€' : v+'€',
      }},
    }} }},
  }},
}});

new Chart(document.getElementById('viewersChart'), {{
  type: 'line',
  data: {{
    labels: {labels_view},
    datasets: [{{
      data: {data_view},
      borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,0.08)',
      borderWidth: 2, pointRadius: 0, fill: true, tension: 0.1,
    }}],
  }},
  options: {{
    ...BASE,
    scales: {{ ...BASE.scales, y: {{ ...BASE.scales.y,
      ticks: {{ ...BASE.scales.y.ticks,
        callback: v => v >= 1000 ? (v/1000).toFixed(0)+'k' : String(v),
      }},
    }} }},
  }},
}});
</script>
</body></html>"""

    # -- chart builders -------------------------------------------------------

    def _build_g4_ranking(self) -> QFrame:
        frame = self._make_frame()
        vl = QVBoxLayout(frame)
        vl.setContentsMargins(8, 8, 8, 8)
        vl.setSpacing(6)

        hdr = QLabel("CLASSEMENT CAGNOTTES")
        hdr.setFont(_bold_font(_FONT_SEGOE, 10))
        hdr.setStyleSheet("color: #00ff87; letter-spacing: 2px;")
        vl.addWidget(hdr)

        self._ranking_table = QTableWidget(0, 4)
        self._ranking_table.setHorizontalHeaderLabels(["#", "Streamer", "Cagnotte", "Viewers"])
        hh = self._ranking_table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._ranking_table.setColumnWidth(0, 30)
        self._ranking_table.setColumnWidth(2, 110)
        self._ranking_table.setColumnWidth(3, 60)
        self._ranking_table.verticalHeader().setVisible(False)
        self._ranking_table.setShowGrid(False)
        self._ranking_table.setAlternatingRowColors(True)
        self._ranking_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._ranking_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._ranking_table.setStyleSheet("""
            QTableWidget {
                background-color: #0f0f0f;
                alternate-background-color: #111111;
                color: #ffffff;
                font-family: "Segoe UI Variable";
                font-size: 11px;
                border: none;
            }
            QTableWidget::item { padding: 4px; border: none; }
            QHeaderView::section {
                background-color: #111111;
                color: #00ff87;
                font-size: 10px;
                border: none;
                padding: 4px;
            }
        """)
        vl.addWidget(self._ranking_table, stretch=1)
        return frame

    def _build_g5_top_viewers(self) -> QFrame:
        frame = self._make_frame()
        vl = QVBoxLayout(frame)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        title = QLabel("TOP VIEWERS")
        title.setFont(_bold_font(_FONT_SEGOE, 9))
        title.setStyleSheet(_SS_GREEN_SPACED)
        vl.addWidget(title)

        self._g5_plot = pg.PlotWidget()
        self._g5_plot.setBackground("#0f0f0f")
        self._g5_plot.setMouseEnabled(x=False, y=False)
        self._g5_plot.setMenuEnabled(False)
        self._g5_plot.hideButtons()
        self._g5_plot.showGrid(x=True, y=False, alpha=0.15)
        for axis_name in ("left", "bottom"):
            ax = self._g5_plot.getAxis(axis_name)
            ax.setStyle(tickFont=QFont(_FONT_MONO, 8))
            ax.setTextPen("#888888")
            ax.setPen("#333333")
        vl.addWidget(self._g5_plot)
        return frame

    # -- public API -----------------------------------------------------------

    def update_history(self, history: HistoryStore) -> None:
        """Met à jour les graphes Chart.js cagnotte et viewers."""
        ts_d, vals_d = history.get_donation_series()
        ts_v, vals_v = history.get_viewers_series()
        if self._charts_view is not None:
            html = self._build_charts_html(ts_d, vals_d, ts_v, list(vals_v))
            tmp = _CHARTJS_PATH.parent / "stats_chart.html"
            tmp.write_text(html, encoding="utf-8")
            self._charts_view.setUrl(QUrl.fromLocalFile(str(tmp)))

    def update_streamers(self, streamers: list[StreamerInfo]) -> None:
        """Rafraîchit LAN/Online, ranking et top viewers."""
        # LAN vs Online
        self._lan_online.update_data(streamers)

        # Ranking (top 20 par donation)
        sorted_s = sorted(streamers, key=lambda x: -x.donation)[:20]
        self._ranking_table.setRowCount(len(sorted_s))
        for i, s in enumerate(sorted_s):
            rank_item = QTableWidgetItem(str(i + 1))
            rank_item.setForeground(QBrush(QColor("#00ff87")))
            self._ranking_table.setItem(i, 0, rank_item)
            self._ranking_table.setItem(i, 1, QTableWidgetItem(s.display))
            amt_item = QTableWidgetItem(s.donation_formatted)
            amt_item.setForeground(QBrush(QColor("#00ff87")))
            self._ranking_table.setItem(i, 2, amt_item)
            v_item = QTableWidgetItem(
                f"{s.viewers / 1000:.1f}k" if s.viewers > 999 else str(s.viewers)
            )
            v_item.setForeground(QBrush(QColor("#00ff87" if s.online else "#555555")))
            self._ranking_table.setItem(i, 3, v_item)

        # Top 10 viewers — barres horizontales
        top10 = sorted([s for s in streamers if s.online], key=lambda x: -x.viewers)[:10]
        self._g5_plot.clear()
        self._g5_bars = None
        if not top10:
            return
        y = list(range(len(top10)))
        x = [s.viewers for s in top10]
        names = [s.display[:12] for s in top10]
        self._g5_bars = pg.BarGraphItem(
            x0=0, y=y, height=0.6, width=x,
            brush=pg.mkBrush("#00ff87"),
            pen=pg.mkPen(None),
        )
        self._g5_plot.addItem(self._g5_bars)
        ax = self._g5_plot.getAxis("left")
        ax.setTicks([list(zip(y, names))])
        ax.setStyle(tickFont=QFont(_FONT_MONO, 8))
        max_v = max(x) if x else 1
        self._g5_plot.setXRange(0, max_v * 1.1)
        self._g5_plot.setYRange(-0.5, len(top10) - 0.5)


# ---------------------------------------------------------------------------
# Tab: Placeholder (Stats)
# ---------------------------------------------------------------------------

class _PlaceholderTab(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(text)
        lbl.setObjectName("placeholder")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)


# ---------------------------------------------------------------------------
# TabButton — bouton d'onglet personnalisé
# ---------------------------------------------------------------------------

class _TabButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFont(QFont(_FONT_SEGOE, 12))
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._apply(False)

    def set_active(self, on: bool) -> None:
        self.setChecked(on)
        self._apply(on)

    def _apply(self, on: bool) -> None:
        if on:
            self.setStyleSheet(
                "QPushButton { color: #ffffff; background: transparent; border: none; "
                "border-bottom: 2px solid #00ff87; padding: 8px 16px; "
                "font-family: 'Segoe UI Variable'; font-size: 12px; }"
            )
        else:
            self.setStyleSheet(
                "QPushButton { color: #888888; background: transparent; border: none; "
                "border-bottom: 2px solid transparent; padding: 8px 16px; "
                "font-family: 'Segoe UI Variable'; font-size: 12px; }"
                "QPushButton:hover { color: #cccccc; }"
            )


# ---------------------------------------------------------------------------
# _SplashOverlay — écran de chargement au démarrage
# ---------------------------------------------------------------------------

class _SplashOverlay(QWidget):
    """Overlay plein-fenêtre affiché jusqu'au premier fetch API réussi."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background-color: #0a0a0a;")
        self.resize(parent.size())
        self.raise_()

        vl = QVBoxLayout(self)
        vl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.setSpacing(12)

        logo = QLabel("ZLink")
        logo.setFont(_bold_font(_FONT_SEGOE, 52))
        logo.setStyleSheet("color: #00ff87; background: transparent;")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(logo)

        sub = QLabel("ZEvent Viewer")
        sub.setFont(QFont(_FONT_SEGOE, 13))
        sub.setStyleSheet("color: #2a2a2a; background: transparent;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(sub)

        vl.addSpacing(32)

        self._status_lbl = QLabel("Connexion aux APIs")
        self._status_lbl.setFont(QFont(_FONT_SEGOE, 11))
        self._status_lbl.setStyleSheet("color: #444444; background: transparent;")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(self._status_lbl)

        self._dots = 0
        self._timer = QTimer(self)
        self._timer.setInterval(420)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._anim: QPropertyAnimation | None = None

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        self._status_lbl.setText("Connexion aux APIs" + " ·" * self._dots)

    def set_status(self, text: str) -> None:
        self._timer.stop()
        self._status_lbl.setText(text)

    def dismiss(self) -> None:
        """Fade-out de 350 ms puis masquage."""
        self._timer.stop()
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        self._anim = QPropertyAnimation(eff, b"opacity", self)
        self._anim.setDuration(350)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)
        self._anim.start()


# ---------------------------------------------------------------------------
# PanelWindow
# ---------------------------------------------------------------------------

class PanelWindow(QMainWindow):
    """Fenêtre panel fullscreen. show_grid_tab=True en mode dual."""

    stream_selected = pyqtSignal(str)
    grid_selection_changed = pyqtSignal(list)  # list[str] twitch_logins
    settings_changed = pyqtSignal(dict)

    def __init__(
        self,
        screen: QScreen,
        *,
        show_grid_tab: bool = False,
        show_on_init: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._target_screen = screen
        self._show_grid_tab = show_grid_tab
        self.setWindowTitle("ZLink — Panel")
        self.setStyleSheet(PANEL_STYLE)

        self._tab_btns: list[_TabButton] = []
        self._grid_window: GridWindow | None = None

        # Cache local pour les données les plus récentes
        self._last_streamers: list[StreamerInfo] = []
        self._last_stats: GlobalStats = GlobalStats(0.0, "—", 0, "offline")

        # Références aux onglets dynamiques
        self._accueil_tab: _AccueilTab
        self._stats_tab: _StatsTab
        self._programme_tab: _ProgrammeTab
        self._goals_tab: _GoalsTab
        self._streamers_tab: _StreamersTab

        # Badge hors-event (dans le header)
        self._offline_badge: QLabel

        self._splash: _SplashOverlay | None = None
        self._first_data_received: bool = False

        self._build()
        if show_on_init:
            self._move_to_screen(screen)
            self._splash = _SplashOverlay(self)
            self._splash.show()

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header 60 px ─────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(60)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        hl.setSpacing(12)

        logo = QLabel("ZLink")
        logo.setFont(_bold_font(_FONT_SEGOE, 20))
        logo.setStyleSheet(_SS_GREEN)
        hl.addWidget(logo)

        # Badge hors-event (masqué par défaut)
        self._offline_badge = QLabel("HORS EVENT")
        self._offline_badge.setFont(_bold_font(_FONT_SEGOE, 10))
        self._offline_badge.setStyleSheet(
            "color: #ff8800; background-color: #2a1800; "
            "border: 1px solid #ff8800; border-radius: 4px; "
            "padding: 2px 8px;"
        )
        self._offline_badge.setVisible(False)
        hl.addWidget(self._offline_badge)

        hl.addStretch()

        ev = QLabel("ZEvent 2026")
        ev.setFont(QFont(_FONT_SEGOE, 13))
        ev.setStyleSheet(_SS_MUTED)
        hl.addWidget(ev)

        _btn_ss_normal = (
            "QPushButton { background: transparent; color: #666666; "
            "border: none; border-radius: 4px; }"
            "QPushButton:hover { color: #ffffff; background: #1a1a1a; }"
        )
        _btn_ss_quit = (
            "QPushButton { background: transparent; color: #666666; "
            "border: none; border-radius: 4px; }"
            "QPushButton:hover { color: #ff4444; background: #2a1a1a; }"
        )
        _btn_ss_bigscreen = (
            "QPushButton { background: transparent; color: #666666; "
            "border: none; border-radius: 4px; }"
            "QPushButton:hover { color: #ffffff; background: #1a1a1a; }"
            "QPushButton:checked { color: #00ff87; }"
        )

        settings_btn = QPushButton()
        settings_btn.setFixedSize(32, 32)
        settings_btn.setToolTip("Paramètres")
        settings_btn.setCheckable(True)
        if _QTA_OK:
            settings_btn.setIcon(qta.icon("mdi6.cog-outline", color="#666666"))
            settings_btn.setIconSize(settings_btn.size())
        else:
            settings_btn.setText("⚙")
            settings_btn.setStyleSheet(_btn_ss_normal + " QPushButton { font-size: 16px; }")
        settings_btn.setStyleSheet(_btn_ss_bigscreen)
        settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        settings_btn.clicked.connect(self._toggle_settings)
        self._settings_btn = settings_btn
        hl.addWidget(settings_btn)

        bigscreen_btn = QPushButton()
        bigscreen_btn.setFixedSize(32, 32)
        bigscreen_btn.setToolTip("Mode Big Screen")
        bigscreen_btn.setCheckable(True)
        if _QTA_OK:
            bigscreen_btn.setIcon(qta.icon("mdi6.fullscreen", color="#666666"))
            bigscreen_btn.setIconSize(bigscreen_btn.size())
        else:
            bigscreen_btn.setText("⧆")
        bigscreen_btn.setStyleSheet(_btn_ss_bigscreen)
        bigscreen_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        bigscreen_btn.clicked.connect(self._toggle_bigscreen)
        self._bigscreen_btn = bigscreen_btn
        hl.addWidget(bigscreen_btn)

        quit_btn = QPushButton()
        quit_btn.setFixedSize(32, 32)
        if _QTA_OK:
            quit_btn.setIcon(qta.icon("mdi6.close", color="#666666"))
            quit_btn.setIconSize(quit_btn.size())
        else:
            quit_btn.setText("✕")
        quit_btn.setStyleSheet(_btn_ss_quit)
        quit_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        quit_btn.clicked.connect(QApplication.quit)
        hl.addWidget(quit_btn)

        self._header = header
        root.addWidget(header)

        # ── Tab bar 40 px ────────────────────────────────────────────
        bar = QWidget()
        bar.setObjectName("tabBar")
        bar.setFixedHeight(40)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 0, 16, 0)
        bl.setSpacing(0)

        names = ["Accueil", "Programme", "Stats", "Goals", "Streamers"]
        if self._show_grid_tab:
            names.append("Grille")
        for n in names:
            btn = _TabButton(n)
            btn.clicked.connect(self._on_tab)
            bl.addWidget(btn)
            self._tab_btns.append(btn)
        bl.addStretch()
        self._tab_bar = bar
        root.addWidget(bar)

        # ── Stack ────────────────────────────────────────────────────
        self._stack = QStackedWidget()

        self._accueil_tab = _AccueilTab()
        self._accueil_tab.stream_selected.connect(self.stream_selected)
        self._accueil_tab.add_to_grid.connect(self._on_add_to_grid)
        self._stack.addWidget(self._accueil_tab)

        self._programme_tab = _ProgrammeTab()
        self._stack.addWidget(self._programme_tab)

        self._stats_tab = _StatsTab()
        self._stack.addWidget(self._stats_tab)

        self._goals_tab = _GoalsTab()
        self._stack.addWidget(self._goals_tab)

        self._streamers_tab = _StreamersTab()
        self._streamers_tab.grid_selection_changed.connect(self.grid_selection_changed)
        self._stack.addWidget(self._streamers_tab)

        if self._show_grid_tab:
            # Placeholder vide — le clic sur cet onglet déclenche le switch vers GridWindow
            self._stack.addWidget(QWidget())

        root.addWidget(self._stack, stretch=1)

        # ── Big Screen (caché par défaut, superposé au stack) ────────
        self._bigscreen = BigScreenWidget(central)
        self._bigscreen.close_requested.connect(self._close_bigscreen)
        self._bigscreen.hide()

        # ── Settings panel (caché par défaut, superposé au stack) ────
        from windows.settings import SettingsPanel
        self._settings_panel = SettingsPanel(central)
        self._settings_panel.settings_changed.connect(self._on_settings_changed)
        self._settings_panel.close_requested.connect(self._close_settings)
        self._settings_panel.hide()

        self.setCentralWidget(central)
        self._set_active(0)

    def _move_to_screen(self, screen: QScreen) -> None:
        g = screen.geometry()
        self.setGeometry(g)
        self.show()  # crée le handle natif à la bonne position
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.showFullScreen()
        logger.info("Panel ouverte sur %s (%dx%d)", screen.name(), g.width(), g.height())

    # -- public API -----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._splash is not None and self._splash.isVisible():
            self._splash.resize(self.size())

    def update_streamers(
        self,
        streamers: list[StreamerInfo],
        selected_logins: list[str] | None = None,
    ) -> None:
        """Propagé depuis DataManager.streamers_updated."""
        if not self._first_data_received:
            self._first_data_received = True
            if self._splash is not None:
                self._splash.dismiss()
        self._last_streamers = streamers
        self._accueil_tab.refresh(streamers, self._last_stats)
        self._goals_tab.set_streamers(streamers)
        self._streamers_tab.refresh(streamers, selected_logins or [])
        self._stats_tab.update_streamers(streamers)
        gdoc_display = {s.gdoc_id: s.display for s in streamers if s.gdoc_id}
        self._programme_tab.set_gdoc_map(gdoc_display)
        self._bigscreen.update_streamers(streamers)

    def update_stats(self, stats: GlobalStats) -> None:
        """Propagé depuis DataManager.global_stats_updated."""
        self._last_stats = stats
        self._accueil_tab.refresh(self._last_streamers, stats)
        self._offline_badge.setVisible(stats.website_mode != "live")
        self._bigscreen.update_stats(stats)

    def update_data(
        self,
        streamers: list[StreamerInfo],
        stats: GlobalStats,
        selected_logins: list[str] | None = None,
    ) -> None:
        """Met à jour streamers ET stats en une seule passe (évite le double refresh)."""
        self._last_streamers = streamers
        self._last_stats = stats
        self._accueil_tab.refresh(streamers, stats)
        self._goals_tab.set_streamers(streamers)
        self._streamers_tab.refresh(streamers, selected_logins or [])
        self._offline_badge.setVisible(stats.website_mode != "live")
        self._stats_tab.update_streamers(streamers)
        gdoc_display = {s.gdoc_id: s.display for s in streamers if s.gdoc_id}
        self._programme_tab.set_gdoc_map(gdoc_display)

    def update_events(self, events: list[EventItem]) -> None:
        """Propagé depuis DataManager.events_updated."""
        self._programme_tab.update_events(events)
        self._accueil_tab.update_events(events)

    def update_history(self, history: HistoryStore) -> None:
        """Mise à jour des graphes d'historique (Accueil + Stats)."""
        self._accueil_tab.update_history(history)
        self._stats_tab.update_history(history)

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        """Propagé depuis DataManager.goals_updated."""
        self._accueil_tab.update_goals(goals)
        self._bigscreen.update_goals(goals)

    def update_goals_cache(self, cache: dict) -> None:
        """Propagé depuis DataManager.goals_raw_updated — seed le cache local du tab goals."""
        self._goals_tab.seed_cache(cache)

    # -- bigscreen ------------------------------------------------------------

    def _toggle_bigscreen(self, checked: bool) -> None:
        if checked:
            cw = self.centralWidget()
            self._bigscreen.setGeometry(0, 0, cw.width(), cw.height())
            self._header.hide()
            self._tab_bar.hide()
            self._stack.hide()
            self._bigscreen.show()
            self._bigscreen.raise_()
        else:
            self._bigscreen.hide()
            self._header.show()
            self._tab_bar.show()
            self._stack.show()

    def _close_bigscreen(self) -> None:
        self._bigscreen_btn.setChecked(False)
        self._bigscreen.hide()
        self._header.show()
        self._tab_bar.show()
        self._stack.show()

    # -- grid window ----------------------------------------------------------

    def set_grid_window(self, grid_window: GridWindow) -> None:
        """Associe la GridWindow pour le switch de visibilité (mode dual)."""
        self._grid_window = grid_window

    def _on_add_to_grid(self, login: str) -> None:
        """Ajoute un login à la grille depuis le clic timeline."""
        self._streamers_tab.add_login(login)

    # -- tabs -----------------------------------------------------------------

    def _on_tab(self) -> None:
        sender = self.sender()
        for i, b in enumerate(self._tab_btns):
            if b is sender:
                self._set_active(i)
                return

    def _set_active(self, idx: int) -> None:
        for i, b in enumerate(self._tab_btns):
            b.set_active(i == idx)
        # Onglet "Grille" → switch vers GridWindow (panel se cache)
        if idx < len(self._tab_btns) and self._tab_btns[idx].text() == "Grille":
            if self._grid_window is not None:
                self._grid_window.showFullScreen()
                self.hide()
            return
        self._stack.setCurrentIndex(idx)

    def switch_to_tab(self, tab_name: str) -> None:
        """Active un onglet par son nom. N'active jamais 'Grille' (déclenche le switch)."""
        for i, btn in enumerate(self._tab_btns):
            if btn.text() == tab_name and tab_name != "Grille":
                for j, b in enumerate(self._tab_btns):
                    b.set_active(j == i)
                self._stack.setCurrentIndex(i)
                return

    def _toggle_settings(self, checked: bool) -> None:
        if checked:
            cw = self.centralWidget()
            self._settings_panel.setGeometry(0, 0, cw.width(), cw.height())
            self._settings_panel.refresh_config()
            self._header.hide()
            self._tab_bar.hide()
            self._stack.hide()
            self._settings_panel.show()
            self._settings_panel.raise_()
        else:
            self._settings_panel.hide()
            self._header.show()
            self._tab_bar.show()
            self._stack.show()

    def _close_settings(self) -> None:
        self._settings_btn.setChecked(False)
        self._settings_panel.hide()
        self._header.show()
        self._tab_bar.show()
        self._stack.show()

    def _on_settings_changed(self, config: dict) -> None:
        max_s = config.get("max_active_streams", 20)
        self._streamers_tab.set_max_streams(max_s)
        self.settings_changed.emit(config)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)