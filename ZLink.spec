# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
#
# Recette d'empaquetage, écrite en fichier .spec plutôt qu'en options de ligne
# de commande : les chemins de données ont un séparateur différent selon la
# plateforme, et une recette unique évite trois invocations divergentes.
#
# Produit un dossier (« onedir ») et non un fichier unique : le mode onefile
# extrait tout dans un répertoire temporaire à chaque lancement, ce qui coûte
# plusieurs secondes avec Qt et WebEngine, et complique la cohabitation avec
# libmpv et streamlink qui doivent être trouvés à côté de l'exécutable.

import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

donnees = [("assets", "assets")]
binaires_extra: list[tuple[str, str]] = []

import os

# libmpv n'est pas une dépendance Python : elle est chargée par ctypes à
# l'exécution, donc PyInstaller ne la voit pas. Elle doit être ajoutée à la
# main, sous le nom que widgets/mpv_widget.py cherchera dans le paquet.
if sys.platform.startswith("win"):
    if os.path.exists("libmpv-2.dll"):
        donnees.append(("libmpv-2.dll", "."))
    if os.path.exists("libmpv-2.dll.sha256"):
        donnees.append(("libmpv-2.dll.sha256", "."))
elif sys.platform == "darwin":
    # Déposée par le workflow depuis Homebrew. Ses propres dépendances (ffmpeg
    # et consorts) sont rapatriées après coup par dylibbundler.
    #
    # Déclarée en BINAIRE et non en donnée : dans un .app, PyInstaller range les
    # données sous Contents/Resources et les binaires sous Contents/Frameworks.
    # Du code scellé comme ressource n'est pas signé comme du code, ce que la
    # notarisation refuse.
    if os.path.exists("libmpv.2.dylib"):
        binaires_extra.append(("libmpv.2.dylib", "."))

qta_datas, qta_binaires, qta_imports = collect_all("qtawesome")
donnees += qta_datas

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=qta_binaires + binaires_extra,
    datas=donnees,
    # streamlink est lancé comme un PROCESSUS séparé, mais ses plugins sont
    # chargés dynamiquement : sans cette collecte, l'exécutable streamlink
    # produit à côté ne saurait pas lire Twitch.
    hiddenimports=(
        qta_imports
        + collect_submodules("streamlink")
        + ["PyQt6.QtMultimedia", "Crypto.Signature.eddsa", "Crypto.PublicKey.ECC"]
        # Écrit juste avant la construction ; l'import vit dans un try, et
        # n'existe pas du tout dans un dépôt de travail.
        + (["core.build_info"] if os.path.exists("core/build_info.py") else [])
    ),
    hookspath=[],
    runtime_hooks=[],
    # Modules volumineux dont ZLink ne se sert pas.
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)
# Second exécutable : streamlink. ZLink l'invoque comme un processus séparé, et
# une version empaquetée n'a pas d'interpréteur Python pour le faire tourner.
# Il est posé à côté de l'application, là où _streamlink_exe() cherche d'abord.
sl = Analysis(
    ["packaging/streamlink_entry.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("streamlink") + collect_submodules("streamlink_cli"),
    excludes=["tkinter", "test", "unittest", "PyQt6"],
    noarchive=False,
)

pyz = PYZ(a.pure)
pyz_sl = PYZ(sl.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZLink",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

exe_sl = EXE(
    pyz_sl,
    sl.scripts,
    [],
    exclude_binaries=True,
    name="streamlink",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    exe_sl,
    sl.binaries,
    sl.datas,
    strip=False,
    upx=False,
    name="ZLink",
)

# macOS : produire un vrai paquet .app. Un simple dossier ne peut être ni
# signé, ni notarisé, ni agrafé — et Gatekeeper refuse alors de le lancer.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ZLink.app",
        icon=None,
        bundle_identifier="fr.zipname.zlink",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            # L'application n'accède ni au micro ni à la caméra ; on ne déclare
            # que ce qu'elle fait réellement.
            "LSMinimumSystemVersion": "12.0",
        },
    )
