# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""La page Raccourcis dit-elle la vérité ?

Elle est écrite à la main : `windows/fullscreen.py` range ses touches dans un
dictionnaire construit à la première frappe, à partir de méthodes liées à
l'instance — il n'y a rien à lire tant qu'une fenêtre n'existe pas, et les
réglages s'ouvrent sans plein écran.

Une liste recopiée dérive. Ces tests la rattachent au code : ajouter une touche
au plein écran sans l'écrire dans les réglages fait échouer la suite, avec le
nom de la touche oubliée. Une documentation fausse est pire que pas de
documentation — on essaie la touche annoncée, il ne se passe rien, et on
conclut que la fonction est cassée.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

RACINE = pathlib.Path(__file__).resolve().parent.parent

#: Nom de touche Qt → l'étiquette telle que la page des réglages l'affiche.
#: Une touche absente de cette table n'est documentée nulle part : c'est
#: exactement ce que le test doit attraper.
ETIQUETTES = {
    "Key_Up": "↑ / ↓", "Key_Down": "↑ / ↓",
    "Key_Return": "Entrée / Espace", "Key_Space": "Entrée / Espace",
    "Key_Plus": "+ / −", "Key_Equal": "+ / −", "Key_Minus": "+ / −",
    "Key_M": "M", "Key_C": "C", "Key_R": "R", "Key_F": "F",
    "Key_Left": "← / →", "Key_Right": "← / →",
}


def _touches_du_plein_ecran() -> set[str]:
    """Les `Key_*` posées dans `_carte_des_touches`, lues dans la source.

    Par l'AST et non par import : la carte n'existe qu'une fois une fenêtre
    plein écran construite, ce qui suppose un écran, mpv et un flux.
    """
    arbre = ast.parse((RACINE / "windows" / "fullscreen.py").read_text(
        encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.FunctionDef) and noeud.name == "_carte_des_touches":
            return {a.attr for n in ast.walk(noeud)
                    if isinstance(n, ast.Attribute) and (a := n).attr.startswith("Key_")}
    pytest.fail("_carte_des_touches introuvable dans windows/fullscreen.py")


@pytest.fixture(scope="module")
def documentees() -> dict:
    """Les raccourcis de la page des réglages, par section."""
    from windows.settings import _RACCOURCIS

    return {titre: dict(lignes) for titre, lignes in _RACCOURCIS}


def test_chaque_touche_du_plein_ecran_est_documentee(documentees):
    """Une touche ajoutée au code doit apparaître dans les réglages."""
    annoncees = set(documentees["Plein écran"])
    oubliees = sorted(
        touche for touche in _touches_du_plein_ecran()
        if ETIQUETTES.get(touche) not in annoncees
    )
    assert not oubliees, (
        f"touches actives mais absentes des réglages : {oubliees} — "
        "les ajouter à _RACCOURCIS dans windows/settings.py"
    )


def test_aucun_raccourci_annonce_n_est_mort(documentees):
    """L'inverse : une touche affichée doit être branchée sur quelque chose.

    Retirer une touche du plein écran sans nettoyer la page laisserait une
    promesse que rien ne tient.
    """
    vivantes = {ETIQUETTES[t] for t in _touches_du_plein_ecran()
                if t in ETIQUETTES}
    # Les plages et l'échappement ne passent pas par la carte : ils sont
    # traités avant elle dans keyPressEvent, à part.
    hors_carte = {"1 … 9", "Échap"}
    fantomes = sorted(set(documentees["Plein écran"]) - vivantes - hors_carte)
    assert not fantomes, f"raccourcis annoncés mais non branchés : {fantomes}"


def test_les_plages_traitees_a_part_sont_bien_la():
    """« 1 … 9 » et Échap sont codés en dur avant la carte : ils doivent y rester."""
    source = (RACINE / "windows" / "fullscreen.py").read_text(encoding="utf-8")
    assert "Qt.Key.Key_1 <= key <= Qt.Key.Key_9" in source
    assert "key == Qt.Key.Key_Escape" in source


def test_la_recherche_est_annoncee_partout(documentees):
    """Ctrl+K vaut dans les trois fenêtres : c'est pour ça qu'il est en commun."""
    assert "Ctrl + K" in documentees["Partout"]
    for fichier in ("fullscreen.py", "grid.py"):
        source = (RACINE / "windows" / fichier).read_text(encoding="utf-8")
        assert 'QKeySequence("Ctrl+K")' in source, fichier
    panel = (RACINE / "windows" / "panel.py").read_text(encoding="utf-8")
    assert "Qt.Key.Key_K" in panel


def test_la_page_est_atteignable_depuis_la_barre_laterale():
    """Une page absente de la navigation ne s'ouvre jamais."""
    from windows.settings import _NAV_ITEMS

    assert "Raccourcis" in [nom for nom, _icone in _NAV_ITEMS]


def test_l_ordre_des_pages_suit_celui_de_la_navigation():
    """C'est l'index de la barre latérale qui désigne la page.

    `_switch_page` cherche le rang du libellé dans `_NAV_ITEMS` et l'applique
    tel quel à la pile : une page insérée d'un seul côté décale toutes les
    suivantes, et chaque entrée ouvre alors la page de sa voisine.
    """
    import inspect

    from windows import settings

    source = inspect.getsource(settings.SettingsPanel._build)
    debut = source.index("for page in (")
    ordre = source[debut:source.index(")", debut)]
    attendu = ["_page_streams", "_page_screens", "_page_hype", "_page_clips",
               "_page_deck", "_page_domo", "_page_touches", "_page_credits"]
    assert [p for p in attendu if p in ordre] == attendu
    assert len(attendu) == len(settings._NAV_ITEMS)
