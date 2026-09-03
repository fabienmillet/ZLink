# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Le Stream Deck piloté en direct, sans logiciel Elgato ni matériel.

Aucun test n'ouvre de boîtier. Le vrai matériel n'est pas sur la machine
d'intégration — et quand il est sur celle d'un développeur, une suite de tests
n'a pas à réécrire ses touches ni à les laisser dans l'état où elle s'arrête.
Un faux boîtier retient ce qu'on lui envoie, ce qui suffit : ce module ne
décide de rien, il traduit des appuis en signaux et un état en images.

Ce qui est vérifié tient en trois questions :

- une touche pressée émet-elle LE signal que main.py branche déjà ;
- la disposition posée par le code est-elle bien celle des profils livrés,
  pour qu'un utilisateur passant de Windows à Linux retrouve ses touches ;
- le dessin résiste-t-il à un état incomplet — c'est ce qui arrive à chaque
  démarrage, avant que ZLink ait publié quoi que ce soit.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PIL", reason="Pillow absent : le pilote ne dessine pas")

from core import streamdeck_direct as sd            # noqa: E402


# ── Doublures ────────────────────────────────────────────────────────────────

class FauxDeck:
    """Un boîtier qui garde ce qu'on lui écrit, au lieu de le brancher."""

    def __init__(self, touches: int = 15, molettes: int = 0,
                 cote: int = 72, lignes: int = 3) -> None:
        self._touches = touches
        self._molettes = molettes
        self._cote = cote
        self._lignes = lignes
        self.images: dict[int, bytes] = {}
        self.ecran: bytes | None = None
        self.luminosite: int | None = None
        self.ferme = False
        self.remis_a_zero = 0

    # ce que le pilote interroge
    def deck_type(self) -> str:
        return "Faux Deck"

    def key_count(self) -> int:
        return self._touches

    def key_layout(self) -> tuple:
        return (self._lignes, self._touches // self._lignes)

    def key_image_format(self) -> dict:
        return {"size": (self._cote, self._cote), "format": "JPEG",
                "flip": (False, False), "rotation": 0}

    def dial_count(self) -> int:
        return self._molettes

    def is_touch(self) -> bool:
        return bool(self._molettes)

    def touchscreen_image_format(self) -> dict:
        return {"size": (800, 100), "format": "JPEG",
                "flip": (False, False), "rotation": 0}

    # ce que le pilote lui fait
    def open(self) -> None:
        pass

    def reset(self) -> None:
        self.remis_a_zero += 1

    def close(self) -> None:
        self.ferme = True

    def set_brightness(self, valeur: int) -> None:
        self.luminosite = valeur

    def set_key_callback(self, rappel) -> None:
        self.rappel_touche = rappel

    def set_dial_callback(self, rappel) -> None:
        self.rappel_molette = rappel

    def set_touchscreen_callback(self, rappel) -> None:
        self.rappel_ecran = rappel

    def set_key_image(self, index: int, image) -> None:
        self.images[index] = image

    def set_touchscreen_image(self, image, x=0, y=0, w=0, h=0) -> None:
        self.ecran = image


ETAT = {
    "type": "etat", "actif": "domingo", "volume": 60, "muet": False,
    "chat": True, "favori": False,
    "cellules": [
        {"login": "zerator", "viewers": 45500, "online": True,
         "epingle": True, "avatar": "", "volume": 80, "muet": False},
        {"login": "domingo", "viewers": 30100, "online": True,
         "epingle": True, "avatar": "", "volume": 55, "muet": True},
        {"login": "ponce", "viewers": 0, "online": False,
         "epingle": False, "avatar": "", "volume": 100, "muet": False},
    ],
}


@pytest.fixture
def pilote(qtbot):
    """Un pilote monté sur un faux boîtier, sans fil de dessin.

    `demarrer()` n'est pas appelé : il énumère le matériel réel. On pose le
    boîtier à la main, ce que le fil de dessin ferait de toute façon.
    """
    def monter(touches=15, molettes=0, cote=72, lignes=3):
        objet = sd.PiloteStreamDeck()
        deck = FauxDeck(touches, molettes, cote, lignes)
        objet._boitiers.append(sd.Boitier(deck))
        objet.publier_etat(ETAT)
        return objet, deck
    return monter


# ── Disposition ──────────────────────────────────────────────────────────────

def test_un_boitier_sans_molette_recoit_la_grille():
    """Treize chaînes et deux flèches : le profil « ZLink — Grille »."""
    pose = sd.disposition(15, 0)
    assert [t["famille"] for t in pose] == ["flux"] * 13 + ["navigation"] * 2
    assert [t["rang"] for t in pose[:13]] == list(range(13))
    assert [t["cle"] for t in pose[13:]] == ["page_precedente", "page_suivante"]


def test_un_boitier_a_molettes_recoit_la_regie():
    """Les huit gestes du profil « ZLink — Régie », dans le même ordre."""
    pose = sd.disposition(8, 4)
    assert [(t["famille"], t["cle"]) for t in pose] == list(sd.GESTES_REGIE)


def test_les_deux_dispositions_sont_celles_des_profils_livres():
    """Le code et les profils Elgato doivent montrer la MÊME chose.

    Ils sont écrits à deux endroits — ici et `streamdeck/gen_profils.py` — pour
    deux systèmes qui ne se ressemblent pas. Rien n'empêcherait l'un de dériver
    de l'autre, sinon ce test : un utilisateur qui passe de Windows à Linux ne
    doit pas avoir à réapprendre où sont ses touches.
    """
    profils = pytest.importorskip("streamdeck.gen_profils")
    attendus = [(famille, cle) for _place, famille, cle, _nom
                in profils.GESTES_PLUS]
    assert [(t["famille"], t["cle"]) for t in sd.disposition(8, 4)] == attendus


def test_un_petit_boitier_ne_sacrifie_pas_deux_touches_a_la_pagination():
    """Sur six touches, deux flèches coûteraient plus qu'elles ne donnent."""
    assert all(t["famille"] == "flux" for t in sd.disposition(6, 0))


def test_les_touches_en_trop_restent_eteintes():
    """Un boîtier à molettes plus large que le Stream Deck + n'invente rien."""
    pose = sd.disposition(12, 4)
    assert [t["famille"] for t in pose[8:]] == ["vide"] * 4


# ── Appuis ───────────────────────────────────────────────────────────────────

def test_une_touche_de_flux_demande_sa_chaine(pilote, qtbot):
    objet, deck = pilote()
    with qtbot.waitSignal(objet.chaine_demandee) as attrape:
        objet._sur_touche(deck, 1, True)
    assert attrape.args == ["domingo"]


def test_le_relachement_ne_declenche_rien(pilote):
    """Seul l'appui compte, comme le « keyDown » du plugin Elgato."""
    objet, deck = pilote()
    recu = []
    objet.chaine_demandee.connect(recu.append)
    objet._sur_touche(deck, 1, False)
    assert recu == []


def test_une_touche_d_action_porte_son_geste(pilote, qtbot):
    objet, deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    with qtbot.waitSignal(objet.action_demandee) as attrape:
        objet._sur_touche(deck, 2, True)          # « clip »
    assert attrape.args == ["clip"]


def test_une_touche_hors_disposition_est_ignoree(pilote):
    """Un index impossible ne doit pas tuer le fil de lecture du boîtier."""
    objet, deck = pilote()
    objet._sur_touche(deck, 99, True)             # ne lève pas


def test_une_touche_sans_cellule_ne_demande_rien(pilote):
    """Trois chaînes, treize touches : les dix dernières ne portent rien."""
    objet, deck = pilote()
    recu = []
    objet.chaine_demandee.connect(recu.append)
    objet._sur_touche(deck, 7, True)
    assert recu == []


# ── Pagination ───────────────────────────────────────────────────────────────

def test_la_page_suivante_decale_les_touches(pilote, qtbot):
    """Deux touches de flux, trois chaînes : la page 2 commence à la 3e."""
    objet, deck = pilote(touches=8, lignes=2)     # 6 flux + 2 flèches
    boitier = objet._boitiers[0]
    boitier.page = 0
    objet._naviguer(boitier, "page_suivante")
    assert boitier.page == 0, "trois chaînes sur six touches tiennent en une page"


def test_la_pagination_tourne_en_rond(pilote):
    """Sur une seule page, paginer ne doit pas sortir de la liste."""
    objet, _deck = pilote()
    boitier = objet._boitiers[0]
    objet._naviguer(boitier, "page_precedente")
    assert boitier.page == 0


def test_la_page_change_quand_les_chaines_debordent(pilote):
    objet, _deck = pilote(touches=8, lignes=2)    # 6 touches de flux
    objet.publier_etat({**ETAT, "cellules": ETAT["cellules"] * 4})   # 12 chaînes
    boitier = objet._boitiers[0]
    objet._naviguer(boitier, "page_suivante")
    assert boitier.page == 1


def test_changer_de_page_redessine_tout(pilote):
    """Sans oubli, les touches garderaient l'image de la page précédente."""
    objet, _deck = pilote(touches=8, lignes=2)
    objet.publier_etat({**ETAT, "cellules": ETAT["cellules"] * 4})
    boitier = objet._boitiers[0]
    boitier.a_change(0, ("flux", "zerator"))
    objet._naviguer(boitier, "page_suivante")
    assert boitier.a_change(0, ("flux", "zerator")) is True


# ── Molettes ─────────────────────────────────────────────────────────────────

def test_une_molette_regle_le_plein_ecran(pilote, qtbot):
    """La première molette porte le son principal : cinq points par cran."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    with qtbot.waitSignal(objet.volume_demande) as attrape:
        objet._tourner(0, 2)
    assert attrape.args == [70]                   # 60 + 2 × 5


def test_une_molette_regle_une_chaine_epinglee(pilote, qtbot):
    """Les molettes suivantes suivent l'ordre des chaînes épinglées."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    with qtbot.waitSignal(objet.volume_chaine_demande) as attrape:
        objet._tourner(1, -1)
    assert attrape.args == ["zerator", 75]        # 80 − 5


def test_le_volume_ne_sort_pas_de_ses_bornes(pilote, qtbot):
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    with qtbot.waitSignal(objet.volume_demande) as attrape:
        objet._tourner(0, 40)
    assert attrape.args == [100]


def test_appuyer_sur_une_molette_coupe_sa_piste(pilote, qtbot):
    """Domingo est déjà coupé : l'appui le rend, il ne recoupe pas."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    with qtbot.waitSignal(objet.muet_chaine_demande) as attrape:
        objet._couper(2)
    assert attrape.args == ["domingo", False]


def test_une_molette_sans_chaine_epinglee_reste_inerte(pilote):
    """Deux chaînes épinglées seulement : la quatrième molette ne vise rien."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    recu = []
    objet.volume_demande.connect(recu.append)
    objet.volume_chaine_demande.connect(lambda *a: recu.append(a))
    objet._tourner(3, 1)
    assert recu == []


# ── Dessin ───────────────────────────────────────────────────────────────────

def test_le_dessin_ecrit_toutes_les_touches(pilote):
    objet, deck = pilote()
    objet._peindre_tout()
    assert sorted(deck.images) == list(range(15))


def test_une_touche_inchangee_n_est_pas_reecrite(pilote):
    """Quinze écritures USB à chaque changement d'audience, pour rien."""
    objet, deck = pilote()
    objet._peindre_tout()
    deck.images.clear()
    objet._peindre_tout()
    assert deck.images == {}


def test_une_audience_qui_change_redessine_sa_seule_touche(pilote):
    objet, deck = pilote()
    objet._peindre_tout()
    deck.images.clear()
    cellules = [dict(c) for c in ETAT["cellules"]]
    cellules[0]["viewers"] = 46000
    objet.publier_etat({**ETAT, "cellules": cellules})
    objet._peindre_tout()
    assert list(deck.images) == [0]


def test_un_etat_vide_ne_casse_pas_le_dessin(pilote):
    """C'est l'état du démarrage : ZLink n'a encore rien publié."""
    objet, deck = pilote()
    objet.publier_etat({})
    objet._peindre_tout()
    assert len(deck.images) == 15


def test_l_ecran_des_molettes_est_dessine(pilote):
    objet, deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    objet._peindre_tout()
    assert deck.ecran is not None


def test_la_touche_muet_suit_l_etat_publie(pilote):
    """Une touche qui ne montre que le geste ne dit pas s'il est déjà fait."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    boitier = objet._boitiers[0]
    _image, avant = objet._touche(boitier, {"famille": "action", "cle": "muet"})
    objet.publier_etat({**ETAT, "muet": True})
    _image, apres = objet._touche(boitier, {"famille": "action", "cle": "muet"})
    assert avant != apres


# ── Ajustement du texte ──────────────────────────────────────────────────────

def test_un_nom_trop_long_est_reduit_avant_d_etre_coupe():
    from PIL import Image, ImageDraw

    dessin = ImageDraw.Draw(Image.new("RGB", (72, 72)))
    texte, police = sd._texte_tenant(dessin, "antoinedaniel", 66, 11)
    assert texte == "antoinedaniel", "il tient en rapetissant, rien à couper"
    assert dessin.textlength(texte, font=police) <= 66


def test_un_nom_impossible_finit_par_un_point_de_suite():
    from PIL import Image, ImageDraw

    dessin = ImageDraw.Draw(Image.new("RGB", (72, 72)))
    texte, police = sd._texte_tenant(dessin, "u" * 60, 66, 11)
    assert texte.endswith("…")
    assert dessin.textlength(texte, font=police) <= 66


# ── Cycle de vie ─────────────────────────────────────────────────────────────

def test_l_arret_rend_le_boitier(pilote):
    """Sans reset, les touches gardent leur image après la fermeture de ZLink."""
    objet, deck = pilote()
    objet.arreter()
    assert deck.ferme and deck.remis_a_zero >= 1
    assert objet.boitiers == 0


def test_l_arret_est_idempotent(pilote):
    """atexit l'appelle après un arrêt normal : le second passage ne doit rien faire."""
    objet, _deck = pilote()
    objet.arreter()
    objet.arreter()


def test_sans_bibliotheque_l_enumeration_rend_une_liste_vide(monkeypatch):
    """Un poste sans hidapi n'est pas en panne : il n'a pas ce matériel."""
    import builtins

    vrai_import = builtins.__import__

    def refuser(nom, *args, **kwargs):
        if nom.startswith("StreamDeck"):
            raise ImportError("pas de StreamDeck ici")
        return vrai_import(nom, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuser)
    assert sd.PiloteStreamDeck._enumerer() == []


# ── Molettes sans épingle ────────────────────────────────────────────────────

def test_une_molette_sans_epingle_ne_regle_rien(pilote):
    """Le README l'annonce : « reste inerte plutôt que d'agir sur autre chose ».

    Sans cela, une molette dont l'épingle manque retombait sur le son du plein
    écran — et sur un boîtier sans aucune chaîne épinglée, les quatre molettes
    affichaient « Plein écran » et réglaient le même volume.
    """
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    objet.publier_etat({**ETAT, "cellules": [
        {**c, "epingle": False} for c in ETAT["cellules"]]})
    recu = []
    objet.volume_demande.connect(recu.append)
    objet.volume_chaine_demande.connect(lambda *a: recu.append(a))
    objet._tourner(1, 3)
    objet._couper(2)
    assert recu == []


def test_la_premiere_molette_garde_le_plein_ecran(pilote, qtbot):
    """« principal » n'est pas une épingle absente : elle, elle règle."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    objet.publier_etat({**ETAT, "cellules": [
        {**c, "epingle": False} for c in ETAT["cellules"]]})
    with qtbot.waitSignal(objet.volume_demande):
        objet._tourner(0, 1)


def test_une_molette_inerte_se_voit(pilote):
    """Une case qui affiche « Plein écran » promet un réglage qu'elle ne rend pas."""
    objet, _deck = pilote(touches=8, molettes=4, cote=120, lignes=2)
    objet.publier_etat({**ETAT, "cellules": [
        {**c, "epingle": False} for c in ETAT["cellules"]]})
    assert objet._piste(0)["titre"] == "Plein écran"
    assert objet._piste(1) == {"titre": "—", "volume": 0, "muet": False,
                               "avatar": "", "inerte": True}


# ── Les dessins livrés ───────────────────────────────────────────────────────

def _glyphes_attendus() -> set[str]:
    """Tous les noms de fichier que le pilote peut demander.

    Un nom qui ne correspond à rien ne lève pas : `glyphe()` rend un carré noir.
    La touche sort donc muette sur le boîtier, sans une ligne pour le dire.
    """
    noms = set()
    for famille, cle in sd.GESTES_REGIE:
        noms.add(f"{famille}-{cle}")
        if cle in sd.ETATS_ACTION:
            noms.add(f"{famille}-{cle}-actif")
    for touche in sd.disposition(15, 0):
        if touche["famille"] == "navigation":
            noms.add(f"navigation-{touche['cle']}")
    return noms


def test_chaque_dessin_demande_existe():
    """Sinon la touche sort noire, et rien n'explique pourquoi."""
    dossier = sd.dossier_glyphes()
    manquants = sorted(n for n in _glyphes_attendus()
                       if not (dossier / f"{n}.png").is_file())
    assert not manquants, f"dessins de touche absents : {manquants}"


def test_le_paquet_linux_embarque_les_dessins():
    """Le .spec ne livrait l'extension que sous Windows.

    Sous Linux il n'y a pas de logiciel Elgato, donc pas d'extension à
    installer — mais `core/streamdeck_direct.py` DESSINE les touches avec ces
    mêmes fichiers. Sans eux dans le paquet, les touches d'action du binaire
    publié sortaient noires alors qu'elles marchaient depuis les sources.
    """
    import pathlib

    spec = (pathlib.Path(__file__).resolve().parent.parent
            / "ZLink.spec").read_text(encoding="utf-8")
    assert '_GLYPHES = os.path.join(_EXTENSION, "touches")' in spec
    assert 'sys.platform.startswith("linux") and os.path.isdir(_GLYPHES)' in spec
    assert "donnees.append((_GLYPHES, _GLYPHES))" in spec


def test_l_absence_des_dessins_se_dit_une_fois(monkeypatch, caplog):
    """Dix-sept lignes de debug par redessin ne préviennent personne."""
    import pathlib

    monkeypatch.setattr(sd, "dossier_glyphes",
                        lambda: pathlib.Path("/introuvable/touches"))
    sd.PiloteStreamDeck._glyphes_verifies = False
    with caplog.at_level("WARNING"):
        sd.PiloteStreamDeck._verifier_glyphes()
        sd.PiloteStreamDeck._verifier_glyphes()
    assert sum("dessins de touche introuvables" in m
               for m in caplog.messages) == 1
    sd.PiloteStreamDeck._glyphes_verifies = False
