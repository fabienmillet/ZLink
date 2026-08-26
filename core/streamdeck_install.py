# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Pose l'extension Stream Deck chez Elgato, depuis ZLink.

Installer un plugin Stream Deck « à la main » suppose de connaître un dossier
caché, de savoir qu'il faut redémarrer le logiciel, et de ne pas se tromper de
nom de dossier. Personne ne devrait avoir à faire ça : l'extension est livrée
avec ZLink, et un bouton dans les paramètres la met en place.

Ce module ne connaît aucune fenêtre. Il répond à trois questions — le logiciel
Elgato est-il là, l'extension est-elle installée, dans quelle version — et sait
faire une chose : copier. L'interface s'en sert, les tests aussi.

Deux refus délibérés :

**On ne redémarre pas le logiciel Elgato à la place de l'utilisateur.** Il
relit ses plugins au démarrage, mais le tuer pendant qu'il pilote un boîtier
posé sur un bureau n'est pas à nous de le décider.

**On ne copie pas par-dessus un exécutable en cours d'exécution.** Si le plugin
tourne déjà, Windows verrouille son fichier : mieux vaut le dire clairement que
laisser une installation à moitié faite.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil

from core.paths import RESOURCE_ROOT

logger = logging.getLogger(__name__)

#: Identifiant du plugin, qui est aussi son nom de dossier des deux côtés.
NOM_PLUGIN = "com.zlink.deck.sdPlugin"

#: L'exécutable que le logiciel Elgato lancera. Il est produit par
#: `streamdeck/construire.py` et livré avec les versions publiées de ZLink ;
#: depuis un dépôt fraîchement cloné, il n'existe pas encore.
NOM_EXE = "zlink-deck.exe"

#: Ce qui n'a rien à faire dans une installation.
EXCLUS = {"__pycache__", "zlink-deck.log"}


def source() -> pathlib.Path:
    """Le dossier de l'extension tel que livré avec ZLink."""
    return RESOURCE_ROOT / "streamdeck" / NOM_PLUGIN


def dossier_profils() -> pathlib.Path | None:
    """Où sont les profils prêts à l'emploi, s'ils ont été livrés.

    Ils ne s'installent pas comme l'extension : un profil s'IMPORTE, par le
    logiciel Elgato, qui garde les siens dans un dossier qu'il réécrit à sa
    fermeture. Y déposer un fichier pendant qu'il tourne le verrait disparaître
    à la sortie — on ouvre donc le dossier, et l'utilisateur double-clique.
    """
    dossier = source() / "profils"
    return dossier if any(dossier.glob("*.streamDeckProfile")) else None


def dossier_elgato() -> pathlib.Path | None:
    """Où le logiciel Elgato range ses plugins, s'il est installé.

    Rend None quand rien n'indique sa présence : proposer d'installer une
    extension pour un logiciel absent n'aurait aucun sens.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = pathlib.Path(appdata) / "Elgato" / "StreamDeck"
    return base / "Plugins" if base.is_dir() else None


def _version(dossier: pathlib.Path) -> str:
    """Version déclarée par un manifeste, ou "" s'il est absent ou illisible."""
    try:
        manifeste = json.loads(
            (dossier / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("manifeste illisible (%s) : %s", dossier, exc)
        return ""
    return str(manifeste.get("Version") or "")


def etat() -> dict:
    """Ce qu'il faut savoir pour décider quoi afficher.

    Les clés sont pensées pour être lues telles quelles par l'interface :
    `possible` dit si le bouton doit répondre, `raison` dit pourquoi non.
    """
    depart = source()
    elgato = dossier_elgato()
    installee = elgato / NOM_PLUGIN if elgato else None

    disponible = _version(depart)
    posee = _version(installee) if installee and installee.is_dir() else ""

    if elgato is None:
        raison = "Le logiciel Stream Deck n'est pas installé sur cette machine."
    elif not disponible:
        raison = "Cette copie de ZLink ne contient pas l'extension."
    elif not (depart / NOM_EXE).exists():
        raison = ("L'extension livrée est incomplète : "
                  f"{NOM_EXE} manque. Depuis les sources, le produire avec "
                  "streamdeck/construire.py.")
    else:
        raison = ""

    return {
        "logiciel": elgato is not None,
        "disponible": disponible,
        "installee": posee,
        "a_jour": bool(posee) and posee == disponible,
        "possible": not raison,
        "raison": raison,
        "dossier": str(installee) if installee else "",
    }


def _fichiers(depart: pathlib.Path):
    for chemin in sorted(depart.rglob("*")):
        if chemin.is_file():
            relatif = chemin.relative_to(depart)
            if not any(part in EXCLUS for part in relatif.parts):
                yield chemin, relatif


def _balayer(dossier: pathlib.Path) -> None:
    """Efface un dossier de travail. Un échec ici ne s'ajoute pas au précédent."""
    try:
        shutil.rmtree(dossier, ignore_errors=True)
    except OSError as exc:                       # pragma: no cover - défensif
        logger.debug("dossier provisoire non effacé : %s", exc)


def installer() -> tuple[bool, str]:
    """Copie l'extension chez Elgato. Rend (réussi, message pour l'écran).

    Le message est écrit pour être montré tel quel : c'est la seule chose que
    l'utilisateur verra de cette opération.
    """
    situation = etat()
    if not situation["possible"]:
        return False, situation["raison"]

    depart = source()
    cible = pathlib.Path(situation["dossier"])
    # Un dossier de travail à côté, renommé à la fin : une copie interrompue à
    # mi-chemin laisserait une extension mutilée, que le logiciel Elgato
    # refuserait sans rien dire.
    provisoire = cible.with_name(cible.name + ".neuf")
    try:
        if provisoire.exists():
            shutil.rmtree(provisoire)
        for chemin, relatif in _fichiers(depart):
            destination = provisoire / relatif
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(chemin, destination)
        if cible.exists():
            shutil.rmtree(cible)
        provisoire.rename(cible)
    except PermissionError:
        logger.warning("Stream Deck : installation refusée, fichier verrouillé")
        _balayer(provisoire)
        return False, ("Le logiciel Stream Deck utilise l'extension en ce "
                       "moment. Le quitter, puis réessayer.")
    except OSError as exc:
        logger.exception("Stream Deck : installation impossible")
        _balayer(provisoire)
        return False, f"Installation impossible : {exc}"

    logger.info("Stream Deck : extension installée dans %s", cible)
    return True, ("Extension installée. Quitter et relancer le logiciel "
                  "Stream Deck pour la voir apparaître.")
