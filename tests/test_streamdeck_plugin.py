# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""L'extension Stream Deck tient-elle debout, fichiers en main.

Un plugin Elgato échoue en silence : icône manquante, panneau de réglages
introuvable, et le logiciel se contente de ne pas l'afficher — sans message.
Ces tests remplacent le message.

Le plugin n'est pas importé : il dépend de `websockets`, installé dans son
propre environnement (`streamdeck/requirements.txt`), pas dans celui de
l'application. On lit donc sa source, ce qui suffit à vérifier ce qui compte —
que les deux moitiés parlent bien le même vocabulaire.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

RACINE = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = RACINE / "streamdeck" / "com.zlink.deck.sdPlugin"
MANIFESTE = PLUGIN / "manifest.json"

pytestmark = pytest.mark.skipif(
    not MANIFESTE.exists(),
    reason="extension Stream Deck absente de cette copie du dépôt")


@pytest.fixture(scope="module")
def manifeste() -> dict:
    return json.loads(MANIFESTE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source_plugin() -> str:
    return (PLUGIN / "zlink_deck.py").read_text(encoding="utf-8")


def _constante(source: str, nom: str):
    """Valeur d'une constante littérale du plugin, sans l'importer."""
    for noeud in ast.parse(source).body:
        cibles = getattr(noeud, "targets", [])
        if cibles and getattr(cibles[0], "id", "") == nom:
            return ast.literal_eval(noeud.value)
    raise AssertionError(f"constante {nom} introuvable")


# ── ce que le logiciel Elgato exige ──────────────────────────────────────────

def test_toutes_les_icones_annoncees_existent(manifeste):
    """Une icône manquante, et le plugin n'apparaît pas dans la liste."""
    annoncees = [manifeste["Icon"], manifeste["CategoryIcon"]]
    for action in manifeste["Actions"]:
        annoncees.append(action["Icon"])
        annoncees += [etat["Image"] for etat in action.get("States", [])
                      if "Image" in etat]

    manquantes = []
    for nom in sorted(set(annoncees)):
        # Le manifeste nomme les images SANS extension. Les vignettes de la
        # liste existent en deux définitions, le logiciel choisit selon
        # l'écran ; une image de touche est déjà à la définition maximale et
        # n'a pas de @2x.
        suffixes = ("",) if nom.startswith("touches/") else ("", "@2x")
        for suffixe in suffixes:
            chemin = PLUGIN / f"{nom}{suffixe}.png"
            if not chemin.exists():
                manquantes.append(str(chemin.relative_to(PLUGIN)))
    assert not manquantes, "icônes annoncées mais absentes : " + ", ".join(manquantes)


def test_les_touches_sont_a_la_definition_d_une_touche(manifeste):
    """Une image de 40 px étirée sur une touche de 144 se voit tout de suite."""
    from struct import unpack

    trop_petites = []
    for image in sorted((PLUGIN / "touches").glob("*.png")):
        # En-tête PNG : la largeur tient sur quatre octets, à l'offset 16.
        largeur = unpack(">I", image.read_bytes()[16:20])[0]
        if largeur < 144:
            trop_petites.append(f"{image.name} ({largeur} px)")
    assert not trop_petites, "images de touche sous-dimensionnées : " +         ", ".join(trop_petites)


def test_chaque_geste_a_son_glyphe(source_plugin):
    """Six touches « Action » toutes frappées du même éclair ne se lisent pas."""
    for famille, constante in (("action", "LIBELLES_ACTION"),
                               ("navigation", "LIBELLES_NAVIGATION")):
        for cle in _constante(source_plugin, constante):
            chemin = PLUGIN / "touches" / f"{famille}-{cle}.png"
            assert chemin.exists(), f"glyphe manquant : {chemin.name}"


def test_tous_les_panneaux_de_reglages_existent(manifeste):
    for action in manifeste["Actions"]:
        chemin = PLUGIN / action["PropertyInspectorPath"]
        assert chemin.exists(), f"{action['UUID']} : {chemin.name} manquant"


def test_les_panneaux_ne_chargent_rien_depuis_le_reseau():
    """Un Stream Deck hors ligne doit afficher ses réglages quand même.

    Les composants officiels viennent d'un CDN ; un panneau qui en dépend
    s'ouvre vide dès que la machine n'a pas Internet.
    """
    fautifs = []
    for page in sorted((PLUGIN / "pi").glob("*.html")):
        texte = page.read_text(encoding="utf-8")
        if any(marque in texte for marque in ("http://", "https://", 'src="//')):
            fautifs.append(page.name)
    assert not fautifs, "ressources distantes dans : " + ", ".join(fautifs)


def test_l_executable_annonce_est_celui_que_la_construction_produit(manifeste):
    construire = (RACINE / "streamdeck" / "construire.py").read_text(
        encoding="utf-8")
    assert manifeste["CodePathWin"] == _constante(construire, "NOM_EXE") + ".exe"


# ── ce que les deux moitiés doivent avoir en commun ──────────────────────────

def test_les_gestes_proposes_sont_ceux_que_zlink_execute(source_plugin):
    """Un geste offert par le panneau que ZLink ignore ne fait rien.

    L'échec serait muet des deux côtés : la touche s'allume, l'appui part, et
    `run_action` laisse tomber une clé inconnue sans bruit — c'est voulu, une
    télécommande peut être plus récente que l'application.
    """
    from windows.fullscreen import FullscreenWindow

    page = (PLUGIN / "pi" / "action.html").read_text(encoding="utf-8")
    proposes = set(_valeurs_du_select(page))
    connus = set(FullscreenWindow.ACTIONS)
    assert proposes <= connus, (
        "gestes proposés que ZLink ne connaît pas : " + ", ".join(proposes - connus))
    assert set(_constante(source_plugin, "LIBELLES_ACTION")) >= proposes, (
        "gestes proposés sans libellé sur la touche")


def test_chaque_geste_basculable_a_ses_deux_dessins(source_plugin):
    """Sans variante engagée, la touche ne dit que ce qu'elle FERAIT.

    Un haut-parleur barré en permanence n'apprend pas si le son est coupé :
    on appuie pour voir, et on découvre en coupant.
    """
    for cle in _constante(source_plugin, "ETATS_ACTION"):
        for suffixe in ("", "-actif"):
            chemin = PLUGIN / "touches" / f"action-{cle}{suffixe}.png"
            assert chemin.exists(), f"état manquant : {chemin.name}"


def test_les_etats_basculables_sont_publies_par_zlink(source_plugin):
    """Le plugin lit ces clés dans l'état ; ZLink doit les écrire."""
    import inspect

    import main

    source_zlink = inspect.getsource(main._etat_pour_telecommande)
    for etat in _constante(source_plugin, "ETATS_ACTION").values():
        assert f'"{etat}"' in source_zlink, (
            f"« {etat} » attendu par la touche mais absent de l'état publié")


def test_les_sens_de_navigation_ont_tous_un_libelle(source_plugin):
    page = (PLUGIN / "pi" / "navigation.html").read_text(encoding="utf-8")
    proposes = set(_valeurs_du_select(page))
    assert set(_constante(source_plugin, "LIBELLES_NAVIGATION")) == proposes


def test_le_port_par_defaut_est_le_meme_des_deux_cotes(source_plugin):
    from core import remote_api

    assert _constante(source_plugin, "ZLINK_PORT") == remote_api.PORT_DEFAUT


def test_le_plugin_cherche_le_rendez_vous_ecrit_par_zlink(source_plugin):
    """Le seul chemin qui marche quand le plugin est installé chez Elgato."""
    from core import remote_api

    assert remote_api.NOM_RENDEZ_VOUS in source_plugin


def _valeurs_du_select(html: str) -> list[str]:
    import re

    return re.findall(r'<option value="([^"]+)"', html)


# ── Le cache des images ne doit pas figer ce qui change ─────────────────────

def test_la_pastille_se_cache_par_image_pas_par_molette(source_plugin):
    """La molette principale porte un login VIDE, quelle que soit la chaîne.

    Indexé sur le login, le cache rendait la première photo chargée pour
    toutes les suivantes : l'icône du mixer ne changeait plus jamais de flux.
    """
    debut = source_plugin.index("async def pastille")
    corps = source_plugin[debut:source_plugin.index("def ", debut + 10)]
    assert 'f"pastille|{url}|{muet}"' in corps, (
        "le cache de la pastille doit être indexé sur l'URL de l'avatar")
