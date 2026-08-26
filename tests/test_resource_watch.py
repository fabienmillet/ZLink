# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Surveillance des ressources : décision d'alerte et agrégation GPU.

L'enjeu tient en une phrase : ne conseiller de baisser le nombre de flux que
lorsque c'est vraiment ZLink qui sature la machine. Une alerte injustifiée
apprend à l'utilisateur à ignorer les suivantes.
"""

from __future__ import annotations

import pytest

from core.resource_watch import (
    LIBELLES,
    Releve,
    _charge_par_moteur,
    _Detecteur,
    _EtatCpu,
    lire_cpu,
    message_conseil,
)


def _sature(**kw) -> Releve:
    """Relevé saturé sur les deux ressources, sauf indication contraire."""
    base = dict(cpu_systeme=95.0, cpu_zlink=80.0,
                gpu_systeme=95.0, gpu_zlink=80.0)
    base.update(kw)
    return Releve(**base)


# ── il faut que ça dure ──────────────────────────────────────────────────────

def test_un_pic_isole_ne_declenche_rien():
    """Un changement de flux ou une pub qui démarre fait un pic d'une seconde.

    Alerter dessus rendrait le conseil inutilisable.
    """
    d = _Detecteur(consecutifs=3)
    assert d.observer(_sature(), 0.0) is None
    assert d.observer(Releve(cpu_systeme=10.0, cpu_zlink=5.0,
                             gpu_systeme=10.0, gpu_zlink=5.0), 1.0) is None
    assert d.observer(_sature(), 2.0) is None


def test_une_saturation_qui_dure_finit_par_alerter():
    d = _Detecteur(consecutifs=3)
    assert d.observer(_sature(), 0.0) is None
    assert d.observer(_sature(), 5.0) is None
    assert d.observer(_sature(), 10.0) == "gpu"


def test_la_serie_repart_de_zero_apres_une_accalmie():
    d = _Detecteur(consecutifs=3)
    d.observer(_sature(), 0.0)
    d.observer(_sature(), 5.0)
    d.observer(Releve(gpu_systeme=10.0, gpu_zlink=5.0), 10.0)   # accalmie
    assert d.observer(_sature(), 15.0) is None, "la série doit repartir de zéro"


# ── les deux conditions ──────────────────────────────────────────────────────

def test_une_machine_saturee_par_AUTRE_CHOSE_ne_declenche_pas():
    """Un export vidéo lancé à côté ne se corrige pas en fermant des cellules."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(_sature(gpu_zlink=5.0, cpu_zlink=5.0), 0.0) is None


def test_zlink_gourmand_sur_une_machine_qui_respire_ne_declenche_pas():
    """80 % d'une machine à 40 % de charge : il reste de la marge."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(gpu_systeme=40.0, gpu_zlink=80.0,
                             cpu_systeme=40.0, cpu_zlink=80.0), 0.0) is None


@pytest.mark.parametrize("total,part,attendu", [
    (90.0, 50.0, "gpu"),      # bornes incluses des deux côtés
    (89.9, 50.0, None),
    (90.0, 49.9, None),
    (100.0, 100.0, "gpu"),
])
def test_bornes_des_seuils(total, part, attendu):
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(gpu_systeme=total, gpu_zlink=part), 0.0) == attendu


def test_une_mesure_indisponible_n_est_pas_une_saturation():
    """On ne devine pas : sans mesure, pas d'alerte."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(), 0.0) is None
    assert d.observer(Releve(gpu_systeme=99.0, gpu_zlink=None), 0.0) is None
    assert d.observer(Releve(gpu_systeme=None, gpu_zlink=99.0), 0.0) is None


# ── ne pas répéter ───────────────────────────────────────────────────────────

def test_le_conseil_n_est_pas_repete_en_boucle():
    """La situation ne se corrige pas en dix secondes.

    Redire la même chose toutes les cinq secondes ne la rendrait pas plus utile.
    """
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    # Uniquement le GPU : avec les deux saturés, le détecteur bascule sur le
    # processeur pendant le silence du GPU, ce qui est un autre comportement.
    gpu_seul = Releve(gpu_systeme=95.0, gpu_zlink=80.0)
    assert d.observer(gpu_seul, 0.0) == "gpu"
    for t in (5.0, 60.0, 599.0):
        assert d.observer(gpu_seul, t) is None
    assert d.observer(gpu_seul, 601.0) == "gpu"


def test_pendant_le_silence_du_gpu_le_processeur_reste_signalable():
    """Deux ressources, deux silences : ce n'est pas la même information.

    Le conseil est le même, mais savoir QUOI sature oriente le réglage — la
    qualité des flux pour le décodeur, leur nombre pour le processeur.
    """
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    assert d.observer(_sature(), 0.0) == "gpu"
    assert d.observer(_sature(), 5.0) == "cpu"
    assert d.observer(_sature(), 10.0) is None, "les deux sont maintenant en silence"


def test_les_deux_ressources_ont_leur_propre_silence():
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    assert d.observer(_sature(), 0.0) == "gpu"
    # Le GPU se calme, le processeur sature : le conseil reste pertinent.
    assert d.observer(Releve(cpu_systeme=95.0, cpu_zlink=80.0), 5.0) == "cpu"


def test_une_seule_ressource_signalee_a_la_fois():
    """Si les deux saturent, le conseil est le même — le dire deux fois n'aide pas."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(_sature(), 0.0) == "gpu"


# ── agrégation GPU ───────────────────────────────────────────────────────────

def _inst(pid: int, moteur: str) -> str:
    return f"pid_{pid}_luid_0x0_0x1_phys_0_eng_0_engtype_{moteur}"


def test_le_moteur_le_plus_charge_l_emporte():
    """Un GPU fait tourner rendu, décodage et copies EN PARALLÈLE.

    Sommer le tout donnait 97 % là où Windows en affiche 63.
    """
    valeurs = [
        (_inst(42, "3D"), 29.4),
        (_inst(42, "Video Codec 0"), 40.0),
        (_inst(99, "Video Codec 0"), 19.1),
        (_inst(7, "Copy"), 0.4),
    ]
    total, mien, moteur = _charge_par_moteur(valeurs, pid=42)
    assert moteur == "Video Codec 0"
    assert total == pytest.approx(59.1)
    assert mien == pytest.approx(40.0), "la part de ce pid sur CE moteur"


def test_la_charge_est_plafonnee_a_cent():
    valeurs = [(_inst(1, "3D"), 80.0), (_inst(2, "3D"), 80.0)]
    total, _mien, _m = _charge_par_moteur(valeurs, pid=1)
    assert total == 100.0


def test_instances_sans_type_de_moteur_ignorees():
    total, _mien, moteur = _charge_par_moteur([("compteur_bizarre", 50.0)], pid=1)
    assert total is None and moteur == ""


def test_aucune_instance():
    assert _charge_par_moteur([], pid=1) == (None, 0.0, "")


def test_un_pid_absent_a_une_part_nulle():
    total, mien, _m = _charge_par_moteur([(_inst(99, "3D"), 50.0)], pid=42)
    assert total == pytest.approx(50.0) and mien == 0.0


# ── lecture CPU ──────────────────────────────────────────────────────────────

def test_le_premier_releve_ne_rend_rien(monkeypatch):
    """Un pourcentage d'usage se lit sur un ÉCART, pas sur un compteur cumulé."""
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: (1000.0, 500.0, 100.0))
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: (1000.0, 500.0, 100.0))
    assert lire_cpu(_EtatCpu()) == (None, None)


def test_l_usage_se_lit_sur_l_ecart(monkeypatch):
    compteurs = {"v": (1000.0, 500.0, 100.0)}
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: compteurs["v"])
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: compteurs["v"])
    etat = _EtatCpu()
    lire_cpu(etat)
    # +100 de temps total, dont +75 actif, dont +30 pour nous.
    compteurs["v"] = (1100.0, 575.0, 130.0)
    charge, part = lire_cpu(etat)
    assert charge == pytest.approx(75.0)
    assert part == pytest.approx(40.0), "30 des 75 unités actives"


def test_une_machine_au_repos_donne_une_part_nulle(monkeypatch):
    compteurs = {"v": (1000.0, 500.0, 100.0)}
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: compteurs["v"])
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: compteurs["v"])
    etat = _EtatCpu()
    lire_cpu(etat)
    compteurs["v"] = (1100.0, 500.0, 100.0)     # rien d'actif
    charge, part = lire_cpu(etat)
    assert charge == 0.0 and part == 0.0


def test_sonde_indisponible(monkeypatch):
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows", lambda: None)
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux", lambda: None)
    assert lire_cpu(_EtatCpu()) == (None, None)


# ── message ──────────────────────────────────────────────────────────────────

def test_le_message_dit_le_constat_puis_quoi_faire():
    m = message_conseil("gpu", 97.0, 82.0)
    assert LIBELLES["gpu"] in m.lower()
    assert "97" in m and "82" in m
    assert "flux" in m, "un constat sans conseil ne sert à rien"


def test_le_message_supporte_une_ressource_inconnue():
    assert "memoire" in message_conseil("memoire", 99.0, 90.0).lower()
