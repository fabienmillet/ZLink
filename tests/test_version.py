# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Comparaison de versions — ce qui décide si une mise à jour est proposée."""

from __future__ import annotations

import pytest

from core.version import display_version, is_newer, parse


@pytest.mark.parametrize("brut,attendu", [
    ("1.2.3", (1, 2, 3, "")),
    ("v1.2.3", (1, 2, 3, "")),
    ("  v1.2.3  ", (1, 2, 3, "")),
    ("1.2.3-beta.1", (1, 2, 3, "beta.1")),
    ("0.0.0", (0, 0, 0, "")),
    ("10.20.30", (10, 20, 30, "")),
])
def test_parse_des_formes_reconnues(brut, attendu):
    assert parse(brut) == attendu


@pytest.mark.parametrize("brut", ["", None, "1.2", "abc", "1.2.3.4", "v", "1..3"])
def test_parse_rend_none_sur_le_reste(brut):
    assert parse(brut) is None


@pytest.mark.parametrize("candidat,courant", [
    ("1.2.4", "1.2.3"),
    ("1.3.0", "1.2.9"),
    ("2.0.0", "1.9.9"),
    ("1.2.3", "1.2.3-rc1"),      # la finale bat la pré-version
    ("1.2.3-rc2", "1.2.3-rc1"),
])
def test_plus_recent(candidat, courant):
    assert is_newer(candidat, courant) is True


@pytest.mark.parametrize("candidat,courant", [
    ("1.2.3", "1.2.3"),
    ("1.2.3", "1.2.4"),
    ("1.2.3-rc1", "1.2.3"),      # une pré-version ne remplace pas la finale
    ("0.9.9", "1.0.0"),
])
def test_pas_plus_recent(candidat, courant):
    assert is_newer(candidat, courant) is False


@pytest.mark.parametrize("candidat,courant", [
    ("pas une version", "1.2.3"),
    ("1.2.3", "pas une version"),
    ("", "1.2.3"),
    (None, "1.2.3"),
])
def test_version_illisible_n_est_jamais_proposee(candidat, courant):
    """On ne propose pas une mise à jour dont on n'a pas su lire le numéro."""
    assert is_newer(candidat, courant) is False


def test_display_version_est_une_chaine_non_vide():
    v = display_version()
    assert isinstance(v, str) and v
