#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Génère les sons d'alerte livrés dans assets/sounds/.

Ils sont SYNTHÉTISÉS plutôt qu'empruntés : pas de fichier tiers à créditer ni
à vérifier côté licence, et deux timbres nettement distincts qu'on reconnaît
sans regarder l'écran — ce qui est tout l'objet de la fonction.

À relancer seulement si l'on veut retoucher les sons ; les fichiers produits
sont versionnés.
"""

from __future__ import annotations

import math
import pathlib
import struct
import wave

TAUX = 44_100
DEST = pathlib.Path(__file__).resolve().parent.parent / "assets" / "sounds"


def _note(freq: float, debut: float, duree: float, gain: float,
          total: list[float]) -> None:
    """Ajoute une note à l'échantillon, avec une enveloppe douce.

    L'attaque et l'extinction progressives évitent le claquement qu'un signal
    coupé net produit — c'est ce qui distingue un son discret d'un bip.
    """
    i0 = int(debut * TAUX)
    n = int(duree * TAUX)
    for i in range(n):
        t = i / TAUX
        # Attaque de 8 ms, extinction exponentielle.
        attaque = min(1.0, t / 0.008)
        extinction = math.exp(-3.2 * t / duree)
        # Une pointe d'harmonique deux : plus proche d'une cloche qu'un sinus nu.
        s = (math.sin(2 * math.pi * freq * t)
             + 0.28 * math.sin(4 * math.pi * freq * t))
        idx = i0 + i
        if idx < len(total):
            total[idx] += gain * attaque * extinction * s


def _ecrire(nom: str, duree: float, notes: list[tuple[float, float, float, float]]) -> None:
    total = [0.0] * int(duree * TAUX)
    for freq, debut, d, gain in notes:
        _note(freq, debut, d, gain, total)
    crete = max(1e-9, max(abs(x) for x in total))
    # Normalisé à -6 dBFS : un son d'alerte ne doit pas couvrir le direct.
    facteur = 0.5 / crete
    dest = DEST / nom
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAUX)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, x * facteur)) * 32_000))
            for x in total))
    print(f"  {dest.name} — {dest.stat().st_size / 1024:.0f} Ko, {duree:.2f} s")


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    # Palier de cagnotte : arpège ascendant, lumineux, il annonce une bonne
    # nouvelle collective.
    _ecrire("milestone.wav", 0.90, [
        (1046.50, 0.00, 0.30, 0.9),   # do6
        (1318.51, 0.10, 0.32, 0.9),   # mi6
        (1567.98, 0.20, 0.60, 1.0),   # sol6
    ])
    # Objectif atteint : deux notes, plus bref et plus sec — ça concerne une
    # seule chaîne, ça ne doit pas prendre autant de place.
    _ecrire("goal.wav", 0.60, [
        (783.99, 0.00, 0.22, 0.9),    # sol5
        (1046.50, 0.11, 0.42, 1.0),   # do6
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
