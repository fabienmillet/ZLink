# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Résolution des chemins — où l'application lit ses ressources et écrit sa
configuration.

Deux erreurs sont coûteuses ici, et ce sont elles que ces tests protègent :

- écrire la configuration ailleurs que là où `ZLINK_CONFIG` le demande, ce qui
  ferait déborder une instance de test sur la configuration réelle ;
- confondre la racine des ressources et la racine des données une fois
  empaqueté, ce qui ferait écrire l'exécutable dans son propre dossier
  d'installation — interdit sous « Program Files ».

`core.paths` décide tout à l'import : chaque cas exige donc un `importlib.reload`
sous un environnement modifié, puis une remise à l'état d'origine.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys

import pytest

import core.paths


@pytest.fixture
def recharge():
    """Recharge `core.paths` sous un environnement choisi, puis le rétablit.

    On n'utilise pas `monkeypatch` pour les variables restaurées ici : ses
    annulations se joueraient APRÈS la fin de cette fixture, donc après le
    rechargement final, qui repartirait alors d'un environnement encore trafiqué.
    """
    env_origine = dict(os.environ)
    frozen_origine = getattr(sys, "frozen", None)
    meipass_origine = getattr(sys, "_MEIPASS", None)

    def recharger(*, frozen: bool = False, meipass: str | None = None, **env):
        for cle, valeur in env.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        if frozen:
            sys.frozen = True          # type: ignore[attr-defined]
        elif hasattr(sys, "frozen"):
            del sys.frozen             # type: ignore[attr-defined]
        if meipass is not None:
            sys._MEIPASS = meipass     # type: ignore[attr-defined]
        elif hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS           # type: ignore[attr-defined]
        return importlib.reload(core.paths)

    yield recharger

    os.environ.clear()
    os.environ.update(env_origine)
    for nom, valeur in (("frozen", frozen_origine), ("_MEIPASS", meipass_origine)):
        if valeur is None:
            if hasattr(sys, nom):
                delattr(sys, nom)
        else:
            setattr(sys, nom, valeur)
    importlib.reload(core.paths)


# ── ZLINK_CONFIG ─────────────────────────────────────────────────────────────

def test_zlink_config_impose_le_fichier_de_configuration(recharge, tmp_path):
    """C'est la variable dont dépend toute l'isolation des tests."""
    cible = tmp_path / "ailleurs" / "config.json"
    paths = recharge(ZLINK_CONFIG=str(cible))
    assert paths.CONFIG_PATH == cible


def test_zlink_config_developpe_le_tilde(recharge):
    """Une valeur saisie à la main vaut souvent « ~/… » ; le tilde n'est pas un
    nom de dossier valide."""
    paths = recharge(ZLINK_CONFIG="~/zlink-perso.json")
    assert "~" not in str(paths.CONFIG_PATH)
    assert paths.CONFIG_PATH == pathlib.Path.home() / "zlink-perso.json"


@pytest.mark.parametrize("valeur", ["", "   ", None])
def test_zlink_config_vide_retombe_sur_la_racine_des_donnees(recharge, valeur):
    """Une variable vide ou blanche ne doit pas produire un chemin absurde."""
    paths = recharge(ZLINK_CONFIG=valeur)
    assert paths.CONFIG_PATH == paths.DATA_ROOT / "config.json"


def test_zlink_config_est_nettoye_de_ses_espaces(recharge, tmp_path):
    cible = tmp_path / "config.json"
    paths = recharge(ZLINK_CONFIG=f"  {cible}  ")
    assert paths.CONFIG_PATH == cible


# ── sources vs exécutable empaqueté ──────────────────────────────────────────

def test_depuis_les_sources_tout_reste_dans_le_depot(recharge):
    """Lancé depuis le dépôt, rien ne doit partir dans le profil utilisateur."""
    paths = recharge(ZLINK_CONFIG=None)
    assert paths.FROZEN is False
    assert paths.DATA_ROOT == paths.RESOURCE_ROOT
    assert paths.PROJECT_ROOT == paths.RESOURCE_ROOT
    # La racine est bien celle du dépôt : core/paths.py en dépend directement.
    assert (paths.RESOURCE_ROOT / "core" / "paths.py").is_file()


def test_empaquete_les_ressources_viennent_de_meipass(recharge, tmp_path):
    """PyInstaller extrait les fichiers livrés dans _MEIPASS, pas dans le dépôt."""
    paths = recharge(frozen=True, meipass=str(tmp_path / "extraction"))
    assert paths.FROZEN is True
    assert paths.RESOURCE_ROOT == tmp_path / "extraction"
    assert paths.PROJECT_ROOT == paths.RESOURCE_ROOT


def test_empaquete_sans_meipass_retombe_sur_le_dossier_de_l_executable(recharge):
    """Cas onedir : pas de _MEIPASS, les ressources sont à côté du binaire."""
    paths = recharge(frozen=True, meipass=None)
    assert paths.RESOURCE_ROOT == pathlib.Path(sys.executable).resolve().parent


def test_empaquete_les_donnees_quittent_le_dossier_d_installation(recharge, tmp_path):
    """Le point sensible : un exécutable installé sous « Program Files » n'a pas
    le droit d'écrire chez lui, la configuration doit partir dans le profil."""
    paths = recharge(frozen=True, meipass=str(tmp_path / "extraction"),
                     ZLINK_CONFIG=None)
    assert paths.DATA_ROOT != paths.RESOURCE_ROOT
    assert paths.CONFIG_PATH == paths.DATA_ROOT / "config.json"


def test_zlink_config_prime_meme_empaquete(recharge, tmp_path):
    cible = tmp_path / "impose.json"
    paths = recharge(frozen=True, meipass=str(tmp_path), ZLINK_CONFIG=str(cible))
    assert paths.CONFIG_PATH == cible


# ── dossier de configuration par plateforme ──────────────────────────────────

def test_dossier_utilisateur_windows_suit_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(pathlib.Path("D:/Profil/AppData/Roaming")))
    attendu = pathlib.Path("D:/Profil/AppData/Roaming") / "ZLink"
    assert core.paths._dossier_utilisateur() == attendu


def test_dossier_utilisateur_windows_sans_appdata_utilise_le_home(monkeypatch):
    """APPDATA manque sur certaines sessions de service : ne pas planter."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert core.paths._dossier_utilisateur() == pathlib.Path.home() / "ZLink"


def test_dossier_utilisateur_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    attendu = pathlib.Path.home() / "Library" / "Application Support" / "ZLink"
    assert core.paths._dossier_utilisateur() == attendu


def test_dossier_utilisateur_linux_suit_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert core.paths._dossier_utilisateur() == tmp_path / "xdg" / "zlink"


def test_dossier_utilisateur_linux_sans_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert core.paths._dossier_utilisateur() == pathlib.Path.home() / ".config" / "zlink"


def test_les_chemins_sont_absolus(recharge):
    """Lancer l'application depuis un autre dossier ne doit rien déplacer."""
    paths = recharge(ZLINK_CONFIG=None)
    assert paths.RESOURCE_ROOT.is_absolute()
    assert paths.DATA_ROOT.is_absolute()
    assert paths.CONFIG_PATH.is_absolute()
