# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fenêtre de découpe : revoir un clip et en rogner les bords.

Aucun lecteur n'est ouvert et aucun ffmpeg n'est lancé. Ce qui est vérifié,
c'est ce que la fenêtre DÉCIDE : où tombent les bornes, ce qu'elle refuse, et
ce qu'elle fait du fichier d'origine.
"""

from __future__ import annotations

import pytest

# Importé au NIVEAU DU MODULE, comme les autres fichiers de tests : QtWebEngine
# doit être chargé avant qu'une QApplication existe, et `main` l'entraîne.
import main
from core import conversion_clip
from windows import decoupe_clip


@pytest.fixture
def clip(tmp_path):
    source = tmp_path / "clip_193012.ts"
    source.write_bytes(b"\x47" * 512)
    return source


@pytest.fixture
def fenetre(qtbot, clip, monkeypatch):
    """Fenêtre montée sur un fichier réel, avec une durée déjà connue.

    `_armer` est ce que fait le suivi dès que mpv annonce la durée : l'appeler
    à la main évite d'attendre un lecteur qui, hors écran, ne démarre pas.
    """
    f = decoupe_clip.FenetreDecoupe(str(clip))
    qtbot.addWidget(f)
    f._armer(20.0)
    return f


# ── disponibilité ────────────────────────────────────────────────────────────

def test_sans_libmpv_la_decoupe_n_est_pas_proposee(monkeypatch):
    """Elle s'ouvrirait sur du noir et des curseurs à zéro : mieux vaut un
    clip brut enregistré qu'une fenêtre qui ne montre rien."""
    from widgets import mpv_widget

    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", False)
    possible, raison = decoupe_clip.disponible()
    assert possible is False and "libmpv" in raison


def test_sans_ffmpeg_la_decoupe_n_est_pas_proposee(monkeypatch):
    monkeypatch.setattr(conversion_clip, "_ffmpeg", lambda: "")
    possible, raison = decoupe_clip.disponible()
    assert possible is False and "ffmpeg" in raison


# ── bornes ───────────────────────────────────────────────────────────────────

def test_les_bornes_couvrent_tout_le_clip_au_depart(fenetre):
    """On ouvre sur ce qui a été pris, pas sur une sélection arbitraire."""
    assert fenetre.bornes() == (0.0, 20.0)


def test_les_poignees_se_reglent_a_la_fraction_de_seconde(fenetre):
    fenetre._debut.setValue(int(5.25 * decoupe_clip._PAS))
    fenetre._fin.setValue(int(12.5 * decoupe_clip._PAS))
    assert fenetre.bornes() == (5.25, 12.5)


def test_le_debut_ne_depasse_pas_la_fin(fenetre):
    """Sans cela on pourrait demander une portion négative."""
    fenetre._fin.setValue(int(8 * decoupe_clip._PAS))
    fenetre._debut.setValue(int(15 * decoupe_clip._PAS))
    debut, fin = fenetre.bornes()
    assert debut < fin


def test_la_fin_ne_repasse_pas_avant_le_debut(fenetre):
    fenetre._debut.setValue(int(10 * decoupe_clip._PAS))
    fenetre._fin.setValue(int(3 * decoupe_clip._PAS))
    debut, fin = fenetre.bornes()
    assert debut < fin


def test_la_duree_gardee_est_annoncee(fenetre):
    fenetre._debut.setValue(int(5 * decoupe_clip._PAS))
    fenetre._fin.setValue(int(12 * decoupe_clip._PAS))
    assert "7.0" in fenetre._garde.text()


def test_une_selection_trop_courte_desactive_l_enregistrement(fenetre):
    """Un fichier d'un dixième de seconde ne se lit nulle part."""
    fenetre._debut.setValue(int(5 * decoupe_clip._PAS))
    fenetre._fin.setValue(int(5.4 * decoupe_clip._PAS))
    assert fenetre._valider.isEnabled() is False


def test_le_bouton_est_eteint_tant_que_la_duree_est_inconnue(qtbot, clip):
    """Avant la première image, mpv ne connaît ni position ni durée : le
    bouton proposerait une coupe de zéro seconde."""
    f = decoupe_clip.FenetreDecoupe(str(clip))
    qtbot.addWidget(f)
    assert f._valider.isEnabled() is False


# ── enregistrement ───────────────────────────────────────────────────────────

def test_la_decoupe_est_demandee_avec_les_bornes_posees(fenetre, monkeypatch,
                                                        qtbot):
    appels = []

    def _faux(source, debut, fin, cible=None):
        appels.append((str(source), debut, fin))
        return "/tmp/decoupe.mp4", ""

    monkeypatch.setattr(conversion_clip, "decouper", _faux)
    fenetre._debut.setValue(int(4 * decoupe_clip._PAS))
    fenetre._fin.setValue(int(9 * decoupe_clip._PAS))

    with qtbot.waitSignal(fenetre.enregistre, timeout=3000) as bloc:
        fenetre._enregistrer()
    assert appels and appels[0][1:] == (4.0, 9.0)
    assert bloc.args == ["/tmp/decoupe.mp4", 5.0]


def test_une_decoupe_qui_echoue_laisse_reessayer(fenetre, monkeypatch, qtbot):
    """La fenêtre ne se ferme pas sur un échec : le clip est encore là, et
    l'utilisateur peut corriger ses bornes."""
    monkeypatch.setattr(conversion_clip, "decouper",
                        lambda *a, **k: ("", "ffmpeg a refusé"))
    fenetre._debut.setValue(0)
    fenetre._fin.setValue(int(10 * decoupe_clip._PAS))
    fenetre._enregistrer()
    qtbot.waitUntil(lambda: fenetre._valider.isEnabled(), timeout=3000)
    assert "refusé" in fenetre._note.text()


# ── ce qu'on fait du .ts d'origine ──────────────────────────────────────────

def test_le_ts_est_efface_quand_l_utilisateur_veut_des_mp4(clip, monkeypatch):
    """Le réglage de conversion est réutilisé plutôt qu'un second inventé :
    qui demande des MP4 ne veut pas garder le brut à côté."""
    from core import config_store

    monkeypatch.setattr(config_store, "load",
                        lambda: {"clips": {"convert_mp4": True}})
    main._sur_decoupe_enregistree(str(clip), "/tmp/final.mp4")
    assert not clip.exists()


def test_le_ts_reste_quand_l_utilisateur_garde_ses_ts(clip, monkeypatch):
    from core import config_store

    monkeypatch.setattr(config_store, "load",
                        lambda: {"clips": {"convert_mp4": False}})
    main._sur_decoupe_enregistree(str(clip), "/tmp/final.mp4")
    assert clip.exists()


# ── aiguillage depuis le clip ────────────────────────────────────────────────

def test_sans_decoupe_possible_la_conversion_reprend_la_main(clip, monkeypatch):
    """Rendre False plutôt que lever, c'est ce qui laisse un clip brut être
    enregistré quand la découpe n'est pas offerte."""
    from core import config_store

    monkeypatch.setattr(config_store, "load",
                        lambda: {"clips": {"decouper": True, "convert_mp4": True}})
    monkeypatch.setattr(decoupe_clip, "disponible",
                        lambda: (False, "libmpv est absent"))
    convertis = []
    monkeypatch.setattr(conversion_clip, "convertir_en_arriere_plan",
                        lambda src, supprimer_source=False: convertis.append(src))
    main._traiter_le_clip("zerator", str(clip))
    assert convertis == [str(clip)]


def test_la_decoupe_passe_avant_la_conversion(clip, monkeypatch):
    """La lancer après reviendrait à convertir deux fois, et à découper une
    copie plutôt que l'original."""
    from core import config_store

    monkeypatch.setattr(config_store, "load",
                        lambda: {"clips": {"decouper": True, "convert_mp4": True}})
    monkeypatch.setattr(main, "_ouvrir_la_decoupe", lambda ch, parent: True)
    convertis = []
    monkeypatch.setattr(conversion_clip, "convertir_en_arriere_plan",
                        lambda *a, **k: convertis.append(a))
    main._traiter_le_clip("zerator", str(clip))
    assert convertis == []


def test_un_fichier_qui_n_est_pas_un_ts_est_ignore(monkeypatch):

    ouvertures = []
    monkeypatch.setattr(main, "_ouvrir_la_decoupe",
                        lambda ch, parent: ouvertures.append(ch) or True)
    main._traiter_le_clip("zerator", "/tmp/deja.mp4")
    main._traiter_le_clip("zerator", "")
    assert ouvertures == []


# ── extinction ───────────────────────────────────────────────────────────────

class _FauxLecteur:
    """Ne note qu'une chose : est-ce qu'on l'a bien arrêté ?"""

    def __init__(self) -> None:
        self.eteint = False

    def shutdown(self) -> None:
        self.eteint = True

    def stop(self) -> None:
        self.eteint = True


@pytest.mark.parametrize("sortie", ["accept", "reject", "close"])
def test_le_lecteur_s_arrete_par_toutes_les_sorties(fenetre, sortie):
    """`accept()` et `reject()` MASQUENT la fenêtre, ils ne la ferment pas :
    le rappel de fermeture n'était jamais appelé et le son du clip continuait
    après l'enregistrement — plus de fenêtre, mais mpv jouait toujours.
    """
    lecteur = _FauxLecteur()
    fenetre._lecteur = lecteur
    # Affichée d'abord : `close()` sur une fenêtre jamais montrée n'envoie
    # aucun événement de fermeture, et ce cas-là n'arrive pas en vrai.
    fenetre.show()
    getattr(fenetre, sortie)()
    assert lecteur.eteint is True
    assert fenetre._suivi.isActive() is False


def test_le_suivi_ne_touche_plus_un_lecteur_eteint(fenetre):
    """Le timer peut avoir un tour en vol au moment où la fenêtre se ferme."""
    fenetre._lecteur = _FauxLecteur()
    fenetre.accept()
    fenetre._rafraichir()          # ne doit pas lever
