# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
"""Point d'entrée de l'exécutable streamlink livré avec ZLink.

ZLink appelle streamlink comme un PROCESSUS séparé — c'est ce qui permet de le
tuer net quand une cellule change de flux. Dans une version empaquetée, aucun
interpréteur Python n'est disponible pour l'exécuter : on produit donc un
second exécutable, posé à côté de celui de l'application, que
`core.stream_manager._streamlink_exe()` trouve en premier puisqu'il regarde le
dossier de `sys.executable`.
"""

import sys

from streamlink_cli.main import main

if __name__ == "__main__":
    sys.exit(main())
