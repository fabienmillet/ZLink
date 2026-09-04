# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Mégaphone du ZEvent : le canal audio du plateau, allumé ou éteint.

Aucun flux n'est ouvert : `mpv.MPV` est remplacé par un double qui note ce
qu'on lui demande et rend les mesures qu'on lui dicte. Ce qui est vérifié, ce
sont les décisions — allumer, éteindre, dire si ça parle — pas le décodage.
"""

from __future__ import annotations

import pytest

from widgets import megaphone as M
from widgets.megaphone import Megaphone


class _FauxLecteur:
    """Double de `mpv.MPV`, réduit à ce que le mégaphone lui demande."""

    def __init__(self, **options) -> None:
        self.options = options
        self.lu: list[str] = []
        self.volume = options.get("volume")
        self.termine = False
        #: Ce que le filtre astats est censé publier au prochain relevé.
        self.rms: str | None = None

    def play(self, url: str) -> None:
        self.lu.append(url)

    def terminate(self) -> None:
        self.termine = True

    def _get_property(self, nom: str):
        assert nom == "af-metadata/zl", nom
        return {} if self.rms is None else {
            "lavfi.astats.Overall.RMS_level": self.rms}


@pytest.fixture
def mpv_factice(monkeypatch):
    """Rend la fabrique : `mpv_factice.dernier` est le lecteur en cours."""
    class _Fabrique:
        dernier: _FauxLecteur | None = None

        def MPV(self, **options):                       # noqa: N802 — API mpv
            self.dernier = _FauxLecteur(**options)
            return self.dernier

    fabrique = _Fabrique()
    monkeypatch.setattr(M, "_MPV_AVAILABLE", True)
    monkeypatch.setattr(M, "_mpv_module", fabrique)
    monkeypatch.setattr(M.MpvWidget, "_garantir_locale_c", staticmethod(lambda: None))
    return fabrique


@pytest.fixture
def mega(qtbot, mpv_factice):
    return Megaphone()


# ── allumage et extinction ───────────────────────────────────────────────────

def test_allumer_ouvre_le_flux(mega, mpv_factice):
    assert mega.demarrer() is True
    assert mega.actif is True
    assert mpv_factice.dernier.lu == [M.URL]


def test_le_lecteur_n_a_ni_image_ni_interface(mega, mpv_factice):
    """Un lecteur de fond mal réglé surgit à l'écran : le flux n'a pas d'image
    de toute façon, et rien ici n'a d'interface à offrir."""
    mega.demarrer()
    options = mpv_factice.dernier.options
    assert options["video"] is False and options["vo"] == "null"
    assert options["osc"] is False and options["load_scripts"] is False


def test_eteindre_detruit_le_lecteur(mega, mpv_factice):
    """Une pause laisserait un lecteur, ses fils et sa connexion ouverts toute
    la soirée pour un son que personne n'écoute."""
    mega.demarrer()
    lecteur = mpv_factice.dernier
    mega.arreter()
    assert lecteur.termine is True
    assert mega.actif is False


def test_rallumer_ne_double_pas_le_lecteur(mega, mpv_factice):
    mega.demarrer()
    premier = mpv_factice.dernier
    assert mega.demarrer() is True
    assert mpv_factice.dernier is premier


def test_eteindre_deux_fois_ne_leve_pas(mega):
    mega.demarrer()
    mega.arreter()
    mega.arreter()


def test_un_lecteur_recalcitrant_a_l_arret_est_abandonne(mega, mpv_factice):
    """On ne le garde pas : l'échec de `terminate` ne doit pas laisser le
    mégaphone se croire allumé."""
    mega.demarrer()
    def _refuse():
        raise RuntimeError("déjà mort")
    mpv_factice.dernier.terminate = _refuse
    mega.arreter()
    assert mega.actif is False


# ── indisponibilité ──────────────────────────────────────────────────────────

def test_sans_libmpv_le_megaphone_dit_pourquoi(qtbot, monkeypatch):
    monkeypatch.setattr(M, "_MPV_AVAILABLE", False)
    mega = Megaphone()
    raisons: list[str] = []
    mega.echec.connect(raisons.append)
    assert mega.disponible is False
    assert mega.demarrer() is False
    assert raisons and "libmpv" in raisons[0]


def test_un_flux_qui_refuse_de_s_ouvrir_rend_la_main(qtbot, monkeypatch):
    """C'est ce qui permet au bouton de se relever au lieu de rester enfoncé
    sur un silence."""
    class _Cassee:
        def MPV(self, **_kw):                           # noqa: N802 — API mpv
            raise OSError("réseau injoignable")

    monkeypatch.setattr(M, "_MPV_AVAILABLE", True)
    monkeypatch.setattr(M, "_mpv_module", _Cassee())
    monkeypatch.setattr(M.MpvWidget, "_garantir_locale_c", staticmethod(lambda: None))
    mega = Megaphone()
    raisons: list[str] = []
    mega.echec.connect(raisons.append)
    assert mega.basculer(True) is False
    assert mega.actif is False
    assert raisons


def test_basculer_rend_l_etat_reellement_obtenu(mega):
    assert mega.basculer(True) is True
    assert mega.basculer(False) is False


# ── détection de la parole ───────────────────────────────────────────────────

def test_un_niveau_franc_annonce_la_parole(mega, mpv_factice):
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mega.demarrer()
    mpv_factice.dernier.rms = "-20.0"
    mega._relever_le_niveau()
    assert parles == [True]


def test_le_silence_parfait_n_est_pas_de_la_parole(mega, mpv_factice):
    """ffmpeg rend « -inf » sur un canal muet, ce qui n'est pas un flottant."""
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mega.demarrer()
    mpv_factice.dernier.rms = "-inf"
    mega._relever_le_niveau()
    assert parles == []
    assert mega._niveau_db() is None


@pytest.mark.parametrize("rms", [None, "", "bruit", "-90.0"])
def test_rien_d_audible_ne_declenche_rien(mega, mpv_factice, rms):
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mega.demarrer()
    mpv_factice.dernier.rms = rms
    mega._relever_le_niveau()
    assert parles == []


def test_la_parole_est_maintenue_entre_deux_mots(mega, mpv_factice, monkeypatch):
    """Une phrase est pleine de silences : sans maintien, l'étiquette
    clignoterait entre chaque mot."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr(M.time, "monotonic", lambda: horloge["t"])
    parles: list[bool] = []
    mega.demarrer()
    mega.parole.connect(parles.append)

    mpv_factice.dernier.rms = "-20.0"
    mega._relever_le_niveau()               # ça parle
    mpv_factice.dernier.rms = "-inf"
    horloge["t"] += M._MAINTIEN_S / 2
    mega._relever_le_niveau()               # court silence : on tient
    assert parles == [True]

    horloge["t"] += M._MAINTIEN_S
    mega._relever_le_niveau()               # silence installé : on lâche
    assert parles == [True, False]


def test_la_parole_reprend_sans_delai(mega, mpv_factice, monkeypatch):
    """Le maintien ne joue QUE dans le sens de l'extinction."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr(M.time, "monotonic", lambda: horloge["t"])
    mega.demarrer()
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mpv_factice.dernier.rms = "-20.0"
    mega._relever_le_niveau()
    assert parles == [True]


def test_l_etat_n_est_emis_qu_au_changement(mega, mpv_factice):
    """Le relevé passe trois fois par seconde : réémettre à chaque fois
    repeindrait l'en-tête pour rien."""
    mega.demarrer()
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mpv_factice.dernier.rms = "-20.0"
    for _ in range(5):
        mega._relever_le_niveau()
    assert parles == [True]


def test_eteindre_retombe_au_silence(mega, mpv_factice):
    """Sinon l'étiquette resterait sur « annonce » après extinction."""
    mega.demarrer()
    mpv_factice.dernier.rms = "-20.0"
    mega._relever_le_niveau()
    parles: list[bool] = []
    mega.parole.connect(parles.append)
    mega.arreter()
    assert parles == [False]


def test_la_sonde_s_arrete_avec_le_flux(mega):
    """Un timer qui tourne pour rien réveille l'application toute la soirée."""
    mega.demarrer()
    assert mega._sonde.isActive() is True
    mega.arreter()
    assert mega._sonde.isActive() is False


# ── volume ───────────────────────────────────────────────────────────────────

def test_le_volume_est_retenu_pour_la_prochaine_ecoute(mega, mpv_factice):
    mega.set_volume(40)
    mega.demarrer()
    assert mpv_factice.dernier.options["volume"] == 40


@pytest.mark.parametrize("demande,attendu", [(-5, 0), (0, 0), (150, 100)])
def test_le_volume_est_borne(mega, mpv_factice, demande, attendu):
    mega.demarrer()
    mega.set_volume(demande)
    assert mpv_factice.dernier.volume == attendu
