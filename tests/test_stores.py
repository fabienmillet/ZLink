# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Persistance : configuration, favoris, sélection de grille et préréglages.

Toutes ces écritures visent le MÊME config.json, et deux d'entre elles font une
lecture-modification-écriture. Le point sensible est donc qu'une écriture n'en
efface pas une autre — c'est ce que ces tests vérifient.
"""

from __future__ import annotations

import json

import pytest

from core import config_store, favorites, selection_store


@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json neuf, partagé par config_store et favorites."""
    cible = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", cible)
    monkeypatch.setattr(favorites, "CONFIG_PATH", cible)
    # Les favoris gardent leur liste en mémoire : sans remise à zéro, un test
    # hériterait du cache du précédent.
    monkeypatch.setattr(favorites, "_cache", None)
    return cible


@pytest.fixture
def selection(tmp_path, monkeypatch):
    monkeypatch.setattr(selection_store, "STORE_PATH",
                        tmp_path / "grid_selection.json")


# ── config_store ─────────────────────────────────────────────────────────────

def test_config_absente_rend_un_dict_vide(config):
    assert config_store.load() == {}


def test_ecriture_puis_relecture(config):
    assert config_store.save_merge({"a": 1}) is True
    assert config_store.load() == {"a": 1}


def test_la_fusion_conserve_les_autres_cles(config):
    config_store.save_merge({"a": 1, "b": 2})
    config_store.save_merge({"b": 3})
    assert config_store.load() == {"a": 1, "b": 3}


def test_config_illisible_ne_leve_pas(config):
    config.write_text("{ ceci n'est pas du json", encoding="utf-8")
    assert config_store.load() == {}


def test_config_qui_n_est_pas_un_objet_est_ignoree(config):
    config.write_text('["une", "liste"]', encoding="utf-8")
    assert config_store.load() == {}


def test_patch_invalide_refuse(config):
    assert config_store.save_merge("pas un dict") is False
    assert config_store.save_merge(None) is False


def test_les_cles_retirees_disparaissent_a_la_prochaine_ecriture(config):
    """Deux de ces clés étaient des emplacements de clés d'API.

    La fonctionnalité a été supprimée ; autant ne pas conserver de champ à
    secret inutile dans le fichier.
    """
    config.write_text(json.dumps({
        "openai_api_key": "sk-secret", "gemini_api_key": "x",
        "ai_provider": "openai", "ai_model": "gpt", "garde_moi": 1,
    }), encoding="utf-8")
    config_store.save_merge({"autre": 2})
    reste = config_store.load()
    assert "openai_api_key" not in reste
    assert "gemini_api_key" not in reste
    assert "ai_provider" not in reste
    assert "ai_model" not in reste
    assert reste["garde_moi"] == 1 and reste["autre"] == 2


# ── favoris ──────────────────────────────────────────────────────────────────

def test_aucun_favori_au_depart(config):
    assert favorites.get() == set()
    assert favorites.is_favorite("zerator") is False


def test_bascule_aller_retour(config):
    assert favorites.toggle("ZeratoR") is True
    assert favorites.is_favorite("zerator") is True
    assert favorites.is_favorite("ZERATOR") is True, "insensible à la casse"
    assert favorites.toggle("zerator") is False
    assert favorites.is_favorite("zerator") is False


def test_favori_vide_refuse(config):
    assert favorites.toggle("") is False
    assert favorites.get() == set()


def test_les_favoris_sont_ecrits_dans_config_json(config):
    favorites.toggle("zerator")
    ecrit = json.loads(config.read_text(encoding="utf-8"))
    assert ecrit["favorite_logins"] == ["zerator"]


def test_ecrire_un_favori_n_efface_pas_le_reste_de_la_config(config):
    """Le piège de deux lectures-modifications-écritures sur le même fichier."""
    config_store.save_merge({"max_active_streams": 20})
    favorites.toggle("zerator")
    reste = config_store.load()
    assert reste["max_active_streams"] == 20
    assert reste["favorite_logins"] == ["zerator"]


def test_favoris_illisibles_rendent_un_ensemble_vide(config, monkeypatch):
    config.write_text('{"favorite_logins": "pas une liste"}', encoding="utf-8")
    monkeypatch.setattr(favorites, "_cache", None)
    assert favorites.get() == set()


# ── sélection de grille ──────────────────────────────────────────────────────

def test_selection_vide_au_depart(selection):
    s = selection_store.SelectionStore()
    assert s.get_selected() == [] and s.count() == 0


def test_l_ordre_de_selection_est_conserve(selection):
    s = selection_store.SelectionStore()
    s.set_selected("b", True)
    s.set_selected("a", True)
    s.set_selected("c", True)
    assert s.get_selected() == ["b", "a", "c"], "l'ordre pilote les numéros de slot"


def test_deselection(selection):
    s = selection_store.SelectionStore()
    s.set_all(["a", "b", "c"])
    s.set_selected("b", False)
    assert s.get_selected() == ["a", "c"]


def test_set_all_dedoublonne(selection):
    s = selection_store.SelectionStore()
    s.set_all(["a", "b", "a", "b"])
    assert s.get_selected() == ["a", "b"]


def test_la_selection_survit_a_un_rechargement(selection):
    s = selection_store.SelectionStore()
    s.set_all(["z", "a", "m"])
    s.save()
    assert selection_store.SelectionStore().get_selected() == ["z", "a", "m"]


def test_fichier_de_selection_corrompu_ne_leve_pas(selection, tmp_path):
    (tmp_path / "grid_selection.json").write_text("{pas du json", encoding="utf-8")
    assert selection_store.SelectionStore().get_selected() == []


def test_clear(selection):
    s = selection_store.SelectionStore()
    s.set_all(["a", "b"])
    s.clear()
    assert s.get_selected() == []


# ── préréglages ──────────────────────────────────────────────────────────────

def test_preregles_aller_retour(config, selection):
    s = selection_store.SelectionStore()
    assert s.save_preset("Soirée", ["a", "b"]) is True
    assert s.presets() == {"Soirée": ["a", "b"]}


def test_preregle_sans_nom_refuse(config, selection):
    s = selection_store.SelectionStore()
    assert s.save_preset("   ", ["a"]) is False
    assert s.presets() == {}


def test_preregle_ecrase_son_homonyme(config, selection):
    s = selection_store.SelectionStore()
    s.save_preset("P", ["a"])
    s.save_preset("P", ["b", "c"])
    assert s.presets() == {"P": ["b", "c"]}


def test_suppression_d_un_preregle(config, selection):
    s = selection_store.SelectionStore()
    s.save_preset("P", ["a"])
    assert s.delete_preset("P") is True
    assert s.presets() == {}
    assert s.delete_preset("inconnu") is False


def test_preregles_corrompus_rendent_un_dict_vide(config, selection):
    config.write_text('{"grid_presets": "pas un objet"}', encoding="utf-8")
    assert selection_store.SelectionStore().presets() == {}
