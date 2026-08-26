# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Assistant de première configuration.

Au premier lancement, ZLink ouvrait ses fenêtres selon une auto-détection et
laissait l'utilisateur découvrir seul les réglages qui comptent. Les quatre
décisions structurantes — combien d'écrans, combien de flux dans la grille,
comment les ranger, et si HypeWatcher surveille les chats — sont posées ici,
une par étape, avant que la moindre fenêtre ne soit créée : le nombre d'écrans
détermine la disposition, il ne peut donc pas se changer après coup sans
redémarrer.

L'assistant ne s'ouvre qu'une fois. Tous ces réglages restent modifiables
ensuite dans Réglages.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import config_store

logger = logging.getLogger(__name__)

# Mêmes teintes que la fenêtre de réglages, pour que l'assistant n'ait pas
# l'air d'appartenir à une autre application.
_C_BG      = "#111111"
_C_SURFACE = "#1a1a1a"
_C_BORDER  = "#2a2a2a"
_C_TEXT    = "#cccccc"
_C_MUTED   = "#6a6a6a"
_C_GREEN   = "#00ff87"

_FONT = "Segoe UI Variable"

#: Rôles attribués aux écrans selon le nombre retenu. L'ordre suit la
#: disposition physique gauche → droite, comme core.monitors.build_layout.
_ROLE_PLAN: dict[int, list[str]] = {
    1: ["fullscreen"],
    2: ["panel", "fullscreen"],
    3: ["panel", "fullscreen", "grid"],
}

_ROLE_LABELS = {
    "panel": "Panel (stats, programme, streamers)",
    "fullscreen": "Plein écran (le direct principal)",
    "grid": "Grille (tous les flux en mosaïque)",
}

#: Ce que chaque configuration donne, et comment atteindre le reste. Aucune ne
#: PRIVE d'une vue : à un écran, une barre escamotable en haut fait passer de
#: l'une à l'autre ; à deux, la grille s'ouvre depuis le panel. Ne le pas dire
#: laissait croire qu'on renonçait à la grille ou au panel.
_PLAN_NOTES: dict[int, str] = {
    1: "Les trois vues sur cet écran ; une barre en haut fait passer de l'une "
       "à l'autre.",
    2: "La grille s'ouvre depuis le panel.",
    3: "Tout est visible en même temps, rien à basculer.",
}


def _title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(_FONT, 17, QFont.Weight.Bold))
    lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
    return lbl


def _sub(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(_FONT, 10))
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {_C_MUTED}; background: transparent; border: none;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(_FONT, 10))
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {_C_TEXT}; background: transparent; border: none;")
    return lbl


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("wizCard")
    f.setStyleSheet(
        f"QFrame#wizCard {{ background: {_C_SURFACE}; border: 1px solid {_C_BORDER};"
        f" border-radius: 8px; }}"
    )
    return f


class _ChoiceCard(QFrame):
    """Carte de choix cliquable dans TOUTE sa surface.

    Une puce QRadioButton mesure treize pixels, se fond dans le thème et ne dit
    rien de cliquable : rien n'indiquait qu'il fallait viser la carte, ni
    laquelle était retenue. Ici la carte entière réagit, le curseur change au
    survol, et la sélection se voit au liseré vert et au fond teinté.
    """

    clicked = pyqtSignal(object)   # la valeur portée par la carte

    def __init__(self, value: object, title: str, desc: str,
                 badge: str = "") -> None:
        super().__init__()
        self._value = value
        self._selected = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("choiceCard")

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 11, 14, 11)
        h.setSpacing(12)

        self._dot = QFrame()
        self._dot.setFixedSize(18, 18)
        h.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        self._title = QLabel(title)
        self._title.setFont(QFont(_FONT, 12, QFont.Weight.Bold))
        line.addWidget(self._title)
        if badge:
            b = QLabel(badge)
            b.setFont(QFont(_FONT, 8, QFont.Weight.Bold))
            b.setStyleSheet(
                f"color: {_C_GREEN}; background: rgba(0,255,135,26);"
                " border: none; border-radius: 4px; padding: 1px 6px;"
            )
            line.addWidget(b)
        line.addStretch()
        col.addLayout(line)
        if desc:
            d = QLabel(desc)
            d.setFont(QFont(_FONT, 9))
            d.setWordWrap(True)
            d.setStyleSheet(
                f"color: {_C_MUTED}; background: transparent; border: none;")
            col.addWidget(d)
        h.addLayout(col, stretch=1)

        self._restyle()

    @property
    def value(self) -> object:
        return self._value

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        if on == self._selected:
            return
        self._selected = on
        self._restyle()

    # -- rendu -----------------------------------------------------------

    def _restyle(self) -> None:
        if self._selected:
            border, bg = _C_GREEN, "#0f1a14"
        elif self._hover:
            border, bg = "#3a3a3a", "#1f1f1f"
        else:
            border, bg = _C_BORDER, _C_SURFACE
        self.setStyleSheet(
            f"QFrame#choiceCard {{ background: {bg}; border: 1px solid {border};"
            f" border-radius: 8px; }}"
        )
        self._title.setStyleSheet(
            f"color: {'#ffffff' if self._selected else _C_TEXT};"
            f" background: transparent; border: none;"
        )
        if self._selected:
            self._dot.setStyleSheet(
                f"background: {_C_GREEN}; border: 5px solid {_C_SURFACE};"
                f" border-radius: 9px;"
            )
        else:
            self._dot.setStyleSheet(
                "background: transparent; border: 2px solid #4a4a4a;"
                " border-radius: 9px;"
            )

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hover = True
        self._restyle()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover = False
        self._restyle()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self._value)
        super().mouseReleaseEvent(event)


class _ChoiceList(QWidget):
    """Groupe de cartes à choix unique."""

    changed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(8)
        self._cards: list[_ChoiceCard] = []

    def add(self, value: object, title: str, desc: str, badge: str = "") -> None:
        card = _ChoiceCard(value, title, desc, badge)
        card.clicked.connect(self.select)
        self._cards.append(card)
        self._v.addWidget(card)

    def select(self, value: object) -> None:
        for c in self._cards:
            c.set_selected(c.value == value)
        self.changed.emit(value)

    def selected(self) -> object:
        for c in self._cards:
            if c.is_selected():
                return c.value
        return None


class _ScreenPicker(QWidget):
    """Schéma des moniteurs, à leur position et à leur échelle réelles.

    Trois lignes de texte par configuration, c'était illisible : on décrivait
    des dispositions au lieu de les montrer. Ici les écrans sont dessinés là où
    ils sont physiquement, numérotés comme dans les réglages du système, et un
    clic les active ou les désactive — le geste de Windows.

    Les rôles se répartissent tout seuls, de gauche à droite, sur les écrans
    actifs, et chaque rectangle affiche le sien : on voit immédiatement ce qu'on
    obtient, sans avoir à le lire.
    """

    changed = pyqtSignal()

    _PAD = 10
    _ROLE_SHORT = {"panel": "PANEL", "fullscreen": "DIRECT", "grid": "GRILLE"}

    def __init__(self, geometries: list[tuple[int, int, int, int]]) -> None:
        super().__init__()
        self._geos = geometries or [(0, 0, 1920, 1080)]
        # Au départ, on retient autant d'écrans qu'on sait en exploiter.
        self._enabled = [i < 3 for i in range(len(self._geos))]
        self._rects: list[QRect] = []
        self.setMinimumHeight(190)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- état ------------------------------------------------------------

    def enabled_indexes(self) -> list[int]:
        """Indices des écrans retenus, dans l'ordre physique gauche → droite."""
        idx = [i for i, on in enumerate(self._enabled) if on]
        idx.sort(key=lambda i: self._geos[i][0])
        return idx[:3]          # au-delà de trois, il n'y a plus de rôle à donner

    def assignments(self) -> dict[str, str]:
        """{index d'écran: rôle} tel que le comprend core.monitors."""
        active = self.enabled_indexes()
        plan = _ROLE_PLAN.get(len(active), [])
        return {str(i): role for i, role in zip(active, plan)}

    def _role_of(self, index: int) -> str:
        return self.assignments().get(str(index), "")

    # -- géométrie -------------------------------------------------------

    def _compute(self) -> None:
        """Projette les géométries réelles dans le widget, en gardant l'échelle."""
        xs0 = min(g[0] for g in self._geos)
        ys0 = min(g[1] for g in self._geos)
        xs1 = max(g[0] + g[2] for g in self._geos)
        ys1 = max(g[1] + g[3] for g in self._geos)
        span_w, span_h = max(1, xs1 - xs0), max(1, ys1 - ys0)
        avail_w = max(1, self.width() - 2 * self._PAD)
        avail_h = max(1, self.height() - 2 * self._PAD)
        # Une seule échelle pour les deux axes : sinon un moniteur vertical
        # apparaîtrait aussi large qu'un horizontal.
        scale = min(avail_w / span_w, avail_h / span_h)
        off_x = self._PAD + (avail_w - span_w * scale) / 2
        off_y = self._PAD + (avail_h - span_h * scale) / 2
        self._rects = [
            QRect(
                int(off_x + (g[0] - xs0) * scale),
                int(off_y + (g[1] - ys0) * scale),
                max(52, int(g[2] * scale) - 6),
                max(38, int(g[3] * scale) - 6),
            )
            for g in self._geos
        ]

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._compute()

    # -- interaction -----------------------------------------------------

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        for i, r in enumerate(self._rects):
            if not r.contains(pos):
                continue
            # Le dernier écran actif ne se désactive pas : le direct doit bien
            # s'afficher quelque part, et une sélection vide bloquerait la suite
            # sans que la raison soit visible.
            if self._enabled[i] and sum(self._enabled) == 1:
                return
            self._enabled[i] = not self._enabled[i]
            self.update()
            self.changed.emit()
            return

    # -- rendu -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if not self._rects:
            self._compute()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, r in enumerate(self._rects):
            self._dessiner_ecran(p, i, r)
        p.end()

    def _dessiner_ecran(self, p: QPainter, i: int, r: QRect) -> None:
        """Un rectangle d'écran : cadre, numéro, rôle attribué, définition.

        Un écran désactivé garde sa place mais passe en pointillés grisés :
        le voir barré vaut mieux que le voir disparaître de la disposition.
        """
        on = self._enabled[i]
        role = self._role_of(i)
        p.setBrush(QBrush(QColor("#16211b" if on else "#141414")))
        pen = QPen(QColor(_C_GREEN if on else "#333333"))
        pen.setWidth(2 if on else 1)
        if not on:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, 7, 7)

        # Numéro, comme dans les réglages d'affichage du système.
        p.setPen(QColor("#ffffff" if on else "#4a4a4a"))
        f = QFont(_FONT, max(13, min(26, r.height() // 4)), QFont.Weight.Bold)
        p.setFont(f)
        num = QRect(r.x(), r.y() + 6, r.width(), r.height() // 2)
        p.drawText(num, Qt.AlignmentFlag.AlignCenter, str(i + 1))

        # Rôle attribué, ou l'état inactif.
        p.setPen(QColor(_C_GREEN if on else "#3d3d3d"))
        p.setFont(QFont(_FONT, 8, QFont.Weight.Bold))
        lab = QRect(r.x(), r.y() + r.height() // 2, r.width(), r.height() // 2 - 6)
        p.drawText(lab, Qt.AlignmentFlag.AlignCenter,
                   self._ROLE_SHORT.get(role, "" if on else "DÉSACTIVÉ"))

        # Définition, seulement si le rectangle est assez grand pour la lire.
        if r.height() >= 70:
            p.setPen(QColor("#5a5a5a" if on else "#2f2f2f"))
            p.setFont(QFont(_FONT, 7))
            g = self._geos[i]
            foot = QRect(r.x(), r.bottom() - 16, r.width(), 14)
            p.drawText(foot, Qt.AlignmentFlag.AlignCenter,
                       f"{g[2]}\u00d7{g[3]}")


class _Step(QWidget):
    """Une étape. `collect` écrit dans la config, `valid` bloque « Suivant »."""

    def collect(self, config: dict) -> None:  # pragma: no cover - surchargé
        raise NotImplementedError

    def valid(self) -> bool:
        return True


# ── Étape 1 — bienvenue ────────────────────────────────────────────────────

class _StepWelcome(_Step):
    def __init__(self, config: dict) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setSpacing(14)
        v.addWidget(_title("Bienvenue dans ZLink"))
        v.addWidget(_body(
            "Un panneau de régie pour suivre le ZEvent : tous les directs en "
            "mosaïque, le programme, la cagnotte et les objectifs, sur un ou "
            "plusieurs écrans."
        ))
        v.addWidget(_sub(
            "Quatre questions, puis c'est prêt. Tout reste modifiable ensuite "
            "dans Réglages."
        ))
        v.addStretch()

    def collect(self, config: dict) -> None:
        return


# ── Étape 2 — écrans ───────────────────────────────────────────────────────

class _StepScreens(_Step):
    def __init__(self, config: dict) -> None:
        super().__init__()
        screens = sorted(
            QApplication.instance().screens(),            # type: ignore[union-attr]
            key=lambda sc: sc.geometry().x(),
        )
        geos = [(sc.geometry().x(), sc.geometry().y(),
                 sc.geometry().width(), sc.geometry().height())
                for sc in screens] or [(0, 0, 1920, 1080)]

        v = QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(_title("Quels écrans utiliser ?"))
        v.addWidget(_sub("Cliquez sur un écran pour l'activer ou le désactiver."))

        self._picker = _ScreenPicker(geos)
        # Restaurer la sélection enregistrée, s'il y en a une.
        saved = config.get("screen_assignments") or {}
        if saved:
            keep = {int(k) for k, r in saved.items()
                    if r and r != "disabled" and k.isdigit()}
            if keep:
                self._picker._enabled = [
                    i in keep for i in range(len(geos))
                ]
        self._picker.changed.connect(self._refresh)
        v.addWidget(self._picker, stretch=1)

        self._note = _sub("")
        v.addWidget(self._note)
        self._refresh()

    def _refresh(self) -> None:
        n = len(self._picker.enabled_indexes())
        self._note.setText(_PLAN_NOTES.get(n, ""))

    def collect(self, config: dict) -> None:
        assigned = self._picker.assignments()
        if assigned:
            config["screen_assignments"] = assigned

    def valid(self) -> bool:
        return bool(self._picker.enabled_indexes())


def _count_from_config(config: dict) -> int:
    """Nombre d'écrans déjà configurés, 0 si rien n'est enregistré."""
    assigned = config.get("screen_assignments") or {}
    return len({k for k, v in assigned.items() if v and v != "disabled"})


# ── Étape 3 — grille ───────────────────────────────────────────────────────

_SORT_CHOICES = [
    ("viewers", "Par audience",
     "Les plus regardés en tête, réordonnés automatiquement."),
    ("manual", "Manuel",
     "Vous placez chaque flux au glisser-déposer, l'ordre ne bouge plus."),
    ("favorites", "Favoris puis manuel",
     "Vos favoris restent en tête, le reste se glisse librement."),
]


class _StepGrid(_Step):
    def __init__(self, config: dict) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(_title("La grille"))
        v.addWidget(_sub(
            "Chaque flux consomme du réseau et du processeur. La qualité "
            "s'adapte toute seule au nombre de flux affichés."
        ))

        card = _card()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 12, 12, 12)
        cv.setSpacing(8)
        row = QHBoxLayout()
        # wordWrap actif couperait « Flux simultanés au maximum » en deux lignes
        # dans une rangée déjà étroite.
        _lbl = _body("Nombre maximum de flux")
        _lbl.setWordWrap(False)
        row.addWidget(_lbl)
        row.addStretch()
        self._value_lbl = QLabel()
        self._value_lbl.setFont(QFont("Cascadia Code", 15, QFont.Weight.Bold))
        self._value_lbl.setStyleSheet(
            f"color: {_C_GREEN}; background: transparent; border: none;")
        row.addWidget(self._value_lbl)
        cv.addLayout(row)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(1, 25)
        self._slider.setValue(int(config.get("max_active_streams", 16) or 16))
        self._slider.valueChanged.connect(self._on_value)
        cv.addWidget(self._slider)
        self._hint_lbl = _sub("")
        cv.addWidget(self._hint_lbl)
        v.addWidget(card)
        self._on_value(self._slider.value())

        v.addWidget(_body("Disposition"))
        self._sort = _ChoiceList()
        for value, label, desc in _SORT_CHOICES:
            self._sort.add(value, label, desc)
        v.addWidget(self._sort)
        current = config.get("grid_sort", "viewers")
        known = {v for v, _, _ in _SORT_CHOICES}
        self._sort.select(current if current in known else "viewers")
        v.addStretch()

    def _on_value(self, n: int) -> None:
        self._value_lbl.setText(str(n))
        if n <= 4:
            txt = "Qualité maximale, très peu de charge."
        elif n <= 9:
            txt = "Bon compromis sur une machine de bureau."
        elif n <= 16:
            txt = "Confortable avec une connexion à l'aise."
        else:
            txt = "Exigeant : réservez-le à une machine et un réseau solides."
        self._hint_lbl.setText(txt)

    def collect(self, config: dict) -> None:
        config["max_active_streams"] = self._slider.value()
        chosen = self._sort.selected()
        if chosen:
            config["grid_sort"] = str(chosen)


# ── Étape 4 — HypeWatcher ──────────────────────────────────────────────────

class _StepHype(_Step):
    def __init__(self, config: dict) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(_title("HypeWatcher"))
        v.addWidget(_body(
            "HypeWatcher suit les chats des directs affichés et vous signale "
            "les moments où ça s'emballe : un pic de messages très au-dessus "
            "du rythme habituel de la chaîne."
        ))
        v.addWidget(_sub(
            "Il se connecte aux chats Twitch en lecture seule, de façon anonyme. "
            "Aucun compte n'est nécessaire et rien n'est envoyé."
        ))
        card = _card()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 12, 12, 12)
        self._check = QCheckBox("Activer HypeWatcher")
        self._check.setFont(QFont(_FONT, 11, QFont.Weight.Bold))
        self._check.setStyleSheet(
            f"QCheckBox {{ color: {_C_TEXT}; background: transparent; }}"
            f"QCheckBox::indicator {{ width: 15px; height: 15px; }}"
        )
        hw = config.get("hypewatcher") or {}
        self._check.setChecked(bool(hw.get("enabled", True)))
        cv.addWidget(self._check)
        v.addWidget(card)
        v.addStretch()

    def collect(self, config: dict) -> None:
        hw = config.setdefault("hypewatcher", {})
        hw["enabled"] = self._check.isChecked()


# ── Étape 5 — récapitulatif ────────────────────────────────────────────────

class _StepSummary(_Step):
    def __init__(self, config: dict) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(_title("C'est prêt"))
        self._recap = _body("")
        self._card = _card()
        cv = QVBoxLayout(self._card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.addWidget(self._recap)
        v.addWidget(self._card)
        v.addWidget(_sub("Modifiable à tout moment dans Réglages."))
        v.addStretch()

    def refresh(self, config: dict) -> None:
        assigned = config.get("screen_assignments") or {}
        lignes = [
            f"• {len(assigned)} écran" + ("s" if len(assigned) > 1 else "")
            + " : " + ", ".join(
                f"écran {int(i) + 1} → {_ROLE_LABELS.get(r, r).split(' (')[0]}"
                # Tri NUMERIQUE : par cle texte, l'ecran 10 passerait
                # avant le 2.
                for i, r in sorted(assigned.items(), key=lambda kv: int(kv[0]))
            ),
            f"• Jusqu'à {config.get('max_active_streams', 16)} flux dans la grille",
            "• Disposition : " + {
                "viewers": "par audience",
                "manual": "manuelle",
                "favorites": "favoris puis manuel",
            }.get(config.get("grid_sort", "viewers"), "par audience"),
            "• HypeWatcher : " + (
                "activé" if (config.get("hypewatcher") or {}).get("enabled", True)
                else "désactivé"
            ),
        ]
        self._recap.setText("\n".join(lignes))
        # Le texte arrive APRÈS la première mise en page : sans relance
        # explicite, la carte garde la hauteur qu'elle avait quand l'étiquette
        # était vide et tronque le récapitulatif.
        self._recap.adjustSize()
        self._card.updateGeometry()
        lay = self.layout()
        if lay is not None:
            lay.activate()

    def collect(self, config: dict) -> None:
        return


# ── Assistant ──────────────────────────────────────────────────────────────

class FirstRunWizard(QDialog):
    """Fenêtre d'assistant. `result_config` porte la config retenue."""

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ZLink — première configuration")
        self.setModal(True)
        self.setMinimumSize(560, 560)
        self.setStyleSheet(
            f"QDialog {{ background: {_C_BG}; }}"
            f"QLabel {{ color: {_C_TEXT}; }}"
        )
        self.result_config = dict(config)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 18)
        root.setSpacing(16)

        self._stack = QStackedWidget()
        self._steps: list[_Step] = [
            _StepWelcome(config),
            _StepScreens(config),
            _StepGrid(config),
            _StepHype(config),
            _StepSummary(config),
        ]
        for st in self._steps:
            self._stack.addWidget(st)
        root.addWidget(self._stack, stretch=1)

        self._dots = QLabel("")
        self._dots.setFont(QFont("Cascadia Code", 11))
        self._dots.setStyleSheet(
            f"color: {_C_BORDER}; background: transparent; border: none;")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(self._dots)
        bar.addStretch()
        self._skip = self._button("Passer", subdued=True)
        self._skip.clicked.connect(self._on_skip)
        bar.addWidget(self._skip)
        self._prev = self._button("Précédent", subdued=True)
        self._prev.clicked.connect(self._go_prev)
        bar.addWidget(self._prev)
        self._next = self._button("Suivant")
        self._next.clicked.connect(self._go_next)
        bar.addWidget(self._next)
        root.addLayout(bar)

        self._sync()

    # -- interne ---------------------------------------------------------

    def _button(self, text: str, subdued: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(32)
        b.setMinimumWidth(96)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if subdued:
            b.setStyleSheet(
                f"QPushButton {{ background: {_C_SURFACE}; color: {_C_TEXT};"
                f" border: 1px solid {_C_BORDER}; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: #232323; }}"
                f"QPushButton:disabled {{ color: #3a3a3a; }}"
            )
        else:
            b.setStyleSheet(
                f"QPushButton {{ background: {_C_GREEN}; color: #08130d;"
                f" border: none; border-radius: 6px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: #4dffab; }}"
                f"QPushButton:disabled {{ background: #1e3a2c; color: #4a4a4a; }}"
            )
        return b

    def _sync(self) -> None:
        i = self._stack.currentIndex()
        last = i == len(self._steps) - 1
        self._prev.setEnabled(i > 0)
        self._next.setText("Terminer" if last else "Suivant")
        self._next.setEnabled(self._steps[i].valid())
        self._skip.setVisible(not last)
        self._dots.setText(
            "  ".join("●" if k == i else "○"
                      for k in range(len(self._steps)))
        )

    def _go_prev(self) -> None:
        if self._stack.currentIndex() > 0:
            self._stack.setCurrentIndex(self._stack.currentIndex() - 1)
            self._sync()

    def _go_next(self) -> None:
        i = self._stack.currentIndex()
        step = self._steps[i]
        if not step.valid():
            return
        step.collect(self.result_config)
        if i == len(self._steps) - 1:
            self.accept()
            return
        nxt = self._steps[i + 1]
        # Le récapitulatif lit ce que les étapes précédentes ont écrit : il doit
        # être rafraîchi juste avant d'être montré, pas à sa construction.
        if isinstance(nxt, _StepSummary):
            nxt.refresh(self.result_config)
        self._stack.setCurrentIndex(i + 1)
        self._sync()

    def _on_skip(self) -> None:
        """Quitte sans appliquer : les valeurs par défaut restent en place."""
        self.reject()


# ── Point d'entrée ─────────────────────────────────────────────────────────

def _load_config() -> dict:
    return config_store.load()


def _save_config(config: dict) -> bool:
    return config_store.save_merge(config)


def needs_first_run() -> bool:
    """Vrai tant que l'assistant n'a pas été mené à son terme ou écarté."""
    return not bool(_load_config().get("setup_done"))


def run_first_run_wizard(force: bool = False) -> dict:
    """Ouvre l'assistant si besoin. Retourne la configuration à utiliser.

    À appeler AVANT la construction des fenêtres : le nombre d'écrans décide de
    la disposition, et celle-ci est figée au démarrage.

    `force` le rouvre même s'il a déjà été passé — c'est ce que fait `--setup`.
    """
    config = _load_config()
    if config.get("setup_done") and not force:
        return config

    wiz = FirstRunWizard(config)
    accepted = wiz.exec() == QDialog.DialogCode.Accepted
    config = wiz.result_config if accepted else config
    # Marqué comme fait dans les deux cas : un assistant écarté qui revient à
    # chaque lancement serait pénible, et « Passer » est une réponse.
    config["setup_done"] = True
    _save_config(config)
    logger.info("Assistant de première configuration : %s",
                "terminé" if accepted else "passé")
    return config
