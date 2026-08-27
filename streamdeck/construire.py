#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Fabrique et installe l'extension Stream Deck.

    streamdeck/.venv/Scripts/python streamdeck/construire.py            # bâtir
    streamdeck/.venv/Scripts/python streamdeck/construire.py --installer

Le logiciel Elgato lance un exécutable, pas un script Python : il faut donc
en produire un. PyInstaller s'en charge, en mode fenêtré — sans quoi une
console noire resterait ouverte tant que le Stream Deck est branché.

Ce qui sort :

* `com.zlink.deck.sdPlugin/zlink-deck.exe` — le plugin lui-même ;
* `dist/zlink-deck.streamDeckPlugin` — l'archive à double-cliquer.

`--installer` copie en plus le dossier dans celui du logiciel Elgato. C'est
la voie sûre : elle ne dépend pas de la manière dont le logiciel lit les
archives, et c'est ainsi que se testent tous les plugins en développement.
Le logiciel ne relit ses plugins qu'au démarrage — il faut le quitter et le
relancer pour voir ZLink apparaître.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

ICI = pathlib.Path(__file__).resolve().parent
PLUGIN = ICI / "com.zlink.deck.sdPlugin"
SOURCE = PLUGIN / "zlink_deck.py"
DIST = ICI / "dist"
TRAVAIL = ICI / "build"
NOM_EXE = "zlink-deck"

#: Ce qui part dans l'archive et dans le dossier installé. Tout le reste —
#: journal, cache Python, exécutable d'une build précédente — reste ici.
EXCLUS = {"__pycache__", "zlink-deck.log", "build", "dist"}


def _dossier_elgato() -> pathlib.Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA absent : cette commande est pour Windows")
    return pathlib.Path(appdata) / "Elgato" / "StreamDeck" / "Plugins"


def _python_avec_qt() -> str | None:
    """Un interpréteur qui sait dessiner : les icônes passent par Qt.

    L'environnement du plugin, lui, n'a que websockets et Pillow — inutile d'y
    installer PyQt6 pour six PNG que le dépôt garde de toute façon.
    """
    candidats = [ICI.parent / ".venv" / "Scripts" / "python.exe", sys.executable]
    for candidat in candidats:
        try:
            essai = subprocess.run([str(candidat), "-c", "import PyQt6.QtSvg"],
                                   capture_output=True)
        except OSError:
            # Un candidat qui n'existe pas n'est pas une panne : c'est le cas
            # normal sur un poste sans .venv, et sur les machines de la CI. La
            # boucle passe au suivant, et le repli sur les icônes du dépôt
            # reste possible — il était déjà prévu plus bas.
            continue
        if essai.returncode == 0:
            return str(candidat)
    return None


def _icones() -> None:
    """Régénère les icônes. Sans elles, le logiciel refuse le plugin."""
    print("-> icones")
    interprete = _python_avec_qt()
    if interprete is None:
        manquantes = [n for n in ("plugin", "flux", "action", "navigation",
                                  "mixage", "categorie")
                      if not (PLUGIN / "icones" / f"{n}.png").exists()]
        if manquantes:
            raise RuntimeError(
                "PyQt6 introuvable et icones manquantes : " + ", ".join(manquantes))
        print("   PyQt6 absent, on garde celles du depot")
        return
    subprocess.run([interprete, str(ICI / "gen_icones.py")], check=True)


def _profils() -> None:
    """Régénère les profils prêts à l'emploi, livrés dans le plugin."""
    print("-> profils")
    subprocess.run([sys.executable, str(ICI / "gen_profils.py")], check=True)


def _executable() -> pathlib.Path:
    print("-> executable")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        # Fenêtré : le plugin n'a rien à afficher, et une console ouverte
        # pendant toute la session est exactement ce qu'on ne veut pas.
        "--noconsole",
        "--name", NOM_EXE,
        "--distpath", str(DIST),
        "--workpath", str(TRAVAIL),
        "--specpath", str(TRAVAIL),
        "--noconfirm",
        str(SOURCE),
    ], check=True, cwd=str(ICI))
    produit = DIST / f"{NOM_EXE}.exe"
    cible = PLUGIN / f"{NOM_EXE}.exe"
    shutil.copy2(produit, cible)
    print(f"   {cible.relative_to(ICI.parent)}"
          f"  ({cible.stat().st_size // 1024} Ko)")
    return cible


def _fichiers_du_plugin():
    """Les fichiers à embarquer, chemin relatif compris."""
    for chemin in sorted(PLUGIN.rglob("*")):
        if not chemin.is_file():
            continue
        relatif = chemin.relative_to(PLUGIN)
        if any(part in EXCLUS for part in relatif.parts):
            continue
        yield chemin, relatif


def _archive() -> pathlib.Path:
    print("-> archive")
    DIST.mkdir(parents=True, exist_ok=True)
    cible = DIST / f"{NOM_EXE}.streamDeckPlugin"
    with zipfile.ZipFile(cible, "w", zipfile.ZIP_DEFLATED) as zip_:
        for chemin, relatif in _fichiers_du_plugin():
            zip_.write(chemin, str(pathlib.Path(PLUGIN.name) / relatif))
    print(f"   {cible.relative_to(ICI.parent)}"
          f"  ({cible.stat().st_size // 1024} Ko)")
    return cible


def _installer() -> pathlib.Path:
    print("-> installation")
    cible = _dossier_elgato() / PLUGIN.name
    # On remplace, on ne fusionne pas : un fichier d'une version précédente
    # qui traîne est plus difficile à diagnostiquer qu'une réinstallation.
    if cible.exists():
        shutil.rmtree(cible)
    for chemin, relatif in _fichiers_du_plugin():
        destination = cible / relatif
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chemin, destination)
    print(f"   {cible}")
    print("   Quitter puis relancer le logiciel Stream Deck pour le voir.")
    return cible


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--installer", action="store_true",
                           help="copier aussi dans le dossier du logiciel Elgato")
    analyseur.add_argument("--sans-exe", action="store_true",
                           help="réutiliser l'exécutable déjà bâti")
    options = analyseur.parse_args()

    # La console Windows est en cp1252 : sans cela, le moindre accent dans un
    # message d'avancement fait echouer la construction.
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    _icones()
    _profils()
    if not options.sans_exe:
        _executable()
    elif not (PLUGIN / f"{NOM_EXE}.exe").exists():
        print("aucun exécutable à réutiliser", file=sys.stderr)
        return 1
    _archive()
    if options.installer:
        _installer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
