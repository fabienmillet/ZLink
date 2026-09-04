# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les clips Twitch de la catégorie ZEvent.

Rien n'ici ne touche au réseau : la réponse de `gql.twitch.tv` est fournie par
le test. Ce qui est vérifié, c'est ce que ZLink en fait — le tri, la borne de
sept jours, et le fait qu'une source muette ne casse pas l'onglet.
"""

from __future__ import annotations

import asyncio

import pytest

from core import twitch_clips as tc


def _noeud(slug="abc", titre="Un moment", vues=100, duree=30.0,
           login="ponce", chaine="Ponce", auteur="Quelquun",
           cree="2026-09-03T18:18:00Z"):
    return {"slug": slug, "title": titre, "viewCount": vues,
            "createdAt": cree, "durationSeconds": duree,
            "broadcaster": {"login": login, "displayName": chaine},
            "curator": {"displayName": auteur},
            "thumbnailURL": "https://exemple.test/v.jpg"}


@pytest.fixture
def reponse(monkeypatch):
    """Remplace la requête GraphQL par la charge voulue, et la retient."""
    vues = []

    def poser(charge):
        async def _faux(requete, client=None):
            vues.append(requete)
            return charge
        monkeypatch.setattr(tc, "_demander", _faux)
        return vues
    return poser


# ── la liste ────────────────────────────────────────────────────────────────

def test_les_clips_se_lisent(reponse):
    reponse({"game": {"clips": {"edges": [{"node": _noeud()}]}}})
    clips = asyncio.run(tc.lister())
    assert len(clips) == 1
    c = clips[0]
    assert (c.slug, c.chaine, c.vues, c.auteur) == ("abc", "Ponce", 100, "Quelquun")
    assert c.url == "https://clips.twitch.tv/abc"


def test_la_fenetre_est_de_sept_jours(reponse):
    """Sans elle, la catégorie remonte les clips de l'édition précédente."""
    vues = reponse({"game": {"clips": {"edges": []}}})
    asyncio.run(tc.lister())
    assert "LAST_WEEK" in vues[0]


def test_une_seule_requete_suffit(reponse):
    """Twitch oppose un contrôle d'intégrité aux requêtes paginées.

    On en fait donc UNE, large, plutôt que cinq qui se feraient refuser.
    """
    vues = reponse({"game": {"clips": {"edges": []}}})
    asyncio.run(tc.lister())
    assert len(vues) == 1
    assert f"first: {tc.PAR_REQUETE}" in vues[0]


def test_un_clip_sans_slug_est_ecarte(reponse):
    """Sans lui, ni lecture ni lien : la ligne ne mènerait nulle part."""
    reponse({"game": {"clips": {"edges": [
        {"node": _noeud(slug="")}, {"node": _noeud(slug="ok")}]}}})
    assert [c.slug for c in asyncio.run(tc.lister())] == ["ok"]


def test_une_date_illisible_vaut_zero(reponse):
    """Elle ne fait donc jamais partie de l'événement : mieux vaut un clip de
    moins qu'un clip d'une autre semaine au milieu de ceux de la nuit."""
    assert tc._lire(_noeud(cree="jamais")).cree_le == 0.0


def test_une_source_muette_rend_une_liste_vide(reponse):
    """Un clip manquant n'empêche pas de suivre l'événement."""
    reponse({})
    assert asyncio.run(tc.lister()) == []


# ── les tris ────────────────────────────────────────────────────────────────

def _clips():
    return [
        tc.Clip("a", "A", vues=10, cree_le=300.0, duree_s=60.0,
                login="zerator", chaine="ZeratoR", auteur="", vignette=""),
        tc.Clip("b", "B", vues=90, cree_le=100.0, duree_s=20.0,
                login="ponce", chaine="Ponce", auteur="", vignette=""),
        tc.Clip("c", "C", vues=50, cree_le=200.0, duree_s=40.0,
                login="ponce", chaine="Ponce", auteur="", vignette=""),
    ]


@pytest.mark.parametrize("cle,attendu", [
    ("vues", ["b", "c", "a"]),
    ("recents", ["a", "c", "b"]),
    ("duree", ["a", "c", "b"]),
    # La chaîne d'abord, puis les vues : un tri par nom qui mélangerait les
    # clips d'un même streamer n'aiderait pas à les parcourir.
    ("chaine", ["b", "c", "a"]),
])
def test_les_tris(cle, attendu):
    assert [c.slug for c in tc.trier(_clips(), cle)] == attendu


def test_un_tri_inconnu_retombe_sur_les_vues():
    assert [c.slug for c in tc.trier(_clips(), "inventé")] == ["b", "c", "a"]


def test_les_tris_proposes_sont_tous_traites():
    """Une entrée du menu sans effet se remarque après coup, en direct."""
    reference = [c.slug for c in tc.trier(_clips(), "vues")]
    autres = [cle for cle in tc.TRIS if cle != "vues"]
    assert autres, "il faut plus d'un tri pour que le menu ait un sens"
    assert any([c.slug for c in tc.trier(_clips(), cle)] != reference
               for cle in autres)


# ── la lecture ──────────────────────────────────────────────────────────────

def test_l_url_de_lecture_porte_le_jeton(reponse):
    """Sans le jeton signé, le CDN répond 403 : l'adresse seule ne suffit pas."""
    reponse({"clip": {
        "videoQualities": [{"quality": "1080", "sourceURL": "https://cdn/x.mp4"}],
        "playbackAccessToken": {"signature": "sig1", "value": "tok1"}}})
    url = asyncio.run(tc.url_de_lecture("abc"))
    assert url.startswith("https://cdn/x.mp4?")
    assert "sig=sig1" in url and "token=tok1" in url


def test_la_meilleure_piste_est_prise(reponse):
    """Twitch les rend par qualité décroissante."""
    reponse({"clip": {
        "videoQualities": [{"quality": "1080", "sourceURL": "https://cdn/haut"},
                           {"quality": "360", "sourceURL": "https://cdn/bas"}],
        "playbackAccessToken": {"signature": "s", "value": "t"}}})
    assert asyncio.run(tc.url_de_lecture("abc")).startswith("https://cdn/haut?")


@pytest.mark.parametrize("charge", [
    {},
    {"clip": {"videoQualities": [], "playbackAccessToken": {"signature": "s"}}},
    {"clip": {"videoQualities": [{"sourceURL": "u"}],
              "playbackAccessToken": {}}},
])
def test_une_lecture_impossible_rend_une_adresse_vide(reponse, charge):
    """L'onglet propose alors d'ouvrir sur Twitch, plutôt que de rester muet."""
    reponse(charge)
    assert asyncio.run(tc.url_de_lecture("abc")) == ""


# ── par chaîne : ce que la catégorie ne voit pas ────────────────────────────

def test_les_chaines_sont_interrogees_par_lots(reponse):
    """Trois cents chaînes en douze requêtes, pas trois cents.

    C'est le réglage que `core/live_uptime.py` applique déjà : GraphQL accepte
    autant d'alias qu'on veut dans un même document.
    """
    vues = reponse({})
    logins = [f"c{i}" for i in range(60)]
    asyncio.run(tc.lister_par_chaines(logins))
    assert len(vues) == 3                      # 60 / 25, arrondi au supérieur
    assert vues[0].count("user(login:") == tc.MAX_PAR_LOT


def test_les_clips_de_toutes_les_chaines_reviennent(reponse):
    reponse({"u0": {"clips": {"edges": [{"node": _noeud(slug="a")}]}},
             "u1": {"clips": {"edges": [{"node": _noeud(slug="b")}]}}})
    clips = asyncio.run(tc.lister_par_chaines(["x", "y"]))
    assert {c.slug for c in clips} == {"a", "b"}


def test_un_meme_clip_n_apparait_qu_une_fois(reponse):
    """Il peut remonter par sa chaîne ET par la catégorie : deux cartes pour
    un même moment se remarquent tout de suite."""
    reponse({"u0": {"clips": {"edges": [{"node": _noeud(slug="a")}]}},
             "u1": {"clips": {"edges": [{"node": _noeud(slug="a")}]}}})
    assert len(asyncio.run(tc.lister_par_chaines(["x", "y"]))) == 1


def test_les_plus_vus_arrivent_en_tete(reponse):
    reponse({"u0": {"clips": {"edges": [
        {"node": _noeud(slug="petit", vues=5)},
        {"node": _noeud(slug="gros", vues=900)}]}}})
    assert [c.slug for c in asyncio.run(tc.lister_par_chaines(["x"]))] == [
        "gros", "petit"]


def test_une_chaine_sans_compte_ne_casse_pas_le_lot(reponse):
    """Un login disparu rend `null` : le lot entier ne doit pas s'effondrer."""
    reponse({"u0": None, "u1": {"clips": {"edges": [{"node": _noeud("b")}]}}})
    assert [c.slug for c in asyncio.run(tc.lister_par_chaines(["x", "y"]))] == ["b"]


def test_sans_chaine_aucune_requete_ne_part(reponse):
    vues = reponse({})
    assert asyncio.run(tc.lister_par_chaines([])) == []
    assert vues == []


def test_la_fenetre_de_sept_jours_vaut_aussi_par_chaine(reponse):
    vues = reponse({})
    asyncio.run(tc.lister_par_chaines(["x"]))
    assert "LAST_WEEK" in vues[0]


# ── ce qui n'a rien à voir avec l'événement ─────────────────────────────────

@pytest.fixture
def ouverture(monkeypatch):
    """Fixe l'instant d'ouverture, sans dépendre du calendrier réel."""
    monkeypatch.setattr(tc, "depuis_quand", lambda: 1000.0)
    return 1000.0


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def test_un_clip_anterieur_a_l_evenement_est_ecarte(reponse, ouverture):
    """Interroger les chaînes rattrape aussi leurs streams ordinaires.

    Mesuré sur quatre chaînes du plateau : cent cinquante-six clips
    précédaient l'ouverture contre un seul après — du VALORANT, du PUBG, rien
    du ZEvent. Sept jours ne suffisent pas à trancher, et la catégorie du clip
    non plus : pendant l'événement les participants jouent à tout.
    """
    reponse({"u0": {"clips": {"edges": [
        {"node": _noeud(slug="avant", cree=_iso(ouverture - 3600))},
        {"node": _noeud(slug="apres", cree=_iso(ouverture + 3600))}]}}})
    clips = asyncio.run(tc.lister_par_chaines(["x"]))
    assert [c.slug for c in clips] == ["apres"]


def test_l_instant_d_ouverture_est_retenu(reponse, ouverture):
    """La borne est inclusive : un clip pris à l'ouverture en fait partie."""
    reponse({"u0": {"clips": {"edges": [
        {"node": _noeud(slug="pile", cree=_iso(ouverture))}]}}})
    assert [c.slug for c in asyncio.run(tc.lister_par_chaines(["x"]))] == ["pile"]


def test_la_categorie_est_filtree_de_la_meme_facon(reponse, ouverture):
    """Les deux chemins doivent retenir la même chose, sans quoi rafraîchir
    changerait la liste selon l'ordre d'arrivée des participants."""
    reponse({"game": {"clips": {"edges": [
        {"node": _noeud(slug="avant", cree=_iso(ouverture - 60))},
        {"node": _noeud(slug="apres", cree=_iso(ouverture + 60))}]}}})
    assert [c.slug for c in asyncio.run(tc.lister())] == ["apres"]


def test_une_date_illisible_ne_passe_pas_le_filtre(reponse, ouverture):
    """Elle vaut zéro, donc antérieure à tout : mieux vaut un clip de moins
    qu'un clip d'une autre semaine au milieu de ceux de la nuit."""
    reponse({"u0": {"clips": {"edges": [
        {"node": _noeud(slug="flou", cree="jamais")}]}}})
    assert asyncio.run(tc.lister_par_chaines(["x"])) == []


def test_l_ouverture_vient_de_l_historique():
    """Une seule date fait autorité, celle qui ouvre déjà les courbes."""
    from core.history_store import OUVERTURE_CAGNOTTE

    assert tc.depuis_quand() == OUVERTURE_CAGNOTTE


def test_le_lot_rend_ses_clips_et_son_compte_d_ecartes(ouverture):
    """La boucle intérieure est sortie de `lister_par_chaines` : trois boucles
    imbriquées et deux conditions dans une fonction qui gère aussi son client
    HTTP, on ne voyait plus laquelle faisait quoi."""
    donnees = {
        "u0": {"clips": {"edges": [
            {"node": _noeud(slug="garde", cree=_iso(ouverture + 60))},
            {"node": _noeud(slug="vieux", cree=_iso(ouverture - 60))}]}},
        "u1": None,
        "u2": {"clips": {"edges": [{"node": _noeud(slug="autre",
                                                   cree=_iso(ouverture + 1))}]}},
    }
    retenus, ecartes = tc._clips_du_lot(donnees, 3)
    assert [c.slug for c in retenus] == ["garde", "autre"]
    assert ecartes == 1


def test_un_lot_vide_ne_rend_rien():
    assert tc._clips_du_lot({}, 3) == ([], 0)
