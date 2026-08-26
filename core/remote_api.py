# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Télécommande locale de ZLink, en WebSocket.

Une extension Stream Deck est un processus SÉPARÉ, lancé par le logiciel
Elgato : elle ne peut pas appeler ZLink dans son processus. Il faut donc que
ZLink écoute. Ce module est ce point d'entrée — et le seul.

Trois décisions, et les trois comptent :

**QWebSocketServer, pas une bibliothèque tierce.** Le serveur vit dans la
boucle d'événements de Qt, celle qui possède déjà les fenêtres. Une pile
asyncio dans un fil séparé aurait imposé de faire traverser chaque commande
d'un fil à l'autre, pour un gain nul.

**127.0.0.1 uniquement.** Le serveur n'écoute JAMAIS sur une adresse joignable
depuis le réseau. Une télécommande qui peut couper le son et changer de flux
n'a rien à faire sur un réseau de LAN party.

**Un jeton, obligatoire.** La première trame d'un client doit le porter, sinon
la connexion est fermée sans autre forme de procès. Il est tiré au sort à la
première utilisation et rangé dans config.json. Le local n'est pas une
frontière : n'importe quel programme de la machine peut ouvrir une connexion
sur la boucle locale.

Le module ne connaît AUCUNE fenêtre : il traduit des trames en signaux Qt, et
main.py les branche sur ce qui existe déjà. C'est ce qui permet de le tester
sans interface.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtWebSockets import QWebSocketServer

from core import config_store
from core.paths import USER_DATA_ROOT

logger = logging.getLogger(__name__)

#: Port d'écoute par défaut. Au-dessus des ports réservés, et sans usage
#: répandu qui risquerait de le disputer.
PORT_DEFAUT = 8730

#: Fichier de rendez-vous, écrit à chaque écoute réussie.
#:
#: L'extension Stream Deck est installée dans le dossier du logiciel Elgato :
#: de là, elle ne voit pas le config.json d'un ZLink lancé depuis les sources,
#: qui est resté dans le dépôt. Ce fichier-ci est toujours au même endroit,
#: quelle que soit la manière dont ZLink a démarré, et porte le port en plus
#: du jeton — le jour où le port par défaut est pris, la télécommande suit
#: sans qu'on ait à la reconfigurer.
NOM_RENDEZ_VOUS = "remote.json"

#: Longueur du jeton, en octets tirés au sort avant encodage.
_OCTETS_JETON = 32

#: Taille maximale d'une trame acceptée. Une commande fait quelques dizaines
#: d'octets ; au-delà, ce n'en est pas une.
_TAILLE_MAX = 64 * 1024


def jeton(config: dict | None = None) -> str:
    """Jeton de la télécommande, tiré au sort à la première demande.

    Rangé dans config.json sous `remote.token`. On le fabrique plutôt que de
    le demander à l'utilisateur : un secret qu'il faut inventer soi-même finit
    en « 1234 ».
    """
    config = config if config is not None else config_store.load()
    existant = str((config.get("remote") or {}).get("token") or "").strip()
    if existant:
        return existant
    neuf = secrets.token_urlsafe(_OCTETS_JETON)
    config_store.save_merge({"remote": {"token": neuf}})
    logger.info("Télécommande : jeton créé dans config.json")
    return neuf


def _ecrire_rendez_vous(port: int, jeton_courant: str) -> None:
    """Dépose port et jeton là où l'extension sait regarder.

    Un échec n'empêche rien : la télécommande écoute déjà, et le plugin sait
    aussi lire config.json. On le note, on continue.
    """
    try:
        dossier = USER_DATA_ROOT
        dossier.mkdir(parents=True, exist_ok=True)
        (dossier / NOM_RENDEZ_VOUS).write_text(
            json.dumps({"port": port, "token": jeton_courant}),
            encoding="utf-8")
    except OSError as exc:
        logger.warning("Télécommande : rendez-vous non écrit — %s", exc)


class RemoteAPI(QObject):
    """Serveur de la télécommande. Traduit des trames en signaux Qt.

    Les signaux portent EXACTEMENT les gestes que les fenêtres savent déjà
    faire : afficher la cellule N, passer à la voisine, garder le moment,
    régler le son. Rien de neuf n'est inventé ici.
    """

    #: Index 0-based d'une cellule de la grille à passer en plein écran.
    slot_demande = pyqtSignal(int)
    #: -1 ou +1 — flux précédent ou suivant dans l'ordre de la grille.
    voisin_demande = pyqtSignal(int)
    #: Clé d'action : « clip », « replay », « recap ».
    action_demandee = pyqtSignal(str)
    #: Volume du plein écran, 0-100.
    volume_demande = pyqtSignal(int)
    #: Coupure du plein écran.
    muet_demande = pyqtSignal(bool)
    #: (login, volume) — une piste de la console de mixage.
    volume_chaine_demande = pyqtSignal(str, int)
    #: (login, coupé) — coupure d'une piste de la console.
    muet_chaine_demande = pyqtSignal(str, bool)
    #: Une chaîne demandée par son login, plutôt que par son rang.
    chaine_demandee = pyqtSignal(str)

    def __init__(self, jeton_attendu: str = "",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jeton = jeton_attendu or jeton()
        self._serveur: QWebSocketServer | None = None
        self._clients: list = []          # clients AUTHENTIFIÉS seulement
        self._en_attente: list = []       # connectés, pas encore authentifiés
        self._dernier_etat: dict = {}

    # ── cycle de vie ────────────────────────────────────────────────────────

    def demarrer(self, port: int = PORT_DEFAUT) -> bool:
        """Ouvre l'écoute sur la boucle locale. Rend False en cas d'échec.

        Un échec n'est pas fatal : ZLink fonctionne sans télécommande, et le
        port peut être pris par une autre instance.
        """
        serveur = QWebSocketServer("ZLink", QWebSocketServer.SslMode.NonSecureMode,
                                   self)
        if not serveur.listen(QHostAddress.SpecialAddress.LocalHost, port):
            logger.warning("Télécommande : écoute impossible sur %d — %s",
                           port, serveur.errorString())
            return False
        serveur.newConnection.connect(self._sur_connexion)
        self._serveur = serveur
        # Le port RÉELLEMENT obtenu : demander 0 laisse le système en
        # choisir un, et c'est celui-là que le plugin doit trouver écrit.
        obtenu = serveur.serverPort()
        logger.info("Télécommande à l'écoute sur 127.0.0.1:%d", obtenu)
        _ecrire_rendez_vous(obtenu, self._jeton)
        return True

    def arreter(self) -> None:
        """Ferme l'écoute et toutes les connexions. Idempotent."""
        for client in list(self._clients) + list(self._en_attente):
            client.close()
        self._clients.clear()
        self._en_attente.clear()
        if self._serveur is not None:
            self._serveur.close()
            self._serveur = None

    @property
    def port(self) -> int:
        """Port réellement obtenu, 0 si le serveur n'écoute pas."""
        return int(self._serveur.serverPort()) if self._serveur else 0

    @property
    def clients(self) -> int:
        """Nombre de clients authentifiés."""
        return len(self._clients)

    # ── connexions ──────────────────────────────────────────────────────────

    def _sur_connexion(self) -> None:
        if self._serveur is None:
            return
        client = self._serveur.nextPendingConnection()
        if client is None:
            return
        self._en_attente.append(client)
        client.textMessageReceived.connect(
            lambda trame, c=client: self._sur_trame(c, trame))
        client.disconnected.connect(lambda c=client: self._oublier(c))

    def _oublier(self, client) -> None:
        for liste in (self._clients, self._en_attente):
            if client in liste:
                liste.remove(client)
        client.deleteLater()

    def _sur_trame(self, client, trame: str) -> None:
        """Une trame arrive : d'abord l'authentification, ensuite les commandes."""
        if len(trame) > _TAILLE_MAX:
            logger.warning("Télécommande : trame de %d octets rejetée", len(trame))
            client.close()
            return
        try:
            message = json.loads(trame)
        except (ValueError, TypeError):
            logger.warning("Télécommande : trame illisible, ignorée")
            return
        if not isinstance(message, dict):
            return

        if client in self._en_attente:
            self._authentifier(client, message)
            return
        if client in self._clients:
            self._executer(message)

    def _authentifier(self, client, message: dict) -> None:
        """Première trame : le jeton, ou rien.

        `compare_digest` plutôt que `==` : la comparaison naïve s'arrête au
        premier octet qui diffère, et le temps qu'elle met renseigne sur le
        préfixe correct.
        """
        fourni = str(message.get("jeton") or "")
        if not secrets.compare_digest(fourni, self._jeton):
            logger.warning("Télécommande : jeton refusé, connexion fermée")
            client.close()
            return
        self._en_attente.remove(client)
        self._clients.append(client)
        logger.info("Télécommande : client authentifié (%d connecté(s))",
                    len(self._clients))
        # L'état courant tout de suite : sans lui, les touches resteraient
        # vides jusqu'au premier changement.
        if not self._dernier_etat:
            logger.warning("Télécommande : aucun état à envoyer au client")
            return
        trame = json.dumps(self._dernier_etat, ensure_ascii=False)
        client.sendTextMessage(trame)
        logger.info("Télécommande : état initial envoyé (%d octets)", len(trame))

    # ── commandes ───────────────────────────────────────────────────────────

    def _executer(self, message: dict) -> None:
        """Traduit une commande en signal. Une commande inconnue est ignorée."""
        commande = str(message.get("commande") or "")
        try:
            self._router(commande, message)
        except (KeyError, TypeError, ValueError) as exc:
            # KeyError : argument absent. TypeError/ValueError : argument du
            # mauvais type. Aucun des trois ne justifie de fermer la connexion
            # — une télécommande qui bafouille ne doit pas se faire raccrocher
            # au nez au milieu d'un direct.
            logger.warning("Télécommande : commande « %s » mal formée — %s",
                           commande, exc)

    def _router(self, commande: str, message: dict) -> None:
        if commande == "slot":
            self.slot_demande.emit(int(message["index"]))
        elif commande == "voisin":
            self.voisin_demande.emit(1 if int(message["pas"]) >= 0 else -1)
        elif commande == "action":
            self.action_demandee.emit(str(message["cle"]))
        elif commande == "chaine":
            self.chaine_demandee.emit(str(message["login"]))
        elif commande == "volume":
            self.volume_demande.emit(_borne(message["valeur"]))
        elif commande == "muet":
            self.muet_demande.emit(bool(message["valeur"]))
        elif commande == "volume_chaine":
            self.volume_chaine_demande.emit(str(message["login"]),
                                            _borne(message["valeur"]))
        elif commande == "muet_chaine":
            self.muet_chaine_demande.emit(str(message["login"]),
                                          bool(message["valeur"]))
        else:
            logger.debug("Télécommande : commande inconnue « %s »", commande)

    # ── état ────────────────────────────────────────────────────────────────

    def publier_etat(self, etat: dict[str, Any]) -> None:
        """Envoie l'état courant à tous les clients authentifiés.

        Conservé pour le prochain client qui se connecte : une télécommande
        branchée après coup doit afficher quelque chose sans attendre qu'un
        streamer change de jeu.
        """
        self._dernier_etat = {"type": "etat", **etat}
        if not self._clients:
            return
        trame = json.dumps(self._dernier_etat, ensure_ascii=False)
        for client in list(self._clients):
            client.sendTextMessage(trame)


def _borne(valeur: object) -> int:
    """Un volume, ramené entre 0 et 100."""
    return max(0, min(100, int(valeur)))
