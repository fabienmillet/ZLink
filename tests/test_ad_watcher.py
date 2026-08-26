# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection des coupures publicitaires : marqueurs HLS et machine à états.

L'enjeu du compteur de confirmations est qu'un relevé aberrant isolé ne fasse
pas clignoter le bandeau. C'est ce qu'on vérifie ici.
"""

from __future__ import annotations

import pytest

from core import ad_watcher
from core.ad_watcher import (
    _AD_CONFIRM,
    _END_CONFIRM,
    _POLL_INTERVAL,
    _STARTUP_GRACE,
    _playlist_has_ad,
)


# ── marqueurs de playlist ────────────────────────────────────────────────────

@pytest.mark.parametrize("texte", [
    '#EXT-X-DATERANGE:ID="1",CLASS="twitch-stitched-ad"',
    '#EXT-X-DATERANGE:ID="1",CLASS="stitched-ad"',
    "https://cdn.test/ad_video/segment1.ts",
    "#EXT-X-ASSET:CAID=12345",
    '#ext-x-daterange:class="TWITCH-STITCHED-AD"',      # insensible à la casse
])
def test_marqueurs_de_pub_reconnus(texte):
    assert _playlist_has_ad(texte) is True


@pytest.mark.parametrize("texte", [
    "",
    "#EXTM3U\n#EXT-X-VERSION:3\nsegment1.ts",
    '#EXT-X-DATERANGE:ID="1",CLASS="autre-chose"',
    "https://cdn.test/video/segment1.ts",
])
def test_playlist_ordinaire_sans_marqueur(texte):
    assert _playlist_has_ad(texte) is False


# ── machine à états ──────────────────────────────────────────────────────────

class _Veilleur:
    """_StreamWatcher sans thread ni réseau : seule la transition nous intéresse."""

    def __init__(self):
        self.login = "zerator"
        self.debuts: list[str] = []
        self.fins: list[str] = []
        self._on_start = self.debuts.append
        self._on_end = self.fins.append
        self._pub_active = False
        self._pos_streak = 0
        self._neg_streak = 0

    transition = ad_watcher._StreamWatcher._transition


def test_une_pub_est_annoncee_apres_confirmation():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
        assert v.debuts == [], "annoncée trop tôt"
    v.transition(True)
    assert v.debuts == ["zerator"]


def test_un_releve_positif_isole_n_annonce_rien():
    """Le cas que le compteur existe pour écarter."""
    v = _Veilleur()
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
    v.transition(False)          # la série est cassée
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
    assert v.debuts == []


def test_la_fin_demande_sa_propre_confirmation():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    assert v.debuts == ["zerator"]
    for _ in range(_END_CONFIRM - 1):
        v.transition(False)
        assert v.fins == [], "fin annoncée trop tôt"
    v.transition(False)
    assert v.fins == ["zerator"]


def test_pas_de_seconde_annonce_tant_que_la_pub_dure():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM * 3):
        v.transition(True)
    assert v.debuts == ["zerator"]


def test_pas_de_fin_sans_debut():
    v = _Veilleur()
    for _ in range(_END_CONFIRM * 2):
        v.transition(False)
    assert v.fins == []


def test_cycle_complet_puis_nouvelle_pub():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    for _ in range(_END_CONFIRM):
        v.transition(False)
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    assert v.debuts == ["zerator", "zerator"]
    assert v.fins == ["zerator"]


# ── outillage : ni fil d'exécution, ni socket ────────────────────────────────

class _FilFactice:
    """`threading.Thread` de remplacement : retient la cible sans la lancer.

    Un vrai fil partirait sonder une URL Twitch depuis la suite de tests, et
    survivrait au test qui l'a créé. Les tests appellent `_run()` eux-mêmes,
    quand ils veulent et avec l'horloge qu'ils ont choisie.
    """

    def __init__(self, target=None, daemon=False, name="") -> None:
        self.target = target
        self.daemon = daemon
        self.name = name
        self.demarre = False

    def start(self) -> None:
        self.demarre = True


class _ArretFactice:
    """`threading.Event` de remplacement : borne la boucle et n'attend jamais.

    `_run` boucle sur `is_set()` et dort `_POLL_INTERVAL` à chaque tour : sans
    cette doublure, éprouver trois sondages coûterait neuf secondes.
    """

    def __init__(self, tours: int = 1) -> None:
        self.tours_restants = tours
        self.attentes: list[float | None] = []
        self._pose = False

    def is_set(self) -> bool:
        if self._pose:
            return True
        if self.tours_restants <= 0:
            return True
        self.tours_restants -= 1
        return False

    def set(self) -> None:
        self._pose = True

    def wait(self, timeout: float | None = None) -> bool:
        self.attentes.append(timeout)
        return self._pose


class _Reponse:
    """Le strict nécessaire d'une réponse httpx pour `_fetch`."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _ClientFactice:
    """`httpx.Client` de remplacement : sert des réponses préparées.

    Une exception placée dans la file est levée au lieu d'être rendue, ce qui
    rejoue une coupure réseau sans ouvrir de socket.
    """

    def __init__(self, reponses=()) -> None:
        self._reponses = list(reponses)
        self.urls: list[str] = []
        self.ferme = False

    def get(self, url: str) -> _Reponse:
        self.urls.append(url)
        reponse = self._reponses.pop(0) if self._reponses else _Reponse(200, "")
        if isinstance(reponse, Exception):
            raise reponse
        return reponse

    def close(self) -> None:
        self.ferme = True


@pytest.fixture
def fils(monkeypatch):
    """Neutralise `threading` dans le module et rend la liste des fils créés.

    On remplace le module vu par `core.ad_watcher` plutôt que l'attribut
    `Thread` du vrai module : sinon tout code lançant un fil pendant le test,
    ZLink ou pytest, recevrait la doublure.
    """
    import threading
    import types

    crees: list[_FilFactice] = []

    def fabrique(target=None, daemon=False, name="") -> _FilFactice:
        fil = _FilFactice(target=target, daemon=daemon, name=name)
        crees.append(fil)
        return fil

    monkeypatch.setattr(
        ad_watcher, "threading",
        types.SimpleNamespace(Thread=fabrique, Event=threading.Event),
    )
    return crees


@pytest.fixture
def veilleur(fils):
    """Fabrique un `_StreamWatcher` dont le fil ne part pas.

    Les rappels sont branchés sur deux listes, exposées en `debuts` et `fins`.
    """
    def _fabrique(url: str = "https://cdn.test/live.m3u8") -> ad_watcher._StreamWatcher:
        debuts: list[str] = []
        fins: list[str] = []
        v = ad_watcher._StreamWatcher("zerator", url, debuts.append, fins.append)
        v.debuts = debuts
        v.fins = fins
        return v
    return _fabrique


@pytest.fixture
def client_http(monkeypatch):
    """Rend une fonction qui installe un `httpx.Client` factice et le rend."""
    def _poser(*reponses) -> _ClientFactice:
        client = _ClientFactice(reponses)
        monkeypatch.setattr(ad_watcher.httpx, "Client", lambda **kw: client)
        return client
    return _poser


@pytest.fixture
def horloge(monkeypatch):
    """Fige `time.monotonic` et rend le cadran, réglable par le test.

    La période de grâce du démarrage se mesure sur cette horloge. Sans la
    figer, le test dépendrait de sa propre durée d'exécution : douze secondes
    ne s'écoulent pas pendant trois sondages simulés.
    """
    etat = {"t": 1_000.0, "pas": 0.0}

    def _monotonic() -> float:
        maintenant = etat["t"]
        etat["t"] += etat["pas"]
        return maintenant

    monkeypatch.setattr("core.ad_watcher.time.monotonic", _monotonic)
    return etat


# ── le fil de surveillance ───────────────────────────────────────────────────

def test_le_fil_de_surveillance_est_daemon_et_porte_le_login(fils, veilleur):
    """Un fil non-daemon empêcherait ZLink de se fermer.

    Rien n'attend ces fils à la fermeture : `unwatch_all` pose seulement le
    drapeau d'arrêt et rend la main. Le nom est ce qu'on lit dans une pile
    d'exécution quand un veilleur part en vrille.
    """
    veilleur()
    assert len(fils) == 1
    assert fils[0].daemon is True
    assert fils[0].name == "ad-watch-zerator"
    assert fils[0].demarre is True, "le fil doit démarrer dès la construction"


def test_un_veilleur_neuf_ne_croit_a_aucune_pub(veilleur):
    """L'état de départ décide de la première transition : le supposer actif
    ferait annoncer une fin de pub qui n'a jamais commencé."""
    v = veilleur()
    assert v._pub_active is False
    assert v._pos_streak == 0
    assert v._neg_streak == 0


def test_stop_empeche_tout_sondage(veilleur, client_http):
    """`unwatch` peut arriver avant que le fil n'ait fait son premier tour.

    La boucle doit alors ne rien demander du tout — et refermer quand même le
    client HTTP qu'elle vient d'ouvrir.
    """
    v = veilleur()
    client = client_http()
    v.stop()
    v._run()
    assert client.urls == [], "aucune requête ne doit partir après stop()"
    assert client.ferme is True


def test_le_client_http_est_referme_a_la_sortie_de_la_boucle(veilleur, client_http):
    """Un client laissé ouvert garde son pool de connexions : autant de flux
    ouverts dans la session, autant de pools abandonnés."""
    v = veilleur()
    client = client_http(_Reponse(200, "#EXTM3U"))
    v._stop = _ArretFactice(tours=1)
    v._run()
    assert client.ferme is True


def test_le_client_est_referme_meme_si_un_rappel_leve(veilleur, client_http, horloge):
    """Le rappel émet un signal Qt : un slot fautif remonterait jusqu'ici.

    Le `finally` est la seule chose qui referme alors le client ; sans lui la
    connexion resterait ouverte jusqu'à la fin du processus.
    """
    def _rappel_fautif(login: str) -> None:
        raise RuntimeError("slot fautif")

    v = veilleur()
    v._on_start = _rappel_fautif
    horloge["pas"] = _STARTUP_GRACE          # la grâce est passée dès le 1er tour
    marque = '#EXT-X-DATERANGE:CLASS="twitch-stitched-ad"'
    client = client_http(*[_Reponse(200, marque) for _ in range(_AD_CONFIRM)])
    v._stop = _ArretFactice(tours=_AD_CONFIRM)
    with pytest.raises(RuntimeError):
        v._run()
    assert client.ferme is True


def test_chaque_tour_de_boucle_attend_l_intervalle_de_sondage(veilleur, client_http):
    """Sans cette attente la boucle martèlerait le CDN Twitch en continu — de
    quoi se faire couper, et pour une playlist qui ne bouge pas plus vite."""
    v = veilleur()
    client_http(_Reponse(200, "#EXTM3U"), _Reponse(200, "#EXTM3U"))
    v._stop = _ArretFactice(tours=2)
    v._run()
    assert v._stop.attentes == [_POLL_INTERVAL, _POLL_INTERVAL]


def test_les_marqueurs_des_premieres_secondes_sont_ignores(veilleur, client_http, horloge):
    """La raison d'être de la période de grâce.

    Le début d'un flux Twitch charrie souvent les marqueurs de la pub de
    pré-roll déjà passée : sans immunité, tout changement de streamer
    afficherait le bandeau « pub en cours » dans la foulée.
    """
    v = veilleur()
    marque = '#EXT-X-DATERANGE:ID="1",CLASS="twitch-stitched-ad"'
    tours = _AD_CONFIRM * 2
    client_http(*[_Reponse(200, marque) for _ in range(tours)])
    v._stop = _ArretFactice(tours=tours)          # horloge figée : 0 s écoulée
    v._run()
    assert v.debuts == []
    assert v._pos_streak == 0, "aucune série ne doit être comptée pendant la grâce"


def test_une_pub_est_annoncee_des_que_la_grace_est_passee(veilleur, client_http, horloge):
    """Le pendant du test précédent : la grâce protège le démarrage, elle ne
    doit pas rendre le veilleur sourd pour toujours."""
    v = veilleur()
    marque = '#EXT-X-DATERANGE:ID="1",CLASS="twitch-stitched-ad"'
    horloge["pas"] = _STARTUP_GRACE
    client_http(*[_Reponse(200, marque) for _ in range(_AD_CONFIRM)])
    v._stop = _ArretFactice(tours=_AD_CONFIRM)
    v._run()
    assert v.debuts == ["zerator"]


def test_une_playlist_illisible_ne_vaut_pas_pub(veilleur, client_http, horloge):
    """`_fetch` rend '' quand la requête échoue : la boucle doit lire ce vide
    comme « pas de pub », et surtout ne pas le passer aux expressions
    régulières comme s'il s'agissait d'une playlist."""
    v = veilleur()
    horloge["pas"] = _STARTUP_GRACE
    client_http(*[_Reponse(503) for _ in range(_AD_CONFIRM)])
    v._stop = _ArretFactice(tours=_AD_CONFIRM)
    v._run()
    assert v.debuts == []
    assert v._neg_streak == _AD_CONFIRM


def test_la_boucle_interroge_l_url_hls_qu_on_lui_a_donnee(veilleur, client_http):
    """Une URL réécrite ou perdue en route sonderait le mauvais flux — et
    l'erreur ne se verrait qu'à un bandeau qui n'apparaît jamais."""
    v = veilleur("https://usher.test/api/channel/hls/zerator.m3u8")
    client = client_http(_Reponse(200, "#EXTM3U"))
    v._stop = _ArretFactice(tours=1)
    v._run()
    assert client.urls == ["https://usher.test/api/channel/hls/zerator.m3u8"]


# ── récupération du M3U8 ─────────────────────────────────────────────────────

def test_un_m3u8_servi_en_200_est_rendu_tel_quel(veilleur):
    """Le texte doit arriver intact aux expressions régulières : le moindre
    rognage ferait rater les marqueurs placés en fin de playlist."""
    v = veilleur()
    corps = '#EXTM3U\n#EXT-X-DATERANGE:CLASS="twitch-stitched-ad"\n'
    assert v._fetch(_ClientFactice([_Reponse(200, corps)])) == corps


@pytest.mark.parametrize("code", [204, 301, 403, 404, 410, 500, 503])
def test_un_code_http_autre_que_200_ne_rend_rien(veilleur, code):
    """Le corps d'un 404 ou d'une page d'erreur du CDN n'est pas une playlist.

    Le prendre pour tel reviendrait à chercher des marqueurs de pub dans du
    HTML — et un flux terminé (410) serait interprété n'importe comment.
    """
    v = veilleur()
    assert v._fetch(_ClientFactice([_Reponse(code, "erreur")])) == ""


@pytest.mark.parametrize("erreur", [
    ConnectionError("réseau coupé"),
    TimeoutError("réseau coupé"),
    ValueError("réseau coupé"),
])
def test_une_erreur_reseau_ne_remonte_jamais_de_la_recuperation(veilleur, erreur):
    """Le fil de surveillance mourrait à la première coupure Wi-Fi.

    Il n'est jamais joint ni redémarré : une exception qui s'échappe d'ici
    laisserait le flux sans surveillance pour le reste de la session, sans que
    rien ne le signale.
    """
    v = veilleur()
    assert v._fetch(_ClientFactice([erreur])) == ""


def test_une_erreur_reseau_est_tracee(veilleur, caplog):
    """Une panne silencieuse est indébogable : la règle du dépôt interdit
    d'avaler une exception sans la journaliser. La trace doit nommer le
    streamer, sans quoi on ignore lequel des veilleurs a échoué."""
    import logging

    v = veilleur()
    with caplog.at_level(logging.DEBUG, logger="core.ad_watcher"):
        v._fetch(_ClientFactice([ConnectionError("réseau coupé")]))
    assert "zerator" in caplog.text
    assert "réseau coupé" in caplog.text


# ── le gestionnaire ──────────────────────────────────────────────────────────

def test_un_gestionnaire_neuf_ne_surveille_rien(qapp):
    """Rien ne doit partir avant qu'un flux ne soit effectivement lu."""
    g = ad_watcher.AdWatcher()
    assert g._watchers == {}


def test_watch_demarre_un_veilleur_pour_le_login(qapp, fils):
    """Sans entrée dans le registre, `unwatch` n'aurait plus rien à arrêter :
    le fil continuerait à sonder un flux qu'on ne regarde plus."""
    g = ad_watcher.AdWatcher()
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    assert set(g._watchers) == {"zerator"}
    assert g._watchers["zerator"].hls_url == "https://cdn.test/zerator.m3u8"
    assert len(fils) == 1


def test_une_url_hls_vide_ne_demarre_aucun_veilleur(qapp, fils):
    """Cas réel : streamlink n'a pas résolu le flux et rend une chaîne vide.

    Sonder '' donnerait une erreur httpx toutes les trois secondes, pour
    toujours.
    """
    g = ad_watcher.AdWatcher()
    g.watch("zerator", "")
    assert g._watchers == {}
    assert fils == [], "aucun fil ne doit être créé"


def test_une_url_vide_arrete_quand_meme_la_surveillance_en_cours(qapp, fils):
    """`watch` commence par `unwatch` : une résolution ratée après un flux qui
    marchait ne doit pas laisser l'ancien veilleur tourner dans le vide."""
    g = ad_watcher.AdWatcher()
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    ancien = g._watchers["zerator"]
    g.watch("zerator", "")
    assert ancien._stop.is_set() is True
    assert g._watchers == {}


def test_watch_deux_fois_remplace_le_veilleur_precedent(qapp, fils):
    """Une URL HLS Twitch expire : on la re-résout et on re-`watch`.

    Sans arrêt du précédent, deux fils sonderaient le même login et le bandeau
    clignoterait au gré de leurs machines à états respectives.
    """
    g = ad_watcher.AdWatcher()
    g.watch("zerator", "https://cdn.test/v1.m3u8")
    ancien = g._watchers["zerator"]
    g.watch("zerator", "https://cdn.test/v2.m3u8")
    nouveau = g._watchers["zerator"]
    assert ancien is not nouveau
    assert ancien._stop.is_set() is True
    assert nouveau._stop.is_set() is False
    assert nouveau.hls_url == "https://cdn.test/v2.m3u8"


def test_unwatch_arrete_le_veilleur_et_l_oublie(qapp, fils):
    """Le laisser dans le registre le ferait « arrêter » une seconde fois et,
    surtout, mentirait sur ce qui est encore surveillé."""
    g = ad_watcher.AdWatcher()
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    v = g._watchers["zerator"]
    g.unwatch("zerator")
    assert v._stop.is_set() is True
    assert g._watchers == {}


@pytest.mark.parametrize("login", ["", "jamais_vu"])
def test_unwatch_sur_un_login_inconnu_ne_leve_pas(qapp, fils, login):
    """La fenêtre plein écran appelle `unwatch(self._current_login)` à chaque
    changement de flux, y compris au tout premier — quand ce login vaut ''."""
    g = ad_watcher.AdWatcher()
    g.unwatch(login)
    assert g._watchers == {}


def test_unwatch_all_arrete_toutes_les_surveillances(qapp, fils):
    """Appelé à la fermeture de l'application : un seul veilleur oublié
    continue de sonder Twitch pendant que ZLink se ferme."""
    g = ad_watcher.AdWatcher()
    for login in ("zerator", "aypierre", "domingo"):
        g.watch(login, f"https://cdn.test/{login}.m3u8")
    veilleurs = list(g._watchers.values())
    g.unwatch_all()
    assert all(v._stop.is_set() for v in veilleurs)
    assert g._watchers == {}


def test_unwatch_all_sur_un_gestionnaire_vide_ne_leve_pas(qapp):
    """La fermeture ne doit pas dépendre d'un flux ayant été lu."""
    ad_watcher.AdWatcher().unwatch_all()


def test_le_debut_de_pub_ressort_par_le_signal_ad_detected(qapp, fils):
    """Le veilleur ne connaît que deux fonctions : c'est ce câblage qui porte
    l'information jusqu'au bandeau. Débranché, la détection marcherait sans
    que rien ne s'affiche."""
    g = ad_watcher.AdWatcher()
    recus: list[str] = []
    g.ad_detected.connect(recus.append)
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    g._watchers["zerator"]._on_start("zerator")
    assert recus == ["zerator"]


def test_la_fin_de_pub_ressort_par_le_signal_ad_ended(qapp, fils):
    """Sans ce signal, le bandeau « pub en cours » resterait à l'écran."""
    g = ad_watcher.AdWatcher()
    recus: list[str] = []
    g.ad_ended.connect(recus.append)
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    g._watchers["zerator"]._on_end("zerator")
    assert recus == ["zerator"]


def test_les_signaux_portent_le_login_du_veilleur_concerne(qapp, fils):
    """Deux flux surveillés en même temps : la fenêtre plein écran compare le
    login reçu au sien pour décider d'afficher le bandeau. Un login mélangé
    afficherait la pub d'un autre."""
    g = ad_watcher.AdWatcher()
    recus: list[str] = []
    g.ad_detected.connect(recus.append)
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    g.watch("aypierre", "https://cdn.test/aypierre.m3u8")
    veilleur_aypierre = g._watchers["aypierre"]
    veilleur_aypierre._on_start(veilleur_aypierre.login)
    assert recus == ["aypierre"]


def test_une_pub_detectee_dans_la_boucle_va_jusqu_au_signal(qapp, fils, client_http, horloge):
    """Le chemin complet, du texte M3U8 au signal Qt.

    Les tests précédents éprouvent chaque maillon séparément ; celui-ci vérifie
    qu'ils sont bien reliés — c'est le seul qui casserait si la fabrique de
    veilleurs perdait ses rappels en route.
    """
    g = ad_watcher.AdWatcher()
    recus: list[str] = []
    g.ad_detected.connect(recus.append)
    g.watch("zerator", "https://cdn.test/zerator.m3u8")
    v = g._watchers["zerator"]
    horloge["pas"] = _STARTUP_GRACE
    client_http(*[_Reponse(200, "https://cdn.test/ad_video/1.ts")
                  for _ in range(_AD_CONFIRM)])
    v._stop = _ArretFactice(tours=_AD_CONFIRM)
    v._run()
    assert recus == ["zerator"]
