# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection des écrans et dispatch des fenêtres."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QApplication

from core.models import DisplayLayout, DisplayMode, ScreenAssignment, WindowRole

if TYPE_CHECKING:
    from PyQt6.QtGui import QScreen

logger = logging.getLogger(__name__)


def detect_screens(app: QApplication) -> list[QScreen]:
    """Retourne les écrans triés de gauche à droite par position X."""
    screens = app.screens()
    screens.sort(key=lambda s: s.geometry().x())
    for i, s in enumerate(screens):
        g = s.geometry()
        logger.info(
            "Écran %d : %s — %dx%d @ (%d, %d)",
            i, s.name(), g.width(), g.height(), g.x(), g.y(),
        )
    return screens


def build_layout(app: QApplication) -> DisplayLayout:
    """Détecte les écrans et assigne les rôles selon le mode (1/2/3 écrans).

    Priorité : config.json → screen_assignments, sinon auto-détection.
    - 3 écrans : gauche=Panel, centre=Fullscreen, droite=Grille
    - 2 écrans : gauche=Panel, droite=Fullscreen
    - 1 écran  : unique=Fullscreen (sidebar overlay plus tard)
    """
    screens = detect_screens(app)
    count = len(screens)

    # Tentative de lecture de la config utilisateur
    configured = _apply_screen_config(screens)
    if configured is not None:
        return configured

    # Auto-détection
    if count >= 3:
        mode = DisplayMode.TRIPLE
        assignments = [
            ScreenAssignment(screen=screens[0], role=WindowRole.PANEL, index=0),
            ScreenAssignment(screen=screens[1], role=WindowRole.FULLSCREEN, index=1),
            ScreenAssignment(screen=screens[2], role=WindowRole.GRID, index=2),
        ]
    elif count == 2:
        mode = DisplayMode.DUAL
        assignments = [
            ScreenAssignment(screen=screens[0], role=WindowRole.PANEL, index=0),
            ScreenAssignment(screen=screens[1], role=WindowRole.FULLSCREEN, index=1),
            ScreenAssignment(screen=screens[0], role=WindowRole.GRID, index=0),  # même écran que panel
        ]
    else:
        mode = DisplayMode.SINGLE
        assignments = [
            ScreenAssignment(screen=screens[0], role=WindowRole.FULLSCREEN, index=0),
        ]

    logger.info("Mode auto-détecté : %s (%d écran(s))", mode.name, count)
    for a in assignments:
        logger.info("  %s → %s (%s @ %s)", a.role.value, a.name, a.resolution, a.position)

    return DisplayLayout(mode=mode, assignments=assignments)


def _apply_screen_config(screens: list[QScreen]) -> DisplayLayout | None:
    """Lit screen_assignments depuis config.json et retourne un DisplayLayout.

    Retourne None si la config est absente, invalide, ou si aucun écran
    n'est assigné au rôle Fullscreen (fallback auto-détection).
    """
    try:
        from core.paths import CONFIG_PATH as cfg_path
        if not cfg_path.exists():
            return None
        screen_cfg: dict[str, str] = json.loads(
            cfg_path.read_text(encoding="utf-8")
        ).get("screen_assignments", {})
        if not screen_cfg:
            return None
        if not isinstance(screen_cfg, dict):
            # Le type est verifie, pas seulement la presence : une chaine ou une
            # liste passait le `if not`, puis .get() levait une AttributeError
            # HORS du try — et l'application ne demarrait plus du tout.
            logger.warning(
                "Config ecrans : screen_assignments n'est pas un objet (%s) — "
                "repli sur l'auto-detection", type(screen_cfg).__name__)
            return None
    except Exception as exc:
        logger.warning("_apply_screen_config: lecture config impossible — %s", exc)
        return None

    assignments: list[ScreenAssignment] = []
    for i, screen in enumerate(screens):
        role_str = screen_cfg.get(str(i), "disabled")
        if role_str == "panel":
            assignments.append(ScreenAssignment(screen=screen, role=WindowRole.PANEL, index=i))
        elif role_str == "fullscreen":
            assignments.append(ScreenAssignment(screen=screen, role=WindowRole.FULLSCREEN, index=i))
        elif role_str == "grid":
            assignments.append(ScreenAssignment(screen=screen, role=WindowRole.GRID, index=i))

    roles = {a.role for a in assignments}
    if WindowRole.FULLSCREEN not in roles:
        logger.warning("Config écrans : aucun écran Fullscreen — fallback auto-détection")
        return None

    has_panel = WindowRole.PANEL in roles
    has_grid = WindowRole.GRID in roles

    if has_panel and has_grid:
        mode = DisplayMode.TRIPLE
    elif has_panel:
        mode = DisplayMode.DUAL
        # La grille partage l'écran du panel en mode DUAL
        panel_a = next(a for a in assignments if a.role == WindowRole.PANEL)
        assignments.append(ScreenAssignment(screen=panel_a.screen, role=WindowRole.GRID, index=panel_a.index))
    else:
        mode = DisplayMode.SINGLE

    logger.info("Mode config : %s (%d écran(s) assignés)", mode.name, len(screens))
    for a in assignments:
        logger.info("  %s → %s (%s @ %s)", a.role.value, a.name, a.resolution, a.position)

    return DisplayLayout(mode=mode, assignments=assignments)
