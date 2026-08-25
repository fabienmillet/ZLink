#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Surveillance GPU macOS — utilisation globale et activité du moteur de décodage.

Lit les compteurs IOAccelerator (aucun sudo requis). Le compteur VCNxDec est
le moteur de décodage vidéo matériel : c'est lui qui porte le budget < 50 %
visé pour la grille (cf. claude.md).

    python tools_gpu_watch.py [intervalle_secondes]
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

_UTIL = re.compile(r'"Device Utilization %"=(\d+)')
_VCN = re.compile(r'"HWChannel (VCN\d+Dec) \| Commands Completed"=(\d+)')


def sample() -> tuple[int, int]:
    out = subprocess.run(
        ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
        capture_output=True, text=True, timeout=10,
    ).stdout
    util = max((int(m) for m in _UTIL.findall(out)), default=0)
    decode = sum(int(v) for _, v in _VCN.findall(out))
    return util, decode


def main() -> None:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    print(f"{'heure':<10}{'GPU %':>7}{'décodage (cmds/s)':>20}")
    _, prev = sample()
    try:
        while True:
            time.sleep(interval)
            util, decode = sample()
            # Les compteurs sont globaux et remis à zéro quand un contexte GPU
            # disparaît : un delta négatif signale un reset, pas une activité.
            rate = max(0.0, (decode - prev) / interval)
            prev = decode
            bar = "█" * int(util / 5)
            print(f"{time.strftime('%H:%M:%S'):<10}{util:>6}%{rate:>19.0f}  {bar}")
    except KeyboardInterrupt:
        print("\narrêt")


if __name__ == "__main__":
    main()
