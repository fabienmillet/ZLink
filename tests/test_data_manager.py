# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fusion des deux sources de données, et pas entre paliers de cagnotte.

zevent.fr fait foi pendant l'event ; hors event il ne renvoie ni lieu, ni
cagnotte par streamer, ni statut live, et l'API communautaire prend le relais.
Écraser les données ZEvent avec celles de l'API communautaire serait une
régression silencieuse : c'est ce que ces tests verrouillent.
"""

from __future__ import annotations

import pytest

from core import data_manager
from core.api_client import GlobalStats, Participation, StreamerInfo
from core.data_manager import (
    DataManager,
    _apply_participations_to_streamers,
    _completer_streamer,
)


def _streamer(**kw) -> StreamerInfo:
    base = dict(
        twitch_login="zerator", display="", online=False, game="",
        location="", viewers=0, donation=0.0, donation_formatted="",
        profile_url="",
    )
    base.update(kw)
    return StreamerInfo(**base)


def _participation(**kw) -> Participation:
    base = dict(
        streamer_id="sid", participation_id="pid", twitch_login="zerator",
        display="ZeratoR", location="LAN", live=True, game="Minecraft",
        viewers=42000, donation=694000.0, profile_url="https://s.test/z.png",
    )
    base.update(kw)
    return Participation(**base)


# ── _completer_streamer ──────────────────────────────────────────────────────

def test_champs_vides_sont_combles():
    s = _streamer()
    _completer_streamer(s, _participation(), live_mode=False)
    assert s.display == "ZeratoR"
    assert s.location == "LAN"
    assert s.donation == pytest.approx(694000.0)
    assert s.donation_formatted
    assert s.profile_url == "https://s.test/z.png"


def test_en_mode_live_les_donnees_zevent_ne_sont_pas_ecrasees():
    s = _streamer(online=True, viewers=1234, game="Chess", title="Mon titre")
    _completer_streamer(s, _participation(live=False, viewers=0, game="Autre"),
                        live_mode=True)
    assert s.online is True
    assert s.viewers == 1234
    assert s.game == "Chess"
    assert s.title == "Mon titre"


def test_hors_event_le_direct_vient_de_l_api_communautaire():
    s = _streamer(online=False, viewers=0, game="", title="vieux titre")
    _completer_streamer(s, _participation(), live_mode=False)
    assert s.online is True
    assert s.viewers == 42000
    assert s.game == "Minecraft"
    assert s.title == "", "le titre ZEvent n'a plus cours hors event"


def test_un_display_deja_renseigne_est_conserve():
    s = _streamer(display="Nom choisi")
    _completer_streamer(s, _participation(), live_mode=False)
    assert s.display == "Nom choisi"


def test_un_display_egal_au_login_compte_comme_absent():
    """L'API principale met le login faute de mieux : ce n'est pas un vrai nom."""
    s = _streamer(display="zerator")
    _completer_streamer(s, _participation(), live_mode=False)
    assert s.display == "ZeratoR"


def test_une_cagnotte_deja_connue_n_est_pas_remplacee():
    s = _streamer(donation=100.0, donation_formatted="100 €")
    _completer_streamer(s, _participation(), live_mode=False)
    assert s.donation == pytest.approx(100.0)


def test_une_cagnotte_nulle_cote_participation_ne_remplace_rien():
    s = _streamer(donation=0.0)
    _completer_streamer(s, _participation(donation=0.0), live_mode=False)
    assert s.donation == 0.0


# ── _apply_participations_to_streamers ───────────────────────────────────────

def _stats(mode: str) -> GlobalStats:
    return GlobalStats(donation_total=0.0, donation_formatted="",
                       viewers_total=0, website_mode=mode)


def test_appariement_insensible_a_la_casse():
    s = _streamer(twitch_login="ZeratoR")
    _apply_participations_to_streamers([s], _stats("offline"), [_participation()])
    assert s.location == "LAN"


def test_streamer_sans_participation_reste_intact():
    s = _streamer(twitch_login="inconnu")
    _apply_participations_to_streamers([s], _stats("offline"), [_participation()])
    assert s.location == "" and s.donation == 0.0


def test_total_des_viewers_recalcule_hors_event():
    stats = _stats("offline")
    _apply_participations_to_streamers(
        [_streamer()], stats,
        [_participation(live=True, viewers=100),
         _participation(twitch_login="autre", live=False, viewers=999)],
    )
    assert stats.viewers_total == 100, "les hors-ligne ne comptent pas"


def test_total_des_viewers_intact_en_mode_live():
    stats = _stats("live")
    stats.viewers_total = 555
    _apply_participations_to_streamers([_streamer()], stats,
                                       [_participation(viewers=100)])
    assert stats.viewers_total == 555


# ── paliers de cagnotte ──────────────────────────────────────────────────────

@pytest.mark.parametrize("total,pas", [
    (0, 250_000.0),
    (999_999, 250_000.0),
    (1_000_000, 500_000.0),
    (4_999_999, 500_000.0),
    (5_000_000, 1_000_000.0),
    (16_000_000, 1_000_000.0),
])
def test_le_pas_suit_l_ordre_de_grandeur(total, pas):
    """Un pas fixe conviendrait mal aux deux bouts de l'édition.

    À 250 000 €, le premier million produirait quarante annonces ; à un
    million, les premières heures n'en produiraient aucune.
    """
    assert DataManager._milestone_step(total) == pas


# ── shows qui débordent sur le lendemain ─────────────────────────────────────

def _show(nom, jour, debut, id_=None):
    from core.api_client import EventItem

    return EventItem(id=id_ or f"{nom}-{jour}", name=nom, day=jour,
                     start_local=debut, end_local="02:30", description="")


def test_un_show_rendu_par_deux_journees_n_est_compte_qu_une_fois(qapp):
    """L'API rend un show qui déborde dans les DEUX journées interrogées.
    Tant qu'il portait le jour demandé, les deux copies se distinguaient —
    mal. Maintenant qu'il porte son vrai jour de début, ce serait deux fois
    la même ligne dans l'onglet Programme.
    """
    dm = data_manager.DataManager()
    dm.stop_polling()
    deborde = _show("DJ Set Big Edition", "2026-09-05", "23:00", id_="dj")
    # Le même objet revient dans la réponse du samedi ET du dimanche.
    resultats = [[] for _ in data_manager._EVENT_DAYS]
    resultats[2] = [deborde]
    resultats[3] = [deborde]

    dm._apply_events(resultats)
    assert [e.name for e in dm.get_events_for_day("2026-09-05")] == [
        "DJ Set Big Edition"]
    assert dm.get_events_for_day("2026-09-06") == []


def test_les_shows_sont_ranges_sur_leur_jour_de_debut(qapp):
    """`get_events_for_day` doit rendre ce que son nom annonce, et non ce que
    la requête qui les a ramenés annonçait."""
    dm = data_manager.DataManager()
    dm.stop_polling()
    resultats = [[] for _ in data_manager._EVENT_DAYS]
    # Rendus par la requête du dimanche, mais commencés le samedi.
    resultats[3] = [_show("Nuit blanche", "2026-09-05", "23:00"),
                    _show("Matin", "2026-09-06", "08:00")]

    dm._apply_events(resultats)
    assert [e.name for e in dm.get_events_for_day("2026-09-05")] == ["Nuit blanche"]
    assert [e.name for e in dm.get_events_for_day("2026-09-06")] == ["Matin"]


def test_une_journee_en_erreur_n_emporte_pas_les_autres(qapp, caplog):
    dm = data_manager.DataManager()
    dm.stop_polling()
    resultats = [[] for _ in data_manager._EVENT_DAYS]
    resultats[2] = OSError("réseau coupé")
    resultats[3] = [_show("Matin", "2026-09-06", "08:00")]

    dm._apply_events(resultats)
    assert [e.name for e in dm.get_events_for_day("2026-09-06")] == ["Matin"]
