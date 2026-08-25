#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Signe les artefacts d'une release avec la clé Ed25519 du projet.

Utilisé par .github/workflows/release.yml. La clé privée arrive par la variable
d'environnement SIGNING_KEY (secret du dépôt), jamais par un fichier ni un
argument : un argument de ligne de commande est visible dans les logs et la
table des processus.

Produit un fichier « <artefact>.sig » de 64 octets par artefact.
"""

from __future__ import annotations

import os
import pathlib
import sys

from Crypto.Signature import eddsa


def main(argv: list[str]) -> int:
    key_hex = (os.environ.get("SIGNING_KEY") or "").strip()
    if len(key_hex) != 64:
        print("SIGNING_KEY absente ou malformée (64 caractères hex attendus)",
              file=sys.stderr)
        return 1
    try:
        key = eddsa.import_private_key(bytes.fromhex(key_hex))
    except Exception as exc:
        print(f"Clé privée illisible : {exc}", file=sys.stderr)
        return 1

    signer = eddsa.new(key, "rfc8032")
    signed = 0
    for name in argv:
        path = pathlib.Path(name)
        # Ne pas signer une signature, ni un répertoire.
        if not path.is_file() or path.suffix == ".sig":
            continue
        sig = signer.sign(path.read_bytes())
        path.with_name(path.name + ".sig").write_bytes(sig)
        print(f"signé : {path.name} ({len(sig)} octets)")
        signed += 1

    if signed == 0:
        print("Aucun artefact à signer", file=sys.stderr)
        return 1
    print(f"{signed} artefact(s) signé(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
