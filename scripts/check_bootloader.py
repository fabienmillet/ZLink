#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Vérifie que le bootloader de PyInstaller a bien été recompilé.

Le bootloader livré sur PyPI est identique octet pour octet dans des milliers
d'échantillons malveillants — PyInstaller est très employé pour distribuer du
maliciel Python. Plusieurs moteurs antivirus le signalent donc tel quel, et
l'application en hérite des détections génériques.

La chaîne de publication le recompile. Mais le sdist contient déjà les binaires
Windows : sans la variable `PYINSTALLER_COMPILE_BOOTLOADER`, pip les réutilise
sans rien compiler et l'étape ne sert à rien, silencieusement. D'où ce contrôle,
qui compare l'empreinte du bootloader installé à celle de la roue officielle.

    python scripts/check_bootloader.py [nom-du-bootloader]

Sort en erreur si les deux coïncident.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys
import tempfile
import zipfile


def empreinte_locale(motif: str) -> tuple[pathlib.Path, str]:
    """Bootloader installé dans l'environnement courant, et son empreinte."""
    import PyInstaller

    racine = pathlib.Path(PyInstaller.__file__).parent / "bootloader"
    chemin = next(racine.glob(motif), None)
    if chemin is None:
        raise SystemExit(
            f"::error::aucun bootloader ne correspond à « {motif} » sous {racine}"
        )
    return chemin, hashlib.sha256(chemin.read_bytes()).hexdigest()


def empreinte_officielle(nom: str) -> str:
    """Même bootloader, tel qu'il est publié sur PyPI."""
    dossier = tempfile.mkdtemp(prefix="pyi-officiel-")
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--only-binary", ":all:",
         "--no-deps", "--no-cache-dir", "pyinstaller", "-d", dossier],
        check=True, stdout=subprocess.DEVNULL,
    )
    roue = next(pathlib.Path(dossier).glob("*.whl"))
    with zipfile.ZipFile(roue) as z:
        entree = next(
            (n for n in z.namelist() if n.endswith("/" + nom)), None
        )
        if entree is None:
            raise SystemExit(f"::error::{nom} absent de la roue officielle {roue.name}")
        return hashlib.sha256(z.read(entree)).hexdigest()


def main() -> int:
    # runw.exe est le bootloader Windows sans console, celui qu'utilise une
    # application graphique — donc celui qui finit dans ZLink.exe.
    nom = sys.argv[1] if len(sys.argv) > 1 else "runw.exe"
    chemin, locale_ = empreinte_locale(f"*/{nom}")
    officielle = empreinte_officielle(nom)

    print(f"  bootloader   : {chemin}")
    print(f"  construit    : {locale_}")
    print(f"  publié (PyPI): {officielle}")
    if locale_ == officielle:
        raise SystemExit(
            "::error::bootloader identique à celui de PyPI — la recompilation "
            "n'a pas eu lieu (PYINSTALLER_COMPILE_BOOTLOADER manquant ?)"
        )
    print("  recompilation confirmée")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
