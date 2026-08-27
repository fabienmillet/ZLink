# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Mode mock : les données simulées qui remplacent les API hors ZEvent.

`python main.py --mock` est le seul moyen de voir le panel vivre les 362 jours
où l'event n'a pas lieu : c'est donc lui qui sert à valider les animations, le
bandeau des objectifs proches et le bouton « donner ». Un mock qui ment fait
perdre des heures à débugger un bug qui n'existe pas, et un mock qui n'émet pas
exactement ce qu'émet DataManager laisse passer un vrai bug jusqu'au jour J.

Ces tests verrouillent donc le contrat : mêmes signaux et mêmes arités que
DataManager, URL de don sur un hôte que l'allowlist accepte, et la même règle
des 90-100 % pour les objectifs proches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from core.api_client import (
    _DONATION_HOSTS,
    DonationGoal,
    StreamerInfo,
    _safe_https_url,
)
from core.mock_injector import (
    _MOCK_GOAL_PALIERS,
    _MOCK_GOALS,
    _MOCK_STREAMERS_SEED,
    _PALIERS_DON,
    MockInjector,
)

_UTC2 = timezone(timedelta(hours=2))


class _DataManagerFactice(QObject):
    """Sosie de DataManager réduit aux signaux que MockInjector rejoue.

    Instancier le vrai DataManager démarrerait ses timers réseau ; seules ses
    signatures de signaux comptent ici, et un test les compare aux vraies.
    """

    streamers_updated = pyqtSignal(list)
    global_stats_updated = pyqtSignal(object)
    events_updated = pyqtSignal(list)
    goals_updated = pyqtSignal(list)
    goals_raw_updated = pyqtSignal(dict)
    goal_accomplished = pyqtSignal(str, str)
    goal_imminent = pyqtSignal(str, str, str, float, str)
    history_updated = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        # Un VRAI HistoryStore : c'est lui que l'injecteur doit alimenter, et
        # une doublure ne dirait rien de la vitesse ni de la projection.
        from core.history_store import HistoryStore
        self._history = HistoryStore()


@pytest.fixture(scope="module")
def boucle_qt():
    """Les QTimer de MockInjector n'existent pas sans application Qt.

    QCoreApplication suffit : l'injecteur n'a aucun widget, et une QApplication
    réclamerait la couche graphique.
    """
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def dm():
    return _DataManagerFactice()


@pytest.fixture
def injecteur(boucle_qt, dm):
    inj = MockInjector(dm)
    yield inj
    inj.stop()


def _ecouter(dm) -> dict[str, list]:
    """Branche un espion sur chaque signal et retourne ce qu'il a reçu."""
    recu: dict[str, list] = {
        "streamers": [], "stats": [], "events": [],
        "goals": [], "goals_raw": [], "accomplis": [], "imminents": [],
    }
    dm.streamers_updated.connect(lambda v: recu["streamers"].append(v))
    dm.global_stats_updated.connect(lambda v: recu["stats"].append(v))
    dm.events_updated.connect(lambda v: recu["events"].append(v))
    dm.goals_updated.connect(lambda v: recu["goals"].append(v))
    dm.goals_raw_updated.connect(lambda v: recu["goals_raw"].append(v))
    dm.goal_accomplished.connect(lambda *a: recu["accomplis"].append(a))
    dm.goal_imminent.connect(lambda *a: recu["imminents"].append(a))
    return recu


def _streamer(login: str = "zerator", donation: float = 1_000.0,
              online: bool = True) -> StreamerInfo:
    return StreamerInfo(
        twitch_login=login, display=login.upper(), online=online,
        game="Minecraft", location="LAN", viewers=1_000, donation=donation,
        donation_formatted="", profile_url="",
        donation_url=f"https://zevent.fr/dons?streamer={login}",
    )


def _objectif(amount: float, accomplished: bool = False,
              nom: str = "Un objectif") -> DonationGoal:
    return DonationGoal(id=f"g-{amount}", name=nom, amount=amount,
                        accomplished=accomplished, category="mock", links=[])


def _seul_en_direct(injecteur, index: int = 0) -> StreamerInfo:
    """Ne laisse qu'un streamer en direct, pour rendre les tirages prévisibles."""
    for i, s in enumerate(injecteur._streamers):
        s.online = (i == index)
    return injecteur._streamers[index]


# ── contrat avec DataManager ─────────────────────────────────────────────────

def _types_du_signal(signal) -> tuple[str, ...]:
    """Types déclarés par un signal, sans son nom.

    `pyqtSignal.signatures` préfixe le nom du signal dès que Qt a construit le
    méta-objet de la classe — c'est-à-dire selon qu'un autre test a déjà
    instancié DataManager ou non. Comparer les chaînes brutes rendrait ce test
    dépendant de l'ordre d'exécution de la suite.
    """
    return tuple(s.split("(")[-1].rstrip(")") for s in signal.signatures)


def test_le_sosie_declare_les_memes_signaux_que_le_vrai_datamanager():
    """MockInjector émet sur un vrai DataManager en production.

    Si un signal change d'arité côté DataManager sans changer ici, le mock
    lèverait au premier tick — mais seulement en mode mock, donc jamais en CI.
    Ce test fait échouer la suite à la place.
    """
    from core.data_manager import DataManager

    for nom in ("streamers_updated", "global_stats_updated", "events_updated",
                "goals_updated", "goals_raw_updated", "goal_accomplished",
                "goal_imminent"):
        vrai = _types_du_signal(getattr(DataManager, nom))
        faux = _types_du_signal(getattr(_DataManagerFactice, nom))
        assert faux == vrai, f"{nom} a dérivé"


# ── données de départ ────────────────────────────────────────────────────────

def test_le_panel_demarre_avec_du_monde_dont_des_hors_ligne(injecteur):
    """Deux streamers hors ligne au départ : sans eux, les cartes « offline »
    et le filtre « en direct » ne se voient jamais en mode mock."""
    streamers = injecteur.streamers
    assert len(streamers) == len(_MOCK_STREAMERS_SEED)
    hors_ligne = [s for s in streamers if not s.online]
    assert len(hors_ligne) == 2
    assert all(s.viewers == 0 for s in hors_ligne)


def test_la_liste_des_streamers_est_une_copie(injecteur):
    """L'UI trie et filtre la liste reçue ; qu'elle le fasse sur l'état interne
    de l'injecteur créerait des bugs impossibles à reproduire."""
    dehors = injecteur.streamers
    dehors.clear()
    assert len(injecteur.streamers) == len(_MOCK_STREAMERS_SEED)


def test_chaque_cagnotte_arrive_deja_formatee(injecteur):
    """Les cartes affichent `donation_formatted` tel quel : un champ vide
    afficherait un blanc à la place du montant."""
    for s in injecteur.streamers:
        assert s.donation_formatted == MockInjector._fmt(s.donation)


def test_chaque_url_de_don_passe_l_allowlist(injecteur):
    """`_safe_https_url` rejette tout ce qui n'est pas https sur zevent.fr.

    Une URL de mock hors allowlist rendrait le bouton « donner » inerte, et on
    croirait le bouton cassé alors que seule la donnée l'est.
    """
    for s in injecteur.streamers:
        assert _safe_https_url(s.donation_url, _DONATION_HOSTS) == s.donation_url


def test_le_mode_site_est_force_en_live(injecteur):
    """Toutes les animations du panel sont conditionnées à website_mode ==
    "live" : c'est précisément ce qu'on vient voir en mode mock."""
    assert injecteur._stats.website_mode == "live"


def test_les_totaux_globaux_sont_la_somme_des_streamers(injecteur):
    stats = injecteur._stats
    assert stats.donation_total == pytest.approx(
        sum(s.donation for s in injecteur.streamers))
    assert stats.viewers_total == sum(s.viewers for s in injecteur.streamers)
    assert stats.donation_formatted == MockInjector._fmt(stats.donation_total)


# ── mise en forme des montants ───────────────────────────────────────────────

@pytest.mark.parametrize("montant,attendu", [
    (0, "0 €"),
    (999, "999 €"),
    (1_000, "1 000 €"),
    (1_234_567, "1 234 567 €"),
    (1_234_567.9, "1 234 567 €"),   # tronqué, jamais arrondi
])
def test_les_montants_utilisent_l_espace_insecable(montant, attendu):
    """L'espace insécable évite qu'un montant se coupe en fin de ligne dans les
    bandeaux ; une espace ordinaire passerait inaperçue ici mais pas à l'écran."""
    assert MockInjector._fmt(montant) == attendu


# ── cache d'objectifs ────────────────────────────────────────────────────────

def test_trois_objectifs_par_streamer_avec_des_identifiants_uniques(injecteur):
    """L'onglet Goals indexe par id : deux objectifs homonymes s'écraseraient."""
    cache = injecteur._goals_cache
    assert set(cache) == {s.twitch_login for s in injecteur.streamers}
    assert all(len(v) == len(_MOCK_GOAL_PALIERS) for v in cache.values())
    ids = [g.id for objectifs in cache.values() for g in objectifs]
    assert len(ids) == len(set(ids))


def test_les_trois_objectifs_d_un_streamer_portent_des_noms_differents(injecteur):
    """Trois lignes identiques dans l'onglet Goals ressembleraient à un bug
    d'affichage : les intitulés sont piochés en séquence, pas au hasard."""
    for objectifs in injecteur._goals_cache.values():
        noms = [g.name for g in objectifs]
        assert len(set(noms)) == len(noms)


def test_les_montants_montent_avec_les_paliers(injecteur):
    """Un objectif accompli, un tout proche, un lointain : c'est ce trio qui
    fait vivre l'onglet sans attendre un vrai ZEvent."""
    for streamer in injecteur.streamers:
        objectifs = injecteur._goals_cache[streamer.twitch_login]
        montants = [g.amount for g in objectifs]
        assert montants == sorted(montants)
        assert [g.accomplished for g in objectifs] == \
            [accompli for _, accompli in _MOCK_GOAL_PALIERS]


def test_le_palier_intermediaire_tombe_dans_la_fenetre_imminente(injecteur):
    """Le ratio 1,05 vise ~95 % : c'est lui qui garantit qu'au démarrage le
    bandeau « objectifs proches » n'est pas vide."""
    proches = injecteur._goals_enrichis()
    assert len(proches) == len(injecteur.streamers)


def test_un_streamer_sans_cagnotte_garde_un_objectif_atteignable(injecteur):
    """Sans plancher, les trois objectifs vaudraient 0 € : tous « atteints »
    d'entrée, et une division par zéro dans le calcul du pourcentage."""
    injecteur._streamers = [_streamer(donation=0.0)]
    objectifs = injecteur._build_goals_cache()["zerator"]
    assert [g.amount for g in objectifs] == [1_000.0] * len(_MOCK_GOAL_PALIERS)


def test_chaque_objectif_pointe_vers_la_page_de_don_de_son_streamer(injecteur):
    """Le lien de l'objectif est ce que l'onglet Goals ouvre au clic."""
    for streamer in injecteur.streamers:
        for objectif in injecteur._goals_cache[streamer.twitch_login]:
            assert objectif.links == [streamer.donation_url]


# ── règle des objectifs proches ──────────────────────────────────────────────

@pytest.mark.parametrize("donation,montant,retenu", [
    (899.0, 1_000.0, False),    # 89,9 % — pas encore digne du bandeau
    (900.0, 1_000.0, True),     # 90 % — borne basse incluse
    (1_000.0, 1_000.0, True),   # 100 % — borne haute incluse
    (1_001.0, 1_000.0, False),  # 100,1 % — au-delà le proxy n'a plus de sens
    (5_000.0, 1_000.0, False),  # très dépassé : objectif sûrement obsolète
])
def test_la_fenetre_des_objectifs_proches_va_de_90_a_100(
        injecteur, donation, montant, retenu):
    """Même règle que DataManager, sinon le mode mock testerait autre chose que
    l'application. Le plafond est volontaire : au-dessus de 100 %, la cagnotte
    du streamer n'est plus un bon proxy de l'avancement de l'objectif."""
    injecteur._streamers = [_streamer(donation=donation)]
    injecteur._goals_cache = {"zerator": [_objectif(montant)]}
    assert bool(injecteur._goals_enrichis()) is retenu


def test_un_objectif_deja_accompli_ne_revient_pas_dans_le_bandeau(injecteur):
    injecteur._streamers = [_streamer(donation=950.0)]
    injecteur._goals_cache = {"zerator": [_objectif(1_000.0, accomplished=True)]}
    assert injecteur._goals_enrichis() == []


@pytest.mark.parametrize("montant", [0.0, -100.0])
def test_un_objectif_sans_montant_est_ignore_sans_division_par_zero(
        injecteur, montant):
    """L'API a déjà renvoyé des objectifs à 0 € ; le mock doit s'en protéger de
    la même façon que le vrai chemin."""
    injecteur._streamers = [_streamer(donation=950.0)]
    injecteur._goals_cache = {"zerator": [_objectif(montant)]}
    assert injecteur._goals_enrichis() == []


def test_les_objectifs_proches_sortent_du_plus_avance_au_moins_avance(injecteur):
    """Le bandeau ne montre que les premiers : l'ordre décide de ce qu'on voit."""
    injecteur._streamers = [_streamer("a", 910.0), _streamer("b", 990.0),
                            _streamer("c", 950.0)]
    injecteur._goals_cache = {
        login: [_objectif(1_000.0)] for login in ("a", "b", "c")}
    proches = injecteur._goals_enrichis()
    assert [g.streamer_login for g in proches] == ["b", "c", "a"]
    assert proches[0].pct == pytest.approx(99.0)
    assert proches[0].accomplished is False


# ── émissions ────────────────────────────────────────────────────────────────

def test_le_demarrage_envoie_un_lot_complet_et_lance_les_timers(injecteur, dm):
    """Sans ce premier lot, le panel resterait vide jusqu'au premier tick —
    trois secondes plus tard, et quarante-cinq pour le bandeau imminent."""
    recu = _ecouter(dm)
    injecteur.start()

    assert len(recu["events"]) == 1 and recu["events"][0]
    assert len(recu["streamers"]) == 1
    assert len(recu["stats"]) == 1
    assert len(recu["goals_raw"]) == 1
    assert len(recu["goals"]) == 1
    for timer in (injecteur._t_donation, injecteur._t_viewers,
                  injecteur._t_online, injecteur._t_goals,
                  injecteur._t_imminent):
        assert timer.isActive()


def test_l_arret_coupe_tous_les_timers(injecteur):
    """Un timer oublié continuerait d'injecter des dons fictifs par-dessus les
    vraies données."""
    injecteur.start()
    injecteur.stop()
    assert not any(t.isActive() for t in (
        injecteur._t_donation, injecteur._t_viewers, injecteur._t_online,
        injecteur._t_goals, injecteur._t_imminent))


def test_le_cache_brut_emis_est_une_copie(injecteur, dm):
    """L'onglet Goals reçoit ce dictionnaire à chaque batch ; s'il le modifie,
    ce n'est pas l'état de l'injecteur qui doit bouger."""
    recu = _ecouter(dm)
    injecteur._emit_goals()
    recu["goals_raw"][0].clear()
    assert len(injecteur._goals_cache) == len(_MOCK_STREAMERS_SEED)


# ── dons ─────────────────────────────────────────────────────────────────────

def test_un_don_ne_tombe_que_sur_un_streamer_en_direct(injecteur):
    """Voir la cagnotte d'un streamer hors ligne grimper serait un faux signal
    difficile à distinguer d'un vrai bug de fusion des sources."""
    cible = _seul_en_direct(injecteur, index=3)
    avant = {s.twitch_login: s.donation for s in injecteur._streamers}
    injecteur._tick_donation()
    for s in injecteur._streamers:
        if s is cible:
            assert s.donation > avant[s.twitch_login]
        else:
            assert s.donation == avant[s.twitch_login]


def test_aucun_don_quand_personne_n_est_en_direct(injecteur, dm):
    """Cas atteint dès que le tick « online » a fait tomber tout le monde :
    `random.choice` sur une liste vide lèverait IndexError."""
    for s in injecteur._streamers:
        s.online = False
    recu = _ecouter(dm)
    injecteur._tick_donation()
    assert recu["streamers"] == []


def test_un_don_met_a_jour_le_total_et_le_texte_formate(injecteur):
    cible = _seul_en_direct(injecteur, index=0)
    injecteur._tick_donation()
    assert cible.donation_formatted == MockInjector._fmt(cible.donation)
    assert injecteur._stats.donation_total == pytest.approx(
        sum(s.donation for s in injecteur._streamers))
    assert injecteur._stats.donation_formatted == MockInjector._fmt(
        injecteur._stats.donation_total)


def test_un_seul_palier_est_tire_par_don(injecteur, monkeypatch):
    """La version précédente tirait un montant dans CHACUN des quatre paliers
    pour n'en garder qu'un : trois tirages jetés toutes les trois secondes.

    On vérifie donc que les poids partent en une seule fois à `random.choices`,
    et que le montant est tiré dans le palier retenu.
    """
    appels: list[tuple] = []

    def faux_choices(population, weights):
        appels.append((list(population), list(weights)))
        return [population[0]]

    monkeypatch.setattr("core.mock_injector.random.choices", faux_choices)
    monkeypatch.setattr("core.mock_injector.random.randint",
                        lambda bas, haut: bas)
    cible = _seul_en_direct(injecteur, index=0)
    avant = cible.donation

    injecteur._tick_donation()

    assert len(appels) == 1
    population, poids = appels[0]
    assert population == [bornes for bornes, _ in _PALIERS_DON]
    assert poids == [poids_palier for _, poids_palier in _PALIERS_DON]
    assert cible.donation - avant == _PALIERS_DON[0][0][0]


def test_le_gros_don_reste_le_plus_rare():
    """Les paliers montent en montant et descendent en probabilité : c'est ce
    qui rend le mode mock crédible plutôt que d'inonder l'écran d'alertes."""
    montants = [bornes for bornes, _ in _PALIERS_DON]
    poids = [p for _, p in _PALIERS_DON]
    assert montants == sorted(montants)
    assert poids == sorted(poids, reverse=True)


# ── viewers et passages en ligne ─────────────────────────────────────────────

def test_les_viewers_ne_bougent_que_pour_les_streamers_en_direct(injecteur):
    """Un streamer hors ligne affiché avec des viewers ferait douter du statut
    live, l'information la plus regardée du panel."""
    cible = _seul_en_direct(injecteur, index=0)
    avant = {s.twitch_login: s.viewers for s in injecteur._streamers}
    injecteur._tick_viewers()
    assert all(s.viewers == avant[s.twitch_login]
               for s in injecteur._streamers if s is not cible)
    # Le total global ne compte que le direct : un hors-ligne resté à sa
    # dernière valeur ne doit pas gonfler l'audience affichée en tête de panel.
    assert injecteur._stats.viewers_total == cible.viewers


def test_les_viewers_ne_descendent_jamais_sous_le_plancher(injecteur):
    """Un compteur à zéro sur un streamer en direct se lirait comme « hors
    ligne » : le plancher à 100 évite cette confusion."""
    for s in injecteur._streamers:
        s.viewers = 100
    for _ in range(20):
        injecteur._tick_viewers()
    assert all(s.viewers >= 100 for s in injecteur._streamers if s.online)


def test_passer_hors_ligne_remet_les_viewers_a_zero(injecteur, monkeypatch):
    cible = injecteur._streamers[0]
    cible.online = True
    monkeypatch.setattr("core.mock_injector.random.choice", lambda seq: cible)
    injecteur._tick_online()
    assert cible.online is False
    assert cible.viewers == 0


def test_un_retour_en_ligne_redonne_une_audience_plausible(injecteur, monkeypatch):
    """Revenir avec zéro viewer laisserait la carte incohérente : en direct,
    mais devant personne."""
    cible = injecteur._streamers[0]
    cible.online = False
    cible.viewers = 0
    monkeypatch.setattr("core.mock_injector.random.choice", lambda seq: cible)
    monkeypatch.setattr("core.mock_injector.random.uniform",
                        lambda bas, haut: 0.5)
    injecteur._tick_online()
    seed_viewers = _MOCK_STREAMERS_SEED[0][4]
    assert cible.online is True
    assert cible.viewers == int(seed_viewers * 0.5)
    assert injecteur._stats.viewers_total == sum(
        s.viewers for s in injecteur._streamers if s.online)


# ── objectifs imminents ──────────────────────────────────────────────────────

def test_l_alerte_imminente_porte_l_url_de_don(injecteur, dm):
    """C'est la seule alerte dont le bouton ouvre le navigateur : sans URL
    valide, ce chemin ne se teste qu'un vrai jour de ZEvent."""
    recu = _ecouter(dm)
    cible = _seul_en_direct(injecteur, index=0)
    injecteur._tick_imminent()

    assert len(recu["imminents"]) == 1
    login, display, nom, reste, url = recu["imminents"][0]
    assert login == cible.twitch_login
    assert display == cible.display
    assert nom
    assert reste > 0
    assert url == cible.donation_url
    assert _safe_https_url(url, _DONATION_HOSTS) == url


def test_l_objectif_annonce_est_le_plus_proche_de_tomber(injecteur, dm):
    """Annoncer un objectif lointain comme « imminent » userait l'alerte."""
    recu = _ecouter(dm)
    cible = _seul_en_direct(injecteur, index=0)
    injecteur._goals_cache[cible.twitch_login] = [
        _objectif(cible.donation + 5_000, nom="lointain"),
        _objectif(cible.donation + 100, nom="le plus proche"),
        _objectif(cible.donation + 900, nom="entre les deux"),
    ]
    injecteur._tick_imminent()
    _, _, nom, reste, _ = recu["imminents"][0]
    assert nom == "le plus proche"
    assert reste == pytest.approx(100.0)


def test_un_objectif_deja_franchi_n_est_pas_annonce(injecteur, dm):
    """Le reste à parcourir serait négatif : « plus que -300 € » n'a aucun sens."""
    recu = _ecouter(dm)
    cible = _seul_en_direct(injecteur, index=0)
    injecteur._goals_cache[cible.twitch_login] = [
        _objectif(cible.donation - 300),
        _objectif(cible.donation + 400, accomplished=True),
    ]
    injecteur._tick_imminent()
    assert recu["imminents"] == []


def test_aucune_alerte_sans_streamer_en_direct(injecteur, dm):
    recu = _ecouter(dm)
    for s in injecteur._streamers:
        s.online = False
    injecteur._tick_imminent()
    assert recu["imminents"] == []


def test_les_alertes_tournent_d_un_streamer_a_l_autre(injecteur, dm):
    """Sans rotation, le mode mock alerterait toujours sur ZeratoR et on ne
    verrait jamais le bandeau changer de nom."""
    recu = _ecouter(dm)
    for _ in range(3):
        injecteur._tick_imminent()
    logins = [a[0] for a in recu["imminents"]]
    assert len(logins) == 3
    assert len(set(logins)) == 3


# ── objectifs accomplis ──────────────────────────────────────────────────────

def test_les_objectifs_accomplis_defilent_puis_recommencent(injecteur, dm):
    """Le flash de la grille se déclenche sur ce signal ; il doit pouvoir se
    rejouer indéfiniment sans sortir de la liste."""
    recu = _ecouter(dm)
    for _ in range(len(_MOCK_GOALS) + 2):
        injecteur._tick_goal()
    annonces = [(login, nom) for login, nom in recu["accomplis"]]
    assert annonces[:len(_MOCK_GOALS)] == list(_MOCK_GOALS)
    assert annonces[len(_MOCK_GOALS):] == list(_MOCK_GOALS[:2])


def test_une_liste_d_objectifs_vide_ne_fait_pas_tomber_le_timer(
        injecteur, dm, monkeypatch):
    """Le tick calcule un modulo sur la longueur de la liste : la vider —
    en la retaillant pour une démo, par exemple — lèverait toutes les 90 s
    dans un timer, donc loin de l'appel fautif."""
    recu = _ecouter(dm)
    monkeypatch.setattr("core.mock_injector._MOCK_GOALS", [])
    injecteur._tick_goal()
    assert recu["accomplis"] == []


def test_les_objectifs_accomplis_visent_des_streamers_existants():
    """Le flash cherche la carte du login annoncé : un login absent de la liste
    ne ferait rien clignoter, et le test du flash serait aveugle."""
    logins_connus = {seed[0] for seed in _MOCK_STREAMERS_SEED}
    assert {login for login, _ in _MOCK_GOALS} <= logins_connus


# ── événements de la timeline ────────────────────────────────────────────────

def test_un_evenement_est_toujours_en_cours_maintenant():
    """La timeline place un curseur sur l'instant présent : sans événement
    couvrant maintenant, on ne verrait jamais l'état « en cours »."""
    maintenant = datetime.now(tz=_UTC2).timestamp()
    events = MockInjector._build_mock_events()
    assert any(e.start_ts <= maintenant <= e.end_ts for e in events)


def test_les_evenements_couvrent_plusieurs_jours():
    """L'onglet Programme se navigue par jour : un seul jour peuplé ne
    montrerait pas que la navigation fonctionne."""
    events = MockInjector._build_mock_events()
    assert len({e.day for e in events}) >= 3


def test_chaque_evenement_finit_apres_avoir_commence():
    events = MockInjector._build_mock_events()
    assert all(e.end_ts > e.start_ts for e in events)


def test_les_horaires_affiches_correspondent_aux_timestamps():
    """La timeline positionne les blocs avec les timestamps mais écrit les
    heures : un décalage entre les deux passerait pour un bug de rendu."""
    for e in MockInjector._build_mock_events():
        debut = datetime.fromtimestamp(e.start_ts, tz=_UTC2)
        fin = datetime.fromtimestamp(e.end_ts, tz=_UTC2)
        assert debut.strftime("%H:%M") == e.start_local
        assert fin.strftime("%H:%M") == e.end_local


def test_les_identifiants_d_evenements_sont_uniques():
    """Les identifiants sont tronqués à huit caractères du nom : deux shows aux
    noms voisins partageraient la même carte."""
    ids = [e.id for e in MockInjector._build_mock_events()]
    assert len(ids) == len(set(ids))


def test_les_hotes_des_evenements_sont_des_streamers_du_mock():
    """L'avatar de l'hôte est cherché dans la liste des streamers : un hôte
    inconnu afficherait un trou dans la carte de l'événement."""
    logins_connus = {seed[0] for seed in _MOCK_STREAMERS_SEED}
    for e in MockInjector._build_mock_events():
        assert set(e.host_uuids) <= logins_connus


# ── historique ───────────────────────────────────────────────────────────────

def test_le_mock_alimente_l_historique(injecteur):
    """Le mock rejoue les signaux un par un, sans passer par
    `_on_data_updated` — c'est ce qui lui évite tout réseau. Mais cette
    méthode faisait DEUX choses de plus qu'émettre : ranger le point dans
    l'historique, et annoncer `history_updated`.

    Sans elles, l'historique restait vide : pas de courbes, pas de vitesse, et
    l'Accueil affichait « disponible au début de l'event » pendant que des
    dons tombaient à chaque seconde.
    """
    injecteur.start()
    try:
        ts, vals = injecteur._dm._history.get_donation_series()
        assert len(ts) > 2, "l'historique doit être amorcé"
        assert vals[-1] > 0
    finally:
        injecteur.stop()


def test_le_mock_amorce_assez_de_passe_pour_une_vitesse(injecteur):
    """`donation_rate` exige deux relevés espacés de six minutes : sans passé,
    la vitesse restait muette tout ce temps devant l'écran."""
    injecteur.start()
    try:
        assert injecteur._dm._history.donation_rate() is not None
    finally:
        injecteur.stop()


def test_la_projection_du_mock_reste_un_ordre_de_grandeur(injecteur):
    """Une rampe de zéro au total en une heure donnait 77 000 €/min et une
    projection à 453 MILLIONS — l'absurdité même que le garde-fou évitait.

    L'amorce suit donc la pente MOYENNE d'une édition.
    """
    injecteur.start()
    try:
        histoire = injecteur._dm._history
        projete = histoire.projected_total(histoire.event_end_ts)
        assert projete is not None
        acquis = histoire.get_donation_series()[1][-1]
        assert acquis < projete < acquis * 5, (
            f"projection invraisemblable : {projete:.0f} pour {acquis:.0f} acquis")
    finally:
        injecteur.stop()


def test_l_audience_amorcee_ne_part_pas_de_zero(injecteur):
    """Elle oscille autour de son niveau, elle ne monte pas depuis rien."""
    injecteur.start()
    try:
        _ts, vues = injecteur._dm._history.get_viewers_series()
        assert min(vues) == max(vues) > 0
    finally:
        injecteur.stop()


def test_le_mock_date_ses_releves_dans_le_calendrier_de_l_evenement(injecteur):
    """Le graphe portait des abscisses du jour où l'on lance ZLink.

    À sept jours de l'événement, il annonçait fin août en regard des courbes
    de 2025, qui commencent un vendredi soir de septembre.
    """
    from core.history_store import OUVERTURE_CAGNOTTE, _EVENT_END

    injecteur.start()
    try:
        ts, _vals = injecteur._dm._history.get_donation_series()
        assert ts[0] >= OUVERTURE_CAGNOTTE
        assert ts[-1] <= _EVENT_END
    finally:
        injecteur.stop()


def test_les_releves_du_mock_restent_chronologiques(injecteur):
    """Les relevés amorcés étaient datés en septembre, ceux pris en direct
    à la date du jour : le deque partait dans le désordre, et la vitesse
    comparait deux points séparés d'un écart NÉGATIF."""
    injecteur.start()
    try:
        injecteur._alimenter_historique()      # un relevé « en direct » de plus
        histoire = injecteur._dm._history
        ts, _vals = histoire.get_donation_series()
        assert ts == sorted(ts), "le deque doit rester chronologique"
        # Sur l'heure écoulée, l'amorce et le direct se suivent : la pente est
        # mesurable. Sur les quinze dernières minutes, deux relevés pris coup
        # sur coup n'ont rien à comparer — et c'est très bien ainsi.
        assert histoire.donation_rate(3600.0) is not None
    finally:
        injecteur.stop()
