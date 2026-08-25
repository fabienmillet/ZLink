#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Produit toutes les icônes de l'application à partir du SVG source.

Un seul fichier fait autorité : `assets/zevent.svg`. Tout le reste — les PNG,
le .ico de Windows, le .icns de macOS — en découle et se régénère par :

    python scripts/gen_icons.py

Le rendu passe par QSvgRenderer et non par un outil externe : c'est le même
moteur que celui qui dessinera l'icône dans l'application, donc ce qu'on voit
dans la barre des tâches correspond exactement aux fichiers produits.

Les .ico et .icns sont écrits ici plutôt que délégués à ImageMagick, dont le
support de ces deux formats est inégal. Les deux conteneurs sont simples et
acceptent des images PNG telles quelles.
"""

from __future__ import annotations

import pathlib
import struct
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
SOURCE = RACINE / "assets" / "zevent.svg"
SORTIE = RACINE / "assets" / "icons"

#: Tailles rendues. 1024 sert au Retina de macOS, 16 au coin d'une fenêtre.
TAILLES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)

#: Ce que Windows lit dans un .ico. Au-delà de 256 il ignore, en deçà de 16 il
#: rééchantillonne mal : on lui donne exactement ce qu'il sait utiliser.
TAILLES_ICO = (16, 24, 32, 48, 64, 128, 256)

#: Types ICNS et la taille de rendu correspondante. Les types « @2x » portent
#: la même image que leur équivalent simple mais au double de pixels : macOS
#: choisit selon la densité de l'écran.
TYPES_ICNS = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic13", 256),
    (b"ic14", 512),
)


def rend(svg: pathlib.Path, taille: int):
    """Rastérise le SVG en QImage carrée de `taille` pixels, fond transparent."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    moteur = QSvgRenderer(str(svg))
    if not moteur.isValid():
        raise SystemExit(f"SVG illisible : {svg}")
    img = QImage(taille, taille, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    moteur.render(p)
    p.end()
    return img


def en_png(img) -> bytes:
    """Encode une QImage en PNG, en mémoire."""
    from PyQt6.QtCore import QBuffer, QByteArray

    octets = QByteArray()
    tampon = QBuffer(octets)
    tampon.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(tampon, "PNG")
    tampon.close()
    return bytes(octets)


def ecrit_ico(pngs: dict[int, bytes], vers: pathlib.Path) -> None:
    """Assemble un .ico multi-résolution à partir d'images PNG.

    Windows accepte les entrées PNG depuis Vista, et Inno Setup 6 aussi. La
    largeur et la hauteur se codent sur un octet, où 0 signifie 256.
    """
    entrees = sorted(pngs.items())
    entete = struct.pack("<HHH", 0, 1, len(entrees))  # réservé, type icône, n
    decalage = len(entete) + 16 * len(entrees)
    repertoire, donnees = b"", b""
    for taille, png in entrees:
        dim = 0 if taille >= 256 else taille
        repertoire += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), decalage
        )
        donnees += png
        decalage += len(png)
    vers.write_bytes(entete + repertoire + donnees)


def ecrit_icns(rendus: dict[int, bytes], vers: pathlib.Path) -> None:
    """Assemble un .icns à partir d'images PNG.

    Le format est une suite de blocs « type + longueur + données », précédée du
    même en-tête pour le fichier entier. La longueur inclut les huit octets
    d'en-tête du bloc, ce qui est la seule subtilité.
    """
    blocs = b""
    for typ, taille in TYPES_ICNS:
        png = rendus[taille]
        blocs += typ + struct.pack(">I", len(png) + 8) + png
    vers.write_bytes(b"icns" + struct.pack(">I", len(blocs) + 8) + blocs)


def main() -> int:
    if not SOURCE.is_file():
        raise SystemExit(f"Source absente : {SOURCE}")

    # Offscreen : aucune fenêtre ne doit s'ouvrir pour fabriquer des images.
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication(sys.argv[:1])  # noqa: F841 - requis par QPainter

    SORTIE.mkdir(parents=True, exist_ok=True)
    rendus: dict[int, bytes] = {}
    for taille in TAILLES:
        img = rend(SOURCE, taille)
        png = en_png(img)
        rendus[taille] = png
        chemin = SORTIE / f"zlink-{taille}.png"
        chemin.write_bytes(png)
        print(f"  {chemin.relative_to(RACINE)}  {len(png):>7} octets")

    ico = SORTIE / "zlink.ico"
    ecrit_ico({t: rendus[t] for t in TAILLES_ICO}, ico)
    print(f"  {ico.relative_to(RACINE)}  {ico.stat().st_size:>7} octets"
          f"  ({len(TAILLES_ICO)} résolutions)")

    icns = SORTIE / "zlink.icns"
    ecrit_icns(rendus, icns)
    print(f"  {icns.relative_to(RACINE)}  {icns.stat().st_size:>7} octets"
          f"  ({len(TYPES_ICNS)} blocs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
