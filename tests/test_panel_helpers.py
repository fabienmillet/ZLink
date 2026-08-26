# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fonctions de mise en forme et de persistance du panel.

Deux d'entre elles méritent une attention particulière :
`_infobulle`, parce que Qt rend le texte riche dans les infobulles et que ces
textes viennent d'une API ; et `_save_reminders`, qui partage config.json avec
les favoris et les réglages — une écriture ne doit pas en effacer une autre.
"""

from __future__ import annotations

import json

import pytest

from windows import panel


# ── montants et audiences ────────────────────────────────────────────────────

@pytest.mark.parametrize("n,attendu", [
    (0, "0 €"),
    (5, "5 €"),
    # Separateur de milliers : espace INSECABLE (U+00A0), echappee ici pour
    # ne pas dependre d'un caractere invisible dans le fichier de test.
    (1000, "1\u00a0000 €"),
    (1154212, "1\u00a0154\u00a0212 €"),
])
def test_euros_avec_espace_insecable(n, attendu):
    assert panel._fmt_euros(n) == attendu


@pytest.mark.parametrize("n,attendu", [
    (0, "0"), (1, "1"), (999, "999"),
    (1000, "1.0k"), (1500, "1.5k"), (42000, "42.0k"), (999_999, "1000.0k"),
    (1_000_000, "1.0M"), (16_172_355, "16.2M"),
])
def test_audiences_abregees(n, attendu):
    assert panel._fmt_viewers(n) == attendu


# ── dates et durées du programme ─────────────────────────────────────────────

def test_en_tete_de_jour_en_francais():
    assert panel._day_header_fr("2025-09-05") == "VENDREDI 5 SEPTEMBRE"


def test_en_tete_de_jour_illisible_reste_tel_quel():
    assert panel._day_header_fr("pas une date") == "pas une date"
    assert panel._day_header_fr("") == ""


@pytest.mark.parametrize("brut,attendu", [
    ("14:30", "14h30"), ("00:00", "00h00"), ("9:05", "9h05"),
    ("", ""), ("sans deux-points", "sans deux-points"),
])
def test_heure_a_la_francaise(brut, attendu):
    assert panel._fmt_time_fr(brut) == attendu


@pytest.mark.parametrize("debut,fin,attendu", [
    ("14:30", "17:00", "2h30"),
    ("14:00", "16:00", "2h"),
    ("14:00", "14:45", "45min"),
    ("14:00", "14:00", "24h"),        # même heure : un tour complet
])
def test_duree_d_un_show(debut, fin, attendu):
    assert panel._fmt_duration(debut, fin) == attendu


def test_duree_qui_franchit_minuit():
    """Un show de 23h à 1h dure deux heures, pas moins vingt-deux."""
    assert panel._fmt_duration("23:00", "01:00") == "2h"


@pytest.mark.parametrize("debut,fin", [
    ("", ""), ("14:30", ""), ("abc", "def"), (None, None), ("14h30", "17h00"),
])
def test_duree_illisible_rend_une_chaine_vide(debut, fin):
    assert panel._fmt_duration(debut, fin) == ""


# ── infobulles ───────────────────────────────────────────────────────────────

def test_infobulle_neutralise_le_texte_riche():
    """Qt rend le texte riche dans les infobulles.

    Un nom d'objectif contenant une balise serait interprété, et une « image »
    distante y déclencherait une requête réseau à l'insu de l'utilisateur.
    """
    sortie = panel._infobulle('<img src="https://pisteur.test/x.png">')
    assert "<img" not in sortie
    assert "&lt;img" in sortie
    assert sortie.startswith("<qt>") and sortie.endswith("</qt>")


@pytest.mark.parametrize("brut", ["<b>gras</b>", "a & b", '"guillemets"', "<script>"])
def test_infobulle_echappe_tout_ce_qui_compte(brut):
    sortie = panel._infobulle(brut)
    assert "<" not in sortie[4:-5] or "&lt;" in sortie


def test_infobulle_accepte_autre_chose_qu_une_chaine():
    assert panel._infobulle(42) == "<qt>42</qt>"


# ── clé de cache d'avatar ────────────────────────────────────────────────────

def test_un_login_sert_de_cle_tel_quel():
    assert panel._avatar_cache_key("zerator", "https://x.test/a.png") == "zerator"


def test_un_invite_sans_login_recoit_une_cle_derivee():
    """Les artistes invités n'ont pas de compte Twitch.

    Sans clé dérivée, tous partageraient le fichier "".png et afficheraient la
    même image — le dernier téléchargé écrasant les autres.
    """
    a = panel._avatar_cache_key("", "https://x.test/a.png")
    b = panel._avatar_cache_key("", "https://x.test/b.png")
    assert a.startswith("guest_") and len(a) == len("guest_") + 16
    assert a != b


def test_meme_url_meme_cle():
    url = "https://x.test/a.png"
    assert panel._avatar_cache_key("", url) == panel._avatar_cache_key("", url)


def test_ni_login_ni_url_donne_une_cle_vide():
    assert panel._avatar_cache_key("", "") == ""


# ── rappels du programme ─────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path, monkeypatch):
    cible = tmp_path / "config.json"
    monkeypatch.setattr(panel, "_CFG_PATH", cible)
    return cible


def test_aucun_rappel_au_depart(config):
    assert panel._load_reminders() == set()


def test_rappels_aller_retour(config):
    panel._save_reminders({"b", "a"})
    assert panel._load_reminders() == {"a", "b"}


def test_enregistrer_un_rappel_n_efface_pas_le_reste_de_la_config(config):
    """config.json est partagé avec les favoris et les réglages.

    Chacun fait une lecture-modification-écriture : oublier la relecture ferait
    disparaître le travail des autres.
    """
    config.write_text(json.dumps({"max_active_streams": 20,
                                  "favorite_logins": ["zerator"]}),
                      encoding="utf-8")
    panel._save_reminders({"show-1"})
    reste = json.loads(config.read_text(encoding="utf-8"))
    assert reste["max_active_streams"] == 20
    assert reste["favorite_logins"] == ["zerator"]
    assert reste["programme_reminders"] == ["show-1"]


def test_config_corrompue_ne_leve_pas(config):
    config.write_text("{pas du json", encoding="utf-8")
    assert panel._load_reminders() == set()


def test_rappels_d_un_type_inattendu_ne_levent_pas(config):
    config.write_text('{"programme_reminders": null}', encoding="utf-8")
    assert panel._load_reminders() == set()


# ── Intervenants : un nom seul ne se reconnaît pas ───────────────────────────

@pytest.mark.parametrize("entree,attendu", [
    ("Ultia", ("Ultia", "", "")),
    (("Ultia", "ultia", "https://cdn/u.png"), ("Ultia", "ultia", "https://cdn/u.png")),
    (("Invité", "", ""), ("Invité", "", "")),
    # Un artiste de show n'a ni login ni avatar : None ne doit pas se propager
    # jusqu'aux appels Qt, qui attendent des chaînes.
    (("Invité", None, None), ("Invité", "", "")),
])
def test_un_intervenant_se_normalise_en_triplet(entree, attendu):
    assert panel._personne(entree) == attendu


def test_la_popup_des_participants_porte_les_avatars(qtbot):
    """Le nom seul demandait de connaître la personne pour la reconnaître."""
    dlg = panel._ParticipantsDialog(
        "Tournoi",
        [("Anaee", "anaee", "https://cdn/a.png"), "Sans compte"],
        [("Ultia", "ultia", "https://cdn/u.png")],
    )
    qtbot.addWidget(dlg)
    puces = dlg.findChildren(panel.QFrame, "personChip")
    assert len(puces) == 3, "un hôte et deux participants"
    # Chaque puce porte une pastille d'avatar et un nom, dans cet ordre.
    labels = puces[0].findChildren(panel.QLabel)
    assert len(labels) == 2
    assert labels[1].text() == "Ultia"


def test_une_popup_sans_personne_ne_leve_pas(qtbot):
    dlg = panel._ParticipantsDialog("Vide", [], [])
    qtbot.addWidget(dlg)
    assert dlg.findChildren(panel.QFrame, "personChip") == []


# ── Onglet Goals : le cache arrive après la liste ────────────────────────────
#
# L'onglet sélectionne son premier participant dès que la liste des streamers
# arrive ; le prefetch des objectifs, lui, arrive après. Le cache se remplissait
# donc APRÈS l'affichage, et « Aucun objectif trouvé » restait figé sur des
# objectifs désormais connus.

class _FauxStreamer:
    def __init__(self, login: str, pid: str = "p1",
                 donation: float = 0.0) -> None:
        self.twitch_login = login
        self.display = login
        self.participation_id = pid
        self.donation = donation
        self.profile_url = ""


class _FauxObjectif:
    def __init__(self, nom: str, montant: float = 100.0) -> None:
        self.id = nom
        self.name = nom
        self.amount = montant
        self.accomplished = False
        self.category = ""
        self.links = []


def _onglet_goals(qtbot, monkeypatch):
    onglet = panel._GoalsTab()
    qtbot.addWidget(onglet)
    # Aucun accès réseau : la sélection déclenche sinon un vrai fetch.
    monkeypatch.setattr(onglet, "_do_fetch", lambda *a: None)
    return onglet


def test_le_prefetch_rafraichit_l_affichage_courant(qtbot, monkeypatch):
    onglet = _onglet_goals(qtbot, monkeypatch)
    onglet.set_streamers([_FauxStreamer("domingo")])
    montres: list[list] = []
    monkeypatch.setattr(onglet, "_show_goals", montres.append)
    onglet.seed_cache({"domingo": [_FauxObjectif("Manger un piment")]})
    assert len(montres) == 1 and montres[0][0].name == "Manger un piment"


def test_un_prefetch_qui_ne_concerne_pas_le_streamer_affiche_ne_rafraichit_rien(
        qtbot, monkeypatch):
    onglet = _onglet_goals(qtbot, monkeypatch)
    onglet.set_streamers([_FauxStreamer("domingo")])
    montres: list[list] = []
    monkeypatch.setattr(onglet, "_show_goals", montres.append)
    onglet.seed_cache({"zerator": [_FauxObjectif("Autre")]})
    assert montres == []


def test_un_prefetch_vide_ne_touche_a_rien(qtbot, monkeypatch):
    onglet = _onglet_goals(qtbot, monkeypatch)
    onglet.set_streamers([_FauxStreamer("domingo")])
    onglet._cache["domingo"] = [_FauxObjectif("Déjà là")]
    onglet.seed_cache({})
    assert len(onglet._cache["domingo"]) == 1


def test_une_reponse_vide_n_ecrase_pas_un_cache_garni(qtbot, monkeypatch):
    """Une requête qui échoue ne prouve pas l'absence d'objectifs.

    Le prefetch peut avoir répondu pendant que la requête tournait ; écraser
    sur un tableau vide effaçait ce qu'on savait déjà.
    """
    onglet = _onglet_goals(qtbot, monkeypatch)
    onglet.set_streamers([_FauxStreamer("domingo")])
    onglet._cache["domingo"] = [_FauxObjectif("Déjà là")]
    montres: list[list] = []
    monkeypatch.setattr(onglet, "_show_goals", montres.append)
    onglet._on_goals_arrived("domingo", [])
    assert len(onglet._cache["domingo"]) == 1
    assert montres and montres[0][0].name == "Déjà là"


def test_une_reponse_garnie_remplace_bien_le_cache(qtbot, monkeypatch):
    onglet = _onglet_goals(qtbot, monkeypatch)
    onglet.set_streamers([_FauxStreamer("domingo")])
    onglet._cache["domingo"] = []
    onglet._on_goals_arrived("domingo", [_FauxObjectif("Nouveau")])
    assert onglet._cache["domingo"][0].name == "Nouveau"


# ── Console de mixage : le curseur suit le son réel ─────────────────────────
#
# Le plein écran se règle aussi au clavier et à sa propre glissière. Sans
# retour, la tranche du mixer restait sur la valeur d'avant.

def _mixer(qtbot):
    m = panel._MixerTab()
    qtbot.addWidget(m)
    m.set_main_stream("zerator")
    m.set_pinned([])
    return m


def test_le_curseur_principal_suit_le_plein_ecran(qtbot):
    m = _mixer(qtbot)
    m.set_main_volume(37)
    assert m._strips[m._MAIN]._slider.value() == 37


def test_le_retour_ne_repart_pas_vers_le_plein_ecran(qtbot):
    """Sinon le plein écran réappliquerait la valeur et le curseur se
    battrait contre lui-même dès qu'on le déplace."""
    m = _mixer(qtbot)
    recus: list[int] = []
    m.main_volume_changed.connect(recus.append)
    m.set_main_volume(42)
    assert recus == []


@pytest.mark.parametrize("valeur,attendu", [(-10, 0), (0, 0), (150, 100)])
def test_le_volume_recu_est_borne(qtbot, valeur, attendu):
    m = _mixer(qtbot)
    m.set_main_volume(valeur)
    assert m._strips[m._MAIN]._slider.value() == attendu


def test_la_coupure_du_plein_ecran_se_voit_a_la_console(qtbot):
    m = _mixer(qtbot)
    m.set_main_muted(True)
    assert m._strips[m._MAIN]._muet is True


def test_la_coupure_ne_repart_pas_non_plus(qtbot):
    m = _mixer(qtbot)
    recus: list[bool] = []
    m.main_mute_changed.connect(recus.append)
    m.set_main_muted(True)
    assert recus == []


def test_un_reglage_arrive_avant_la_tranche_est_conserve(qtbot):
    """Les données peuvent précéder la construction de la console."""
    m = panel._MixerTab()
    qtbot.addWidget(m)
    m.set_main_volume(55)          # aucune tranche encore
    m.set_main_stream("zerator")
    m.set_pinned([])
    assert m._strips[m._MAIN]._slider.value() == 55


# ── Onglet Goals : la distance, pas seulement la cible ──────────────────────
#
# L'ancienne liste n'affichait que le montant visé. « 559 600 € » ne dit pas si
# l'objectif tombe dans l'heure ou s'il restera lettre morte.

@pytest.mark.parametrize("cagnotte,cible,attendu", [
    (0.0, 1000.0, 0.0),
    (500.0, 1000.0, 0.5),
    (1000.0, 1000.0, 1.0),
    (5000.0, 1000.0, 1.0),        # borné : on ne dépasse pas 100 %
    (-10.0, 1000.0, 0.0),
    (500.0, 0.0, 0.0),            # cible absurde : pas de division
    (500.0, -3.0, 0.0),
])
def test_la_part_d_un_objectif_est_bornee(cagnotte, cible, attendu):
    assert panel._part_objectif(cagnotte, cible) == pytest.approx(attendu)


@pytest.mark.parametrize("url,attendu", [
    ("https://exemple.test/x", True),
    ("http://exemple.test/x", False),
    ("file:///C:/Windows/System32", False),
    ("javascript:alert(1)", False),
    ("", False),
    (None, False),
])
def test_un_lien_d_objectif_doit_etre_en_https(qtbot, url, attendu):
    """Ces liens viennent d'une API communautaire et pointent où leurs auteurs
    veulent : un `file://` ou un `javascript:` n'a aucune raison d'arriver
    jusqu'au système."""
    bouton = panel._bouton_lien(url)
    assert (bouton is not None) is attendu
    if bouton is not None:
        qtbot.addWidget(bouton)


class _FauxGoal:
    def __init__(self, nom, montant, accompli=False, categorie="", liens=()):
        self.id = nom
        self.name = nom
        self.amount = montant
        self.accomplished = accompli
        self.category = categorie
        self.links = list(liens)


@pytest.fixture
def goals_tab(qtbot, monkeypatch):
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)   # aucun réseau
    o.set_streamers([
        _FauxStreamer("a", donation=500.0),
        _FauxStreamer("b", donation=900.0),
    ])
    o.seed_cache({
        "a": [_FauxGoal("loin", 5000.0), _FauxGoal("proche", 520.0),
              _FauxGoal("fait", 100.0, accompli=True)],
        "b": [_FauxGoal("imminent", 950.0)],
    })
    return o


def _lignes(onglet):
    return onglet.findChildren(panel._LigneObjectif)


def test_les_objectifs_les_plus_proches_viennent_en_premier(goals_tab):
    """C'est l'ordre dans lequel ils tomberont."""
    goals_tab._combo.setCurrentIndex(goals_tab._combo.findText("a"))
    noms = [lg._goal.name for lg in _lignes(goals_tab)]
    assert noms[:2] == ["proche", "loin"]


def test_les_objectifs_accomplis_sont_a_part(goals_tab):
    goals_tab._combo.setCurrentIndex(goals_tab._combo.findText("a"))
    noms = [lg._goal.name for lg in _lignes(goals_tab)]
    assert noms[-1] == "fait", "les accomplis ferment la marche"


def test_l_entete_compte_les_objectifs_atteints(goals_tab):
    goals_tab._combo.setCurrentIndex(goals_tab._combo.findText("a"))
    assert "1" in goals_tab._entete._sous_titre.text()
    assert "3" in goals_tab._entete._sous_titre.text()


def test_l_entete_suit_le_prefetch(qtbot, monkeypatch):
    """Il était calculé avant l'arrivée du cache et annonçait « 0 sur 0 »."""
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)
    o.set_streamers([_FauxStreamer("a", donation=500.0)])
    o.seed_cache({"a": [_FauxGoal("x", 1000.0), _FauxGoal("y", 10.0, True)]})
    assert "2" in o._entete._sous_titre.text()


# ── vue « les plus proches » ────────────────────────────────────────────────

def test_la_vue_tous_rassemble_les_chaines(goals_tab):
    goals_tab._changer_vue("tous")
    noms = [lg._goal.name for lg in _lignes(goals_tab)]
    assert set(noms) == {"imminent", "proche", "loin"}


def test_la_vue_tous_classe_par_proximite(goals_tab):
    goals_tab._changer_vue("tous")
    noms = [lg._goal.name for lg in _lignes(goals_tab)]
    assert noms == ["proche", "imminent", "loin"], \
        "96 %, puis 94 %, puis 10 %"


def test_la_vue_tous_ignore_les_objectifs_atteints(goals_tab):
    goals_tab._changer_vue("tous")
    assert "fait" not in [lg._goal.name for lg in _lignes(goals_tab)]


def test_la_vue_tous_masque_la_fiche_du_streamer(goals_tab):
    """Elle ne parle que d'une chaîne : elle n'a pas de sens ici."""
    goals_tab._changer_vue("tous")
    assert not goals_tab._entete.isVisible()
    goals_tab._changer_vue("streamer")
    assert goals_tab._combo.isEnabled()


def test_la_vue_tous_ignore_une_chaine_inconnue(qtbot, monkeypatch):
    """Le cache peut contenir un login absent de la liste courante."""
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)
    o.set_streamers([_FauxStreamer("a", donation=10.0)])
    o.seed_cache({"fantome": [_FauxGoal("x", 100.0)]})
    o._changer_vue("tous")
    assert _lignes(o) == []


def test_la_vue_tous_est_plafonnee(qtbot, monkeypatch):
    """Au-delà, on ne lit plus — et on le dit plutôt que de tronquer en
    silence."""
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)
    combien = panel._GoalsTab._MAX_TOUS + 10
    o.set_streamers([_FauxStreamer("a", donation=10.0)])
    o.seed_cache({"a": [_FauxGoal(f"g{i}", 100.0 + i) for i in range(combien)]})
    o._changer_vue("tous")
    assert len(_lignes(o)) == panel._GoalsTab._MAX_TOUS
    textes = [w.text() for w in o.findChildren(panel.QLabel)]
    assert any("autres non affich" in t for t in textes)


def test_une_vue_tous_sans_rien_le_dit(qtbot, monkeypatch):
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)
    o.set_streamers([_FauxStreamer("a")])
    o._changer_vue("tous")
    assert _lignes(o) == []


# ── _clear_layout : détacher n'est pas masquer ──────────────────────────────
#
# Un widget détaché de son parent devient une fenêtre de PREMIER NIVEAU, et un
# widget de premier niveau visible est une fenêtre à l'écran. Reconstruire une
# liste de soixante lignes en faisait donc surgir soixante — mesuré à +124
# fenêtres par rafraîchissement sur l'onglet Goals, toutes les trois secondes,
# jusqu'à faire ramer la machine.

def test_vider_un_layout_ne_laisse_aucun_widget_visible(qtbot):
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

    hote = QWidget()
    qtbot.addWidget(hote)
    lay = QVBoxLayout(hote)
    enfants = [QLabel(f"ligne {i}") for i in range(5)]
    for e in enfants:
        lay.addWidget(e)
    hote.show()

    panel._clear_layout(lay)
    assert lay.count() == 0
    assert [e for e in enfants if e.isVisible()] == [], \
        "un widget détaché et visible est une fenêtre à l'écran"


def test_vider_un_layout_detache_bien_les_widgets(qtbot):
    """setParent(None) reste indispensable : deleteLater ne fait que
    PROGRAMMER la destruction, et le widget continuait de se peindre."""
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

    hote = QWidget()
    qtbot.addWidget(hote)
    lay = QVBoxLayout(hote)
    enfant = QLabel("x")
    lay.addWidget(enfant)
    panel._clear_layout(lay)
    assert enfant.parent() is None


def test_vider_un_layout_descend_dans_les_sous_layouts(qtbot):
    """Un layout imbriqué n'est pas un widget : ses enfants survivaient."""
    from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

    hote = QWidget()
    qtbot.addWidget(hote)
    lay = QVBoxLayout(hote)
    interne = QHBoxLayout()
    profond = QLabel("caché")
    interne.addWidget(profond)
    lay.addLayout(interne)
    hote.show()

    panel._clear_layout(lay)
    assert not profond.isVisible()
    assert lay.count() == 0


def test_vider_un_layout_vide_ne_leve_pas(qtbot):
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    hote = QWidget()
    qtbot.addWidget(hote)
    panel._clear_layout(QVBoxLayout(hote))


def test_les_goals_ne_reconstruisent_pas_pour_rien(goals_tab):
    """Le mock réémet ses données toutes les trois secondes.

    Reconstruire soixante lignes identiques ne change rien à l'écran et fait
    ramer la machine : mesuré à 1 340 widgets de premier niveau accumulés en
    dix rafraîchissements, contre 8 une fois l'empreinte en place.
    """
    goals_tab._changer_vue("tous")
    avant = _lignes(goals_tab)
    goals_tab.seed_cache({
        "a": [_FauxGoal("loin", 5000.0), _FauxGoal("proche", 520.0),
              _FauxGoal("fait", 100.0, accompli=True)],
        "b": [_FauxGoal("imminent", 950.0)],
    })
    apres = _lignes(goals_tab)
    assert [id(x) for x in avant] == [id(x) for x in apres], \
        "les mêmes données doivent laisser les mêmes widgets en place"


def test_une_donnee_qui_change_reconstruit_bien(goals_tab):
    goals_tab._changer_vue("tous")
    avant = [lg._goal.name for lg in _lignes(goals_tab)]
    goals_tab.seed_cache({"b": [_FauxGoal("nouveau", 910.0)]})
    apres = [lg._goal.name for lg in _lignes(goals_tab)]
    assert avant != apres


def test_la_barre_de_l_entete_est_reutilisee(goals_tab):
    """La remplacer la détachait de son parent — une fenêtre de plus."""
    goals_tab._combo.setCurrentIndex(goals_tab._combo.findText("a"))
    barre = goals_tab._entete._barre
    goals_tab.seed_cache({"a": [_FauxGoal("x", 100.0, accompli=True)]})
    assert goals_tab._entete._barre is barre


def test_la_cle_de_cache_n_utilise_pas_un_algorithme_faible():
    """SHA-256, comme partout ailleurs dans le projet.

    La clé ne protège rien — un nom de fichier de cache dérivé d'une URL
    publique — mais un algorithme réputé faible dans le code invite à le
    recopier là où ça compterait.
    """
    import hashlib
    import inspect

    source = inspect.getsource(panel._avatar_cache_key)
    assert "sha1" not in source and "md5" not in source
    url = "https://cdn.test/a.png"
    attendu = "guest_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    assert panel._avatar_cache_key("", url) == attendu
