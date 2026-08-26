# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Persistance de la sélection de streamers pour la grille."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_PATH = Path.home() / ".zlink" / "grid_selection.json"
MAX_SELECTED = 25


class SelectionStore:
    """Stocke et persiste la liste ordonnée des logins sélectionnés pour la grille.

    L'ordre d'insertion est conservé (numéros de slots).
    """

    def __init__(self) -> None:
        self._selected: list[str] = []   # ordre = ordre de sélection (slot 1, 2, …)
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            if STORE_PATH.exists():
                data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    seen: set[str] = set()
                    self._selected = [
                        x for x in data
                        if isinstance(x, str) and x not in seen and not seen.add(x)  # type: ignore[func-returns-value]
                    ]
                    logger.info("SelectionStore: %d logins chargés (ordre préservé)", len(self._selected))
        except (OSError, ValueError) as exc:
            logger.warning("SelectionStore._load: %s", exc)

    def save(self) -> None:
        try:
            STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Sauvegarder dans l'ordre de sélection, pas alphabétique
            STORE_PATH.write_text(
                json.dumps(self._selected, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, ValueError) as exc:
            logger.warning("SelectionStore.save: %s", exc)

    # -- API ------------------------------------------------------------------

    def is_selected(self, login: str) -> bool:
        return login in self._selected

    def set_selected(self, login: str, selected: bool) -> None:
        if selected:
            if login not in self._selected:
                self._selected.append(login)
        else:
            if login in self._selected:
                self._selected.remove(login)
        self.save()

    def set_all(self, logins: list[str]) -> None:
        """Remplace la sélection complète en préservant l'ordre de la liste passée."""
        seen: set[str] = set()
        self._selected = [
            x for x in logins
            if x not in seen and not seen.add(x)  # type: ignore[func-returns-value]
        ]
        self.save()

    def clear(self) -> None:
        self._selected.clear()
        self.save()

    # -- dispositions nommées --------------------------------------------

    def presets(self) -> dict[str, list[str]]:
        """Dispositions enregistrées : nom → liste de logins."""
        from core import config_store
        raw = config_store.load().get("grid_presets")
        if not isinstance(raw, dict):
            return {}
        return {str(k): [str(x) for x in v]
                for k, v in raw.items() if isinstance(v, list)}

    def save_preset(self, nom: str, logins: list[str]) -> bool:
        """Enregistre la sélection sous un nom. Écrase un homonyme."""
        from core import config_store
        nom = (nom or "").strip()[:40]
        if not nom:
            return False
        presets = self.presets()
        presets[nom] = [str(lg) for lg in logins if lg]
        return config_store.save_merge({"grid_presets": presets})

    def delete_preset(self, nom: str) -> bool:
        from core import config_store
        presets = self.presets()
        if nom not in presets:
            return False
        presets.pop(nom)
        return config_store.save_merge({"grid_presets": presets})

    def get_selected(self) -> list[str]:
        """Retourne les logins sélectionnés dans l'ordre de sélection."""
        return list(self._selected)

    def count(self) -> int:
        return len(self._selected)
