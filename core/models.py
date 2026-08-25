# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Dataclasses partagées pour ZLink."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from PyQt6.QtGui import QScreen


class DisplayMode(Enum):
    """Mode d'affichage selon le nombre d'écrans détectés."""
    TRIPLE = auto()   # 3 écrans : panel + fullscreen + grille
    DUAL = auto()     # 2 écrans : panel + fullscreen
    SINGLE = auto()   # 1 écran  : fullscreen + sidebar overlay


class WindowRole(Enum):
    """Rôle assigné à une fenêtre."""
    PANEL = "panel"
    FULLSCREEN = "fullscreen"
    GRID = "grid"


@dataclass
class ScreenAssignment:
    """Association écran physique → rôle de fenêtre."""
    screen: QScreen
    role: WindowRole
    index: int  # position gauche→droite (0-based)

    @property
    def name(self) -> str:
        return self.screen.name()

    @property
    def resolution(self) -> str:
        g = self.screen.geometry()
        return f"{g.width()}x{g.height()}"

    @property
    def position(self) -> tuple[int, int]:
        g = self.screen.geometry()
        return (g.x(), g.y())


@dataclass
class DisplayLayout:
    """Résultat complet de la détection d'écrans."""
    mode: DisplayMode
    assignments: list[ScreenAssignment] = field(default_factory=list)

    def get_screen(self, role: WindowRole) -> ScreenAssignment | None:
        """Retourne l'assignment pour un rôle donné."""
        for a in self.assignments:
            if a.role == role:
                return a
        return None
