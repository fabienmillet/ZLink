# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Sons d'alerte : coupés par défaut, volume borné, jamais bruyants par accident.

Un son qui part alors qu'on l'a coupé est le défaut le plus pénible du module,
parce qu'il surprend en direct. La règle est donc double : `play` ne doit rien
faire tant que `configure` n'a pas explicitement activé les sons, et un volume
aberrant dans le fichier de configuration ne doit jamais donner un son à fond.

AUCUN son n'est réellement joué ici : `QSoundEffect` est remplacé par un double
qui note ce qu'on lui demande. Un test qui ferait du bruit sur la machine de
développement serait vite désactivé.
"""

from __future__ import annotations

import pytest

from core import sounds


class _FauxEffet:
    """Double de QSoundEffect : retient le volume et compte les lectures."""

    def __init__(self):
        self.source = None
        self.volume = None
        self.lectures = 0

    def setSource(self, url):        # noqa: N802 — signature de Qt
        self.source = url

    def setVolume(self, v):          # noqa: N802 — signature de Qt
        self.volume = v

    def play(self):
        self.lectures += 1


@pytest.fixture(autouse=True)
def module_neuf(monkeypatch):
    """Remet le module dans son état d'import et neutralise l'audio.

    `_effets`, `_actif` et `_volume` sont des variables de module : sans cette
    remise à zéro, l'ordre des tests changerait leur résultat.
    """
    monkeypatch.setattr(sounds, "_effets", {})
    monkeypatch.setattr(sounds, "_actif", False)
    monkeypatch.setattr(sounds, "_volume", 0.6)
    monkeypatch.setattr(sounds, "QSoundEffect", _FauxEffet)


# ── configure : activation ───────────────────────────────────────────────────

def test_les_sons_sont_coupes_par_defaut():
    """Le panel ne doit pas se mettre à sonner à la première installation."""
    sounds.configure({})
    assert sounds.is_enabled() is False


@pytest.mark.parametrize("config,attendu", [
    ({"sounds": {"enabled": True}}, True),
    ({"sounds": {"enabled": False}}, False),
    ({"sounds": {}}, False),
    ({"sounds": None}, False),
    ({}, False),
    (None, False),
    ({"autre": 1}, False),
    ({"sounds": {"enabled": 1}}, True),
    ({"sounds": {"enabled": 0}}, False),
    ({"sounds": {"enabled": "oui"}}, True),
    ({"sounds": {"enabled": ""}}, False),
    ({"sounds": {"enabled": None}}, False),
], ids=["activé", "désactivé", "section vide", "section nulle", "sans section",
        "sans config", "autre section", "1", "0", "chaîne", "chaîne vide", "nul"])
def test_activation_depuis_la_configuration(config, attendu):
    """Tout ce qui n'est pas explicitement vrai laisse les sons coupés."""
    sounds.configure(config)
    assert sounds.is_enabled() is attendu


# ── configure : volume ───────────────────────────────────────────────────────

@pytest.mark.parametrize("brut,attendu", [
    (0, 0.0),
    (50, 0.5),
    (100, 1.0),
    (60, 0.6),
    ("75", 0.75),          # le JSON peut rendre une chaîne
    (33.3, 0.333),
], ids=["muet", "moitié", "maximum", "défaut", "chaîne chiffrée", "décimal"])
def test_le_volume_est_un_pourcentage(brut, attendu):
    sounds.configure({"sounds": {"volume": brut}})
    assert sounds._volume == pytest.approx(attendu)


@pytest.mark.parametrize("brut", [150, 1000, 1e9], ids=["150", "1000", "énorme"])
def test_un_volume_excessif_est_ramene_au_maximum(brut):
    """Personne ne doit pouvoir se faire surprendre par un 500 % dans le JSON."""
    sounds.configure({"sounds": {"volume": brut}})
    assert sounds._volume == 1.0


@pytest.mark.parametrize("brut", [-1, -100, float("-inf")],
                         ids=["-1", "-100", "moins l'infini"])
def test_un_volume_negatif_est_ramene_a_zero(brut):
    sounds.configure({"sounds": {"volume": brut}})
    assert sounds._volume == 0.0


@pytest.mark.parametrize("brut", ["fort", None, [], {}, ""],
                         ids=["mot", "nul", "liste", "dict", "vide"])
def test_un_volume_illisible_retombe_sur_le_defaut(brut):
    """Un réglage qu'on ne sait pas lire vaut mieux à 60 % qu'à fond."""
    sounds.configure({"sounds": {"volume": brut}})
    assert sounds._volume == pytest.approx(0.6)


@pytest.mark.parametrize("brut", [float("nan"), "nan"],
                         ids=["flottant", "chaîne"])
def test_un_volume_nan_ne_donne_pas_le_maximum(brut):
    """Le cas retors : NaN traverse `float()` sans lever et le bornage aussi.

    C'est exactement la surprise que le module cherche à éviter — un son à
    fond en plein direct — obtenue par une valeur qu'aucune interface n'écrit
    mais qu'un fichier de configuration édité à la main peut contenir.
    """
    sounds.configure({"sounds": {"volume": brut}})
    assert sounds._volume != 1.0


def test_le_volume_est_applique_aux_sons_deja_charges():
    """Régler le volume en cours de session doit agir tout de suite.

    Les QSoundEffect sont conservés d'une lecture à l'autre : sans cette
    boucle, le nouveau réglage n'aurait d'effet qu'au prochain démarrage.
    """
    sounds.configure({"sounds": {"enabled": True, "volume": 100}})
    sounds.play("milestone")
    effet = sounds._effets["milestone"]
    assert effet.volume == 1.0

    sounds.configure({"sounds": {"enabled": True, "volume": 20}})
    assert effet.volume == pytest.approx(0.2)


@pytest.mark.parametrize("section", ["oui", ["enabled"], 1],
                         ids=["chaîne", "liste", "nombre"])
def test_une_section_sounds_aberrante_ne_leve_pas(section):
    """Une configuration abîmée ne doit pas empêcher l'application de démarrer.

    `configure` est appelé au lancement : une exception ici remonte jusqu'à
    l'initialisation, alors que le pire qui devrait arriver est de perdre les
    sons.
    """
    sounds.configure({"sounds": section})
    assert sounds.is_enabled() is False


# ── play ─────────────────────────────────────────────────────────────────────

def test_rien_n_est_joue_quand_les_sons_sont_coupes():
    """La promesse principale du module."""
    sounds.configure({"sounds": {"enabled": False}})
    assert sounds.play("milestone") is False
    assert sounds._effets == {}, "pas même de chargement inutile"


def test_un_son_est_joue_quand_les_sons_sont_actifs():
    sounds.configure({"sounds": {"enabled": True}})
    assert sounds.play("milestone") is True
    assert sounds._effets["milestone"].lectures == 1


def test_force_joue_malgre_la_coupure():
    """Le bouton « écouter » de la fenêtre de réglages : sans lui, impossible
    de choisir un volume avant d'activer les sons."""
    sounds.configure({"sounds": {"enabled": False}})
    assert sounds.play("goal", force=True) is True
    assert sounds._effets["goal"].lectures == 1


@pytest.mark.parametrize("nom", ["milestone", "goal"])
def test_les_deux_timbres_existent_sur_le_disque(nom):
    """Les .wav sont livrés avec l'application : un renommage les perdrait."""
    assert (sounds._DOSSIER / sounds._FICHIERS[nom]).exists()


@pytest.mark.parametrize("nom", ["inconnu", "", "milestone.wav", None],
                         ids=["inconnu", "vide", "nom de fichier", "nul"])
def test_un_son_inconnu_ne_joue_rien(nom):
    sounds.configure({"sounds": {"enabled": True}})
    assert sounds.play(nom) is False


def test_un_fichier_manquant_ne_leve_pas(tmp_path, monkeypatch, caplog):
    """Un paquet incomplet doit coûter le son, pas l'alerte."""
    monkeypatch.setattr(sounds, "_DOSSIER", tmp_path)
    sounds.configure({"sounds": {"enabled": True}})
    with caplog.at_level("WARNING", logger=sounds.logger.name):
        assert sounds.play("milestone") is False
    assert any("introuvable" in enr.message for enr in caplog.records)


def test_le_son_n_est_charge_qu_une_fois():
    """Recharger l'échantillon à chaque alerte ajouterait une latence, et un
    QSoundEffect neuf n'a pas fini de charger au moment où on lui dit de
    jouer — l'alerte serait muette."""
    sounds.configure({"sounds": {"enabled": True}})
    sounds.play("milestone")
    sounds.play("milestone")
    premier = sounds._effets["milestone"]
    assert premier.lectures == 2
    assert len(sounds._effets) == 1


def test_une_erreur_de_lecture_ne_remonte_pas():
    """Un pilote audio capricieux ne doit pas faire tomber l'appelant : `play`
    est invoqué depuis le traitement d'une alerte."""
    class _EffetCasse(_FauxEffet):
        def play(self):
            raise RuntimeError("périphérique audio absent")

    monkeypatched = _EffetCasse()
    sounds._effets["milestone"] = monkeypatched
    sounds.configure({"sounds": {"enabled": True}})
    assert sounds.play("milestone") is False
