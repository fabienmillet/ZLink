# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Version de ZLink — source unique de vérité.

Le tag git d'une release doit valoir « v » + cette chaîne : le workflow de
publication refuse de continuer si les deux divergent, pour qu'on ne puisse pas
publier une v0.2.0 dont le binaire s'annonce en 0.1.0.
"""

from __future__ import annotations

import re

__version__ = "0.2.3"

# Dépôt interrogé pour les mises à jour.
GITHUB_OWNER = "fabienmillet"
GITHUB_REPO = "ZLink"

_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)


def parse(version: str) -> tuple[int, int, int, str] | None:
    """« v1.2.3-beta.1 » → (1, 2, 3, 'beta.1'). None si la forme est inattendue."""
    m = _SEMVER_RE.match((version or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""


def is_newer(candidate: str, current: str = __version__) -> bool:
    """Vrai si `candidate` est strictement plus récent que `current`.

    Une version sans suffixe l'emporte sur la même avec suffixe : 1.2.0 est plus
    récent que 1.2.0-rc1, conformément à semver. Une chaîne non reconnue n'est
    jamais considérée comme plus récente — on ne propose pas une mise à jour
    dont on n'a pas su lire le numéro.
    """
    a, b = parse(candidate), parse(current)
    if a is None or b is None:
        return False
    if a[:3] != b[:3]:
        return a[:3] > b[:3]
    # Mêmes chiffres : la version finale bat la pré-version.
    if a[3] == b[3]:
        return False
    if not a[3]:
        return True
    if not b[3]:
        return False
    return a[3] > b[3]


# ── Identification d'une construction ────────────────────────────────────────
# Deux binaires portant « 0.1.0 » ne sont pas forcément le même code : pendant
# le développement, la version ne bouge qu'au moment d'une publication. Le
# commit lève l'ambiguïté quand on reçoit une capture d'écran ou un journal.

_commit_cache: str | None = None


def commit() -> str:
    """Commit court de la construction, ou "" si on ne peut pas le savoir.

    Deux sources, dans cet ordre : le fichier `core/build_info.py` que la chaîne
    de publication écrit avant d'empaqueter, puis — seulement hors paquet gelé —
    le dépôt git de travail. Un paquet installé n'a ni git ni dépôt : l'appel
    système y serait vain, et on ne le tente pas.
    """
    global _commit_cache
    if _commit_cache is not None:
        return _commit_cache

    _commit_cache = ""
    try:
        from core.build_info import COMMIT  # type: ignore[attr-defined]
        _commit_cache = str(COMMIT).strip()[:7]
        return _commit_cache
    except Exception:
        pass

    from core.paths import FROZEN, PROJECT_ROOT
    if not FROZEN:
        import subprocess

        from core.sous_processus import sans_fenetre
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                timeout=2.0, **sans_fenetre(),
            )
            if r.returncode == 0:
                _commit_cache = r.stdout.strip()
        except Exception:
            # git absent, dépôt absent, appel trop lent : le numéro de version
            # seul reste parfaitement utilisable.
            pass
    return _commit_cache


def is_dev_build() -> bool:
    """Vrai pour un lancement depuis les sources, faux pour un paquet publié."""
    from core.paths import FROZEN
    try:
        # Module ÉCRIT À LA CONSTRUCTION, absent des sources : son absence
        # est précisément ce qui signale un lancement depuis le dépôt.
        import core.build_info  # type: ignore[import-not-found]  # noqa: F401
        return False
    except Exception:
        return not FROZEN


def display_version() -> str:
    """Version telle qu'on la montre à l'utilisateur.

    « 0.1.0 » pour une version publiée, « 0.1.0-dev+1a2b3c4 » depuis les
    sources : personne ne doit croire tenir la version publiée alors qu'il fait
    tourner un dépôt de travail.
    """
    if not is_dev_build():
        return __version__
    c = commit()
    return f"{__version__}-dev+{c}" if c else f"{__version__}-dev"
