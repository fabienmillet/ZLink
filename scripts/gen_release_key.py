#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Génère la paire de clés Ed25519 servant à signer les releases.

À exécuter UNE SEULE FOIS, en local, jamais dans la CI.

  - La clé publique va dans core/updater.py (RELEASE_PUBKEY_HEX) et sera donc
    distribuée avec l'application : c'est elle qui permet aux utilisateurs de
    vérifier qu'une mise à jour vient bien de vous.
  - La clé privée va dans les secrets du dépôt GitHub, sous le nom
    RELEASE_SIGNING_KEY. Elle ne doit jamais être committée ni transiter par
    autre chose qu'un canal sûr.

Perdre la clé privée oblige à en publier une nouvelle et à mettre à jour tous
les clients : gardez-en une sauvegarde hors ligne.
"""

from __future__ import annotations

from Crypto.PublicKey import ECC


def main() -> int:
    key = ECC.generate(curve="ed25519")
    # key.seed pour la privée : export_key(format="raw") refuse les clés
    # privées. C'est bien cette graine de 32 octets que eddsa.import_private_key
    # attend en retour.
    priv = key.seed.hex()
    pub = key.public_key().export_key(format="raw").hex()

    print("Clé PRIVÉE — secret GitHub « RELEASE_SIGNING_KEY »")
    print("  (à ne jamais committer, ni coller dans un ticket)")
    print(f"  {priv}")
    print()
    print("Clé PUBLIQUE — à coller dans core/updater.py :")
    print(f'  RELEASE_PUBKEY_HEX = "{pub}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
