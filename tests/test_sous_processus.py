# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Aucun sous-processus ne doit ouvrir de fenêtre.

Sous Windows, démarrer un exécutable de console depuis une application
graphique lui alloue une console : une fenêtre noire qui surgit, prend le
premier plan, puis disparaît. Vingt cellules de grille qui résolvent leur flux,
c'est une pluie de fenêtres qui volent le focus.

Mesuré avant d'écrire ces tests, en comptant les fenêtres de classe console
apparues pendant un appel : une sans le drapeau, zéro avec.

Le garde-fou porte moins sur la fonction — trois lignes — que sur les APPELS :
c'est un oubli à un seul endroit qui ramène la pluie.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

from core.sous_processus import sans_fenetre

RACINE = pathlib.Path(__file__).resolve().parent.parent

#: Fichiers de l'application qui lancent un programme extérieur.
SOURCES = ["core/replay_hd.py", "core/stream_manager.py", "core/version.py",
           "widgets/mpv_widget.py"]


def test_le_drapeau_est_pose_sous_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sans_fenetre() == {"creationflags": subprocess.CREATE_NO_WINDOW}


@pytest.mark.parametrize("plateforme", ["linux", "darwin"])
def test_ailleurs_l_appel_reste_ce_qu_il_etait(monkeypatch, plateforme):
    """Un dictionnaire vide : `subprocess.run(..., **sans_fenetre())` inchangé."""
    monkeypatch.setattr(sys, "platform", plateforme)
    assert sans_fenetre() == {}


def _appels_subprocess(chemin: pathlib.Path):
    """Tous les appels à subprocess.run / Popen du fichier, avec leur ligne."""
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Call):
            continue
        fn = noeud.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("run", "Popen")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            yield noeud


@pytest.mark.parametrize("source", SOURCES)
def test_chaque_lancement_passe_par_sans_fenetre(source):
    """Le garde-fou : un oubli ici et les consoles reviennent."""
    chemin = RACINE / source
    appels = list(_appels_subprocess(chemin))
    assert appels, f"{source} ne lance plus rien : retirer de SOURCES"
    for appel in appels:
        etoiles = [kw for kw in appel.keywords if kw.arg is None]
        deplie = any(isinstance(kw.value, ast.Call)
                     and isinstance(kw.value.func, ast.Name)
                     and kw.value.func.id == "sans_fenetre"
                     for kw in etoiles)
        assert deplie, (
            f"{source}:{appel.lineno} lance un programme sans **sans_fenetre()"
            " — une console apparaîtra sous Windows")


def test_aucun_autre_fichier_de_l_application_ne_lance_de_processus():
    """Si un nouveau point d'appel apparaît, il doit rejoindre SOURCES.

    Sans quoi il échapperait au test précédent, et personne ne le verrait
    avant qu'un utilisateur signale des fenêtres qui s'ouvrent seules.
    """
    connus = {RACINE / s for s in SOURCES}
    oublies = []
    for dossier in ("core", "widgets", "windows"):
        for chemin in (RACINE / dossier).rglob("*.py"):
            if chemin in connus or chemin.name == "sous_processus.py":
                continue
            if list(_appels_subprocess(chemin)):
                oublies.append(str(chemin.relative_to(RACINE)))
    assert oublies == [], f"points d'appel non couverts : {oublies}"
