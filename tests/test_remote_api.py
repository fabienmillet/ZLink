# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Télécommande locale : ce qu'elle accepte, et surtout ce qu'elle refuse.

Une télécommande qui coupe le son et change de flux est une surface d'attaque,
si modeste soit-elle. Trois garanties sont éprouvées ici comme des exigences,
pas comme des détails :

- elle n'écoute QUE sur la boucle locale ;
- une connexion sans jeton valable est fermée, et ses commandes ne sont jamais
  exécutées ;
- une trame illisible ou démesurée ne fait pas tomber le serveur.

Les échanges passent par de vrais QWebSocket, pas par des doublures : c'est le
protocole qu'on vérifie, pas seulement l'aiguillage.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtWebSockets import QWebSocket

from core import remote_api

JETON = "jeton-de-test-qui-ne-sert-qu-ici"


@pytest.fixture(autouse=True)
def dossier_utilisateur_isole(tmp_path, monkeypatch):
    """Détourne le fichier de rendez-vous vers un dossier jetable.

    `demarrer()` l'écrit à chaque écoute réussie : sans ce détournement, la
    suite de tests écraserait le jeton de la vraie installation, et la
    télécommande de l'utilisateur cesserait de répondre à son Stream Deck.
    """
    monkeypatch.setattr(remote_api, "USER_DATA_ROOT", tmp_path / "ZLink")
    return tmp_path / "ZLink"


@pytest.fixture
def serveur(qtbot):
    api = remote_api.RemoteAPI(jeton_attendu=JETON)
    # Port 0 : le système en attribue un libre. Deux tests qui se disputent un
    # port fixe échouent au hasard de l'ordre d'exécution.
    assert api.demarrer(0), "le serveur doit pouvoir écouter"
    yield api
    api.arreter()


def _client(qtbot, api, jeton=JETON, authentifie=True):
    """Un client connecté, authentifié par défaut."""
    ws = QWebSocket()
    recues: list = []
    ws.textMessageReceived.connect(recues.append)
    ws.ws_recues = recues            # type: ignore[attr-defined]

    with qtbot.waitSignal(ws.connected, timeout=3000):
        ws.open(QUrl(f"ws://127.0.0.1:{api.port}"))
    if authentifie:
        ws.sendTextMessage(json.dumps({"jeton": jeton}))
        qtbot.waitUntil(lambda: api.clients == 1, timeout=3000)
    return ws


# ── écoute ───────────────────────────────────────────────────────────────────

def test_le_serveur_n_ecoute_que_sur_la_boucle_locale(serveur):
    """Une télécommande n'a rien à faire sur un réseau de LAN party."""
    assert serveur._serveur.serverAddress() == QHostAddress(
        QHostAddress.SpecialAddress.LocalHost)


def test_un_port_indisponible_ne_fait_pas_tomber_zlink(qtbot):
    """ZLink fonctionne sans télécommande : l'échec se journalise, c'est tout."""
    premier = remote_api.RemoteAPI(jeton_attendu=JETON)
    assert premier.demarrer(0)
    second = remote_api.RemoteAPI(jeton_attendu=JETON)
    try:
        assert second.demarrer(premier.port) is False
    finally:
        premier.arreter()
        second.arreter()


def test_arreter_est_idempotent(serveur):
    serveur.arreter()
    serveur.arreter()
    assert serveur.port == 0


# ── authentification ─────────────────────────────────────────────────────────

def test_un_jeton_valable_authentifie(qtbot, serveur):
    _client(qtbot, serveur)
    assert serveur.clients == 1


def test_un_mauvais_jeton_ferme_la_connexion(qtbot, serveur):
    ws = _client(qtbot, serveur, jeton="pas le bon", authentifie=False)
    with qtbot.waitSignal(ws.disconnected, timeout=3000):
        ws.sendTextMessage(json.dumps({"jeton": "pas le bon"}))
    assert serveur.clients == 0


def test_une_commande_sans_authentification_n_est_jamais_executee(qtbot, serveur):
    """Le cœur du garde-fou : se connecter ne suffit pas à commander."""
    recus: list = []
    serveur.slot_demande.connect(recus.append)
    ws = _client(qtbot, serveur, authentifie=False)
    ws.sendTextMessage(json.dumps({"commande": "slot", "index": 3}))
    qtbot.wait(200)
    assert recus == []
    ws.close()


def test_le_jeton_se_compare_sans_fuite_de_temps():
    """`==` s'arrête au premier octet qui diffère : le temps qu'il met
    renseigne sur le préfixe correct."""
    import inspect
    source = inspect.getsource(remote_api.RemoteAPI._authentifier)
    assert "compare_digest" in source


# ── commandes ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trame,signal,attendu", [
    ({"commande": "slot", "index": 4}, "slot_demande", 4),
    ({"commande": "voisin", "pas": 1}, "voisin_demande", 1),
    ({"commande": "voisin", "pas": -1}, "voisin_demande", -1),
    ({"commande": "action", "cle": "clip"}, "action_demandee", "clip"),
    ({"commande": "chaine", "login": "zerator"}, "chaine_demandee", "zerator"),
    ({"commande": "volume", "valeur": 42}, "volume_demande", 42),
    ({"commande": "muet", "valeur": True}, "muet_demande", True),
])
def test_une_commande_devient_un_signal(qtbot, serveur, trame, signal, attendu):
    recus: list = []
    getattr(serveur, signal).connect(recus.append)
    ws = _client(qtbot, serveur)
    ws.sendTextMessage(json.dumps(trame))
    qtbot.waitUntil(lambda: bool(recus), timeout=3000)
    assert recus == [attendu]
    ws.close()


def test_les_commandes_de_console_portent_leur_chaine(qtbot, serveur):
    recus: list = []
    serveur.volume_chaine_demande.connect(lambda lg, v: recus.append((lg, v)))
    ws = _client(qtbot, serveur)
    ws.sendTextMessage(json.dumps(
        {"commande": "volume_chaine", "login": "mistermv", "valeur": 30}))
    qtbot.waitUntil(lambda: bool(recus), timeout=3000)
    assert recus == [("mistermv", 30)]
    ws.close()


@pytest.mark.parametrize("valeur,attendu", [(-20, 0), (0, 0), (150, 100)])
def test_un_volume_hors_bornes_est_ramene(qtbot, serveur, valeur, attendu):
    recus: list = []
    serveur.volume_demande.connect(recus.append)
    ws = _client(qtbot, serveur)
    ws.sendTextMessage(json.dumps({"commande": "volume", "valeur": valeur}))
    qtbot.waitUntil(lambda: bool(recus), timeout=3000)
    assert recus == [attendu]
    ws.close()


# ── robustesse ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trame", [
    "pas du json",
    "[1, 2, 3]",                                  # json valide, pas un objet
    '{"commande": "slot"}',                       # argument manquant
    '{"commande": "slot", "index": "quatre"}',    # argument du mauvais type
    '{"commande": "inconnue"}',
    "{}",
])
def test_une_trame_bancale_ne_fait_pas_tomber_le_serveur(qtbot, serveur, trame):
    ws = _client(qtbot, serveur)
    ws.sendTextMessage(trame)
    qtbot.wait(150)
    assert serveur.clients == 1, "la connexion reste ouverte"
    ws.close()


def test_une_trame_demesuree_est_rejetee(qtbot, serveur):
    """Une commande fait quelques dizaines d'octets ; au-delà, ce n'en est pas
    une, et rien ne justifie de la désérialiser."""
    ws = _client(qtbot, serveur)
    ws.sendTextMessage("x" * (64 * 1024 + 1))
    # On attend que le SERVEUR ait oublié le client : sa fermeture et le signal
    # reçu côté client ne tombent pas forcément dans le même tour de boucle.
    qtbot.waitUntil(lambda: serveur.clients == 0, timeout=3000)


# ── état ─────────────────────────────────────────────────────────────────────

def test_l_etat_est_pousse_aux_clients(qtbot, serveur):
    ws = _client(qtbot, serveur)
    serveur.publier_etat({"actif": "zerator", "volume": 80})
    qtbot.waitUntil(lambda: bool(ws.ws_recues), timeout=3000)
    recu = json.loads(ws.ws_recues[-1])
    assert recu["type"] == "etat"
    assert recu["actif"] == "zerator"
    ws.close()


def test_un_client_qui_arrive_apres_coup_recoit_l_etat_courant(qtbot, serveur):
    """Sinon les touches resteraient vides jusqu'au prochain changement."""
    serveur.publier_etat({"actif": "domingo"})
    ws = _client(qtbot, serveur)
    qtbot.waitUntil(lambda: bool(ws.ws_recues), timeout=3000)
    assert json.loads(ws.ws_recues[-1])["actif"] == "domingo"
    ws.close()


def test_publier_sans_client_ne_leve_pas(serveur):
    serveur.publier_etat({"actif": "personne"})


# ── jeton ────────────────────────────────────────────────────────────────────

def test_le_jeton_est_tire_au_sort_et_conserve(tmp_path, monkeypatch):
    """Un secret qu'il faut inventer soi-même finit en « 1234 »."""
    ecrits: dict = {}
    monkeypatch.setattr(remote_api.config_store, "load", lambda: {})
    monkeypatch.setattr(remote_api.config_store, "save_merge",
                        lambda patch: ecrits.update(patch) or True)
    premier = remote_api.jeton()
    assert len(premier) >= 32
    assert ecrits["remote"]["token"] == premier


def test_un_jeton_existant_n_est_pas_remplace(monkeypatch):
    monkeypatch.setattr(remote_api.config_store, "load",
                        lambda: {"remote": {"token": "deja-la"}})
    monkeypatch.setattr(remote_api.config_store, "save_merge",
                        lambda patch: pytest.fail("ne doit pas réécrire"))
    assert remote_api.jeton() == "deja-la"


# ── rendez-vous ──────────────────────────────────────────────────────────────

def test_l_ecoute_laisse_un_rendez_vous(qtbot, dossier_utilisateur_isole):
    """Le plugin Stream Deck n'a que ce fichier pour trouver ZLink.

    Installé chez Elgato, il ne peut pas deviner où le dépôt est cloné : sans
    ce dépôt à un endroit fixe, un ZLink lancé depuis les sources reste
    invisible pour lui.
    """
    api = remote_api.RemoteAPI(jeton_attendu=JETON)
    assert api.demarrer(0)
    try:
        fichier = dossier_utilisateur_isole / remote_api.NOM_RENDEZ_VOUS
        assert fichier.exists(), "aucun rendez-vous écrit"
        contenu = json.loads(fichier.read_text(encoding="utf-8"))
        assert contenu["token"] == JETON
        assert contenu["port"] == api._serveur.serverPort()
    finally:
        api.arreter()


def test_un_rendez_vous_impossible_a_ecrire_n_empeche_pas_l_ecoute(
        qtbot, monkeypatch, tmp_path):
    """Un dossier en lecture seule ne doit pas priver ZLink de télécommande."""
    # Un FICHIER là où le code attend un dossier : mkdir lève, comme le
    # ferait un disque plein ou un dossier interdit en écriture.
    obstacle = tmp_path / "pas-un-dossier"
    obstacle.write_text("", encoding="utf-8")
    monkeypatch.setattr(remote_api, "USER_DATA_ROOT", obstacle)
    api = remote_api.RemoteAPI(jeton_attendu=JETON)
    try:
        assert api.demarrer(0), "l'écoute doit survivre à l'échec d'écriture"
    finally:
        api.arreter()


def test_le_rendez_vous_suit_le_port_reellement_obtenu(
        qtbot, dossier_utilisateur_isole):
    """Le port par défaut peut être pris : le plugin doit suivre le vrai."""
    api = remote_api.RemoteAPI(jeton_attendu=JETON)
    assert api.demarrer(0)
    try:
        contenu = json.loads(
            (dossier_utilisateur_isole / remote_api.NOM_RENDEZ_VOUS)
            .read_text(encoding="utf-8"))
        assert contenu["port"] != remote_api.PORT_DEFAUT
    finally:
        api.arreter()
