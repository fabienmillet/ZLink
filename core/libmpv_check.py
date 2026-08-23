"""Vérification d'intégrité de libmpv-2.dll.

La DLL est du code natif chargé dans le processus, distribuée hors dépôt et
non signée (comme toutes les builds mpv pour Windows). La seule garantie
possible est l'empreinte relevée au téléchargement, notée dans
libmpv-2.dll.sha256 — voir la section « Dépendances » de claude.md pour la
provenance exacte.

En cas de remplacement volontaire de la DLL, mettre à jour le .sha256 :
    shasum -a 256 libmpv-2.dll > libmpv-2.dll.sha256
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

from core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

LIBMPV_PATH: pathlib.Path = PROJECT_ROOT / "libmpv-2.dll"
SHA256_PATH: pathlib.Path = PROJECT_ROOT / "libmpv-2.dll.sha256"


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_digest() -> str:
    """Première empreinte du fichier .sha256 (format `shasum -a 256`)."""
    line = SHA256_PATH.read_text(encoding="utf-8").strip().split("\n")[0]
    return line.split()[0].lower()


def verify_libmpv() -> bool | None:
    """Compare l'empreinte de la DLL à celle attendue.

    Retourne True si elle correspond, False si elle diffère, None si la
    vérification n'est pas applicable (DLL absente — cas macOS/Linux, où
    libmpv vient du gestionnaire de paquets — ou fichier .sha256 manquant).
    Ne lève jamais : un échec de vérification ne doit pas empêcher le démarrage.
    """
    try:
        if not LIBMPV_PATH.is_file():
            return None
        if not SHA256_PATH.is_file():
            logger.warning(
                "libmpv-2.dll présente mais %s absent — intégrité non vérifiable",
                SHA256_PATH.name,
            )
            return None
        expected = _expected_digest()
        actual = _sha256(LIBMPV_PATH)
        if actual == expected:
            logger.info("libmpv-2.dll: empreinte vérifiée (%s…)", actual[:12])
            return True
        logger.error(
            "libmpv-2.dll: EMPREINTE INATTENDUE — attendu %s…, obtenu %s…. "
            "La bibliothèque a été remplacée depuis son téléchargement : "
            "vérifier sa provenance avant de continuer.",
            expected[:12], actual[:12],
        )
        return False
    except Exception as exc:
        logger.error("Vérification de libmpv-2.dll impossible : %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
    result = verify_libmpv()
    print({True: "OK", False: "ÉCHEC", None: "non applicable"}[result])
    raise SystemExit(0 if result is not False else 1)
