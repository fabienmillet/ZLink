# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection de moments forts : ligne de base, token dominant, qualification.

Le cœur de la règle est qu'un moment fort se mesure en UTILISATEURS DISTINCTS
qui écrivent la même chose, pas en messages. Un spammeur seul ne doit pas
suffire, et un mot ordinaire non plus.
"""

from __future__ import annotations

import pytest

import core.hype_watcher as hw
from core.hype_watcher import (
    _BASELINE_MIN_SAMPLES,
    _C_DONO,
    _C_FUNNY,
    _Ewma,
    _LIBELLE_MOMENT_FORT,
    _MIN_DOMINANT_USERS,
    _classify_local,
    _dominant_token,
    _kw_matcher,
)


# ── moyenne glissante ────────────────────────────────────────────────────────

def test_pas_de_verdict_avant_d_avoir_observe_assez():
    """Une chaîne doit être observée avant de pouvoir déclencher une alerte."""
    e = _Ewma()
    for _ in range(_BASELINE_MIN_SAMPLES - 1):
        e.update(10.0, 2.0)
        assert e.deviation(1000.0) is None
    e.update(10.0, 2.0)
    assert e.deviation(1000.0) is not None


def test_ecart_borne_entre_zero_et_un():
    e = _Ewma()
    for _ in range(_BASELINE_MIN_SAMPLES):
        e.update(10.0, 2.0)
    assert e.deviation(10_000.0) == pytest.approx(1.0)
    assert e.deviation(0.0) == 0.0, "un creux n'est pas un moment fort"


def test_une_valeur_normale_ne_sature_pas():
    e = _Ewma()
    for i in range(_BASELINE_MIN_SAMPLES * 2):
        e.update(10.0 + (i % 3), 2.0)
    assert e.deviation(11.0) < 0.5


def test_premiere_mesure_devient_la_moyenne():
    e = _Ewma()
    e.update(42.0, 2.0)
    assert e.mean == pytest.approx(42.0)
    assert e.n == 1


# ── correspondance de mots-clés ──────────────────────────────────────────────

def test_les_mots_cles_sont_cherches_en_MOTS_ENTIERS():
    """Une recherche de sous-chaîne classait « pardon » et « abandonne » en
    Donation — catégorie factuelle, donc prioritaire sur tout le reste."""
    match = _kw_matcher(["don", "dono"])
    assert match("gros don sur la cagnotte") is True
    assert match("pardon") is False
    assert match("abandonne") is False
    assert match("donc") is False
    assert match("donner") is False


def test_les_symboles_sont_cherches_tels_quels():
    """€ et emoji n'ont pas de limite de mot au sens des regex."""
    match = _kw_matcher(["€", "💀"])
    assert match("500€ !") is True
    assert match("mdr 💀") is True
    assert match("rien") is False


# ── token dominant ───────────────────────────────────────────────────────────

def _chat(*couples):
    return list(couples)


def test_le_token_dominant_compte_des_personnes():
    entries = _chat(*[(f"user{i}", "pogchamp") for i in range(5)])
    token, n = _dominant_token(entries)
    assert token == "pogchamp"
    assert n == 5


def test_un_spammeur_seul_ne_fait_pas_un_moment_fort():
    """Compter les occurrences laisserait une personne imposer son mot."""
    entries = _chat(*[("spammeur", "pogchamp") for _ in range(50)])
    _token, n = _dominant_token(entries)
    assert n == 1


def test_un_message_ne_vote_qu_une_fois_par_token():
    entries = _chat(("u1", "lul lul lul lul"), ("u2", "lul"))
    _token, n = _dominant_token(entries)
    assert n == 2


def test_chat_vide():
    assert _dominant_token([]) == ("", 0)


# ── qualification ────────────────────────────────────────────────────────────

def test_sans_chat_le_libelle_reste_generique():
    label, _color, excerpt = _classify_local([])
    assert label == _LIBELLE_MOMENT_FORT
    assert excerpt == ""


def test_un_message_parlant_d_argent_ne_fait_pas_une_donation():
    """Le contraire était vrai, et remplissait le fil de fausses donations.

    Un seul message contenant « € », « don » ou « cagnotte » suffisait à
    étiqueter le moment « Donation 💸 » et à se citer lui-même — en
    court-circuitant toute la mesure de convergence. Pendant un ZEvent le chat
    parle d'argent en permanence : « euh, 85 % du revenu c'est 15 M€ ? »
    devenait ainsi une donation. Une donation est un fait, et ZLink le tient
    de l'API, chiffré.
    """
    entries = _chat(*[(f"u{i}", "lul") for i in range(10)])
    entries.append(("u99", "grosse donation de 500 euros"))
    label, color, excerpt = _classify_local(entries)
    assert color == _C_FUNNY, "c'est le mouvement du chat qui qualifie"
    assert excerpt == "« lul » ×10"


def test_le_chat_qui_converge_sur_l_argent_reste_une_donation():
    """Les mots-clés servent toujours — sur ce que le chat a REPRIS."""
    entries = _chat(*[(f"u{i}", "€") for i in range(10)])
    _label, color, excerpt = _classify_local(entries)
    assert color == _C_DONO
    assert excerpt == "« € » ×10"


def test_un_token_domine_sous_le_seuil_ne_qualifie_pas():
    entries = _chat(*[(f"u{i}", "pogchamp")
                      for i in range(_MIN_DOMINANT_USERS - 1)])
    label, _color, excerpt = _classify_local(entries)
    assert label == _LIBELLE_MOMENT_FORT
    assert excerpt == ""


def test_l_extrait_decrit_le_mouvement_de_chat():
    entries = _chat(*[(f"u{i}", "lul") for i in range(8)])
    _label, color, excerpt = _classify_local(entries)
    assert excerpt == "« lul » ×8"
    assert color == _C_FUNNY


# ── Ce qui distingue une réaction d'une phrase ──────────────────────────────
#
# Compter un mot pris n'importe où laissait gagner le remplissage : « voir »
# présent dans douze phrases différentes se retrouvait cité comme la preuve
# d'un moment fort. Une réaction de chat est un message COURT.

@pytest.mark.parametrize("texte,attendu", [
    ("PARTYHAT", "partyhat"),
    ("lul lul lul lul", "lul"),          # la forme typique du spam d'emote
    ("LUL KEKW", "lul kekw"),
    ("je pense que ce boss est vraiment dur", ""),   # une phrase
    ("faut voir ce qui se passe la", ""),
    ("", ""),
    ("le la les", ""),                   # que des mots vides
])
def test_une_reaction_est_un_message_court(texte, attendu):
    assert hw._reaction(texte) == attendu


# ── Un terme doit DOMINER, pas seulement apparaître ─────────────────────────
#
# Quatre personnes sur quarante qui reprennent l'emote de la chaîne, c'est la
# ligne de base — cette emote est tapée toute la journée. Les mêmes quatre sur
# huit locuteurs, c'est le chat entier qui dit la même chose.

def _chat_mixte(locuteurs: int, repreneurs: int, mot: str = "partyhat"):
    phrases = ["il va y arriver je pense sincerement",
               "ce boss est vraiment tres complique",
               "quelqu un sait combien il reste avant"]
    return [(f"u{i}", mot if i < repreneurs else phrases[i % 3])
            for i in range(locuteurs)]


@pytest.mark.parametrize("locuteurs,repreneurs,retenu", [
    (40, 4, False),     # l'emote de la chaîne, en fond sonore
    (40, 12, True),     # 30 % : le chat converge
    (25, 3, False),     # trois personnes ne font pas un mouvement
    (6, 5, True),       # petit chat, mais tout le monde dit la même chose
    (6, 4, False),      # sous le plancher absolu
])
def test_le_terme_doit_dominer_le_chat(locuteurs, repreneurs, retenu):
    _libelle, _couleur, extrait = hw._classify_local(
        _chat_mixte(locuteurs, repreneurs))
    assert bool(extrait) is retenu


def test_l_exigence_suit_la_taille_du_chat():
    """Un nombre absolu ne peut pas convenir aux deux bouts."""
    assert hw._exigence(_chat_mixte(40, 0)) == 12
    assert hw._exigence(_chat_mixte(6, 0)) == hw._MIN_DOMINANT_USERS


# ── L'audience compte autant que la part ────────────────────────────────────
#
# Sur un million de spectateurs, le chat défile trop vite pour qu'on en voie
# une part représentative : 30 % d'un petit échantillon peut valoir quatre
# personnes. Quatre kappa sur un million de spectateurs ne sont pas un moment
# fort. Un vrai emballement de ZEvent, c'est quarante à cent personnes qui
# écrivent la même chose.

@pytest.mark.parametrize("viewers,exige", [
    (0, 5), (500, 5),
    (1_000, 10), (9_999, 10),
    (10_000, 20), (99_999, 20),
    (100_000, 40), (1_000_000, 40),
])
def test_le_plancher_suit_l_audience(viewers, exige):
    assert hw._plancher_audience(viewers) == exige


def test_quatre_personnes_sur_un_million_ne_declenchent_rien():
    """Le cas exact qui rendait le fil illisible."""
    _l, _c, extrait = hw._classify_local(_chat_mixte(120, 4, "kappa"),
                                         viewers=1_000_000)
    assert extrait == ""


def test_soixante_personnes_sur_un_million_declenchent():
    _l, _c, extrait = hw._classify_local(_chat_mixte(120, 60, "kappa"),
                                         viewers=1_000_000)
    assert extrait == "« kappa » ×60"


def test_une_petite_chaine_garde_un_seuil_atteignable():
    """Sinon une chaîne de trois cents spectateurs n'alerterait jamais."""
    _l, _c, extrait = hw._classify_local(_chat_mixte(12, 5, "vindieu"),
                                         viewers=400)
    assert extrait == "« vindieu » ×5"


def test_c_est_la_mesure_la_plus_exigeante_qui_gagne():
    """Part et plancher se cumulent, ils ne se remplacent pas."""
    # 200 locuteurs → 30 % = 60, bien au-dessus du plancher de 40.
    assert hw._exigence(_chat_mixte(200, 0), viewers=1_000_000) == 60
    # 20 locuteurs → 30 % = 6, c'est le plancher qui commande.
    assert hw._exigence(_chat_mixte(20, 0), viewers=1_000_000) == 40


def test_le_tampon_permet_d_observer_un_vrai_emballement():
    """Quarante messages ne laissaient pas la place à quarante personnes."""
    assert hw._MAX_RECENT_MSGS >= 2 * max(e for _v, e in hw._PLANCHERS_AUDIENCE)
