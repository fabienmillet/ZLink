# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Interrupteurs des alertes : ce que l'utilisateur a le droit de faire taire.

Deux promesses à tenir. Une alerte coupée dans les réglages doit rester coupée
— c'est la raison d'être du module. Et une configuration partielle ou abîmée ne
doit pas éteindre en silence des alertes qu'on n'a jamais demandé à couper : le
défaut d'une famille absente est son défaut, pas « faux ».

`_ETATS` est un état de module partagé : la fixture le remet à neuf, sinon un
test hériterait des réglages du précédent.
"""

from __future__ import annotations

import pytest

from core import alerts

TOUTES = [cle for cle, _lib, _def, _aide in alerts.FAMILLES]


@pytest.fixture(autouse=True)
def etats_neufs(monkeypatch):
    """Isole chaque test de l'état global laissé par le précédent.

    Les DEUX réglages, pas seulement les familles : la restriction aux favoris
    est elle aussi un état de module, et l'oublier ici la laissait active pour
    tous les fichiers de test suivants — sept d'entre eux tombaient d'un coup,
    en passant pourtant chacun isolément.
    """
    monkeypatch.setattr(alerts, "_ETATS", dict(alerts._DEFAUTS))
    monkeypatch.setattr(alerts, "_OBJECTIFS_FAVORIS_SEULEMENT", False)


# ── le catalogue lui-même ────────────────────────────────────────────────────

def test_le_catalogue_est_coherent():
    """Chaque famille a une clé, un libellé et une aide non vides.

    Ces trois champs alimentent la fenêtre de réglages : un libellé oublié y
    apparaîtrait comme une case à cocher anonyme.
    """
    assert len(set(TOUTES)) == len(TOUTES), "clés dupliquées"
    for cle, libelle, defaut, aide in alerts.FAMILLES:
        assert cle and libelle and aide, cle
        assert isinstance(defaut, bool), cle


def test_tout_est_actif_sans_configuration():
    """Une première installation doit voir passer les alertes."""
    assert all(alerts.enabled(cle) for cle in TOUTES)


# ── configure ────────────────────────────────────────────────────────────────

def test_une_famille_coupee_le_reste():
    alerts.configure({"alerts": {"raid": False}})
    assert alerts.enabled("raid") is False


def test_couper_une_famille_n_en_coupe_pas_d_autres():
    """Le piège d'une boucle qui écraserait tout avec la même valeur."""
    alerts.configure({"alerts": {"raid": False}})
    assert [c for c in TOUTES if not alerts.enabled(c)] == ["raid"]


def test_tout_couper_puis_tout_rallumer():
    alerts.configure({"alerts": dict.fromkeys(TOUTES, False)})
    assert not any(alerts.enabled(c) for c in TOUTES)
    alerts.configure({"alerts": dict.fromkeys(TOUTES, True)})
    assert all(alerts.enabled(c) for c in TOUTES)


def test_une_configuration_ne_laisse_rien_du_reglage_precedent():
    """`configure` remplace, il n'ajoute pas : recharger une configuration où
    « raid » est de nouveau coché doit vraiment le rallumer."""
    alerts.configure({"alerts": {"raid": False}})
    alerts.configure({"alerts": {}})
    assert alerts.enabled("raid") is True


@pytest.mark.parametrize("config,pourquoi", [
    ({}, "configuration vide"),
    (None, "aucune configuration"),
    ({"alerts": None}, "section présente mais nulle"),
    ({"alerts": "oui"}, "section d'un type inattendu"),
    ({"alerts": ["hype"]}, "section sous forme de liste"),
    ({"alerts": 0}, "section numérique"),
    ({"autre_chose": {"raid": False}}, "section absente"),
], ids=["vide", "aucune", "nulle", "chaîne", "liste", "nombre", "absente"])
def test_une_configuration_inexploitable_garde_les_defauts(config, pourquoi):
    """Une configuration abîmée ne doit pas faire taire les alertes en douce.

    Le silence est le pire des échecs ici : l'utilisateur croirait simplement
    qu'il ne se passe rien pendant l'event.
    """
    alerts.configure(config)
    assert all(alerts.enabled(c) for c in TOUTES), pourquoi


def test_une_famille_inconnue_dans_la_configuration_est_ignoree():
    """Un réglage laissé par une version plus récente ne doit rien casser."""
    alerts.configure({"alerts": {"famille_disparue": False}})
    assert all(alerts.enabled(c) for c in TOUTES)
    assert "famille_disparue" not in alerts.states()


@pytest.mark.parametrize("valeur,attendu", [
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("", False),
    ("false", True),      # une chaîne non vide reste vraie : voir la docstring
    (None, False),
    ([], False),
], ids=["vrai", "faux", "1", "0", "chaîne vide", "chaîne 'false'", "nul", "liste vide"])
def test_les_valeurs_aberrantes_passent_par_bool(valeur, attendu):
    """Le réglage est ramené à un booléen Python, sans interprétation.

    « false » écrit à la main dans le JSON reste donc VRAI. C'est assumé : la
    fenêtre de réglages n'écrit que des booléens, et deviner l'intention d'une
    chaîne mènerait à des surprises pires (« off », « non », « 0 »…).
    """
    alerts.configure({"alerts": {"raid": valeur}})
    assert alerts.enabled("raid") is attendu


def test_les_familles_coupees_sont_journalisees(caplog):
    """Une alerte muette doit être explicable en lisant le journal."""
    with caplog.at_level("INFO", logger=alerts.logger.name):
        alerts.configure({"alerts": {"raid": False, "hype": False}})
    trace = " ".join(enr.getMessage() for enr in caplog.records)
    assert "raid" in trace and "hype" in trace


def test_rien_n_est_journalise_quand_tout_est_actif(caplog):
    """Pas de ligne inutile au démarrage dans le cas courant."""
    with caplog.at_level("INFO", logger=alerts.logger.name):
        alerts.configure({"alerts": {}})
    assert caplog.records == []


# ── enabled / states ─────────────────────────────────────────────────────────

def test_une_famille_inconnue_est_autorisee():
    """Défaut permissif : une alerte ajoutée au code et pas encore au
    catalogue doit sortir, quitte à ne pas être réglable tout de suite.
    Se taire serait plus difficile à diagnostiquer."""
    assert alerts.enabled("pas_une_famille") is True
    assert alerts.enabled("") is True


def test_states_rend_le_catalogue_complet():
    assert set(alerts.states()) == set(TOUTES)


def test_states_rend_une_copie():
    """Sans copie, un appelant curieux pourrait couper une alerte par erreur."""
    copie = alerts.states()
    copie["raid"] = False
    assert alerts.enabled("raid") is True


# ── objectifs restreints aux favoris ─────────────────────────────────────────

@pytest.fixture
def favoris(monkeypatch):
    """Ensemble de favoris contrôlé, sans toucher au fichier de l'utilisateur."""
    table: set[str] = set()
    from core import favorites
    monkeypatch.setattr(favorites, "get", lambda: table)
    return table


def test_par_defaut_les_objectifs_alertent_pour_tout_le_monde(favoris):
    """Couper des alertes sans qu'on l'ait demandé serait pire que d'en
    recevoir trop : on ne remarque pas ce qui n'arrive pas."""
    alerts.configure({})
    assert alerts.objectifs_favoris_seulement() is False
    assert alerts.enabled_pour("goal_done", "inconnu") is True


@pytest.mark.parametrize("famille", ["goal_done", "goal_imminent"])
def test_restreints_aux_favoris_les_objectifs_se_taisent_pour_les_autres(
        favoris, famille):
    alerts.configure({alerts.CLE_OBJECTIFS_FAVORIS: True})
    favoris.add("morrigh4n")
    assert alerts.enabled_pour(famille, "morrigh4n") is True
    assert alerts.enabled_pour(famille, "quelqu_un_d_autre") is False


def test_la_restriction_ne_touche_pas_les_autres_familles(favoris):
    """Elle ne vise QUE les objectifs : un raid ou un palier restent annoncés."""
    alerts.configure({alerts.CLE_OBJECTIFS_FAVORIS: True})
    for famille in ("raid", "milestone", "hype", "favorite_live"):
        assert alerts.enabled_pour(famille, "quelqu_un_d_autre") is True


def test_une_famille_eteinte_le_reste_meme_pour_un_favori(favoris):
    """La restriction affine, elle ne rallume rien."""
    alerts.configure({"alerts": {"goal_done": False},
                      alerts.CLE_OBJECTIFS_FAVORIS: True})
    favoris.add("morrigh4n")
    assert alerts.enabled_pour("goal_done", "morrigh4n") is False


def test_une_alerte_sans_chaine_passe(favoris):
    """La restriction n'a rien à mordre : l'alerte ne vise personne."""
    alerts.configure({alerts.CLE_OBJECTIFS_FAVORIS: True})
    assert alerts.enabled_pour("goal_done", "") is True


def test_la_casse_du_login_n_ecarte_pas_un_favori(favoris):
    """Les favoris sont rangés en minuscules, les logins arrivent tels quels."""
    alerts.configure({alerts.CLE_OBJECTIFS_FAVORIS: True})
    favoris.add("morrigh4n")
    assert alerts.enabled_pour("goal_done", "Morrigh4n") is True
