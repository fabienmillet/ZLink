# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Isolation des tests.

Deux précautions, prises AVANT tout import de `core` :

- `ZLINK_CONFIG` déplace le fichier de configuration dans un dossier temporaire.
  Sans lui, le moindre test qui enregistre un favori écrirait dans la
  configuration réelle de l'utilisateur — clés API comprises.
- `QT_QPA_PLATFORM=offscreen` évite qu'un widget testé n'ouvre une fenêtre.

conftest.py est chargé par pytest avant les modules de test, donc avant que
`core.paths` ne lise l'environnement à l'import.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="zlink-tests-"))
os.environ["ZLINK_CONFIG"] = str(_TMP / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Le dépôt doit être importable même si pytest est lancé d'ailleurs.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture
def config_vierge(tmp_path, monkeypatch):
    """Redirige CONFIG_PATH vers un fichier neuf, pour un test qui écrit.

    Les modules capturent CONFIG_PATH à l'import : il faut donc le remplacer
    dans CHACUN d'eux, pas seulement dans core.paths.
    """
    cible = tmp_path / "config.json"
    import core.config_store
    import core.favorites
    import core.paths
    import core.selection_store

    for module in (core.paths, core.config_store, core.favorites,
                   core.selection_store):
        if hasattr(module, "CONFIG_PATH"):
            monkeypatch.setattr(module, "CONFIG_PATH", cible)
    return cible
