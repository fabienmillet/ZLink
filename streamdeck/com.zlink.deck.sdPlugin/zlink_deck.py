# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Extension Stream Deck de ZLink.

Le logiciel Elgato lance ce programme et lui parle en WebSocket ; ZLink en
expose un second. Le plugin ne fait rien d'autre que tenir les deux bouts :

    Stream Deck  ⇄  ce plugin  ⇄  ZLink (127.0.0.1:8730)

Aucune logique de régie ici. Une touche pressée devient une commande, un état
reçu devient une image — c'est tout. Ce qui décide de ce qui se passe à l'écran
reste dans ZLink, où c'est déjà écrit et déjà testé.

Deux points méritent d'être sus avant de lire :

**Le plugin survit à ZLink.** Le Stream Deck démarre avec la machine, ZLink
non. La connexion à ZLink est donc reprise indéfiniment, et les touches
s'affichent en gris tant qu'il n'est pas là — plutôt que de laisser le plugin
mourir et le Stream Deck afficher un carré noir jusqu'au prochain
redémarrage du logiciel Elgato.

**Les avatars sont mis en cache.** Une touche se redessine à chaque
changement d'audience, soit toutes les trente secondes par chaîne. Retélécharger
l'image à chaque fois serait absurde ; elle est gardée en mémoire, redimensionnée
une fois.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import pathlib
import sys

import websockets

logger = logging.getLogger("zlink.deck")

#: Où ZLink écoute, et où lire son jeton.
ZLINK_HOTE = "127.0.0.1"
ZLINK_PORT = 8730

#: Côté touche, tout est carré. 144 px couvre les Stream Deck actuels, y
#: compris le XL ; le logiciel réduit lui-même pour les modèles plus petits.
TAILLE_TOUCHE = 144

#: Suffixes des identifiants d'action, tels que le manifeste les déclare.
#:
#: Le logiciel Elgato envoie l'UUID entier — « com.zlink.deck.flux ». On n'en
#: compare que la fin : le préfixe est le nom du plugin, et le renommer ne doit
#: pas faire taire toutes les touches d'un coup.
SUFFIXE_FLUX = ".flux"
SUFFIXE_ACTION = ".action"
SUFFIXE_NAVIGATION = ".navigation"
SUFFIXE_MIXAGE = ".mixage"

#: Gestes dont la touche porte un ÉTAT, et la clé que ZLink publie pour lui.
#:
#: Une touche « Muet » qui montre toujours un haut-parleur barré ne dit pas si
#: le son est coupé — seulement ce qu'elle ferait. On appuyait pour voir.
ETATS_ACTION = {"muet": "muet", "favori": "favori", "chat": "chat"}

#: Taille de l'avatar posé sur une molette. La zone d'icône d'un écran de
#: Stream Deck + est petite : au-delà, on transporte des pixels pour rien.
TAILLE_PASTILLE = 72

#: Ce qu'une touche écrit sur elle-même. Les clés sont celles que ZLink
#: attend : les changer ici ne changerait que le mot affiché.
LIBELLES_ACTION = {
    "clip": "Clip",
    "replay": "Revoir",
    "chat": "Chat",
    "don": "Don",
    "favori": "Favori",
    "muet": "Muet",
}

LIBELLES_NAVIGATION = {
    "precedent": "Précédent",
    "suivant": "Suivant",
    "page_precedente": "Page préc.",
    "page_suivante": "Page suiv.",
}

#: Attente entre deux tentatives de connexion à ZLink. Assez court pour que
#: lancer ZLink rallume les touches sans qu'on ait le temps de s'en étonner,
#: assez long pour ne pas marteler la boucle locale.
RECONNEXION_S = 3.0

#: Un avatar Twitch pèse quelques dizaines de kilo-octets. Au-delà de ce
#: plafond, ce n'est pas un avatar, et rien ne justifie de le charger en
#: mémoire pour le découvrir.
_AVATAR_MAX_OCTETS = 4 * 1024 * 1024


# ── Jeton ────────────────────────────────────────────────────────────────────

def _dossier_du_plugin() -> pathlib.Path:
    """Le dossier installé, pas celui où PyInstaller s'est déplié.

    En exécutable, `__file__` pointe vers un dossier temporaire effacé à la
    fermeture : le journal y serait écrit puis perdu, précisément le jour où
    on en a besoin.
    """
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def _chemins_config() -> list[pathlib.Path]:
    """Où chercher le jeton, du plus sûr au plus incertain.

    `remote.json` est écrit par ZLink à chaque fois qu'il ouvre sa
    télécommande, toujours au même endroit. C'est le seul chemin qui marche
    quand le plugin est installé chez Elgato et ZLink lancé depuis un dépôt
    cloné n'importe où — les suivants ne servent qu'aux cas particuliers, et
    à un plugin exécuté depuis les sources.
    """
    chemins = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        chemins.append(pathlib.Path(appdata) / "ZLink" / "remote.json")
        chemins.append(pathlib.Path(appdata) / "ZLink" / "config.json")
    # Depuis les sources : le plugin est dans streamdeck/<...>.sdPlugin/
    ici = pathlib.Path(__file__).resolve()
    for parent in ici.parents:
        trouves = [parent / nom for nom in ("remote.json", "config.json")
                   if (parent / nom).exists()]
        if trouves:
            chemins.extend(trouves)
            break
    return chemins


#: Dernier rendez-vous annoncé au journal. Le fichier est relu à CHAQUE
#: tentative de connexion — toutes les trois secondes tant que ZLink est
#: fermé — et le journaliser à chaque fois remplirait le fichier de la même
#: ligne des milliers de fois par jour. On ne dit que ce qui change.
_annonce: tuple | None = None


def _annoncer_rendez_vous(chemin, port: int) -> None:
    global _annonce

    if _annonce == (chemin, port):
        return
    _annonce = (chemin, port)
    if chemin is None:
        logger.warning("aucun jeton trouvé : lancer ZLink une fois d'abord")
    else:
        logger.info("jeton lu dans %s (port %d)", chemin, port)


def lire_rendez_vous() -> tuple[str, int]:
    """Jeton et port de la télécommande, tels que ZLink les a laissés.

    On va les chercher plutôt que de les demander : ils sont déjà écrits sur
    la machine, et faire recopier un secret de 43 caractères dans un panneau
    de réglages est le meilleur moyen qu'il finisse copié de travers.

    Rend `("", ZLINK_PORT)` si rien n'a été trouvé — ZLink n'a alors jamais
    tourné sur cette machine, et le plugin attendra qu'il le fasse.
    """
    for chemin in _chemins_config():
        try:
            config = json.loads(chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("config illisible (%s) : %s", chemin, exc)
            continue
        # remote.json est plat, config.json range la même chose sous
        # « remote » : les deux formes sont acceptées.
        bloc = config.get("remote") if isinstance(config.get("remote"), dict)             else config
        jeton_lu = str(bloc.get("token") or "").strip()
        if jeton_lu:
            try:
                port = int(bloc.get("port") or ZLINK_PORT)
            except (TypeError, ValueError):
                port = ZLINK_PORT
            _annoncer_rendez_vous(chemin, port)
            return jeton_lu, port
    _annoncer_rendez_vous(None, ZLINK_PORT)
    return "", ZLINK_PORT


# ── Images de touche ─────────────────────────────────────────────────────────

def _en_image_de_touche(brut: bytes) -> str:
    """Encode des octets PNG comme le Stream Deck attend de les recevoir.

    Les trois fabriques d'images passent par ici : le préfixe et l'encodage
    sont la même convention, et la recopier à chaque fois est le meilleur
    moyen d'en oublier une le jour où elle change.
    """
    return "data:image/png;base64," + base64.b64encode(brut).decode("ascii")


def _encoder_image(image) -> str:
    """Sérialise une image Pillow puis l'encode pour la touche."""
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return _en_image_de_touche(tampon.getvalue())


class Vignettes:
    """Fabrique et garde les images de touche.

    Sans Pillow, le plugin ne dessine pas : il renvoie alors None et les
    touches se contentent de leur titre. C'est volontairement dégradé plutôt
    que fatal — un plugin qui ne démarre pas ne dit rien à personne, un plugin
    sans avatar reste utilisable.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}     # image composée, prête à envoyer
        self._bruts: dict[str, bytes] = {}   # photo d'origine, une par URL
        try:
            from PIL import Image, ImageDraw  # noqa: F401
            self._pil = True
        except ImportError:
            logger.warning("Pillow absent : les touches n'auront pas d'avatar")
            self._pil = False

    def fichier(self, nom: str) -> str:
        """Une image de touche livrée avec le plugin, prête pour `setImage`.

        Rien à composer ici : le PNG est déjà à la bonne définition, il est
        seulement encodé. Gardé après la première lecture — une touche se
        redessine à chaque changement d'audience, et relire le disque toutes
        les trente secondes pour un fichier qui ne bouge jamais serait bête.
        """
        cle = f"fichier|{nom}"
        if cle in self._cache:
            return self._cache[cle]
        chemin = _dossier_du_plugin() / "touches" / f"{nom}.png"
        try:
            brut = chemin.read_bytes()
        except OSError as exc:
            logger.debug("image de touche absente (%s) : %s", chemin, exc)
            self._cache[cle] = ""
            return ""
        encodee = _en_image_de_touche(brut)
        self._cache[cle] = encodee
        return encodee

    async def pastille(self, login: str, url: str, muet: bool) -> str | None:
        """Avatar pour une molette de mixage, terni si la piste est coupée.

        Le ternissement est fait dans l'image plutôt que laissé à l'opacité du
        gabarit : c'est le seul moyen sûr de le voir, quel que soit le
        traitement de `opacity` par le logiciel.
        """
        if not self._pil or not url:
            return None
        # Clé sur l'URL, PAS sur le login : la molette principale porte un
        # login vide, quelle que soit la chaîne au plein écran. La première
        # photo chargée restait donc affichée pour toutes les suivantes.
        cle = f"pastille|{url}|{muet}"
        if cle in self._cache:
            return self._cache[cle]
        brut = await asyncio.to_thread(self._telecharger, url)
        if brut is None:
            return None
        image = self._composer_pastille(brut, muet)
        if image is not None:
            self._cache[cle] = image
        return image

    def _composer_pastille(self, brut: bytes, muet: bool) -> str | None:
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(brut)).convert("RGB")
        except Exception as exc:                          # noqa: BLE001
            logger.debug("avatar illisible : %s", exc)
            return None
        image = image.resize((TAILLE_PASTILLE, TAILLE_PASTILLE))
        if muet:
            gris = image.convert("L").point(lambda v: int(v * 0.35))
            image = Image.merge("RGB", (gris, gris, gris))
        return _encoder_image(image)

    async def avatar(self, login: str, url: str, en_direct: bool,
                     actif: bool) -> str | None:
        """Image encodée en base64 pour `setImage`, ou None."""
        if not self._pil or not url:
            return None
        cle = f"{login}|{en_direct}|{actif}"
        if cle in self._cache:
            return self._cache[cle]
        brut = await asyncio.to_thread(self._telecharger, url)
        if brut is None:
            return None
        image = self._composer(brut, en_direct, actif)
        if image is not None:
            self._cache[cle] = image
        return image

    def _telecharger(self, url: str) -> bytes | None:
        """Récupère l'avatar. Appelé dans un fil : urllib est bloquant.

        Le brut est gardé à part des images composées : la même photo sert aux
        quatre combinaisons d'état (en direct ou non, affichée ou non), et rien
        ne justifie de la retélécharger pour changer une bordure.
        """
        if url in self._bruts:
            return self._bruts[url]
        if not url.lower().startswith("https://"):
            logger.debug("avatar refusé (pas https) : %s", url[:60])
            return None
        import urllib.request

        # Le User-Agent n'est pas décoratif : « Python-urllib/3.x » se fait
        # refuser par les protections anti-robots, et l'avatar manquerait sans
        # qu'on comprenne pourquoi.
        requete = urllib.request.Request(
            url, headers={"User-Agent": "ZLink-Deck/1.0"})
        try:
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                donnees = reponse.read(_AVATAR_MAX_OCTETS + 1)
        except Exception as exc:                          # noqa: BLE001
            logger.debug("avatar %s : %s", url[:60], exc)
            return None
        if len(donnees) > _AVATAR_MAX_OCTETS:
            logger.debug("avatar écarté, trop volumineux : %s", url[:60])
            return None
        self._bruts[url] = donnees
        return donnees

    def _composer(self, brut: bytes, en_direct: bool, actif: bool) -> str | None:
        """Avatar carré, grisé hors direct, cerclé d'orange s'il est en grand."""
        from PIL import Image, ImageDraw

        try:
            image = Image.open(io.BytesIO(brut)).convert("RGB")
        except Exception as exc:                          # noqa: BLE001
            logger.debug("avatar illisible : %s", exc)
            return None
        image = image.resize((TAILLE_TOUCHE, TAILLE_TOUCHE))
        if not en_direct:
            # Une chaîne hors ligne reste lisible mais ne réclame pas l'œil.
            gris = image.convert("L").point(lambda v: int(v * 0.45))
            image = Image.merge("RGB", (gris, gris, gris))
        if actif:
            dessin = ImageDraw.Draw(image)
            dessin.rectangle(
                [(0, 0), (TAILLE_TOUCHE - 1, TAILLE_TOUCHE - 1)],
                outline=(255, 107, 0), width=8)
        return _encoder_image(image)


def _abrege(nombre: int) -> str:
    """Une audience, tenue en quatre caractères."""
    if nombre >= 1_000_000:
        return f"{nombre / 1_000_000:.1f}M"
    if nombre >= 1_000:
        return f"{nombre / 1_000:.1f}k"
    return str(nombre)


# ── Le plugin ────────────────────────────────────────────────────────────────

class Plugin:
    """Tient les deux WebSocket et fait la traduction.

    `_touches` associe le contexte d'une touche — l'identifiant qu'Elgato lui
    donne — à ses réglages. Sans cette table, on ne saurait pas quelle touche
    redessiner quand l'état de ZLink change : Elgato ne les annonce qu'une
    fois, à leur apparition.
    """

    def __init__(self, port: int, uuid: str, evenement: str) -> None:
        self._port = port
        self._uuid = uuid
        self._evenement = evenement
        self._deck = None            # WebSocket vers le logiciel Elgato
        self._zlink = None           # WebSocket vers ZLink
        self._touches: dict[str, dict] = {}
        self._etat: dict = {}
        self._vignettes = Vignettes()
        self._page = 0

    # ── boucles ─────────────────────────────────────────────────────────────

    async def executer(self) -> None:
        """Les deux connexions, menées de front jusqu'à ce qu'Elgato raccroche.

        C'est la fermeture côté Elgato qui termine le plugin : lui seul sait
        quand il n'a plus besoin de nous. ZLink, on l'attend indéfiniment.
        """
        async with websockets.connect(f"ws://127.0.0.1:{self._port}") as deck:
            self._deck = deck
            await self._envoyer_deck({"event": self._evenement,
                                      "uuid": self._uuid})
            logger.info("enregistre aupres du Stream Deck")
            suivi = asyncio.create_task(self._boucle_zlink())
            try:
                await self._boucle_deck()
            finally:
                suivi.cancel()

    async def _boucle_deck(self) -> None:
        async for trame in self._deck:
            try:
                await self._sur_evenement(json.loads(trame))
            except Exception:                             # noqa: BLE001
                logger.exception("evenement Stream Deck non traite")

    async def _boucle_zlink(self) -> None:
        """Reprend la connexion à ZLink indéfiniment.

        Le Stream Deck démarre avec la machine, ZLink non — et ZLink se ferme
        et se rouvre pendant une session. Abandonner à la première fermeture
        laisserait des touches mortes jusqu'au prochain redémarrage du logiciel
        Elgato.
        """
        while True:
            # Relu à chaque tour : au premier, ZLink n'a peut-être jamais
            # tourné et le fichier n'existe pas encore.
            jeton, port = lire_rendez_vous()
            try:
                async with websockets.connect(
                        f"ws://{ZLINK_HOTE}:{port}") as zlink:
                    self._zlink = zlink
                    await zlink.send(json.dumps({"jeton": jeton}))
                    logger.info("connecte a ZLink")
                    async for trame in zlink:
                        await self._sur_etat(json.loads(trame))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # noqa: BLE001
                logger.debug("ZLink injoignable (%s), nouvel essai", exc)
            finally:
                self._zlink = None
            self._etat = {}
            await self._rafraichir()
            await asyncio.sleep(RECONNEXION_S)

    # ── réception ───────────────────────────────────────────────────────────

    async def _sur_evenement(self, message: dict) -> None:
        evenement = message.get("event")
        contexte = message.get("context", "")
        reglages = (message.get("payload") or {}).get("settings") or {}

        if evenement in ("willAppear", "didReceiveSettings"):
            self._touches[contexte] = {
                "action": message.get("action", ""),
                "reglages": reglages,
            }
            await self._peindre(contexte, self._touches[contexte])
        elif evenement == "willDisappear":
            self._touches.pop(contexte, None)
        elif evenement == "keyDown":
            await self._sur_appui(self._touches.get(contexte, {}), reglages)
        elif evenement == "dialRotate":
            crans = int((message.get("payload") or {}).get("ticks", 0))
            await self._sur_molette(reglages, crans)
        elif evenement in ("dialDown", "touchTap"):
            await self._sur_appui_molette(reglages)

    async def _sur_etat(self, message: dict) -> None:
        if message.get("type") != "etat":
            return
        self._etat = message
        await self._rafraichir()

    # ── envoi vers ZLink ────────────────────────────────────────────────────

    async def _commander(self, commande: dict) -> None:
        """Une commande, si ZLink est là.

        Sinon rien : les touches sont déjà grisées, et insister n'apporterait
        qu'une ligne d'erreur de plus au journal.
        """
        if self._zlink is None:
            logger.debug("commande ignoree, ZLink absent : %s", commande)
            return
        try:
            await self._zlink.send(json.dumps(commande))
        except Exception as exc:                          # noqa: BLE001
            logger.debug("commande non transmise : %s", exc)

    async def _sur_appui(self, touche: dict, reglages: dict) -> None:
        action = touche.get("action", "")
        if action.endswith(SUFFIXE_FLUX):
            await self._appui_flux(reglages)
        elif action.endswith(SUFFIXE_ACTION):
            await self._commander({"commande": "action",
                                   "cle": reglages.get("cle", "clip")})
        elif action.endswith(SUFFIXE_NAVIGATION):
            await self._naviguer(reglages.get("sens", "suivant"))

    async def _appui_flux(self, reglages: dict) -> None:
        cellules = self._etat.get("cellules") or []
        index = self._index_de_flux(reglages)
        if 0 <= index < len(cellules):
            await self._commander({"commande": "chaine",
                                   "login": cellules[index]["login"]})

    async def _naviguer(self, sens: str) -> None:
        if sens in ("precedent", "suivant"):
            await self._commander({"commande": "voisin",
                                   "pas": 1 if sens == "suivant" else -1})
            return
        # Pagination : le Stream Deck a moins de touches que la grille n'a de
        # cellules, et une chaîne inatteignable est une chaîne perdue.
        pages = self._nombre_de_pages()
        pas = 1 if sens == "page_suivante" else -1
        self._page = (self._page + pas) % pages
        await self._rafraichir()

    async def _sur_molette(self, reglages: dict, crans: int) -> None:
        """Une molette tourne : cinq points par cran, comme les touches +/-."""
        login = self._login_de_molette(reglages)
        vise = max(0, min(100, self._volume_de(login) + crans * 5))
        if login:
            await self._commander({"commande": "volume_chaine",
                                   "login": login, "valeur": vise})
        else:
            await self._commander({"commande": "volume", "valeur": vise})

    async def _sur_appui_molette(self, reglages: dict) -> None:
        login = self._login_de_molette(reglages)
        muet = not bool(self._etat.get("muet", False))
        if login:
            await self._commander({"commande": "muet_chaine",
                                   "login": login, "valeur": muet})
        else:
            await self._commander({"commande": "muet", "valeur": muet})

    # ── affichage ───────────────────────────────────────────────────────────

    async def _rafraichir(self) -> None:
        # Une COPIE de la table : `_peindre` attend le socket, et pendant
        # cette attente la boucle Elgato peut traiter un « willDisappear » et
        # retirer une touche. Itérer la table elle-même lèverait alors
        # « dictionary changed size during iteration ».
        for contexte, touche in list(self._touches.items()):  # NOSONAR
            await self._peindre(contexte, touche)

    async def _peindre(self, contexte: str, touche: dict) -> None:
        action = touche.get("action", "")
        reglages = touche.get("reglages") or {}
        if action.endswith(SUFFIXE_FLUX):
            await self._peindre_flux(contexte, reglages)
        elif action.endswith(SUFFIXE_MIXAGE):
            await self._peindre_molette(contexte, reglages)
        elif action.endswith(SUFFIXE_ACTION):
            await self._peindre_geste(contexte, reglages, "action",
                                      LIBELLES_ACTION, "clip")
        elif action.endswith(SUFFIXE_NAVIGATION):
            await self._peindre_geste(contexte, reglages, "navigation",
                                      LIBELLES_NAVIGATION, "suivant")

    async def _peindre_geste(self, contexte: str, reglages: dict,
                             famille: str, libelles: dict, defaut: str) -> None:
        """Donne à la touche le dessin et le mot de ce qu'elle fait.

        Sans cela, six touches « Action » côte à côte portent le même éclair
        et deviennent indiscernables. Le glyphe suit le réglage, le libellé
        aussi — ce dernier peut être refusé par qui a écrit son propre titre
        et ne veut pas le voir remplacé.
        """
        cle = str(reglages.get("cle") or reglages.get("sens") or defaut)
        nom = f"{famille}-{cle}"
        etat = ETATS_ACTION.get(cle) if famille == "action" else None
        image = ""
        if etat and self._etat.get(etat):
            image = self._vignettes.fichier(f"{nom}-actif")
        image = image or self._vignettes.fichier(nom)
        if image:
            await self._envoyer_deck({"event": "setImage", "context": contexte,
                                      "payload": {"image": image}})
        if reglages.get("libelle", True):
            await self._envoyer_deck({"event": "setTitle", "context": contexte,
                                      "payload": {"title": libelles.get(cle, "")}})

    async def _peindre_flux(self, contexte: str, reglages: dict) -> None:
        cellules = self._etat.get("cellules") or []
        index = self._index_de_flux(reglages)
        if not 0 <= index < len(cellules):
            await self._vider(contexte)
            return
        cellule = cellules[index]
        actif = cellule.get("login") == self._etat.get("actif")
        titre = str(cellule.get("login", ""))[:10]
        if cellule.get("online"):
            titre += "\n" + _abrege(int(cellule.get("viewers", 0) or 0))
        await self._envoyer_deck({"event": "setTitle", "context": contexte,
                                  "payload": {"title": titre}})
        image = await self._vignettes.avatar(
            str(cellule.get("login", "")), str(cellule.get("avatar", "")),
            bool(cellule.get("online")), actif)
        if image:
            await self._envoyer_deck({"event": "setImage", "context": contexte,
                                      "payload": {"image": image}})

    async def _vider(self, contexte: str) -> None:
        """Une touche sans cellule : ni titre, ni image d'une chaîne d'avant."""
        await self._envoyer_deck({"event": "setTitle", "context": contexte,
                                  "payload": {"title": ""}})
        await self._envoyer_deck({"event": "setImage", "context": contexte,
                                  "payload": {"image": ""}})

    async def _peindre_molette(self, contexte: str, reglages: dict) -> None:
        login = self._login_de_molette(reglages)
        volume = self._volume_de(login)
        muet = self._muet_de(login)
        # Une piste coupée reste lisible, mais ne réclame plus l'œil : c'est
        # ce que fait l'opacité. La barre tombe à zéro parce qu'elle montre ce
        # qu'on ENTEND, pas le réglage retenu pour le retour du son.
        voile = 0.4 if muet else 1.0
        retour = {
            "title": {"value": login or "Plein ecran", "opacity": voile},
            "value": {"value": "Muet" if muet else f"{volume} %",
                      "opacity": voile},
            "indicator": {"value": 0 if muet else volume, "opacity": voile},
        }
        icone = await self._vignettes.pastille(
            login, self._avatar_de(login), muet)
        if icone:
            retour["icon"] = icone
        await self._envoyer_deck({"event": "setFeedback", "context": contexte,
                                  "payload": retour})

    async def _envoyer_deck(self, message: dict) -> None:
        if self._deck is None:
            return
        await self._deck.send(json.dumps(message))

    # ── lecture de l'état ───────────────────────────────────────────────────

    def _index_de_flux(self, reglages: dict) -> int:
        """Rang de la cellule que cette touche montre, pagination comprise."""
        try:
            rang = int(reglages.get("rang", 0))
        except (TypeError, ValueError):
            rang = 0
        return self._page * self._touches_de_flux() + rang

    def _touches_de_flux(self) -> int:
        """Combien de touches « Flux » sont posées.

        C'est ce qui définit la taille d'une page : l'utilisateur en met
        treize sur un 5×3, huit sur un Stream Deck +, et la pagination doit
        suivre sans qu'il ait à le déclarer nulle part.
        """
        combien = sum(1 for t in self._touches.values()
                      if t.get("action", "").endswith(SUFFIXE_FLUX))
        return max(1, combien)

    def _nombre_de_pages(self) -> int:
        cellules = len(self._etat.get("cellules") or [])
        par_page = self._touches_de_flux()
        return max(1, -(-cellules // par_page))    # arrondi au supérieur

    def _login_de_molette(self, reglages: dict) -> str:
        """Chaîne pilotée par une molette. Vide = le son du plein écran."""
        cible = str(reglages.get("cible", "principal"))
        if cible == "principal":
            return ""
        epingles = [str(c.get("login", ""))
                    for c in (self._etat.get("cellules") or [])
                    if c.get("epingle")]
        try:
            return epingles[int(cible)]
        except (ValueError, IndexError):
            return ""

    def _muet_de(self, login: str) -> bool:
        if not login:
            return bool(self._etat.get("muet", False))
        for cellule in self._etat.get("cellules") or []:
            if cellule.get("login") == login:
                return bool(cellule.get("muet", False))
        return False

    def _avatar_de(self, login: str) -> str:
        """URL de l'avatar d'une piste. Vide = celui de la chaîne au plein écran."""
        vise = login or str(self._etat.get("actif") or "")
        for cellule in self._etat.get("cellules") or []:
            if cellule.get("login") == vise:
                return str(cellule.get("avatar") or "")
        return ""

    def _volume_de(self, login: str) -> int:
        if not login:
            return int(self._etat.get("volume", 0) or 0)
        for cellule in self._etat.get("cellules") or []:
            if cellule.get("login") == login:
                return int(cellule.get("volume", 100) or 0)
        return 100


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
        filename=str(_dossier_du_plugin() / "zlink-deck.log"),
    )
    # Elgato passe ses arguments avec UN seul tiret : « -port 1234 ». argparse
    # l'accepte, à condition de déclarer les options sous cette forme.
    analyseur = argparse.ArgumentParser(add_help=False)
    analyseur.add_argument("-port", type=int, required=True)
    analyseur.add_argument("-pluginUUID", required=True)
    analyseur.add_argument("-registerEvent", required=True)
    analyseur.add_argument("-info", default="")
    options, _ignores = analyseur.parse_known_args()

    plugin = Plugin(options.port, options.pluginUUID, options.registerEvent)
    try:
        asyncio.run(plugin.executer())
    except KeyboardInterrupt:
        return 0
    except Exception:                                     # noqa: BLE001
        logger.exception("le plugin s'est arrete")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
