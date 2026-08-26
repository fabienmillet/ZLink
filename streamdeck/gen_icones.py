#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Produit les images du plugin Stream Deck.

    python streamdeck/gen_icones.py

Deux familles, et les confondre est exactement ce qui rend une touche floue :

* `icones/` — les vignettes de la LISTE des actions, celle où on fait glisser
  une action vers une touche. 20 px de côté, 40 en @2x.
* `touches/` — ce qui s'affiche SUR la touche. 144 px : c'est la définition
  d'un Stream Deck récent, et le logiciel réduit lui-même pour les autres.
  Servir ici l'image de 40 px revient à l'agrandir trois fois et demie, ce qui
  se voit immédiatement.

Le logiciel Elgato refuse de charger un plugin dont une icône manque : ces
fichiers ne sont pas décoratifs, ils conditionnent l'installation. Ils sont
donc générés plutôt que déposés à la main, et se régénèrent à l'identique.

Les glyphes sont tracés ici plutôt qu'empruntés à une police d'icônes : une
touche porte son libellé en bas, il ne reste que le haut pour le dessin, et
chaque geste mérite le sien — six touches « Action » côte à côte toutes
frappées du même éclair ne se distinguent plus.

Tout est dessiné dans un carré de 20 unités, quelle que soit la taille finale.
"""

from __future__ import annotations

import math
import os
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = pathlib.Path(__file__).resolve().parent / "com.zlink.deck.sdPlugin"
LOGO = RACINE / "assets" / "zevent.svg"

#: Vignette de la liste des actions, et icône du plugin dans la fenêtre.
TAILLE_LISTE = 20
TAILLE_PLUGIN = 28

#: Image de touche. Le Stream Deck XL affiche 144 px ; les autres modèles
#: reçoivent la même image, réduite par le logiciel.
TAILLE_TOUCHE = 144

#: Blanc franc : le fond d'une touche est noir, une icône grise s'y perd.
#: L'accent vert est celui de l'application.
BLANC = "#ffffff"
VERT = "#38d18a"
ROUGE = "#ff5f56"
NOIR = "#1d1d1f"


# ── outils de tracé ──────────────────────────────────────────────────────────

def _neuve(taille: int):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    image = QImage(taille, taille, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    return image


def _peintre(image, taille: int, remonter: float = 0.0):
    """Un peintre en unités de 20, éventuellement remonté.

    `remonter` sert aux images de touche : le libellé occupe le bas, et un
    glyphe centré sur la touche le chevaucherait.
    """
    from PyQt6.QtGui import QPainter

    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(taille / 20.0, taille / 20.0)
    if remonter:
        p.translate(0, -remonter)
    return p


def _plume(couleur: str, epaisseur: float):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPen

    stylo = QPen(QColor(couleur), epaisseur)
    stylo.setCapStyle(Qt.PenCapStyle.RoundCap)
    stylo.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return stylo


def _trait(p, couleur: str, epaisseur: float) -> None:
    """Passe le peintre en mode trait : contour, sans remplissage."""
    from PyQt6.QtGui import QBrush

    p.setPen(_plume(couleur, epaisseur))
    p.setBrush(QBrush())


def _plein(p, couleur: str) -> None:
    """Passe le peintre en mode aplat : remplissage, sans contour."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(couleur))


def _triangle(p, pointe_x: float, sens: int, largeur: float, hauteur: float,
              couleur: str = VERT) -> None:
    """Un triangle plein, pointe à gauche ou à droite."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPolygonF

    _plein(p, couleur)
    dos = pointe_x - largeur * sens
    p.drawPolygon(QPolygonF([
        QPointF(pointe_x, 10),
        QPointF(dos, 10 - hauteur / 2),
        QPointF(dos, 10 + hauteur / 2),
    ]))


# ── glyphes ──────────────────────────────────────────────────────────────────

def _logo(p) -> None:
    """Le logo ZEvent, celui de la barre des tâches."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtSvg import QSvgRenderer

    QSvgRenderer(str(LOGO)).render(p, QRectF(1.2, 1.2, 17.6, 17.6))


def _flux(p) -> None:
    """Une cellule de grille, et la lecture qui s'y trouve."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QPolygonF

    _trait(p, BLANC, 1.5)
    p.drawRoundedRect(QRectF(2, 4, 16, 12), 2, 2)
    _plein(p, VERT)
    p.drawPolygon(QPolygonF([QPointF(8.4, 7.2), QPointF(8.4, 12.8),
                             QPointF(13.2, 10.0)]))


def _action(p) -> None:
    """L'éclair : un geste, immédiat. Vignette générique de la famille."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPolygonF

    _plein(p, VERT)
    p.drawPolygon(QPolygonF([
        QPointF(11.5, 2), QPointF(5, 11), QPointF(9.2, 11),
        QPointF(8.5, 18), QPointF(15, 9), QPointF(10.8, 9),
    ]))


def _navigation(p) -> None:
    """Deux chevrons : ce qui précède, ce qui suit."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    _trait(p, BLANC, 2.0)
    for depart, sens in ((8.2, -1), (11.8, 1)):
        chemin = QPainterPath(QPointF(depart, 6.2))
        chemin.lineTo(QPointF(depart + 4.4 * sens, 10))
        chemin.lineTo(QPointF(depart, 13.8))
        p.drawPath(chemin)


def _mixage(p) -> None:
    """Trois curseurs : la console de mixage, en abrégé."""
    from PyQt6.QtCore import QPointF, QRectF

    for x, y in ((5, 12), (10, 7.5), (15, 14)):
        _trait(p, BLANC, 1.6)
        p.drawLine(QPointF(x, 3), QPointF(x, 17))
        _plein(p, VERT)
        p.drawRoundedRect(QRectF(x - 2.6, y - 1.5, 5.2, 3), 1.2, 1.2)


def _clip(p) -> None:
    """Le point d'enregistrement : ce qui vient de passer, gardé."""
    from PyQt6.QtCore import QRectF

    _trait(p, BLANC, 1.5)
    p.drawEllipse(QRectF(3.5, 3.5, 13, 13))
    _plein(p, ROUGE)
    p.drawEllipse(QRectF(7, 7, 6, 6))


def _replay(p) -> None:
    """La flèche qui revient en arrière.

    L'arc est OUVERT en haut : c'est l'ouverture, et la pointe qui la ferme,
    qui distinguent « revenir en arrière » d'un simple cercle.
    """
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QPainterPath, QPolygonF

    cadre = QRectF(4, 4.4, 12, 12)
    _trait(p, BLANC, 1.8)
    chemin = QPainterPath()
    chemin.arcMoveTo(cadre, 120)
    chemin.arcTo(cadre, 120, -300)
    p.drawPath(chemin)
    # La pointe englobe le bout de l'arc — à 120°, soit (7.0 ; 5.2). Posée à
    # côté plutôt que dessus, elle flotte et le dessin se lit comme deux
    # formes sans rapport.
    _plein(p, BLANC)
    p.drawPolygon(QPolygonF([QPointF(4.9, 5.2), QPointF(8.7, 3.2),
                             QPointF(8.7, 7.2)]))


def _bulle(p, pleine: bool) -> None:
    """La bulle de parole, en aplat ou en contour."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QPainterPath, QPolygonF

    queue = QPolygonF([QPointF(6, 12.8), QPointF(6, 17.5),
                       QPointF(10.5, 13.4)])
    if pleine:
        _plein(p, BLANC)
        p.drawRoundedRect(QRectF(2.5, 4, 15, 10), 2.4, 2.4)
        p.drawPolygon(queue)
        _plein(p, NOIR)
    else:
        # Corps et queue RÉUNIS avant le tracé : dessinés séparément, le
        # contour barrerait la bulle là où la queue s'y raccorde.
        corps = QPainterPath()
        corps.addRoundedRect(QRectF(2.5, 4, 15, 10), 2.4, 2.4)
        pointe = QPainterPath()
        pointe.addPolygon(queue)
        _trait(p, BLANC, 1.5)
        p.drawPath(corps.united(pointe))
        _plein(p, BLANC)
    for x in (7, 10, 13):
        p.drawEllipse(QRectF(x - 0.9, 8.1, 1.8, 1.8))


def _chat(p) -> None:
    """Chat fermé : la bulle est un contour."""
    _bulle(p, pleine=False)


def _chat_actif(p) -> None:
    """Chat ouvert : la bulle est pleine."""
    _bulle(p, pleine=True)


def _don(p) -> None:
    """Le cœur : ce pour quoi tout le reste existe."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    _plein(p, VERT)
    chemin = QPainterPath(QPointF(10, 17))
    chemin.cubicTo(QPointF(-1.5, 9.5), QPointF(3.5, 1.5), QPointF(10, 6.6))
    chemin.cubicTo(QPointF(16.5, 1.5), QPointF(21.5, 9.5), QPointF(10, 17))
    p.drawPath(chemin)


def _etoile(p, remplie: bool) -> None:
    """L'étoile à cinq branches, creuse ou pleine."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPolygonF

    points = []
    for i in range(10):
        rayon = 8.2 if i % 2 == 0 else 3.5
        angle = math.radians(-90 + i * 36)
        points.append(QPointF(10 + rayon * math.cos(angle),
                              10 + rayon * math.sin(angle)))
    if remplie:
        _plein(p, VERT)
    else:
        _trait(p, BLANC, 1.5)
    p.drawPolygon(QPolygonF(points))


def _favori(p) -> None:
    """Pas en favori : l'étoile n'est qu'un contour."""
    _etoile(p, remplie=False)


def _favori_actif(p) -> None:
    """En favori : l'étoile est pleine."""
    _etoile(p, remplie=True)


def _haut_parleur(p) -> None:
    """Le corps du haut-parleur, commun aux deux états."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QPolygonF

    _plein(p, BLANC)
    p.drawRect(QRectF(2.6, 7.6, 3.4, 4.8))
    p.drawPolygon(QPolygonF([QPointF(6, 8), QPointF(10.6, 4),
                             QPointF(10.6, 16), QPointF(6, 12)]))


def _muet(p) -> None:
    """Le son passe : haut-parleur et ondes. État AU REPOS de la touche.

    C'est l'état courant qui est dessiné, pas le geste : une touche « Muet »
    qui montre toujours un haut-parleur barré ne dit pas si le son est coupé,
    seulement ce qu'elle ferait — et on appuie pour voir.
    """
    from PyQt6.QtCore import QPointF, QRectF

    _haut_parleur(p)
    for rayon, epaisseur in ((2.6, 1.5), (5.0, 1.5)):
        _trait(p, BLANC, epaisseur)
        p.drawArc(QRectF(10.6 - rayon, 10 - rayon, rayon * 2, rayon * 2),
                  -55 * 16, 110 * 16)


def _muet_actif(p) -> None:
    """Le son est coupé : les ondes remplacées par une croix."""
    from PyQt6.QtCore import QPointF

    _haut_parleur(p)
    _trait(p, ROUGE, 1.9)
    p.drawLine(QPointF(13, 7.2), QPointF(17.4, 12.8))
    p.drawLine(QPointF(17.4, 7.2), QPointF(13, 12.8))


def _precedent(p) -> None:
    _triangle(p, 6.2, -1, 5.4, 10)


def _suivant(p) -> None:
    _triangle(p, 13.8, 1, 5.4, 10)


def _page_precedente(p) -> None:
    _triangle(p, 3.4, -1, 4.6, 8.6)
    _triangle(p, 9.8, -1, 4.6, 8.6)


def _page_suivante(p) -> None:
    _triangle(p, 10.2, 1, 4.6, 8.6)
    _triangle(p, 16.6, 1, 4.6, 8.6)


# ── ce qu'on produit ─────────────────────────────────────────────────────────

#: Vignettes de la liste des actions : nom → (glyphe, taille de base).
VIGNETTES = {
    "plugin": (_logo, TAILLE_PLUGIN),
    "categorie": (_logo, TAILLE_PLUGIN),
    "flux": (_flux, TAILLE_LISTE),
    "action": (_action, TAILLE_LISTE),
    "navigation": (_navigation, TAILLE_LISTE),
    "mixage": (_mixage, TAILLE_LISTE),
}

#: Images de touche. Les quatre premières servent d'état par défaut dans le
#: manifeste ; les suivantes sont posées par le plugin selon le réglage de la
#: touche — c'est ce qui distingue « Clip » de « Chat » d'un coup d'œil.
TOUCHES = {
    "flux": _flux,
    "action": _action,
    "navigation": _navigation,
    "mixage": _mixage,
    "action-clip": _clip,
    "action-replay": _replay,
    "action-chat": _chat,
    "action-don": _don,
    "action-favori": _favori,
    "action-muet": _muet,
    # Variantes « engagées ». Le plugin les choisit selon ce que ZLink
    # publie ; à défaut de fichier, il retombe sur l'état au repos.
    "action-muet-actif": _muet_actif,
    "action-favori-actif": _favori_actif,
    "action-chat-actif": _chat_actif,
    "navigation-precedent": _precedent,
    "navigation-suivant": _suivant,
    "navigation-page_precedente": _page_precedente,
    "navigation-page_suivante": _page_suivante,
}

#: De combien le glyphe d'une touche est remonté, en unités de 20. Le libellé
#: s'écrit sur les quelques unités du bas.
REMONTEE = 1.6


def _ecrire(image, chemin: pathlib.Path) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(chemin), "PNG")


def _vignettes() -> int:
    combien = 0
    for nom, (glyphe, base) in VIGNETTES.items():
        for suffixe, facteur in (("", 1), ("@2x", 2)):
            taille = base * facteur
            image = _neuve(taille)
            p = _peintre(image, taille)
            glyphe(p)
            p.end()
            _ecrire(image, PLUGIN / "icones" / f"{nom}{suffixe}.png")
            combien += 1
    return combien


def _touches() -> int:
    for nom, glyphe in TOUCHES.items():
        image = _neuve(TAILLE_TOUCHE)
        p = _peintre(image, TAILLE_TOUCHE, remonter=REMONTEE)
        glyphe(p)
        p.end()
        _ecrire(image, PLUGIN / "touches" / f"{nom}.png")
    return len(TOUCHES)


def main() -> int:
    if not LOGO.exists():
        print(f"logo introuvable : {LOGO}", file=sys.stderr)
        return 1

    # Le rendu hors écran suffit : aucune fenêtre n'est ouverte, mais QPainter
    # a besoin qu'une application graphique existe.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication([])
    combien = _vignettes() + _touches()
    print(f"   {combien} images ecrites "
          f"({len(VIGNETTES)} vignettes, {len(TOUCHES)} touches)")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
