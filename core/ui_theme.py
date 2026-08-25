# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Apparence des composants que ZLink ne peint pas lui-même.

Presque tout est stylé explicitement dans l'application. Restent les éléments
que Qt crée pour son compte — menus contextuels, infobulles, listes déroulantes
— qui retombent sur la palette du thème système. Sous un bureau où celle-ci
mélange fonds sombres et texte noir, le résultat est du noir sur noir :
illisible. Le menu contextuel de la grille en était l'exemple.

La palette posée ici est cohérente avec le reste de l'interface et ne dépend
d'aucun thème installé.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette

# Feuille de style commune aux menus contextuels.
MENU_QSS = (
    "QMenu { background: #111111; border: 1px solid #2a2a2a; color: #cccccc; "
    "padding: 4px; }"
    "QMenu::item { padding: 6px 20px; }"
    "QMenu::item:selected { background: #1e1e1e; color: #ffffff; }"
    "QMenu::separator { height: 1px; background: #2a2a2a; margin: 2px 0; }"
)

_BG        = QColor("#111111")
_BG_ALT    = QColor("#0c0c0c")
_TEXT      = QColor("#e0e0e0")
_TEXT_DIM  = QColor("#6a6a6a")
_ACCENT    = QColor("#00ff87")
_ACCENT_TX = QColor("#08130d")


def apply_dark_palette(app) -> None:
    """Impose une palette sombre lisible, indépendante du thème du bureau."""
    pal = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    for r, c in (
        (role.Window, _BG), (role.WindowText, _TEXT),
        (role.Base, _BG_ALT), (role.AlternateBase, _BG),
        (role.Text, _TEXT),
        (role.Button, _BG), (role.ButtonText, _TEXT),
        (role.ToolTipBase, QColor("#1a1a1a")), (role.ToolTipText, _TEXT),
        (role.Highlight, _ACCENT), (role.HighlightedText, _ACCENT_TX),
        (role.Link, _ACCENT), (role.PlaceholderText, _TEXT_DIM),
        # Nuances employées par le style natif pour dessiner reliefs et
        # bordures. Laissées aux valeurs claires par défaut, elles produisaient
        # des contours blancs sur les contrôles sombres.
        (role.Light, QColor("#2a2a2a")), (role.Midlight, QColor("#222222")),
        (role.Mid, QColor("#1a1a1a")), (role.Dark, QColor("#0a0a0a")),
        (role.Shadow, QColor("#000000")), (role.BrightText, QColor("#ffffff")),
    ):
        pal.setColor(r, c)
    # Un texte désactivé qui garderait la couleur normale ne se distinguerait
    # plus d'un texte actif.
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, _TEXT_DIM)
    app.setPalette(pal)
