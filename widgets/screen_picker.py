# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Le schéma des moniteurs, et l'attribution des rôles.

Trois lignes de texte par configuration, c'était illisible : on décrivait des
dispositions au lieu de les montrer. Ici les écrans sont dessinés là où ils
sont physiquement, à leur échelle, numérotés comme dans les réglages du
système — on voit immédiatement ce qu'on obtient, sans avoir à le lire.

Un clic ouvre le choix du rôle. L'assistant répartissait les rôles tout seuls
de gauche à droite : ça convient au cas courant, mais pas à qui a son grand
écran à droite, ou son écran vertical au milieu. Le plan automatique reste le
point de départ ; il n'est plus une fatalité.

Le même widget sert à l'assistant et aux réglages : deux présentations
différentes du même choix laissaient croire à deux réglages différents.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QMenu, QWidget

logger = logging.getLogger(__name__)

#: Un écran sans rôle. La chaîne vide, et non "disabled" : c'est ce que
#: `core.monitors` attend en creux — une clé absente de screen_assignments.
AUCUN = ""

#: Rôles attribuables, dans l'ordre où ils comptent.
ROLES: tuple[str, ...] = ("fullscreen", "panel", "grid")

#: Plan de départ selon le nombre d'écrans retenus. L'ordre suit la disposition
#: physique gauche → droite, comme core.monitors.build_layout.
ROLE_PLAN: dict[int, list[str]] = {
    1: ["fullscreen"],
    2: ["panel", "fullscreen"],
    3: ["panel", "fullscreen", "grid"],
}

ROLE_LABELS: dict[str, str] = {
    "panel": "Panel (stats, programme, streamers)",
    "fullscreen": "Plein écran (le direct principal)",
    "grid": "Grille (tous les flux en mosaïque)",
}

#: Ce qui tient sur un rectangle de moniteur.
ROLE_SHORT: dict[str, str] = {
    "panel": "PANEL", "fullscreen": "DIRECT", "grid": "GRILLE",
}

#: Ce que chaque configuration donne, et comment atteindre le reste. Aucune ne
#: PRIVE d'une vue : à un écran, une barre escamotable en haut fait passer de
#: l'une à l'autre ; à deux, la grille s'ouvre depuis le panel. Ne pas le dire
#: laissait croire qu'on renonçait à la grille ou au panel.
PLAN_NOTES: dict[int, str] = {
    1: "Les trois vues sur cet écran ; une barre en haut fait passer de l'une "
       "à l'autre.",
    2: "La grille s'ouvre depuis le panel.",
    3: "Tout est visible en même temps, rien à basculer.",
}

_C_GREEN = "#00ff87"
_FONT = "Segoe UI Variable"

_SS_MENU = """
QMenu { background: #1a1a1a; border: 1px solid #2a2a2a; padding: 4px; }
QMenu::item { color: #cccccc; padding: 6px 26px 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: #223c30; color: #00ff87; }
QMenu::item:disabled { color: #4a4a4a; }
QMenu::separator { height: 1px; background: #2a2a2a; margin: 4px 6px; }
"""


def plan_par_defaut(geometries: list[tuple[int, int, int, int]]) -> list[str]:
    """Le rôle de chaque écran quand rien n'est encore enregistré.

    Gauche → droite, dans la limite de trois : au-delà, il n'y a plus de rôle
    à donner et les écrans en trop restent libres pour autre chose.
    """
    ordre = sorted(range(len(geometries)), key=lambda i: geometries[i][0])
    plan = ROLE_PLAN.get(min(len(ordre), 3), [])
    roles = [AUCUN] * len(geometries)
    for indice, role in zip(ordre, plan):
        roles[indice] = role
    return roles


class ScreenPicker(QWidget):
    """Schéma cliquable des moniteurs, un rôle par écran."""

    changed = pyqtSignal()

    _PAD = 10

    def __init__(self, geometries: list[tuple[int, int, int, int]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._geos = geometries or [(0, 0, 1920, 1080)]
        self._roles = plan_par_defaut(self._geos)
        self._rects: list[QRect] = []
        self.setMinimumHeight(190)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- état ------------------------------------------------------------

    def roles(self) -> list[str]:
        """Le rôle de chaque écran, indexé comme les géométries."""
        return list(self._roles)

    def enabled_indexes(self) -> list[int]:
        """Indices des écrans retenus, dans l'ordre physique gauche → droite."""
        idx = [i for i, r in enumerate(self._roles) if r]
        idx.sort(key=lambda i: self._geos[i][0])
        return idx

    def assignments(self) -> dict[str, str]:
        """{index d'écran: rôle} tel que le comprend core.monitors."""
        return {str(i): r for i, r in enumerate(self._roles) if r}

    def definir_assignments(self, sauvegarde: dict) -> None:
        """Restaure une attribution enregistrée.

        Ce qui n'a plus de sens est écarté : un écran débranché depuis, un rôle
        inconnu, une clé qui n'est pas un nombre, un rôle donné deux fois.

        S'il ne reste pas de plein écran — config.json s'édite à la main — on
        en place un plutôt que de tout jeter : le reste du choix était valable,
        et `core.monitors` retomberait de toute façon sur l'auto-détection
        devant une disposition sans direct.
        """
        if not isinstance(sauvegarde, dict):
            return
        roles = [AUCUN] * len(self._geos)
        for cle, role in sauvegarde.items():
            texte = str(cle)
            if not texte.isdigit() or role not in ROLES or role in roles:
                continue
            indice = int(texte)
            if 0 <= indice < len(roles):
                roles[indice] = role
        if not any(roles):
            return
        if "fullscreen" not in roles:
            ordre = sorted(range(len(roles)), key=lambda i: self._geos[i][0])
            cible = next((i for i in ordre if not roles[i]), ordre[0])
            logger.info(
                "Attribution enregistree sans plein ecran — ecran %d le prend",
                cible + 1)
            roles[cible] = "fullscreen"
        self._roles = roles
        self.update()

    # -- attribution -----------------------------------------------------

    def peut_attribuer(self, index: int, role: str) -> bool:
        """Vrai si ce rôle peut être donné à cet écran.

        Le plein écran ne se retire jamais : c'est la seule vue dont
        l'application ne peut pas se passer, et une configuration sans direct
        bloque le démarrage sans que la raison soit visible. Il se DÉPLACE —
        le donner à un autre écran rend à celui-ci le rôle qu'il avait.
        """
        if not 0 <= index < len(self._roles):
            return False
        if role == self._roles[index]:
            return False
        if self._roles[index] != "fullscreen":
            return True
        if not role:
            return False
        return any(j != index and r == role for j, r in enumerate(self._roles))

    def attribuer(self, index: int, role: str) -> bool:
        """Donne un rôle à un écran, en échangeant avec celui qui l'avait."""
        if not self.peut_attribuer(index, role):
            return False
        ancien = self._roles[index]
        if role:
            for j, r in enumerate(self._roles):
                if j != index and r == role:
                    self._roles[j] = ancien
                    break
        self._roles[index] = role
        self.update()
        self.changed.emit()
        return True

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

    def index_a(self, pos) -> int:
        """L'écran sous ce point, ou -1."""
        for i, r in enumerate(self._rects):
            if r.contains(pos):
                return i
        return -1

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.index_a(event.position().toPoint())
        if index >= 0:
            self.ouvrir_menu(index, event.globalPosition().toPoint())

    def construire_menu(self, index: int) -> QMenu:
        """Le choix de rôle pour un écran.

        Les entrées impossibles restent VISIBLES, grisées : les masquer ferait
        croire que le rôle n'existe pas, alors qu'il est seulement indéplaçable.
        """
        menu = QMenu(self)
        menu.setStyleSheet(_SS_MENU)
        menu.setTitle(f"Écran {index + 1}")
        courant = self._roles[index] if 0 <= index < len(self._roles) else AUCUN
        for role in ROLES:
            action = menu.addAction(ROLE_LABELS[role])
            action.setCheckable(True)
            action.setChecked(role == courant)
            action.setEnabled(self.peut_attribuer(index, role) or role == courant)
            action.setData(role)
        menu.addSeparator()
        eteindre = menu.addAction("Ne pas utiliser cet écran")
        eteindre.setCheckable(True)
        eteindre.setChecked(courant == AUCUN)
        eteindre.setEnabled(
            self.peut_attribuer(index, AUCUN) or courant == AUCUN)
        eteindre.setData(AUCUN)
        return menu

    def ouvrir_menu(self, index: int, position) -> None:
        menu = self.construire_menu(index)
        choix = menu.exec(position)
        if choix is not None:
            self.attribuer(index, choix.data())

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

        Un écran sans rôle garde sa place mais passe en pointillés grisés :
        le voir barré vaut mieux que le voir disparaître de la disposition.
        """
        role = self._roles[i]
        on = bool(role)
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
                   ROLE_SHORT.get(role, "INUTILISÉ"))

        # Définition, seulement si le rectangle est assez grand pour la lire.
        if r.height() >= 70:
            p.setPen(QColor("#5a5a5a" if on else "#2f2f2f"))
            p.setFont(QFont(_FONT, 7))
            g = self._geos[i]
            foot = QRect(r.x(), r.bottom() - 16, r.width(), 14)
            p.drawText(foot, Qt.AlignmentFlag.AlignCenter, f"{g[2]}×{g[3]}")
