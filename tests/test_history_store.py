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


def test_les_points_hors_fenetre_event_sont_filtres(horloge):
    """La fenêtre isole l'édition en cours de l'historique préchargé.

    Sans ce filtre, la courbe de l'édition précédente s'afficherait comme si
    elle appartenait à celle-ci.
    """
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


@pytest.mark.xfail(reason="DEBUG=True ne rend rien hors fenêtre : le filtre "
                          "par timestamp de get_*_series annule le contournement",
                   strict=False)
def test_debug_rend_les_points_meme_hors_fenetre(horloge, monkeypatch):
    """Intention documentée en tête de module : « En test → renvoie les données
    même hors fenêtre event ».

    En pratique DEBUG ne court-circuite que le test sur l'heure courante ; les
    points restent filtrés un par un sur la fenêtre, donc un relevé pris
    aujourd'hui (hors édition) disparaît quand même.
    """
    monkeypatch.setattr(history_store, "DEBUG", True)
    store = HistoryStore()
    horloge["t"] = _EVENT_START - 86400.0
    store.add_point(1000.0, 10)
    assert store.get_donation_series()[1] == [1000.0]


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

def test_pas_de_projection_hors_de_l_edition(horloge):
    """Extrapoler quatorze jours avant le coup d'envoi donnait des milliards."""
    store = HistoryStore()
    _remplir(store, horloge, [(0, 0.0, 10), (600, 6000.0, 10)])
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
