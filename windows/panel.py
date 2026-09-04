# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fenêtre panel — stats, programme, donation goals, grille optionnelle."""

from __future__ import annotations

import asyncio
import json
import math
import hashlib
import os
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
import html as _html
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon, QLinearGradient, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QRegion, QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QSlider,
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
except Exception:  # noqa: BLE001
    # Pas seulement ImportError : qtawesome charge des polices et peut
    # échouer autrement. Un except trop étroit laissait _QTA_OK non
    # défini, et le démarrage plantait par NameError une fois sur six.
    qta = None  # type: ignore[assignment]
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
/* Bouton par défaut. Sans cette règle, tout bouton sans nom d'objet — ceux
   des boîtes de dialogue Qt notamment — retombe sur le style natif et la
   palette, et se retrouvait illisible sur fond sombre. */
QPushButton {
    background-color: #1e1e1e;
    color: #e8e8e8;
    border: 1px solid #333333;
    border-radius: 4px;
    padding: 4px 14px;
}
QPushButton:hover {
    background-color: #2a2a2a;
    border-color: #4a4a4a;
    color: #ffffff;
}
QPushButton:pressed {
    background-color: #151515;
    color: #ffffff;
}
QPushButton:disabled {
    background-color: #161616;
    color: #4a4a4a;
    border-color: #262626;
}

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
_SS_MUTED_CLEAR  = "color: #888888; border: none; background: transparent;"
_SS_FAINT_CLEAR  = "color: #444444; border: none; background: transparent;"
_SS_SOFT_CLEAR   = "color: #cccccc; border: none; background: transparent;"
_SS_BG_DARK      = "background-color: #0a0a0a;"

#: Feuilles de style répétées, nommées une fois. Le nom dit l'INTENTION :
#: « le vert de marque sur fond transparent » se relit, « la chaîne numéro 3 »
#: non — et une couleur changée à un seul endroit ne laisse plus les autres
#: derrière elle.
#: Ni fond ni bordure : l'étiquette ne pose rien sur ce qu'il y a dessous.
_SS_NU_SANS_BORDURE = "background: transparent; border: none;"
#: Fond effacé, bordure laissée telle quelle.
_SS_NU = "background: transparent;"
#: Titre de section : vert de marque, lettres espacées.
_SS_VERT_TITRE = "color: #00ff87; letter-spacing: 2px; background: transparent;"
#: Le vert de marque, posé sur ce qu'il y a derrière.
_SS_VERT_NU = "color: #00ff87; background: transparent;"
#: Gris de second plan — un libellé qu'on lit après le reste.
_SS_GRIS_NU = "color: #555555; background: transparent;"
#: Gris le plus effacé : présent, mais qui ne réclame rien.
_SS_GRIS_EFFACE = "color: #444444; background: transparent;"
#: Blanc franc sur ce qu'il y a derrière — le texte qu'on lit en premier.
_SS_BLANC_NU = "color: #ffffff; background: transparent;"
#: Gris clair : lisible sans appeler l'œil. Plus présent que _SS_GRIS_NU.
_SS_GRIS_CLAIR_NU = "color: #888888; background: transparent;"
#: Le noir du fond de page, sous les cartes. Il apparaît aussi dans
#: _SS_SCROLL_NU, où il est écrit en toutes lettres : une feuille de style Qt
#: n'accepte pas d'interpolation, et la découper pour l'y glisser la rendrait
#: moins lisible qu'elle ne gagnerait en unicité.
_SS_FOND_PAGE = "background: #0a0a0a;"
#: Zone défilante fondue dans la page : son cadre couperait la liste, et la
#: seconde règle atteint le conteneur interne que Qt insère lui-même — sans
#: elle, une bande claire restait derrière le contenu.
_SS_SCROLL_NU = (
    "QScrollArea { background: #0a0a0a; border: none; }"
    "QScrollArea > QWidget > QWidget { background: #0a0a0a; }"
)
#: Nom d'objet des conteneurs qui ne peignent rien.
_NU = "conteneurNu"

#: Règle de fond transparent, SCOPÉE sur ce nom d'objet.
#:
#: Le sélecteur est indispensable. Une feuille de style sans sélecteur —
#: « background: transparent; border: none; » — ne s'applique pas qu'au widget
#: sur lequel on la pose : Qt la fait descendre sur TOUTE sa descendance. Posée
#: sur le conteneur d'un onglet, elle effaçait le fond et la bordure de chaque
#: carte qu'il contient, et la page se retrouvait sans aucun cadre.
_FOND_TRANSPARENT = (
    f"QWidget#{_NU}, QFrame#{_NU} {{ background: transparent; border: none; }}"
)
_FOND_TRANSPARENT_SANS_BORDURE = _FOND_TRANSPARENT
_FOND_SOMBRE = "background-color: #0f0f0f;"


def _conteneur_nu(w: "QWidget") -> "QWidget":
    """Rend `w` transparent sans toucher au style de ses enfants.

    À utiliser partout où l'on veut qu'un conteneur ne peigne rien : il pose le
    nom d'objet que la règle scopée attend, et rend le widget pour permettre
    `layout.addWidget(_conteneur_nu(QWidget()))`.
    """
    w.setObjectName(_NU)
    w.setStyleSheet(_FOND_TRANSPARENT)
    return w


def _bold_font(family: str, size: int) -> "QFont":
    """Retourne QFont(family, size) avec Bold — évite le constructeur 3-args de PyQt6."""
    f = QFont(family, size)
    f.setBold(True)
    return f


_ICON_IDLE  = "#8a8a8a"
_ICON_HOVER = "#ffffff"
_ICON_DANGER = "#ff4444"
_ICON_ON    = "#00ff87"


def _mk_header_btn(
    icon_name: str,
    fallback: str,
    tooltip: str,
    *,
    checkable: bool = False,
    danger: bool = False,
) -> "QPushButton":
    """Bouton icône 32x32 du header (paramètres, big screen, quitter)."""
    btn = QPushButton()
    btn.setFixedSize(32, 32)
    btn.setToolTip(tooltip)
    btn.setCheckable(checkable)
    hover = _ICON_DANGER if danger else _ICON_HOVER
    if _QTA_OK:
        # L'icône est un pixmap : la couleur des feuilles de style ne s'applique
        # qu'au texte et la laisserait grise en survol comme à l'état coché.
        # color_active couvre le survol, le branchement sur toggled l'état coché.
        off = qta.icon(icon_name, color=_ICON_IDLE, color_active=hover)
        btn.setIcon(off)
        # 18px dans un bouton de 32 : l'icône respire au lieu d'en toucher les bords.
        btn.setIconSize(QSize(18, 18))
        if checkable:
            on = qta.icon(icon_name, color=_ICON_ON, color_active=_ICON_ON)
            btn.toggled.connect(
                lambda checked, b=btn, on=on, off=off: b.setIcon(on if checked else off)
            )
    else:
        btn.setText(fallback)
    btn.setStyleSheet(
        f"QPushButton {{ background: transparent; color: {_ICON_IDLE}; "
        f"border: none; border-radius: 4px; font-size: 16px; }}"
        f"QPushButton:hover {{ color: {hover}; "
        f"background: {'#2a1a1a' if danger else '#1a1a1a'}; }}"
        f"QPushButton:checked {{ color: {_ICON_ON}; }}"
    )
    btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    return btn

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


from core import favorites
from widgets.megaphone import Megaphone
from core.ui_theme import MENU_QSS
from core.win_foreground import ceder_premier_plan, remonter_navigateur
from core.win_fullscreen import mark_fullscreen
from widgets.command_palette import CommandPalette as _CommandPalette
from core.paths import CONFIG_PATH as _CFG_PATH
from core.version import (
    GITHUB_OWNER as _GH_OWNER,
    GITHUB_REPO as _GH_REPO,
    display_version as _display_version,
)


def _sep_vertical() -> QFrame:
    """Un filet vertical d'un pixel, de la teinte des autres séparateurs.

    Deux colonnes de même fond posées côte à côte se lisent comme un seul
    bloc : l'espace seul ne dit pas où l'une finit.
    """
    trait = QFrame()
    trait.setFrameShape(QFrame.Shape.VLine)
    trait.setFixedWidth(1)
    trait.setStyleSheet("border: none; background: #222222;")
    return trait


def _arrondi_ou_rien(serie: list) -> list:
    """Arrondit une série de comparaison, en gardant ses trous.

    `None` traverse jusqu'au JSON en `null`, que Chart.js comprend comme une
    interruption : la courbe de référence s'arrête là où l'édition passée ne
    couvre plus, au lieu de retomber à zéro et de dessiner une falaise.

    Une série entièrement vide rend une liste vide, ce qui masque la courbe et
    son entrée de légende — plutôt qu'une ligne invisible qu'on chercherait.
    """
    if not serie or all(v is None for v in serie):
        return []
    return [None if v is None else round(v) for v in serie]


def _references(series: dict) -> dict:
    """Arrondit les courbes de comparaison, en écartant celles qui sont vides.

    Une édition dont l'alignement ne rend que des trous n'a rien à montrer sur
    la fenêtre affichée : la garder ajouterait une entrée de légende pour une
    courbe absente du graphe.
    """
    gardees = {}
    for libelle, valeurs in (series or {}).items():
        arrondi = _arrondi_ou_rien(valeurs)
        if arrondi:
            gardees[libelle] = arrondi
    return gardees


def cle_evenement(ev) -> str:
    """Identifiant d'un show pour les rappels.

    Fonction de MODULE : l'onglet Programme pose les abonnements, la timeline
    de l'Accueil les propose aussi. Deux calculs de clé qui divergeraient d'un
    caractère feraient deux abonnements distincts pour un même show.

    L'identifiant de l'API quand il existe ; sinon jour + heure + nom, qui ne
    bougent pas non plus d'un sondage à l'autre.
    """
    ident = getattr(ev, "id", "")
    if ident:
        return str(ident)
    return f"{ev.day}_{ev.start_local}_{ev.name}"


def _load_reminders() -> set[str]:
    """Clés d'événements dont le rappel est actif, depuis config.json."""
    try:
        if not _CFG_PATH.exists():
            return set()
        raw = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
        return {str(k) for k in (raw.get("programme_reminders") or [])}
    except Exception as exc:
        logger.warning("Rappels illisibles — %s", exc)
        return set()


def _save_reminders(keys: set[str]) -> None:
    """Écrit les rappels dans config.json (lecture-modification-écriture)."""
    try:
        cfg = {}
        if _CFG_PATH.exists():
            cfg = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
        cfg["programme_reminders"] = sorted(keys)
        tmp = _CFG_PATH.with_name(f"{_CFG_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, _CFG_PATH)
        os.chmod(_CFG_PATH, 0o600)
    except Exception:
        # `exception` et non `error` : la pile dit QUELLE écriture a échoué —
        # le fichier temporaire, le remplacement ou le changement de droits —
        # là où le message seul les confond toutes les trois.
        logger.exception("Sauvegarde des rappels impossible")


def _clear_layout(layout) -> None:  # type: ignore[type-arg]
    """Supprime tous les widgets d'un layout.

    Trois gestes, et l'ordre compte :

    - `hide()` D'ABORD. Un widget qu'on détache devient une fenêtre de premier
      niveau, et un widget de premier niveau VISIBLE est une fenêtre à l'écran.
      Sans ce masquage, reconstruire une liste de soixante lignes en faisait
      surgir soixante — mesuré à +124 fenêtres par rafraîchissement sur
      l'onglet Goals, toutes les trois secondes, jusqu'à faire ramer la
      machine ;
    - `setParent(None)` ensuite : `deleteLater()` ne fait que PROGRAMMER la
      destruction. Retiré du layout mais toujours enfant de son parent, le
      widget continuait de se peindre à sa dernière position — d'où des textes
      fantômes superposés au contenu reconstruit ;
    - `deleteLater()` enfin, pour que Qt le libère à son prochain tour.

    Les sous-layouts sont vidés récursivement : un layout imbriqué n'est pas un
    widget, `takeAt` le rend tel quel et ses widgets survivaient au nettoyage.
    """
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
            continue
        sous_layout = item.layout()
        if sous_layout is not None:
            _clear_layout(sous_layout)
            sous_layout.deleteLater()



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


# ── Bandeau d'actualité ──────────────────────────────────────────────────────

class _AccueilBanner(QWidget):
    """Bande supérieure : ce qui vient de se passer, en une phrase.

    Volontairement PAS des chiffres — la cagnotte, les viewers et le nombre de
    directs ont déjà leurs tuiles juste en dessous, les répéter ici ne servait
    à rien. Cette bande porte l'actualité : un objectif atteint, un favori qui
    lance son direct, un ajout au programme, le prochain show.

    Les annonces arrivent en tête et sont montrées tout de suite ; entre deux,
    la bande fait tourner ce qui reste d'actuel.
    """

    _H = 36
    _ROTATE_MS = 7_000       # rotation entre deux messages
    _FADE_MS = 260
    _MAX_KEPT = 6            # annonces gardées dans la rotation

    #: Teintes par nature d'annonce, alignées sur le fil d'événements.
    _COLORS = {
        "goal":  "#00ff87",
        "live":  "#38bdf8",
        "event": "#a855f7",
        "next":  "#8a8a8a",
        "idle":  "#6a6a6a",
        "money": "#f5c518",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self.setStyleSheet(
            "background-color: #0d0d0d; border-bottom: 1px solid #1e1e1e;")

        self._label = QLabel()
        # Texte brut : ces phrases contiennent des noms venus d'APIs tierces.
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setFont(QFont(_FONT_MONO, 12))
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 0, 12, 0)
        root.addWidget(self._label)

        self._opacity = QGraphicsOpacityEffect(self._label)
        self._opacity.setOpacity(1.0)
        self._label.setGraphicsEffect(self._opacity)
        self._anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.finished.connect(self._on_anim_finished)
        self._fading_out = False

        # (nature, texte) — la première est celle qui s'affiche.
        self._items: list[tuple[str, str]] = []
        # Messages de fond, un par sujet. Une clé remplace le message précédent
        # du même sujet : sans cela le bandeau accumulerait dix versions du
        # même « à suivre » au fil des rafraîchissements.
        self._ambient: dict[str, tuple[str, str]] = {}
        self._index = 0
        self._apply(("idle", "ZEvent 2026 — en attente des premières données"))

        self._timer = QTimer(self)
        self._timer.setInterval(self._ROTATE_MS)
        self._timer.timeout.connect(self._rotate)

    # -- cycle de vie ----------------------------------------------------

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # Ne pas animer une bande invisible : l'onglet peut être en arrière-plan.
        if len(self._items) > 1:
            self._timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._timer.stop()
        super().hideEvent(event)

    # -- API -------------------------------------------------------------

    def push(self, kind: str, text: str) -> None:
        """Annonce un événement : affiché immédiatement, puis mis en rotation."""
        text = (text or "").strip()
        if not text:
            return
        entry = (kind if kind in self._COLORS else "event", text)
        if entry in self._items:
            self._items.remove(entry)
        self._items.insert(0, entry)
        del self._items[self._MAX_KEPT:]
        self._index = 0
        self._apply(entry)
        self._restart_timer()

    #: Ordre d'apparition des messages de fond dans la rotation.
    _AMBIENT_ORDER = ("now", "next", "goal", "favs", "count", "info")

    def set_ambient(self, key: str, kind: str, text: str) -> None:
        """Pose ou retire un message de fond. Texte vide = retrait."""
        text = (text or "").strip()
        current = self._ambient.get(key)
        if not text:
            if current is None:
                return
            self._ambient.pop(key, None)
        else:
            entry = (kind if kind in self._COLORS else "next", text)
            if entry == current:
                return
            self._ambient[key] = entry
        if not self._items:
            pool = self._pool()
            self._apply(pool[min(self._index, len(pool) - 1)])
        self._restart_timer()

    def set_next_show(self, name: str, when: str) -> None:
        """Prochain show au programme."""
        self.set_ambient("next", "next",
                         f"À suivre : {name} à {when}" if name and when else "")

    def set_context(self, context: dict) -> None:
        """Conservé pour l'appelant : cette bande n'affiche pas de chiffres."""

    def trigger_refresh(self) -> None:
        return

    # -- interne ---------------------------------------------------------

    def _pool(self) -> list[tuple[str, str]]:
        pool = list(self._items)
        for key in self._AMBIENT_ORDER:
            entry = self._ambient.get(key)
            if entry is not None:
                pool.append(entry)
        return pool or [("idle", "ZEvent 2026 — du 3 au 7 septembre")]

    def _restart_timer(self) -> None:
        if len(self._pool()) > 1 and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def _apply(self, entry: tuple[str, str]) -> None:
        kind, text = entry
        self._label.setStyleSheet(
            f"color: {self._COLORS.get(kind, '#cccccc')}; background: transparent;"
            " border: none;"
        )
        fm = QFontMetrics(self._label.font())
        self._label.setText(
            fm.elidedText(text, Qt.TextElideMode.ElideRight,
                          max(120, self.width() - 32)))
        self._label.setToolTip(_infobulle(text))

    def _rotate(self) -> None:
        if self._anim.state() == QPropertyAnimation.State.Running:
            return
        self._fading_out = True
        self._anim.stop()
        self._anim.setDuration(self._FADE_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if not self._fading_out:
            return
        self._fading_out = False
        pool = self._pool()
        self._index = (self._index + 1) % len(pool)
        self._apply(pool[self._index])
        self._anim.setDuration(self._FADE_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        pool = self._pool()
        if pool:
            self._apply(pool[min(self._index, len(pool) - 1)])


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
        self._name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._name_lbl.setFont(_bold_font(_FONT_SEGOE, 13))
        self._name_lbl.setStyleSheet(_SS_WHITE_CLEAR)
        name_col.addWidget(self._name_lbl)
        self._game_lbl = QLabel("")
        self._game_lbl.setTextFormat(Qt.TextFormat.PlainText)
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
        self._donation_lbl.setStyleSheet(_SS_MUTED_CLEAR)
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
    #: Couloirs d'événements superposables. Au-delà de deux, la bande de
    #: cinquante pixels ne laisse plus de quoi lire un titre.
    _MAX_COULOIRS = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._events: list[EventItem] = []
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.update)
        # Démarré par showEvent : inutile de tourner tant que le
        # widget n'est pas affiché.

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._timer.stop()

    def set_events(self, events: list[EventItem]) -> None:
        self._events = events
        self.update()

    def _layout_events(self, now: float, cx: int, w: int, px_per_sec: float):
        """Place les événements en couloirs et renvoie [(ev, x, largeur, y, hauteur)].

        Tous les événements étaient dessinés dans la MÊME bande verticale :
        deux qui se chevauchent se recouvraient, et le clic renvoyait le
        premier de la liste alors que le dernier dessiné était celui qu'on
        voyait. On les répartit donc en couloirs, et le clic teste aussi la
        hauteur — ce qu'on voit est ce qu'on ouvre.
        """
        items = self._evenements_visibles(now, cx, w, px_per_sec)
        n_lanes = self._repartir_en_couloirs(items)

        band = self._BASELINE_Y - self._CARD_TOP - 4
        gap = 2 if n_lanes > 1 else 0
        lane_h = max(12, (band - gap * (n_lanes - 1)) // n_lanes)
        return [
            (ev, x, ew, self._CARD_TOP + lane * (lane_h + gap), lane_h)
            for ev, _s, _e, x, ew, lane in items
        ]

    def _evenements_visibles(self, now: float, cx: int, w: int,
                             px_per_sec: float) -> list[list]:
        """Les événements qui touchent la bande affichée, mesurés en pixels.

        Rend des listes MUTABLES [ev, début, fin, x, largeur, couloir] : le
        couloir est décidé juste après, sur ces mêmes entrées.
        """
        items: list[list] = []
        for ev in self._events:
            s_ts = ev.start_ts if ev.start_ts else self._parse_ts(ev.day, ev.start_local)
            e_ts = ev.end_ts   if ev.end_ts   else self._parse_ts(ev.day, ev.end_local)
            if s_ts is None or e_ts is None:
                continue
            x = cx + int((s_ts - now) * px_per_sec)
            ew = max(int((e_ts - s_ts) * px_per_sec), 60)
            if x > w or x + ew < 0:
                continue
            items.append([ev, s_ts, e_ts, x, ew, 0])
        return items

    @staticmethod
    def _est_favori(ev: "EventItem", favoris: set[str]) -> bool:
        """Un favori participe-t-il à cet événement ?

        `logins` couvre hôtes ET invités : un show où un favori est convié
        compte autant qu'un show qu'il anime.
        """
        if not favoris:
            return False
        return any((lg or "").lower() in favoris
                   for lg in (ev.logins or {}).values())

    @classmethod
    def _repartir_en_couloirs(cls, items: list[list]) -> int:
        """Affecte un couloir à chaque événement. Rend le nombre de couloirs.

        Premier couloir libre : deux événements qui se chevauchent finissent
        l'un sous l'autre plutôt que l'un SUR l'autre. Les entrées sont
        modifiées sur place.

        DEUX couloirs au plus. Il n'y avait pas de plafond, et une soirée
        chargée en empilait quatre : la bande fait cinquante pixels de haut,
        chaque couloir tombait à douze, et plus rien n'était lisible — quatre
        filets de texte tronqué. Mieux vaut deux événements qu'on lit que
        quatre qu'on devine.

        Le tri décide donc de ce qui reste, et il place les FAVORIS devant :
        quand deux shows se chevauchent, celui où joue quelqu'un qu'on suit
        prend le couloir, et l'autre est écarté plutôt que rétréci. À égalité
        de faveur, le plus proche dans le temps l'emporte.
        """
        favoris = favorites.get()
        items.sort(key=lambda it: (not cls._est_favori(it[0], favoris), it[1]))
        fins_de_couloir: list[int] = []
        gardes: list[list] = []
        for it in items:
            debut, largeur = it[3], it[4]
            for i, fin_x in enumerate(fins_de_couloir):
                if debut >= fin_x:
                    fins_de_couloir[i] = debut + largeur
                    it[5] = i
                    break
            else:
                if len(fins_de_couloir) >= cls._MAX_COULOIRS:
                    continue          # plus de place : cet événement saute
                fins_de_couloir.append(debut + largeur)
                it[5] = len(fins_de_couloir) - 1
            gardes.append(it)
        # `items` est la liste que l'appelant dessine : elle ne doit contenir
        # que ce qui a trouvé un couloir, sinon les écartés seraient tracés
        # par-dessus le couloir 0.
        items[:] = gardes
        return max(1, len(fins_de_couloir))

    def _hit_event(self, mouse_x: int) -> "EventItem | None":
        import time as _t
        now = _t.time()
        w = self.width()
        px_per_sec = w / (8 * 3600)
        cx = w // 2
        for ev, x, ew, _y, _h in self._layout_events(now, cx, w, px_per_sec):
            if x <= mouse_x <= x + ew:
                return ev
        return None

    def _hit_event_at(self, mouse_x: int, mouse_y: int) -> "EventItem | None":
        """Comme _hit_event mais en tenant compte du couloir."""
        import time as _t
        now = _t.time()
        w = self.width()
        px_per_sec = w / (8 * 3600)
        cx = w // 2
        for ev, x, ew, y, h in self._layout_events(now, cx, w, px_per_sec):
            if x <= mouse_x <= x + ew and y <= mouse_y <= y + h:
                return ev
        return None

    def mouseMoveEvent(self, _event: QMouseEvent) -> None:  # type: ignore[override]
        ev = self._hit_event_at(_event.pos().x(), _event.pos().y())
        self.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor) if ev
            else QCursor(Qt.CursorShape.ArrowCursor)
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        ev = self._hit_event_at(event.pos().x(), event.pos().y())
        if ev is not None:
            self.event_clicked.emit(ev)

    #: Demi-largeur de la bande affichée, en secondes. La frise couvre huit
    #: heures, quatre de part et d'autre du repère.
    _DEMI_BANDE = 4 * 3600

    @classmethod
    def _heures_pleines(cls, now: float) -> list[int]:
        """Horodatages des heures pleines visibles dans la bande.

        De VRAIES heures pleines, et non des multiples d'une heure comptés
        depuis maintenant. C'est toute la différence : comptées depuis
        maintenant, l'étiquette sous le repère affichait éternellement l'heure
        courante sans jamais bouger, et « 20h » se tenait une heure pleine à sa
        droite alors que 20h00 n'était plus qu'à vingt minutes. Les cartes,
        elles, sont posées à leur heure vraie — les deux se contredisaient à
        l'écran, la carte « 20h00 » commençant loin à gauche du trait « 20h ».

        L'arrondi porte sur l'horodatage Unix : l'affichage est en UTC+2, un
        nombre ENTIER d'heures, donc une heure pleine ici en est une là aussi.
        """
        premiere = int((now - cls._DEMI_BANDE) // 3600 + 1) * 3600
        return list(range(premiere, int(now + cls._DEMI_BANDE) + 1, 3600))

    def _draw_tick_marks(
        self, p: QPainter, cx: int, w: int, now: float, px_per_sec: float
    ) -> None:
        for instant in self._heures_pleines(now):
            x = cx + int((instant - now) * px_per_sec)
            if not 0 <= x <= w:
                continue
            # petit tick
            p.setPen(QPen(QColor("#2a2a2a"), 1))
            p.drawLine(x, self._BASELINE_Y - 4, x, self._BASELINE_Y + 4)
            dt = datetime.fromtimestamp(instant, tz=timezone.utc) + timedelta(hours=2)
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
        for ev, start_x, ev_w, haut, hauteur in self._layout_events(
                now, cx, w, px_per_sec):
            start_ts = ev.start_ts if ev.start_ts else self._parse_ts(ev.day, ev.start_local)
            end_ts   = ev.end_ts   if ev.end_ts   else self._parse_ts(ev.day, ev.end_local)
            if start_ts is None or end_ts is None:
                continue

            palette = self._palette_de_carte(ev, start_ts, end_ts, now)
            self._peindre_cadre(p, start_x, ev_w, haut, hauteur, palette)
            self._peindre_texte(p, ev, start_x, ev_w, haut, hauteur, palette)
            self._peindre_pastille_en_cours(p, start_x, ev_w, haut, hauteur,
                                            palette)

    #: Rayon des coins d'une carte d'événement.
    _RAYON_CARTE = 4

    def _palette_de_carte(self, ev: "EventItem", start_ts: float,
                          end_ts: float, now: float) -> dict:
        """Couleurs et états d'une carte, selon qu'elle est passée, en cours,
        ou à venir. Tout le reste du dessin ne fait que les appliquer."""
        is_past = end_ts < now
        is_current = start_ts <= now <= end_ts
        color_hex = self._event_color(ev.name or "")
        commun = {"is_past": is_past, "is_current": is_current,
                  "color": QColor(color_hex)}
        if is_past:
            # passé : fond très sombre, texte grisé
            return {**commun, "bg": QColor("#111111"),
                    "border": QColor("#2a2a2a"), "text": QColor("#555555"),
                    "time": QColor("#3a3a3a"), "accent": 0}
        if is_current:
            # en cours : fond coloré semi-transparent + bordure vive
            return {**commun, "bg": QColor(color_hex + "28"),
                    "border": QColor(color_hex), "text": QColor("#ffffff"),
                    "time": QColor(color_hex), "accent": 255}
        # futur : fond légèrement visible, bordure colorée
        return {**commun, "bg": QColor("#181818"),
                "border": QColor(color_hex + "bb"), "text": QColor("#dddddd"),
                "time": QColor(color_hex), "accent": 200}

    def _peindre_cadre(self, p: QPainter, start_x: int, ev_w: int, haut: int,
                       hauteur: int, palette: dict) -> None:
        """Fond arrondi, bordure, et bande colorée à gauche."""
        rayon = self._RAYON_CARTE
        card_rect = QRectF(start_x + 1, haut, ev_w - 2, hauteur)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(palette["bg"]))
        p.drawRoundedRect(card_rect, rayon, rayon)

        p.setPen(QPen(palette["border"], 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card_rect.adjusted(0.5, 0.5, -0.5, -0.5),
                          rayon, rayon)

        # L'accent ne se pose pas sur une carte passée, ni sur une trop étroite
        # pour qu'il reste de la place au texte.
        if palette["accent"] > 0 and ev_w > 8:
            accent = QColor(palette["color"])
            accent.setAlpha(palette["accent"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(accent))
            p.drawRoundedRect(QRectF(start_x + 1, haut, 3, float(hauteur)), 2, 2)

    def _peindre_texte(self, p: QPainter, ev: "EventItem", start_x: int,
                       ev_w: int, haut: int, hauteur: int, palette: dict) -> None:
        """Heure et nom, ou le nom seul quand la carte est trop petite.

        L'origine du texte est bornée à gauche : sans ça, une carte qui déborde
        du bord gauche (start_x négatif) écrivait hors du widget.
        """
        text_x = max(start_x + 7, 4)
        visible_w = start_x + ev_w - text_x - 6
        inner = QRectF(text_x, haut, max(visible_w, 0), hauteur)

        if not palette["is_past"] and visible_w > 50 and hauteur >= 28:
            p.setFont(QFont(_FONT_SEGOE, 8))
            p.setPen(QPen(palette["time"]))
            p.drawText(inner.adjusted(0, 1, 0, 0).toRect(),
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                       _fmt_time_fr(ev.start_local) if ev.start_local else "")
            police = QFont(_FONT_SEGOE, 9)
            police.setBold(palette["is_current"])
            p.setFont(police)
            p.setPen(QPen(palette["text"]))
            p.drawText(inner.adjusted(0, 13, 0, 0).toRect(),
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                       self._nom_elide(p, ev, visible_w))
        elif visible_w > 30:
            p.setFont(QFont(_FONT_SEGOE, 9))
            p.setPen(QPen(palette["text"]))
            p.drawText(inner.toRect(),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self._nom_elide(p, ev, visible_w))

    @staticmethod
    def _nom_elide(p: QPainter, ev: "EventItem", largeur: float) -> str:
        """Nom coupé à la largeur disponible, avec la police EN COURS."""
        return QFontMetrics(p.font()).elidedText(
            ev.name or "", Qt.TextElideMode.ElideRight, int(largeur))

    @staticmethod
    def _peindre_pastille_en_cours(p: QPainter, start_x: int, ev_w: int,
                                   haut: int, hauteur: int, palette: dict) -> None:
        """Point coloré à droite : ce qui se passe MAINTENANT se repère seul."""
        if not palette["is_current"]:
            return
        dot_x = start_x + ev_w - 10
        dot_y = haut + hauteur // 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(palette["color"]))
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
    """Bande 36 px défilante — peinture directe des seuls éléments visibles.

    L'implémentation précédente était un QLabel en texte riche de 90 000 px de
    large contenant une balise <img> par streamer, défilé par une QScrollArea.
    Chaque tick repassait par le QTextDocument (13,9 ms par image, ~46 % d'un
    cœur en continu), et reconstruire le HTML rechargeait les images depuis le
    disque — 539 ms de gel au premier rafraîchissement.

    Ici on ne dessine que ce qui tient à l'écran, avec des pixmaps déjà en
    mémoire : une poignée d'éléments au lieu de trois cents.
    """

    _H = 36
    _AV = 22          # diamètre des avatars
    _GAP = 10         # espace avatar ↔ texte
    _SEP_W = 34       # largeur du séparateur entre deux entrées
    _SPEED = 60.0     # pixels par seconde
    _TICK_MS = 33     # ~30 fps

    _C_LAN = QColor("#00ff87")
    _C_REMOTE = QColor("#38bdf8")
    _C_GAME = QColor("#888888")
    _C_SEP = QColor("#444444")
    _BG = QColor("#0d0d0d")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        # Polices et métriques construites UNE fois : les recréer par image
        # coûtait plus cher que le dessin lui-même.
        self._f_name = _bold_font(_FONT_MONO, 11)
        self._f_game = QFont(_FONT_MONO, 11)
        self._fm_name = QFontMetrics(self._f_name)
        self._fm_game = QFontMetrics(self._f_game)

        # [(login, nom, couleur_nom, jeu, largeur_totale, url_photo)]
        self._items: list[tuple[str, str, QColor, str, int, str]] = []
        self._total_w: int = 0
        self._offset: float = 0.0
        self._idle_text: str = _WAIT_MSG

        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)
        # Démarré par showEvent.

    # -- cycle de vie ---------------------------------------------------------

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._timer.stop()

    # -- données --------------------------------------------------------------

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        live = [s for s in streamers if s.online]
        # Rien de visible ne change tant que ce quadruplet est identique :
        # recomposer la liste coûtait ~100 ms sur le thread GUI à chaque poll.
        sig = tuple((s.twitch_login, s.display, s.game, s.location) for s in live)
        if sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        items: list[tuple[str, str, QColor, str, int, str]] = []
        total = 0
        for s in live:
            lan = (s.location or "").upper() == "LAN"
            colour = self._C_LAN if lan else self._C_REMOTE
            # On garde l'IDENTITÉ du streamer, PAS son pixmap. Le capturer ici
            # figeait ce que le cache avait à cet instant — les initiales, tant
            # que la photo n'était pas téléchargée — et le garde `sig` ci-dessus
            # empêchait ensuite toute reconstruction : les initiales restaient
            # affichées indéfiniment. La photo est donc relue à la peinture.
            game = s.game or ""
            w = (self._AV + self._GAP
                 + self._fm_name.horizontalAdvance(s.display))
            if game:
                w += self._GAP + self._fm_game.horizontalAdvance(game)
            w += self._SEP_W
            items.append((s.twitch_login, s.display, colour, game, w,
                          getattr(s, "profile_url", "")))
            total += w
        self._items = items
        self._total_w = total
        if total and self._offset >= total:
            self._offset = 0.0
        self.update()

    # -- animation ------------------------------------------------------------

    def _tick(self) -> None:
        if not self._total_w:
            return
        prev = int(self._offset)
        self._offset = (self._offset
                        + self._SPEED * self._TICK_MS / 1000.0) % self._total_w
        # Inutile de repeindre si le décalage entier n'a pas bougé.
        if int(self._offset) != prev:
            self.update()

    # -- peinture -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.fillRect(self.rect(), self._BG)
        w = self.width()

        if not self._items:
            p.setFont(self._f_game)
            p.setPen(QPen(self._C_GAME))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._idle_text)
            p.end()
            return

        # On part de l'élément qui couvre le bord gauche, et on ne dessine que
        # jusqu'au bord droit : une poignée d'entrées, pas les trois cents.
        start = -int(self._offset) % self._total_w - self._total_w
        x = start
        n = len(self._items)
        i = 0
        guard = 0
        while x < w and guard < n * 2 + 4:
            login, name, colour, game, item_w, purl = self._items[i % n]
            if x + item_w > 0:
                self._draw_item(p, x, login, name, colour, game, purl)
            x += item_w
            i += 1
            guard += 1
        p.end()

    def _draw_item(self, p: QPainter, x: int, login: str,
                   name: str, colour: QColor, game: str,
                   profile_url: str = "") -> None:
        y = (self._H - self._AV) // 2
        # Lecture au moment de peindre : simple accès au dictionnaire du cache
        # mémoire quand la photo est là, et l'affichage se corrige tout seul dès
        # qu'elle arrive — le bandeau défile, donc il repeint de toute façon.
        from widgets.bigscreen_widget import _avatar_cache
        av = _avatar_cache.get(login, name, self._AV, None, profile_url)
        if av is not None:
            p.drawPixmap(x, y, av)  # type: ignore[arg-type]
        tx = x + self._AV + self._GAP
        p.setFont(self._f_name)
        p.setPen(QPen(colour))
        p.drawText(tx, 0, self._fm_name.horizontalAdvance(name), self._H,
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   name)
        if game:
            tx += self._fm_name.horizontalAdvance(name) + self._GAP
            p.setFont(self._f_game)
            p.setPen(QPen(self._C_GAME))
            p.drawText(tx, 0, self._fm_game.horizontalAdvance(game), self._H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       game)
            tx += self._fm_game.horizontalAdvance(game)
        p.setPen(QPen(self._C_SEP))
        p.drawText(tx, 0, self._SEP_W, self._H,
                   int(Qt.AlignmentFlag.AlignCenter), "·")


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
        self._name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._name_lbl.setFont(QFont(_FONT_SEGOE, 12))
        self._name_lbl.setStyleSheet("color: #aaaaaa; background: transparent;")
        h.addWidget(self._name_lbl, stretch=1)

        # Viewers
        self._v_lbl = QLabel(_fmt_viewers(s.viewers))
        self._v_lbl.setFont(_bold_font(_FONT_SEGOE, 12))
        self._v_lbl.setStyleSheet(_SS_VERT_NU)
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
        title.setStyleSheet(_SS_VERT_TITRE)
        hdr.addWidget(title)
        self._count_lbl = QLabel("")
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet(_SS_GRIS_NU)
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._count_lbl, stretch=1)
        root.addLayout(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            _SS_SCROLL_NU
        )
        content = QWidget()
        content.setStyleSheet(_SS_FOND_PAGE)
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

        # Structure changée : on ne touche qu'aux items concernés. Reconstruire
        # les 220 items (environ 1 100 widgets, ~245 ms mesurées) parce qu'un
        # seul streamer arrive était le même travers que dans l'onglet
        # Streamers.
        self._prev_live = live_logins
        self._retirer_les_partis(live_logins)
        self._retirer_stretch_final()
        self._reordonner(live)
        self._list_layout.addStretch()

    def _retirer_les_partis(self, encore_en_ligne: frozenset) -> None:
        """Détruit les items des chaînes qui ne sont plus en direct."""
        for lg in [lg for lg in self._item_map if lg not in encore_en_ligne]:
            item = self._item_map.pop(lg)
            self._list_layout.removeWidget(item)
            item.hide()   # avant de détacher : détaché et visible = une fenêtre
            item.setParent(None)
            item.deleteLater()

    def _retirer_stretch_final(self) -> None:
        """Retire le ressort de queue : il doit le rester après insertion."""
        while self._list_layout.count():
            dernier = self._list_layout.itemAt(self._list_layout.count() - 1)
            if dernier is not None and dernier.widget() is None:
                self._list_layout.takeAt(self._list_layout.count() - 1)
            else:
                break

    def _reordonner(self, live: list) -> None:
        """Replace les items dans l'ordre des viewers, en réutilisant l'existant."""
        for idx, s in enumerate(live):
            item = self._item_map.get(s.twitch_login)
            if item is None:
                item = _AccueilStreamerItem(s)
                self._item_map[s.twitch_login] = item
            else:
                item.patch(s)
            # insertWidget déplace sans détruire : l'ordre suit les viewers.
            self._list_layout.insertWidget(idx, item)


# ── Accueil — goals proches d'atteinte ───────────────────────────────────────

def _distance_objectif(g: GoalWithStreamer) -> str:
    """« plus que 40 € · 96% », ou le seul pourcentage quand il ne manque rien.

    L'ordre compte : le montant d'abord, le pourcentage ensuite. C'est le
    montant qu'on lit en diagonale et sur lequel on peut agir ; le pourcentage
    ne fait que redire ce que la barre montre déjà.
    """
    if g.reste <= 0:
        return f"{g.pourcent_affiche}%"
    return f"plus que {_fmt_euros(math.ceil(g.reste))}  ·  {g.pourcent_affiche}%"


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
        # L'item fait 52 px et ne peut pas grandir : la coupe est inévitable.
        # Ce qui ne l'est pas, c'est de la subir sans le savoir — les points de
        # suspension la signalent, l'infobulle rend le texte entier.
        streamer_lbl = QLabel(_couper_avec_points(g.streamer_display, 16))
        streamer_lbl.setTextFormat(Qt.TextFormat.PlainText)
        streamer_lbl.setFont(_bold_font(_FONT_SEGOE, 11))
        streamer_lbl.setStyleSheet(_SS_BLANC_NU)
        if streamer_lbl.text() != g.streamer_display:
            streamer_lbl.setToolTip(_infobulle(g.streamer_display))
        row1.addWidget(streamer_lbl)
        goal_lbl = QLabel(_couper_avec_points(g.goal_name, 30))
        goal_lbl.setTextFormat(Qt.TextFormat.PlainText)
        goal_lbl.setFont(QFont(_FONT_SEGOE, 11))
        goal_lbl.setStyleSheet(_SS_GRIS_CLAIR_NU)
        if goal_lbl.text() != g.goal_name:
            goal_lbl.setToolTip(_infobulle(g.goal_name))
        row1.addWidget(goal_lbl, stretch=1)
        # Le pourcentage seul ne dit pas s'il faut dix euros ou mille : ces
        # objectifs-ci sont tous entre 90 et 100 %, et s'affichaient donc
        # « 100% » les uns sous les autres, indiscernables. Ce qui les sépare,
        # et ce sur quoi on peut agir, c'est ce qu'il reste à réunir.
        pct_lbl = QLabel(_distance_objectif(g))
        pct_lbl.setFont(QFont(_FONT_SEGOE, 11))
        pct_lbl.setStyleSheet(_SS_VERT_NU)
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
    """Colonne droite — les objectifs les plus proches d'être atteints."""

    _MAX_SHOWN = 12

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
        title = QLabel("PROCHAINS OBJECTIFS")
        title.setFont(_bold_font(_FONT_SEGOE, 10))
        title.setStyleSheet(_SS_VERT_TITRE)
        hdr.addWidget(title)
        self._count_lbl = QLabel("")
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet(_SS_GRIS_NU)
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._count_lbl, stretch=1)
        root.addLayout(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            _SS_SCROLL_NU
        )
        self._content = QWidget()
        self._content.setStyleSheet(_SS_FOND_PAGE)
        self._goals_layout = QVBoxLayout(self._content)
        self._goals_layout.setContentsMargins(0, 0, 0, 0)
        self._goals_layout.setSpacing(0)
        self._goals_layout.addStretch()
        emp = QLabel("Aucun objectif publié\npour l'instant")
        emp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        emp.setFont(QFont(_FONT_SEGOE, 12))
        emp.setStyleSheet(_SS_GRIS_EFFACE)
        self._goals_layout.addWidget(emp)
        self._goals_layout.addStretch()
        scroll.setWidget(self._content)
        root.addWidget(scroll, stretch=1)

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        # Le seuil à 90 % laissait la colonne vide l'essentiel du temps, sur
        # 40 % de la largeur. On montre les plus proches quel que soit leur
        # avancement : c'est ce qu'on veut voir en direct.
        # `atteint` et non `accomplished` : un objectif dont la somme est
        # réunie n'est plus PROCHE. Trié sur -pct, il se classait premier et
        # ne bougeait plus — la première ligne affichait « 100% » en
        # permanence, sans montant restant, pendant que les vrais prochains
        # objectifs attendaient derrière.
        pending = sorted(
            [g for g in goals if not g.atteint],
            key=lambda g: -g.pct,
        )
        to_show = pending[:self._MAX_SHOWN]
        self.setVisible(bool(to_show))
        self._count_lbl.setText(
            f"{len(to_show)} sur {len(pending)}" if pending else ""
        )
        _clear_layout(self._goals_layout)
        if not to_show:
            self._goals_layout.addStretch()
            emp = QLabel("Aucun objectif publié\npour l'instant")
            emp.setAlignment(Qt.AlignmentFlag.AlignCenter)
            emp.setFont(QFont(_FONT_SEGOE, 12))
            emp.setStyleSheet(_SS_GRIS_EFFACE)
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


class _EventFeed(QWidget):
    """Fil chronologique des événements marquants.

    Les alertes HypeWatcher, les objectifs atteints et les rappels de programme
    n'existaient que sous forme de toasts : dix minutes d'absence et tout était
    perdu. Ici ils s'accumulent, horodatés et consultables.
    """

    stream_requested = pyqtSignal(str)

    _MAX_ITEMS = 60

    _KIND_COLORS = {
        "hype":  "#ff6b00",
        "goal":  "#00ff87",
        "live":  "#38bdf8",
        "off":   "#666666",
        "event": "#a855f7",
        "money": "#f5c518",   # or — palier de cagnotte
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #0a0a0a; border-left: 1px solid #1a1a1a;")
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 12, 4)
        root.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setSpacing(0)
        title = QLabel("FIL D'ÉVÉNEMENTS")
        title.setFont(_bold_font(_FONT_SEGOE, 10))
        title.setStyleSheet(_SS_VERT_TITRE)
        hdr.addWidget(title)
        self._count_lbl = QLabel("")
        self._count_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._count_lbl.setStyleSheet(_SS_GRIS_NU)
        self._count_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._count_lbl, stretch=1)
        root.addLayout(hdr)

        self._list = QListWidget()
        self._list.setFont(QFont(_FONT_SEGOE, 11))
        # Retour à la ligne plutôt qu'un défilement horizontal : la colonne est
        # étroite et l'extrait du chat, qui porte le contexte, se retrouvait
        # tronqué en plein milieu.
        self._list.setWordWrap(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; color: #cccccc; }"
            "QListWidget::item { padding: 4px 2px; }"
            "QListWidget::item:hover { background: #14251c; }"
        )
        self._list.itemActivated.connect(self._on_activate)
        self._list.itemClicked.connect(self._on_activate)
        root.addWidget(self._list, stretch=1)

        self._empty = QLabel("Rien à signaler\npour l'instant")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setFont(QFont(_FONT_SEGOE, 11))
        self._empty.setStyleSheet(_SS_GRIS_EFFACE)
        root.addWidget(self._empty, stretch=1)

    def add_event(self, kind: str, login: str, text: str) -> None:
        """Ajoute une entrée en tête de fil."""
        stamp = datetime.now().strftime("%H:%M")
        item = QListWidgetItem(f"{stamp}  {text}")
        item.setForeground(QBrush(QColor(self._KIND_COLORS.get(kind, "#cccccc"))))
        # Le login voyage avec l'item : un clic ramène au stream concerné.
        item.setData(Qt.ItemDataRole.UserRole, login)
        self._list.insertItem(0, item)
        while self._list.count() > self._MAX_ITEMS:
            self._list.takeItem(self._list.count() - 1)
        self._count_lbl.setText(str(self._list.count()))
        self._empty.setVisible(False)
        self._list.setVisible(True)

    def _on_activate(self, item: QListWidgetItem) -> None:
        login = item.data(Qt.ItemDataRole.UserRole)
        if login:
            self.stream_requested.emit(str(login))


# ── Accueil Tab ───────────────────────────────────────────────────────────

class _AccueilTab(QWidget):
    stream_selected = pyqtSignal(str)
    add_to_grid     = pyqtSignal(str)  # twitch_login
    #: (login du présentateur, nom du show) — un show vient de commencer.
    show_started    = pyqtSignal(str, str)
    #: Clé d'un show dont on demande à basculer le rappel. L'état vit dans
    #: l'onglet Programme, qui le détient et le persiste : la frise le
    #: demande, elle ne le décide pas.
    rappel_bascule  = pyqtSignal(str)

    #: Largeur maximale de la colonne « EN LIVE ». Un avatar de 28 px, un nom,
    #: un jeu et une audience : au-delà, la place ne sert qu'à écarter les noms
    #: des chiffres qui leur correspondent, et l'œil perd la ligne.
    _LARGEUR_LIVE = 580

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prev_viewers: int = 0
        self._started_shows: set[str] = set()
        self._shows_init_done = False
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

        # 1. Bandeau d'annonces — 36px fixe, pleine largeur
        self._banner = _AccueilBanner()
        # « dans 25 min » doit devenir « dans 24 min » : les messages de fond
        # sont recomposés régulièrement, indépendamment des sondages réseau.
        self._ambient_timer = QTimer(self)
        self._ambient_timer.setInterval(45_000)
        self._ambient_timer.timeout.connect(self._refresh_ambient)
        self._ambient_timer.timeout.connect(self._check_started_shows)
        self._ambient_timer.start()
        root.addWidget(self._banner)

        # 2. Cards stats — 90px fixe
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(12, 8, 12, 0)
        cards_row.setSpacing(8)
        self._card_donation, self._amt_lbl, self._rate_lbl = self._make_card_donation()
        self._card_viewers, self._viewers_lbl, self._trend_lbl = self._make_card_viewers()
        self._card_live, self._live_count_lbl = self._make_card_live()
        self._card_proj, self._proj_lbl, self._proj_sub = self._make_card_projection()
        cards_row.addWidget(self._card_donation)
        cards_row.addWidget(self._card_viewers)
        cards_row.addWidget(self._card_live)
        cards_row.addWidget(self._card_proj)
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
        # « EN LIVE » ne porte qu'un avatar, un nom, un jeu et une audience :
        # son contenu tient dans 350 px. Avec un stretch de 7 contre 3, elle
        # réclamait les deux tiers de l'onglet et laissait NEUF CENTS PIXELS de
        # vide entre les noms et la colonne des audiences, pendant que les deux
        # autres blocs s'entassaient dans un tiers.
        self._streamers_list.setMaximumWidth(self._LARGEUR_LIVE)
        central_layout.addWidget(self._streamers_list, stretch=3)

        # Objectifs et fil d'événements CÔTE À CÔTE, dans la place ainsi
        # rendue. Empilés, chacun n'avait qu'une demi-hauteur dans une colonne
        # étroite : cinq objectifs et un seul événement visibles à la fois.
        right = QWidget()
        right_l = QHBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)
        right_l.addWidget(self._goals_widget, stretch=1)
        # Le trait SEUL, sans marge de part et d'autre. Les deux blocs portent
        # déjà leurs marges internes — douze pixels à droite des objectifs,
        # huit à gauche du fil : y ajouter vingt de chaque côté portait la
        # coupure visible à soixante et un pixels, un couloir vide au milieu
        # de l'onglet. Le filet suffit à dire où l'un finit.
        self._sep_droite = _sep_vertical()
        right_l.addWidget(self._sep_droite)
        self._feed = _EventFeed()
        self._feed.stream_requested.connect(self.stream_selected.emit)
        right_l.addWidget(self._feed, stretch=1)
        central_layout.addWidget(right, stretch=7)
        # Hors event, aucun objectif n'existe : la colonne se masque d'elle-même
        # et le fil récupère toute la hauteur.
        self._goals_widget.setVisible(False)
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

    def _make_card_donation(self) -> tuple[QFrame, QLabel, QLabel]:
        card = self._base_card()
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 0, 14, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        amt = QLabel("0 €")
        amt.setFont(_bold_font(_FONT_MONO, 22))
        amt.setStyleSheet(_SS_WHITE_CLEAR)
        vl.addWidget(amt)
        row = QHBoxLayout()
        row.setSpacing(6)
        sub = QLabel("cagnotte totale")
        self._don_sub = sub
        sub.setFont(QFont(_FONT_SEGOE, 10))
        sub.setStyleSheet(_SS_GREY_CLEAR)
        row.addWidget(sub)
        # Vitesse de collecte : la question qu'on se pose en boucle pendant
        # l'event, et la série temporelle est déjà en mémoire.
        rate = QLabel("")
        rate.setFont(_bold_font(_FONT_MONO, 10))
        rate.setStyleSheet(_SS_GREEN_CLEAR)
        row.addWidget(rate)
        row.addStretch()
        vl.addLayout(row)
        return card, amt, rate

    def _make_card_projection(self) -> tuple[QFrame, QLabel, QLabel]:
        card = self._base_card()
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 0, 14, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        proj = QLabel("—")
        proj.setFont(_bold_font(_FONT_MONO, 22))
        proj.setStyleSheet("color: #38bdf8; border: none; background: transparent;")
        vl.addWidget(proj)
        # Libellé initial neutre : tant qu'aucun historique n'est arrivé,
        # annoncer « au rythme actuel » sous un tiret serait mensonger.
        sub = QLabel("en attente de données")
        sub.setFont(QFont(_FONT_SEGOE, 10))
        sub.setStyleSheet(_SS_GREY_CLEAR)
        vl.addWidget(sub)
        return card, proj, sub

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
        # « viewers » seul laissait croire à l'audience d'une seule chaîne : ce
        # compteur est la somme de TOUS les participants en direct.
        sub = QLabel("viewers au total")
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

        self._maj_carte_viewers(stats.viewers_total)

        nb_live = sum(1 for s in streamers if s.online)
        nb_total = len(streamers)
        self._live_count_lbl.setText(f"{nb_live} / {nb_total}")

        en_direct = sorted([s for s in streamers if s.online],
                           key=lambda s: -s.viewers)
        self._maj_podium(en_direct)
        self._ticker.set_streamers(en_direct[:15])
        self._streamers_list.update_streamers(streamers)

        self._banner.set_context({
            "year": 2025,
            "donation": stats.donation_formatted,
            "viewers": _fmt_viewers(stats.viewers_total),
            "live_count": nb_live,
            "total_count": nb_total,
            "_raw_donation": stats.donation_total,
        })
        self._banner.set_ambient("favs", "live", self._phrase_favoris(streamers))

        self._prev_live = {s.twitch_login for s in streamers if s.online}
        self._prev_donation = stats.donation_total
        self._initialized = True
        # Rebuild UUID map for timeline click resolution
        self._uuid_to_login = {s.gdoc_id: s.twitch_login for s in streamers if s.gdoc_id}
        self._all_logins = {s.twitch_login for s in streamers}

    def _maj_carte_viewers(self, total: int) -> None:
        """Audience du moment, avec sa flèche de tendance.

        La flèche compare au RELEVÉ PRÉCÉDENT, pas à une moyenne : ce qu'on
        veut savoir, c'est si ça monte à l'instant.
        """
        if total > self._prev_viewers:
            self._trend_lbl.setText("▲")
            self._trend_lbl.setStyleSheet(_SS_GREEN_CLEAR)
        elif total < self._prev_viewers:
            self._trend_lbl.setText("▼")
            self._trend_lbl.setStyleSheet(
                "color: #ff4444; border: none; background: transparent;")
        else:
            self._trend_lbl.setText("")
        self._viewers_lbl.setText(_fmt_viewers(total))
        self._prev_viewers = total

    def _maj_podium(self, en_direct: list) -> None:
        """Les trois plus grosses audiences. Les cartes en trop sont masquées.

        Elles ne sont pas détruites : elles resserviront au prochain
        rafraîchissement, et les recréer ferait clignoter la rangée.
        """
        top3 = en_direct[:3]
        max_v = top3[0].viewers if top3 else 1
        for i, carte in enumerate(self._player_cards):
            if i < len(top3):
                carte.set_streamer(top3[i], i + 1, max_v)
                carte.setVisible(True)
            else:
                carte.setVisible(False)

    @staticmethod
    def _phrase_favoris(streamers: list) -> str:
        """« Vos favoris en direct : … », ou rien s'il n'y en a aucun."""
        favs = favorites.get()
        noms_live = [s.display or s.twitch_login for s in streamers
                     if s.online and s.twitch_login.lower() in favs]
        if not noms_live:
            return ""
        cites = ", ".join(noms_live[:3])
        reste = len(noms_live) - 3
        if reste <= 0:
            return f"Vos favoris en direct : {cites}"
        pluriel = "s" if reste > 1 else ""
        return f"Vos favoris en direct : {cites} et {reste} autre{pluriel}"

    def _host_login(self, ev: EventItem) -> str:
        """Login du présentateur d'un show, vide si non résolvable."""
        for uid in (ev.host_uuids or []):
            if uid in self._uuid_to_login:
                return self._uuid_to_login[uid]
            if uid in self._all_logins:
                return uid
        return ""

    def _check_started_shows(self) -> None:
        """Signale les shows qui viennent de COMMENCER.

        Les rappels du Programme préviennent AVANT ; ici il s'agit de proposer
        la bascule au moment où ça démarre. Chaque show n'est proposé qu'une
        fois, et seulement s'il a démarré dans les deux dernières minutes :
        au lancement de l'application, un show en cours depuis une heure n'est
        pas une nouvelle.
        """
        now = time.time()
        for ev in self._events:
            start, _end = self._ev_bounds(ev)
            if start is None or not (0 <= now - start <= 120):
                continue
            cle = f"{ev.day}_{ev.start_local}_{ev.name}"
            if cle in self._started_shows:
                continue
            self._started_shows.add(cle)
            if not self._shows_init_done:
                continue
            login = self._host_login(ev)
            if login:
                from core import alerts
                if alerts.enabled("show_started"):
                    self.show_started.emit(login, ev.name or "Événement")
        self._shows_init_done = True

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

        menu = self.menu_d_un_show(ev, login)
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        action = str(chosen.data() or "")
        if action == "fullscreen":
            self.stream_selected.emit(login)
        elif action == "grille":
            self.add_to_grid.emit(login)
        elif action == "rappel":
            self.rappel_bascule.emit(cle_evenement(ev))

    def menu_d_un_show(self, ev: EventItem, login: str) -> QMenu:
        """Ce qu'on peut faire d'un show depuis la frise.

        Rendu plutôt qu'exécuté : un menu qui s'ouvre tout seul ne se teste
        qu'en le cliquant.
        """
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        host_act = QAction(f"{ev.name or 'Événement'}  —  {login}", menu)
        host_act.setEnabled(False)
        menu.addAction(host_act)
        menu.addSeparator()
        menu.addAction("▶  Charger en fullscreen").setData("fullscreen")
        menu.addAction("⊞  Ajouter à la grille").setData("grille")
        menu.addSeparator()
        menu.addAction(self._action_rappel(menu, ev))
        return menu

    def _action_rappel(self, menu: QMenu, ev: EventItem) -> QAction:
        """L'entrée « me rappeler », dans l'état où elle se trouve.

        Le rappel existait déjà, mais seulement sous forme de cloche dans
        l'onglet Programme : depuis la frise de l'Accueil — là où l'on
        découvre justement qu'un show approche — il fallait changer d'onglet
        et retrouver la bonne carte.

        Un show déjà commencé garde son entrée, grisée : la masquer ferait
        croire que le rappel n'existe pas pour ce show-là, alors qu'il est
        seulement trop tard.
        """
        debut, _fin = self._ev_bounds(ev)
        passe = debut is not None and debut <= time.time()
        abonne = cle_evenement(ev) in _load_reminders()
        if passe:
            action = QAction("\U0001f514  Rappel — le show a déjà commencé", menu)
            action.setEnabled(False)
            return action
        action = QAction(
            "\U0001f514  Désactiver le rappel" if abonne
            else "\U0001f514  Me rappeler 5 min avant", menu)
        action.setData("rappel")
        return action

    def update_events(self, events: list[EventItem]) -> None:
        self._events = events
        self._timeline.set_events(events)
        self._refresh_next_show()

    @staticmethod
    def _ev_bounds(ev: EventItem) -> tuple[float | None, float | None]:
        start = ev.start_ts if ev.start_ts else _AccueilTimeline._parse_ts(
            ev.day, ev.start_local)
        end = ev.end_ts if getattr(ev, "end_ts", 0) else _AccueilTimeline._parse_ts(
            ev.day, ev.end_local)
        return start, end

    @staticmethod
    def _delai(seconds: float) -> str:
        """« dans 25 min », « dans 3 h 10 », « dans 2 jours »."""
        m = max(0, int(seconds // 60))
        if m < 60:
            return f"dans {m} min" if m else "dans un instant"
        h, mm = divmod(m, 60)
        if h < 24:
            return f"dans {h} h {mm:02d}" if mm else f"dans {h} h"
        j = h // 24
        return f"dans {j} jour" + ("s" if j > 1 else "")

    def _refresh_ambient(self) -> None:
        """Recompose les messages de fond du bandeau.

        Rappelée périodiquement, pas seulement à l'arrivée de données : les
        formulations sont relatives à l'instant (« dans 25 min »), et un
        bandeau qui répète la même phrase pendant une heure ne donne plus
        l'impression de suivre quoi que ce soit.
        """
        now = time.time()
        courant, suivant, debut_suivant = self._en_cours_et_a_venir(now)

        self._banner.set_ambient("now", "event", self._phrase_en_cours(courant))
        self._banner.set_ambient(
            "next", "next", self._phrase_a_suivre(suivant, debut_suivant, now))
        self._banner.set_ambient("count", "next", self._phrase_reste_du_jour(now))
        self._banner.set_ambient("info", "idle", self._phrase_avant_le_coup_d_envoi(now))

    def _en_cours_et_a_venir(self, now: float) -> tuple:
        """(événement en cours, prochain, heure du prochain) — un seul balayage.

        Le premier en cours gagne : deux shows qui se chevauchent sont rares, et
        annoncer les deux ferait clignoter le bandeau entre eux.
        """
        courant: EventItem | None = None
        suivant: EventItem | None = None
        debut_suivant = 0.0
        for ev in self._events:
            start, end = self._ev_bounds(ev)
            if start is None:
                continue
            if end is not None and start <= now <= end:
                courant = courant or ev
            elif start > now and (suivant is None or start < debut_suivant):
                suivant, debut_suivant = ev, start
        return courant, suivant, debut_suivant

    @staticmethod
    def _phrase_en_cours(ev: "EventItem | None") -> str:
        if ev is None:
            return ""
        fin = f" jusqu'à {_fmt_time_fr(ev.end_local)}" if ev.end_local else ""
        return f"En ce moment : {ev.name or 'Événement'}{fin}"

    def _phrase_a_suivre(self, ev: "EventItem | None", debut: float,
                         now: float) -> str:
        if ev is None:
            return ""
        heure = _fmt_time_fr(ev.start_local) if ev.start_local else ""
        return (f"À suivre : {ev.name or 'Événement'} à {heure} "
                f"({self._delai(debut - now)})")

    def _phrase_reste_du_jour(self, now: float) -> str:
        """Combien de rendez-vous restent aujourd'hui — tu à partir d'un seul.

        Annoncer « 1 rendez-vous encore » alors qu'il est déjà nommé juste
        au-dessus n'apprend rien.
        """
        jour = datetime.now().strftime("%Y-%m-%d")
        reste = sum(1 for ev in self._events
                    if ev.day == jour
                    and (self._ev_bounds(ev)[0] or 0) > now)
        if reste <= 1:
            return ""
        return f"{reste} rendez-vous encore au programme aujourd'hui"

    def _phrase_avant_le_coup_d_envoi(self, now: float) -> str:
        """Avant l'ouverture, le bandeau dit dans combien de temps ça commence."""
        debut = _AccueilTimeline._parse_ts(_PROG_DAYS_ORDERED[0], "18:00")
        if not debut or now >= debut:
            return ""
        return f"Le ZEvent 2026 commence {self._delai(debut - now)}"

    def _refresh_next_show(self) -> None:
        self._refresh_ambient()

    def update_history(self, history: HistoryStore) -> None:
        ts, _ = history.get_donation_series()
        if ts:
            logger.debug(
                "AccueilTab.update_history: %d points, premier ts UTC = %s",
                len(ts),
                datetime.fromtimestamp(ts[0], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )

        # Comparaison avec l'édition précédente : la question que tout le monde
        # se pose pendant l'event, et la donnée est déjà sur le disque.
        cmp_ = history.compare_to_previous(getattr(self, "_prev_donation", 0.0))
        if cmp_ is not None:
            ref, ecart = cmp_
            signe = "+" if ecart >= 0 else ""
            self._banner.set_ambient(
                "vs", "money",
                f"À la même heure en 2025 : {ref:,.0f} €".replace(",", "\u202f")
                + f" — nous sommes {signe}{ecart:.0f} %")
            self._don_sub.setText(f"cagnotte totale · {signe}{ecart:.0f} % vs 2025")
        else:
            self._banner.set_ambient("vs", "money", "")
            self._don_sub.setText("cagnotte totale")

        rate = history.donation_rate()
        if rate is None:
            self._rate_lbl.setText("")
        else:
            self._rate_lbl.setText(f"{'+' if rate >= 0 else ''}{rate:,.0f} €/min"
                                   .replace(",", "\u202f"))

        proj = history.projected_total(history.event_end_ts)
        if proj is None:
            self._proj_lbl.setText("—")
            # Dire POURQUOI : hors event il n'y a rien à extrapoler, ce n'est
            # pas une panne.
            now = time.time()
            if now < history.event_start_ts:
                self._proj_sub.setText("disponible au début de l'event")
            elif now > history.event_end_ts:
                self._proj_sub.setText("event terminé")
            else:
                self._proj_sub.setText("en attente de données")
        else:
            self._proj_lbl.setText(f"{proj:,.0f} €".replace(",", "\u202f"))
            self._proj_sub.setText("projection au rythme actuel")

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        self._goals_widget.update_goals(goals)
        # Celui qui va tomber en premier : c'est l'information qui a une chance
        # de se vérifier dans les minutes qui suivent.
        pending = [g for g in goals if not g.accomplished and g.pct > 0]
        if pending:
            g = max(pending, key=lambda x: x.pct)
            self._banner.set_ambient(
                "goal", "goal",
                f"{g.streamer_display} est à {g.pourcent_affiche} % de son objectif "
                f"« {g.goal_name} »")
        else:
            self._banner.set_ambient("goal", "goal", "")

    def add_feed_event(self, kind: str, login: str, text: str) -> None:
        self._feed.add_event(kind, login, text)
        # Les alertes de chat se comptent par dizaines par heure : les faire
        # défiler dans le bandeau le rendrait illisible. Seuls les événements
        # rares y montent.
        if kind in ("goal", "live", "event", "money"):
            self._banner.push(kind, text)


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


# Édition 2026 : jeudi 3 → lundi 7 septembre. Ces tables étaient restées sur
# l'édition 2025, donc sur des dates qui ne correspondaient à aucun événement.
_PROG_DAY_LABELS: dict[str, str] = {
    "2026-09-03": "Jeudi 3",
    "2026-09-04": "Vendredi 4",
    "2026-09-05": "Samedi 5",
    "2026-09-06": "Dimanche 6",
    "2026-09-07": "Lundi 7",
}
_PROG_DAYS_ORDERED: list[str] = sorted(_PROG_DAY_LABELS)

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

_PERSON_AV_SZ = 24


def _avatar_cache_key(login: str, profile_url: str) -> str:
    """Clé de cache disque pour un intervenant.

    Les invités d'un show (artistes) n'ont pas de compte Twitch : leur login est
    vide et leur avatar est hébergé hors Twitch. Sans clé dérivée, tous
    partageraient le fichier "".png et afficheraient la même image — le dernier
    téléchargé écrasant les autres.

    SHA-256 comme partout ailleurs dans le projet. La clé ne protège rien — un
    nom de fichier de cache, dérivé d'une URL publique — mais un algorithme
    réputé faible dans le code invite à le recopier là où ça compterait, et ici
    il ne coûte rien de plus.
    """
    if login:
        return login
    if not profile_url:
        return ""
    return "guest_" + hashlib.sha256(
        profile_url.encode("utf-8")).hexdigest()[:16]


def _couper_avec_points(texte: str, limite: int) -> str:
    """Raccourcit à `limite` caractères, en terminant par « … ».

    Le découpage brut `texte[:limite]` tombait où il tombait, souvent en plein
    mot, et rien ne signalait qu'il manquait quelque chose : « Je repein » se
    lit comme un nom complet. Les points de suspension le disent, et l'appelant
    pose le texte entier en infobulle.

    On coupe au dernier espace disponible plutôt qu'au caractère quand il en
    reste un dans le dernier tiers : un mot entier de moins vaut mieux qu'un
    mot tronqué.
    """
    texte = str(texte)
    if len(texte) <= limite:
        return texte
    garde = texte[:limite]
    espace = garde.rfind(" ")
    if espace >= limite * 2 // 3:
        garde = garde[:espace]
    return garde.rstrip() + "…"


def _infobulle(texte: str) -> str:
    """Infobulle sûre pour du texte venu d'une API.

    Qt interprète le texte riche dans les infobulles : un nom d'affichage ou un
    nom d'objectif contenant une balise serait rendu comme telle, et une
    « image » distante y déclencherait une requête réseau. L'échappement dans
    un conteneur <qt> restitue le texte d'origine, inerte.
    """
    return "<qt>" + _html.escape(str(texte)) + "</qt>"


def _make_person_avatar(
    display: str, login: str, size: int = _PERSON_AV_SZ, ring: str = "",
    profile_url: str = "",
) -> QLabel:
    """Pastille ronde portant l'avatar du streamer, son nom en infobulle.

    Plus compact qu'une puce texte : on affiche davantage de participants sur
    une ligne, et le nom reste accessible au survol.
    """
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setToolTip(_infobulle(display))
    lbl.setStyleSheet(
        f"border-radius: {size // 2}px; background-color: #1e1e1e; "
        + (f"border: 1px solid {ring};" if ring else "border: none;")
        + " color: #8a8a8a; font-family: 'Segoe UI Variable'; "
        "font-size: 9px; font-weight: bold;"
    )
    lbl.setText(display[:2].upper())
    key = _avatar_cache_key(login, profile_url)
    if key:
        from widgets.bigscreen_widget import load_avatar_into_label as _load_av
        # profile_url non vide : les invités d'un show ne sont pas dans le cache
        # disque, le loader doit pouvoir aller chercher l'image.
        _load_av(lbl, key, display, size, profile_url)
    return lbl


def _make_chip(name: str) -> QLabel:
    """Petit badge arrondi avec le nom du participant."""
    lbl = QLabel(name)
    # Nom de participant servi par une API tierce : Qt DEVINE le format, et
    # une balise y serait rendue comme telle.
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setFont(QFont(_FONT_SEGOE, 10))
    lbl.setStyleSheet(
        "color: #cccccc; background: #1e1e1e; border: 1px solid #2a2a2a; "
        "border-radius: 10px; padding: 1px 8px;"
    )
    lbl.setFixedHeight(20)
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


def _personne(entree) -> tuple[str, str, str]:
    """Normalise un intervenant en (nom affiché, login, url d'avatar).

    Les appelants historiques ne passaient qu'un nom ; ceux qui disposent du
    triplet complet le passent tel quel, et l'avatar suit.
    """
    if isinstance(entree, str):
        return entree, "", ""
    display, login, purl = (list(entree) + ["", "", ""])[:3]
    return str(display), str(login or ""), str(purl or "")


def _make_person_chip(display: str, login: str = "", profile_url: str = "",
                      accent: bool = False) -> QWidget:
    """Puce arrondie : avatar à gauche, nom à droite.

    Un nom seul demande de connaître la personne pour la reconnaître ; la photo
    se lit d'un coup d'œil. Sans image disponible — les invités d'un show n'ont
    pas de compte Twitch — la pastille garde les initiales, et la puce conserve
    la même hauteur pour que la grille reste alignée.
    """
    chip = QFrame()
    chip.setObjectName("personChip")
    chip.setFixedHeight(26)
    chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    # Sélecteur nommé : sans lui la règle descendrait sur l'avatar et le nom,
    # qui portent leur propre fond et leur propre bordure.
    couleurs = ("#00ff87", "#0d1f16", "#00ff87") if accent else \
               ("#cccccc", "#1e1e1e", "#2a2a2a")
    texte, fond, bordure = couleurs
    chip.setStyleSheet(
        f"QFrame#personChip {{ background: {fond}; border: 1px solid {bordure}; "
        "border-radius: 13px; }"
    )
    h = QHBoxLayout(chip)
    h.setContentsMargins(3, 0, 9, 0)
    h.setSpacing(6)
    h.addWidget(_make_person_avatar(display, login, size=20,
                                    profile_url=profile_url))
    nom = QLabel(display)
    nom.setTextFormat(Qt.TextFormat.PlainText)
    nom.setFont(QFont(_FONT_SEGOE, 10))
    nom.setStyleSheet(f"color: {texte}; background: transparent; border: none;")
    h.addWidget(nom)
    return chip


# ── Popup : liste complète des participants ────────────────────────────────

class _ParticipantsDialog(QDialog):
    """Popup modale listant tous les participants d'un événement."""

    def __init__(self, event_name: str, participants: list,
                 hosts: list, parent: QWidget | None = None) -> None:
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
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setFont(_bold_font(_FONT_SEGOE, 14))
        title.setWordWrap(True)
        lay.addWidget(title)

        if hosts:
            host_lbl = QLabel("Hôtes")
            host_lbl.setFont(_bold_font(_FONT_SEGOE, 10))
            host_lbl.setStyleSheet("color: #555555; letter-spacing: 1px;")
            lay.addWidget(host_lbl)
            hw = QWidget()
            hw.setStyleSheet(_SS_NU)
            hfl = _FlowLayout(hw, h_spacing=6, v_spacing=6)
            for entree in hosts:
                display, login, purl = _personne(entree)
                hfl.addWidget(_make_person_chip(display, login, purl,
                                                accent=True))
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
            pw.setStyleSheet(_SS_NU)
            pfl = _FlowLayout(pw, h_spacing=6, v_spacing=6)
            for entree in participants:
                display, login, purl = _personne(entree)
                pfl.addWidget(_make_person_chip(display, login, purl))
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
        # Un rappel s'affiche PENDANT qu'on fait autre chose : sans cet
        # attribut, la fenêtre flottante active l'application, et ZLink
        # revenait au premier plan à chaque toast — insupportable en mode
        # mock, où ils tombent en rafale.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        bell = QLabel("🔔")
        bell.setFont(QFont(_FONT_SEGOE, 14))
        bell.setStyleSheet(_SS_NU_SANS_BORDURE)
        lay.addWidget(bell)

        txt = QLabel(message)
        # Le message porte le nom d'un show, écrit par l'organisation et servi
        # par une API tierce. Qt DEVINE le texte enrichi : un nom contenant une
        # balise serait rendu comme telle, et une « image » distante y
        # déclencherait une requête réseau depuis le poste. On impose donc le
        # texte brut, comme partout ailleurs où de la donnée d'API s'affiche.
        txt.setTextFormat(Qt.TextFormat.PlainText)
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
        self._gdoc_login: dict[str, str] = {}     # gdoc_id → twitch_login
        self._needs_render: bool = False          # rendu différé si onglet caché
        self._events: list[EventItem] = []
        self._day_btns: dict[str, QPushButton] = {}
        # Rappels relus depuis config.json : ils étaient jusqu'ici en mémoire
        # seulement, donc perdus à chaque redémarrage — gênant sur un event de
        # trois jours où l'application est forcément relancée.
        self._subscribed_ids: set[str] = _load_reminders()
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

    def set_gdoc_map(
        self,
        gdoc_id_to_display: dict[str, str],
        gdoc_id_to_login: dict[str, str] | None = None,
    ) -> None:
        logins = gdoc_id_to_login or {}
        # Le mapping est reconstruit à chaque cycle de 30 s mais il est
        # identique la quasi-totalité du temps ; le re-rendre détruisait et
        # recréait ~650 widgets pour rien, sur un onglet le plus souvent caché.
        if (gdoc_id_to_display == self._gdoc_display
                and logins == self._gdoc_login):
            return
        self._gdoc_display = gdoc_id_to_display
        self._gdoc_login = logins
        if self._events:
            self._render_deferred()

    def _render_deferred(self) -> None:
        """Rend maintenant si l'onglet est visible, sinon au prochain affichage."""
        if self.isVisible():
            self._needs_render = False
            self._render_current_day()
        else:
            self._needs_render = True

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._needs_render:
            self._needs_render = False
            self._render_current_day()

    def update_events(self, events: list[EventItem]) -> None:
        self._events = events
        # TOUS les jours de l'édition restent proposés, plus ceux qu'apporterait
        # l'API. Se limiter aux jours ayant déjà des événements faisait
        # disparaître un jour dont le programme n'est pas encore publié — samedi
        # s'était ainsi volatilisé entre vendredi et dimanche.
        days = sorted(set(_PROG_DAYS_ORDERED) | {ev.day for ev in events if ev.day})
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
            row_w.setStyleSheet(_SS_NU_SANS_BORDURE)
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

    def _resolve_people(
        self,
        uuids: list[str],
        names: dict[str, str] | None = None,
        logins: dict[str, str] | None = None,
        avatars: dict[str, str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Renvoie (nom_affiché, twitch_login, url_avatar) pour chaque uuid.

        Les invités d'un show (artistes, groupes) n'apparaissent pas dans la
        liste des streamers ZEvent : leur login et leur avatar ne viennent que
        de la charge du show, d'où la priorité donnée à `logins`/`avatars`.
        """
        names = names or {}
        logins = logins or {}
        avatars = avatars or {}
        out: list[tuple[str, str, str]] = []
        for uid in uuids:
            display = names.get(uid) or self._gdoc_display.get(uid)
            if display is None:
                continue
            login = logins.get(uid) or self._gdoc_login.get(uid, "")
            out.append((display, login, avatars.get(uid, "")))
        if not out and uuids:
            out = [((uid[:12] + "…" if len(uid) > 12 else uid), "", "") for uid in uuids]
        return out

    @staticmethod
    def _event_key(ev: EventItem) -> str:
        return cle_evenement(ev)

    # ── Card ─────────────────────────────────────────────────────────

    #: Au-delà, les intervenants passent dans la popup plutôt que de faire
    #: déborder la carte sur deux lignes.
    _MONTRES_SUR_LA_CARTE = 8

    def _event_card(self, ev: EventItem) -> QFrame:
        """Une carte de show : heure, nom, durée, rappel, puis les intervenants."""
        key = self._event_key(ev)
        logins = getattr(ev, "logins", None)
        avatars = getattr(ev, "profile_urls", None)
        hotes = self._resolve_people(ev.host_uuids, ev.names, logins, avatars)
        parts = self._resolve_people(ev.participant_uuids, ev.names, logins,
                                     avatars)

        card, cl = self._cadre_de_carte()
        cl.addLayout(self._ligne_titre(ev, key))

        ligne_hotes = self._ligne_hotes(ev, hotes, parts)
        if ligne_hotes is not None:
            cl.addLayout(ligne_hotes)
        ligne_parts = self._ligne_participants(ev, parts, hotes)
        if ligne_parts is not None:
            cl.addLayout(ligne_parts)

        if not hotes and not parts:
            absent = QLabel("Participants non disponibles")
            absent.setFont(QFont(_FONT_SEGOE, 10))
            absent.setStyleSheet(_SS_FAINT_CLEAR)
            cl.addWidget(absent)
        return card

    @staticmethod
    def _cadre_de_carte() -> tuple:
        """(carte, layout de son contenu). La barre verte est posée ici.

        Elle est un frère du contenu, pas une bordure : une bordure gauche de
        3 px se serait arrondie avec le cadre et aurait pincé aux angles.
        """
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #111111; border: 1px solid #1e1e1e;"
            " border-radius: 6px; }"
            "QFrame:hover { border-color: #2a2a2a; }"
        )
        card.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        outer = QHBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        accent = QFrame()
        accent.setFixedWidth(3)
        accent.setStyleSheet(
            "background: #00ff87; border-radius: 2px; border: none;")
        accent.setSizePolicy(QSizePolicy.Policy.Fixed,
                             QSizePolicy.Policy.Expanding)
        outer.addWidget(accent)

        inner = QWidget()
        inner.setStyleSheet(_SS_NU_SANS_BORDURE)
        cl = QVBoxLayout(inner)
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(6)
        outer.addWidget(inner, stretch=1)
        return card, cl

    def _ligne_titre(self, ev: EventItem, key: str) -> QHBoxLayout:
        """Heure, nom, durée, et la cloche de rappel."""
        ligne = QHBoxLayout()
        ligne.setSpacing(6)

        heure = QLabel(_fmt_time_fr(ev.start_local) if ev.start_local else "—")
        heure.setFixedWidth(54)
        heure.setFont(_bold_font(_FONT_SEGOE, 11))
        heure.setStyleSheet(_SS_GREEN_CLEAR)
        ligne.addWidget(heure)

        nom = QLabel(ev.name or "—")
        nom.setTextFormat(Qt.TextFormat.PlainText)
        nom.setFont(_bold_font(_FONT_SEGOE, 13))
        nom.setStyleSheet(_SS_WHITE_CLEAR)
        nom.setWordWrap(True)
        ligne.addWidget(nom, stretch=1)

        duree = _fmt_duration(ev.start_local, ev.end_local)
        if duree:
            lbl = QLabel(duree)
            lbl.setFont(QFont(_FONT_SEGOE, 10))
            lbl.setStyleSheet(_SS_MUTED_CLEAR)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
            ligne.addWidget(lbl)
            ligne.addSpacing(2)

        ligne.addWidget(self._bouton_rappel(ev, key))
        return ligne

    def _bouton_rappel(self, ev: EventItem, key: str) -> QPushButton:
        """Cloche de rappel.

        Pas d'emoji : U+1F514/U+1F515 ne rendent aucun glyphe hors Windows — le
        bouton apparaissait vide — on passe par qtawesome comme le header.
        """
        abonne = key in self._subscribed_ids
        b = QPushButton()
        b.setFixedSize(26, 26)
        b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        b.setToolTip("Désactiver le rappel" if abonne
                     else "Me rappeler 5 min avant")
        b.setFont(_bold_font(_FONT_SEGOE, 11))
        self._style_bell(b, abonne)
        b.clicked.connect(
            lambda _, k=key, bouton=b, e=ev: self._toggle_reminder(k, bouton, e))
        return b

    def _ligne_hotes(self, ev: EventItem, hotes: list,
                     parts: list) -> "QHBoxLayout | None":
        """Animateurs : avatars cerclés de vert, ou puces s'ils n'ont pas de compte.

        Rend None quand l'événement n'a pas d'animateur : l'appelant n'ajoute
        alors aucune ligne, plutôt qu'une ligne vide.
        """
        if not hotes:
            return None
        ligne = QHBoxLayout()
        ligne.setSpacing(4)
        ligne.addWidget(self._icone_micro())
        for display, login, purl in hotes[:self._MONTRES_SUR_LA_CARTE]:
            if login or purl:
                # Anneau vert : marque l'animateur face aux participants.
                ligne.addWidget(_make_person_avatar(display, login,
                                                    ring="#1a3328",
                                                    profile_url=purl))
                continue
            chip = _make_chip(display)
            chip.setStyleSheet(
                "color: #00ff87; background: #0d1f16; border: 1px solid #1a3328; "
                "border-radius: 10px; padding: 1px 8px;")
            ligne.addWidget(chip)
        reste = len(hotes) - self._MONTRES_SUR_LA_CARTE
        if reste > 0:
            ligne.addWidget(self._lien_vers_popup(f"+{reste}", "#00ff87",
                                                  ev, hotes, parts))
        ligne.addStretch()
        return ligne

    @staticmethod
    def _icone_micro() -> QLabel:
        """Le micro qui annonce « animé par »."""
        icone = QLabel()
        icone.setFixedSize(14, 14)
        icone.setStyleSheet(_SS_NU_SANS_BORDURE)
        icone.setToolTip("Animé par")
        if _QTA_OK:
            icone.setPixmap(
                qta.icon("mdi6.microphone", color="#8a8a8a").pixmap(QSize(14, 14)))
        else:
            icone.setText("\U0001f399")
        return icone

    def _ligne_participants(self, ev: EventItem, parts: list,
                            hotes: list) -> "QHBoxLayout | None":
        """Participants en avatars, nom au survol. None s'il n'y en a aucun.

        Les avatars tiennent bien plus serré que les puces texte : on en montre
        huit au lieu de trois avant de renvoyer vers la popup.
        """
        if not parts:
            return None
        ligne = QHBoxLayout()
        ligne.setSpacing(4)
        montres = parts[:self._MONTRES_SUR_LA_CARTE]
        for display, login, purl in montres:
            if login or purl:
                ligne.addWidget(_make_person_avatar(display, login,
                                                    profile_url=purl))
            else:
                # Intervenant hors ZEvent (artiste, invité) : pas de login donc
                # pas d'avatar, la puce texte reste la seule option.
                ligne.addWidget(_make_chip(display))
        reste = len(parts) - len(montres)
        if reste > 0:
            ligne.addWidget(self._lien_vers_popup(
                f"voir les {reste} autres…", "#888888", ev, hotes, parts))
        ligne.addStretch()
        return ligne

    def _lien_vers_popup(self, libelle: str, couleur: str, ev: EventItem,
                         hotes: list, parts: list) -> QPushButton:
        """Le « +N » qui ouvre la liste complète."""
        b = QPushButton(libelle)
        b.setFont(QFont(_FONT_SEGOE, 10))
        b.setFixedHeight(20)
        b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        survol = "#ffffff" if couleur == "#00ff87" else "#00ff87"
        b.setStyleSheet(
            f"QPushButton {{ color: {couleur}; background: transparent;"
            " border: none; text-decoration: underline; padding: 0 2px; }"
            f"QPushButton:hover {{ color: {survol}; }}")
        b.clicked.connect(
            lambda _, e=ev, h=hotes, p=parts:
                self._open_participants_popup(e, h, p))
        return b

    def _open_participants_popup(self, ev: EventItem,
                                  hosts: list, parts: list) -> None:
        dlg = _ParticipantsDialog(ev.name or "Événement", parts, hosts, parent=self)
        dlg.exec()

    # ── Système de rappels ────────────────────────────────────────────

    @staticmethod
    def _style_bell(btn: QPushButton, subscribed: bool) -> None:
        """Applique l'état du bouton rappel.

        L'icône est un pixmap qtawesome : il faut la REMPLACER, pas poser un
        texte. Poser les deux faisait cohabiter icône et libellé côte à côte,
        l'icône se retrouvant décalée et rognée dans un bouton de 26 px.
        """
        if _QTA_OK:
            btn.setIcon(qta.icon(
                "mdi6.bell" if subscribed else "mdi6.bell-outline",
                color="#00ff87" if subscribed else "#8a8a8a",
                color_active="#00ff87",
            ))
            btn.setIconSize(QSize(15, 15))
        else:
            btn.setText("!" if subscribed else "·")
        btn.setToolTip("Désactiver le rappel" if subscribed else "Me rappeler 5 min avant")
        btn.setStyleSheet(
            f"QPushButton {{ background: {'#0d1f16' if subscribed else 'transparent'}; "
            f"color: {'#00ff87' if subscribed else '#8a8a8a'}; "
            f"border: 1px solid {'#00ff87' if subscribed else '#333333'}; "
            "border-radius: 4px; }"
            "QPushButton:hover { border-color: #00ff87; background: #0d1f16; }"
        )

    def _toggle_reminder(self, key: str, btn: QPushButton, ev: EventItem) -> None:
        if key in self._subscribed_ids:
            self._subscribed_ids.discard(key)
            self._style_bell(btn, False)
        else:
            self._subscribed_ids.add(key)
            self._style_bell(btn, True)
        # Sans cette purge, un événement déjà rappelé restait marqué à vie :
        # se désabonner puis se réabonner ne redéclenchait plus rien.
        self._reminded_ids.discard(key)
        _save_reminders(self._subscribed_ids)

    def basculer_rappel_par_cle(self, cle: str) -> bool:
        """Bascule le rappel d'un show désigné par sa seule clé.

        La frise de l'Accueil propose le rappel mais ne le détient pas : c'est
        cet onglet qui garde l'ensemble et l'écrit. Passer par ici plutôt que
        d'écrire la configuration des deux côtés évite que les deux vues se
        contredisent — une cloche éteinte ici et un rappel actif là.

        Rend le nouvel état, pour que l'appelant puisse le dire.
        """
        if cle in self._subscribed_ids:
            self._subscribed_ids.discard(cle)
        else:
            self._subscribed_ids.add(cle)
        # Sans cette purge, un show déjà rappelé restait marqué à vie : se
        # désabonner puis se réabonner ne redéclenchait plus rien.
        self._reminded_ids.discard(cle)
        _save_reminders(self._subscribed_ids)
        # Les cloches se peignent à la construction des cartes : les
        # reconstruire les remet toutes d'accord, sans avoir à retrouver le
        # bouton exact — et il n'y en a peut-être aucun, si l'onglet n'a
        # jamais été ouvert.
        self._render_current_day()
        return cle in self._subscribed_ids

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


class _BarreObjectif(QWidget):
    """Barre de progression d'un objectif, quatre pixels de haut.

    L'ancienne liste n'affichait que le montant visé. « 559 600 € » ne dit pas
    si l'objectif tombe dans l'heure ou s'il restera lettre morte : c'est la
    DISTANCE qui intéresse, pas la cible.
    """

    HAUTEUR = 5

    def __init__(self, part: float, accompli: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HAUTEUR)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._part = max(0.0, min(1.0, part))
        self._accompli = accompli

    def set_part(self, part: float) -> None:
        """Change la part affichée SANS remplacer le widget.

        Détruire la barre pour en poser une neuve à chaque rafraîchissement la
        détachait de son parent, et un widget détaché qui n'a pas été masqué
        est une fenêtre à l'écran.
        """
        borne = max(0.0, min(1.0, part))
        if abs(borne - self._part) < 0.001:
            return
        self._part = borne
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#1c1c1c"))
        if self._part > 0:
            couleur = QColor("#00ff87") if self._accompli else QColor("#38bdf8")
            # Au-delà de 90 %, l'objectif est à portée : la couleur le dit
            # avant qu'on ait lu le pourcentage.
            if not self._accompli and self._part >= 0.9:
                couleur = QColor("#f5c518")
            p.fillRect(0, 0, int(w * self._part), h, couleur)
        p.end()


def _part_objectif(cagnotte: float, cible: float) -> float:
    """Part d'un objectif atteinte, entre 0 et 1.

    Approximation assumée, et la même que partout ailleurs dans ZLink : on
    rapporte la cagnotte TOTALE du streamer au montant de l'objectif. L'API ne
    publie pas de compteur par objectif ; c'est le seul proxy disponible, et il
    vaut surtout pour situer un ordre de grandeur.
    """
    if cible <= 0:
        return 0.0
    return max(0.0, min(1.0, cagnotte / cible))


class _LigneObjectif(QFrame):
    """Un objectif : son état, son nom, sa catégorie, et où il en est."""

    def __init__(self, goal: DonationGoal, cagnotte: float,
                 prefixe: tuple[str, str, str] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("eventRow")
        self._goal = goal
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(5)
        v.addLayout(self._ligne_titre(goal, prefixe))
        if not goal.accomplished:
            v.addWidget(_BarreObjectif(_part_objectif(cagnotte, goal.amount)))
            v.addWidget(self._ligne_distance(goal, cagnotte))

    def _ligne_titre(self, goal: DonationGoal,
                     prefixe: tuple[str, str, str] | None) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(10)
        etat = QLabel("✓" if goal.accomplished else "○")
        etat.setFont(_bold_font(_FONT_SEGOE, 13))
        etat.setStyleSheet(_SS_GREEN if goal.accomplished else _SS_GREY)
        etat.setFixedWidth(16)
        h.addWidget(etat)

        if prefixe is not None:
            # Vue « toutes les chaînes » : l'objectif ne veut rien dire sans
            # savoir de qui il est.
            display, login, purl = prefixe
            h.addWidget(_make_person_avatar(display, login, size=22,
                                            profile_url=purl))
            qui = QLabel(display)
            qui.setTextFormat(Qt.TextFormat.PlainText)
            qui.setFont(_bold_font(_FONT_SEGOE, 11))
            qui.setStyleSheet(_SS_SOFT_CLEAR)
            h.addWidget(qui)

        nom = QLabel(goal.name or "Objectif sans nom")
        nom.setTextFormat(Qt.TextFormat.PlainText)
        nom.setFont(QFont(_FONT_SEGOE, 12))
        nom.setStyleSheet(_SS_MUTED if goal.accomplished else _SS_WHITE)
        nom.setWordWrap(True)
        h.addWidget(nom, stretch=1)

        if goal.category:
            h.addWidget(_etiquette_categorie(goal.category))
        for lien in goal.links[:2]:
            bouton = _bouton_lien(lien)
            if bouton is not None:
                h.addWidget(bouton)

        montant = QLabel(_fmt_euros(goal.amount))
        montant.setFont(_bold_font(_FONT_MONO, 11))
        montant.setStyleSheet(_SS_GREEN if goal.accomplished else _SS_MUTED)
        h.addWidget(montant)
        return h

    @staticmethod
    def _ligne_distance(goal: DonationGoal, cagnotte: float) -> QLabel:
        """Le pourcentage et, surtout, ce qu'il reste à réunir."""
        part = _part_objectif(cagnotte, goal.amount)
        reste = max(0.0, goal.amount - cagnotte)
        texte = f"{part * 100:.1f} %".replace(".", ",")
        if reste > 0:
            texte += f"  ·  plus que {_fmt_euros(reste)}"
        lbl = QLabel(texte)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setFont(QFont(_FONT_MONO, 10))
        lbl.setStyleSheet(
            f"color: {'#f5c518' if part >= 0.9 else '#777777'};"
            " background: transparent; border: none;")
        return lbl


def _etiquette_categorie(categorie: str) -> QLabel:
    """Petite pastille grise portant la catégorie déclarée par l'API."""
    lbl = QLabel(str(categorie)[:18])
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setFont(QFont(_FONT_SEGOE, 9))
    lbl.setStyleSheet(
        "color: #888888; background: #1a1a1a; border: 1px solid #262626;"
        " border-radius: 8px; padding: 1px 7px;")
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


def _bouton_lien(url: str) -> QPushButton | None:
    """Ouvre un lien attaché à un objectif, ou None si l'URL n'est pas sûre.

    Ces liens viennent d'une API communautaire et pointent où leurs auteurs
    veulent : on n'y applique donc PAS l'allowlist des dons, qui n'a rien à
    voir, mais on refuse tout ce qui n'est pas du https — un `file://` ou un
    `javascript:` n'a aucune raison d'arriver jusqu'au système.
    """
    texte = str(url or "").strip()
    if not texte.lower().startswith("https://"):
        return None
    b = QPushButton("🔗")
    b.setFixedSize(22, 22)
    b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    b.setToolTip(_infobulle(texte))
    b.setStyleSheet(
        "QPushButton { background: transparent; border: none; color: #666666; }"
        "QPushButton:hover { color: #00ff87; }")
    b.clicked.connect(lambda: _ouvrir_lien_objectif(texte))
    return b


def _ouvrir_lien_objectif(url: str) -> None:
    """Ouvre le lien dans le navigateur, premier plan cédé."""
    ceder_premier_plan()
    QDesktopServices.openUrl(QUrl(url))
    for delai in (400, 1200):
        QTimer.singleShot(delai, remonter_navigateur)


class _EnteteStreamer(QFrame):
    """Ce qu'on veut savoir avant de lire la liste : qui, combien, où il en est."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        haut = QHBoxLayout()
        haut.setSpacing(12)
        self._avatar = QLabel()
        self._avatar.setFixedSize(44, 44)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar.setStyleSheet(
            "border-radius: 22px; background-color: #1e1e1e; color: #8a8a8a;"
            " font-size: 12px; font-weight: bold; border: none;")
        haut.addWidget(self._avatar)

        colonne = QVBoxLayout()
        colonne.setSpacing(2)
        self._nom = QLabel("—")
        self._nom.setTextFormat(Qt.TextFormat.PlainText)
        self._nom.setFont(_bold_font(_FONT_SEGOE, 15))
        self._nom.setStyleSheet(_SS_WHITE)
        colonne.addWidget(self._nom)
        self._sous_titre = QLabel("")
        self._sous_titre.setFont(QFont(_FONT_SEGOE, 10))
        self._sous_titre.setStyleSheet(_SS_MUTED)
        colonne.addWidget(self._sous_titre)
        haut.addLayout(colonne, stretch=1)

        self._cagnotte = QLabel("")
        self._cagnotte.setFont(_bold_font(_FONT_MONO, 17))
        self._cagnotte.setStyleSheet(
            "color: #00ff87; background: transparent; border: none;")
        haut.addWidget(self._cagnotte, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(haut)

        self._barre = _BarreObjectif(0.0, accompli=True)
        v.addWidget(self._barre)

    def montrer(self, s: StreamerInfo, goals: list) -> None:
        self._nom.setText(s.display)
        self._avatar.setText(s.display[:2].upper())
        from widgets.bigscreen_widget import load_avatar_into_label as _load_av
        _load_av(self._avatar, s.twitch_login, s.display, 44, s.profile_url)
        self._cagnotte.setText(_fmt_euros(s.donation))
        faits = sum(1 for g in goals if _objectif_atteint(g, s.donation))
        total = len(goals)
        self._sous_titre.setText(self._compte_objectifs(faits, total))
        self._barre.set_part(faits / total if total else 0.0)

    @staticmethod
    def _compte_objectifs(faits: int, total: int) -> str:
        """« 2 objectifs atteints sur 5 », ou le cas où il n'y en a aucun."""
        if not total:
            return "aucun objectif publié"
        pluriel = "s" if faits > 1 else ""
        return f"{faits} objectif{pluriel} atteint{pluriel} sur {total}"


#: Ce que dit l'en-tête de la liste, selon la portée retenue.
_TITRES_OBJECTIFS = {
    "tous": "LES PLUS PROCHES",
    "grille": "LES PLUS PROCHES · GRILLE",
    "favoris": "LES PLUS PROCHES · FAVORIS",
    "principal": "OBJECTIFS · FLUX PRINCIPAL",
}

#: Et ce qu'on dit quand il n'y a rien à montrer. Le message change avec la
#: portée : « ils apparaîtront au fil des chaînes consultées » est vrai pour
#: l'ensemble, mais trompeur pour une grille vide — là, c'est la grille qu'il
#: faut remplir, pas attendre.
_ABSENCE_OBJECTIFS = {
    "tous": ("Aucun objectif à afficher pour l'instant.\n"
             "Ils apparaîtront au fil des chaînes consultées."),
    "grille": ("Aucun objectif parmi les chaînes de la grille.\n"
               "Ajoutez-en depuis l'onglet Streamers, ou changez de portée."),
    "favoris": ("Aucun objectif parmi vos favoris.\n"
                "L'étoile d'une fiche de streamer l'y ajoute."),
    "principal": ("Aucun objectif sur le flux affiché en grand.\n"
                  "Cette chaîne n'en a pas déclaré, ou aucun n'est encore "
                  "connu."),
}


def portee_des_objectifs(vue: str, principal: str = "") -> set[str] | None:
    """Les logins que la vue retient, ou None pour ne rien filtrer.

    Lue à chaque affichage plutôt que gardée : la grille se remplit, les
    favoris se posent et le flux principal change pendant que l'onglet est
    ouvert, et une liste figée à la construction montrerait l'état d'il y a
    une heure.

    « principal » rend un ensemble VIDE quand aucune chaîne n'est affichée en
    grand, et non None : sans flux principal, la vue n'a rien à montrer — pas
    tout à montrer.
    """
    if vue == "grille":
        from core.selection_store import SelectionStore

        return set(SelectionStore().get_selected())
    if vue == "favoris":
        return set(favorites.get())
    if vue == "principal":
        return {principal.lower()} if principal else set()
    return None


class _GoalsTab(QWidget):
    """Onglet Goals — les objectifs d'une chaîne, ou les plus proches de tomber.

    L'ancienne version listait des noms et des montants. Il y manquait la seule
    chose qu'on cherche vraiment : la DISTANCE. Chaque objectif porte donc sa
    barre, son pourcentage et ce qu'il reste à réunir, et une seconde vue
    rassemble tous les objectifs connus, le plus proche en tête.
    """

    _sig_goals = pyqtSignal(str, list)  # login, goals — cross-thread

    #: Plafond de la vue « toutes les chaînes ». Au-delà, on ne lit plus.
    _MAX_TOUS = 60

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._streamers: list[StreamerInfo] = []
        self._cache: dict[str, list[DonationGoal]] = {}  # login → goals
        self._pending_login: str = ""
        self._vue: str = "streamer"
        #: Chaîne affichée en plein écran, tenue à jour par le panel.
        self._principal: str = ""
        #: Empreinte de ce qui est actuellement affiché. Le mock réémet ses
        #: données toutes les trois secondes et l'application toutes les
        #: trente : reconstruire soixante lignes identiques à chaque fois ne
        #: change rien à l'écran et fait ramer la machine pour rien.
        self._empreinte: tuple = ()
        self._sig_goals.connect(self._on_goals_arrived)
        self._build()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addLayout(self._build_barre_outils())

        self._entete = _EnteteStreamer()
        root.addWidget(self._entete)

        self._goals_scroll = QScrollArea()
        self._goals_scroll.setWidgetResizable(True)
        self._goals_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._goals_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")
        self._goals_content = QWidget()
        _conteneur_nu(self._goals_content)
        self._goals_layout = QVBoxLayout(self._goals_content)
        self._goals_layout.setContentsMargins(0, 0, 2, 0)
        self._goals_layout.setSpacing(0)
        self._goals_scroll.setWidget(self._goals_content)
        root.addWidget(self._goals_scroll, stretch=1)

        self._show_placeholder("Sélectionner un streamer pour voir ses objectifs")

    def _build_barre_outils(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        lbl = QLabel("Streamer :")
        lbl.setFont(QFont(_FONT_SEGOE, 11))
        lbl.setStyleSheet(_SS_MUTED)
        h.addWidget(lbl)

        self._combo = QComboBox()
        self._combo.setFont(QFont(_FONT_SEGOE, 11))
        self._combo.setMinimumHeight(30)
        # Un nom de streamer n'a pas besoin de 1900 px : largeur fixe, puis on
        # pousse le reste à droite plutôt que d'étirer le champ.
        self._combo.setFixedWidth(280)

        # ON TAPE, ON TROUVE. Il y a plus de trois cents participants : dérouler
        # une liste alphabétique jusqu'à « Ponce » demandait de faire défiler
        # une page et demie. Le champ devient éditable, et le complètement
        # filtre sur ce que le nom CONTIENT — « ponc » comme « nce » ramènent
        # Ponce, alors qu'un complètement par préfixe impose de savoir par quoi
        # le nom commence, ce qui est justement ce qu'on cherche.
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.lineEdit().setPlaceholderText("Taper trois lettres…")
        completeur = self._combo.completer()
        completeur.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion)
        completeur.setFilterMode(Qt.MatchFlag.MatchContains)
        completeur.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Une saisie qui ne correspond à personne ne doit pas laisser le champ
        # sur un nom inventé, alors que la fiche affichée est encore l'ancienne.
        self._combo.lineEdit().editingFinished.connect(self._recaler_la_saisie)
        self._combo.currentIndexChanged.connect(self._on_streamer_changed)
        h.addWidget(self._combo)
        h.addStretch(1)

        self._boutons_vue: dict[str, QPushButton] = {}
        # « Les plus proches » regarde tout ce qui est en cache — trois cents
        # participants un soir d'événement. Or ce qu'on pilote, c'est la
        # grille ; et ce qu'on suit, ce sont les favoris. Un objectif à deux
        # euros de tomber chez quelqu'un qu'on n'affiche pas ne se joue pas.
        # « Flux principal » juste après « Ce streamer » : ce sont les deux
        # portées qui ne visent qu'UNE chaîne, l'une choisie à la main,
        # l'autre suivant ce qu'on regarde en grand.
        for cle, libelle in (("streamer", "Ce streamer"),
                             ("principal", "Flux principal"),
                             ("tous", "Les plus proches"),
                             ("grille", "Grille"),
                             ("favoris", "Favoris")):
            b = QPushButton(libelle)
            b.setFont(QFont(_FONT_SEGOE, 10))
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setFixedHeight(26)
            b.clicked.connect(lambda _=False, k=cle: self._changer_vue(k))
            self._boutons_vue[cle] = b
            h.addWidget(b)
        self._peindre_vues()
        return h

    # -- vues -----------------------------------------------------------------

    def set_main_stream(self, login: str) -> None:
        """Le flux affiché en grand a changé.

        Ne repeint QUE si la vue le regarde : le plein écran change de chaîne
        souvent, et reconstruire une liste d'objectifs qui ne bouge pas est
        du travail pour rien.
        """
        login = str(login or "")
        if login == self._principal:
            return
        self._principal = login
        if self._vue == "principal":
            self._empreinte = ()
            self._rafraichir()

    def _changer_vue(self, cle: str) -> None:
        self._vue = cle
        self._empreinte = ()      # on change de contenu : tout est à refaire
        self._peindre_vues()
        self._rafraichir()

    def _peindre_vues(self) -> None:
        for cle, bouton in self._boutons_vue.items():
            actif = cle == self._vue
            bouton.setStyleSheet(
                "QPushButton { border-radius: 13px; padding: 2px 14px; "
                + ("background: #16341f; color: #00ff87;"
                   " border: 1px solid #00ff87; }"
                   if actif else
                   "background: #161616; color: #888888;"
                   " border: 1px solid #262626; }")
                + "QPushButton:hover { color: #ffffff; }")

    def _rafraichir(self) -> None:
        """Réaffiche la vue courante depuis ce qu'on sait déjà."""
        self._entete.setVisible(self._vue == "streamer")
        self._combo.setEnabled(self._vue == "streamer")
        if self._vue == "streamer":
            self._on_streamer_changed(self._combo.currentIndex())
        else:
            self._montrer_tous()

    def _montrer_tous(self) -> None:
        """Les objectifs connus, le plus proche de tomber en tête.

        On ne montre que ce qui est déjà en cache : aller chercher les
        objectifs des trois cents participants ferait trois cents requêtes pour
        une page qu'on parcourt en diagonale.

        La portée suit le bouton actif — tout, la grille, ou les favoris.
        """
        portee = portee_des_objectifs(self._vue, self._principal)
        dons = {s.twitch_login: s for s in self._streamers}
        lignes = []
        for login, goals in self._cache.items():
            s = dons.get(login)
            if s is None or (portee is not None and login not in portee):
                continue
            for g in goals:
                # Financé = arrivé. Sans cette condition, un objectif dépassé
                # depuis longtemps trônait en tête à 100 % et repoussait ceux
                # qu'on aurait pu accompagner.
                if _objectif_atteint(g, s.donation) or g.amount <= 0:
                    continue
                lignes.append((_part_objectif(s.donation, g.amount), s, g))
        if not lignes:
            self._show_placeholder(_ABSENCE_OBJECTIFS.get(
                self._vue, _ABSENCE_OBJECTIFS["tous"]))
            return
        lignes.sort(key=lambda t: -t[0])
        retenues = lignes[:self._MAX_TOUS]
        empreinte = (self._vue, len(lignes),
                     tuple((round(part, 4), s.twitch_login, g.name, g.amount)
                           for part, s, g in retenues))
        if empreinte == self._empreinte:
            return          # rien n'a bougé : ne pas reconstruire pour rien
        self._empreinte = empreinte
        _clear_layout(self._goals_layout)
        self._ajouter_entete(
            f"{_TITRES_OBJECTIFS[self._vue]} ({len(retenues)})", "#f5c518")
        for _part, s, g in retenues:
            self._goals_layout.addWidget(_LigneObjectif(
                g, s.donation,
                prefixe=(s.display, s.twitch_login, s.profile_url)))
        if len(lignes) > len(retenues):
            self._ajouter_entete(
                f"… et {len(lignes) - len(retenues)} autres non affichés",
                "#555555")
        self._goals_layout.addStretch()

    # -- public ---------------------------------------------------------------

    def _recaler_la_saisie(self) -> None:
        """Ramène le champ sur le streamer réellement affiché.

        Éditable, il accepte n'importe quoi. Laisser « zerat » écrit alors que
        la fiche montre encore quelqu'un d'autre ferait croire à un affichage
        figé.
        """
        saisie = self._combo.currentText()
        if self._combo.findText(saisie, Qt.MatchFlag.MatchExactly) >= 0:
            return
        courant: StreamerInfo | None = self._combo.currentData()
        self._combo.setEditText(courant.display if courant else "")

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        """Met à jour la liste des streamers dans le combo.

        Les favoris d'abord, puis ceux en direct, puis les autres par audience
        décroissante — l'ordre de la palette Ctrl+K. L'ordre alphabétique
        plaçait « Antoine Daniel » en tête quoi qu'il arrive, alors que le
        premier nom proposé devrait être celui qu'on a le plus de chances de
        vouloir.
        """
        current = self._combo.currentText()

        self._streamers = streamers
        favs = favorites.get()
        ordonnes = sorted(
            streamers,
            key=lambda x: (x.twitch_login.lower() not in favs,
                           not x.online, -x.viewers, x.display.lower()),
        )
        self._combo.blockSignals(True)
        self._combo.clear()
        for s in ordonnes:
            self._combo.addItem(s.display, userData=s)
        idx = self._combo.findText(current)
        self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._combo.blockSignals(False)

        if self._combo.count() > 0:
            self._rafraichir()

    # -- slots ----------------------------------------------------------------

    def seed_cache(self, cache: dict) -> None:
        """Pré-remplit le cache local depuis le prefetch DataManager.

        Le prefetch arrive APRÈS la liste des streamers : au moment où l'onglet
        a sélectionné son premier participant, le cache était encore vide et il
        a affiché « Aucun objectif trouvé ». Remplir sans réafficher laissait
        donc ce message figé sur des objectifs désormais connus.
        """
        if not cache:
            return
        self._cache.update(cache)
        if self._vue == "tous":
            self._montrer_tous()
            return
        courant: StreamerInfo | None = self._combo.currentData()
        if courant is not None and courant.twitch_login in cache:
            # L'en-tête compte les objectifs : il était calculé avant l'arrivée
            # du cache et annonçait « 0 sur 0 » sur une chaîne qui en avait.
            goals = self._cache[courant.twitch_login]
            self._entete.montrer(courant, goals)
            self._show_goals(goals)

    def _on_streamer_changed(self, idx: int) -> None:
        if idx < 0 or self._vue != "streamer":
            return
        s: StreamerInfo | None = self._combo.itemData(idx)
        if s is None:
            return
        self._entete.montrer(s, self._cache.get(s.twitch_login, []))
        if not s.participation_id:
            self._show_placeholder("Ce streamer n'a pas d'objectifs publiés")
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
        except Exception:
            logger.exception("_do_fetch(%s)", participation_id)
            goals = []
        self._sig_goals.emit(login, goals)

    def _on_goals_arrived(self, login: str, goals: list) -> None:
        """Slot main-thread — met en cache et affiche si le streamer est encore là.

        Une réponse vide n'écrase pas un cache garni : le prefetch peut avoir
        répondu entre-temps, et une requête qui échoue ne prouve pas que le
        streamer n'a pas d'objectifs.
        """
        if goals or not self._cache.get(login):
            self._cache[login] = goals
        else:
            goals = self._cache[login]
        if self._vue == "tous":
            self._montrer_tous()
            return
        current: StreamerInfo | None = self._combo.currentData()
        if current is not None and current.twitch_login == login:
            self._entete.montrer(current, goals)
            self._show_goals(goals)

    def _show_placeholder(self, text: str) -> None:
        self._empreinte = ("placeholder", text, 0.0, ())
        _clear_layout(self._goals_layout)
        ph = QLabel(text)
        ph.setObjectName("placeholder")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setWordWrap(True)
        self._goals_layout.addStretch()
        self._goals_layout.addWidget(ph)
        self._goals_layout.addStretch()

    def _ajouter_entete(self, texte: str, couleur: str) -> None:
        hdr = QLabel(texte)
        hdr.setFont(_bold_font(_FONT_SEGOE, 10))
        hdr.setStyleSheet(
            f"color: {couleur}; letter-spacing: 1px; padding: 8px 0 4px 0;"
            " background: transparent; border: none;")
        self._goals_layout.addWidget(hdr)

    def _show_goals(self, goals: list[DonationGoal]) -> None:
        courant: StreamerInfo | None = self._combo.currentData()
        cagnotte = courant.donation if courant is not None else 0.0
        empreinte = ("streamer",
                     courant.twitch_login if courant is not None else "",
                     round(cagnotte, 2),
                     tuple((g.name, g.amount, g.accomplished) for g in goals))
        if empreinte == self._empreinte:
            return          # rien n'a bougé : ne pas reconstruire pour rien
        self._empreinte = empreinte
        _clear_layout(self._goals_layout)
        if not goals:
            self._show_placeholder("Aucun objectif trouvé")
            return

        # Les plus proches d'abord : c'est l'ordre dans lequel ils tomberont.
        pending = sorted((g for g in goals if not g.accomplished),
                         key=lambda g: -_part_objectif(cagnotte, g.amount))
        accomplished = sorted((g for g in goals if g.accomplished),
                              key=lambda g: g.amount)

        if pending:
            self._ajouter_entete(f"À ACCOMPLIR ({len(pending)})", "#888888")
            for g in pending:
                self._goals_layout.addWidget(_LigneObjectif(g, cagnotte))
        if accomplished:
            self._ajouter_entete(f"ACCOMPLIS ({len(accomplished)})", "#00ff87")
            for g in accomplished:
                self._goals_layout.addWidget(_LigneObjectif(g, cagnotte))
        self._goals_layout.addStretch()


# ---------------------------------------------------------------------------
# Tab: Streamers — sélection pour la grille (vue en cartes)
# ---------------------------------------------------------------------------

# ── Constantes visuelles ─────────────────────────────────────────────────

_CARD_W = 220          # largeur de référence pour le texte elide
_CARD_H = 168          # hauteur fixe des cartes
_FAV_COLOR = "#f5c518"   # or — étoile favori

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
    #: Clic droit sur la carte — ouvrir la fiche du participant.
    sheet_requested = pyqtSignal(str)
    #: (login, favori) — l'étoile vient d'être posée ou retirée.
    #:
    #: Le bouton ne repeignait que lui-même : le favori était bien enregistré,
    #: mais rien dans l'application ne l'apprenait. Une autre carte de la même
    #: chaîne gardait son étoile creuse, et la touche « Favori » du Stream Deck
    #: restait sur l'état d'avant le clic.
    favori_change = pyqtSignal(str, bool)

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

        self.setObjectName("streamerCard")
        self._apply_style()
        self._build(s)

    # -- construction ----------------------------------------------------------

    def _build(self, s: StreamerInfo) -> None:
        """Assemble la carte. Chaque partie a sa méthode : la rangée du haut,
        les textes, et les trois boutons posés en surimpression."""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 10)
        root.setSpacing(4)

        root.addLayout(self._ligne_avatar(s))
        root.addSpacing(14)
        self._ajouter_textes(root, s)
        root.addStretch()

        self._construire_bouton_favori()
        self._construire_bouton_don(s)
        self._ajouter_bouton_fiche()
        self._construire_badge_slot()

    def _ligne_avatar(self, s: StreamerInfo) -> QHBoxLayout:
        """Avatar, badge viewers en surimpression, badge LAN/Online à droite."""
        av_row = QHBoxLayout()
        av_row.setSpacing(0)
        av_row.setContentsMargins(0, 0, 0, 0)

        # Conteneur avatar + badge viewers superposés via positions absolues
        av_container = QWidget()
        av_container.setFixedSize(_AVATAR_SZ + 4, _AVATAR_SZ + 4)
        av_container.setStyleSheet(_SS_NU)

        self._avatar_lbl = QLabel(av_container)
        self._avatar_lbl.setFixedSize(_AVATAR_SZ, _AVATAR_SZ)
        self._avatar_lbl.move(2, 2)
        self._avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._avatar_lbl.setStyleSheet(
            f"border-radius: {_AVATAR_SZ // 2}px; border: none; "
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

        self._viewers_badge = self._badge_viewers(s, av_container)

        av_row.addWidget(av_container)
        av_row.addStretch()

        libelle, css = self._style_badge_type(s)
        type_badge = QLabel(libelle)
        type_badge.setFont(_bold_font(_FONT_SEGOE, 8))
        type_badge.setStyleSheet(css)
        type_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        av_row.addWidget(type_badge, 0, Qt.AlignmentFlag.AlignTop)
        return av_row

    @staticmethod
    def _badge_viewers(s: StreamerInfo, hote: QWidget):
        """Audience en pastille, dans le coin bas-droit de l'avatar."""
        if not (s.online and s.viewers):
            return None
        vbadge = QLabel(_fmt_viewers(s.viewers), hote)
        vbadge.setFont(_bold_font(_FONT_MONO, 7))
        vbadge.setStyleSheet(
            "background-color: rgba(0,0,0,210); color: #00ff87; "
            "border-radius: 6px; padding: 1px 4px; border: none;"
        )
        vbadge.adjustSize()
        vbadge.move(_AVATAR_SZ + 4 - vbadge.width(),
                    _AVATAR_SZ + 4 - vbadge.height())
        return vbadge

    @staticmethod
    def _style_badge_type(s: StreamerInfo) -> tuple:
        """(libellé, feuille de style) du badge LAN / Online."""
        if (s.location or "").upper() == "LAN":
            return "LAN", (
                f"background-color: #451a03; color: {_COL_LAN}; "
                f"border: 1px solid {_COL_LAN}; border-radius: 6px; padding: 0px 5px;"
            )
        return "Online", (
            f"background-color: {_COL_ONLINE_BG}; color: {_COL_ONLINE}; "
            f"border: 1px solid {_COL_ONLINE}55; border-radius: 6px; padding: 0px 5px;"
        )

    @staticmethod
    def _ligne_de_l_heure(s: StreamerInfo) -> "QLabel | None":
        """Ce que la chaîne a fait dans la dernière heure, ou None.

        L'audience seule ne dit pas ce qui se PASSE : une chaîne peut être
        petite et monter, une grosse peut être en train de redescendre. C'est
        cette variation qu'on cherche du regard pendant l'event, et elle ne
        figurait nulle part.

        Rien n'est affiché tant que la mesure n'existe pas — au lancement, il
        faut un quart d'heure de relevés avant qu'un écart ait un sens.
        """
        from core import tendances

        if not s.online:
            return None
        delta = tendances.viewers(s.twitch_login)
        euros = tendances.cagnotte(s.twitch_login)
        morceaux = []
        if delta:
            signe = "+" if delta > 0 else "−"
            morceaux.append(f"{signe}{_fmt_viewers(abs(delta))} viewers")
        if euros:
            morceaux.append(f"+{_fmt_euros(euros)}")
        if not morceaux:
            return None
        etiquette = QLabel(" · ".join(morceaux) + " / h")
        etiquette.setTextFormat(Qt.TextFormat.PlainText)
        etiquette.setFont(QFont(_FONT_MONO, 8))
        # La couleur porte le SENS : vert on monte, rouge on descend. Un seul
        # gris pour les deux obligerait à lire le signe.
        monte = (delta or 0) > 0 or bool(euros)
        etiquette.setStyleSheet(
            f"color: {'#00ff87' if monte else '#ff6b6b'};"
            " background: transparent; border: none;")
        return etiquette

    def _ajouter_textes(self, root: QVBoxLayout, s: StreamerInfo) -> None:
        """Nom, jeu et cagnotte, élidés à la largeur de carte.

        Jeu et cagnotte sont omis quand ils sont vides plutôt qu'affichés à
        blanc : la carte se resserre au lieu de garder des lignes creuses.
        """
        name_lbl = QLabel(s.display)
        name_lbl.setTextFormat(Qt.TextFormat.PlainText)
        name_lbl.setFont(_bold_font(_FONT_SEGOE, 12))
        name_lbl.setStyleSheet(
            f"color: {'#e8e8e8' if s.online else '#505050'};"
            " background: transparent; border: none;")
        fm = QFontMetrics(name_lbl.font())
        name_lbl.setText(fm.elidedText(s.display, Qt.TextElideMode.ElideRight,
                                       _CARD_W))
        root.addWidget(name_lbl)

        if s.game:
            game_lbl = QLabel(s.game)
            game_lbl.setTextFormat(Qt.TextFormat.PlainText)
            game_lbl.setFont(QFont(_FONT_SEGOE, 10))
            game_lbl.setStyleSheet(
                f"color: {'#888888' if s.online else '#3a3a3a'};"
                " background: transparent; border: none;")
            fm2 = QFontMetrics(game_lbl.font())
            game_lbl.setText(fm2.elidedText(s.game, Qt.TextElideMode.ElideRight,
                                            _CARD_W))
            root.addWidget(game_lbl)

        ligne_heure = self._ligne_de_l_heure(s)
        if ligne_heure is not None:
            root.addWidget(ligne_heure)

        if s.donation > 0:
            don_lbl = QLabel(f"\u2665 {s.donation_formatted}")
            don_lbl.setFont(QFont(_FONT_SEGOE, 9))
            don_lbl.setStyleSheet(
                f"color: {'#3d9970' if s.online else '#2a2a2a'};"
                " background: transparent; border: none;")
            root.addWidget(don_lbl)

    def _construire_bouton_favori(self) -> None:
        """Étoile favori : enfant DIRECT de la carte, posée en bas à droite.

        Dans la rangée du haut elle se disputait la place avec le badge
        LAN/Online ; l'angle libre en bas de carte est plus lisible.
        """
        self._fav_btn = QPushButton(self)
        self._fav_btn.setFixedSize(26, 26)
        self._fav_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._fav_btn.clicked.connect(self._toggle_favorite)
        self._refresh_fav_btn()
        self._fav_btn.raise_()

    def _construire_bouton_don(self, s: StreamerInfo) -> None:
        """Bouton « donner à CE streamer », s'il a une URL de don.

        L'URL est relevée par l'API pour chacun, mais elle ne servait jusqu'ici
        qu'au plein écran, pour la chaîne en cours.
        """
        self._don_url = getattr(s, "donation_url", "") or ""
        self._don_btn = None
        if not self._don_url:
            return
        b = QPushButton(self)
        b.setFixedSize(26, 26)
        b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        b.setToolTip(_infobulle(f"Donner à {s.display}"))
        if _QTA_OK:
            b.setIcon(qta.icon("mdi6.hand-heart-outline", color="#00ff87"))
            b.setIconSize(QSize(16, 16))
        else:
            b.setText("\u2665")
        b.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " color: #00ff87; font-size: 15px; }"
            "QPushButton:hover { background: #0f1a14; border-radius: 4px; }"
        )
        b.clicked.connect(self._on_donate)
        self._don_btn = b

    def _construire_badge_slot(self) -> None:
        """Cercle vert du numéro de slot, en surimpression coin haut-gauche."""
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


    # -- favori ----------------------------------------------------------------

    def _refresh_fav_btn(self) -> None:
        fav = favorites.is_favorite(self._login)
        self._fav_btn.setToolTip(
            "Retirer des favoris" if fav else "Mettre en favori"
        )
        # #5a5a5a sur un fond #111111 ne faisait que 1,5:1 de contraste : le
        # contour de l'étoile se devinait plus qu'il ne se voyait. Un gris clair
        # la rend franchement lisible sans lui donner l'air déjà activée, et
        # l'or reste réservé aux favoris.
        idle = "#b0b6bd"
        if _QTA_OK:
            self._fav_btn.setIcon(qta.icon(
                "mdi6.star" if fav else "mdi6.star-outline",
                color=_FAV_COLOR if fav else idle,
                color_active=_FAV_COLOR,
            ))
            self._fav_btn.setIconSize(QSize(20, 20))
        else:
            self._fav_btn.setText("★" if fav else "☆")
        self._fav_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {_FAV_COLOR if fav else idle}; font-size: 17px; }}"
            "QPushButton:hover { background: #262626; border-radius: 4px; }"
        )

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._place_fav_btn()

    def _place_fav_btn(self) -> None:
        btn = getattr(self, "_fav_btn", None)
        if btn is None:
            return
        m = 8
        btn.move(self.width() - btn.width() - m, self.height() - btn.height() - m)
        btn.raise_()
        # Les boutons secondaires s'alignent de droite à gauche, dans l'ordre
        # où on les rencontre. Chacun ne se pose que s'il existe : le don n'a
        # de bouton que si l'API donne son URL.
        x = self.width() - btn.width() - m
        for nom in ("_don_btn", "_fiche_btn"):
            autre = getattr(self, nom, None)
            if autre is None:
                continue
            x -= autre.width() + 4
            autre.move(x, self.height() - autre.height() - m)
            autre.raise_()

    def _ajouter_bouton_fiche(self) -> None:
        """Ouvre la fiche du participant, d'un clic qu'on voit.

        La fiche n'était atteignable qu'au clic DROIT sur la carte : personne
        ne le découvre, et une fonction qu'on ne trouve pas n'existe pas. Le
        clic droit reste, pour qui le connaît.
        """
        b = QPushButton(self)
        b.setFixedSize(26, 26)
        b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        b.setToolTip(_infobulle("Voir la fiche et les statistiques"))
        if _QTA_OK:
            b.setIcon(qta.icon("mdi6.chart-box-outline", color="#8a8a8a"))
            b.setIconSize(QSize(17, 17))
        else:
            b.setText("\u2261")
        b.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " color: #8a8a8a; font-size: 15px; }"
            "QPushButton:hover { background: #1a1a1a; border-radius: 4px;"
            " color: #ffffff; }"
        )
        b.clicked.connect(lambda: self.sheet_requested.emit(self._login))
        self._fiche_btn = b

    def _on_donate(self) -> None:
        from windows.fullscreen import ouvrir_page_de_don
        ouvrir_page_de_don(getattr(self, "_don_url", ""))

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        """Clic droit : ouvrir la fiche du participant."""
        self.sheet_requested.emit(self._login)
        event.accept()

    def _toggle_favorite(self) -> None:
        etat = favorites.toggle(self._login)
        self._refresh_fav_btn()
        self.favori_change.emit(self._login, etat)

    def _apply_style(self) -> None:
        """Habillage de la carte, restreint à la carte elle-même.

        Le sélecteur porte le nom d'objet, et ce n'est pas cosmétique : QLabel
        DÉRIVE de QFrame, si bien qu'un simple « QFrame { border: ... } » posé
        sur la carte s'appliquait aussi à chacune de ses étiquettes. L'avatar
        héritait ainsi du liseré vert de la sélection — le cerclage disgracieux
        autour des photos. Les autres étiquettes n'y échappaient que parce
        qu'elles redéclarent toutes « border: none ».
        """
        if self._slot is not None:
            self.setStyleSheet(
                f"QFrame#streamerCard {{"
                f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
                f" stop:0 rgba(0,255,135,24), stop:0.45 #131313, stop:1 #111111);"
                f"border: 2px solid {_COL_SEL};"
                f"border-radius: 8px;"
                f"}}"
            )
        elif self._online:
            self.setStyleSheet(
                "QFrame#streamerCard { background-color: #111111; "
                "border: 1px solid #282828; border-radius: 8px; }"
                "QFrame#streamerCard:hover { background-color: #181818; "
                "border-color: #3a3a3a; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#streamerCard { background-color: #0c0c0c; "
                "border: 1px solid #1a1a1a; border-radius: 8px; }"
            )

    def _update_slot_badge(self) -> None:
        if self._slot is not None:
            self._slot_lbl.setText(str(self._slot))
            self._slot_lbl.show()
        else:
            self._slot_lbl.hide()

    # -- public API ------------------------------------------------------------

    def set_slot(self, slot: int | None) -> None:
        if slot == self._slot:
            return   # même valeur : pas de repolish inutile
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
        self._count_lbl.setStyleSheet(_SS_GRIS_EFFACE)
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


# ── _MixerTab ─────────────────────────────────────────────────────────────

class _MixerStrip(QFrame):
    """Une tranche de console : photo, nom, curseur vertical, muet."""

    volume_changed = pyqtSignal(str, int)   # login, 0-100
    mute_toggled   = pyqtSignal(str, bool)  # login, muet
    unpin_requested = pyqtSignal(str)       # retirer cette chaîne de la console

    _W = 84

    def __init__(self, login: str, display: str, volume: int = 100,
                 parent: QWidget | None = None, principal: bool = False) -> None:
        super().__init__(parent)
        self._login = login
        #: La chaîne RÉELLEMENT affichée par cette tranche.
        #:
        #: `_login` est écrasé juste après la construction par la clé de
        #: routage — « #main » pour le plein écran, qui ne change pas quand le
        #: flux change. Sans cette seconde référence, plus rien ne dit quelle
        #: chaîne la tranche montre.
        self._login_reel = login
        self._muet = False
        self.setFixedWidth(self._W)
        # Sans plafond, la tranche s'étire sur toute la hauteur de l'onglet et
        # le fader devient une barre de neuf cents pixels, impossible à doser.
        self.setMaximumHeight(330)
        self.setObjectName("mixStrip")
        # La tranche du plein écran se distingue : c'est la source principale,
        # celle qu'on dose par rapport aux autres.
        self._principal = principal
        bord = "#38bdf8" if principal else "#262626"
        self.setStyleSheet(
            f"QFrame#mixStrip {{ background: #141414; border: 1px solid {bord};"
            " border-radius: 8px; }"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 10, 8, 10)
        v.setSpacing(6)

        if not principal:
            # Retirer une chaîne depuis la console : y aller pour constater
            # qu'on ne veut plus l'entendre, puis devoir retourner faire un
            # clic droit dans la grille, n'avait aucun sens.
            haut = QHBoxLayout()
            haut.setContentsMargins(0, 0, 0, 0)
            haut.addStretch()
            fermer = QPushButton()
            fermer.setFixedSize(18, 18)
            fermer.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            fermer.setToolTip("Retirer des audios épinglés")
            if _QTA_OK:
                fermer.setIcon(qta.icon("mdi6.close", color="#6a6a6a"))
                fermer.setIconSize(QSize(11, 11))
            else:
                fermer.setText("\u2715")
            fermer.setStyleSheet(
                "QPushButton { background: transparent; border: none;"
                " color: #6a6a6a; font-size: 11px; }"
                "QPushButton:hover { background: #2a1414; color: #ff4444;"
                " border-radius: 3px; }"
            )
            fermer.clicked.connect(
                lambda: self.unpin_requested.emit(self._login))
            haut.addWidget(fermer)
            v.addLayout(haut)

        if principal:
            cap = QLabel("PLEIN ÉCRAN")
            cap.setFont(_bold_font(_FONT_SEGOE, 7))
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap.setStyleSheet(
                "color: #38bdf8; background: transparent; border: none;"
                " letter-spacing: 1px;")
            v.addWidget(cap)

        av = _make_person_avatar(display or login, login, 34)
        av.setToolTip(_infobulle(display or login))
        v.addWidget(av, 0, Qt.AlignmentFlag.AlignHCenter)

        self._val = QLabel(f"{volume}")
        self._val.setFont(_bold_font(_FONT_MONO, 12))
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._val.setStyleSheet(
            f"color: {'#38bdf8' if principal else '#00ff87'};"
            " background: transparent; border: none;")
        v.addWidget(self._val)

        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)
        self._slider.setValue(volume)
        self._slider.setMinimumHeight(150)
        self._slider.setMaximumHeight(200)
        self._slider.setStyleSheet(
            "QSlider::groove:vertical { background: #202020; width: 6px;"
            " border-radius: 3px; }"
            "QSlider::sub-page:vertical { background: #202020; border-radius: 3px; }"
            "QSlider::add-page:vertical { background: #00ff87; border-radius: 3px; }"
            "QSlider::handle:vertical { background: #e8e8e8; height: 14px;"
            " margin: 0 -5px; border-radius: 7px; }"
        )
        self._slider.valueChanged.connect(self._on_slider)
        v.addWidget(self._slider, 1, Qt.AlignmentFlag.AlignHCenter)

        self._mute = QPushButton()
        self._mute.setCheckable(True)
        self._mute.setFixedSize(28, 24)
        self._mute.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._mute.setToolTip("Couper cette chaîne")
        self._refresh_mute()
        self._mute.toggled.connect(self._on_mute)
        v.addWidget(self._mute, 0, Qt.AlignmentFlag.AlignHCenter)

        nom = QLabel(display or login)
        nom.setTextFormat(Qt.TextFormat.PlainText)
        nom.setFont(QFont(_FONT_SEGOE, 9))
        nom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nom.setStyleSheet("color: #9a9a9a; background: transparent; border: none;")
        fm = QFontMetrics(nom.font())
        nom.setText(fm.elidedText(display or login, Qt.TextElideMode.ElideRight,
                                  self._W - 16))
        nom.setToolTip(_infobulle(display or login))
        v.addWidget(nom)

    @property
    def login(self) -> str:
        return self._login

    def volume(self) -> int:
        return self._slider.value()

    def _refresh_mute(self) -> None:
        if _QTA_OK:
            self._mute.setIcon(qta.icon(
                "mdi6.volume-off" if self._muet else "mdi6.volume-high",
                color="#ff4444" if self._muet else "#8a8a8a"))
        else:
            self._mute.setText("\u25cf")
        self._mute.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
            "QPushButton:hover { background: #202020; border-radius: 4px; }"
            "QPushButton:checked { background: #2a1414; border-radius: 4px; }"
        )

    def _on_slider(self, v: int) -> None:
        self._val.setText(str(v))
        if not self._muet:
            self.volume_changed.emit(self._login, v)

    def _on_mute(self, muet: bool) -> None:
        self._muet = muet
        self._refresh_mute()
        self._val.setStyleSheet(
            f"color: {'#ff4444' if muet else '#00ff87'};"
            " background: transparent; border: none;")
        # Le curseur reste à sa valeur : la coupure est une notion distincte du
        # volume, et couper puis rétablir doit retrouver le réglage.
        self.mute_toggled.emit(self._login, muet)


class _MixerTab(QWidget):
    """Console de mixage des audios épinglés.

    L'épinglage était binaire : une chaîne s'entendait ou pas. Suivre deux
    directs à la fois demande de doser — le principal fort, le second en fond.
    On ne mixe QUE les chaînes épinglées : les autres sont muettes par nature,
    et une console de trois cents tranches ne se pilote pas.
    """

    volume_changed = pyqtSignal(str, int)   # login, 0-100
    #: Volume du flux affiché en plein écran, réglé depuis la console.
    main_volume_changed = pyqtSignal(int)
    #: Chaîne à retirer des audios épinglés.
    unpin_requested = pyqtSignal(str)
    #: (login, coupé) — coupure d'une chaîne depuis la console.
    mute_changed = pyqtSignal(str, bool)
    #: Coupure du flux affiché en plein écran.
    main_mute_changed = pyqtSignal(bool)

    #: Clé interne de la tranche « plein écran ». Elle ne peut pas entrer en
    #: collision avec un login Twitch, qui n'accepte pas ce caractère.
    _MAIN = "#main"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._main_login: str = ""
        self._muets: dict[str, bool] = {}
        self._strips: dict[str, _MixerStrip] = {}
        #: Ce pour quoi les tranches actuelles ont été construites : la chaîne
        #: du plein écran, puis les clés de chaque tranche. Comparer les seules
        #: clés ne suffit pas — celle du plein écran est la constante
        #: `_MAIN`, identique d'une chaîne à l'autre.
        self._empreinte: tuple | None = None
        self._volumes: dict[str, int] = {}
        self._displays: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        self.setStyleSheet(_SS_BG_DARK)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        entete = QHBoxLayout()
        titre = QLabel("MIXER")
        titre.setFont(_bold_font(_FONT_SEGOE, 11))
        titre.setStyleSheet(
            "color: #00ff87; background: transparent; letter-spacing: 2px;")
        entete.addWidget(titre)
        self._compte = QLabel("")
        self._compte.setFont(QFont(_FONT_SEGOE, 10))
        self._compte.setStyleSheet(_SS_GREY_CLEAR)
        entete.addWidget(self._compte)
        entete.addStretch()
        root.addLayout(entete)

        self._vide = QLabel(
            "Aucune source audio.\n\n"
            "Le flux en plein écran apparaît ici, ainsi que les cellules dont "
            "vous épinglez l'audio\n(clic droit sur une cellule de la grille).")
        self._vide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vide.setFont(QFont(_FONT_SEGOE, 11))
        self._vide.setStyleSheet(_SS_GRIS_NU)
        root.addWidget(self._vide, 1)

        self._rangee = QHBoxLayout()
        self._rangee.setSpacing(10)
        self._rangee.setContentsMargins(0, 0, 0, 0)
        self._conteneur = QWidget()
        self._conteneur.setStyleSheet(_SS_NU)
        self._conteneur.setLayout(self._rangee)
        root.addWidget(self._conteneur, 1)
        self._conteneur.setVisible(False)

    # -- API -------------------------------------------------------------

    def set_displays(self, noms: dict[str, str]) -> None:
        """Noms d'affichage, pour ne pas montrer des logins bruts."""
        self._displays = dict(noms)

    def set_main_stream(self, login: str) -> None:
        """Chaîne affichée en plein écran : première tranche de la console."""
        login = str(login or "")
        if login == self._main_login:
            return
        self._main_login = login
        self._rebuild()

    def set_pinned(self, logins: list[str]) -> None:
        """Reconstruit la console pour les chaînes épinglées."""
        self._pinned = [str(lg) for lg in logins if lg]
        self._rebuild()

    def _rebuild(self) -> None:
        # Le plein écran d'abord : c'est la source principale, celle par rapport
        # à laquelle on dose les autres. Il n'apparaît qu'une fois, même s'il
        # est aussi épinglé dans la grille.
        logins = []
        if self._main_login:
            logins.append(self._MAIN)
        logins += [lg for lg in getattr(self, "_pinned", [])
                   if lg != self._main_login]
        # La chaîne du plein écran fait PARTIE de l'empreinte. Sans elle, un
        # changement de flux laissait la comparaison inchangée — la clé de sa
        # tranche vaut toujours `_MAIN` — et la console gardait l'avatar et le
        # nom de la chaîne précédente.
        empreinte = (self._main_login, tuple(logins))
        if self._empreinte == empreinte:
            return
        self._empreinte = empreinte
        while self._rangee.count():
            item = self._rangee.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()   # avant de détacher : détaché et visible = une fenêtre
                w.setParent(None)
                w.deleteLater()
        self._strips.clear()
        for lg in logins:
            principal = lg == self._MAIN
            vrai = self._main_login if principal else lg
            # Le réglage précédent est retrouvé : dépingler puis rappeler une
            # chaîne ne doit pas remettre son volume à fond.
            vol = self._volumes.get(lg, 100)
            strip = _MixerStrip(vrai, self._displays.get(vrai, vrai), vol,
                                principal=principal)
            strip._login = lg      # la clé interne pilote le routage
            if self._muets.get(lg):
                strip._mute.setChecked(True)
            strip.volume_changed.connect(self._on_volume)
            strip.mute_toggled.connect(self._on_mute)
            strip.unpin_requested.connect(self.unpin_requested)
            if strip._muet:
                # Une tranche reconstruite doit retrouver son état de coupure.
                self._on_mute(lg, True)
            self._strips[lg] = strip
            self._rangee.addWidget(strip, 0, Qt.AlignmentFlag.AlignTop)
        self._rangee.addStretch()
        # Appliquer les réglages mémorisés SANS attendre que l'utilisateur
        # touche un curseur : une chaîne rappelée doit retrouver son niveau.
        for lg, strip in self._strips.items():
            self.volume_changed.emit(lg, strip.volume())
        self._compte.setText(self._compte_sources(len(logins)))
        self._vide.setVisible(not logins)
        self._conteneur.setVisible(bool(logins))

    @staticmethod
    def _compte_sources(combien: int) -> str:
        """« · 3 sources », ou rien du tout quand la console est vide."""
        if not combien:
            return ""
        return f"· {combien} source" + ("s" if combien > 1 else "")

    def _on_mute(self, login: str, muet: bool) -> None:
        self._muets[login] = muet
        if login == self._MAIN:
            self.main_mute_changed.emit(muet)
        else:
            self.mute_changed.emit(login, muet)

    def set_main_volume(self, valeur: int) -> None:
        """Repose la tranche du plein écran sans relancer l'aller-retour.

        Les signaux de la tranche sont bloqués le temps du réglage : les
        laisser passer renverrait la valeur au plein écran, qui la
        réappliquerait, et le curseur se battrait contre lui-même dès qu'on le
        déplace.
        """
        valeur = max(0, min(100, int(valeur)))
        self._volumes[self._MAIN] = valeur
        strip = self._strips.get(self._MAIN)
        if strip is None:
            return
        bloque = strip._slider.blockSignals(True)
        strip._slider.setValue(valeur)
        strip._slider.blockSignals(bloque)
        strip._val.setText(str(valeur))

    def set_main_muted(self, muet: bool) -> None:
        """Repose la coupure du plein écran, même règle de non-retour."""
        muet = bool(muet)
        self._muets[self._MAIN] = muet
        strip = self._strips.get(self._MAIN)
        if strip is None or strip._muet == muet:
            return
        bloque = strip._mute.blockSignals(True)
        strip._mute.setChecked(muet)
        strip._mute.blockSignals(bloque)
        strip._muet = muet
        strip._refresh_mute()

    def regler_volume(self, login: str, valeur: int) -> None:
        """Règle une tranche depuis l'EXTÉRIEUR de la console — télécommande.

        Différent de `set_main_volume`, qui ne fait que reposer le curseur
        après un réglage venu d'ailleurs : ici la console est le point de
        départ, donc elle doit à la fois bouger ET prévenir la grille.

        Passer par la console plutôt que d'appeler la grille directement n'est
        pas un détour : c'est elle qui garde le niveau de chaque tranche, et
        c'est ce niveau que la télécommande relit avant le cran suivant. Court-
        circuitée, elle continuait d'annoncer l'ancienne valeur — la molette
        repartait du même point à chaque cran, et le volume ne bougeait plus.
        """
        cle = self._MAIN if not login else login
        valeur = max(0, min(100, int(valeur)))
        self._volumes[cle] = valeur
        strip = self._strips.get(cle)
        if strip is not None:
            bloque = strip._slider.blockSignals(True)
            strip._slider.setValue(valeur)
            strip._slider.blockSignals(bloque)
            strip._val.setText(str(valeur))
        if cle == self._MAIN:
            self.main_volume_changed.emit(valeur)
        else:
            self.volume_changed.emit(cle, valeur)

    def regler_muet(self, login: str, muet: bool) -> None:
        """Coupe une tranche depuis l'extérieur. Même règle que `regler_volume`."""
        cle = self._MAIN if not login else login
        muet = bool(muet)
        self._muets[cle] = muet
        strip = self._strips.get(cle)
        if strip is not None and strip._muet != muet:
            bloque = strip._mute.blockSignals(True)
            strip._mute.setChecked(muet)
            strip._mute.blockSignals(bloque)
            strip._muet = muet
            strip._refresh_mute()
        if cle == self._MAIN:
            self.main_mute_changed.emit(muet)
        else:
            self.mute_changed.emit(cle, muet)

    def niveaux(self) -> dict[str, tuple[int, bool]]:
        """Volume et coupure de chaque tranche, par login.

        Publié pour la télécommande : une molette de Stream Deck doit afficher
        le niveau RÉEL avant qu'on la tourne, sinon le premier cran fait sauter
        le son d'un endroit inattendu.

        La tranche du plein écran est rendue sous la clé vide, comme partout
        ailleurs dans la télécommande.
        """
        rendu: dict[str, tuple[int, bool]] = {}
        for cle in set(self._volumes) | set(self._muets):
            login = "" if cle == self._MAIN else str(cle)
            rendu[login] = (int(self._volumes.get(cle, 100)),
                            bool(self._muets.get(cle, False)))
        return rendu

    def _on_volume(self, login: str, v: int) -> None:
        strip = self._strips.get(login)
        if strip is not None and not strip._muet:
            self._volumes[login] = v
        if login == self._MAIN:
            self.main_volume_changed.emit(v)
        else:
            self.volume_changed.emit(login, v)


# ── _StreamersTab ─────────────────────────────────────────────────────────

class _StreamersTab(QWidget):
    """Onglet Streamers — vue en cartes interactives pour sélectionner la grille."""

    grid_selection_changed = pyqtSignal(list)  # list[str] twitch_logins
    #: Login dont on veut ouvrir la fiche.
    sheet_requested = pyqtSignal(str)
    #: (login, favori) — relayé depuis les cartes.
    favori_change = pyqtSignal(str, bool)

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
        # Filtre : trois cents participants sans moyen de chercher, il fallait
        # faire défiler à l'œil pour trouver quelqu'un.
        self._query: str = ""
        self._only_online: bool = False
        self._only_lan: bool = False
        self._only_fav: bool = False
        self._game: str = ""
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

        btn_all = QPushButton("\u2713 Tous")
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

        # Dispositions enregistrées : recharger une sélection en un clic plutôt
        # que de recocher vingt-cinq cases à chaque changement de contexte.
        toolbar.addSpacing(8)
        self._preset_combo = QComboBox()
        self._preset_combo.setFont(QFont(_FONT_SEGOE, 10))
        self._preset_combo.setMinimumHeight(26)
        self._preset_combo.setMinimumWidth(160)
        self._preset_combo.activated.connect(self._on_preset_chosen)
        toolbar.addWidget(self._preset_combo)

        btn_save = QPushButton("Enregistrer")
        btn_save.setObjectName("watchBtn")
        btn_save.setFont(QFont(_FONT_SEGOE, 9))
        btn_save.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_save.setToolTip("Enregistrer la sélection actuelle sous un nom")
        btn_save.clicked.connect(self._on_preset_save)
        toolbar.addWidget(btn_save)
        self._refresh_presets()

        root.addLayout(toolbar)
        root.addWidget(self._make_filter_bar())

        # Affiché quand le filtre ne laisse rien : une page vide sans un mot
        # laisse croire à un chargement qui n'arrive pas.
        self._empty_lbl = QLabel("Aucun streamer ne correspond à ce filtre.")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setFont(QFont(_FONT_SEGOE, 12))
        self._empty_lbl.setStyleSheet(_SS_GRIS_NU)
        self._empty_lbl.setFixedHeight(48)
        self._empty_lbl.setVisible(False)
        root.addWidget(self._empty_lbl)

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
            _SS_SCROLL_NU
        )

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet(_SS_FOND_PAGE)
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

        # Grilles par section (créées une fois, repeuplées).
        #
        # Le parent est donné DÈS LA CONSTRUCTION, et ce n'est pas un détail de
        # style : sans parent, ces six widgets sont des fenêtres de premier
        # niveau. `_rebuild_cards` les rendait visibles AVANT de les insérer
        # dans le layout — six fenêtres nues surgissaient sur le bureau à
        # chaque réagencement, et un réagencement a lieu dès qu'un streamer
        # change d'état. Tracé sur l'application réelle : _SectionHeader et
        # _CardsGrid, sans parent, rendus visibles depuis _rebuild_cards.
        hote = self._scroll_content
        self._grid_lan    = _CardsGrid(hote)
        self._grid_online = _CardsGrid(hote)
        self._grid_off    = _CardsGrid(hote)

        # `parent=` nommé : le deuxième paramètre positionnel est le compteur.
        self._hdr_lan    = _SectionHeader("LAN", parent=hote)
        self._hdr_online = _SectionHeader("EN LIGNE", parent=hote)
        self._hdr_off    = _SectionHeader("HORS LIGNE", parent=hote)

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
            self._refresh_game_list()
            self._content_stack.setCurrentIndex(0)  # affiche "Chargement…"
            QTimer.singleShot(0, self._deferred_rebuild)

    def set_max_streams(self, n: int) -> None:
        """Propage le maximum de streams autorisés (depuis settings).

        Et ROGNE la sélection en cours si elle dépasse. Abaisser ce réglage
        est un aveu : la machine ne suit plus. Se contenter de réécrire le
        compteur laissait les flux excédentaires ouverts — exactement ceux
        qu'on venait de dire de trop — et affichait « 5 / 3 », un total
        supérieur à son propre maximum.
        """
        self.MAX_SELECTED = max(1, n)
        if len(self._selected) > self.MAX_SELECTED:
            retires = self._selected[self.MAX_SELECTED:]
            self._selected = self._selected[:self.MAX_SELECTED]
            logger.info("Plafond abaissé à %d : %d chaîne(s) retirée(s) — %s",
                        self.MAX_SELECTED, len(retires), ", ".join(retires))
            self._renumber_slots()
            self.grid_selection_changed.emit(list(self._selected))
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

    # -- dispositions ----------------------------------------------------------

    def _refresh_presets(self) -> None:
        from core.selection_store import SelectionStore
        presets = SelectionStore().presets()
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItem(
            f"Dispositions ({len(presets)})" if presets else "Aucune disposition", "")
        for nom in sorted(presets):
            self._preset_combo.addItem(f"{nom}  ({len(presets[nom])})", nom)
        if presets:
            self._preset_combo.insertSeparator(self._preset_combo.count())
            self._preset_combo.addItem("Supprimer une disposition\u2026", "__del__")
        self._preset_combo.setCurrentIndex(0)
        self._preset_combo.blockSignals(False)

    def _on_preset_chosen(self, _idx: int) -> None:
        nom = str(self._preset_combo.currentData() or "")
        if not nom:
            return
        from core.selection_store import SelectionStore
        store = SelectionStore()
        if nom == "__del__":
            self._delete_preset_dialog(store)
            return
        logins = store.presets().get(nom, [])
        # Ne garder que ce qui existe encore : une disposition d'hier peut citer
        # des chaînes absentes aujourd'hui.
        connus = {s.twitch_login for s in self._streamers}
        self._selected = [lg for lg in logins if lg in connus][:self.MAX_SELECTED]
        self._renumber_slots()
        self._update_counter()
        self.grid_selection_changed.emit(list(self._selected))
        self._preset_combo.setCurrentIndex(0)

    def _on_preset_save(self) -> None:
        from PyQt6.QtWidgets import QInputDialog
        if not self._selected:
            return
        nom, ok = QInputDialog.getText(
            self, "Enregistrer la disposition",
            f"Nom pour ces {len(self._selected)} chaînes :")
        if not ok or not nom.strip():
            return
        from core.selection_store import SelectionStore
        SelectionStore().save_preset(nom, list(self._selected))
        self._refresh_presets()

    def _delete_preset_dialog(self, store) -> None:
        from PyQt6.QtWidgets import QInputDialog
        noms = sorted(store.presets())
        if not noms:
            self._preset_combo.setCurrentIndex(0)
            return
        nom, ok = QInputDialog.getItem(
            self, "Supprimer une disposition", "Disposition :", noms, 0, False)
        if ok and nom:
            store.delete_preset(nom)
        self._refresh_presets()

    # -- filtre ----------------------------------------------------------------

    def _make_filter_bar(self) -> QWidget:
        """Recherche libre + bascules. Tout se fait en mémoire, sans requête."""
        bar = QWidget()
        bar.setStyleSheet(_SS_NU)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Rechercher un streamer ou un jeu\u2026")
        self._search.setClearButtonEnabled(True)
        self._search.setFont(QFont(_FONT_SEGOE, 10))
        self._search.setFixedHeight(28)
        self._search.setStyleSheet(
            "QLineEdit { background: #141414; color: #e8e8e8; border: 1px solid "
            "#2a2a2a; border-radius: 6px; padding: 0 8px; }"
            "QLineEdit:focus { border-color: #00ff87; }"
        )
        self._search.textChanged.connect(self._on_query_changed)
        h.addWidget(self._search, stretch=1)

        self._toggles: dict[str, QPushButton] = {}
        for key, label in (("online", "En ligne"), ("lan", "LAN"),
                           ("fav", "Favoris")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFont(QFont(_FONT_SEGOE, 10))
            btn.setFixedHeight(28)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setStyleSheet(
                "QPushButton { background: #141414; color: #9a9a9a; border: 1px "
                "solid #2a2a2a; border-radius: 6px; padding: 0 12px; }"
                "QPushButton:hover { color: #cccccc; border-color: #3a3a3a; }"
                "QPushButton:checked { background: #0f1a14; color: #00ff87; "
                "border-color: #00ff87; }"
            )
            btn.toggled.connect(self._on_toggle_changed)
            self._toggles[key] = btn
            h.addWidget(btn)

        self._game_combo = QComboBox()
        self._game_combo.setFont(QFont(_FONT_SEGOE, 10))
        self._game_combo.setMinimumHeight(28)
        self._game_combo.setMinimumWidth(150)
        self._game_combo.addItem("Tous les jeux", "")
        self._game_combo.currentIndexChanged.connect(self._on_game_changed)
        h.addWidget(self._game_combo)

        self._filter_count = QLabel("")
        self._filter_count.setFont(QFont(_FONT_SEGOE, 10))
        self._filter_count.setStyleSheet(_SS_GREY_CLEAR)
        h.addWidget(self._filter_count)
        return bar

    def _on_query_changed(self, text: str) -> None:
        self._query = (text or "").strip().casefold()
        self._rebuild_cards()

    def _on_toggle_changed(self) -> None:
        self._only_online = self._toggles["online"].isChecked()
        self._only_lan = self._toggles["lan"].isChecked()
        self._only_fav = self._toggles["fav"].isChecked()
        self._rebuild_cards()

    def _on_game_changed(self) -> None:
        self._game = str(self._game_combo.currentData() or "")
        self._rebuild_cards()

    def _refresh_game_list(self) -> None:
        """Réalimente la liste des jeux depuis les streamers en ligne."""
        jeux = sorted({(s.game or "").strip() for s in self._streamers
                       if s.online and (s.game or "").strip()},
                      key=str.casefold)
        if jeux == [self._game_combo.itemData(i)
                    for i in range(1, self._game_combo.count())]:
            return
        # Le jeu choisi doit survivre au réassemblage de la liste.
        garder = self._game
        self._game_combo.blockSignals(True)
        self._game_combo.clear()
        self._game_combo.addItem("Tous les jeux", "")
        for j in jeux:
            self._game_combo.addItem(j, j)
        idx = self._game_combo.findData(garder)
        self._game_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._game_combo.blockSignals(False)
        self._game = str(self._game_combo.currentData() or "")

    def _matches(self, s: StreamerInfo) -> bool:
        if self._only_online and not s.online:
            return False
        if self._only_lan and (s.location or "").upper() != "LAN":
            return False
        if self._only_fav and s.twitch_login.lower() not in favorites.get():
            return False
        if self._game and (s.game or "").strip() != self._game:
            return False
        if self._query:
            foin = f"{s.display} {s.twitch_login} {s.game or ''}".casefold()
            if self._query not in foin:
                return False
        return True

    def _filter_active(self) -> bool:
        return bool(self._query or self._only_online or self._only_lan
                    or self._only_fav or self._game)

    def _purger_cartes_obsoletes(self, voulus: dict) -> None:
        """Détruit les cartes qui n'ont plus lieu d'être, et vide le layout.

        Une carte porte sa couleur, son curseur et son badge selon l'état en
        ligne : celle dont l'état a basculé doit être refaite, pas retouchée.
        """
        perimees = [
            lg for lg, card in self._card_map.items()
            if lg not in voulus or card._online != voulus[lg].online
        ]
        for lg in perimees:
            card = self._card_map.pop(lg)
            card.hide()   # avant de détacher : détaché et visible = une fenêtre
            card.setParent(None)  # type: ignore[arg-type]
            card.deleteLater()

        # Les grilles et les en-têtes, eux, sont RÉUTILISÉS : on les retire du
        # layout sans les détruire.
        while self._scroll_vl.count():
            self._scroll_vl.takeAt(0)

    def _cartes_pour(self, items: list, sel_set: set) -> list:
        """Les cartes d'une section, réutilisées quand elles existent déjà."""
        cartes = []
        for s in items:
            slot = (self._selected.index(s.twitch_login) + 1
                    if s.twitch_login in sel_set else None)
            card = self._card_map.get(s.twitch_login)
            if card is None:
                card = _StreamerCard(s, slot)
                card.toggled.connect(self._on_card_toggled)
                card.sheet_requested.connect(self.sheet_requested)
                card.favori_change.connect(self._sur_favori)
                self._card_map[s.twitch_login] = card
            else:
                # Réutilisée : seuls le slot et les viewers peuvent avoir bougé,
                # et set_slot ne repolit que si la valeur change.
                card.set_slot(slot)
                card.update_viewers(s.viewers)
            cartes.append(card)
        return cartes

    def _poser_section(self, section: list, entete, grille, sel_set: set) -> None:
        """Insère une section dans le layout, ou la masque si elle est vide.

        L'ordre n'est pas cosmétique : `addWidget` REPARENTE le widget, et
        `setVisible(True)` doit venir après. L'inverse affiche un widget sans
        parent — c'est-à-dire une fenêtre nue sur le bureau, et il y en avait
        six à chaque réagencement.
        """
        if not section:
            # Retirée du layout par takeAt() mais toujours parentée : sans ce
            # masquage elle continuerait de se peindre à sa dernière position,
            # et les titres se superposaient.
            entete.hide()
            grille.hide()
            return
        entete.set_count(len(section))
        self._scroll_vl.addWidget(entete)
        entete.setVisible(True)
        grille.populate(self._cartes_pour(section, sel_set))
        self._scroll_vl.addWidget(grille)
        grille.setVisible(True)

    def _rebuild_cards(self) -> None:
        """Réagence les cartes selon le tri courant, en réutilisant l'existant.

        La version précédente détruisait les 300 cartes (≈2 900 widgets, ~785 ms
        mesurées) dès qu'UN SEUL streamer changeait d'état — ce qui arrive
        quasiment à chaque cycle pendant l'event. On ne recrée désormais que les
        cartes réellement concernées : celles qui apparaissent, celles qui
        disparaissent, et celles dont l'état en ligne a basculé.
        """
        self._purger_cartes_obsoletes(
            {s.twitch_login: s for s in self._streamers if s.twitch_login})

        sel_set = set(self._selected)
        visibles = [s for s in self._streamers if self._matches(s)]

        def _lan(s):
            return (s.location or "").upper() == "LAN"

        sections = (
            (self._sorted_streamers([s for s in visibles if s.online and _lan(s)]),
             self._hdr_lan, self._grid_lan),
            (self._sorted_streamers([s for s in visibles if s.online and not _lan(s)]),
             self._hdr_online, self._grid_online),
            (self._sorted_streamers([s for s in visibles if not s.online]),
             self._hdr_off, self._grid_off),
        )
        for section, entete, grille in sections:
            self._poser_section(section, entete, grille, sel_set)

        self._scroll_vl.addStretch()

        # Une carte écartée par le filtre reste enfant de sa grille : sans la
        # masquer explicitement, elle continuerait de se peindre par-dessus.
        gardees = {s.twitch_login for s in visibles}
        for login, card in self._card_map.items():
            card.setVisible(login in gardees)

        total = len(self._streamers)
        self._filter_count.setText(
            f"{len(visibles)} / {total}" if self._filter_active() else "")
        self._empty_lbl.setVisible(not visibles and total > 0)


    def _renumber_slots(self) -> None:
        """Resynchronise les numéros de slot sur toutes les cartes.

        Seules les cartes dont le slot CHANGE sont retouchées : appeler
        set_slot() sur les 280 autres leur réappliquait la feuille de style
        qu'elles avaient déjà, et un setStyleSheet sur une carte repolit tout
        son sous-arbre (0,32 ms pièce, ~100 ms au total à chaque clic).
        """
        slots = {lg: i + 1 for i, lg in enumerate(self._selected)}
        for login, card in self._card_map.items():
            new = slots.get(login)
            if card._slot != new:
                card.set_slot(new)

    def _update_counter(self) -> None:
        n = len(self._selected)
        color = "#ff4444" if n >= self.MAX_SELECTED else "#888888"
        self._counter_lbl.setText(f"Grille : {n} / {self.MAX_SELECTED}")
        self._counter_lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")

    # -- slots ----------------------------------------------------------------

    def rafraichir_favori(self, login: str) -> None:
        """Repeint l'étoile d'une carte après un changement venu d'AILLEURS.

        Le favori se pose aussi au clavier et depuis le boîtier Stream Deck :
        la carte doit alors suivre, sans quoi les deux moitiés de
        l'application affichent l'inverse l'une de l'autre.
        """
        carte = self._card_map.get(login)
        if carte is not None:
            carte._refresh_fav_btn()

    def _sur_favori(self, login: str, favori: bool) -> None:
        """Répercute l'étoile : mêmes cartes, puis le reste de l'application."""
        carte = self._card_map.get(login)
        if carte is not None:
            carte._refresh_fav_btn()
        self.favori_change.emit(login, favori)

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

#: En deçà de cette étendue, l'axe des graphes passe aux minutes. Deux heures,
#: comme `DateAxisItem` : au-delà, le jour et l'heure suffisent à situer un
#: point ; en deçà, ils sont identiques d'un bout à l'autre de la série.
_SEUIL_MINUTES_AXE = 2 * 3600


def _etiquette_graphe(ts: float, avec_minutes: bool) -> str:
    """Un instant, tel que l'axe des graphes HTML l'écrit."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + PARIS
    if avec_minutes:
        return f"{dt.hour:02d}h{dt.minute:02d}"
    return f"{JOURS_FR[dt.weekday()]} {dt.hour:02d}h"


def abscisses_graphe(instants: list[float]) -> list[str]:
    """Étiquettes d'axe, à l'heure VRAIE des relevés.

    Elles étaient rebasées sur une origine fixe — l'ouverture de la cagnotte —
    pour que l'axe raconte la même chose que les courbes de comparaison. Le
    remède valait quand ZLink ne traçait que ses propres relevés : leur date
    n'avait alors aucun rapport avec le temps de course.

    L'édition en cours est désormais préchargée depuis son début. Ses
    horodatages SONT le temps de course, et les éditions passées sont replacées
    dessus par `_alignee` — Chart.js les aligne ensuite par indice, sur ces
    mêmes abscisses. Rebaser une seconde fois ne corrigeait donc plus rien :
    ça déplaçait l'axe. Douze heures de relevés allant du jeudi midi au
    vendredi minuit s'affichaient « Ven 18h → Sam 06h ».

    Le format, lui, suit l'ÉTENDUE de la série : en deçà de deux heures il
    passe aux minutes, comme `DateAxisItem` le fait pour les graphes pyqtgraph
    du même fichier. Sans quoi une série courte affiche la même heure d'un bout
    à l'autre.
    """
    if not instants:
        return []
    minutes = (instants[-1] - instants[0]) <= _SEUIL_MINUTES_AXE
    return [_etiquette_graphe(t, minutes) for t in instants]


# ---------------------------------------------------------------------------
# Tab: Stats — 5 graphes en layout 2 colonnes
# ---------------------------------------------------------------------------

#: Feuille de style du classement. Deux règles y sont indispensables et
#: manquaient : sans `:selected`, Qt retombe sur la palette du système, le
#: texte prend la couleur « highlighted text » — identique au fond de
#: sélection sur ce thème sombre — et la ligne choisie s'affichait VIDE,
#: cerclée du rectangle de focus.
_STYLE_CLASSEMENT = """
    QTableWidget {
        outline: none;
        background-color: #0f0f0f;
        alternate-background-color: #121212;
        color: #dddddd;
        font-family: "Segoe UI Variable";
        font-size: 11px;
        border: none;
    }
    QTableWidget::item { padding: 2px 6px; border: none; }
    QTableWidget::item:selected {
        background-color: #16341f;
        color: #ffffff;
    }
    QTableWidget::item:hover { background-color: #161616; }
    QHeaderView::section {
        background-color: #111111;
        color: #00ff87;
        font-size: 10px;
        border: none;
        padding: 5px 6px;
    }
    QHeaderView::section:hover { color: #ffffff; }
"""

class _Tuile(QFrame):
    """Un chiffre, son libellé, et rien d'autre.

    Les quatre nombres qu'on regarde en premier méritaient mieux que d'être
    déduits d'un tableau de trois cents lignes.
    """

    def __init__(self, libelle: str, couleur: str = "#ffffff") -> None:
        super().__init__()
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(2)
        titre = QLabel(libelle)
        titre.setFont(_bold_font(_FONT_SEGOE, 9))
        titre.setStyleSheet("color: #666666; letter-spacing: 2px; "
                            "background: transparent; border: none;")
        v.addWidget(titre)
        self._valeur = QLabel("—")
        self._valeur.setFont(_bold_font(_FONT_MONO, 20))
        self._valeur.setStyleSheet(
            f"color: {couleur}; background: transparent; border: none;")
        v.addWidget(self._valeur)
        self._detail = QLabel("")
        self._detail.setFont(QFont(_FONT_SEGOE, 9))
        self._detail.setStyleSheet("color: #666666; background: transparent; "
                                   "border: none;")
        v.addWidget(self._detail)

    def set_valeur(self, valeur: str, detail: str = "") -> None:
        self._valeur.setText(valeur)
        self._detail.setText(detail)


class _BarreDeCellule(QStyledItemDelegate):
    """Peint une barre proportionnelle DERRIÈRE la valeur d'une cellule.

    Un graphe séparé disait déjà ce que la colonne dit : deux endroits à lire
    pour une seule information, et un axe écrasé par les trois plus gros. La
    barre dans la cellule compare sur place, et le nombre reste exact.

    Volontairement très transparente : c'est un repère de grandeur, le texte
    par-dessus doit rester le premier lu.
    """

    #: Rôle où la cellule range sa part, entre 0 et 1.
    PART = Qt.ItemDataRole.UserRole + 1

    def __init__(self, couleur: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._couleur = QColor(couleur)
        self._couleur.setAlpha(45)

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        super().paint(painter, option, index)
        try:
            part = float(index.data(self.PART) or 0.0)
        except (TypeError, ValueError):
            return
        if part <= 0.0:
            return
        r = option.rect
        largeur = max(2, int(r.width() * min(1.0, part)))
        painter.fillRect(r.left(), r.top() + 4, largeur, r.height() - 8,
                         self._couleur)


class _CelluleNombre(QTableWidgetItem):  # NOSONAR — voir __lt__
    """Cellule qui se compare par son nombre, pas par son texte.

    « 11.9k » est inférieur à « 617 » dans l'ordre alphabétique.

    Un seul `__lt__`, et c'est voulu : ce n'est pas un ordre partiel qu'il
    faudrait compléter, mais la redéfinition de `operator<` de Qt. Le tri d'un
    QTableWidget n'appelle que celui-là, depuis le C++ ; les trois autres
    comparaisons ne seraient jamais invoquées, et `functools.total_ordering`
    exigerait un `__eq__` qui changerait l'identité de l'item pour Qt.
    """

    def __init__(self, texte: str, valeur: float) -> None:
        super().__init__(texte)
        self.valeur = valeur

    def __lt__(self, autre) -> bool:  # type: ignore[override]
        if isinstance(autre, _CelluleNombre):
            return self.valeur < autre.valeur
        return super().__lt__(autre)


def _est_sur_place(location: str) -> bool:
    """LAN, Ankama, Villa : tout ce qui n'est pas « à distance ».

    L'API nomme plusieurs lieux physiques selon l'édition ; les énumérer un par
    un vieillirait. On prend le complément de ce qui est explicitement distant.
    """
    lieu = (location or "").strip().lower()
    return bool(lieu) and lieu not in ("online", "remote", "distance")


#: Couleur de la variation horaire. La couleur porte le SENS : sans elle, il
#: faudrait lire le signe pour savoir si la chaîne monte ou descend.
#:
#: « inconnu » et « stable » ne sont PAS la même chose — l'un veut dire qu'on
#: ne sait pas encore, l'autre que rien n'a bougé — et se distinguent donc
#: aussi par leur gris.
_COULEURS_TENDANCE = {
    "inconnu": "#555555",
    "stable": "#666666",
    "hausse": "#00ff87",
    "baisse": "#ff6b6b",
}


def _sens_tendance(delta: int | None) -> str:
    """Dans quel sens va la chaîne : inconnu, stable, hausse ou baisse."""
    if delta is None:
        return "inconnu"
    if delta > 0:
        return "hausse"
    return "baisse" if delta < 0 else "stable"


def _texte_tendance(delta: int | None) -> str:
    """« +6.0k », « -2.5k », « = » quand rien n'a bougé, « — » quand on ignore."""
    if delta is None:
        return "—"
    if not delta:
        return "="
    return f"{'+' if delta > 0 else '-'}{_fmt_viewers(abs(delta))}"


def _texte_tendance_euros(delta: float | None) -> str:
    """« +1 240 € », « = » quand rien n'a bougé, « — » quand on ignore encore.

    Jamais négatif : `tendances.cagnotte` borne déjà à zéro, une cagnotte ne
    redescendant pas. Un « = » dit donc « rien reçu sur la fenêtre », ce qui
    est une information, pas une absence.
    """
    if delta is None:
        return "—"
    if delta < 1.0:
        return "="
    return f"+{_fmt_euros(delta)}"


def _tendance_dons_de(s: StreamerInfo) -> float:
    """Euros récoltés dans l'heure, 0 si on ne sait pas encore — pour trier."""
    from core import tendances

    return float(tendances.cagnotte(s.twitch_login) or 0.0)


def _sens_tendance_euros(delta: float | None) -> str:
    """Dans quel sens va la cagnotte : inconnu, stable ou hausse.

    Jamais « baisse » : `tendances.cagnotte` borne déjà à zéro, une cagnotte
    ne redescendant pas. Et jamais « hausse » sous l'euro : un centime d'écart
    entre deux relevés est du bruit d'arrondi, pas une montée.
    """
    if delta is None:
        return "inconnu"
    return "hausse" if delta >= 1.0 else "stable"


def _objectif_atteint(but: object, cagnotte: float | None = None) -> bool:
    """Un objectif de dons est-il tombé.

    `DonationGoal` — ce que `DataManager` met dans le cache — expose
    `accomplished`. Lire `done` rendait TOUJOURS faux : la colonne affichait
    « 0/N » quel que soit le nombre d'objectifs atteints, et le tri mettait à
    égalité toutes les chaînes qui en publient.

    `done` reste accepté en second : CLAUDE.md documente la compatibilité
    historique « accomplished ?? done » des données communautaires.

    Le drapeau ne suffit pas. Le streamer le coche quand il veut, et parfois
    jamais : une chaîne à seize mille euros annonçait « 0 objectif atteint sur
    17 » pendant que chacune des dix-sept barres affichait 100,0 %. La
    cagnotte, quand on la connaît, tranche donc aussi — un objectif dont la
    cible est dépassée est atteint, coché ou non.
    """
    atteint = getattr(but, "accomplished", None)
    if atteint is None:
        atteint = getattr(but, "done", False)
    if bool(atteint):
        return True
    if cagnotte is None:
        return False
    cible = float(getattr(but, "amount", 0.0) or 0.0)
    return cible > 0 and cagnotte >= cible


def _duree_de(s: StreamerInfo) -> float:
    """Secondes de direct, 0 si inconnu — pour trier, pas pour afficher."""
    from core import live_uptime

    return (live_uptime.depuis(s.twitch_login) or 0.0) if s.online else 0.0


def _tendance_de(s: StreamerInfo) -> float:
    """Viewers gagnés ou perdus dans l'heure, 0 si on ne sait pas encore."""
    from core import tendances

    return float(tendances.viewers(s.twitch_login) or 0) if s.online else 0.0


def _part_objectifs(cache: dict, s: StreamerInfo) -> float:
    """Part des objectifs atteints. -1 quand la chaîne n'en annonce aucun.

    Trier sur la PART et non sur le compte : trois objectifs sur quatre valent
    mieux que trois sur vingt, et un classement qui dit l'inverse ment.
    """
    buts = cache.get(s.twitch_login) or []
    if not buts:
        return -1.0
    return sum(1 for b in buts if _objectif_atteint(b, s.donation)) / len(buts)


#: Rang de chaque colonne du classement. Nommés : six index nus dans le
#: remplissage puis dans la configuration, c'est une colonne insérée au
#: milieu et trois décalages silencieux.
(_C_RANG, _C_NOM, _C_LIEU, _C_JEU, _C_DUREE,
 _C_OBJ, _C_VUE, _C_TEND, _C_DON, _C_TEND_DON) = range(10)


# ---------------------------------------------------------------------------
# Tab: Clips — ce que la communauté a gardé du plateau
# ---------------------------------------------------------------------------

def _duree_courte(secondes: float) -> str:
    """« 1:05 ». Les clips font moins d'une minute pour la plupart."""
    total = max(0, int(round(secondes)))
    return f"{total // 60}:{total % 60:02d}"


def _il_y_a(instant: float, maintenant: float | None = None) -> str:
    """« il y a 3 h ». La date exacte d'un clip n'apprend rien ; sa fraîcheur si."""
    if not instant:
        return ""
    ecart = max(0.0, (time.time() if maintenant is None else maintenant) - instant)
    if ecart < 3600:
        return f"il y a {int(ecart // 60)} min"
    if ecart < 86400:
        return f"il y a {int(ecart // 3600)} h"
    return f"il y a {int(ecart // 86400)} j"


class _CacheVignettes(QObject):
    """Les images de prévisualisation des clips, téléchargées une fois.

    Un cache à part de celui des avatars : celui-là rend des pastilles RONDES,
    et une vignette de clip est un rectangle 16:9. Le détourner aurait rogné
    chaque image en cercle.

    Le résultat revient par un SIGNAL. Un fil de téléchargement ne peut pas
    toucher aux widgets, et un `QTimer.singleShot` posé depuis lui ne part
    jamais — il naît dans un fil sans boucle d'événements.
    """

    #: L'adresse dont la vignette vient d'arriver. Chaque carte compare avec la
    #: sienne : une seule connexion suffit alors pour toutes.
    prete = pyqtSignal(str)

    #: Au-delà, ce n'est pas une vignette. Twitch les rend en 480×272.
    _MAX_OCTETS = 2 * 1024 * 1024

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._images: dict[str, QPixmap] = {}
        self._encours: set[str] = set()

    def pixmap(self, url: str) -> QPixmap | None:
        """La vignette si elle est là. Sinon None, et le chargement démarre."""
        if not url:
            return None
        if url in self._images:
            return self._images[url]
        if url not in self._encours:
            self._encours.add(url)
            threading.Thread(target=self._charger, args=(url,),
                             daemon=True).start()
        return None

    def _charger(self, url: str) -> None:
        import urllib.request

        donnees = b""
        try:
            requete = urllib.request.Request(
                url, headers={"User-Agent": "ZLink/1.0"})
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                donnees = reponse.read(self._MAX_OCTETS + 1)
        except Exception as exc:                            # noqa: BLE001
            logger.debug("Vignette indisponible (%s) : %s", url[:60], exc)
        if 0 < len(donnees) <= self._MAX_OCTETS:
            image = QPixmap()
            if image.loadFromData(donnees):
                self._images[url] = image
        self._encours.discard(url)
        self.prete.emit(url)


class _CarteClip(QFrame):
    """Un clip en vignette : l'image d'abord, le texte dessous.

    Une liste de titres ne dit pas ce qu'on va voir. La prévisualisation, si —
    c'est elle qu'on parcourt, exactement comme sur une page de vidéos.
    """

    clique = pyqtSignal(object)          # le Clip

    LARGEUR = 300
    _HAUTEUR_IMAGE = 169                 # 300 × 9/16, au pixel près

    def __init__(self, clip, cache: _CacheVignettes,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip = clip
        self._cache = cache
        self.setFixedWidth(self.LARGEUR)
        self.setObjectName("carteClip")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(
            "QFrame#carteClip { background: transparent; border: none; "
            "border-radius: 8px; }"
            "QFrame#carteClip:hover { background: #161616; }")

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 10)
        v.setSpacing(8)

        # ── la vignette, avec la durée posée dessus ─────────────────────────
        cadre = QWidget()
        cadre.setFixedHeight(self._HAUTEUR_IMAGE)
        cadre.setStyleSheet(_FOND_TRANSPARENT)
        self._image = QLabel(cadre)
        self._image.setGeometry(0, 0, self.LARGEUR - 12, self._HAUTEUR_IMAGE)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setStyleSheet(
            "background: #141414; border-radius: 6px; color: #333333;")
        self._image.setText("…")
        self._appliquer_vignette()

        self._duree = QLabel(_duree_courte(clip.duree_s), cadre)
        self._duree.setFont(QFont(_FONT_MONO, 9))
        self._duree.setStyleSheet(
            "background: rgba(0,0,0,200); color: #ffffff; "
            "border-radius: 3px; padding: 1px 5px;")
        self._duree.adjustSize()
        self._duree.move(self.LARGEUR - 18 - self._duree.width(),
                         self._HAUTEUR_IMAGE - 8 - self._duree.height())
        v.addWidget(cadre)

        # ── le texte ────────────────────────────────────────────────────────
        titre = QLabel(clip.titre)
        titre.setTextFormat(Qt.TextFormat.PlainText)   # il vient de Twitch
        titre.setFont(_bold_font(_FONT_SEGOE, 10))
        titre.setStyleSheet(_SS_WHITE)
        titre.setWordWrap(True)
        titre.setFixedHeight(34)                       # deux lignes, pas plus
        titre.setToolTip(_infobulle(clip.titre))
        v.addWidget(titre)

        vues = f"{clip.vues:,}".replace(",", "\u202f")
        dessous = QLabel(" · ".join(x for x in (
            clip.chaine, f"{vues} vues", _il_y_a(clip.cree_le)) if x))
        dessous.setTextFormat(Qt.TextFormat.PlainText)
        dessous.setFont(QFont(_FONT_SEGOE, 9))
        dessous.setStyleSheet(_SS_MUTED)
        v.addWidget(dessous)

        cache.prete.connect(self._sur_vignette)

    def _sur_vignette(self, url: str) -> None:
        if url == self._clip.vignette:
            self._appliquer_vignette()

    def _appliquer_vignette(self) -> None:
        image = self._cache.pixmap(self._clip.vignette)
        if image is None or image.isNull():
            return
        largeur = self.LARGEUR - 12
        self._image.setPixmap(image.scaled(
            largeur, self._HAUTEUR_IMAGE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation))
        self._image.setText("")

    def mousePressEvent(self, event) -> None:      # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clique.emit(self._clip)
        super().mousePressEvent(event)


class _ClipsTab(QWidget):
    """Onglet Clips — ce que la communauté a gardé du plateau.

    Les moments forts y remontent d'eux-mêmes : un clip est fait par quelqu'un
    qui regardait, et vu par d'autres avant de l'être par nous. C'est le seul
    endroit du panel où l'information vient des spectateurs.

    La fenêtre est de sept jours, comme sur Twitch : sans elle, la catégorie
    remonte les clips des éditions précédentes.
    """

    #: Clips par page. Trois cents chaînes en rendent des milliers : les poser
    #: tous d'un coup ferait autant de widgets, et la fenêtre se figerait à
    #: chaque tri. Soixante, c'est une quinzaine de rangs — de quoi faire
    #: défiler sans se perdre, et tout reste atteignable.
    _PAR_PAGE = 60

    clip_choisi = pyqtSignal(object)     # le Clip à lire
    #: Interne. Un fil de travail ne peut pas toucher aux widgets, et
    #: `QTimer.singleShot` posé DEPUIS ce fil ne part jamais : le timer naît
    #: dans un fil sans boucle d'événements. Qt, lui, met une émission de
    #: signal en file d'attente vers le fil du destinataire — c'est ce que fait
    #: déjà `DataManager` pour ses propres relèves.
    _charges = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clips: list = []
        self._logins: list[str] = []
        self._chargement = False
        self._colonnes = 0
        self._page = 0
        self._vignettes = _CacheVignettes(self)
        self._charges.connect(self._recevoir)
        self._build()

    def _build(self) -> None:
        from core import twitch_clips

        # Le fond est posé ici, comme les autres pages : sans lui la page
        # emprunte celui du système, et un titre blanc sur blanc disparaît.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_SS_FOND_PAGE)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 16, 24, 16)
        v.setSpacing(10)

        entete = QHBoxLayout()
        entete.setSpacing(12)
        self._compte = QLabel("")
        self._compte.setFont(QFont(_FONT_SEGOE, 10))
        self._compte.setStyleSheet(_SS_MUTED)
        entete.addWidget(self._compte)
        entete.addStretch(1)

        # Le filtre par chaîne se remplit avec CE QUI A ÉTÉ CLIPPÉ, pas avec
        # les trois cents participants : une liste où la plupart des entrées ne
        # rendent rien ne se parcourt pas.
        self._chaine = QComboBox()
        self._chaine.setFixedHeight(28)
        self._chaine.setMinimumWidth(180)
        self._chaine.currentIndexChanged.connect(self._depuis_le_debut)
        entete.addWidget(self._chaine)

        self._tri = QComboBox()
        self._tri.setFixedHeight(28)
        for cle, libelle in twitch_clips.TRIS.items():
            self._tri.addItem(libelle, cle)
        self._tri.currentIndexChanged.connect(self._depuis_le_debut)
        entete.addWidget(self._tri)

        self._bouton = QPushButton("Rafraîchir")
        self._bouton.setFixedHeight(28)
        self._bouton.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._bouton.clicked.connect(self.rafraichir)
        entete.addWidget(self._bouton)
        v.addLayout(entete)

        zone = QScrollArea()
        zone.setWidgetResizable(True)
        zone.setFrameShape(QFrame.Shape.NoFrame)
        zone.setStyleSheet(_FOND_TRANSPARENT)
        zone.viewport().setStyleSheet(_FOND_TRANSPARENT)
        self._contenu = QWidget()
        self._contenu.setStyleSheet(_SS_FOND_PAGE)
        self._liste = QGridLayout(self._contenu)
        self._liste.setContentsMargins(0, 0, 0, 0)
        self._liste.setSpacing(10)
        self._liste.setAlignment(Qt.AlignmentFlag.AlignTop
                                 | Qt.AlignmentFlag.AlignLeft)
        self._zone = zone
        zone.setWidget(self._contenu)
        v.addWidget(zone, stretch=1)

        self._vide = QLabel("Aucun clip pour l'instant.")
        self._vide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vide.setFont(QFont(_FONT_SEGOE, 12))
        self._vide.setStyleSheet(_SS_GRIS_EFFACE)
        v.addWidget(self._vide)

        pages = QHBoxLayout()
        pages.setContentsMargins(0, 4, 0, 0)
        pages.setSpacing(10)
        pages.addStretch(1)
        self._precedent = QPushButton("‹  Précédent")
        self._precedent.setFixedHeight(28)
        self._precedent.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._precedent.clicked.connect(lambda: self._aller_a(self._page - 1))
        pages.addWidget(self._precedent)
        self._page_lbl = QLabel("")
        self._page_lbl.setFont(QFont(_FONT_MONO, 10))
        self._page_lbl.setStyleSheet(_SS_MUTED)
        pages.addWidget(self._page_lbl)
        self._suivant = QPushButton("Suivant  ›")
        self._suivant.setFixedHeight(28)
        self._suivant.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._suivant.clicked.connect(lambda: self._aller_a(self._page + 1))
        pages.addWidget(self._suivant)
        pages.addStretch(1)
        self._barre_pages = pages
        v.addLayout(pages)

    # -- chargement -----------------------------------------------------------

    def showEvent(self, event) -> None:                    # type: ignore[override]
        """Charge à la PREMIÈRE ouverture, pas au lancement du panel.

        Une requête que personne n'a demandée coûte une seconde de démarrage à
        qui n'ouvrira jamais cet onglet — et la plupart ne l'ouvriront pas.
        """
        super().showEvent(event)
        if not self._clips and not self._chargement:
            self.rafraichir()

    def rafraichir(self) -> None:
        """Recharge la liste. Une passe à la fois.

        Sans ce garde, cliquer deux fois lançait deux requêtes concurrentes
        dont la plus lente écrasait la plus récente.
        """
        if self._chargement:
            return
        self._chargement = True
        self._bouton.setEnabled(False)
        self._bouton.setText("Chargement…")
        threading.Thread(target=self._charger, daemon=True).start()

    def set_streamers(self, streamers: list) -> None:
        """Les participants dont on ira chercher les clips.

        La catégorie ZEvent ne voit que les clips qui en PORTENT l'étiquette :
        une chaîne qui bascule sur autre chose entre deux temps forts en sort.
        Interroger les participants un par un les rattrape tous — six chaînes
        seules rendent deux cent quarante-neuf clips là où la catégorie
        entière en donne soixante-dix-huit.
        """
        self._logins = [s.twitch_login for s in streamers or []
                        if getattr(s, "twitch_login", "")]

    def _charger(self) -> None:
        from core import twitch_clips

        try:
            clips = _run_coro(
                twitch_clips.lister_par_chaines(self._logins) if self._logins
                # Avant l'arrivée des participants — au tout premier
                # affichage — la catégorie donne déjà de quoi remplir la page.
                else twitch_clips.lister())
        except Exception:                                  # noqa: BLE001
            logger.exception("Clips : chargement impossible")
            clips = []
        # Le fil de Qt est le seul à toucher aux widgets : on repasse par lui.
        self._charges.emit(clips)

    def _recevoir(self, clips: list) -> None:
        self._chargement = False
        self._bouton.setEnabled(True)
        self._bouton.setText("Rafraîchir")
        if clips:
            self._clips = clips
            self._remplir_les_chaines()
            self._page = 0
        self._reafficher()

    def _remplir_les_chaines(self) -> None:
        """Les chaînes réellement présentes, par ordre de clips décroissant.

        Le choix courant est retenu : rafraîchir la liste ne doit pas ramener
        d'autorité sur « toutes les chaînes » quelqu'un qui en suivait une.
        """
        courant = self._chaine.currentData()
        comptes: dict[str, int] = {}
        libelles: dict[str, str] = {}
        for clip in self._clips:
            comptes[clip.login] = comptes.get(clip.login, 0) + 1
            libelles[clip.login] = clip.chaine or clip.login
        self._chaine.blockSignals(True)
        self._chaine.clear()
        self._chaine.addItem(f"Toutes les chaînes ({len(comptes)})", "")
        for login, combien in sorted(comptes.items(),
                                     key=lambda kv: (-kv[1], kv[0])):
            self._chaine.addItem(f"{libelles[login]}  ({combien})", login)
        rang = self._chaine.findData(courant) if courant else 0
        self._chaine.setCurrentIndex(max(0, rang))
        self._chaine.blockSignals(False)

    # -- affichage ------------------------------------------------------------

    def _reafficher(self) -> None:
        from core import twitch_clips

        _clear_layout(self._liste)
        cle = self._tri.currentData() or "vues"
        login = str(self._chaine.currentData() or "")
        retenus = [c for c in self._clips if not login or c.login == login]
        clips = twitch_clips.trier(retenus, str(cle))
        self._vide.setVisible(not clips)
        self._vide.setText(
            "Aucun clip pour cette chaîne." if login and self._clips
            else "Aucun clip pour l'instant.")
        # « 7 derniers jours » était faux depuis qu'on interroge les chaînes :
        # ce qu'on garde commence à l'ouverture de la cagnotte, pas une semaine
        # plus tôt.
        self._compte.setText(
            f"{len(clips)} clips · depuis l'ouverture" if clips else "")
        colonnes = self._colonnes_tenables()
        self._colonnes = colonnes
        pages = max(1, -(-len(clips) // self._PAR_PAGE))
        # Filtrer peut raccourcir la liste sous la page où l'on était : sans ce
        # recadrage, on tombait sur une page vide sans comprendre pourquoi.
        self._page = max(0, min(self._page, pages - 1))
        debut = self._page * self._PAR_PAGE
        for rang, clip in enumerate(clips[debut:debut + self._PAR_PAGE]):
            carte = _CarteClip(clip, self._vignettes)
            carte.clique.connect(self.clip_choisi)
            self._liste.addWidget(carte, rang // colonnes, rang % colonnes)
            carte.show()
        self._peindre_les_pages(pages, len(clips))

    def _peindre_les_pages(self, pages: int, total: int) -> None:
        """Les commandes de page, effacées quand une seule suffit.

        Deux boutons grisés sous une page unique n'apprennent rien et donnent
        l'impression qu'il manque quelque chose.
        """
        assez = pages > 1
        for widget in (self._precedent, self._suivant, self._page_lbl):
            widget.setVisible(assez)
        if not assez:
            return
        self._precedent.setEnabled(self._page > 0)
        self._suivant.setEnabled(self._page < pages - 1)
        premier = self._page * self._PAR_PAGE + 1
        dernier = min(total, (self._page + 1) * self._PAR_PAGE)
        self._page_lbl.setText(
            f"{premier}–{dernier} sur {total}   ·   page {self._page + 1}/{pages}")

    def _aller_a(self, page: int) -> None:
        """Change de page et REMONTE : on lit une page depuis son début."""
        if page == self._page:
            return
        self._page = max(0, page)
        self._reafficher()
        self._zone.verticalScrollBar().setValue(0)

    def _depuis_le_debut(self) -> None:
        """Un tri ou un filtre change la liste : la page où l'on était n'a
        plus de sens, et la page 4 d'une liste de trente serait vide."""
        self._page = 0
        self._reafficher()

    def _colonnes_tenables(self) -> int:
        """Combien de cartes tiennent en largeur. Au moins une.

        Recalculé plutôt que fixé : le panel s'affiche sur des écrans de
        1920 comme sur la moitié d'un 2560, et une grille figée à quatre
        colonnes déborde sur l'un et laisse le vide sur l'autre.
        """
        largeur = self._zone.viewport().width() if self._zone else 0
        pas = _CarteClip.LARGEUR + self._liste.spacing()
        return max(1, int((largeur + self._liste.spacing()) // pas))

    def resizeEvent(self, event) -> None:              # type: ignore[override]
        """Recompose la grille quand le nombre de colonnes change.

        Seulement quand il CHANGE : reconstruire soixante-dix-huit cartes à
        chaque pixel de redimensionnement ferait ramer la fenêtre.
        """
        super().resizeEvent(event)
        if self._clips and self._colonnes_tenables() != self._colonnes:
            self._reafficher()


class _LecteurClip(QDialog):
    """Le clip choisi, lu par le lecteur de ZLink.

    Pas l'embed de Twitch : il vérifie le domaine de la page qui l'accueille,
    ce qu'une application de bureau n'a pas. L'API rend l'adresse du MP4 et un
    jeton signé — de quoi le donner à mpv, qui joue déjà tout le reste.
    """

    #: Interne, pour la même raison que dans l'onglet : le fil qui résout
    #: l'adresse ne peut pas toucher au lecteur.
    _resolue = pyqtSignal(str)

    def __init__(self, clip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip = clip
        self._resolue.connect(self._lire)
        self.setWindowTitle(f"{clip.chaine} — {clip.titre}")
        self.resize(960, 600)
        self.setStyleSheet(_SS_FOND_PAGE)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        from widgets.mpv_widget import MpvWidget
        self._lecteur = MpvWidget(self)
        v.addWidget(self._lecteur, stretch=1)

        # ── transport ───────────────────────────────────────────────────────
        transport = QHBoxLayout()
        transport.setContentsMargins(12, 8, 12, 0)
        transport.setSpacing(10)

        self._bouton_pause = QPushButton("⏸")
        self._bouton_pause.setFixedSize(34, 30)
        self._bouton_pause.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._bouton_pause.setToolTip("Lecture / pause — Espace")
        self._bouton_pause.clicked.connect(self.basculer_pause)
        transport.addWidget(self._bouton_pause)

        self._barre = QSlider(Qt.Orientation.Horizontal)
        self._barre.setRange(0, 0)
        self._barre.setToolTip("Se déplacer dans le clip")
        # Le curseur SUIT la lecture, sauf pendant qu'on le tient : sans ce
        # drapeau, la position relue toutes les deux cents millisecondes le
        # ramenait sous le doigt à chaque fois.
        self._barre.sliderPressed.connect(lambda: setattr(self, "_tenu", True))
        self._barre.sliderReleased.connect(self._relacher)
        # Cliquer AILLEURS que sur le curseur doit aussi déplacer la lecture :
        # c'est le geste qu'on fait d'abord, et il ne produisait rien.
        self._barre.valueChanged.connect(self._sur_valeur)
        transport.addWidget(self._barre, stretch=1)

        self._horloge = QLabel("0:00 / 0:00")
        self._horloge.setFont(QFont(_FONT_MONO, 10))
        self._horloge.setStyleSheet(_SS_MUTED)
        transport.addWidget(self._horloge)
        v.addLayout(transport)

        bas = QHBoxLayout()
        bas.setContentsMargins(12, 6, 12, 10)
        bas.setSpacing(8)
        titre = QLabel(clip.titre)
        titre.setTextFormat(Qt.TextFormat.PlainText)
        titre.setFont(_bold_font(_FONT_SEGOE, 11))
        titre.setStyleSheet(_SS_WHITE)
        bas.addWidget(titre, stretch=1)

        self._copier = QPushButton("Copier le lien")
        self._copier.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._copier.clicked.connect(self.copier_le_lien)
        bas.addWidget(self._copier)

        ouvrir = QPushButton("Ouvrir sur Twitch")
        ouvrir.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        ouvrir.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(clip.url)))
        bas.addWidget(ouvrir)
        v.addLayout(bas)

        # Cinq fois par seconde : assez pour que le curseur ne saute pas,
        # assez peu pour ne rien coûter. Démarré à la première image, pas ici :
        # avant elle, mpv ne connaît ni position ni durée.
        self._tenu = False
        self._suivi = QTimer(self)
        self._suivi.setInterval(200)
        self._suivi.timeout.connect(self._rafraichir_transport)

        self._retour = QTimer(self)
        self._retour.setSingleShot(True)
        self._retour.timeout.connect(
            lambda: self._copier.setText("Copier le lien"))

        self._etat = QLabel("Chargement du clip…")
        self._etat.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._etat.setStyleSheet(_SS_MUTED + _SS_NU)
        v.addWidget(self._etat)

        threading.Thread(target=self._resoudre, daemon=True).start()

    def _resoudre(self) -> None:
        from core import twitch_clips

        try:
            url = _run_coro(twitch_clips.url_de_lecture(self._clip.slug))
        except Exception:                                  # noqa: BLE001
            logger.exception("Clips : lecture impossible")
            url = ""
        self._resolue.emit(url)

    def _lire(self, url: str) -> None:
        if not url:
            self._etat.setText(
                "Ce clip ne se laisse pas lire ici — « Ouvrir sur Twitch ».")
            return
        if self._lecteur is None:
            return
        self._etat.hide()
        self._lecteur.play(url)
        self._suivi.start()

    # -- transport ------------------------------------------------------------

    def _rafraichir_transport(self) -> None:
        """Recale le curseur et l'horloge sur la lecture."""
        if self._lecteur is None:
            return
        duree = self._lecteur.duree()
        # `position()` rend None tant que mpv n'a pas démarré : entre le
        # `play()` et la première image, il ne sait pas encore où il en est.
        position = self._lecteur.position() or 0.0
        if duree > 0 and self._barre.maximum() != int(duree * 10):
            self._barre.setRange(0, int(duree * 10))
        if not self._tenu:
            self._barre.blockSignals(True)
            self._barre.setValue(int(position * 10))
            self._barre.blockSignals(False)
        self._horloge.setText(
            f"{_duree_courte(position)} / {_duree_courte(duree)}")
        self._bouton_pause.setText("▶" if self._lecteur.en_pause() else "⏸")

    def _sur_valeur(self, valeur: int) -> None:
        """Un clic dans la barre déplace la lecture tout de suite.

        Pendant un GLISSÉ on ne cherche pas à chaque pixel : mpv redécoderait
        des dizaines de fois par seconde. C'est le relâchement qui tranche.
        """
        if not self._tenu and self._lecteur is not None:
            self._lecteur.chercher(valeur / 10.0)

    def _relacher(self) -> None:
        self._tenu = False
        if self._lecteur is not None:
            self._lecteur.chercher(self._barre.value() / 10.0)

    def basculer_pause(self) -> None:
        if self._lecteur is None:
            return
        en_pause = not self._lecteur.en_pause()
        self._lecteur.set_pause(en_pause)
        self._bouton_pause.setText("▶" if en_pause else "⏸")

    def copier_le_lien(self) -> None:
        """L'adresse du clip dans le presse-papiers, et on le dit.

        Sans retour visible, on ne sait pas si le clic a porté — et on
        recommence, ou on colle dans le vide.
        """
        QApplication.clipboard().setText(self._clip.url)
        self._copier.setText("Lien copié")
        # Un minuteur PORTÉ par la fenêtre, et non `QTimer.singleShot` : celui-ci
        # survit à sa cible. Fermer le lecteur dans la seconde et demie faisait
        # lever « wrapped C/C++ object has been deleted » au fond de la boucle
        # de Qt, loin du geste qui l'avait causé.
        self._retour.start(1500)

    def keyPressEvent(self, event) -> None:                # type: ignore[override]
        """Espace met en pause, les flèches sautent de cinq secondes."""
        touche = event.key()
        if touche == Qt.Key.Key_Space:
            self.basculer_pause()
            return
        if touche in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if self._lecteur is not None:
                pas = 5.0 if touche == Qt.Key.Key_Right else -5.0
                depuis = self._lecteur.position() or 0.0
                self._lecteur.chercher(max(0.0, depuis + pas))
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:                   # type: ignore[override]
        """Démonte le lecteur dans l'ordre, sinon X tue le processus.

        `stop()` seul ne suffisait pas : la fenêtre native de mpv était encore
        détruite par Qt après coup, sur un identifiant que mpv venait de rendre
        — « BadWindow (invalid Window parameter) » sur X_DestroyWindow, fatal
        pour tout le programme.

        `shutdown()` termine le player en reprenant le gestionnaire d'erreur
        Xlib, puis les trois gestes habituels : masquer AVANT de détacher — un
        widget détaché et visible devient une fenêtre à l'écran — détacher,
        puis seulement programmer la destruction.

        C'est la séquence que le plein écran applique déjà à son lecteur de
        replay ; elle n'avait pas été reprise ici.
        """
        self._suivi.stop()
        # Le retour de « Lien copié » aussi : porté par la fenêtre il ne peut
        # plus lever, mais le laisser battre après la fermeture n'a aucun sens.
        self._retour.stop()
        lecteur, self._lecteur = self._lecteur, None
        if lecteur is not None:
            try:
                lecteur.shutdown()
            except Exception:                              # noqa: BLE001
                logger.debug("Clips : arrêt du lecteur")
            lecteur.hide()
            lecteur.setParent(None)
            lecteur.deleteLater()
        super().closeEvent(event)


#: Place réservée au montant dans une ligne de don. Assez pour « 10 000 € »,
#: le plus large qu'on verra passer sans que la colonne saute.
_LARGEUR_MONTANT_DON = 76

#: Durée du fondu d'arrivée d'un don. Assez pour que l'œil accroche la
#: nouvelle ligne, assez court pour que dix dons en une seconde ne se
#: transforment pas en dix fondus superposés.
_DUREE_FONDU_DON_MS = 220


class _LigneDon(QFrame):
    """Un don dans le fil : qui, combien, pour qui, et ce qu'il a écrit."""

    #: Au-delà, la ligne passe en vert et gagne du corps. Le seuil est bas
    #: exprès : sur onze mille dons, ce sont les cent euros qu'on cherche du
    #: regard, pas les records — ceux-là, on les a déjà entendus à l'antenne.
    _SEUIL_MARQUANT = 100.0

    def __init__(self, don: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        montant = _montant_du_don(don)
        #: Retenus sur la ligne : filtrer se fait en relisant ce qu'elle porte,
        #: sans tenir une seconde liste parallèle qui finirait par diverger.
        self.montant = montant
        self.streamer = str(don.get("streamer") or "")
        marquant = montant >= self._SEUIL_MARQUANT
        # Sélecteur CIBLÉ, comme QFrame#streamCell ailleurs dans le projet.
        # Une règle nue posée sur un widget s'applique aussi à toute sa
        # descendance : le liseré vert se répétait sur CHAQUE étiquette de la
        # ligne — montant, donateur, commentaire — au lieu de border la carte.
        self.setObjectName("ligneDon")
        self.setStyleSheet(
            "QFrame#ligneDon { background: #111111; border: none;"
            " border-radius: 6px;"
            + (" border-left: 3px solid #00ff87; }" if marquant else " }")
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 7, 10, 7)
        v.setSpacing(2)

        haut = QHBoxLayout()
        haut.setContentsMargins(0, 0, 0, 0)
        haut.setSpacing(8)

        somme = QLabel(_fmt_euros(montant))
        somme.setFont(QFont(_FONT_MONO, 12 if marquant else 11, QFont.Weight.Bold))
        somme.setStyleSheet(f"color: {'#00ff87' if marquant else '#ffffff'};"
                            " background: transparent;")
        # Largeur réservée et alignement à droite : sans eux, « 5 € » et
        # « 100 € » n'occupent pas la même place et les donateurs partent en
        # escalier. Une colonne de montants se compare d'un coup d'œil, une
        # suite de montants décalés ne se compare pas du tout. Fonte à
        # chasse fixe pour la même raison — c'est un nombre, pas un mot.
        somme.setAlignment(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
        somme.setMinimumWidth(_LARGEUR_MONTANT_DON)
        haut.addWidget(somme)

        qui = QLabel(str(don.get("donor") or "Anonyme"))
        qui.setFont(QFont(_FONT_SEGOE, 11))
        qui.setStyleSheet(_SS_BLANC_NU)
        # Un pseudo peut faire n'importe quelle longueur : sans élision il
        # pousse le streamer et l'heure hors de la colonne.
        qui.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        haut.addWidget(qui, stretch=1)

        pour = str(don.get("streamer") or "")
        if pour:
            cible = QLabel(f"→ {pour}")
            cible.setFont(QFont(_FONT_SEGOE, 10))
            cible.setStyleSheet(_SS_GRIS_CLAIR_NU)
            haut.addWidget(cible)

        heure = QLabel(_heure_du_don(don))
        heure.setFont(QFont(_FONT_SEGOE, 10))
        heure.setStyleSheet(_SS_GRIS_NU)
        haut.addWidget(heure)
        v.addLayout(haut)

        commentaire = str(don.get("comment") or "").strip()
        if commentaire:
            mot = QLabel(commentaire)
            mot.setFont(QFont(_FONT_SEGOE, 10))
            mot.setStyleSheet(_SS_GRIS_CLAIR_NU)
            mot.setWordWrap(True)
            # Décalé sous le DONATEUR, pas sous le montant : le commentaire
            # lui appartient, et la colonne des sommes doit rester une colonne
            # de sommes. Le retrait vaut la place du montant plus l'espace qui
            # le sépare du nom.
            bas = QHBoxLayout()
            bas.setContentsMargins(_LARGEUR_MONTANT_DON + 8, 0, 0, 0)
            bas.addWidget(mot)
            v.addLayout(bas)


def _montant_du_don(don: dict) -> float:
    """Le montant, quoi qu'annonce le flux. 0 quand c'est illisible."""
    try:
        return float(don.get("amount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _heure_du_don(don: dict) -> str:
    """'16:34' à l'heure locale, ou '' si l'horodatage est absent ou illisible.

    Le flux date en UTC avec un décalage explicite ; l'afficher tel quel
    montrerait 16 h pour un don de 18 h.
    """
    brut = str(don.get("createdAt") or don.get("created_at") or "")
    if not brut:
        return ""
    try:
        quand = datetime.fromisoformat(brut.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)
    return quand.astimezone().strftime("%H:%M")


class _DonsTab(QWidget):
    """Onglet Dons — le fil des donations, poussé en direct.

    C'est le seul endroit du panel qui montre l'événement don par don plutôt
    qu'en agrégat. Le compteur de l'Accueil dit combien ; celui-ci dit qui, et
    surtout ce que les gens écrivent — c'est ce qu'on lit à l'antenne.

    Alimenté par `FluxCagnotte` : l'historique du snapshot à l'ouverture, puis
    chaque don à mesure. Rien n'est demandé au réseau ici.
    """

    #: Lignes gardées. Chacune est un widget avec ses labels ; onze mille dons
    #: en feraient autant, et la fenêtre se figerait. Deux cents, c'est bien
    #: plus que ce qu'on remonte à la main pendant un direct.
    _MAX_LIGNES = 200

    #: Cadence d'égrenage : un don posé tous les tant. Assez lent pour qu'on
    #: voie chaque ligne arriver, assez rapide pour que le fil ne prenne pas
    #: de retard sur la réalité tant que le débit reste ordinaire.
    _EGRENAGE_MS = 70

    #: Retard qu'on accepte de rattraper au rythme d'un don par tour, soit
    #: une vingtaine de dons. En dessous, le fil s'écrit ligne à ligne ; au
    #: delà, l'égrenage accélère pour y revenir.
    #:
    #: Ce n'est PAS un plafond au-delà duquel on viderait tout : c'est ce
    #: qu'il y avait, et mesuré, ça ne servait à rien. Le serveur ne pousse
    #: pas les dons en filet mais par GRAPPES de trente à soixante — relevé
    #: sur le vrai flux — donc le seuil était franchi à chaque grappe et tout
    #: retombait d'un bloc, exactement ce qu'on cherchait à éviter.
    _FENETRE_RATTRAPAGE_MS = 1500

    #: Délai de regroupement des rafraîchissements. Un don coûtait deux
    #: parcours des deux cents lignes — l'état et la liste des streamers — et
    #: il en arrive plusieurs par seconde en rafale : le fil saccadait. Ils
    #: sont désormais faits UNE fois par salve. Assez court pour rester
    #: imperceptible, assez long pour absorber une rafale entière.
    _REGROUPEMENT_MS = 60

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_SS_FOND_PAGE)
        self._vus: set[str] = set()
        #: Horodatage du dernier don posé, pour savoir si ça afflue.
        self._dernier_pose: float = 0.0
        #: Dons reçus mais pas encore posés. Le flux les livre par paquets —
        #: tout ce qui s'est accumulé depuis le vidage précédent — et les
        #: poser d'un bloc les fait APPARAÎTRE d'un bloc : cinq lignes
        #: surgissent ensemble, puis plus rien. C'est ce qui se voyait, et ce
        #: n'est pas de la lenteur mais du à-coup. Ils sont donc égrenés.
        self._en_attente: deque = deque(maxlen=self._MAX_LIGNES)
        self._build()
        self._regroupe = QTimer(self)
        self._regroupe.setSingleShot(True)
        self._regroupe.setInterval(self._REGROUPEMENT_MS)
        self._regroupe.timeout.connect(self._rafraichir)
        self._egrenage = QTimer(self)
        self._egrenage.setInterval(self._EGRENAGE_MS)
        self._egrenage.timeout.connect(self._egrener)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        entete = QHBoxLayout()
        entete.setContentsMargins(0, 0, 0, 0)
        titre = QLabel("Dons")
        titre.setFont(QFont(_FONT_SEGOE, 16, QFont.Weight.Bold))
        titre.setStyleSheet(_SS_BLANC_NU)
        entete.addWidget(titre)
        entete.addStretch()

        self._etat = QLabel("en attente du flux…")
        self._etat.setFont(QFont(_FONT_SEGOE, 10))
        self._etat.setStyleSheet(_SS_GRIS_NU)
        entete.addWidget(self._etat)
        root.addLayout(entete)
        root.addLayout(self._barre_de_filtres())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("border: none; background: transparent;")

        self._contenu = QWidget()
        self._contenu.setStyleSheet(_SS_NU)
        self._liste = QVBoxLayout(self._contenu)
        self._liste.setContentsMargins(0, 0, 4, 0)
        self._liste.setSpacing(6)
        self._liste.addStretch()          # pousse les lignes vers le haut
        self._scroll.setWidget(self._contenu)
        root.addWidget(self._scroll, stretch=1)

    # ── filtres ─────────────────────────────────────────────────────────────

    #: Seuils proposés. Des paliers plutôt qu'une saisie libre : en régie on
    #: cherche « les gros dons », pas « au-dessus de 37 € », et un clic vaut
    #: mieux qu'un champ à remplir pendant que le fil défile.
    #:
    #: L'échelle double puis quintuple, et monte jusqu'à dix mille : un ZEvent
    #: voit passer des dons à cinq chiffres, et s'arrêter à cinq cents laissait
    #: la moitié haute sans filtre — c'est pourtant celle qu'on cherche.
    #:
    #: Les libellés sont DÉRIVÉS, pas écrits : « ≥ 1 000 € » et « ≥ 1000 € »
    #: se seraient glissés dans la même liste, et l'espace des milliers est
    #: déjà l'affaire de `_fmt_euros`.
    _SEUILS = (0.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0)

    _TOUS = "Tous les streamers"

    def _barre_de_filtres(self) -> QHBoxLayout:
        """Streamer et montant plancher, appliqués à tout le fil."""
        barre = QHBoxLayout()
        barre.setContentsMargins(0, 0, 0, 0)
        barre.setSpacing(8)

        self._filtre_streamer = QComboBox()
        self._filtre_streamer.addItem(self._TOUS)
        self._filtre_streamer.setMinimumWidth(220)
        self._filtre_streamer.currentIndexChanged.connect(self._appliquer_filtres)
        barre.addWidget(self._filtre_streamer)

        self._filtre_montant = QComboBox()
        for seuil in self._SEUILS:
            # La VALEUR portée par itemData, pas déduite du libellé : changer
            # « ≥ 50 € » en « 50 € et plus » ne doit rien casser.
            libelle = ("Tous les montants" if seuil <= 0
                       else f"≥ {_fmt_euros(seuil)}")
            self._filtre_montant.addItem(libelle, seuil)
        self._filtre_montant.currentIndexChanged.connect(self._appliquer_filtres)
        barre.addWidget(self._filtre_montant)

        barre.addStretch()
        return barre

    def _lignes(self) -> list:
        """Les lignes du fil, du plus récent au plus ancien (sans le stretch)."""
        items = (self._liste.itemAt(i) for i in range(self._liste.count()))
        widgets = (it.widget() for it in items if it is not None)
        return [w for w in widgets if isinstance(w, _LigneDon)]

    def _seuil(self) -> float:
        valeur = self._filtre_montant.currentData()
        return float(valeur) if isinstance(valeur, (int, float)) else 0.0

    def _streamer_choisi(self) -> str:
        choix = self._filtre_streamer.currentText()
        return "" if choix == self._TOUS else choix

    def _retenue(self, ligne) -> bool:
        """Cette ligne passe-t-elle les deux filtres ?"""
        if ligne.montant < self._seuil():
            return False
        vise = self._streamer_choisi()
        return not vise or ligne.streamer == vise

    def _appliquer_filtres(self) -> None:
        """Rejoue les filtres sur TOUT le fil, pas seulement sur les nouveaux.

        Poser un filtre doit cacher ce qui est déjà à l'écran, sinon il ne
        prendrait effet qu'au don suivant — et sur un seuil élevé, cela peut
        être dans plusieurs minutes.
        """
        for ligne in self._lignes():
            ligne.setVisible(self._retenue(ligne))
        self._rafraichir_etat()

    def _rafraichir_streamers(self) -> None:
        """Tient à jour la liste déroulante des streamers vus dans le fil.

        Reconstruite seulement quand l'ENSEMBLE change : la repeupler à chaque
        don rouvrirait la liste sous le doigt de qui la parcourt. La sélection
        est retrouvée par son texte, l'index ne survivant pas à un tri.
        """
        vus = {ligne.streamer for ligne in self._lignes() if ligne.streamer}
        # Le streamer filtré reste proposé même si son dernier don vient de
        # sortir du fil par élagage. Sans cela la sélection ne se retrouvait
        # plus, retombait sur « Tous », et le filtre s'annulait tout seul —
        # on se serait cru devant un fil non filtré.
        vise = self._streamer_choisi()
        if vise:
            vus.add(vise)
        vus = sorted(vus)
        actuels = [self._filtre_streamer.itemText(i)
                   for i in range(1, self._filtre_streamer.count())]
        if vus == actuels:
            return
        choix = self._filtre_streamer.currentText()
        bloque = self._filtre_streamer.blockSignals(True)
        try:
            self._filtre_streamer.clear()
            self._filtre_streamer.addItem(self._TOUS)
            self._filtre_streamer.addItems(vus)
            rang = self._filtre_streamer.findText(choix)
            self._filtre_streamer.setCurrentIndex(max(0, rang))
        finally:
            self._filtre_streamer.blockSignals(bloque)

    # ── alimentation ────────────────────────────────────────────────────────

    def ajouter_don(self, don: object) -> None:
        """Un don qui vient d'arriver : il prend la file, et sera égrené.

        Il n'est pas posé tout de suite, et c'est le but : le flux les livre
        par paquets, les poser sur-le-champ les ferait apparaître ensemble.
        """
        if not isinstance(don, dict):
            return
        self._en_attente.append(don)
        if not self._egrenage.isActive():
            # Le premier ne fait pas antichambre : sur un fil calme, attendre
            # soixante-dix millisecondes pour rien se verrait.
            self._egrener()
            self._egrenage.start()

    def _egrener(self) -> None:
        """Pose le don suivant. Rattrape d'un coup si le retard s'accumule."""
        if not self._en_attente:
            self._egrenage.stop()
            return
        # UN par tour tant que la file tient dans la fenêtre : c'est le régime
        # normal, et c'est lui qui donne le fil qui s'écrit ligne à ligne.
        # Au-delà seulement, on accélère juste ce qu'il faut pour l'y ramener.
        #
        # Le débit n'est PAS proportionnel à la file en régime normal : une
        # règle purement proportionnelle décroît géométriquement — la file
        # fond vite puis traîne — et une grappe de cinquante mettait six
        # secondes à s'écouler au lieu de deux.
        absorbable = max(1, self._FENETRE_RATTRAPAGE_MS // self._EGRENAGE_MS)
        en_attente = len(self._en_attente)
        combien = 1 if en_attente <= absorbable else math.ceil(en_attente / absorbable)
        for _ in range(combien):
            if not self._en_attente:
                break
            self._poser(self._en_attente.popleft(), en_tete=True, anime=True)
        self._planifier_rafraichissement()

    def poser_historique(self, dons: object) -> None:
        """Le lot du snapshot, à l'ouverture ou après une reconnexion.

        Le flux le rend du plus récent au plus ancien : on le parcourt à
        l'endroit en insérant chacun plus bas, sinon l'ordre s'inverserait.
        """
        if not isinstance(dons, list):
            return
        for don in dons:
            if isinstance(don, dict):
                # Sans fondu : ce sont des dons déjà passés, les voir
                # apparaître un à un ferait croire à cent dons d'un coup.
                self._poser(don, en_tete=False, anime=False)
        self._planifier_rafraichissement()

    def signaler_etat(self, ouvert: bool) -> None:
        """Le socket vient de s'ouvrir ou de tomber."""
        self._ouvert = ouvert
        self._rafraichir_etat()

    def _planifier_rafraichissement(self) -> None:
        """Regroupe les parcours de liste d'une salve de dons en un seul.

        Le timer n'est PAS redémarré s'il tourne déjà, et c'est tout l'enjeu :
        `start()` sur un timer actif le repousse, et lors d'un afflux — où les
        dons tombent plus vite que le délai — il aurait été repoussé
        indéfiniment. Liste des streamers et compteur seraient restés figés
        pendant toute la vague, c'est-à-dire précisément quand on les regarde.

        Tel quel, c'est un rafraîchissement par fenêtre, quoi qu'il arrive.
        """
        if not self._regroupe.isActive():
            self._regroupe.start()

    def _rafraichir(self) -> None:
        self._rafraichir_streamers()
        self._rafraichir_etat()

    def _poser(self, don: dict, en_tete: bool, anime: bool = False) -> None:
        """Insère la ligne, sauf si ce don est déjà au fil.

        Le dédoublonnage n'est pas un luxe : une reconnexion renvoie un
        snapshot qui recouvre des dons déjà reçus en direct, et ils
        apparaîtraient deux fois.
        """
        cle = str(don.get("id") or "")
        if cle:
            if cle in self._vus:
                return
            self._vus.add(cle)
        # `count() - 1` : le stretch final occupe la dernière position et doit
        # le rester, sinon les lignes se collent au bas de la page.
        rang = 0 if en_tete else max(0, self._liste.count() - 1)
        ligne = _LigneDon(don, self._contenu)
        # Posée déjà filtrée : l'insérer visible puis la cacher la ferait
        # clignoter à chaque don écarté par le filtre en cours.
        retenue = self._retenue(ligne)
        ligne.setVisible(retenue)
        self._liste.insertWidget(rang, ligne)
        if anime and retenue and self._peut_animer():
            self._faire_apparaitre(ligne)
        self._elaguer()

    def _peut_animer(self) -> bool:
        """Y a-t-il de la place pour un fondu, ou est-ce que ça afflue ?

        Les dons arrivent parfois par dizaines à la seconde. Un fondu par don
        signifierait autant de rendus HORS ÉCRAN simultanés — le fil ralentit
        là où il devrait aller vite, et l'animation, superposée à elle-même
        dix fois, ne se lit plus de toute façon.

        Au-delà de la cadence d'un fondu, on pose donc les lignes sèchement :
        c'est ce que fait aussi l'historique. L'animation est un agrément des
        moments calmes, pas un dû.
        """
        maintenant = time.monotonic()
        assez_espace = (maintenant - self._dernier_pose) * 1000 >= _DUREE_FONDU_DON_MS
        self._dernier_pose = maintenant
        return assez_espace

    @staticmethod
    def _faire_apparaitre(ligne: QWidget) -> None:
        """Fondu d'entrée d'une ligne.

        Effet ET animation sont PARENTÉS À LA LIGNE, et c'est la seule chose
        qui compte ici. Le fil élague en permanence : une ligne peut être
        détruite en plein fondu, et une animation qui lui survivrait
        écrirait dans un effet déjà libéré — segfault, pas exception. Parentée,
        elle est détruite avec sa cible, et Qt l'arrête au passage.

        L'effet est libéré à la fin du fondu : deux cents lignes en portant
        chacune un forceraient autant de rendus hors écran à chaque repeint,
        pour une animation terminée depuis longtemps. `deleteLater` et non
        `setGraphicsEffect(None)` — on est dans le rappel `finished` de
        l'animation, détruire sa cible sur place la couperait sous elle.
        """
        effet = QGraphicsOpacityEffect(ligne)
        effet.setOpacity(0.0)
        ligne.setGraphicsEffect(effet)
        anim = QPropertyAnimation(effet, b"opacity", ligne)
        anim.setDuration(_DUREE_FONDU_DON_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(effet.deleteLater)
        anim.start()

    def _elaguer(self) -> None:
        """Retire les lignes les plus anciennes au-delà du plafond."""
        while self._liste.count() - 1 > self._MAX_LIGNES:
            item = self._liste.takeAt(self._liste.count() - 2)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Masquer AVANT de détacher : un widget visible dont le parent
                # passe à None devient une fenêtre de premier niveau, qui
                # s'ouvre seule au milieu de l'écran. C'est la règle du dépôt,
                # et test_pas_de_fenetres_parasites la fait respecter.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _rafraichir_etat(self) -> None:
        """Le compte porte sur ce qu'on VOIT, pas sur ce qui est en mémoire.

        Annoncer « 200 dons » au-dessus d'une page qui en montre trois, parce
        qu'un filtre est posé, ferait douter du filtre ou du compteur.
        """
        lignes = self._lignes()
        # `isHidden`, et non `isVisible` : celui-ci est faux pour tout enfant
        # d'une fenêtre pas encore affichée, ce qui n'a rien à voir avec le
        # filtre. `isHidden` ne dit que ce qu'on a nous-même masqué.
        montres = sum(1 for ligne in lignes if not ligne.isHidden())
        etat = "en direct" if getattr(self, "_ouvert", False) else "flux hors ligne"
        if montres == len(lignes):
            self._etat.setText(f"{montres} derniers dons · {etat}")
        else:
            self._etat.setText(f"{montres} sur {len(lignes)} dons · {etat}")


class _StatsTab(QWidget):
    """Onglet Stats — chiffres clés, courbes, et un seul classement lisible.

    L'ancienne disposition disait trois fois la même chose : un graphe des top
    viewers, un tableau des cagnottes et deux barres LAN/Online. On garde un
    seul tableau, trié comme on veut, où chaque colonne porte sa propre échelle
    de comparaison.

    Le classement est aussi le meilleur endroit d'où AGIR : c'est là qu'on voit
    qui monte. Double-clic pour la fiche, clic droit pour le reste — les mêmes
    gestes que sur les cartes de l'onglet Streamers, sans quitter le tableau.
    """

    sheet_requested  = pyqtSignal(str)   # twitch_login
    stream_requested = pyqtSignal(str)
    grid_requested   = pyqtSignal(str)
    favori_change    = pyqtSignal(str, bool)

    #: Colonnes du classement : (titre, clé de tri, alignement à droite).
    _COLS = [
        ("#", "", False),
        ("Streamer", "nom", False),
        ("Lieu", "lieu", False),
        ("Jeu", "jeu", False),
        ("Depuis", "duree", True),
        ("Objectifs", "objectifs", True),
        ("Viewers", "viewers", True),
        ("+/h", "tendance", True),
        ("Cagnotte", "cagnotte", True),
        # « + €/h » et non « +/h » : deux colonnes du même nom, l'une après
        # Viewers et l'autre après Cagnotte, se lisent comme un doublon.
        ("+ €/h", "tendance_dons", True),
    ]

    #: Ce que dit une colonne dont on ne devine pas le contenu, et surtout
    #: pourquoi elle peut être vide. Un tiret sans explication passe pour une
    #: panne : ces deux-là n'ont légitimement rien à montrer une bonne partie
    #: du temps, et il faut le dire là où la question se pose.
    _INFOBULLES = {
        _C_DUREE: "Depuis quand la chaîne est en direct.",
        _C_OBJ: "Objectifs de dons atteints sur ceux annoncés.\n"
                "Chargés pour les chaînes en tête du classement et pour vos "
                "favoris.\n"
                "Beaucoup de streamers ne les publient qu'à l'approche de "
                "l'événement.",
        _C_VUE: "Spectateurs en ce moment.",
        _C_TEND: "Spectateurs gagnés ou perdus depuis le début de la "
                 "session.\n"
                 "Il faut cinq minutes d'observation avant que la colonne se "
                 "remplisse,\n"
                 "et elle repart de zéro à chaque lancement de ZLink : aucune "
                 "API ne donne\n"
                 "l'historique d'une chaîne, ZLink le constitue en regardant.",
        _C_DON: "Ce que la chaîne a récolté depuis le début de l'événement.",
    }

    #: Largeur maximale du contenu. Au-delà, l'œil perd la ligne entre le nom
    #: et le chiffre qui lui correspond — le défaut de l'ancienne version, où
    #: mille deux cents pixels de vide séparaient les deux.
    _LARGEUR_MAX = 1500

    _AVATAR = 22

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._charts_view: QWebEngineView | None = None
        self._charts_ready = False
        self._charts_payload: str | None = None   # dernière série non poussée
        self._historique: HistoryStore | None = None  # pour rejouer au recadrage
        self._streamers: list[StreamerInfo] = []
        #: login → objectifs de dons, semés par la fenêtre. Vide tant que
        #: l'onglet Goals n'a rien reçu : la colonne affiche alors « — ».
        self._goals: dict = {}
        self._filtre: str = "tous"
        self._tri: str = "cagnotte"
        self._decroissant: bool = True
        self._build()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        exterieur = QHBoxLayout(self)
        exterieur.setContentsMargins(12, 12, 12, 12)
        exterieur.addStretch()

        colonne = QWidget()
        colonne.setMaximumWidth(self._LARGEUR_MAX)
        _conteneur_nu(colonne)
        exterieur.addWidget(colonne, stretch=1)
        exterieur.addStretch()

        root = QVBoxLayout(colonne)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addWidget(self._build_bandeau())
        root.addWidget(self._build_courbes())
        root.addWidget(self._build_classement(), stretch=1)

    def _build_bandeau(self) -> QWidget:
        bandeau = QWidget()
        _conteneur_nu(bandeau)
        h = QHBoxLayout(bandeau)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self._t_cagnotte = _Tuile("CAGNOTTE", "#00ff87")
        self._t_viewers = _Tuile("VIEWERS", "#38bdf8")
        self._t_direct = _Tuile("EN DIRECT", "#ffffff")
        self._t_lieux = _Tuile("SUR PLACE / À DISTANCE", "#f5c518")
        for tuile in (self._t_cagnotte, self._t_viewers,
                      self._t_direct, self._t_lieux):
            h.addWidget(tuile, stretch=1)
        return bandeau

    def _build_courbes(self) -> QWidget:
        """Les courbes, et à leur place une seule ligne quand il n'y a rien.

        Hors event elles n'ont aucun point à tracer : 420 px d'axes vides
        mangeaient la moitié de la page, et le pavé de repli en occupait
        encore quatre-vingts.
        """
        boite = QWidget()
        _conteneur_nu(boite)
        v = QVBoxLayout(boite)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Le switch de cadrage. Par défaut on suit les relevés — c'est ce qu'on
        # regarde en direct. Coché, l'axe couvre la course entière jusqu'au
        # lundi 1 h : les éditions passées s'y tracent jusqu'au bout, et la
        # courbe de l'année avance derrière, à mesure.
        self._toute_la_course = QCheckBox("Toute la course, jusqu'au lundi 1 h")
        self._toute_la_course.setFont(QFont(_FONT_SEGOE, 10))
        self._toute_la_course.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._toute_la_course.setStyleSheet(
            "QCheckBox { color: #888888; background: transparent; "
            "padding: 6px 2px; }"
            "QCheckBox:hover { color: #ffffff; }")
        self._toute_la_course.toggled.connect(self._recadrer_les_graphes)
        v.addWidget(self._toute_la_course, 0, Qt.AlignmentFlag.AlignRight)

        self._charts_view = QWebEngineView()
        self._charts_view.setFixedHeight(420)
        self._charts_view.loadFinished.connect(self._on_charts_loaded)
        # Fichier écrit une fois : setHtml() prive la page d'une URL de base,
        # ce qui compliquerait tout ajout de ressource locale par la suite.
        shell = _CHARTJS_PATH.parent / "stats_chart.html"
        try:
            shell.write_text(self._build_charts_html(), encoding="utf-8")
            self._charts_view.setUrl(QUrl.fromLocalFile(str(shell)))
        except OSError as exc:
            logger.warning("Page des graphiques non écrite — %s", exc)
        v.addWidget(self._charts_view)
        self._charts_view.setVisible(False)

        self._charts_empty = QLabel(
            "Les courbes apparaîtront dès les premières données de l'édition."
        )
        self._charts_empty.setFont(QFont(_FONT_SEGOE, 10))
        self._charts_empty.setStyleSheet(
            "color: #555555; background: transparent; border: none;")
        v.addWidget(self._charts_empty)
        return boite

    def _build_classement(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        vl = QVBoxLayout(frame)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(8)
        vl.addLayout(self._build_entete_classement())

        self._ranking_table = QTableWidget(0, len(self._COLS))
        self._ranking_table.setHorizontalHeaderLabels([c[0] for c in self._COLS])
        self._configurer_table()
        vl.addWidget(self._ranking_table, stretch=1)
        return frame

    def _build_entete_classement(self) -> QHBoxLayout:
        ligne = QHBoxLayout()
        ligne.setSpacing(8)
        titre = QLabel("CLASSEMENT")
        titre.setFont(_bold_font(_FONT_SEGOE, 10))
        titre.setStyleSheet(_SS_GREEN_SPACED)
        ligne.addWidget(titre)
        self._compte_lbl = QLabel("")
        self._compte_lbl.setFont(QFont(_FONT_SEGOE, 10))
        self._compte_lbl.setStyleSheet(_SS_GREY_CLEAR)
        ligne.addWidget(self._compte_lbl)
        ligne.addStretch()
        self._boutons_filtre: dict[str, QPushButton] = {}
        for cle, libelle in (("tous", "Tous"), ("lan", "Sur place"),
                             ("remote", "À distance")):
            b = QPushButton(libelle)
            b.setFont(QFont(_FONT_SEGOE, 10))
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setFixedHeight(24)
            b.clicked.connect(lambda _=False, k=cle: self._appliquer_filtre(k))
            self._boutons_filtre[cle] = b
            ligne.addWidget(b)
        self._peindre_filtres()
        return ligne

    def _configurer_table(self) -> None:
        table = self._ranking_table
        hh = table.horizontalHeader()
        hh.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._sur_clic_entete)
        # Ce sont les DEUX colonnes chiffrées qui s'étirent, pas le texte :
        # elles portent une barre, et la place en trop devient de la longueur
        # de barre, donc de la comparaison lisible. Étirer le nom ou le jeu
        # rouvrait le gouffre de l'ancienne version — cinq cents pixels de vide
        # entre le pseudo et le chiffre qui lui correspond.
        for colonne in (_C_VUE, _C_DON):
            hh.setSectionResizeMode(colonne, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(_C_RANG, 38)
        table.setColumnWidth(_C_NOM, 200)
        table.setColumnWidth(_C_LIEU, 90)
        table.setColumnWidth(_C_JEU, 190)
        table.setColumnWidth(_C_DUREE, 80)
        table.setColumnWidth(_C_OBJ, 80)
        table.setColumnWidth(_C_TEND, 70)
        for col in (_C_DUREE, _C_OBJ, _C_VUE, _C_TEND, _C_DON):
            item = table.horizontalHeaderItem(col)
            if item is not None:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for col, texte in self._INFOBULLES.items():
            item = table.horizontalHeaderItem(col)
            if item is not None:
                item.setToolTip(texte)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        # Sans ça un libellé trop long pour sa colonne passe à la ligne et
        # double la hauteur de la rangée, cassant l'alignement de la grille.
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setIconSize(QSize(self._AVATAR, self._AVATAR))
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # Le rectangle de focus se dessine PAR-DESSUS la ligne, en pointillés
        # clairs : illisible sur fond sombre, et redondant avec la sélection.
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Par constante, jamais par numéro : les colonnes ajoutées depuis ont
        # décalé Viewers et Cagnotte, et les barres se peignaient sur « Depuis »
        # et « Objectifs », qui n'ont pas de valeur à comparer. Elles avaient
        # donc simplement disparu du tableau.
        table.setItemDelegateForColumn(_C_VUE, _BarreDeCellule("#38bdf8", table))
        table.setItemDelegateForColumn(_C_DON, _BarreDeCellule("#00ff87", table))
        table.setStyleSheet(_STYLE_CLASSEMENT)
        table.cellDoubleClicked.connect(self._sur_double_clic)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._sur_clic_droit)

    # -- interactions ---------------------------------------------------------

    def login_de_la_ligne(self, ligne: int) -> str:
        """Le login porté par une ligne, ou une chaîne vide si elle n'existe pas.

        Le tableau se retrie et se refiltre sans arrêt : un indice de ligne ne
        vaut que pour l'instant où on le lit.
        """
        item = self._ranking_table.item(ligne, _C_NOM)
        return "" if item is None else str(
            item.data(Qt.ItemDataRole.UserRole) or "")

    def _sur_double_clic(self, ligne: int, _colonne: int) -> None:
        login = self.login_de_la_ligne(ligne)
        if login:
            self.sheet_requested.emit(login)

    def _sur_clic_droit(self, position) -> None:
        menu = self.menu_de_la_ligne(
            self._ranking_table.rowAt(position.y()))
        if menu is not None:
            menu.exec(self._ranking_table.viewport().mapToGlobal(position))

    def menu_de_la_ligne(self, ligne: int) -> "QMenu | None":
        """Ce qu'on peut faire d'une chaîne, depuis le classement.

        Rendu plutôt qu'exécuté : un menu qui s'ouvre tout seul ne se teste
        qu'en le cliquant.
        """
        login = self.login_de_la_ligne(ligne)
        if not login:
            return None
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)

        # En-tête non cliquable : il DIT de qui on parle et où en sont ses
        # objectifs. « Objectifs de dons » était une entrée à part, qui ouvrait
        # la fiche — exactement ce que fait « Ouvrir la fiche » deux lignes
        # plus bas. Deux entrées pour un seul geste, alors que le compte
        # n'était qu'une information. Le menu du Programme ouvre déjà de cette
        # façon.
        menu.addAction(self._entete_de_ligne(menu, login))
        menu.addSeparator()

        # Mêmes glyphes que les autres menus de l'application : on reconnaît
        # une action à sa marque avant d'avoir lu son libellé.
        menu.addAction("▶  Regarder en plein écran").triggered.connect(
            lambda: self.stream_requested.emit(login))
        menu.addAction("⊞  Ajouter à la grille").triggered.connect(
            lambda: self.grid_requested.emit(login))
        menu.addSeparator()

        etoile = "★" if favorites.is_favorite(login) else "☆"
        menu.addAction(
            f"{etoile}  " + ("Retirer des favoris"
                             if favorites.is_favorite(login)
                             else "Ajouter aux favoris")
        ).triggered.connect(lambda: self._basculer_favori(login))
        menu.addSeparator()
        menu.addAction("ℹ  Ouvrir la fiche").triggered.connect(
            lambda: self.sheet_requested.emit(login))
        return menu

    def _entete_de_ligne(self, menu: QMenu, login: str) -> QAction:
        """Le titre du menu : de qui on parle, et où en sont ses objectifs.

        Grisé, donc non cliquable — c'est une information, pas une action. Les
        objectifs ne sont chargés que pour le haut du classement et les
        favoris, il n'existe aucun appel à la demande : quand ils manquent,
        l'en-tête dit pourquoi et comment y remédier plutôt que de laisser
        croire que la chaîne n'en publie aucun.
        """
        nom = login
        for s_ in self._streamers:
            if s_.twitch_login == login:
                nom = s_.display
                break
        buts = self._goals.get(login) or []
        if buts:
            faits = sum(1 for b in buts if _objectif_atteint(b))
            texte = f"{nom}  —  {faits}/{len(buts)} objectifs"
        else:
            texte = f"{nom}  —  objectifs chargés pour les favoris"
        action = QAction(texte, menu)
        action.setEnabled(False)
        return action

    def _basculer_favori(self, login: str) -> None:
        etat = favorites.toggle(login)
        self.favori_change.emit(login, etat)
        # L'étoile est peinte dans la cellule du nom : sans redessin, elle
        # n'apparaîtrait qu'au prochain sondage.
        self._remplir()

    def _appliquer_filtre(self, cle: str) -> None:
        self._filtre = cle
        self._peindre_filtres()
        self._remplir()

    def _peindre_filtres(self) -> None:
        for cle, bouton in self._boutons_filtre.items():
            actif = cle == self._filtre
            bouton.setStyleSheet(
                "QPushButton { border-radius: 12px; padding: 2px 12px; "
                + ("background: #16341f; color: #00ff87; "
                   "border: 1px solid #00ff87; }"
                   if actif else
                   "background: #161616; color: #888888; "
                   "border: 1px solid #262626; }")
                + "QPushButton:hover { color: #ffffff; }"
            )

    def _sur_clic_entete(self, colonne: int) -> None:
        """Trie sur la colonne cliquée, et inverse si c'est déjà la sienne."""
        if not 0 <= colonne < len(self._COLS):
            return
        cle = self._COLS[colonne][1]
        if not cle:
            return          # la colonne du rang n'est pas un critère
        if cle == self._tri:
            self._decroissant = not self._decroissant
        else:
            self._tri = cle
            # Un nom se lit de A à Z, un nombre se regarde par le haut.
            self._decroissant = cle not in ("nom", "jeu", "lieu")
        self._marquer_entetes()
        self._remplir()

    def _marquer_entetes(self) -> None:
        """Une flèche sur la colonne qui trie : sinon rien ne dit d'où vient
        l'ordre affiché."""
        fleche = " ▼" if self._decroissant else " ▲"
        for i, (titre, cle, _droite) in enumerate(self._COLS):
            item = self._ranking_table.horizontalHeaderItem(i)
            if item is not None:
                item.setText(titre + (fleche if cle and cle == self._tri else ""))

    # -- données --------------------------------------------------------------

    def update_streamers(self, streamers: list[StreamerInfo]) -> None:
        """Rafraîchit les chiffres clés et le classement."""
        self._streamers = list(streamers)
        self._mettre_a_jour_bandeau()
        self._marquer_entetes()
        self._remplir()

    def _mettre_a_jour_bandeau(self) -> None:
        s = self._streamers
        en_direct = [x for x in s if x.online]
        sur_place = [x for x in s if _est_sur_place(x.location)]
        cagnotte = sum(x.donation for x in s)
        viewers = sum(x.viewers for x in en_direct)
        self._t_cagnotte.set_valeur(_fmt_euros(cagnotte),
                                    "cumul des participants")
        self._t_viewers.set_valeur(f"{viewers:,}".replace(",", " "),
                                   "cumulés sur les directs")
        self._t_direct.set_valeur(str(len(en_direct)),
                                  f"sur {len(s)} participants")
        self._t_lieux.set_valeur(f"{len(sur_place)} / {len(s) - len(sur_place)}",
                                 "sur place / à distance")

    def _selection(self) -> list[StreamerInfo]:
        """La liste filtrée puis triée, telle qu'elle sera affichée."""
        if self._filtre == "lan":
            retenus = [s for s in self._streamers if _est_sur_place(s.location)]
        elif self._filtre == "remote":
            retenus = [s for s in self._streamers
                       if not _est_sur_place(s.location)]
        else:
            retenus = list(self._streamers)
        cles = {
            "nom": lambda s: s.display.lower(),
            "lieu": lambda s: (s.location or "").lower(),
            "jeu": lambda s: (s.game or "").lower(),
            "viewers": lambda s: (s.viewers, s.donation),
            # Avant l'event toutes les cagnottes valent zéro : le tri rendait
            # alors l'ordre alphabétique de l'API, illisible. Les viewers
            # départagent, eux.
            "cagnotte": lambda s: (s.donation, s.viewers),
            "duree": lambda s: (_duree_de(s), s.viewers),
            "objectifs": lambda s: (_part_objectifs(self._goals, s), s.viewers),
            "tendance": lambda s: (_tendance_de(s), s.viewers),
            "tendance_dons": lambda s: (_tendance_dons_de(s), s.donation),
        }
        return sorted(retenus, key=cles.get(self._tri, cles["cagnotte"]),
                      reverse=self._decroissant)

    def seed_goals(self, cache: dict) -> None:
        """Objectifs par login, tels que le panel les a reçus."""
        self._goals = dict(cache or {})
        if self._streamers:
            self._remplir()

    def _remplir(self) -> None:
        retenus = self._selection()
        table = self._ranking_table
        table.setRowCount(len(retenus))
        self._compte_lbl.setText(f"· {len(retenus)}")
        max_v = max((s.viewers for s in retenus), default=0) or 1
        max_d = max((s.donation for s in retenus), default=0.0) or 1.0
        for i, s in enumerate(retenus):
            self._ecrire_ligne(i, s, max_v, max_d)

    def _ecrire_ligne(self, i: int, s: StreamerInfo,
                      max_v: int, max_d: float) -> None:
        table = self._ranking_table
        rang = QTableWidgetItem(str(i + 1))
        rang.setForeground(QBrush(QColor("#555555")))
        table.setItem(i, _C_RANG, rang)
        table.setItem(i, _C_NOM, self._cellule_nom(s))

        sur_place = _est_sur_place(s.location)
        lieu = QTableWidgetItem("sur place" if sur_place else "à distance")
        lieu.setForeground(QBrush(QColor("#f5c518" if sur_place else "#666666")))
        table.setItem(i, _C_LIEU, lieu)

        jeu = QTableWidgetItem(s.game if s.online else "hors ligne")
        jeu.setForeground(QBrush(QColor("#999999" if s.online else "#555555")))
        table.setItem(i, _C_JEU, jeu)

        table.setItem(i, _C_DUREE, self._cellule_duree(s))
        table.setItem(i, _C_OBJ, self._cellule_objectifs(s))

        vue = _CelluleNombre(_fmt_viewers(s.viewers) if s.online else "—",
                             float(s.viewers))
        vue.setData(_BarreDeCellule.PART, s.viewers / max_v if s.online else 0.0)
        vue.setForeground(QBrush(QColor("#38bdf8" if s.online else "#555555")))
        vue.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(i, _C_VUE, vue)

        table.setItem(i, _C_TEND, self._cellule_tendance(s))

        don = _CelluleNombre(s.donation_formatted or _fmt_euros(s.donation),
                             s.donation)
        don.setData(_BarreDeCellule.PART, s.donation / max_d)
        don.setForeground(QBrush(QColor("#00ff87" if s.donation else "#555555")))
        don.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(i, _C_DON, don)

        table.setItem(i, _C_TEND_DON, self._cellule_tendance_dons(s))

    def _cellule_tendance_dons(self, s: StreamerInfo) -> QTableWidgetItem:
        """Euros récoltés dans la dernière heure.

        Le total dit où en est une chaîne, pas si elle reçoit EN CE MOMENT.
        C'est pourtant ce qui distingue une grosse cagnotte constituée hier
        d'un palier en train de tomber.
        """
        from core import tendances

        delta = tendances.cagnotte(s.twitch_login)
        sens = _sens_tendance_euros(delta)
        cellule = _CelluleNombre(_texte_tendance_euros(delta), float(delta or 0.0))
        cellule.setForeground(QBrush(QColor(_COULEURS_TENDANCE[sens])))
        cellule.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return cellule

    def _cellule_duree(self, s: StreamerInfo) -> QTableWidgetItem:
        """Depuis combien de temps la chaîne est en direct.

        Le tri se fait sur les SECONDES, pas sur le texte : « 9 h 05 min »
        passerait avant « 12 h 00 min » dans un ordre alphabétique.
        """
        from core import live_uptime

        secondes = live_uptime.depuis(s.twitch_login) if s.online else None
        cellule = _CelluleNombre(
            live_uptime.duree(secondes) if secondes is not None else "—",
            float(secondes or 0.0))
        cellule.setForeground(QBrush(QColor(
            "#c9c9c9" if secondes is not None else "#555555")))
        cellule.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return cellule

    def _cellule_objectifs(self, s: StreamerInfo) -> QTableWidgetItem:
        """« 3/8 » — objectifs atteints sur ceux annoncés.

        Trié sur la PART atteinte et non sur le nombre brut : trois sur quatre
        est un meilleur résultat que trois sur vingt.
        """
        buts = self._goals.get(s.twitch_login) or []
        if not buts:
            cellule = _CelluleNombre("—", -1.0)
            cellule.setForeground(QBrush(QColor("#555555")))
        else:
            faits = sum(1 for b in buts if _objectif_atteint(b))
            cellule = _CelluleNombre(f"{faits}/{len(buts)}", faits / len(buts))
            cellule.setForeground(QBrush(QColor(
                "#00ff87" if faits else "#777777")))
        cellule.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return cellule

    def _cellule_tendance(self, s: StreamerInfo) -> QTableWidgetItem:
        """Viewers gagnés ou perdus dans la dernière heure.

        Une chaîne peut être petite et MONTER : c'est ce que le classement par
        audience ne dit jamais, et c'est souvent là qu'il se passe quelque
        chose.
        """
        from core import tendances

        delta = tendances.viewers(s.twitch_login) if s.online else None
        sens = _sens_tendance(delta)
        cellule = _CelluleNombre(_texte_tendance(delta), float(delta or 0))
        cellule.setForeground(QBrush(QColor(_COULEURS_TENDANCE[sens])))
        cellule.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return cellule

    def _cellule_nom(self, s: StreamerInfo) -> QTableWidgetItem:
        nom = QTableWidgetItem(s.display)
        nom.setIcon(QIcon(self._photo(s)))
        # La ligne doit pouvoir dire de QUI elle parle : le libellé porte le
        # nom affiché, parfois précédé d'une étoile, jamais le login.
        nom.setData(Qt.ItemDataRole.UserRole, s.twitch_login)
        if favorites.is_favorite(s.twitch_login):
            # Coloré : on doit repérer ses favoris d'un coup d'œil dans une
            # liste de trois cents lignes.
            nom.setText(f"★  {s.display}")
            nom.setForeground(QBrush(QColor("#f5c518")))
        elif not s.online:
            nom.setForeground(QBrush(QColor("#777777")))
        return nom

    def _photo(self, s: StreamerInfo) -> QPixmap:
        """Photo du streamer, ou un pixmap vide en attendant qu'elle arrive.

        Les données repassent toutes les 30 s : la ligne se corrige d'elle-même
        sans qu'on ait à réveiller la table.
        """
        from widgets.bigscreen_widget import _avatar_cache
        av = _avatar_cache.get(s.twitch_login, s.display, self._AVATAR,
                               None, s.profile_url)
        return av if av is not None else QPixmap()

    def _build_charts_html(self) -> str:
        """Page squelette : Chart.js et deux graphes VIDES, construite une fois.

        Les 205 Ko de Chart.js etaient relus sur le disque, reinjectes dans un
        nouveau document et reinterpretes a CHAQUE rafraichissement, toutes les
        30 s pendant l'event, page entierement rechargee. Les donnees passent
        desormais par zlUpdate(), qui ne transporte que les points.
        """
        labels_don = data_don = labels_view = data_view = "[]"

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
    // Deux courbes sur un même graphe ne se distinguent plus sans légende.
    // Elle reste discrète : le trait plein est l'édition en cours, le
    // pointillé gris celle d'avant, alignée sur le même temps de course.
    legend: {{
      display: true, position: 'top', align: 'end',
      labels: {{
        color: '#777', boxWidth: 18, boxHeight: 2, padding: 10,
        font: {{ family: 'Cascadia Code', size: 9 }},
      }},
    }},
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

const chartDon = new Chart(document.getElementById('cagnotteChart'), {{
  type: 'line',
  data: {{
    labels: {labels_don},
    datasets: [{{
      label: '2026',
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

const chartView = new Chart(document.getElementById('viewersChart'), {{
  type: 'line',
  data: {{
    labels: {labels_view},
    datasets: [{{
      label: '2026',
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

// Point d'entree unique cote Python : remplace les series en place, sans
// reconstruire les graphes ni recharger la page. 'none' supprime l'animation,
// qui n'a aucun sens pour un rafraichissement periodique.
// Teintes des éditions passées, de la plus récente à la plus ancienne. Toutes
// en pointillé et désaturées : l'édition EN COURS doit rester la seule courbe
// pleine et vive, les autres sont un décor de comparaison.
const REF_COULEURS = ['#9a9a9a', '#7a7a7a', '#5f5f5f', '#4a4a4a'];

function majReferences(chart, refs) {{
  // On reconstruit : le nombre d'éditions retenues peut changer d'un
  // chargement à l'autre, une source pouvant se dérober.
  chart.data.datasets.length = 1;
  var i = 0;
  for (var nom in refs) {{
    var serie = refs[nom];
    // Une série vide n'ajoute rien : ni courbe, ni entrée de légende qu'on
    // chercherait ensuite sur le graphe.
    if (!serie || !serie.length) {{ continue; }}
    chart.data.datasets.push({{
      label: nom, data: serie,
      borderColor: REF_COULEURS[i % REF_COULEURS.length],
      borderDash: [5, 4], borderWidth: 1.5,
      pointRadius: 0, fill: false, tension: 0.1, spanGaps: false,
    }});
    i++;
  }}
}}

window.zlUpdate = function (payload) {{
  var d = JSON.parse(payload);
  chartDon.data.labels = d.ld;
  chartDon.data.datasets[0].data = d.vd;
  majReferences(chartDon, d.rd || {{}});
  chartView.data.labels = d.lv;
  chartView.data.datasets[0].data = d.vv;
  majReferences(chartView, d.rv || {{}});
  chartDon.update('none');
  chartView.update('none');
}};
</script>
</body></html>"""

    # -- chart builders -------------------------------------------------------

    def _recadrer_les_graphes(self) -> None:
        """Rejoue le dernier historique avec le cadrage qui vient d'être choisi."""
        if self._historique is not None:
            self.update_history(self._historique)

    def update_history(self, history: HistoryStore) -> None:
        """Met à jour les graphes Chart.js cagnotte et viewers."""
        # Gardé : basculer le cadrage doit pouvoir tout redessiner sans
        # attendre la prochaine relève, qui met dix minutes à venir.
        self._historique = history
        ts_d, vals_d = history.get_donation_series()
        ts_v, vals_v = history.get_viewers_series()
        has_data = bool(ts_d or ts_v)
        self._charts_empty.setVisible(not has_data)
        if self._charts_view is not None:
            self._charts_view.setVisible(has_data)
        if not has_data:
            return
        if self._charts_view is None:
            return

        from core.history_store import axe_course, course_commencee

        if self._toute_la_course.isChecked():
            # Un axe fixe, du jeudi 18 h au lundi 1 h. Les deux courbes de
            # l'année y sont rééchantillonnées telles quelles — même
            # calendrier, rien à réaligner — et s'interrompent sur leur
            # dernier relevé plutôt que de retomber à zéro.
            axe = axe_course()
            abscisses_d = abscisses_e = abscisses_graphe(axe)
            valeurs_d = history.serie_courante_sur_axe(axe)
            valeurs_v = history.serie_viewers_sur_axe(axe)
            axe_d = axe_v = axe
        else:
            abscisses_d = abscisses_graphe(ts_d)
            abscisses_e = abscisses_graphe(ts_v)
            valeurs_d = [round(v) for v in vals_d]
            valeurs_v = [round(v) for v in vals_v]
            axe_d, axe_v = ts_d, ts_v

        # Les éditions passées sont calées sur le DÉBUT DE LA COURSE, vendredi
        # 18 h. Avant lui, elles n'ont rien à dire : la cagnotte 2026 ouvre
        # vingt-quatre heures plus tôt, et les y superposer les ferait courir
        # sur un jeudi soir qui, pour elles, n'existe pas.
        refs = course_commencee() or self._toute_la_course.isChecked()
        self._charts_payload = json.dumps({
            "ld": abscisses_d,
            "vd": [None if v is None else round(v) for v in valeurs_d],
            "lv": abscisses_e,
            "vv": [None if v is None else round(v) for v in valeurs_v],
            "rd": _references(history.series_editions_alignees(axe_d))
            if refs else {},
            "rv": _references(history.series_viewers_editions_alignees(axe_v))
            if refs else {},
        })
        self._push_charts()

    def _on_charts_loaded(self, ok: bool) -> None:
        """La page est prete : pousser la serie arrivee avant elle, s'il y en a."""
        self._charts_ready = bool(ok)
        if not ok:
            logger.warning("Page des graphiques non chargee")
            return
        self._push_charts()

    def _push_charts(self) -> None:
        """Envoie la derniere serie a la page, si celle-ci est prete.

        Les donnees peuvent arriver avant la fin du chargement : on les garde
        alors en attente plutot que de les perdre dans un runJavaScript sans
        effet.
        """
        if not self._charts_ready or self._charts_payload is None:
            return
        view = self._charts_view
        if view is None:
            return
        # La serie est CONSERVEE : si la page est rechargee un jour, ses graphes
        # repartent vides et loadFinished doit pouvoir la repousser telle quelle.
        view.page().runJavaScript(
            f"window.zlUpdate({json.dumps(self._charts_payload)})"
        )


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
        logo.setStyleSheet(_SS_VERT_NU)
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
        self._status_lbl.setStyleSheet(_SS_GRIS_EFFACE)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(self._status_lbl)

        self._dots = 0
        self._timer = QTimer(self)
        self._timer.setInterval(420)
        self._timer.timeout.connect(self._tick)
        # Démarré par showEvent : inutile de tourner tant que le
        # widget n'est pas affiché.

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

class _VersionLabel(QLabel):
    """Étiquette de version : cliquer ouvre la page des releases."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _HeaderBar(QWidget):
    """En-tête dont un enfant reste centré sur la LARGEUR de la fenêtre.

    Deux ressorts de part et d'autre dans la rangée ne suffisent pas : ils
    centrent entre le logo et le groupe de droite, qui n'ont pas la même
    largeur, et le titre apparaît décalé d'une soixantaine de pixels.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._centered: QWidget | None = None

    def set_centered(self, w: QWidget) -> None:
        self._centered = w
        w.setParent(self)
        w.raise_()
        self._recenter()

    def _recenter(self) -> None:
        w = self._centered
        if w is None:
            return
        w.adjustSize()
        w.move((self.width() - w.width()) // 2,
               (self.height() - w.height()) // 2)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._recenter()


class PanelWindow(QMainWindow):
    """Fenêtre panel fullscreen. show_grid_tab=True en mode dual."""

    stream_selected = pyqtSignal(str)
    grid_selection_changed = pyqtSignal(list)  # list[str] twitch_logins
    settings_changed = pyqtSignal(dict)
    #: (login du présentateur, nom du show) — relayé depuis l'onglet Accueil.
    show_started = pyqtSignal(str, str)
    #: Action demandée depuis la palette et destinée au plein écran.
    action_requested = pyqtSignal(str)
    #: (login, volume 0-100) — réglage venu de la console de mixage.
    #: (login, favori) — l'étoile d'une carte a changé. Ce qui l'affiche
    #: ailleurs — le boîtier Stream Deck, notamment — s'y raccroche.
    favori_change = pyqtSignal(str, bool)
    cell_volume_changed = pyqtSignal(str, int)
    #: Volume du plein écran, réglé depuis la console.
    main_volume_changed = pyqtSignal(int)
    #: Chaîne à retirer des audios épinglés, demandé depuis la console.
    unpin_requested = pyqtSignal(str)
    #: (login, coupé) — coupure d'une chaîne depuis la console.
    cell_mute_changed = pyqtSignal(str, bool)
    #: Coupure du plein écran depuis la console.
    main_mute_changed = pyqtSignal(bool)

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
        # Sans cadre, comme les deux autres fenêtres. showFullScreen() seul
        # laisse la bordure d'un pixel que DWM dessine autour d'une fenêtre
        # décorée : mesuré, et c'est ce liseré qui donnait à l'écran l'air
        # d'être « en fenêtre » plutôt qu'en plein écran.
        #
        # Posé AVANT le premier show() : changer les drapeaux d'une fenêtre
        # déjà affichée détruit et recrée son handle natif.
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(PANEL_STYLE)

        self._tab_btns: list[_TabButton] = []
        self._grid_window: GridWindow | None = None

        # Cache local pour les données les plus récentes
        self._last_streamers: list[StreamerInfo] = []
        # Objectifs bruts par login, pour la fiche d'un participant.
        self._goals_raw: dict[str, list] = {}
        self._last_stats: GlobalStats = GlobalStats(0.0, "—", 0, "offline")

        # Références aux onglets dynamiques
        self._accueil_tab: _AccueilTab
        self._stats_tab: _StatsTab
        self._programme_tab: _ProgrammeTab
        self._goals_tab: _GoalsTab
        self._streamers_tab: _StreamersTab

        self._accueil_refresh_pending: bool = False
        self._lecteurs: list = []          # fenêtres de clips ouvertes
        self._splash: _SplashOverlay | None = None
        self._first_data_received: bool = False

        self._build()

        # Palette de commandes (Ctrl+K) — superposée au widget central.
        names = ["Accueil", "Programme", "Stats", "Goals", "Clips", "Dons",
                 "Streamers", "Mixer"]
        if self._show_grid_tab:
            names.append("Grille")
        self._palette = _CommandPalette(self.centralWidget(), names)
        self._palette.tab_requested.connect(self.switch_to_tab)
        self._palette.action_requested.connect(self._on_palette_action)
        self._palette.stream_requested.connect(self.stream_selected.emit)
        self._palette.grid_requested.connect(self._on_add_to_grid)

        if show_on_init:
            self._move_to_screen(screen)
            self._splash = _SplashOverlay(self)
            self._splash.show()

    def _ouvrir_le_clip(self, clip) -> None:
        """Ouvre le clip dans sa propre fenêtre.

        Gardée dans une liste : sans référence, Python la ramasserait et la
        fenêtre se refermerait aussitôt. Elle s'en retire d'elle-même à la
        fermeture, sinon elles s'accumuleraient toute la soirée.
        """
        lecteur = _LecteurClip(clip, self)
        self._lecteurs.append(lecteur)
        lecteur.finished.connect(
            lambda _r, w=lecteur: self._lecteurs.remove(w)
            if w in self._lecteurs else None)
        lecteur.show()

    def _on_palette_action(self, cle: str) -> None:
        """Exécute une action choisie dans la palette."""
        if cle == "recap":
            self._open_recap()
        else:
            # clip / replay concernent le plein écran, que le panel ne connaît
            # pas : la demande remonte à main.py, qui relie les deux fenêtres.
            self.action_requested.emit(cle)

    def _open_recap(self) -> None:
        """Ouvre le récapitulatif de la session en cours."""
        from windows.recap import RecapDialog
        RecapDialog(self).exec()

    # ── Badge de version ─────────────────────────────────────────────
    def _apply_version_badge(self) -> None:
        """Peint le badge selon qu'une mise à jour attend ou non."""
        maj = getattr(self, "_update_version", "")
        if maj:
            self._version_lbl.setText(f"{_display_version()} → {maj}")
            self._version_lbl.setStyleSheet(
                "#versionBadge { color: #00ff87; background: #0d1f16;"
                " border: 1px solid #16452f; border-radius: 7px;"
                " padding: 1px 6px; }")
            self._version_lbl.setToolTip(_infobulle(
                f"ZLink {maj} est disponible — vous utilisez "
                f"{_display_version()}.\nCliquer pour ouvrir la page de la "
                f"version."))
        else:
            self._version_lbl.setText(_display_version())
            self._version_lbl.setStyleSheet(
                "#versionBadge { color: #888888; background: transparent;"
                " border: 1px solid #262626; border-radius: 7px;"
                " padding: 1px 6px; }")
            self._version_lbl.setToolTip(_infobulle(
                f"Version installée : {_display_version()}\n"
                f"Cliquer pour ouvrir les versions publiées."))
        self._version_lbl.adjustSize()

    def _open_version_page(self) -> None:
        ceder_premier_plan()
        QDesktopServices.openUrl(QUrl(self._version_url))
        for delai in (400, 1200):
            QTimer.singleShot(delai, remonter_navigateur)

    def set_update_available(self, version: str, url: str) -> None:
        """Signale dans l'en-tête qu'une version plus récente existe."""
        self._update_version = version
        if url.startswith("https://github.com/"):
            self._version_url = url
        self._apply_version_badge()

    def _basculer_megaphone(self, actif: bool) -> None:
        """Allume ou éteint le mégaphone, et remet le bouton d'aplomb.

        `basculer` rend l'état RÉELLEMENT obtenu : si le flux refuse de
        s'ouvrir, le bouton se relève au lieu de rester enfoncé sur un
        silence. Les signaux sont bloqués le temps de le corriger, sinon la
        correction rappellerait cette méthode.
        """
        obtenu = self._megaphone.basculer(actif)
        if obtenu != actif:
            bloque = self._megaphone_btn.blockSignals(True)
            try:
                self._megaphone_btn.setChecked(obtenu)
            finally:
                self._megaphone_btn.blockSignals(bloque)

    def _sur_etat_megaphone(self, allume: bool) -> None:
        """Montre ou retire l'étiquette selon que le canal est ouvert."""
        self._megaphone_lbl.setVisible(allume)
        if allume:
            self._sur_parole_megaphone(False)

    def _sur_parole_megaphone(self, parle: bool) -> None:
        """« ça parle » ou « à l'écoute », d'après le niveau mesuré.

        Deux libellés et non un seul affiché par intermittence : une étiquette
        qui disparaît se lit comme un mégaphone qui s'éteint, alors que le
        silence est son état normal.
        """
        self._megaphone_lbl.setText("● annonce" if parle else "à l'écoute")
        self._megaphone_lbl.setStyleSheet(
            _SS_VERT_NU if parle else _SS_GRIS_NU)

    def _sur_echec_megaphone(self, raison: str) -> None:
        """Dit pourquoi le mégaphone est resté muet, plutôt que rien."""
        self.add_feed_event("event", "", raison)

    def fermer_megaphone(self) -> None:
        """Coupe le flux à la fermeture de l'application.

        Sans cela, le lecteur et sa connexion survivent à la fenêtre : mpv
        n'est pas un enfant Qt, rien ne le détruit avec elle.
        """
        self._megaphone.arreter()

    def _tick_clock(self) -> None:
        """Rafraîchit l'horloge de l'en-tête."""
        now = datetime.now()
        txt = now.strftime("%H:%M")
        if self._clock_lbl.text() != txt:
            self._clock_lbl.setText(txt)
        day = f"{_JOURS_FR[now.weekday()][:3].capitalize()} {now.day}"
        if self._clock_date.text() != day:
            self._clock_date.setText(day)

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header 60 px ─────────────────────────────────────────────
        header = _HeaderBar()
        header.setObjectName("header")
        header.setFixedHeight(60)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        hl.setSpacing(12)

        logo = QLabel("ZLink")
        logo.setFont(_bold_font(_FONT_SEGOE, 20))
        logo.setStyleSheet(_SS_GREEN)
        hl.addWidget(logo)

        # Version collée au logo : en régie on ouvre un ticket ou on envoie une
        # capture sans avoir le temps de fouiller les paramètres.
        self._version_lbl = _VersionLabel(_display_version())
        self._version_lbl.setObjectName("versionBadge")
        self._version_lbl.setFont(QFont(_FONT_MONO, 8))
        self._version_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self._version_url = (
            f"https://github.com/{_GH_OWNER}/{_GH_REPO}/releases")
        self._version_lbl.clicked.connect(self._open_version_page)
        self._apply_version_badge()
        hl.addWidget(self._version_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        hl.addStretch()

        # Hors de la rangée : posé par-dessus et centré sur la fenêtre.
        ev = QLabel("ZEvent 2026")
        ev.setFont(QFont(_FONT_SEGOE, 13))
        ev.setStyleSheet(_SS_MUTED + " background: transparent;")
        header.set_centered(ev)

        # Horloge : en régie, l'écran du panel occupe souvent tout le moniteur
        # et masque celle du système. Le programme se lit à l'heure près.
        hl.addSpacing(10)
        self._clock_lbl = QLabel()
        self._clock_lbl.setFont(_bold_font(_FONT_MONO, 15))
        self._clock_lbl.setStyleSheet(
            "color: #e8e8e8; background: transparent; border: none;")
        self._clock_lbl.setToolTip("Heure locale")
        hl.addWidget(self._clock_lbl)
        self._clock_date = QLabel()
        self._clock_date.setFont(QFont(_FONT_SEGOE, 9))
        self._clock_date.setStyleSheet(_SS_MUTED)
        hl.addWidget(self._clock_date)
        self._tick_clock()
        self._clock_timer = QTimer(self)
        # Une seconde par battement pour rester juste au changement de minute,
        # sans repeindre autre chose que deux étiquettes.
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()

        hl.addSpacing(4)

        # Mégaphone : la voix commune du plateau, qu'on veut par-dessus les
        # flux qu'on regarde. Posé contre l'horloge parce que c'est le coin
        # qu'on regarde déjà, et qu'un interrupteur perdu au milieu des
        # onglets ne se retrouve pas quand une annonce commence.
        self._megaphone = Megaphone(self)
        self._megaphone_btn = _mk_header_btn(
            "mdi6.bullhorn-outline", "\U0001f4e2",
            "Mégaphone du ZEvent — annonces du plateau", checkable=True)
        self._megaphone_btn.setEnabled(self._megaphone.disponible)
        if not self._megaphone.disponible:
            self._megaphone_btn.setToolTip(
                "Mégaphone indisponible : libmpv est absent")
        self._megaphone_btn.toggled.connect(self._basculer_megaphone)
        self._megaphone.echec.connect(self._sur_echec_megaphone)
        hl.addWidget(self._megaphone_btn)

        # Le canal est muet la PLUPART du temps : sans cette étiquette, un
        # mégaphone allumé et un mégaphone en panne se ressemblent — dans les
        # deux cas on n'entend rien. Elle dit lequel des deux c'est.
        self._megaphone_lbl = QLabel()
        self._megaphone_lbl.setFont(_bold_font(_FONT_SEGOE, 9))
        self._megaphone_lbl.hide()
        hl.addWidget(self._megaphone_lbl)
        self._megaphone.etat_change.connect(self._sur_etat_megaphone)
        self._megaphone.parole.connect(self._sur_parole_megaphone)

        settings_btn = _mk_header_btn(
            "mdi6.cog-outline", "\u2699", "Paramètres", checkable=True
        )
        settings_btn.clicked.connect(self._toggle_settings)
        self._settings_btn = settings_btn
        hl.addWidget(settings_btn)

        recap_btn = _mk_header_btn(
            "mdi6.text-box-outline", "\u2261", "Récapitulatif de session"
        )
        recap_btn.clicked.connect(self._open_recap)
        hl.addWidget(recap_btn)

        bigscreen_btn = _mk_header_btn(
            "mdi6.fullscreen", "\u29c6", "Mode Big Screen", checkable=True
        )
        bigscreen_btn.clicked.connect(self._toggle_bigscreen)
        self._bigscreen_btn = bigscreen_btn
        hl.addWidget(bigscreen_btn)

        quit_btn = _mk_header_btn("mdi6.close", "\u2715", "Quitter", danger=True)
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

        names = ["Accueil", "Programme", "Stats", "Goals", "Clips", "Dons",
                 "Streamers", "Mixer"]
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
        self._accueil_tab.show_started.connect(self.show_started)
        self._accueil_tab.add_to_grid.connect(self._on_add_to_grid)
        self._stack.addWidget(self._accueil_tab)

        self._programme_tab = _ProgrammeTab()
        # Le toast de rappel disparaît en quelques secondes : on archive aussi
        # l'entrée dans le fil, consultable après coup. Le signal existait mais
        # n'était connecté nulle part.
        self._programme_tab.reminder_triggered.connect(
            lambda _name, msg: self.add_feed_event("event", "", msg)
        )
        # La frise de l'Accueil propose le rappel ; l'onglet Programme le
        # détient et l'écrit. Sans ce relais, chacun tiendrait son propre état
        # et les deux vues se contrediraient.
        self._accueil_tab.rappel_bascule.connect(
            self._programme_tab.basculer_rappel_par_cle)
        self._stack.addWidget(self._programme_tab)

        self._stats_tab = _StatsTab()
        self._stack.addWidget(self._stats_tab)

        self._goals_tab = _GoalsTab()
        self._stack.addWidget(self._goals_tab)

        # L'ordre de la pile suit celui des boutons : une page insérée d'un
        # seul côté décale toutes les suivantes, et chaque onglet ouvre alors
        # celui de son voisin.
        self._clips_tab = _ClipsTab()
        self._clips_tab.clip_choisi.connect(self._ouvrir_le_clip)
        self._stack.addWidget(self._clips_tab)

        # Inséré ICI, entre Clips et Streamers, exactement comme dans `names` :
        # la pile et les boutons sont appariés par INDICE (voir switch_to_tab).
        self._dons_tab = _DonsTab()
        self._stack.addWidget(self._dons_tab)

        self._streamers_tab = _StreamersTab()
        self._streamers_tab.grid_selection_changed.connect(self.grid_selection_changed)
        self._streamers_tab.sheet_requested.connect(self.open_streamer_sheet)
        # Le classement agit sur les mêmes leviers que les cartes : c'est là
        # qu'on voit qui monte, autant pouvoir y faire quelque chose.
        self._stats_tab.sheet_requested.connect(self.open_streamer_sheet)
        self._stats_tab.stream_requested.connect(self.stream_selected)
        self._stats_tab.grid_requested.connect(self._on_add_to_grid)
        self._stats_tab.favori_change.connect(self.favori_change)
        self._stats_tab.favori_change.connect(
            lambda login, _etat: self._streamers_tab.rafraichir_favori(login))
        self._streamers_tab.favori_change.connect(self.favori_change)
        self._stack.addWidget(self._streamers_tab)

        self._mixer_tab = _MixerTab()
        self._mixer_tab.volume_changed.connect(self.cell_volume_changed)
        self._mixer_tab.main_volume_changed.connect(self.main_volume_changed)
        self._mixer_tab.unpin_requested.connect(self.unpin_requested)
        self._mixer_tab.mute_changed.connect(self.cell_mute_changed)
        self._mixer_tab.main_mute_changed.connect(self.main_mute_changed)
        self._stack.addWidget(self._mixer_tab)

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
        # Déclare la fenêtre comme plein écran auprès du shell : sans ça, la
        # barre des tâches de son écran reste par-dessus.
        mark_fullscreen(self)
        logger.info("Panel ouverte sur %s (%dx%d)", screen.name(), g.width(), g.height())

    # -- public API -----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._splash is not None and self._splash.isVisible():
            self._splash.resize(self.size())
        pal = getattr(self, "_palette", None)
        if pal is not None and pal.isVisible():
            pal.resize(pal.parentWidget().size())

    def update_streamers(
        self,
        streamers: list[StreamerInfo],
        selected_logins: list[str] | None = None,
    ) -> None:
        """Propagé depuis DataManager.streamers_updated."""
        self._mixer_tab.set_displays(
            {s.twitch_login: s.display for s in streamers if s.twitch_login})
        # Historique par streamer : l'API ne publie qu'un cumul instantané, la
        # courbe de la fiche se construit donc au fil de la session.
        from windows.streamer_sheet import note_donation
        for st in streamers:
            if st.twitch_login and st.donation:
                note_donation(st.twitch_login, st.donation)
        if not self._first_data_received:
            self._first_data_received = True
            if self._splash is not None:
                self._splash.dismiss()
        self._last_streamers = streamers
        self._schedule_accueil_refresh()
        self._goals_tab.set_streamers(streamers)
        self._clips_tab.set_streamers(streamers)
        self._streamers_tab.refresh(streamers, selected_logins or [])
        self._stats_tab.update_streamers(streamers)
        gdoc_display = {s.gdoc_id: s.display for s in streamers if s.gdoc_id}
        gdoc_login = {s.gdoc_id: s.twitch_login for s in streamers if s.gdoc_id}
        self._programme_tab.set_gdoc_map(gdoc_display, gdoc_login)
        self._bigscreen.update_streamers(streamers)
        self._palette.set_streamers(streamers)

    def update_stats(self, stats: GlobalStats) -> None:
        """Propagé depuis DataManager.global_stats_updated."""
        self._last_stats = stats
        self._schedule_accueil_refresh()
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
        self._clips_tab.set_streamers(streamers)
        self._streamers_tab.refresh(streamers, selected_logins or [])
        self._stats_tab.update_streamers(streamers)
        gdoc_display = {s.gdoc_id: s.display for s in streamers if s.gdoc_id}
        gdoc_login = {s.gdoc_id: s.twitch_login for s in streamers if s.gdoc_id}
        self._programme_tab.set_gdoc_map(gdoc_display, gdoc_login)

    def update_events(self, events: list[EventItem]) -> None:
        """Propagé depuis DataManager.events_updated."""
        self._programme_tab.update_events(events)
        self._accueil_tab.update_events(events)

    def update_history(self, history: HistoryStore) -> None:
        """Mise à jour des graphes d'historique (Accueil + Stats + grand écran)."""
        self._accueil_tab.update_history(history)
        self._stats_tab.update_history(history)
        self._bigscreen.update_history(history)

    def update_goals(self, goals: list[GoalWithStreamer]) -> None:
        """Propagé depuis DataManager.goals_updated."""
        self._accueil_tab.update_goals(goals)
        self._bigscreen.update_goals(goals)

    def _schedule_accueil_refresh(self) -> None:
        """Fusionne les rafraîchissements de l'Accueil sur un même tour de boucle.

        DataManager émet streamers_updated puis global_stats_updated coup sur
        coup : l'Accueil se reconstruisait deux fois par cycle.
        """
        if self._accueil_refresh_pending:
            return
        self._accueil_refresh_pending = True
        QTimer.singleShot(0, self._do_accueil_refresh)

    def _do_accueil_refresh(self) -> None:
        self._accueil_refresh_pending = False
        self._accueil_tab.refresh(self._last_streamers, self._last_stats)

    def open_streamer_sheet(self, login: str) -> None:
        """Ouvre la fiche d'un participant : tout ce qu'on sait de lui."""
        from windows.streamer_sheet import StreamerSheet
        st = next((x for x in self._last_streamers or []
                   if x.twitch_login == login), None)
        if st is None:
            return
        goals = list(self._goals_raw.get(login, []))
        events = [ev for ev in (self._accueil_tab._events or [])
                  if login in (ev.host_uuids or [])
                  or login in (ev.participant_uuids or [])]
        fiche = StreamerSheet(st, goals, events, self)
        fiche.stream_requested.connect(self.stream_selected)
        fiche.grid_requested.connect(self._on_add_to_grid)
        fiche.exec()

    def set_pinned_audio(self, logins: list) -> None:
        """Chaînes épinglées : elles rejoignent la console de mixage."""
        self._mixer_tab.set_pinned([str(lg) for lg in logins])

    def set_main_stream(self, login: str) -> None:
        """Flux affiché en plein écran : la console et les objectifs le suivent."""
        self._mixer_tab.set_main_stream(login)
        self._goals_tab.set_main_stream(login)

    def set_main_volume(self, valeur: int) -> None:
        """Volume du plein écran, réglé ailleurs qu'à la console."""
        self._mixer_tab.set_main_volume(valeur)

    def set_main_muted(self, muet: bool) -> None:
        """Coupure du plein écran, décidée ailleurs qu'à la console."""
        self._mixer_tab.set_main_muted(muet)

    def rafraichir_favori(self, login: str) -> None:
        """Répercute sur les cartes un favori posé hors du panel."""
        self._streamers_tab.rafraichir_favori(login)

    def niveaux_de_mixage(self) -> dict:
        """Volume et coupure de chaque tranche — relayé à la télécommande."""
        return self._mixer_tab.niveaux()

    def regler_mixage(self, login: str, valeur: int) -> None:
        """Règle une tranche de la console. Login vide = le plein écran."""
        self._mixer_tab.regler_volume(login, valeur)

    def couper_mixage(self, login: str, muet: bool) -> None:
        """Coupe une tranche de la console. Login vide = le plein écran."""
        self._mixer_tab.regler_muet(login, muet)

    def add_feed_event(self, kind: str, login: str, text: str) -> None:
        """Ajoute une entrée au fil d'événements de l'Accueil."""
        self._accueil_tab.add_feed_event(kind, login, text)

    def update_goals_cache(self, cache: dict) -> None:
        """Propagé depuis DataManager.goals_raw_updated — seed le cache local du tab goals."""
        # Conservés aussi pour la fiche d'un participant, qui les affiche.
        self._goals_raw = dict(cache or {})
        self._goals_tab.seed_cache(cache)
        self._stats_tab.seed_goals(cache)

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

    def ajouter_a_la_grille(self, login: str) -> None:
        """Ajoute une chaîne à la grille, demandé depuis une autre fenêtre.

        Le plein écran et la grille ont eux aussi leur palette de commandes,
        mais c'est l'onglet Streamers du panel qui détient la sélection.
        """
        self._on_add_to_grid(login)

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
                mark_fullscreen(self._grid_window)
                self.hide()
            return
        self._stack.setCurrentIndex(idx)

    def ajouter_don(self, don: object) -> None:
        """Un don qui vient d'arriver, pour le fil."""
        self._dons_tab.ajouter_don(don)

    def poser_historique_dons(self, dons: object) -> None:
        """Les derniers dons déjà passés, à la connexion du flux."""
        self._dons_tab.poser_historique(dons)

    def signaler_flux_dons(self, ouvert: bool) -> None:
        """État du flux, affiché en tête de l'onglet."""
        self._dons_tab.signaler_etat(ouvert)

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
        if (event.key() == Qt.Key.Key_K
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._palette.open()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self._palette.isVisible():
                self._palette.hide()
                return
            self.close()
        else:
            super().keyPressEvent(event)