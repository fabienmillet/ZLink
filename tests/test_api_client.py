# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Validation des entrées de l'API et lecture des participations.

Ces fonctions sont la frontière entre des données venues du réseau et le reste
de l'application : un login sert de nom de fichier et d'argument de
sous-processus, une URL de don est ouverte dans un navigateur. C'est ce qu'on
teste en priorité.
"""

from __future__ import annotations

import pytest

from core.api_client import (
    _DONATION_HOSTS,
    _classer_entree,
    _etat_twitch,
    _euros,
    _format_euros,
    _jeu_affiche,
    _location_label,
    _noter_participant,
    _parse_participants,
    _parse_participation,
    _safe_https_url,
    _safe_login,
    _to_local_time,
    _to_unix_ts,
)


# ── _safe_login ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brut", [
    "zerator", "ZeratoR", "z", "a_b_1", "0", "a" * 25,
])
def test_login_accepte_les_formes_valides(brut):
    assert _safe_login(brut) == brut


@pytest.mark.parametrize("brut", [
    "", None, "   ", "a" * 26, "ze rator", "ze-rator", "ze.rator",
    "../../etc/passwd", "ze;rm -rf", "ze\nrator",
])
def test_login_rejette_le_reste(brut):
    assert _safe_login(brut) == ""


@pytest.mark.parametrize("brut", ["zérator", "Зератор", "漢字", "café"])
def test_login_rejette_l_unicode(brut):
    """re.ASCII : sans lui, \\w laisserait passer ces logins.

    Ils servent de nom de fichier pour le cache d'avatars — accepter des
    caractères non ASCII y ouvrirait des surprises selon le système de fichiers.
    """
    assert _safe_login(brut) == ""


def test_login_est_deballe_des_espaces():
    assert _safe_login("  zerator  ") == "zerator"


# ── _safe_https_url ──────────────────────────────────────────────────────────

def test_url_accepte_https():
    assert _safe_https_url("https://exemple.test/a.png") == "https://exemple.test/a.png"


@pytest.mark.parametrize("brut", [
    "http://exemple.test/a.png",
    "file:///etc/passwd",
    "ftp://exemple.test/a",
    "javascript:alert(1)",
    "https://",           # pas d'hôte
    "", None, "   ",
])
def test_url_rejette_les_schemas_non_https(brut):
    assert _safe_https_url(brut) == ""


def test_url_allowlist_accepte_l_hote_et_ses_sous_domaines():
    assert _safe_https_url("https://zevent.fr/dons", _DONATION_HOSTS)
    assert _safe_https_url("https://www.zevent.fr/dons", _DONATION_HOSTS)


@pytest.mark.parametrize("brut", [
    "https://zevent.fr.evil.test/dons",   # suffixe trompeur
    "https://evil.test/dons",
    "https://notzevent.fr/dons",
])
def test_url_allowlist_rejette_les_hotes_voisins(brut):
    assert _safe_https_url(brut, _DONATION_HOSTS) == ""


def test_url_allowlist_regarde_l_hote_pas_l_identifiant():
    """`https://zevent.fr@evil.test` pointe evil.test, pas zevent.fr.

    Le préfixe avant « @ » est un identifiant, pas un hôte : le confondre
    laisserait passer n'importe quel domaine.
    """
    assert _safe_https_url("https://zevent.fr@evil.test/dons", _DONATION_HOSTS) == ""
    assert _safe_https_url("https://evil.test@zevent.fr/dons", _DONATION_HOSTS)


def test_url_allowlist_ignore_le_port():
    assert _safe_https_url("https://zevent.fr:443/dons", _DONATION_HOSTS)


# ── montants ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brut,attendu", [
    (100, 1.0), (0, 0.0), (None, 0.0), ("2500", 25.0), (1, 0.01),
])
def test_euros_convertit_des_centimes(brut, attendu):
    assert _euros(brut) == pytest.approx(attendu)


@pytest.mark.parametrize("brut", ["abc", object(), [1]])
def test_euros_retombe_a_zero_sur_entree_illisible(brut):
    assert _euros(brut) == 0.0


def test_format_euros_arrondit_et_espace_insecable():
    # Espace fine insécable U+202F comme séparateur de milliers.
    assert _format_euros(1154211.58) == "1 154 212 €"
    assert _format_euros(0) == "0 €"


# ── lieux ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brut,attendu", [
    ("lan", "LAN"),
    ("remote_ankama", "Ankama"),
    ("remote_villa", "Villa"),
    ("remote_zbase", "ZBase"),
    ("remote", "Online"),
    ("", ""),
])
def test_lieux_connus(brut, attendu):
    """Les cinq lieux relevés sur /participations en 2026.

    ZBase manquait : ses quatre streamers tombaient dans le repli, qui les
    affichait « Zbase » et remplissait le journal à chaque relève.
    """
    assert _location_label(brut) == attendu


def test_lieu_inconnu_reste_lisible():
    """Un site apparu en cours d'édition ne doit pas disparaître de l'affichage."""
    assert _location_label("remote_nouveau_site") == "Nouveau Site"


def test_un_lieu_inconnu_ne_se_signale_qu_une_fois(caplog):
    """Quatre streamers sur un site, une relève toutes les trente secondes :
    la même ligne revenait onze mille fois par jour."""
    from core import api_client

    api_client._lieux_signales.discard("remote_bruyant")
    with caplog.at_level("INFO", logger="core.api_client"):
        for _ in range(5):
            _location_label("remote_bruyant")
    assert sum("remote_bruyant" in m for m in caplog.messages) == 1
    api_client._lieux_signales.discard("remote_bruyant")


# ── dates ────────────────────────────────────────────────────────────────────

def test_heure_locale_en_utc_plus_2():
    assert _to_local_time("2026-09-05T16:30:00Z") == "18:30"


def test_heure_locale_sur_entree_invalide_garde_le_debut():
    assert _to_local_time("pas une date") == "pas u"
    assert _to_local_time("") == ""


def test_timestamp_unix():
    assert _to_unix_ts("1970-01-01T00:00:00Z") == 0.0
    assert _to_unix_ts("2026-09-05T16:30:00Z") == pytest.approx(1788625800.0)
    assert _to_unix_ts("n'importe quoi") == 0.0


# ── participations ───────────────────────────────────────────────────────────

def _entree(**extra):
    base = {
        "id": "sid-1",
        "participation_id": "part-1",
        "name": "ZeratoR",
        "location": "lan",
        "live": True,
        "amount_raised": 69400000,
        "profile_url": "https://static.test/z.png",
        "streamers": [{
            "id": "sid-1",
            "name": "ZeratoR",
            "socials": {"twitch": {"login": "zerator"}},
            "streaming_states": [
                {"platform": "youtube", "live": False, "game": "Autre"},
                {"platform": "twitch", "live": True, "game": "Minecraft",
                 "viewers": 42000},
            ],
        }],
    }
    base.update(extra)
    return base


def test_etat_twitch_choisit_la_bonne_plateforme():
    first = _entree()["streamers"][0]
    assert _etat_twitch(first)["game"] == "Minecraft"


def test_etat_twitch_sans_correspondance_rend_un_dict_vide():
    assert _etat_twitch({"streaming_states": [{"platform": "youtube"}]}) == {}
    assert _etat_twitch({}) == {}


def test_jeu_masque_hors_direct():
    assert _jeu_affiche({"game": "Minecraft"}, live=True) == "Minecraft"
    assert _jeu_affiche({"game": "Minecraft"}, live=False) == ""


def test_jeu_masque_le_faux_offline_de_l_api():
    """L'API renvoie parfois « offline » comme nom de jeu.

    L'afficher tel quel donnait des cartes annonçant « offline » en catégorie.
    """
    assert _jeu_affiche({"game": "offline"}, live=True) == ""
    assert _jeu_affiche({"game": "OFFLINE"}, live=True) == ""


def test_participation_complete():
    p = _parse_participation(_entree())
    assert p.streamer_id == "sid-1"
    assert p.twitch_login == "zerator"
    assert p.display == "ZeratoR"
    assert p.location == "LAN"
    assert p.live is True
    assert p.game == "Minecraft"
    assert p.viewers == 42000
    assert p.donation == pytest.approx(694000.0)
    assert p.profile_url == "https://static.test/z.png"


def test_participation_hors_direct_neutralise_jeu_et_viewers():
    p = _parse_participation(_entree(live=False, streamers=[{
        "id": "sid-1", "name": "ZeratoR",
        "socials": {"twitch": {"login": "zerator"}},
        "streaming_states": [{"platform": "twitch", "live": False,
                              "game": "Minecraft", "viewers": 42000}],
    }]))
    assert p.live is False
    assert p.game == ""
    assert p.viewers == 0


def test_participation_login_invalide_devient_vide():
    p = _parse_participation(_entree(streamers=[{
        "id": "sid-1", "name": "X",
        "socials": {"twitch": {"login": "ze rator"}},
        "streaming_states": [],
    }]))
    assert p.twitch_login == ""


def test_participation_url_non_https_ecartee():
    p = _parse_participation(_entree(profile_url="http://static.test/z.png"))
    assert p.profile_url == ""


def test_participation_sur_dict_vide_ne_leve_pas():
    p = _parse_participation({})
    assert p.twitch_login == "" and p.donation == 0.0


# ── participants d'un show ───────────────────────────────────────────────────

def test_participants_format_2026():
    hosts, parts, names, logins, avatars = _parse_participants([
        {"streamer_id": "h1", "streamer_name": "Hôte", "role": "host",
         "socials": {"twitch": {"login": "hote"}},
         "profile_url": "https://static.test/h.png"},
        {"streamer_id": "p1", "streamer_name": "Invité", "role": "guest",
         "socials": {"twitch": {"login": "invite"}}},
    ])
    assert hosts == ["h1"]
    assert parts == ["p1"]
    assert names == {"h1": "Hôte", "p1": "Invité"}
    assert logins == {"h1": "hote", "p1": "invite"}
    assert avatars == {"h1": "https://static.test/h.png"}


def test_participants_format_historique_dict():
    hosts, parts, names, logins, avatars = _parse_participants(
        {"host": ["h1"], "participant": ["p1", "p2"]})
    assert hosts == ["h1"] and parts == ["p1", "p2"]
    assert names == {} and logins == {} and avatars == {}


def test_participants_format_historique_uuid_nu():
    hosts, parts, *_ = _parse_participants(["uuid-1", "uuid-2"])
    assert hosts == [] and parts == ["uuid-1", "uuid-2"]


def test_participants_entree_sans_id_est_rejetee():
    """Sans identifiant, rien ne peut être rattaché à la personne."""
    hosts, parts, names, *_ = _parse_participants([
        {"streamer_name": "Sans id", "role": "host"},
    ])
    assert hosts == [] and parts == [] and names == {}


def test_participants_entree_illisible_ne_casse_pas():
    assert _parse_participants(None) == ([], [], {}, {}, {})
    assert _parse_participants("") == ([], [], {}, {}, {})


def test_noter_participant_ecarte_un_login_invalide():
    logins = {}
    sid = _noter_participant(
        {"streamer_id": "s1", "socials": {"twitch": {"login": "in valide"}}},
        {}, logins, {})
    assert sid == "s1"
    assert logins == {}


def test_classer_entree_range_selon_le_role():
    hosts, parts = [], []
    _classer_entree({"streamer_id": "a", "role": "HOST"}, hosts, parts, {}, {}, {})
    _classer_entree({"streamer_id": "b", "role": "autre"}, hosts, parts, {}, {}, {})
    _classer_entree({"streamer_id": "c"}, hosts, parts, {}, {}, {})
    assert hosts == ["a"]
    assert parts == ["b", "c"]


# ── Ce qu'il reste à réunir pour qu'un objectif tombe ───────────────────────

def _objectif(cible: float, pct: float) -> "GoalWithStreamer":
    from core.api_client import GoalWithStreamer

    return GoalWithStreamer(streamer_login="a", streamer_display="A",
                            goal_name="n", amount_target=cible,
                            accomplished=False, pct=pct)


@pytest.mark.parametrize("cible,pct,attendu", [
    (1000.0, 96.0, 40.0),
    (200.0, 90.0, 20.0),
    (800.0, 99.0, 8.0),
])
def test_le_reste_se_deduit_du_pourcentage(cible, pct, attendu):
    """`pct` vaut donation / cible × 100 : la cagnotte s'en retrouve exactement.

    Un champ de plus serait un second endroit où la même vérité pourrait
    diverger, et il faudrait le renseigner sur chaque site de construction —
    l'API, la maquette, et chaque test.
    """
    assert _objectif(cible, pct).reste == pytest.approx(attendu)


def test_un_objectif_atteint_ne_manque_de_rien():
    """Le pourcentage est plafonné à 100 : zéro est la bonne réponse."""
    assert _objectif(500.0, 100.0).reste == 0.0


def test_le_reste_ne_devient_jamais_negatif():
    """Une cagnotte au-delà de la cible ne doit pas rendre « −40 € »."""
    assert _objectif(500.0, 108.0).reste == 0.0


def test_le_reste_ne_souffre_pas_du_flottant():
    """« 100 × (1 − 93/100) » vaut 6,999999999999995 : tronqué, il dirait 6 €.

    Annoncer moins que le nécessaire est le pire des arrondis : on donne la
    somme affichée et l'objectif ne tombe pas.
    """
    assert _objectif(100.0, 93.0).reste == 7.0


# ── Le pourcentage affiché ──────────────────────────────────────────────────

@pytest.mark.parametrize("cible,pct,attendu", [
    (1000.0, 99.6, 99),     # arrondi, il disait « 100% » — 4 € manquaient
    (5000.0, 99.95, 99),    # 2,50 € manquaient
    (300.0, 99.5, 99),      # 1,50 € manquait
    (100.0, 93.0, 93),
])
def test_cent_pour_cent_est_reserve_a_ce_qui_est_atteint(cible, pct, attendu):
    """Un pourcentage qui annonce la fin avant la fin est pire qu'imprécis.

    Trois objectifs de suite affichaient « 100% » sans jamais tomber : ils
    avaient l'air bloqués, et la seule chose qui manquait était le chiffre.
    """
    assert _objectif(cible, pct).pourcent_affiche == attendu


def test_un_objectif_reellement_atteint_affiche_bien_cent():
    assert _objectif(500.0, 100.0).pourcent_affiche == 100
