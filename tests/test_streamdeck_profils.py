# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les profils tout faits sont-ils importables.

Un profil mal formé n'est pas rejeté avec un message : le logiciel Stream Deck
l'ignore, ou l'importe vide. Comme le format vient d'un RELEVÉ de ce que le
logiciel écrit — il n'est documenté nulle part — ces tests fixent ce relevé, et
signaleront le jour où une modification s'en écarte.

Le point le plus important est le dernier : aucun numéro de série ne doit
partir dans un fichier versionné.
"""

from __future__ import annotations

import json
import pathlib
import zipfile

import pytest

RACINE = pathlib.Path(__file__).resolve().parent.parent
PROFILS = RACINE / "streamdeck" / "com.zlink.deck.sdPlugin" / "profils"

pytestmark = pytest.mark.skipif(
    not PROFILS.is_dir(),
    reason="profils Stream Deck absents de cette copie du dépôt")


def _archives() -> list[pathlib.Path]:
    return sorted(PROFILS.glob("*.streamDeckProfile"))


def _lire(archive: pathlib.Path) -> tuple[dict, dict]:
    """Le manifeste racine et celui de l'unique page."""
    with zipfile.ZipFile(archive) as z:
        noms = z.namelist()
        racine = [n for n in noms if n.count("/") == 1 and n.endswith("manifest.json")]
        page = [n for n in noms if n.count("/") == 3 and n.endswith("manifest.json")]
        assert len(racine) == 1, f"{archive.name} : manifeste racine introuvable"
        assert len(page) == 1, f"{archive.name} : page unique attendue"
        return (json.loads(z.read(racine[0])), json.loads(z.read(page[0])))


@pytest.fixture(params=[f.name for f in _archives()])
def profil(request):
    archive = PROFILS / request.param
    racine, page = _lire(archive)
    return {"nom": request.param, "racine": racine, "page": page}


# ── structure ────────────────────────────────────────────────────────────────

def test_les_deux_profils_sont_livres():
    noms = {f.name for f in _archives()}
    assert noms == {"ZLink Grille.streamDeckProfile",
                    "ZLink Regie.streamDeckProfile"}


def test_le_dossier_de_page_correspond_a_la_page_declaree():
    """Le manifeste liste les pages en minuscules, le dossier est en capitales.

    C'est ce que fait le logiciel ; s'en écarter donne un profil dont la page
    n'est pas retrouvée, et qui s'importe vide.
    """
    for archive in _archives():
        with zipfile.ZipFile(archive) as z:
            dossiers = {n.split("/")[2] for n in z.namelist() if n.count("/") == 3}
        racine, _page = _lire(archive)
        declarees = {p.upper() for p in racine["Pages"]["Pages"]}
        assert dossiers == declarees, archive.name


def test_le_manifeste_porte_ce_que_le_logiciel_attend(profil):
    racine = profil["racine"]
    assert racine["Version"] == "3.0"
    assert racine["Name"].startswith("ZLink")
    assert racine["Device"]["Model"]
    assert racine["Pages"]["Current"] == racine["Pages"]["Default"]


def test_aucun_numero_de_serie_n_est_publie(profil):
    """Le manifeste d'un profil existant porte le serial de son appareil.

    Le recopier dans un fichier versionné le rendrait public, et lierait le
    profil à une seule machine.
    """
    assert profil["racine"]["Device"]["UUID"] == ""


# ── contenu ──────────────────────────────────────────────────────────────────

def test_toutes_les_actions_sont_celles_du_plugin(profil):
    manifeste = json.loads(
        (PROFILS.parent / "manifest.json").read_text(encoding="utf-8"))
    connues = {a["UUID"] for a in manifeste["Actions"]}
    for controleur in profil["page"]["Controllers"]:
        for place, action in (controleur["Actions"] or {}).items():
            assert action["UUID"] in connues, f"{place} : {action['UUID']} inconnu"


def test_chaque_action_porte_un_reglage_utilisable(profil):
    """Une action sans réglage retomberait sur le défaut du plugin.

    Ce serait treize touches montrant toutes la première cellule.
    """
    attendus = {
        "com.zlink.deck.flux": "rang",
        "com.zlink.deck.action": "cle",
        "com.zlink.deck.navigation": "sens",
        "com.zlink.deck.mixage": "cible",
    }
    for controleur in profil["page"]["Controllers"]:
        for place, action in (controleur["Actions"] or {}).items():
            champ = attendus[action["UUID"]]
            assert champ in action["Settings"], f"{place} : {champ} manquant"


def test_la_grille_couvre_treize_rangs_distincts():
    """Deux touches sur le même rang, et une chaîne devient inatteignable."""
    _racine, page = _lire(PROFILS / "ZLink Grille.streamDeckProfile")
    actions = page["Controllers"][0]["Actions"]
    rangs = [a["Settings"]["rang"] for a in actions.values()
             if a["UUID"].endswith(".flux")]
    assert sorted(rangs) == list(range(13))


def test_la_grille_offre_les_deux_sens_de_pagination():
    _racine, page = _lire(PROFILS / "ZLink Grille.streamDeckProfile")
    actions = page["Controllers"][0]["Actions"]
    sens = {a["Settings"]["sens"] for a in actions.values()
            if a["UUID"].endswith(".navigation")}
    assert sens == {"page_precedente", "page_suivante"}


def test_la_regie_a_ses_quatre_molettes_sur_des_pistes_distinctes():
    _racine, page = _lire(PROFILS / "ZLink Regie.streamDeckProfile")
    molettes = [c for c in page["Controllers"] if c["Type"] == "Encoder"][0]
    cibles = [a["Settings"]["cible"] for a in molettes["Actions"].values()]
    assert cibles[0] == "principal", "la première molette règle le plein écran"
    assert len(set(cibles)) == 4, "deux molettes sur la même piste"


def test_un_stream_deck_sans_molette_n_annonce_pas_de_molette():
    """Le logiciel n'écrit un contrôleur `Encoder` que pour un appareil qui en a."""
    _racine, page = _lire(PROFILS / "ZLink Grille.streamDeckProfile")
    assert [c["Type"] for c in page["Controllers"]] == ["Keypad"]


def test_les_gestes_de_la_regie_sont_ceux_que_zlink_execute():
    from windows.fullscreen import FullscreenWindow

    _racine, page = _lire(PROFILS / "ZLink Regie.streamDeckProfile")
    touches = [c for c in page["Controllers"] if c["Type"] == "Keypad"][0]
    gestes = {a["Settings"]["cle"] for a in touches["Actions"].values()
              if a["UUID"].endswith(".action")}
    assert gestes <= set(FullscreenWindow.ACTIONS)


def test_les_profils_portent_la_version_du_plugin(profil):
    """Monter la version sans rejouer `construire.py` laisse les profils derrière.

    La divergence est muette : les profils s'importent quand même, en
    annonçant une version d'extension qui n'existe plus. Ce test est le
    rattrapage de la règle écrite dans CLAUDE.md.
    """
    manifeste = json.loads(
        (PROFILS.parent / "manifest.json").read_text(encoding="utf-8"))
    for controleur in profil["page"]["Controllers"]:
        for place, action in (controleur["Actions"] or {}).items():
            assert action["Plugin"]["Version"] == manifeste["Version"], (
                f"{place} : profil figé sur {action['Plugin']['Version']}, "
                f"manifeste en {manifeste['Version']} — "
                "rejouer streamdeck/construire.py")
