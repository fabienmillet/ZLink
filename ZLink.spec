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

import pathlib
import re
import shutil
import subprocess
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

# Extension Stream Deck, livrée AVEC l'application.
#
# Sans elle, l'installer supposerait de trouver un dossier caché dans %APPDATA%
# et de savoir qu'il faut redémarrer le logiciel Elgato : le bouton des
# paramètres (core/streamdeck_install.py) ne peut poser que ce qu'il a sous la
# main. zlink-deck.exe est produit juste avant par streamdeck/construire.py ;
# s'il manque, on embarque quand même le reste, et le bouton dira que
# l'extension livrée est incomplète plutôt que de disparaître sans explication.
_EXTENSION = os.path.join("streamdeck", "com.zlink.deck.sdPlugin")
if sys.platform.startswith("win") and os.path.isdir(_EXTENSION):
    donnees.append((_EXTENSION, _EXTENSION))

# Icône : un seul SVG fait autorité (assets/zevent.svg), scripts/gen_icons.py
# en dérive les conteneurs. Windows veut un .ico, macOS un .icns ; Linux n'en
# lit aucun ici, l'icône de fenêtre y est posée par l'application elle-même.
_ico = os.path.join("assets", "icons", "zlink.ico")
_icns = os.path.join("assets", "icons", "zlink.icns")
if sys.platform.startswith("win"):
    ICONE = _ico if os.path.exists(_ico) else None
elif sys.platform == "darwin":
    ICONE = _icns if os.path.exists(_icns) else None
else:
    ICONE = None

# La version se lit dans core/version.py plutôt que d'être recopiée : deux
# endroits à modifier, c'est un endroit oublié le jour d'une publication.
_VERSION = re.search(
    r'__version__ = "([^"]+)"',
    pathlib.Path("core/version.py").read_text(encoding="utf-8"),
).group(1)

# Métadonnées de l'exécutable Windows.
#
# Sans ce bloc, ZLink.exe ne porte ni éditeur, ni description, ni version : dans
# les propriétés du fichier et le gestionnaire de tâches, il est anonyme. Un
# binaire anonyme, non signé et fraîchement apparu coche plusieurs cases des
# moteurs heuristiques. Ça n'est pas un remède, mais ça retire un signal.
_MORCEAUX = _VERSION.split("-")[0].split(".")
_QUADRUPLET = tuple(int(x) for x in (_MORCEAUX + ["0", "0", "0", "0"])[:4])

INFOS_VERSION = None
if sys.platform.startswith("win"):
    # 040C = français (France), 04B0 = page de codes Unicode. Le contenu reste
    # en ASCII : PyInstaller relit ce fichier, et une surprise d'encodage sous
    # Windows casserait la construction pour un gain nul.
    _texte = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_QUADRUPLET},
    prodvers={_QUADRUPLET},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('CompanyName', 'Fabien MILLET'),
      StringStruct('FileDescription', 'ZLink - Multiscreen app for ZEvent'),
      StringStruct('FileVersion', '{_VERSION}'),
      StringStruct('InternalName', 'ZLink'),
      StringStruct('LegalCopyright',
                   'Copyright (C) 2026 Fabien MILLET - GNU GPL v3 or later'),
      StringStruct('OriginalFilename', 'ZLink.exe'),
      StringStruct('ProductName', 'ZLink'),
      StringStruct('ProductVersion', '{_VERSION}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])])
  ]
)
"""
    _fichier = pathlib.Path("build") / "version_info.txt"
    _fichier.parent.mkdir(parents=True, exist_ok=True)
    _fichier.write_text(_texte, encoding="utf-8")
    INFOS_VERSION = str(_fichier)

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

# ─── Linux : laisser à l'hôte les bibliothèques qui lui appartiennent ────────
#
# PyInstaller ramasse les dépendances natives telles qu'elles sont sur la
# machine de construction et les pose dans _internal/, que le lanceur met
# DEVANT les chemins système. Une bibliothèque embarquée masque donc celle de
# l'hôte pour tout le processus — y compris pour du code qui n'est pas le
# nôtre. Et le binaire de release est bâti sur ubuntu-22.04 exprès, pour viser
# une vieille glibc : ce qu'on embarque est par construction plus ancien que ce
# qu'une distribution à jour installe.
#
# Deux masquages rendaient le paquet Linux inutilisable ailleurs que sur le
# runner :
#
#   - Le runtime GCC. Celui d'Ubuntu 22.04 s'arrête à GLIBCXX_3.4.30, quand le
#     pilote Mesa de l'hôte (bâti avec GCC 13+) en réclame davantage. Mesa
#     charge le nôtre, n'y trouve pas ses symboles, et le pilote ne s'initialise
#     pas : « Could not initialize GLX », puis Qt abandonne au démarrage.
#
#   - libmpv et sa fermeture. Le README dit que mpv vient du système sous Linux,
#     et widgets/mpv_widget.py ne cherche de libmpv embarquée que sous les noms
#     de l'ABI 2 : celle du runner (libmpv.so.1, mpv 0.34) n'est donc jamais
#     utilisée. Elle était pourtant embarquée — PyInstaller résout le
#     ctypes.util.find_library('mpv') de python-mpv à la construction — et ses
#     dépendances masquaient celles de la libmpv de l'hôte, qui ne se chargeait
#     alors plus du tout (« undefined symbol: vaMapBuffer2 »).
#
# On écarte donc libmpv et TOUT ce qu'elle tire, plutôt qu'une liste de noms
# constatés : la fermeture se recalcule à chaque construction et suit le mpv du
# runner sans qu'on ait à la tenir à jour.


def _dependances_declarees(chemin):
    """SONAME listés en DT_NEEDED par un binaire ELF."""
    sortie = subprocess.run(
        ["objdump", "-p", chemin], capture_output=True, text=True, check=True
    ).stdout
    return re.findall(r"NEEDED\s+(\S+)", sortie)


def _fermeture(racines, index):
    """`racines` et, transitivement, tout ce dont elles dépendent dans le paquet."""
    vus, a_voir = set(), list(racines)
    while a_voir:
        nom = a_voir.pop()
        if nom in vus or nom not in index:
            continue
        vus.add(nom)
        a_voir += _dependances_declarees(index[nom])
    return vus


def _ecarter_bibliotheques_de_l_hote(binaires):
    index = {os.path.basename(nom): chemin for nom, chemin, _ in binaires}
    ecartes = _fermeture(
        [nom for nom in index if nom.startswith("libmpv.so")], index
    )
    # Le runtime GCC arrive aussi par l'ICU de Qt : il survivrait à la
    # disparition de libmpv du paquet, on le nomme donc séparément.
    ecartes |= {
        nom for nom in index
        if nom.startswith(("libstdc++.so", "libgcc_s.so"))
    }
    return [e for e in binaires if os.path.basename(e[0]) not in ecartes]


if sys.platform.startswith("linux"):
    # objdump absent : mieux vaut rater la construction que livrer un paquet qui
    # ne démarrera pas, avec un message que personne ne rattachera à ça.
    if shutil.which("objdump") is None:
        raise SystemExit(
            "ZLink.spec : objdump est requis pour construire sous Linux "
            "(paquet binutils)."
        )
    a.binaries = _ecarter_bibliotheques_de_l_hote(a.binaries)
    sl.binaries = _ecarter_bibliotheques_de_l_hote(sl.binaries)


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
    icon=ICONE,
    version=INFOS_VERSION,
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
        icon=ICONE,
        bundle_identifier="fr.zipname.zlink",
        info_plist={
            "CFBundleShortVersionString": _VERSION,
            "CFBundleVersion": _VERSION,
            "NSHighResolutionCapable": True,
            # L'application n'accède ni au micro ni à la caméra ; on ne déclare
            # que ce qu'elle fait réellement.
            "LSMinimumSystemVersion": "12.0",
        },
    )
