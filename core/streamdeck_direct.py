# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Stream Deck piloté en direct, sans logiciel Elgato — la voie Linux.

Sous Windows et macOS, le boîtier appartient au logiciel Elgato : il ouvre le
périphérique USB en exclusif, et un tiers qui veut y écrire doit passer par une
extension qu'il lance lui-même. D'où `streamdeck/` et la télécommande
WebSocket de `core/remote_api.py`.

Ce logiciel n'existe pas sous Linux. Le boîtier y est un simple périphérique
HID, que personne ne réclame : ZLink peut donc l'ouvrir lui-même.

    Stream Deck (hidraw)  ⇄  ce module  ⇄  ZLink, dans le même processus

Ce que ça supprime : l'extension à installer, le second exécutable à
construire, le jeton à déposer, le WebSocket sur la boucle locale, et les
profils à importer. On branche, on lance ZLink, les touches s'allument.

Trois décisions, et les trois comptent :

**Les mêmes signaux que la télécommande.** La classe expose EXACTEMENT les
signaux de `RemoteAPI`, et `publier_etat()` accepte le même dictionnaire.
main.py branche l'une ou l'autre sans savoir laquelle. Aucun geste de régie
n'est réécrit ici : ce module traduit des appuis en signaux et un état en
images, rien de plus — c'est déjà la règle du plugin Elgato, elle ne change pas
parce que le transport change.

**La disposition suit le modèle du boîtier.** Sans logiciel Elgato, il n'y a
pas de profil à importer : les touches sont posées par le code. Un boîtier à
molettes reçoit la régie (les gestes du plein écran, le mixage aux molettes),
un boîtier sans molette reçoit la grille (les chaînes, et de quoi paginer).
Ce sont les deux profils déjà livrés, rendus à leur destination.

**Le dessin vit dans un fil à part.** Composer une touche télécharge un avatar
et redimensionne une image ; fait dans le fil de Qt, chaque changement
d'audience figerait l'interface. Le fil ne rend jamais qu'un état — le dernier
publié : quand ils arrivent plus vite qu'on ne dessine, les intermédiaires
n'ont aucun intérêt, personne ne les aura vus.
"""

from __future__ import annotations

import atexit
import io
import logging
import threading
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from core.paths import RESOURCE_ROOT

logger = logging.getLogger(__name__)

#: Gestes portés par un boîtier à molettes, dans l'ordre des touches.
#:
#: C'est la disposition du profil « ZLink — Régie », que
#: `streamdeck/gen_profils.py` écrit pour le logiciel Elgato. Les deux doivent
#: montrer la même chose : un utilisateur qui passe de Windows à Linux ne doit
#: pas réapprendre où sont ses touches.
GESTES_REGIE: tuple[tuple[str, str], ...] = (
    ("action", "chat"), ("action", "don"),
    ("action", "clip"), ("action", "replay"),
    ("action", "muet"), ("action", "favori"),
    ("navigation", "precedent"), ("navigation", "suivant"),
)

#: Ce que règle chaque molette. Vide = le son du plein écran ; sinon le rang
#: d'une chaîne épinglée dans la console de mixage.
CIBLES_MOLETTES: tuple[str, ...] = ("principal", "0", "1", "2")

#: Sous ce nombre de touches, aucune n'est sacrifiée à la pagination : sur un
#: Stream Deck Mini, deux flèches sur six touches coûteraient plus de chaînes
#: qu'elles n'en donneraient accès.
_MINIMUM_POUR_PAGINER = 8

#: Gestes dont la touche porte un ÉTAT, et la clé que ZLink publie pour lui.
#: Repris tel quel du plugin Elgato : c'est le même contrat d'état.
ETATS_ACTION = {"muet": "muet", "favori": "favori", "chat": "chat"}

LIBELLES_ACTION = {
    "clip": "Clip", "replay": "Revoir", "chat": "Chat",
    "don": "Don", "favori": "Favori", "muet": "Muet",
}

LIBELLES_NAVIGATION = {
    "precedent": "Précédent", "suivant": "Suivant",
    "page_precedente": "Page préc.", "page_suivante": "Page suiv.",
}

#: Orange d'accent de ZLink, posé autour de la chaîne au plein écran.
ORANGE = (255, 107, 0)

#: Luminosité des touches, en pourcentage. Un boîtier posé à côté d'un mur
#: d'écrans en pleine nuit n'a pas besoin d'éblouir ; en dessous, les avatars
#: sombres deviennent illisibles.
LUMINOSITE = 80

#: La règle udev sans laquelle `/dev/hidraw*` appartient à root seul. Citée
#: dans le journal quand un boîtier refuse de s'ouvrir : c'est le seul
#: obstacle sérieux à l'installation sous Linux, et une erreur de hidapi ne
#: le dit à personne.
NOM_REGLE_UDEV = "70-zlink-streamdeck.rules"

#: Un avatar Twitch pèse quelques dizaines de kilo-octets. Au-delà, ce n'en est
#: pas un, et rien ne justifie de le charger en mémoire pour le découvrir.
_AVATAR_MAX_OCTETS = 4 * 1024 * 1024

#: Polices cherchées pour écrire sur les touches, de la mieux dessinée à la
#: plus certainement présente. Aucune n'est livrée : en ajouter une au paquet
#: pour deux lignes de texte par touche ne se défend pas.
_POLICES = (
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
)


def _police(taille: int):
    """Une police de `taille` points, quelle que soit la distribution.

    En dernier recours la police par défaut de Pillow : minuscule et sans
    accent, mais une touche au titre laid reste une touche qui marche.
    """
    from PIL import ImageFont

    for chemin in _POLICES:
        try:
            return ImageFont.truetype(chemin, taille)
        except OSError:
            continue
    # `size` existe depuis Pillow 10.1, et requirements.txt n'accepte pas plus
    # ancien : le repli sans taille qui vivait ici ne pouvait jamais servir.
    return ImageFont.load_default(size=taille)  # NOSONAR — stub SonarLint périmé


#: En dessous, un nom cesse d'être lisible sur une touche : mieux vaut alors
#: en couper la fin que continuer à rapetisser.
#:
#: Sept et non neuf : à neuf, « antoinedaniel » et « littlebigwhale » — deux
#: noms courants du plateau, pas des cas tordus — perdaient leur fin alors
#: qu'ils tiennent entiers deux points en dessous, et restent lisibles.
_TAILLE_PLANCHER = 7


def _texte_tenant(dessin, texte: str, largeur: int, taille: int):
    """`texte` et la police pour qu'il tienne dans `largeur`.

    On rapetisse d'abord, on ne tronque qu'ensuite : « antoinedaniel » déborde
    d'une touche de 72 px, mais le couper à onze lettres pour rien serait
    dommage — deux chaînes au même début deviendraient le même mot. En dessous
    du plancher de lisibilité, la fin est remplacée par un point de suite,
    qui dit au moins qu'il en manque.
    """
    while taille > _TAILLE_PLANCHER:
        police = _police(taille)
        if dessin.textlength(texte, font=police) <= largeur:
            return texte, police
        taille -= 1
    police = _police(taille)
    if dessin.textlength(texte, font=police) <= largeur:
        return texte, police
    while texte and dessin.textlength(texte + "…", font=police) > largeur:
        texte = texte[:-1]
    return texte + "…", police


def _abrege(nombre: int) -> str:
    """Une audience, tenue en quatre caractères."""
    if nombre >= 1_000_000:
        return f"{nombre / 1_000_000:.1f}M"
    if nombre >= 1_000:
        return f"{nombre / 1_000:.1f}k"
    return str(nombre)


def dossier_glyphes():
    """Où sont les dessins de touche, livrés avec l'extension Elgato.

    On les reprend plutôt que d'en dessiner d'autres : ce sont les mêmes
    gestes, et deux jeux d'icônes à tenir en phase finiraient par diverger.
    """
    return RESOURCE_ROOT / "streamdeck" / "com.zlink.deck.sdPlugin" / "touches"


def disposition(touches: int, molettes: int) -> list[dict]:
    """Ce que porte chaque touche d'un boîtier, dans l'ordre.

    Un boîtier à molettes reçoit la régie, les autres la grille. C'est le
    modèle qui décide, et non un réglage : les deux profils livrés sont déjà
    répartis ainsi, et faire choisir l'utilisateur entre deux dispositions
    dont une seule convient à son matériel n'est pas un choix.
    """
    if molettes:
        gestes = [{"famille": famille, "cle": cle}
                  for famille, cle in GESTES_REGIE[:touches]]
        # Un boîtier à molettes plus large que le Stream Deck + n'existe pas
        # aujourd'hui ; s'il arrive, ses touches en trop restent éteintes
        # plutôt que de recevoir un geste tiré au sort.
        return gestes + [{"famille": "vide"}] * (touches - len(gestes))

    if touches >= _MINIMUM_POUR_PAGINER:
        cellules = touches - 2
        pagination = [{"famille": "navigation", "cle": "page_precedente"},
                      {"famille": "navigation", "cle": "page_suivante"}]
    else:
        cellules = touches
        pagination = []
    return [{"famille": "flux", "rang": rang}
            for rang in range(cellules)] + pagination


# ── Images de touche ─────────────────────────────────────────────────────────

class Vignettes:
    """Fabrique les images posées sur le boîtier, et garde ce qui coûte cher.

    Ce qui est gardé et ce qui ne l'est pas n'est pas arbitraire : on met en
    cache ce dont le nombre de formes possibles est BORNÉ — un avatar
    redimensionné, un glyphe, un fond de touche — et on recompose à chaque fois
    ce qui suit une valeur qui bouge. Garder la touche entière, audience
    comprise, ferait grossir le cache d'une image toutes les trente secondes
    par chaîne : sur les cinquante heures d'un ZEvent, le cache finirait plus
    lourd que l'application.
    """

    def __init__(self) -> None:
        self._photos: dict[tuple, Any] = {}    # avatar décodé et redimensionné
        self._fonds: dict[tuple, Any] = {}     # fond de touche Flux, sans texte
        self._gestes: dict[tuple, Any] = {}    # touche d'action, complète
        self._glyphes: dict[str, Any] = {}     # dessin livré, mis à l'échelle
        self._bruts: dict[str, bytes | None] = {}

    # ── avatars ─────────────────────────────────────────────────────────────

    def _telecharger(self, url: str) -> bytes | None:
        """Récupère un avatar. Bloquant : appelé depuis le fil de dessin.

        L'échec est mémorisé au même titre que la réussite : une URL morte
        serait sinon redemandée à chaque redessin, toutes les trente secondes,
        pour échouer à chaque fois.
        """
        if url in self._bruts:
            return self._bruts[url]
        if not url.lower().startswith("https://"):
            logger.debug("Stream Deck : avatar refusé (pas https) — %s", url[:60])
            self._bruts[url] = None
            return None
        import urllib.request

        # Le User-Agent n'est pas décoratif : « Python-urllib/3.x » se fait
        # refuser par les protections anti-robots, et l'avatar manquerait sans
        # qu'on comprenne pourquoi.
        requete = urllib.request.Request(
            url, headers={"User-Agent": "ZLink-Deck/1.0"})
        donnees: bytes | None
        try:
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                donnees = reponse.read(_AVATAR_MAX_OCTETS + 1)
        except Exception as exc:                          # noqa: BLE001
            logger.debug("Stream Deck : avatar %s — %s", url[:60], exc)
            donnees = None
        if donnees is not None and len(donnees) > _AVATAR_MAX_OCTETS:
            logger.debug("Stream Deck : avatar écarté, trop volumineux")
            donnees = None
        self._bruts[url] = donnees
        return donnees

    def photo(self, url: str, cote: int, terni: bool = False):
        """L'avatar, carré, à la taille demandée. None s'il n'y en a pas.

        Gardé : décoder un JPEG et le rééchantillonner à chaque redessin est
        le seul geste vraiment coûteux de tout le dessin d'une touche.
        """
        cle = (url, cote, terni)
        if cle in self._photos:
            return self._photos[cle]
        from PIL import Image

        brut = self._telecharger(url) if url else None
        image = None
        if brut is not None:
            try:
                image = Image.open(io.BytesIO(brut)).convert("RGB").resize(
                    (cote, cote), Image.LANCZOS)
            except Exception as exc:                      # noqa: BLE001
                logger.debug("Stream Deck : avatar illisible — %s", exc)
        if image is not None and terni:
            gris = image.convert("L").point(lambda v: int(v * 0.4))
            image = Image.merge("RGB", (gris, gris, gris))
        self._photos[cle] = image
        return image

    # ── glyphes livrés ──────────────────────────────────────────────────────

    def glyphe(self, nom: str, cote: int):
        """Un dessin de touche de l'extension, aplati sur fond noir.

        Les fichiers sont en RGBA : les coller sur du noir plutôt que de les
        convertir donne le même fond que le boîtier éteint, au lieu du blanc
        que produit une conversion RGB naïve.
        """
        from PIL import Image

        cle = f"{nom}|{cote}"
        if cle in self._glyphes:
            return self._glyphes[cle]
        fond = Image.new("RGB", (cote, cote), "black")
        chemin = dossier_glyphes() / f"{nom}.png"
        try:
            dessin = Image.open(chemin).convert("RGBA").resize(
                (cote, cote), Image.LANCZOS)
            fond.paste(dessin, (0, 0), dessin)
        except OSError as exc:
            logger.debug("Stream Deck : glyphe absent (%s) — %s", chemin, exc)
        self._glyphes[cle] = fond
        return fond

    # ── touches ─────────────────────────────────────────────────────────────

    def flux(self, cellule: dict, actif: bool, cote: int):
        """Une chaîne de la grille : son avatar, son nom, son audience."""
        login = str(cellule.get("login", ""))
        en_direct = bool(cellule.get("online"))
        fond = self._fond_flux(str(cellule.get("avatar", "")), en_direct,
                               actif, cote)
        image = fond.copy()
        vues = int(cellule.get("viewers", 0) or 0)
        self._bandeau(image, cote, login,
                      _abrege(vues) if en_direct else "hors ligne")
        return image

    def _fond_flux(self, url: str, en_direct: bool, actif: bool, cote: int):
        """L'avatar seul, grisé hors direct, cerclé d'orange s'il est en grand.

        Quatre formes possibles par chaîne : le cache ne peut pas déborder.
        """
        from PIL import Image, ImageDraw

        cle = (url, en_direct, actif, cote)
        if cle in self._fonds:
            return self._fonds[cle]
        # Une chaîne hors ligne reste lisible mais ne réclame pas l'œil.
        image = self.photo(url, cote, terni=not en_direct)
        image = image.copy() if image is not None else Image.new(
            "RGB", (cote, cote), "#141414")
        if actif:
            ImageDraw.Draw(image).rectangle(
                [(0, 0), (cote - 1, cote - 1)],
                outline=ORANGE, width=max(3, cote // 18))
        self._fonds[cle] = image
        return image

    def geste(self, famille: str, cle_geste: str, actif: bool, cote: int):
        """Une touche d'action ou de navigation : son dessin et son mot.

        Gardée entière : dix-sept dessins, deux états, une taille par modèle.
        """
        cle = (famille, cle_geste, actif, cote)
        if cle in self._gestes:
            return self._gestes[cle]
        nom = f"{famille}-{cle_geste}"
        libelles = LIBELLES_ACTION if famille == "action" else LIBELLES_NAVIGATION
        image = self.glyphe(f"{nom}-actif" if actif else nom, cote).copy()
        self._bandeau(image, cote, libelles.get(cle_geste, ""), "")
        self._gestes[cle] = image
        return image

    def vide(self, cote: int):
        """Une touche sans rien : noire, pas la chaîne d'avant."""
        from PIL import Image

        return Image.new("RGB", (cote, cote), "black")

    def _bandeau(self, image, cote: int, titre: str, seconde: str) -> None:
        """Écrit une à deux lignes en bas de la touche, sur un fond assombri.

        Le fond n'est pas décoratif : un nom blanc posé sur un avatar clair
        disparaît, et c'est justement quand la grille est pleine qu'on a besoin
        de lire lequel est lequel.
        """
        from PIL import Image, ImageDraw

        if not titre and not seconde:
            return
        lignes = 2 if seconde else 1
        hauteur = int(cote * (0.36 if lignes == 2 else 0.24))
        voile = Image.new("RGBA", (cote, hauteur), (0, 0, 0, 170))
        bas = image.crop((0, cote - hauteur, cote, cote)).convert("RGBA")
        image.paste(Image.alpha_composite(bas, voile).convert("RGB"),
                    (0, cote - hauteur))

        dessin = ImageDraw.Draw(image)
        taille = max(8, int(cote * (0.15 if lignes == 2 else 0.17)))
        haut = cote - hauteur + max(1, int(cote * 0.03))
        self._centrer(dessin, titre, cote, haut, taille, (255, 255, 255))
        if seconde:
            self._centrer(dessin, seconde, cote,
                          haut + taille + max(1, cote // 40),
                          max(7, int(cote * 0.13)), (180, 180, 180))

    @staticmethod
    def _centrer(dessin, texte: str, cote: int, haut: int, taille: int,
                 couleur: tuple) -> None:
        if not texte:
            return
        marge = max(2, cote // 24)
        texte, police = _texte_tenant(dessin, texte, cote - 2 * marge, taille)
        largeur = dessin.textlength(texte, font=police)
        dessin.text(((cote - largeur) / 2, haut), texte, font=police,
                    fill=couleur)

    # ── écran des molettes ──────────────────────────────────────────────────

    def bandeau_molettes(self, pistes: list[dict], largeur: int, hauteur: int):
        """L'écran du Stream Deck +, découpé en une case par molette.

        Le logiciel Elgato dessine ces cases lui-même à partir d'un
        `setFeedback` ; sans lui, l'écran est une seule image de 800 × 100 qu'il
        faut composer entièrement. On y met ce que le plugin y mettait :
        l'avatar de la piste, son nom, son niveau et une barre — ternis quand
        elle est coupée.
        """
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (largeur, hauteur), "black")
        cases = max(1, len(pistes))
        pas = largeur // cases
        for rang, piste in enumerate(pistes):
            self._case_molette(image, piste, rang * pas, pas, hauteur)
        dessin = ImageDraw.Draw(image)
        for rang in range(1, cases):
            dessin.line([(rang * pas, 8), (rang * pas, hauteur - 8)],
                        fill=(45, 45, 45))
        return image

    def _case_molette(self, image, piste: dict, gauche: int, largeur: int,
                      hauteur: int) -> None:
        from PIL import ImageDraw

        muet = bool(piste.get("muet"))
        inerte = bool(piste.get("inerte"))
        # Le ternissement porte sur les couleurs, pas sur une opacité : le fond
        # est noir, une opacité y donnerait le même gris pour tout.
        if inerte:
            attenue = 0.25
        elif muet:
            attenue = 0.4
        else:
            attenue = 1.0
        blanc = tuple(int(255 * attenue) for _ in range(3))
        gris = tuple(int(150 * attenue) for _ in range(3))

        marge = max(4, hauteur // 12)
        cote = hauteur - 2 * marge
        photo = self.photo(str(piste.get("avatar", "")), cote, terni=muet)
        texte_x = gauche + marge
        if photo is not None:
            image.paste(photo, (gauche + marge, marge))
            texte_x += cote + marge

        dessin = ImageDraw.Draw(image)
        place = gauche + largeur - marge - texte_x
        titre, police = _texte_tenant(dessin, str(piste.get("titre", "")),
                                      place, max(11, hauteur // 6))
        dessin.text((texte_x, marge + 2), titre, font=police, fill=blanc)
        if inerte:
            # Ni valeur ni barre : cette molette ne règle rien, et une barre
            # vide se lit comme un volume à zéro — ce qui n'est pas le cas.
            return
        volume = int(piste.get("volume", 0) or 0)
        dessin.text((texte_x, marge + hauteur // 3),
                    "Muet" if muet else f"{volume} %",
                    font=_police(max(10, hauteur // 7)), fill=gris)

        # La barre montre ce qu'on ENTEND, pas le réglage gardé pour le retour
        # du son : coupée, elle tombe à zéro même si le niveau est intact.
        epaisseur = max(3, hauteur // 20)
        bas = hauteur - marge - epaisseur
        droite = gauche + largeur - marge
        dessin.rectangle([(texte_x, bas), (droite, bas + epaisseur)],
                         fill=(38, 38, 38))
        if not muet and volume:
            fin = texte_x + int((droite - texte_x) * volume / 100)
            dessin.rectangle([(texte_x, bas), (fin, bas + epaisseur)],
                             fill=ORANGE)


# ── Un boîtier ───────────────────────────────────────────────────────────────

class Boitier:
    """Un Stream Deck ouvert, et ce qu'on a posé dessus.

    `_envoye` retient la signature de chaque touche déjà écrite. Sans elle,
    chaque publication d'état réécrirait les quinze touches en USB alors qu'une
    seule a changé — le boîtier suit, mais rien ne l'exige.
    """

    def __init__(self, deck) -> None:
        self.deck = deck
        self.molettes = int(getattr(deck, "dial_count", lambda: 0)())
        self.disposition = disposition(deck.key_count(), self.molettes)
        self.cote = int(deck.key_image_format()["size"][0])
        self.page = 0
        self._envoye: dict[int, tuple] = {}

    @property
    def touches_de_flux(self) -> int:
        """Combien de touches montrent une chaîne — c'est la taille d'une page."""
        return max(1, sum(1 for t in self.disposition
                          if t["famille"] == "flux"))

    def a_change(self, index: int, signature: tuple) -> bool:
        """Vrai si cette touche doit être réécrite, et le note."""
        if self._envoye.get(index) == signature:
            return False
        self._envoye[index] = signature
        return True

    def oublier(self) -> None:
        """Rend toutes les touches à réécrire — après une coupure d'état."""
        self._envoye.clear()


# ── Le pilote ────────────────────────────────────────────────────────────────

class PiloteStreamDeck(QObject):
    """Les boîtiers branchés, pilotés en direct.

    Les signaux sont ceux de `core.remote_api.RemoteAPI`, au nom près d'aucun :
    main.py branche l'un ou l'autre sur les mêmes fenêtres, sans savoir par où
    l'ordre est arrivé.
    """

    #: Index 0-based d'une cellule de la grille à passer en plein écran.
    slot_demande = pyqtSignal(int)
    #: -1 ou +1 — flux précédent ou suivant dans l'ordre de la grille.
    voisin_demande = pyqtSignal(int)
    #: Clé d'action : « clip », « replay », « chat »…
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._boitiers: list[Boitier] = []
        self._vignettes = Vignettes()
        self._etat: dict = {}
        self._verrou = threading.Lock()
        self._a_dessiner = threading.Event()
        self._arret = threading.Event()
        self._fil: threading.Thread | None = None

    # ── cycle de vie ────────────────────────────────────────────────────────

    def demarrer(self) -> int:
        """Ouvre les boîtiers branchés. Rend combien ont répondu.

        Zéro n'est pas une erreur : la plupart des postes n'ont pas de Stream
        Deck, et ZLink tourne sans exactement comme avant.
        """
        for deck in self._enumerer():
            try:
                deck.open()
                deck.reset()
                deck.set_brightness(LUMINOSITE)
            except Exception as exc:                      # noqa: BLE001
                self._expliquer_refus(deck, exc)
                continue
            boitier = Boitier(deck)
            deck.set_key_callback(self._sur_touche)
            if boitier.molettes:
                deck.set_dial_callback(self._sur_molette)
                # L'écran du Stream Deck + est tactile, et le plugin Elgato
                # traitait déjà « touchTap » comme un appui de molette. Une
                # case qu'on voit et qu'on touche doit répondre.
                if getattr(deck, "is_touch", lambda: False)():
                    deck.set_touchscreen_callback(self._sur_ecran)
            self._boitiers.append(boitier)
            logger.info("Stream Deck : %s ouvert — %d touches, %d molette(s)",
                        deck.deck_type(), deck.key_count(), boitier.molettes)
        if not self._boitiers:
            return 0
        self._fil = threading.Thread(target=self._boucle_dessin,
                                     name="zlink-streamdeck", daemon=True)
        self._fil.start()
        self._a_dessiner.set()
        # Filet de sécurité, et pas une politesse : libusb ABANDONNE le
        # processus (« usbi_mutex_destroy: Assertion failed ») si ses
        # verrous sont détruits alors qu'un boîtier est encore ouvert. Un
        # `arreter()` oublié — sortie par une exception, script d'essai,
        # suite de tests — ferait donc mourir ZLink sur un plantage à la
        # fermeture. atexit est idempotent avec l'arrêt normal.
        atexit.register(self.arreter)
        return len(self._boitiers)

    @staticmethod
    def _enumerer() -> list:
        """Les boîtiers vus par hidapi. Liste vide si la pile n'est pas là.

        L'absence de la bibliothèque ou de hidapi n'est pas une panne : c'est
        une installation qui n'a pas ce matériel. On le dit une fois, en
        `debug`, et ZLink continue.
        """
        try:
            from StreamDeck.DeviceManager import DeviceManager
        except ImportError as exc:
            logger.debug("Stream Deck : bibliothèque absente — %s", exc)
            return []
        try:
            return DeviceManager().enumerate()
        except Exception as exc:                          # noqa: BLE001
            # Probe transport introuvable : hidapi n'est pas installé.
            logger.debug("Stream Deck : énumération impossible — %s", exc)
            return []

    @staticmethod
    def _expliquer_refus(deck, exc: Exception) -> None:
        """Dit quoi faire, plutôt que de recopier l'erreur de hidapi.

        Un boîtier refusé l'est presque toujours pour une raison : le fichier
        `/dev/hidraw*` appartient à root. « open failed » ne le dit à personne,
        et c'est le seul obstacle sérieux à l'installation sous Linux.
        """
        logger.warning(
            "Stream Deck : %s inaccessible (%s). Sous Linux, poser la règle "
            "udev %s puis rebrancher le boîtier — voir streamdeck/README.md.",
            getattr(deck, "deck_type", lambda: "boîtier")(), exc, NOM_REGLE_UDEV)

    def arreter(self) -> None:
        """Éteint les touches et rend les boîtiers. Idempotent."""
        if not self._boitiers and self._fil is None:
            return
        self._arret.set()
        self._a_dessiner.set()
        fil, self._fil = self._fil, None
        if fil is not None and fil.is_alive():
            fil.join(timeout=2.0)
        for boitier in self._boitiers:
            try:
                boitier.deck.set_key_callback(None)
                if boitier.molettes:
                    boitier.deck.set_dial_callback(None)
                boitier.deck.reset()
                boitier.deck.close()
            except Exception as exc:                      # noqa: BLE001
                # Un boîtier débranché pendant la session lève ici. Il n'y a
                # rien à réparer : on le note et on passe au suivant.
                logger.debug("Stream Deck : fermeture — %s", exc)
        self._boitiers.clear()

    @property
    def boitiers(self) -> int:
        """Nombre de boîtiers pilotés."""
        return len(self._boitiers)

    # ── état ────────────────────────────────────────────────────────────────

    def publier_etat(self, etat: dict[str, Any]) -> None:
        """Reçoit l'état de ZLink. Appelé depuis le fil de Qt.

        On ne dessine pas ici : composer une touche télécharge un avatar, et
        l'interface se figerait le temps du redessin. On dépose, on réveille.
        """
        with self._verrou:
            self._etat = dict(etat)
        self._a_dessiner.set()

    # ── appuis ──────────────────────────────────────────────────────────────

    def _sur_touche(self, deck, index: int, enfoncee: bool) -> None:
        """Une touche du boîtier. Appelé depuis le fil de lecture de hidapi.

        Les signaux traversent seuls : l'objet vit dans le fil de Qt, qui met
        donc l'émission en file d'attente au lieu de l'exécuter ici.
        """
        if not enfoncee:
            return
        try:
            self._agir(self._boitier_de(deck), index)
        except Exception:                                 # noqa: BLE001
            # Une exception qui remonte ici tuerait le fil de lecture du
            # boîtier, et toutes ses touches avec — sans rien dire.
            logger.exception("Stream Deck : appui non traité")

    def _agir(self, boitier: Boitier | None, index: int) -> None:
        if boitier is None or not 0 <= index < len(boitier.disposition):
            return
        touche = boitier.disposition[index]
        famille = touche["famille"]
        if famille == "action":
            self.action_demandee.emit(touche["cle"])
        elif famille == "navigation":
            self._naviguer(boitier, touche["cle"])
        elif famille == "flux":
            self._choisir_flux(boitier, touche["rang"])

    def _choisir_flux(self, boitier: Boitier, rang: int) -> None:
        cellules = self._cellules()
        index = boitier.page * boitier.touches_de_flux + rang
        if 0 <= index < len(cellules):
            self.chaine_demandee.emit(str(cellules[index].get("login", "")))

    def _naviguer(self, boitier: Boitier, sens: str) -> None:
        if sens in ("precedent", "suivant"):
            self.voisin_demande.emit(1 if sens == "suivant" else -1)
            return
        # Pagination : le boîtier a moins de touches que la grille n'a de
        # cellules, et une chaîne inatteignable est une chaîne perdue.
        par_page = boitier.touches_de_flux
        pages = max(1, -(-len(self._cellules()) // par_page))
        boitier.page = (boitier.page + (1 if sens == "page_suivante" else -1)) % pages
        boitier.oublier()
        self._a_dessiner.set()

    def _sur_molette(self, deck, index: int, evenement, valeur) -> None:
        """Une molette tourne ou s'enfonce."""
        try:
            from StreamDeck.Devices.StreamDeck import DialEventType

            if evenement == DialEventType.TURN:
                self._tourner(index, int(valeur))
            elif evenement == DialEventType.PUSH and valeur:
                self._couper(index)
        except Exception:                                 # noqa: BLE001
            logger.exception("Stream Deck : molette non traitée")

    def _tourner(self, index: int, crans: int) -> None:
        """Cinq points par cran, comme les touches +/- du clavier."""
        login = self._login_de_molette(index)
        if login is None:
            return
        vise = max(0, min(100, self._volume_de(login) + crans * 5))
        if login:
            self.volume_chaine_demande.emit(login, vise)
        else:
            self.volume_demande.emit(vise)

    def _couper(self, index: int) -> None:
        login = self._login_de_molette(index)
        if login is None:
            return
        if login:
            self.muet_chaine_demande.emit(login, not self._muet_de(login))
        else:
            self.muet_demande.emit(not self._muet_de(""))

    def _sur_ecran(self, deck, evenement, valeur) -> None:
        """Un appui sur l'écran des molettes : coupe la piste touchée.

        L'écran est une bande unique : c'est l'abscisse du doigt qui dit quelle
        case a été touchée, et donc quelle piste couper.
        """
        try:
            from StreamDeck.Devices.StreamDeck import TouchscreenEventType

            if evenement != TouchscreenEventType.SHORT:
                return
            boitier = self._boitier_de(deck)
            if boitier is None or not boitier.molettes:
                return
            largeur = int(deck.touchscreen_image_format()["size"][0])
            case = int(valeur.get("x", 0)) * boitier.molettes // max(1, largeur)
            self._couper(min(case, boitier.molettes - 1))
        except Exception:                                 # noqa: BLE001
            logger.exception("Stream Deck : appui écran non traité")

    def _boitier_de(self, deck) -> Boitier | None:
        for boitier in self._boitiers:
            if boitier.deck is deck:
                return boitier
        return None

    # ── dessin ──────────────────────────────────────────────────────────────

    def _boucle_dessin(self) -> None:
        """Le fil qui écrit sur les boîtiers.

        Il ne rend jamais qu'un état — le dernier déposé. Quand ils arrivent
        plus vite qu'on ne dessine, les intermédiaires n'ont aucun intérêt :
        personne ne les aura vus passer.
        """
        while not self._arret.is_set():
            self._a_dessiner.wait()
            self._a_dessiner.clear()
            if self._arret.is_set():
                return
            try:
                self._peindre_tout()
            except Exception:                             # noqa: BLE001
                # Le fil doit survivre : un boîtier débranché en plein dessin
                # ne doit pas éteindre les autres jusqu'au prochain lancement.
                logger.exception("Stream Deck : dessin interrompu")

    def _peindre_tout(self) -> None:
        from StreamDeck.ImageHelpers import PILHelper

        # Une COPIE de la liste : `arreter()` la vide depuis le fil de Qt, et
        # ne rejoint celui-ci qu'avec un délai — un dessin en cours de
        # téléchargement d'avatar peut le dépasser. Itérer l'originale
        # lèverait alors « list changed size during iteration ».
        for boitier in list(self._boitiers):  # NOSONAR
            for index, touche in enumerate(boitier.disposition):
                image, signature = self._touche(boitier, touche)
                if not boitier.a_change(index, signature):
                    continue
                boitier.deck.set_key_image(
                    index, PILHelper.to_native_key_format(boitier.deck, image))
            if boitier.molettes and getattr(boitier.deck, "is_touch", None) \
                    and boitier.deck.is_touch():
                self._peindre_ecran(boitier)

    def _touche(self, boitier: Boitier, touche: dict):
        """L'image d'une touche, et la signature qui dit si elle a bougé."""
        famille = touche["famille"]
        if famille == "action":
            cle = touche["cle"]
            actif = bool(self._etat_courant().get(ETATS_ACTION.get(cle, "")))
            return (self._vignettes.geste("action", cle, actif, boitier.cote),
                    ("action", cle, actif))
        if famille == "navigation":
            cle = touche["cle"]
            return (self._vignettes.geste("navigation", cle, False, boitier.cote),
                    ("navigation", cle))
        if famille == "flux":
            return self._touche_flux(boitier, touche["rang"])
        return self._vignettes.vide(boitier.cote), ("vide",)

    def _touche_flux(self, boitier: Boitier, rang: int):
        cellules = self._cellules()
        index = boitier.page * boitier.touches_de_flux + rang
        if not 0 <= index < len(cellules):
            return self._vignettes.vide(boitier.cote), ("vide",)
        cellule = cellules[index]
        actif = cellule.get("login") == self._etat_courant().get("actif")
        signature = ("flux", cellule.get("login"), cellule.get("avatar"),
                     bool(cellule.get("online")),
                     int(cellule.get("viewers", 0) or 0), actif)
        return (self._vignettes.flux(cellule, actif, boitier.cote), signature)

    def _peindre_ecran(self, boitier: Boitier) -> None:
        from StreamDeck.ImageHelpers import PILHelper

        format_ecran = boitier.deck.touchscreen_image_format()
        largeur, hauteur = format_ecran["size"]
        pistes = [self._piste(rang) for rang in range(boitier.molettes)]
        signature = ("ecran", tuple(tuple(sorted(p.items())) for p in pistes))
        # -1 : l'écran partage la table des touches déjà écrites, où aucun
        # index négatif ne peut entrer en conflit avec une vraie touche.
        if not boitier.a_change(-1, signature):
            return
        image = self._vignettes.bandeau_molettes(pistes, largeur, hauteur)
        boitier.deck.set_touchscreen_image(
            PILHelper.to_native_touchscreen_format(boitier.deck, image),
            0, 0, largeur, hauteur)

    # ── lecture de l'état ───────────────────────────────────────────────────

    def _etat_courant(self) -> dict:
        with self._verrou:
            return self._etat

    def _cellules(self) -> list[dict]:
        return list(self._etat_courant().get("cellules") or [])

    def _piste(self, index: int) -> dict:
        """Ce qu'affiche une molette : sa chaîne, son niveau, son avatar.

        Une molette inerte le MONTRE : un tiret, pas de barre. Une case qui
        affiche « Plein écran » comme sa voisine promet un réglage qu'elle ne
        rendra pas.
        """
        login = self._login_de_molette(index)
        if login is None:
            return {"titre": "—", "volume": 0, "muet": False, "avatar": "",
                    "inerte": True}
        return {
            "titre": login or "Plein écran",
            "volume": self._volume_de(login),
            "muet": self._muet_de(login),
            "avatar": self._avatar_de(login),
        }

    def _login_de_molette(self, index: int) -> str | None:
        """Chaîne pilotée par une molette.

        Trois réponses, et pas deux : un login, `""` pour le son du plein
        écran, et None quand la molette ne vise rien — sa chaîne épinglée
        n'existe pas.

        None n'est pas une commodité. Sans lui, une molette dont l'épingle
        manque retombait sur le son du plein écran : sur un Stream Deck + sans
        aucune chaîne épinglée, les QUATRE molettes affichaient « Plein écran »
        et réglaient le même volume. Le README de `streamdeck/` annonce
        l'inverse — « une molette dont la chaîne n'est pas épinglée reste
        inerte plutôt que d'agir sur autre chose ». C'est ce contrat-là qui est
        tenu ici.
        """
        try:
            cible = CIBLES_MOLETTES[index]
        except IndexError:
            return None
        if cible == "principal":
            return ""
        epingles = [str(c.get("login", "")) for c in self._cellules()
                    if c.get("epingle")]
        try:
            return epingles[int(cible)]
        except (ValueError, IndexError):
            return None

    def _cellule(self, login: str) -> dict:
        for cellule in self._cellules():
            if cellule.get("login") == login:
                return cellule
        return {}

    def _volume_de(self, login: str) -> int:
        if not login:
            return int(self._etat_courant().get("volume", 0) or 0)
        return int(self._cellule(login).get("volume", 100) or 0)

    def _muet_de(self, login: str) -> bool:
        if not login:
            return bool(self._etat_courant().get("muet", False))
        return bool(self._cellule(login).get("muet", False))

    def _avatar_de(self, login: str) -> str:
        """URL de l'avatar d'une piste. Vide = celui de la chaîne au plein écran."""
        vise = login or str(self._etat_courant().get("actif") or "")
        return str(self._cellule(vise).get("avatar") or "")
