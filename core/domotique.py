# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Annonce les événements du ZEvent à Home Assistant.

ZLink repère déjà les paliers de cagnotte, les grosses donations, les objectifs
sur le point de tomber et les moments forts. Ce module les fait sortir de
l'application : un POST par événement, sur un webhook Home Assistant.

**Un webhook, pas un appel de service.** Trois raisons, et la troisième est la
vraie :

1. Aucun jeton à créer ni à ranger : l'identifiant du webhook EST le secret,
   et il se colle en une fois dans les réglages.
2. ZLink n'a pas à connaître les lampes, leurs noms ni leurs couleurs.
3. **Le clignotement appartient à Home Assistant.** Faire clignoter depuis ici
   voudrait dire vingt requêtes en dix secondes : une coupure au milieu, et
   les lampes restent éteintes. Un seul envoi, et l'automatisation fait le
   reste — c'est elle qui sait aussi remettre l'éclairage comme il était.

Rien de ce qui se passe ici ne doit se voir ailleurs : une box éteinte, une
URL fausse ou un réseau coupé donnent une ligne de journal, jamais une
exception au milieu d'un direct.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: Familles d'événements qu'on sait annoncer, et ce que chacune recouvre.
#: Le libellé sert aux réglages ; la clé part telle quelle dans le JSON, et
#: c'est sur elle que se branchent les automatisations.
EVENEMENTS: dict[str, str] = {
    "palier": "Palier de cagnotte franchi",
    "don": "Grosse donation",
    "objectif": "Objectif sur le point de tomber",
    "hype": "Moment fort sur une chaîne",
}

#: Au-delà, on n'attend plus : une box qui ne répond pas ne doit pas retenir
#: le fil qui a déclenché l'envoi.
DELAI_S = 5.0

#: En-têtes de chaque envoi.
#:
#: Le `User-Agent` n'est PAS décoratif. Sans lui, Python annonce
#: « Python-urllib/3.x », que les protections anti-robots refusent — un Home
#: Assistant publié derrière Cloudflare répondait 403 à ZLink tout en
#: acceptant la même requête d'un navigateur. Le diagnostic est d'autant plus
#: pénible que le code d'erreur ne vient pas de Home Assistant, qui n'a jamais
#: vu passer la requête.
def _entetes() -> dict[str, str]:
    from core.version import __version__

    return {"Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"ZLink/{__version__}"}


#: Schémas admis. `file://` et consorts n'ont rien à faire dans une URL
#: recopiée depuis une interface web, et ouvriraient une lecture de fichier.
SCHEMAS = ("http", "https")


def url_valable(url: str) -> bool:
    """Une URL de webhook exploitable — schéma web et hôte présents."""
    try:
        morceaux = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return False
    return morceaux.scheme in SCHEMAS and bool(morceaux.netloc)


#: Adresse par défaut d'une installation Home Assistant sur le réseau local.
BASE_DEFAUT = "http://homeassistant.local:8123"


def composer(base: str, identifiant: str) -> str:
    """L'URL du webhook, à partir de ce que Home Assistant MONTRE.

    Son éditeur d'automatisation n'affiche pas d'URL : il affiche un « ID du
    webhook », à charge de l'utilisateur de deviner que l'adresse complète est
    `<base>/api/webhook/<id>`. On lui demande donc les deux morceaux qu'il a
    réellement sous les yeux, et on assemble ici.

    Une URL entière collée dans le champ d'identifiant est acceptée telle
    quelle : c'est ce que fera quiconque l'a trouvée ailleurs.
    """
    ident = str(identifiant or "").strip()
    if ident.lower().startswith(("http://", "https://")):
        return ident
    # Un identifiant part dans un chemin d'URL : encodé, il ne peut pas y
    # greffer un segment ni des paramètres.
    ident = urllib.parse.quote(ident.strip("/").rsplit("/", 1)[-1], safe="")
    base = str(base or "").strip().rstrip("/")
    if not base or not ident:
        return ""
    return f"{base}/api/webhook/{ident}"


#: Réseaux privés, au sens où Home Assistant l'entend pour `local_only`.
_PREFIXES_LOCAUX = ("127.", "10.", "192.168.", "169.254.")


def est_local(url: str) -> bool:
    """L'adresse désigne-t-elle une box joignable SANS passer par Internet.

    C'est ce qui décide de `local_only`. Un déclencheur local atteint par un
    domaine public est écarté par Home Assistant — qui répond quand même 200,
    pour ne pas révéler quels webhooks existent. Le symptôme est donc : « ça
    répond, et rien ne s'allume », sans la moindre trace côté box.
    """
    try:
        hote = (urllib.parse.urlparse(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if not hote:
        return False
    if hote in ("localhost", "homeassistant", "homeassistant.local"):
        return True
    if hote.endswith(".local") or hote.endswith(".lan") or hote.endswith(".home"):
        return True
    if hote.startswith(_PREFIXES_LOCAUX):
        return True
    if hote.startswith("172."):
        # 172.16.0.0/12, et lui seul : 172.32.x est public.
        try:
            second = int(hote.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def identifiant(texte: str) -> str:
    """L'ID du webhook, que l'utilisateur ait collé l'URL entière ou l'ID seul.

    C'est cet identifiant — et lui seul — que réclame le YAML d'une
    automatisation : `webhook_id:` n'accepte pas une adresse.
    """
    brut = str(texte or "").strip().strip("/")
    return brut.rsplit("/", 1)[-1] if brut else ""


#: L'automatisation, prête à coller dans l'éditeur YAML de Home Assistant.
#:
#: Volontairement SANS condition : tout déclenche, y compris le bouton d'essai
#: de ZLink. C'est la seule façon de vérifier la chaîne entière — ZLink, le
#: réseau, la box, les lampes — avant d'y ajouter quoi que ce soit. Le filtre
#: par type vient après, et il est décrit juste en dessous dans l'écran.
#:
#: L'éclairage est photographié avant, et remis après : sans cela, la fin du
#: clignotement laisserait les lampes éteintes jusqu'au lendemain.
_MODELE = """alias: ZLink
description: Fait clignoter l'éclairage aux événements du ZEvent
triggers:
  - trigger: webhook
    allowed_methods: [POST]
    local_only: {local}
    webhook_id: {webhook}
conditions: []
actions:
  - action: scene.create
    data:
      scene_id: zlink_avant
      snapshot_entities:
        - {lampe}
  - repeat:
      count: 10
      sequence:
        - action: light.turn_on
          target: {{entity_id: {lampe}}}
          data: {{brightness: 255, rgb_color: [0, 255, 135], transition: 0}}
        - delay: {{milliseconds: 500}}
        - action: light.turn_off
          target: {{entity_id: {lampe}}}
          data: {{transition: 0}}
        - delay: {{milliseconds: 500}}
  - action: scene.turn_on
    target: {{entity_id: scene.zlink_avant}}
mode: single
"""

#: Ce qu'on met quand aucun webhook n'est encore configuré. Coller le modèle
#: avec un identifiant vide donnerait une automatisation qui ne se déclenche
#: jamais, sans que rien ne le dise.
SANS_WEBHOOK = "COLLEZ_ICI_VOTRE_ID"

#: Idem pour les lampes. « light.salon » avait l'air d'une valeur plausible :
#: collé tel quel, il donnait une automatisation qui se déclenche, s'exécute,
#: et n'allume rien — la panne la plus difficile à diagnostiquer, puisque tout
#: paraît fonctionner. Un nom manifestement faux se remarque dans l'éditeur.
LAMPES_DEFAUT = "light.remplacez_moi"


def automatisation(webhook: str, lampe: str = "",
                   url: str | None = None) -> str:
    """Le YAML à coller, avec l'identifiant et `local_only` déjà réglés.

    `local_only` n'est pas laissé à vrai par principe : ZLink connaît
    l'adresse configurée, et sait donc si elle passe par Internet. Livrer
    `true` à quelqu'un qui joint sa box par un domaine public, c'est livrer
    une automatisation qui ne se déclenchera jamais — en répondant 200.
    """
    adresse = url if url is not None else webhook
    return _MODELE.format(webhook=identifiant(webhook) or SANS_WEBHOOK,
                          lampe=lampe.strip() or LAMPES_DEFAUT,
                          local="true" if est_local(adresse) else "false")


def reglages(config: dict | None = None) -> dict:
    """Le bloc `domotique` de la configuration, complété de ses défauts."""
    if config is None:
        from core import config_store
        config = config_store.load()
    brut = config.get("domotique")
    brut = brut if isinstance(brut, dict) else {}
    familles = brut.get("evenements")
    if not isinstance(familles, list):
        familles = list(EVENEMENTS)
    base = str(brut.get("base") or BASE_DEFAUT).strip()
    identifiant = str(brut.get("webhook_id") or "").strip()
    # `url` reste lu : une configuration écrite à la main peut porter l'adresse
    # entière, et rien ne justifie de la rejeter.
    url = str(brut.get("url") or "").strip() or composer(base, identifiant)
    return {
        "lampes": str(brut.get("lampes") or "").strip(),
        "base": base,
        "webhook_id": identifiant,
        "url": url,
        "evenements": [f for f in familles if f in EVENEMENTS],
    }


def actif(config: dict | None = None) -> bool:
    """Vrai si une URL exploitable est configurée."""
    return url_valable(reglages(config)["url"])


def annonce(famille: str, donnees: dict[str, Any],
            config: dict | None = None) -> bool:
    """Envoie un événement, sans attendre. Rend False si rien n'est parti.

    L'envoi part dans un fil : Home Assistant répond vite, mais « vite » sur
    un réseau domestique reste plusieurs dizaines de millisecondes, et cet
    appel se fait depuis la boucle qui dessine l'interface.
    """
    conf = reglages(config)
    if famille not in conf["evenements"] or not url_valable(conf["url"]):
        return False
    charge = {"type": famille, **donnees}
    threading.Thread(target=_poster, args=(conf["url"], charge),
                     daemon=True).start()
    return True


def _poster(url: str, charge: dict) -> None:
    """L'envoi lui-même. N'échoue jamais bruyamment."""
    corps = json.dumps(charge, ensure_ascii=False).encode("utf-8")
    requete = urllib.request.Request(url, data=corps, method="POST",
                                     headers=_entetes())
    try:
        with urllib.request.urlopen(requete, timeout=DELAI_S) as reponse:
            logger.debug("Domotique : %s → HTTP %s", charge.get("type"),
                         reponse.status)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Box éteinte, URL fausse, réseau coupé : on le note et on continue.
        logger.warning("Domotique : « %s » non transmis — %s",
                       charge.get("type"), exc)


#: Ce qu'un code d'erreur veut dire ICI. Home Assistant répond 200 à un
#: webhook, même inconnu — délibérément, pour ne pas révéler lesquels
#: existent. Un code d'erreur vient donc presque toujours de ce qui est DEVANT
#: lui : reverse proxy, Cloudflare, filtre d'adresses.
_EXPLICATIONS = {
    403: ("403 — refusé AVANT Home Assistant. Un proxy ou Cloudflare bloque "
          "la requête, ou le déclencheur est en « local_only » alors que "
          "l'adresse passe par Internet : essayez l'adresse locale de la box."),
    404: ("404 — cette adresse n'existe pas. Vérifiez le domaine et le chemin "
          "« /api/webhook/ » ; Home Assistant, lui, répond 200 même à un "
          "identifiant inconnu."),
    405: ("405 — l'adresse existe mais refuse un POST. Le déclencheur doit "
          "autoriser POST (allowed_methods)."),
    502: "502 — le proxy ne joint pas Home Assistant. La box est-elle allumée ?",
    503: "503 — Home Assistant ou son proxy ne répond pas pour l'instant.",
}


def _expliquer(code: int) -> str:
    return _EXPLICATIONS.get(code, f"Home Assistant a répondu {code}")


def essayer(url: str) -> tuple[bool, str]:
    """Envoi d'essai, SYNCHRONE, pour le bouton des réglages.

    Rend (réussi, message affichable). Contrairement à `annonce`, on attend :
    l'utilisateur vient de cliquer et veut savoir.
    """
    if not url_valable(url):
        return False, "URL invalide : elle doit commencer par http:// ou https://"
    charge = {"type": "essai", "libelle": "Essai depuis ZLink",
              "montant": 0, "source": "reglages"}
    corps = json.dumps(charge, ensure_ascii=False).encode("utf-8")
    requete = urllib.request.Request(url, data=corps, method="POST",
                                     headers=_entetes())
    try:
        with urllib.request.urlopen(requete, timeout=DELAI_S) as reponse:
            statut = int(reponse.status)
    except urllib.error.HTTPError as exc:
        return False, _expliquer(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Injoignable : {exc}"
    # Un webhook sans automatisation derrière répond 200 et ne fait rien : on
    # ne peut donc pas promettre que les lampes ont bougé.
    # Home Assistant répond 200 à un webhook INCONNU, et aussi quand il écarte
    # une requête distante sur un déclencheur local. Un 200 ne prouve donc que
    # l'acheminement, jamais l'exécution : le dire, plutôt que laisser croire.
    return True, (f"Message acheminé (HTTP {statut}). Ce code ne dit PAS que "
                  "l'automatisation s'est exécutée : Home Assistant répond 200 "
                  "même à un webhook inconnu. Si rien ne s'allume, ouvrez "
                  "l'automatisation → menu ⋮ → « Traces ».")
