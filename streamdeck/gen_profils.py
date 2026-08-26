#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Écrit les profils Stream Deck prêts à l'emploi.

    python streamdeck/gen_profils.py

Poser vingt touches une à une, en réglant le rang de chacune dans son panneau,
prend un quart d'heure et se refait à chaque changement d'avis. Ces deux
profils contiennent la disposition déjà faite : il n'y a qu'à les importer.

**Le format n'est pas deviné, il est relevé.** Le logiciel Stream Deck 7.x
range ses profils dans `%APPDATA%\\Elgato\\StreamDeck\\ProfilesV3`, et ce qui
est écrit ici en reproduit la structure exacte : un manifeste racine qui
désigne l'appareil et les pages, une page par dossier, et dans chaque page
autant de « contrôleurs » que l'appareil en possède — un Stream Deck classique
n'a pas d'entrée `Encoder`, un Stream Deck + en a une.

**Aucun numéro de série n'est écrit.** Le manifeste d'un profil existant porte
l'identifiant matériel de l'appareil sur lequel il a été créé ; le laisser vide
rend le profil transportable, et évite surtout de publier le serial de la
machine qui a servi à le construire.

Les identifiants sont dérivés du nom du profil (uuid5) plutôt que tirés au
sort : deux exécutions produisent le même fichier, donc réimporter un profil
régénéré met à jour l'existant au lieu d'en créer un deuxième.
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid
import zipfile

ICI = pathlib.Path(__file__).resolve().parent
PLUGIN = ICI / "com.zlink.deck.sdPlugin"
#: Les profils voyagent AVEC le plugin : ZLink embarque son dossier, et
#: l'installation le recopie chez Elgato. Un profil rangé ailleurs serait
#: absent de la version publiée.
SORTIE = PLUGIN / "profils"

#: Identifiant du plugin, tel qu'il apparaît dans chaque action.
PLUGIN_UUID = "com.zlink.deck"
PLUGIN_NOM = "ZLink"

#: Racine des uuid5. Un tirage au sort donnerait un profil différent à chaque
#: exécution, et le logiciel en accumulerait les copies.
GRAINE = uuid.UUID("6f1d5b7a-3c9e-5f2a-8d41-2b7c9e4a1f30")

#: Codes produit des appareils visés, tels que le logiciel les inscrit.
MODELE_CLASSIQUE = "20GAA9902"      # Stream Deck MK.2 — 5 × 3 touches
MODELE_PLUS = "20GBD9901"           # Stream Deck + — 4 × 2 touches, 4 molettes

#: Bloc de style d'un état. Repris tel quel de ce qu'écrit le logiciel : un
#: champ manquant et la touche s'affiche sans titre.
ETAT = {
    "FontFamily": "",
    "FontSize": 9,
    "FontStyle": "",
    "FontUnderline": False,
    "OutlineThickness": 2,
    "ShowTitle": True,
    "TitleAlignment": "bottom",
    "TitleColor": "#FFFFFF",
}


def _uuid(*morceaux: str) -> str:
    return str(uuid.uuid5(GRAINE, "/".join(morceaux)))


def _action(profil: str, place: str, court: str, nom: str,
            reglages: dict) -> dict:
    """Une action posée sur une touche ou une molette."""
    return {
        "ActionID": _uuid(profil, place, court),
        "LinkedTitle": True,
        "Name": nom,
        "Plugin": {"Name": PLUGIN_NOM, "UUID": PLUGIN_UUID, "Version": _version()},
        "Resources": None,
        "Settings": reglages,
        "State": 0,
        "States": [dict(ETAT)],
        "UUID": f"{PLUGIN_UUID}.{court}",
    }


def _version() -> str:
    manifeste = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    return str(manifeste.get("Version") or "1.0.0.0")


# ── les deux dispositions ────────────────────────────────────────────────────

def _touches_classique() -> dict:
    """5 × 3 : la grille, et de quoi la faire défiler.

    Treize cellules tiennent sur les treize premières touches ; les deux
    dernières paginent. Le plugin compte les touches « Flux » posées pour
    savoir de combien décaler — trente chaînes passent ainsi sur quinze
    touches, sans qu'on ait à déclarer quoi que ce soit.
    """
    places = [f"{col},{ligne}" for ligne in range(3) for col in range(5)]
    actions = {}
    for rang, place in enumerate(places[:13]):
        actions[place] = _action("classique", place, "flux", "Flux",
                                 {"rang": rang})
    for place, sens, nom in ((places[13], "page_precedente", "Page précédente"),
                             (places[14], "page_suivante", "Page suivante")):
        actions[place] = _action("classique", place, "navigation", nom,
                                 {"sens": sens, "libelle": True})
    return actions


#: Ce que porte la rangée du haut d'un Stream Deck +, puis celle du bas.
GESTES_PLUS = [
    ("0,0", "action", "chat", "Chat"),
    ("1,0", "action", "don", "Don"),
    ("2,0", "action", "clip", "Clip"),
    ("3,0", "action", "replay", "Revoir"),
    ("0,1", "action", "muet", "Muet"),
    ("1,1", "action", "favori", "Favori"),
    ("2,1", "navigation", "precedent", "Précédent"),
    ("3,1", "navigation", "suivant", "Suivant"),
]


def _touches_plus() -> dict:
    """4 × 2 : les gestes du plein écran, et de quoi changer de chaîne."""
    actions = {}
    for place, famille, cle, nom in GESTES_PLUS:
        champ = "cle" if famille == "action" else "sens"
        actions[place] = _action("plus", place, famille, nom,
                                 {champ: cle, "libelle": True})
    return actions


def _molettes_plus() -> dict:
    """Les quatre molettes : le plein écran, puis les chaînes épinglées.

    Une molette dont la chaîne n'est pas épinglée reste inerte plutôt que
    d'agir sur autre chose — c'est le plugin qui le décide, pas le profil.
    """
    cibles = ["principal", "0", "1", "2"]
    noms = ["Plein écran", "Épinglée 1", "Épinglée 2", "Épinglée 3"]
    return {
        f"{i},0": _action("plus", f"{i},0", "mixage", nom, {"cible": cible})
        for i, (cible, nom) in enumerate(zip(cibles, noms))
    }


#: Nom de fichier → profil. Le fichier reste en ASCII — il traverse une
#: archive, un explorateur et un importateur — tandis que le nom affiché dans
#: le logiciel, lui, garde ses accents.
PROFILS = {
    "ZLink Grille": {
        "titre": "ZLink — Grille",
        "modele": MODELE_CLASSIQUE,
        "controleurs": [{"Type": "Keypad", "Actions": _touches_classique}],
    },
    "ZLink Regie": {
        "titre": "ZLink — Régie",
        "modele": MODELE_PLUS,
        "controleurs": [{"Type": "Keypad", "Actions": _touches_plus},
                        {"Type": "Encoder", "Actions": _molettes_plus}],
    },
}


# ── écriture ─────────────────────────────────────────────────────────────────

def _ecrire(nom: str, recette: dict) -> pathlib.Path:
    profil = _uuid(nom).upper()
    page = _uuid(nom, "page")
    racine = f"{profil}.sdProfile"

    manifeste = {
        # Sans identifiant matériel : le logiciel l'attribue à l'import, et le
        # profil reste transportable d'une machine à l'autre.
        "Device": {"Model": recette["modele"], "UUID": ""},
        "Name": recette["titre"],
        "Pages": {"Current": page, "Default": page, "Pages": [page]},
        "Version": "3.0",
    }
    contenu = {
        "Controllers": [{"Actions": c["Actions"](), "Type": c["Type"]}
                        for c in recette["controleurs"]],
        "Icon": "",
        "Name": "",
    }

    SORTIE.mkdir(parents=True, exist_ok=True)
    cible = SORTIE / f"{nom}.streamDeckProfile"
    with zipfile.ZipFile(cible, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{racine}/manifest.json",
                         json.dumps(manifeste, ensure_ascii=False))
        archive.writestr(f"{racine}/Profiles/{page.upper()}/manifest.json",
                         json.dumps(contenu, ensure_ascii=False))
    return cible


def main() -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if not (PLUGIN / "manifest.json").exists():
        print("manifeste du plugin introuvable", file=sys.stderr)
        return 1

    for nom, recette in PROFILS.items():
        chemin = _ecrire(nom, recette)
        combien = sum(len(c["Actions"]()) for c in recette["controleurs"])
        print(f"   {chemin.relative_to(ICI.parent)}  ({combien} actions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
