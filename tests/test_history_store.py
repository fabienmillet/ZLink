# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Historique des séries cagnotte et spectateurs.

Ce module alimente des courbes Qt et une projection affichée à l'écran. Deux
familles de risques justifient ces tests :

- des données venues du réseau (dépôt GitHub tiers, format non contractuel)
  atterrissent dans `datetime.fromtimestamp()` au fond d'un slot Qt sans
  try/except : un timestamp aberrant fait tomber le panel ;
- la vitesse de collecte et la comparaison avec l'édition précédente mélangent
  deux séries de dates différentes ; une erreur d'alignement passe inaperçue à
  la lecture mais affiche des chiffres faux pendant tout l'event.

Aucun test ne sort sur le réseau : `httpx.AsyncClient` est remplacé par un faux
client. Le module n'écrit rien sur disque, il n'y a donc aucun chemin à
détourner vers `tmp_path`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core import history_store
from core.history_store import (
    DEBUT_COURSE,
    OUVERTURE_CAGNOTTE,
    _EVENT_END,
    _EVENT_START,
    _TS_MAX,
    _TS_MIN,
    HistoryStore,
    _sane_point,
)

# Un instant confortablement à l'intérieur de l'édition, pour éviter que le
# moindre décalage de test ne sorte de la fenêtre par accident.
DEBUT = _EVENT_START + 3600.0


# ── outils ───────────────────────────────────────────────────────────────────

@pytest.fixture
def horloge(monkeypatch):
    """Fige `time.time()` vu par le module et permet de le faire avancer.

    `add_point` horodate lui-même : sans horloge maîtrisée, tous les points
    tomberaient à la date d'exécution des tests — donc hors de la fenêtre de
    l'édition, et toutes les séries reviendraient vides.
    """
    etat = {"t": DEBUT}
    monkeypatch.setattr("core.history_store.time.time", lambda: etat["t"])
    return etat


@pytest.fixture
def depot_distant(monkeypatch):
    """Remplace `httpx.AsyncClient` par un faux client hors ligne.

    `load_historical_2026` fait `import httpx` dans son corps : la substitution
    au niveau du module httpx est donc bien vue à l'appel. Renvoie la liste des
    URL demandées, qui sert aussi de preuve qu'aucun vrai appel n'a lieu.
    """
    appels: list[str] = []

    def installer(payload=None, erreur=None):
        class _Reponse:
            def raise_for_status(self):
                if erreur is not None:
                    raise erreur

            def json(self):
                return payload

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                appels.append(url)
                return _Reponse()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        return appels

    return installer


def _payload(pool_labels, pool_values, view_labels, view_values):
    """Forme minimale du JSON du dépôt tiers."""
    return {
        "pools": {"large": {"labels": pool_labels, "values": pool_values}},
        "viewers": {"large": {"labels": view_labels, "values": view_values}},
    }


def _charger(store):
    """Exécute la coroutine de chargement (pas de greffon asyncio ici)."""
    asyncio.run(store.load_historical_2026())


def _remplir(store, horloge, points):
    """Ajoute des points (offset en secondes depuis DEBUT, cagnotte, viewers)."""
    for offset, don, viewers in points:
        horloge["t"] = DEBUT + offset
        store.add_point(don, viewers)


# ── validation des points venus du dépôt tiers ───────────────────────────────

@pytest.mark.parametrize("ts_ms,val,attendu", [
    (1_788_400_000_000, 12345, (1_788_400_000.0, 12345.0)),
    (1_788_400_000_000.0, 1.5, (1_788_400_000.0, 1.5)),
    (0.0 + _TS_MIN * 1000, 0, (_TS_MIN, 0.0)),      # borne basse incluse
    (0.0 + _TS_MAX * 1000, 0, (_TS_MAX, 0.0)),      # borne haute incluse
])
def test_un_point_numerique_dans_la_plage_est_accepte(ts_ms, val, attendu):
    assert _sane_point(ts_ms, val) == attendu


@pytest.mark.parametrize("ts_ms,val", [
    ("1788400000000", 1),          # chaîne : JSON mal typé
    (1_788_400_000_000, "abc"),
    (None, 1),
    (1_788_400_000_000, None),
    ([], {}),
    (1_788_400_000_000, [1]),
])
def test_un_point_non_numerique_est_ecarte(ts_ms, val):
    """Le dépôt tiers n'a aucun contrat de format.

    Une valeur textuelle remonterait jusqu'au tracé de la courbe et ferait
    tomber le panel : elle doit mourir ici.
    """
    assert _sane_point(ts_ms, val) is None


@pytest.mark.parametrize("ts_ms,val", [
    (True, 1),
    (1_788_400_000_000, True),
    (False, 0),
])
def test_un_booleen_n_est_pas_un_nombre(ts_ms, val):
    """`isinstance(True, int)` est vrai en Python.

    Sans le rejet explicite des booléens, un `true` JSON deviendrait le
    timestamp 0,001 s — un point de 1970 au milieu de la courbe.
    """
    assert _sane_point(ts_ms, val) is None


@pytest.mark.parametrize("ts_ms", [
    0,                              # epoch
    -1_788_400_000_000,             # négatif
    (_TS_MIN - 1) * 1000,           # juste avant 2020
    (_TS_MAX + 1) * 1000,           # juste après 2035
    1_788_400_000,                  # secondes prises pour des millisecondes
    1_788_400_000_000_000,          # microsecondes prises pour des ms
])
def test_un_timestamp_hors_plage_est_ecarte(ts_ms):
    """Erreur d'unité ou valeur folle : les deux donnent une date absurde.

    C'est le cas qui a motivé le garde-fou — `datetime.fromtimestamp()` lève
    sur les très grandes valeurs, dans un slot Qt sans try/except.
    """
    assert _sane_point(ts_ms, 1000) is None


# ── ajout de points et lecture des séries ────────────────────────────────────

def test_les_series_partent_vides():
    store = HistoryStore()
    assert store.get_donation_series() == ([], [])
    assert store.get_viewers_series() == ([], [])


def test_les_points_ressortent_dans_l_ordre_chronologique(horloge):
    """Les courbes Qt tracent dans l'ordre du tableau, sans le retrier.

    Un ordre cassé dessinerait un aller-retour dans la courbe.
    """
    store = HistoryStore()
    _remplir(store, horloge, [(0, 1000.0, 10), (30, 1500.0, 20), (60, 1800.0, 15)])

    ts, vals = store.get_donation_series()
    assert ts == [DEBUT, DEBUT + 30, DEBUT + 60]
    assert vals == [1000.0, 1500.0, 1800.0]

    v_ts, v_vals = store.get_viewers_series()
    assert v_ts == ts
    assert v_vals == [10, 20, 15]


def test_les_points_hors_fenetre_event_sont_filtres(horloge, monkeypatch):
    """La fenêtre isole l'édition en cours de l'historique préchargé.

    Hors test, elle tranche seule : un relevé pris avant ou après l'édition
    n'en fait pas partie. En test, c'est la PROVENANCE qui départage — voir
    `test_debug_rend_les_points_pris_en_direct_hors_fenetre`.
    """
    monkeypatch.setattr(history_store, "DEBUG", False)
    store = HistoryStore()
    horloge["t"] = _EVENT_START - 1.0
    store.add_point(500.0, 5)
    horloge["t"] = _EVENT_END + 1.0
    store.add_point(900.0, 9)

    assert store.get_donation_series() == ([], [])
    assert store.get_viewers_series() == ([], [])


@pytest.mark.parametrize("instant", [_EVENT_START, _EVENT_END])
def test_les_bornes_de_la_fenetre_sont_incluses(horloge, instant):
    """Comparaisons larges des deux côtés : un point pile au coup d'envoi
    ou pile à la clôture compte."""
    store = HistoryStore()
    horloge["t"] = instant
    store.add_point(1234.0, 42)
    assert store.get_donation_series() == ([instant], [1234.0])
    assert store.get_viewers_series() == ([instant], [42])


def test_le_nombre_de_points_est_plafonne(horloge):
    """Rétention bornée : le panel tourne des jours, la mémoire ne doit pas
    croître sans fin. Ce sont les points les plus ANCIENS qui tombent."""
    store = HistoryStore(max_points=3)
    _remplir(store, horloge, [(i * 30, float(i), i) for i in range(5)])

    ts, vals = store.get_donation_series()
    assert vals == [2.0, 3.0, 4.0]
    assert ts == [DEBUT + 60, DEBUT + 90, DEBUT + 120]
    assert store.get_viewers_series()[1] == [2, 3, 4]


def test_hors_debug_et_hors_event_les_series_sont_muettes(horloge, monkeypatch):
    """DEBUG=False est le réglage de production : avant l'event, on n'affiche
    aucune courbe plutôt qu'une courbe vide mal cadrée."""
    monkeypatch.setattr(history_store, "DEBUG", False)
    store = HistoryStore()
    _remplir(store, horloge, [(0, 1000.0, 10), (30, 1100.0, 12)])
    horloge["t"] = _EVENT_START - 86400.0

    assert store.get_donation_series() == ([], [])
    assert store.get_viewers_series() == ([], [])


def test_debug_rend_les_points_pris_en_direct_hors_fenetre(horloge, monkeypatch):
    """Intention annoncée en tête de module : « en test, renvoie les données
    même hors fenêtre event ».

    Elle n'était pas tenue : le premier garde respectait DEBUG, le filtre
    point par point l'ignorait, et la série revenait vide quand même. Le mode
    mock restait donc sans courbe, sans vitesse et sans projection.
    """
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    horloge["t"] = _EVENT_START - 86400.0
    store.add_point(1000.0, 10)
    assert store.get_donation_series()[1] == [1000.0]
    assert store.get_viewers_series()[1] == [10]


def test_l_historique_precharge_reste_borne_a_sa_fenetre(horloge, monkeypatch):
    """Même en test, la courbe de l'édition PRÉCÉDENTE ne doit pas ressortir.

    C'est elle qui vit dans le même deque : la publier donnerait la vitesse
    de fin du ZEvent 2025 présentée comme celle en cours.
    """
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    store._donation.append((_EVENT_START - 365 * 86400.0, 9_000_000.0))
    store._viewers.append((_EVENT_START - 365 * 86400.0, 500_000))
    assert store.get_donation_series() == ([], [])
    assert store.get_viewers_series() == ([], [])


def test_un_prechargement_repart_a_zero_pour_le_direct(horloge, monkeypatch):
    """Le préchargement remplace tout : ce qui précède n'est plus du direct."""
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    horloge["t"] = _EVENT_START - 86400.0
    store.add_point(1000.0, 10)
    store._live_depuis = None          # ce que fait un rechargement
    store._donation.clear()
    store._donation.append((_EVENT_START - 86400.0, 1000.0))
    assert store.get_donation_series() == ([], [])


# ── vitesse de collecte ──────────────────────────────────────────────────────

def test_la_vitesse_demande_au_moins_deux_points(horloge):
    store = HistoryStore()
    assert store.donation_rate() is None
    _remplir(store, horloge, [(0, 1000.0, 10)])
    assert store.donation_rate() is None


def test_la_vitesse_est_en_euros_par_minute(horloge):
    store = HistoryStore()
    _remplir(store, horloge, [(0, 0.0, 10), (600, 6000.0, 10)])
    # 6000 € en 10 minutes.
    assert store.donation_rate(900.0) == pytest.approx(600.0)


def test_un_intervalle_trop_court_ne_donne_pas_de_vitesse(horloge):
    """La cagnotte publiée est arrondie ; sur vingt secondes, le bruit
    d'arrondi produirait une vitesse fantaisiste extrapolée sur trois jours."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 1000.0, 10), (20, 1010.0, 10)])
    assert store.donation_rate(900.0) is None
    assert store.donation_rate(100.0) is None   # plancher absolu de 30 s


def test_une_fenetre_trop_maigre_retombe_sur_les_deux_derniers_points(horloge):
    """Après une coupure de collecte, la fenêtre récente ne contient qu'un
    point ; plutôt que de ne rien afficher, on mesure sur les deux derniers."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 1000.0, 10), (1000, 2000.0, 10), (2000, 3000.0, 10)])
    # Fenêtre de 60 s : seul le dernier point y tombe.
    assert store.donation_rate(60.0) == pytest.approx(1000.0 / (1000.0 / 60.0))


def test_une_pente_negative_est_refusee(horloge):
    """Une cagnotte est cumulative.

    Une baisse ne peut venir que d'une correction de l'API ou du raccord entre
    deux séries ; l'extrapoler produirait une projection qui descend.
    """
    store = HistoryStore()
    _remplir(store, horloge, [(0, 6000.0, 10), (600, 5000.0, 10)])
    assert store.donation_rate(900.0) is None


def test_une_pente_nulle_reste_une_vitesse(horloge):
    """Zéro n'est pas une absence de mesure : c'est « rien ne rentre »."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 6000.0, 10), (600, 6000.0, 10)])
    assert store.donation_rate(900.0) == pytest.approx(0.0)


# ── projection ───────────────────────────────────────────────────────────────

def test_pas_de_projection_hors_de_l_edition(horloge, monkeypatch):
    """Extrapoler quatorze jours avant le coup d'envoi donnait des milliards."""
    monkeypatch.setattr(history_store, "DEBUG", False)
    store = HistoryStore()
    _remplir(store, horloge, [(0, 0.0, 10), (600, 6000.0, 10)])
    horloge["t"] = _EVENT_START - 1.0
    assert store.projected_total(_EVENT_END) is None


def test_en_test_la_projection_porte_sur_une_edition_simulee(horloge, monkeypatch):
    """Le garde-fou ci-dessus refermait la porte que DEBUG venait d'ouvrir.

    La série et la vitesse revenaient bien, mais la projection restait vide :
    l'Accueil affichait « disponible au début de l'event » pendant que le mode
    mock injectait des dons à chaque seconde.

    L'horizon devient la DURÉE d'une édition comptée depuis le premier relevé,
    et non le 7 septembre : projeter sur deux semaines redonnerait les
    milliards que le garde-fou évitait.
    """
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    depart = _EVENT_START - 14 * 86400.0
    horloge["t"] = depart
    store.add_point(0.0, 10)
    horloge["t"] = depart + 600.0
    store.add_point(6000.0, 10)

    projete = store.projected_total(_EVENT_END)
    assert projete is not None
    # L'horizon est la durée d'une édition depuis le PREMIER relevé, dont dix
    # minutes sont déjà écoulées quand on projette.
    reste_min = (_EVENT_END - _EVENT_START) / 60.0 - 10.0
    attendu = 6000.0 + store.donation_rate() * reste_min
    assert projete == pytest.approx(attendu, rel=0.001)
    # Et surtout : un ordre de grandeur d'événement, pas les milliards que le
    # garde-fou évitait en projetant jusqu'au 7 septembre.
    assert projete < 100_000_000.0


def test_sans_releve_en_direct_aucune_projection_hors_edition(horloge, monkeypatch):
    """Rien d'observé, rien à extrapoler — même en test."""
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    horloge["t"] = _EVENT_START - 1.0
    assert store.projected_total(_EVENT_END) is None


def test_pas_de_projection_sans_donnees(horloge):
    assert HistoryStore().projected_total(_EVENT_END) is None


def test_pas_de_projection_sans_vitesse_mesurable(horloge):
    store = HistoryStore()
    _remplir(store, horloge, [(0, 1000.0, 10), (20, 1010.0, 10)])
    assert store.projected_total(_EVENT_END) is None


def test_la_projection_extrapole_a_la_vitesse_recente(horloge):
    store = HistoryStore()
    _remplir(store, horloge, [(0, 0.0, 10), (600, 6000.0, 10)])
    # 600 €/min sur les 10 minutes écoulées, 10 minutes restantes.
    fin = DEBUT + 600 + 600
    assert store.projected_total(fin, 3600.0) == pytest.approx(12000.0)


def test_une_fin_deja_passee_rend_le_total_courant(horloge):
    """Après la clôture, « au rythme actuel » n'a plus de sens : on affiche
    le total, pas une extrapolation à rebours qui donnerait moins que collecté."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 0.0, 10), (600, 6000.0, 10)])
    assert store.projected_total(DEBUT, 3600.0) == pytest.approx(6000.0)


# ── référence de l'édition précédente ────────────────────────────────────────

@pytest.fixture
def edition_precedente():
    """Courbe de référence linéaire : 100 000 € étalés sur 24 h."""
    return [(0.0, 0.0), (86400.0, 100_000.0)]


def test_pas_de_reference_avec_moins_de_deux_points():
    store = HistoryStore()
    assert store.previous_total_at(0.0) is None
    store._previous = [(0.0, 1.0)]
    assert store.previous_total_at(0.0) is None


def test_un_temps_ecoule_negatif_n_a_pas_de_reference(edition_precedente):
    store = HistoryStore()
    store._previous = edition_precedente
    assert store.previous_total_at(-1.0) is None


@pytest.mark.parametrize("ecoule,attendu", [
    (0.0, 0.0),               # origine = premier point relevé
    (21600.0, 25_000.0),      # quart de course
    (43200.0, 50_000.0),      # interpolation à mi-chemin
    (86400.0, 100_000.0),     # borne haute incluse
])
def test_la_reference_est_interpolee_lineairement(edition_precedente, ecoule, attendu):
    """L'alignement se fait sur le temps écoulé depuis le coup d'envoi.

    Les deux éditions ne tombent pas les mêmes jours : aligner sur la date
    civile décalerait la comparaison de plusieurs heures.
    """
    store = HistoryStore()
    store._previous = edition_precedente
    assert store.previous_total_at(ecoule) == pytest.approx(attendu)


def test_la_reference_suit_les_coudes_de_la_courbe():
    """La collecte n'est pas linéaire : il y a des paliers de nuit et des pics
    de soirée.

    L'interpolation doit se faire dans le BON segment ; se contenter des deux
    extrémités lisserait la courbe et donnerait une comparaison fausse au
    milieu de l'édition.
    """
    store = HistoryStore()
    store._previous = [(0.0, 0.0), (3600.0, 10_000.0), (7200.0, 60_000.0)]
    # Milieu du second segment, bien plus pentu que le premier.
    assert store.previous_total_at(5400.0) == pytest.approx(35_000.0)
    # Une simple corde entre les extrêmes aurait donné 45 000 €.
    assert store.previous_total_at(5400.0) != pytest.approx(45_000.0)


def test_au_dela_de_la_reference_on_ne_devine_pas(edition_precedente):
    """Prolonger la courbe précédente au-delà de ce qu'elle couvre inventerait
    un point de comparaison."""
    store = HistoryStore()
    store._previous = edition_precedente
    assert store.previous_total_at(86401.0) is None


def test_deux_releves_au_meme_instant_rendent_le_plus_recent():
    """Doublon d'horodatage dans le dépôt tiers : la division par (tb - ta)
    lèverait ZeroDivisionError dans un slot Qt."""
    store = HistoryStore()
    store._previous = [(1000.0, 10.0), (1000.0, 20.0), (2000.0, 30.0)]
    assert store.previous_total_at(0.0) == pytest.approx(20.0)


# ── comparaison à l'édition précédente ───────────────────────────────────────

def test_pas_de_comparaison_hors_edition(edition_precedente):
    store = HistoryStore()
    store._previous = edition_precedente
    assert store.compare_to_previous(50_000.0, now_ts=_EVENT_START - 1.0) is None
    assert store.compare_to_previous(50_000.0, now_ts=_EVENT_END + 1.0) is None


def test_la_comparaison_donne_la_reference_et_l_ecart(horloge, edition_precedente):
    store = HistoryStore()
    store._previous = edition_precedente
    _remplir(store, horloge, [(0, 10_000.0, 10)])

    ref, ecart = store.compare_to_previous(75_000.0, now_ts=DEBUT + 43200.0)
    assert ref == pytest.approx(50_000.0)
    assert ecart == pytest.approx(50.0)


def test_l_origine_est_le_premier_releve_pas_minuit(horloge, edition_precedente):
    """_EVENT_START est une frontière de minuit servant à filtrer la fenêtre,
    alors que le direct démarre en soirée.

    Prendre minuit comme origine décalait la comparaison de plus de quinze
    heures et faisait sortir de la plage couverte avant la fin de l'event.
    """
    store = HistoryStore()
    store._previous = edition_precedente
    _remplir(store, horloge, [(0, 10_000.0, 10)])   # premier relevé à DEBUT

    ref, _ = store.compare_to_previous(1.0, now_ts=DEBUT + 43200.0)
    assert ref == pytest.approx(50_000.0)
    # Depuis minuit, l'écoulé vaudrait 46 800 s → une référence plus haute.
    assert ref != pytest.approx(46800.0 / 86400.0 * 100_000.0)


def test_sans_releve_l_origine_retombe_sur_le_debut_de_l_event(edition_precedente):
    """Au tout premier tour de boucle, aucune donnée live n'est encore
    stockée ; il faut quand même une origine plutôt qu'une exception."""
    store = HistoryStore()
    store._previous = edition_precedente
    ref, _ = store.compare_to_previous(1.0, now_ts=_EVENT_START + 43200.0)
    assert ref == pytest.approx(50_000.0)


@pytest.mark.parametrize("courant,precedent", [
    (0.0, [(0.0, 0.0), (86400.0, 100_000.0)]),      # cagnotte courante nulle
    (-5.0, [(0.0, 0.0), (86400.0, 100_000.0)]),     # cagnotte courante absurde
    (50_000.0, [(0.0, 0.0), (86400.0, 0.0)]),       # référence nulle
])
def test_une_comparaison_sans_base_saine_est_refusee(courant, precedent):
    """Le pourcentage divise par la référence : sans garde, une référence à
    zéro lèverait ZeroDivisionError, et un total courant nul afficherait
    « -100 % » avant même le coup d'envoi."""
    store = HistoryStore()
    store._previous = precedent
    assert store.compare_to_previous(courant, now_ts=_EVENT_START + 43200.0) is None


def test_pas_de_comparaison_sans_reference_chargee():
    """Si le dépôt tiers est injoignable, on n'affiche pas de comparaison."""
    store = HistoryStore()
    assert store.compare_to_previous(50_000.0, now_ts=_EVENT_START + 43200.0) is None


# ── chargement de l'édition précédente ───────────────────────────────────────

def _payload_nominal():
    """Cagnotte en ordre DÉCROISSANT, viewers en ordre CROISSANT — comme le
    dépôt réel."""
    return _payload(
        pool_labels=[(_EVENT_START + 7200) * 1000, (_EVENT_START + 3600) * 1000,
                     _EVENT_START * 1000],
        pool_values=[3000, 2000, 1000],
        view_labels=[_EVENT_START * 1000, (_EVENT_START + 3600) * 1000],
        view_values=[100, 900],
    )


def test_la_cagnotte_chargee_est_remise_dans_l_ordre(depot_distant, horloge):
    """`pools.large` arrive du plus récent au plus ancien.

    L'oublier traçait la courbe à l'envers et donnait une vitesse négative.
    """
    appels = depot_distant(payload=_payload_nominal())
    store = HistoryStore()
    _charger(store)

    ts, vals = store.get_donation_series()
    assert vals == [1000.0, 2000.0, 3000.0]
    assert ts == sorted(ts)
    assert appels == [
        "https://maniarr.github.io/cache.zevent.gdoc.fr/statistics/all.json"]


def test_les_viewers_charges_ne_sont_pas_inverses(depot_distant, horloge):
    """`viewers.large` est déjà chronologique : l'inverser aussi remettrait
    cette série-là à l'envers."""
    depot_distant(payload=_payload_nominal())
    store = HistoryStore()
    _charger(store)

    ts, vals = store.get_viewers_series()
    assert vals == [100.0, 900.0]
    assert ts == [_EVENT_START, _EVENT_START + 3600]


def test_le_chargement_alimente_la_courbe_de_reference(depot_distant):
    """La copie doit être prise AVANT que le direct n'ajoute ses points,
    sinon la « référence » se contaminerait avec l'édition en cours."""
    depot_distant(payload=_payload_nominal())
    store = HistoryStore()
    _charger(store)

    assert store._previous == [(_EVENT_START, 1000.0),
                               (_EVENT_START + 3600, 2000.0),
                               (_EVENT_START + 7200, 3000.0)]
    assert store.previous_peak_viewers == 900


def test_les_points_aberrants_du_depot_sont_ecartes(depot_distant, horloge):
    """Un seul point invalide ne doit pas condamner tout le chargement."""
    depot_distant(payload=_payload(
        pool_labels=[(_EVENT_START + 3600) * 1000, 0, _EVENT_START * 1000],
        pool_values=[2000, 999, "abc"],
        view_labels=[_EVENT_START * 1000, None, (_EVENT_START + 3600) * 1000],
        view_values=[100, 200, 900],
    ))
    store = HistoryStore()
    _charger(store)

    # Le point à timestamp 0 et celui à valeur textuelle disparaissent.
    assert store.get_donation_series()[1] == [2000.0]
    assert store.get_viewers_series()[1] == [100.0, 900.0]


def test_un_rechargement_remplace_l_historique(depot_distant, horloge):
    """Le chargement vide les séries avant de les remplir : sans le clear,
    un second appel doublerait la courbe."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 42.0, 7)])

    depot_distant(payload=_payload_nominal())
    _charger(store)

    assert store.get_donation_series()[1] == [1000.0, 2000.0, 3000.0]
    assert store.get_viewers_series()[1] == [100.0, 900.0]


@pytest.mark.parametrize("erreur", [
    httpx.HTTPError("500"),
    httpx.ConnectError("dépôt injoignable"),
    RuntimeError("surprise"),
])
def test_un_depot_injoignable_ne_fait_pas_tomber_le_panel(depot_distant, erreur):
    """Le chargement est lancé au démarrage : une exception qui remonte
    empêcherait l'application de s'ouvrir hors ligne."""
    depot_distant(erreur=erreur)
    store = HistoryStore()
    _charger(store)

    assert store.get_donation_series() == ([], [])
    assert store._previous == []
    assert store.previous_peak_viewers == 0


@pytest.mark.parametrize("payload", [
    {},
    {"pools": {}},
    {"pools": {"large": {}}, "viewers": {"large": {}}},
    {"pools": {"large": {"labels": None, "values": None}},
     "viewers": {"large": {"labels": [], "values": []}}},
    None,
    "pas du json",
])
def test_un_json_malforme_ne_fait_pas_tomber_le_panel(depot_distant, payload):
    """Le format du dépôt tiers peut changer sans préavis."""
    depot_distant(payload=payload)
    store = HistoryStore()
    _charger(store)
    assert store._previous == []


def test_le_depot_vide_laisse_les_series_vides(depot_distant):
    depot_distant(payload=_payload([], [], [], []))
    store = HistoryStore()
    _charger(store)
    assert store.get_donation_series() == ([], [])
    assert store._previous == []
    assert store.previous_peak_viewers == 0


def test_le_chargement_respecte_le_plafond_de_points(depot_distant, horloge):
    """Un dépôt plus fourni que la rétention ne doit pas gonfler la mémoire ;
    ce sont les points les plus anciens qui sautent."""
    labels = [(_EVENT_START + i * 60) * 1000 for i in range(10)]
    valeurs = [float(i) for i in range(10)]
    depot_distant(payload=_payload(
        pool_labels=list(reversed(labels)), pool_values=list(reversed(valeurs)),
        view_labels=labels, view_values=valeurs,
    ))
    store = HistoryStore(max_points=4)
    _charger(store)

    assert store.get_donation_series()[1] == [6.0, 7.0, 8.0, 9.0]
    assert len(store._previous) == 4


def test_le_pic_de_viewers_est_un_entier(depot_distant):
    """Un nombre de spectateurs est un entier.

    Le laisser en float fait afficher « 900.0 » dès qu'un formatage passe par
    str(), et casse une comparaison stricte de type côté panel.
    """
    depot_distant(payload=_payload_nominal())
    store = HistoryStore()
    _charger(store)
    assert isinstance(store.previous_peak_viewers, int)


def test_un_chargement_interrompu_ne_laisse_pas_d_etat_bancal(depot_distant, horloge):
    """La cagnotte est remplie AVANT que les viewers ne soient lus.

    Si la lecture des viewers lève, `_donation` contient déjà la courbe de
    l'édition précédente alors que `_previous` reste vide : `donation_rate()`
    mesure alors la vitesse de l'édition passée en la présentant comme celle
    en cours, et la comparaison reste indisponible.
    """
    depot_distant(payload={
        "pools": {"large": {
            "labels": [(_EVENT_START + 3600) * 1000, _EVENT_START * 1000],
            "values": [2000, 1000],
        }},
        # Pas de clé "viewers" : le format du dépôt a changé.
    })
    store = HistoryStore()
    _charger(store)

    assert store.get_donation_series() == ([], [])


# ── bornes exposées ──────────────────────────────────────────────────────────

def test_les_bornes_de_l_event_sont_exposees():
    """Le panel s'en sert pour cadrer l'axe des abscisses et pour la
    projection de fin ; elles doivent rester cohérentes entre elles."""
    store = HistoryStore()
    assert store.event_start_ts == _EVENT_START
    assert store.event_end_ts == _EVENT_END
    assert store.event_start_ts < store.event_end_ts


# ── superposition d'une édition passée ───────────────────────────────────────

def _reponse_edition(charge, statut=200):
    """Un client httpx dont chaque GET rend `charge`."""
    import httpx

    def repondre(_requete):
        if isinstance(charge, str):
            return httpx.Response(statut, text=charge)
        return httpx.Response(statut, json=charge)

    return httpx.AsyncClient(transport=httpx.MockTransport(repondre))


def _edition(dons, vues=()):
    return {"graph": {"donations": {"all": {
                          "labels": [t for t, _v in dons],
                          "values": [v for _t, v in dons]}},
                      "viewers": {"labels": [t for t, _v in vues],
                                  "values": [v for _t, v in vues]}}}


def _charger_edition(store, charge, statut=200):
    import asyncio

    async def essai():
        async with _reponse_edition(charge, statut) as client:
            return await store.charger_edition("ev-2025", client=client)

    return asyncio.run(essai())


_J = 1_757_088_000_000    # 5 septembre 2025, en millisecondes
_H = 3_600_000


def test_une_edition_passee_se_charge_avec_ses_deux_courbes():
    store = HistoryStore()
    assert _charger_edition(store, _edition(
        [(_J, 0), (_J + _H, 1000), (_J + 2 * _H, 3000)],
        [(_J, 100), (_J + 2 * _H, 500)])) is True
    assert len(store._previous) == 3
    assert len(store._previous_viewers) == 2
    assert store.previous_peak_viewers == 500


@pytest.mark.parametrize("charge,statut", [
    ({}, 200),                                  # pas de section graph
    ({"graph": {}}, 200),                       # pas de donations
    (_edition([(_J, 0)]), 200),                 # un seul point : rien à tracer
    ("pas du json", 200),
    ({}, 503),
])
def test_une_source_qui_se_derobe_ne_casse_rien(charge, statut):
    """Une comparaison manquante retire une courbe, elle n'empêche pas de
    suivre l'événement."""
    store = HistoryStore()
    assert _charger_edition(store, charge, statut) is False
    assert store._previous == []


def test_un_point_aberrant_est_ecarte_sans_perdre_les_autres():
    """Un timestamp absurde remonterait jusqu'à datetime.fromtimestamp dans un
    slot Qt sans try/except, et ferait tomber le panel."""
    store = HistoryStore()
    _charger_edition(store, _edition([(_J, 0), (1, 500), (_J + _H, 1000)]))
    assert [t for t, _v in store._previous] == [_J / 1000, (_J + _H) / 1000]


def test_les_points_sont_remis_dans_l_ordre():
    """`pools.large` du dépôt historique arrive à l'envers : ne pas trier
    traçait la courbe en sens inverse et donnait une vitesse négative."""
    store = HistoryStore()
    _charger_edition(store, _edition([(_J + 2 * _H, 3000), (_J, 0), (_J + _H, 1000)]))
    assert [v for _t, v in store._previous] == [0.0, 1000.0, 3000.0]


def test_la_courbe_passee_est_alignee_sur_le_temps_de_course(horloge):
    """Les deux éditions ne tombent pas les mêmes jours : c'est le temps écoulé
    depuis L'OUVERTURE DES DONS qui les rend comparables, pas la date.

    Et pas non plus le premier relevé : celui de l'édition en cours précède sa
    collecte de plusieurs heures — les directs ouvrent avant la cagnotte.
    """
    store = HistoryStore()
    # L'édition de référence est publiée dès l'ouverture des dons : premier
    # point déjà positif, comme les quatre vraies.
    _charger_edition(store, _edition([(_J, 600), (_J + _H, 1200),
                                      (_J + 2 * _H, 2000)]))

    # Les relevés partent du DÉPART DE LA COURSE : c'est là que les éditions
    # passées sont calées.
    for i in range(3):
        horloge["t"] = DEBUT_COURSE + i * 1800.0   # toutes les 30 min
        store.add_point(i * 1000.0, 10)

    ts, _vals = store.get_donation_series()
    aligne = store.serie_precedente_alignee(ts)
    # 0 min → 600 €, 30 min → 900 €, 60 min → 1 200 €.
    assert aligne == [600.0, 900.0, 1200.0]


def test_hors_de_la_plage_couverte_la_courbe_s_interrompt(horloge):
    """None, et non zéro : une falaise se lirait comme un effondrement."""
    store = HistoryStore()
    _charger_edition(store, _edition([(_J, 500), (_J + _H, 1000)]))
    for i, montant in ((0, 500.0), (2, 1500.0)):
        horloge["t"] = DEBUT_COURSE + i * 3600.0
        store.add_point(montant, 10)
    ts, _vals = store.get_donation_series()
    # Deux heures après le départ, la référence n'en couvre qu'une.
    assert store.serie_precedente_alignee(ts) == [500.0, None]


def test_sans_reference_chargee_la_serie_alignee_est_vide(horloge):
    store = HistoryStore()
    horloge["t"] = _EVENT_START + 60.0
    store.add_point(10.0, 1)
    ts, _vals = store.get_donation_series()
    assert store.serie_precedente_alignee(ts) == [None]
    assert store.serie_viewers_precedente_alignee(ts) == [None]


# ── plusieurs éditions superposées ───────────────────────────────────────────

def _reponses_par_edition(table, statut=200):
    """Un client qui rend une charge différente selon l'identifiant demandé."""
    import httpx

    def repondre(requete):
        for eid, charge in table.items():
            if eid in str(requete.url):
                if charge is None:
                    return httpx.Response(404, json={})
                return httpx.Response(statut, json=charge)
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(repondre))


def _charger_toutes(store, table, editions):
    import asyncio

    async def essai():
        async with _reponses_par_edition(table) as client:
            return await store.charger_editions(editions, client=client)

    return asyncio.run(essai())


def _edition_complete(total, dons):
    charge = _edition(dons)
    charge["donation_amount"] = int(total * 100)
    return charge


def test_plusieurs_editions_se_chargent_et_se_classent():
    """Une seule référence ne dit pas si l'an dernier était un bon cru."""
    store = HistoryStore()
    table = {
        "id-2025": _edition_complete(3000, [(_J, 0), (_J + _H, 3000)]),
        "id-2024": _edition_complete(2000, [(_J, 0), (_J + _H, 2000)]),
    }
    retenues = _charger_toutes(store, table,
                               (("2025", "id-2025"), ("2024", "id-2024")))
    assert retenues == ["2025", "2024"]
    assert store.editions_chargees() == ["2025", "2024"]


def test_la_plus_recente_devient_la_reference_de_la_phrase():
    """`compare_to_previous` n'en compare qu'une : ce doit être la dernière."""
    store = HistoryStore()
    table = {
        "id-2025": _edition_complete(3000, [(_J, 0), (_J + _H, 3000)]),
        "id-2024": _edition_complete(2000, [(_J, 0), (_J + _H, 2000)]),
    }
    _charger_toutes(store, table, (("2025", "id-2025"), ("2024", "id-2024")))
    assert store._previous[-1][1] == 3000.0


def test_une_edition_dont_la_courbe_contredit_son_total_est_ecartee():
    """Tracer une année sans dons se lirait comme vrai faute de savoir que
    c'est faux — mieux vaut une comparaison de moins."""
    store = HistoryStore()
    table = {
        "id-ok": _edition_complete(3000, [(_J, 0), (_J + _H, 3000)]),
        # Total déclaré à 10 M€, courbe qui s'arrête à 16 k€.
        "id-ko": _edition_complete(10_000_000, [(_J, 0), (_J + _H, 16_000)]),
    }
    retenues = _charger_toutes(store, table,
                               (("bonne", "id-ok"), ("cassee", "id-ko")))
    assert retenues == ["bonne"]


def test_une_edition_sans_total_declare_est_crue_sur_parole():
    """Rien à confronter : refuser la courbe perdrait une comparaison valable."""
    store = HistoryStore()
    table = {"id-x": _edition([(_J, 0), (_J + _H, 3000)])}
    assert _charger_toutes(store, table, (("x", "id-x"),)) == ["x"]


def test_une_edition_indisponible_n_empeche_pas_les_autres():
    store = HistoryStore()
    table = {
        "id-ok": _edition_complete(3000, [(_J, 0), (_J + _H, 3000)]),
        "id-absente": None,
    }
    retenues = _charger_toutes(store, table,
                               (("absente", "id-absente"), ("ok", "id-ok")))
    assert retenues == ["ok"]


def test_des_releves_tous_au_meme_instant_sont_ecartes():
    """Une série sans durée n'a pas de temps de course : l'aligner n'a
    aucun sens, et le tri ne peut pas la réparer."""
    store = HistoryStore()
    table = {"id-fige": _edition([(_J, 0), (_J, 3000)])}
    assert _charger_toutes(store, table, (("figee", "id-fige"),)) == []


def test_les_series_de_toutes_les_editions_s_alignent(horloge):
    store = HistoryStore()
    # Positives dès leur premier point, comme les quatre vraies : une édition
    # est publiée à partir de l'ouverture de ses dons.
    table = {
        "id-a": _edition_complete(2000, [(_J, 500), (_J + 2 * _H, 2000)]),
        "id-b": _edition_complete(1000, [(_J, 250), (_J + 2 * _H, 1000)]),
    }
    _charger_toutes(store, table, (("a", "id-a"), ("b", "id-b")))

    for i in range(3):
        horloge["t"] = DEBUT_COURSE + i * 3600.0
        store.add_point(i * 900.0, 10)
    ts, _vals = store.get_donation_series()

    alignees = store.series_editions_alignees(ts)
    assert set(alignees) == {"a", "b"}
    assert alignees["a"] == [500.0, 1250.0, 2000.0]
    assert alignees["b"] == [250.0, 625.0, 1000.0]


# ── l'édition en cours, préchargée depuis son début ─────────────────────────

_MS = [int((OUVERTURE_CAGNOTTE + i * 3600) * 1000) for i in range(3)]

_GRAPHE_EN_COURS = {
    "graph": {
        "donations": {"all": {"labels": _MS,
                              "values": [0.0, 120_000.0, 532_730.49]}},
        "viewers": {"labels": _MS, "values": [5290, 90_000, 143_633]},
    },
}


@pytest.fixture
def sans_reseau(monkeypatch):
    """Rend le JSON voulu au lieu d'aller le chercher."""
    def poser(charge):
        async def _faux(_event_id, _client=None):
            return charge
        monkeypatch.setattr(HistoryStore, "_telecharger_edition",
                            staticmethod(_faux))
    return poser


def test_l_edition_en_cours_est_prechargee_depuis_son_debut(sans_reseau):
    """Sans elle, ZLink ne trace que ce qu'il a relevé depuis son lancement.

    Lancer le panel à minuit donnait une minute de courbe sur un graphe qui en
    annonce soixante-douze heures, et l'axe des ordonnées répétait « 535k€ »
    huit fois de suite.
    """
    sans_reseau(_GRAPHE_EN_COURS)
    h = HistoryStore()
    assert asyncio.run(h.charger_edition_en_cours("peu-importe"))
    ts, vals = h.get_donation_series()
    assert len(ts) == 3
    assert vals[0] == 0.0 and vals[-1] == pytest.approx(532_730.49)
    assert (ts[-1] - ts[0]) == 7200.0


def test_le_prechargement_ne_se_fait_pas_passer_pour_du_direct(sans_reseau):
    """`_live_depuis` sépare ce que ZLink a observé du reste : le préchargement
    n'est pas une observation, et `_garder` doit continuer de le borner."""
    sans_reseau(_GRAPHE_EN_COURS)
    h = HistoryStore()
    asyncio.run(h.charger_edition_en_cours("peu-importe"))
    assert h._live_depuis is None


def test_une_source_muette_laisse_la_courbe_intacte(sans_reseau):
    """Une comparaison manquante retire une courbe, elle n'efface pas l'autre."""
    sans_reseau(None)
    h = HistoryStore()
    h.add_point(42.0, 7, instant=_EVENT_START + 60)
    assert not asyncio.run(h.charger_edition_en_cours("peu-importe"))
    assert h.get_donation_series()[1] == [42.0]


def test_une_courbe_trop_courte_est_refusee(sans_reseau):
    """Deux points au moins : une valeur seule ne trace rien."""
    sans_reseau({"graph": {"donations": {"all": {"labels": [1_788_408_000_000],
                                                 "values": [0.0]}}}})
    assert not asyncio.run(HistoryStore().charger_edition_en_cours("x"))


# ── l'origine de la course : le premier don, pas le premier relevé ──────────

def test_l_origine_est_le_premier_don():
    """Les éditions passées sont publiées à partir de l'ouverture des dons —
    premier point déjà positif. Celle en cours est relevée dès l'ouverture des
    DIRECTS, six heures et demie plus tôt, à zéro.

    Caler sur le premier relevé décalait les comparaisons d'autant : au bout de
    sept heures, 2026 en était à sa première heure de collecte quand 2021
    affichait déjà sept heures et un million d'euros.
    """
    assert HistoryStore.origine_course(
        [(100.0, 0.0), (200.0, 0.0), (300.0, 42.0), (400.0, 90.0)]) == 300.0


def test_une_edition_publiee_apres_l_ouverture_garde_son_premier_point():
    """Le cas des quatre vraies : elles démarrent déjà positives."""
    assert HistoryStore.origine_course([(100.0, 500.0), (200.0, 900.0)]) == 100.0


def test_une_courbe_restee_a_zero_retombe_sur_son_premier_point():
    """Avant le premier euro, il n'y a pas encore de course : on ne décale pas
    dans le vide, sinon l'origine sortirait de la série."""
    assert HistoryStore.origine_course([(100.0, 0.0), (200.0, 0.0)]) == 100.0


def test_sans_courbe_il_n_y_a_pas_d_origine():
    assert HistoryStore.origine_course([]) is None


def test_l_origine_des_references_est_le_depart_de_la_course():
    """Une DATE, pas le premier don relevé.

    La cagnotte 2026 ouvre le jeudi à 18 h et reçoit des dons d'avant-événement
    toute la nuit ; la course, elle, part le vendredi. Prendre le premier don
    pour le départ avançait les références de vingt-quatre heures et les
    faisait courir sur un jeudi soir qui, pour elles, n'existe pas.
    """
    assert HistoryStore._origine_courante([1.0, 2.0]) == DEBUT_COURSE


def test_les_releves_d_avant_l_ouverture_sont_ecartes(sans_reseau):
    """La source publie dès le jeudi midi, tous à zéro : six heures de plat."""
    avant = int((OUVERTURE_CAGNOTTE - 7200) * 1000)
    sans_reseau({"graph": {
        "donations": {"all": {"labels": [avant] + _MS,
                              "values": [0.0, 0.0, 120_000.0, 532_730.49]}},
        "viewers": {"labels": [avant] + _MS,
                    "values": [0, 5290, 90_000, 143_633]},
    }})
    h = HistoryStore()
    assert asyncio.run(h.charger_edition_en_cours("peu-importe"))
    ts, _vals = h.get_donation_series()
    assert min(ts) >= OUVERTURE_CAGNOTTE


# ── le cadrage « toute la course » ──────────────────────────────────────────

def test_l_axe_de_course_va_de_l_ouverture_au_lundi():
    """Du jeudi 18 h — ouverture de la cagnotte 2026 — au lundi 1 h."""
    from core.history_store import FIN_COURSE, axe_course

    axe = axe_course()
    assert axe[0] == OUVERTURE_CAGNOTTE
    assert axe[-1] == FIN_COURSE
    assert all(b - a == 1800.0 for a, b in zip(axe, axe[1:]))


def test_la_course_ne_commence_pas_a_l_ouverture_de_la_cagnotte():
    """Vingt-quatre heures les séparent, et c'est tout le sujet."""
    from core.history_store import course_commencee

    assert not course_commencee(OUVERTURE_CAGNOTTE + 3600)
    assert course_commencee(DEBUT_COURSE)


def test_la_courbe_en_cours_s_arrete_sur_son_dernier_releve(sans_reseau):
    """Sur un axe qui court jusqu'au lundi, elle avance petit à petit.

    None après le dernier point, et non zéro : une falaise se lirait comme un
    effondrement de la cagnotte.
    """
    sans_reseau(_GRAPHE_EN_COURS)
    h = HistoryStore()
    asyncio.run(h.charger_edition_en_cours("peu-importe"))
    axe = [OUVERTURE_CAGNOTTE + i * 1800.0 for i in range(10)]
    serie = h.serie_courante_sur_axe(axe)
    assert serie[0] == 0.0
    assert serie[4] == pytest.approx(532_730.49)      # dernier relevé, +2 h
    assert all(v is None for v in serie[5:])


def test_une_serie_trop_courte_ne_trace_rien_sur_l_axe():
    assert HistoryStore().serie_courante_sur_axe([1.0, 2.0]) == [None, None]
