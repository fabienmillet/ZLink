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


def _lire_screen_cfg() -> dict | None:
    """`screen_assignments` tel qu'il est dans config.json, ou None.

    None a un seul sens ici : rien d'exploitable, l'auto-detection reprend la
    main. Fichier absent, illisible, cle vide ou d'un autre type — chaque cas
    est trace, mais aucun n'empeche l'application de demarrer.
    """
    try:
        from core.paths import CONFIG_PATH as cfg_path
        if not cfg_path.exists():
            return None
        screen_cfg = json.loads(
            cfg_path.read_text(encoding="utf-8")
        ).get("screen_assignments", {})
    except Exception as exc:
        logger.warning("_apply_screen_config: lecture config impossible — %s", exc)
        return None
    if not screen_cfg:
        return None
    if not isinstance(screen_cfg, dict):
        # Le type est verifie, pas seulement la presence : une chaine ou une
        # liste passait le `if not`, puis .get() levait une AttributeError HORS
        # du try — et l'application ne demarrait plus du tout.
        logger.warning(
            "Config ecrans : screen_assignments n'est pas un objet (%s) — "
            "repli sur l'auto-detection", type(screen_cfg).__name__)
        return None
    return screen_cfg


def _assignations_depuis(screens: list[QScreen],
                         screen_cfg: dict) -> list[ScreenAssignment]:
    """Les ecrans a qui la config donne un role connu. Les autres sont ecartes."""
    connus = {r.value: r for r in WindowRole}
    assignments: list[ScreenAssignment] = []
    for i, screen in enumerate(screens):
        role = connus.get(screen_cfg.get(str(i), ""))
        if role is not None:
            assignments.append(
                ScreenAssignment(screen=screen, role=role, index=i))
    return assignments


def _sans_grille_orpheline(
        assignments: list[ScreenAssignment]) -> list[ScreenAssignment]:
    """Retire la grille si aucun ecran ne porte le panel.

    Il n'y a pas de mode « direct + grille » : faute de panel, le calcul du
    mode retenait SINGLE, main n'ouvrait que le direct, et la grille etait
    perdue sans un mot. Les reglages ne s'ouvrant que depuis le panel, cette
    disposition enfermait de surcroit dans un ecran qu'on ne pouvait plus
    changer.

    On retombe donc en mode un ecran DELIBEREMENT : les trois vues s'y
    superposent et les reglages redeviennent accessibles. Le selecteur
    l'interdit desormais ; ce garde-fou vaut pour un config.json ecrit a la
    main.
    """
    roles = {a.role for a in assignments}
    if WindowRole.GRID not in roles or WindowRole.PANEL in roles:
        return assignments
    logger.warning(
        "Config ecrans : grille sans panel — grille ignoree, tout passe sur "
        "l'ecran du direct (les reglages ne s'ouvrent que du panel)")
    return [a for a in assignments if a.role != WindowRole.GRID]


def _mode_et_assignations(
        assignments: list[ScreenAssignment],
) -> tuple[DisplayMode, list[ScreenAssignment]]:
    """Le mode d'affichage que decrivent ces roles."""
    roles = {a.role for a in assignments}
    if WindowRole.PANEL not in roles:
        return DisplayMode.SINGLE, assignments
    if WindowRole.GRID in roles:
        return DisplayMode.TRIPLE, assignments
    # Sans ecran dedie, la grille partage celui du panel.
    panel_a = next(a for a in assignments if a.role == WindowRole.PANEL)
    return DisplayMode.DUAL, assignments + [
        ScreenAssignment(screen=panel_a.screen, role=WindowRole.GRID,
                         index=panel_a.index)]


def _apply_screen_config(screens: list[QScreen]) -> DisplayLayout | None:
    """Lit screen_assignments depuis config.json et retourne un DisplayLayout.

    Retourne None si la config est absente, invalide, ou si aucun écran
    n'est assigné au rôle Fullscreen (fallback auto-détection).
    """
    screen_cfg = _lire_screen_cfg()
    if screen_cfg is None:
        return None

    assignments = _assignations_depuis(screens, screen_cfg)
    if WindowRole.FULLSCREEN not in {a.role for a in assignments}:
        logger.warning("Config écrans : aucun écran Fullscreen — fallback auto-détection")
        return None

    mode, assignments = _mode_et_assignations(_sans_grille_orpheline(assignments))

    logger.info("Mode config : %s (%d écran(s) assignés)", mode.name, len(screens))
    for a in assignments:
        logger.info("  %s → %s (%s @ %s)", a.role.value, a.name, a.resolution, a.position)

    return DisplayLayout(mode=mode, assignments=assignments)
