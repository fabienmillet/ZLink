# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""HypeWatcher : lecture du fil IRC, fenêtre de chat, fusion des signaux.

Complète `test_hype_watcher.py`, qui couvre la ligne de base et la
qualification du moment. Ici on éprouve tout ce qui entoure ces calculs : ce
qu'on lit sur le fil Twitch, ce qu'on garde de ce fil, et la décision d'alerter.

Aucun test n'ouvre de socket ni ne démarre de thread : les méthodes de parsing
sont appelées sur des lignes IRC brutes et les méthodes de score sur des
`_CellInfo` préremplis. L'horloge est figée, sans quoi une fenêtre glissante ou
un cooldown se testerait à la seconde près.
"""

from __future__ import annotations

import json
import os

import pytest

import core.hype_watcher as hw
from core.hype_watcher import (
    _ALERT_BUDGET_WINDOW_S,
    _BASELINE_MIN_SAMPLES,
    _C_DONO,
    _C_FUNNY,
    _CELL_WARMUP_S,
    _CHAT_WINDOW_S,
    _COOLDOWN_S_DEFAULT,
    _DEBOUNCE_HITS,
    _LIBELLE_MOMENT_FORT,
    _MAX_RECENT_MSGS,
    _MSG_TTL_S,
    _SURGE_MIN_CELLS,
    _W_AUDIO,
    _W_CHAT,
    _W_VIEWERS,
    _CellInfo,
    HypeWatcher,
    _rule_for_token,
)


# ── outillage ────────────────────────────────────────────────────────────────

@pytest.fixture
def horloge(monkeypatch):
    """Fige `time.monotonic` et rend le cadran, réglable par le test.

    Fenêtre de chat, TTL des messages, chauffe et cooldown se mesurent tous sur
    cette horloge : sans la figer, ces tests dépendraient de leur propre durée
    d'exécution.
    """
    etat = {"t": 10_000.0}
    monkeypatch.setattr("core.hype_watcher.time.monotonic", lambda: etat["t"])
    return etat


@pytest.fixture
def watcher(qapp):
    """Un HypeWatcher jamais démarré — on n'appelle que ses méthodes pures.

    `qapp` est requis parce que HypeWatcher est un QThread : ses pyqtSignal
    n'existent qu'avec une application Qt.
    """
    return HypeWatcher({})


class _SocketFactice:
    """Note ce qui part sur le fil, sans jamais ouvrir de connexion."""

    def __init__(self) -> None:
        self.envois: list[str] = []

    def sendall(self, donnees: bytes) -> None:
        self.envois.append(donnees.decode("utf-8"))


class _MpvFactice:
    """Lecteur mpv réduit à sa mesure de niveau audio."""

    def __init__(self, niveau: float | None = None, casse: bool = False) -> None:
        self._niveau = niveau
        self._casse = casse

    def get_audio_rms_db(self) -> float | None:
        if self._casse:
            raise RuntimeError("mpv ne répond plus")
        return self._niveau


def _base_prete(ewma, valeur: float) -> None:
    """Alimente une ligne de base jusqu'à ce qu'elle accepte de se prononcer."""
    for _ in range(_BASELINE_MIN_SAMPLES):
        ewma.update(valeur, 2.0)


def _remplit_chat(info: _CellInfo, personnes: int, texte: str = "pogchamp") -> None:
    """Fait écrire `personnes` pseudos distincts, une fois chacun."""
    for i in range(personnes):
        info.record_msg(f"u{i}", texte)


# ── PING ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ligne,attendu", [
    ("PING :tmi.twitch.tv", "PONG :tmi.twitch.tv\r\n"),
    ("PING :autre.serveur.twitch", "PONG :autre.serveur.twitch\r\n"),
    # Un PING nu n'a pas de charge à renvoyer : on répond au serveur par défaut
    # plutôt que de laisser passer le keepalive et de se faire déconnecter.
    ("PING", "PONG :tmi.twitch.tv\r\n"),
])
def test_le_ping_recoit_toujours_un_pong(watcher, ligne, attendu):
    sock = _SocketFactice()
    watcher._process_line(sock, ligne)
    assert sock.envois == [attendu]


# ── PRIVMSG ──────────────────────────────────────────────────────────────────

def test_un_privmsg_prefixe_de_tags_est_lu(watcher, horloge):
    """Depuis qu'on demande twitch.tv/tags, CHAQUE PRIVMSG arrive préfixé.

    Un motif ancré sur « : » ne reconnaissait alors plus une seule ligne de
    chat, et le débit mesuré restait nul en permanence.
    """
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(None, (
        "@badge-info=;badges=;color=#FF0000;display-name=Zerator;mod=0 "
        ":zerator!zerator@zerator.tmi.twitch.tv PRIVMSG #mistermv :pogchamp"
    ))
    assert watcher._cells["mistermv"].recent() == [("zerator", "pogchamp")]


def test_un_privmsg_sans_tags_reste_lu(watcher, horloge):
    """Le préfixe reste optionnel : une session sans CAP ne doit pas être muette."""
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(None, ":u1!u1@u1.tmi.twitch.tv PRIVMSG #mistermv :salut")
    assert watcher._cells["mistermv"].recent() == [("u1", "salut")]


def test_le_pseudo_et_le_canal_sont_ramenes_en_minuscules(watcher, horloge):
    """Twitch affiche « ZeratoR » mais indexe « zerator ».

    Sans normalisation, la cellule ne serait pas retrouvée et le même
    spectateur compterait deux fois selon la casse de son pseudo.
    """
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(None, ":ZeratoR!z@z.tmi.twitch.tv PRIVMSG #MisterMV :Salut Les Gens")
    # Le TEXTE, lui, est conservé tel quel : c'est lui qu'on affichera.
    assert watcher._cells["mistermv"].recent() == [("zerator", "Salut Les Gens")]


def test_le_texte_conserve_ses_deux_points(watcher, horloge):
    """Le « : » sépare l'entête du message, pas le message de lui-même."""
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(None, ":u1!u1@u1.tmi.twitch.tv PRIVMSG #mistermv :rdv 14:32 : ok")
    assert watcher._cells["mistermv"].recent() == [("u1", "rdv 14:32 : ok")]


@pytest.mark.parametrize("ligne", [
    # Les bots publient en continu : les compter gonflerait le débit sans rien
    # dire de l'ambiance.
    ":nightbot!nightbot@nightbot.tmi.twitch.tv PRIVMSG #mistermv :!uptime",
    ":streamelements!se@se.tmi.twitch.tv PRIVMSG #mistermv :merci !",
    # Canal non affiché : rien à mesurer.
    ":u1!u1@u1.tmi.twitch.tv PRIVMSG #autrechaine :salut",
    # Entêtes que le motif ne reconnaît pas.
    ":sans-pseudo PRIVMSG #mistermv :salut",
    ":u1!u1@u1.tmi.twitch.tv PRIVMSG #mistermv :",
    # Trafic de service.
    ":tmi.twitch.tv 001 justinfan42 :Welcome, GLHF!",
    ":justinfan42!justinfan42@justinfan42.tmi.twitch.tv JOIN #mistermv",
])
def test_lignes_qui_ne_nourrissent_pas_le_chat(watcher, horloge, ligne):
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(None, ligne)
    assert watcher._cells["mistermv"].recent() == []


def test_un_message_de_chat_contenant_USERNOTICE_devrait_etre_compte(watcher, horloge):
    watcher.update_cells([(0, "mistermv", None)])
    watcher._process_line(
        None, ":u1!u1@u1.tmi.twitch.tv PRIVMSG #mistermv :c'est quoi un USERNOTICE ?")
    assert watcher._cells["mistermv"].recent() == [
        ("u1", "c'est quoi un USERNOTICE ?")]


# ── tags IRCv3 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ligne,attendu", [
    ("@a=1;b=2 :reste de la ligne", {"a": "1", "b": "2"}),
    # Valeur vide : le tag existe, il ne dit rien.
    ("@a=;b=2 :reste", {"a": "", "b": "2"}),
    # Tag sans « = » : présence seule.
    ("@drapeau :reste", {"drapeau": ""}),
    # Le premier « = » sépare, les suivants appartiennent à la valeur.
    ("@msg-param-text=a=b :reste", {"msg-param-text": "a=b"}),
    # Pas de préfixe de tags du tout.
    (":u1!u1@u1.tmi.twitch.tv PRIVMSG #x :y", {}),
    ("", {}),
    ("PING :tmi.twitch.tv", {}),
])
def test_lecture_des_tags(ligne, attendu):
    assert HypeWatcher._parse_tags(ligne) == attendu


# ── USERNOTICE / raids ───────────────────────────────────────────────────────

def _ligne_raid(cible: str = "mistermv", **tags: str) -> str:
    """Construit un USERNOTICE de raid, tel que Twitch l'envoie."""
    defauts = {
        "msg-id": "raid",
        "login": "zerator",
        "msg-param-login": "zerator",
        "msg-param-displayName": "ZeratoR",
        "msg-param-viewerCount": "1500",
    }
    defauts.update(tags)
    blob = ";".join(f"{c}={v}" for c, v in defauts.items() if v is not None)
    return f"@{blob} :tmi.twitch.tv USERNOTICE #{cible}"


@pytest.fixture
def raids(watcher, horloge):
    """Un watcher qui surveille « mistermv », et la liste des raids annoncés."""
    watcher.update_cells([(0, "mistermv", None)])
    recus: list[tuple] = []
    watcher.raid_detected.connect(lambda *a: recus.append(a))
    return watcher, recus


def test_un_raid_recu_par_une_chaine_affichee_est_annonce(raids):
    watcher, recus = raids
    watcher._process_line(None, _ligne_raid())
    assert recus == [("zerator", "mistermv", 1500)]


def test_le_pseudo_source_est_ramene_en_minuscules(raids):
    """Le seul tag toujours présent est le nom d'affichage, en casse libre."""
    watcher, recus = raids
    watcher._process_line(None, _ligne_raid(
        **{"login": None, "msg-param-login": None}))
    assert recus == [("zerator", "mistermv", 1500)]


def test_un_compte_de_viewers_illisible_ne_fait_pas_rater_le_raid(raids):
    """Le raid est l'information ; le nombre n'en est qu'un ornement."""
    watcher, recus = raids
    watcher._process_line(None, _ligne_raid(**{"msg-param-viewerCount": "beaucoup"}))
    assert recus == [("zerator", "mistermv", 0)]


@pytest.mark.parametrize("ligne", [
    # Un abonnement n'est pas un raid.
    _ligne_raid(**{"msg-id": "sub"}),
    _ligne_raid(**{"msg-id": "resub"}),
    # Un raid vers une chaîne qu'on n'affiche pas ne concerne pas la grille.
    _ligne_raid(cible="chaine_non_affichee"),
    # Sans source identifiable, il n'y a rien à annoncer.
    _ligne_raid(**{"login": None, "msg-param-login": None,
                   "msg-param-displayName": None}),
    # USERNOTICE sans canal : ligne inexploitable.
    "@msg-id=raid;msg-param-login=zerator :tmi.twitch.tv USERNOTICE",
    # Sans tags, impossible de savoir de quel type d'USERNOTICE il s'agit.
    ":tmi.twitch.tv USERNOTICE #mistermv :ZeratoR raids",
])
def test_usernotice_sans_raid_a_annoncer(raids, ligne):
    watcher, recus = raids
    watcher._process_line(None, ligne)
    assert recus == []


# ── _CellInfo : chauffe ──────────────────────────────────────────────────────

@pytest.mark.parametrize("ecoule,attendu", [
    (0.0, False),
    (_CELL_WARMUP_S - 1.0, False),
    (_CELL_WARMUP_S, True),
    (_CELL_WARMUP_S + 3600.0, True),
])
def test_une_cellule_n_alerte_pas_pendant_sa_chauffe(horloge, ecoule, attendu):
    """Au démarrage, chat, audio et viewers montent tous de zéro à leur régime.

    C'est la plus grosse anomalie que la chaîne connaîtra jamais, et elle ne
    veut rien dire.
    """
    info = _CellInfo(0, "zerator", None)
    horloge["t"] += ecoule
    assert info.warmed_up() is attendu


# ── _CellInfo : fenêtre de chat ──────────────────────────────────────────────

def test_le_debit_compte_des_personnes_pas_des_messages(horloge):
    """Deux cents lignes d'un spammeur ne valent pas deux cents spectateurs."""
    info = _CellInfo(0, "zerator", None)
    for _ in range(200):
        info.record_msg("spammeur", "lul")
    info.record_msg("quelquun", "lul")
    assert info.chat_rate(horloge["t"]) == pytest.approx(2 / _CHAT_WINDOW_S)


def test_un_chat_muet_a_un_debit_nul(horloge):
    assert _CellInfo(0, "zerator", None).chat_rate(horloge["t"]) == 0.0


def test_la_fenetre_de_chat_oublie_ce_qui_est_sorti_du_cadre(horloge):
    """Le débit décrit l'instant : sans purge il ne redescendrait jamais."""
    info = _CellInfo(0, "zerator", None)
    _remplit_chat(info, 10)
    horloge["t"] += _CHAT_WINDOW_S + 1
    info.record_msg("tardif", "encore là")
    assert info.chat_rate(horloge["t"]) == pytest.approx(1 / _CHAT_WINDOW_S)


def test_le_tampon_de_messages_est_plafonne(horloge):
    """Il ne sert qu'à qualifier l'alerte : le garder entier serait une fuite."""
    info = _CellInfo(0, "zerator", None)
    for i in range(_MAX_RECENT_MSGS + 10):
        info.record_msg(f"u{i}", f"message {i}")
    recents = info.recent()
    assert len(recents) == _MAX_RECENT_MSGS
    assert recents[0] == ("u10", "message 10"), "ce sont les PLUS ANCIENS qui partent"


def test_un_vieux_message_ne_decrit_plus_le_moment_present(horloge):
    """Sans TTL, un message vieux de dix minutes pesait encore sur le libellé."""
    info = _CellInfo(0, "zerator", None)
    info.record_msg("u1", "message d'avant")
    horloge["t"] += _MSG_TTL_S + 1
    info.record_msg("u2", "message d'ici")
    info.chat_rate(horloge["t"])   # c'est chat_rate() qui déclenche la purge
    assert info.recent() == [("u2", "message d'ici")]


# ── _CellInfo : audio et viewers ─────────────────────────────────────────────

def test_sans_lecteur_mpv_il_n_y_a_pas_de_mesure_audio(horloge):
    assert _CellInfo(0, "zerator", None).audio_level() is None


def test_le_niveau_audio_vient_du_lecteur(horloge):
    assert _CellInfo(0, "zerator", _MpvFactice(-23.5)).audio_level() == -23.5


def test_un_lecteur_muet_rend_None_et_pas_une_valeur_inventee(horloge):
    """Injecter une constante de repli biaisait le score sans qu'on puisse
    distinguer un vrai signal d'une valeur fabriquée."""
    assert _CellInfo(0, "zerator", _MpvFactice(casse=True)).audio_level() is None


@pytest.mark.parametrize("viewers,precedent,attendu", [
    (110, 100, 0.10),
    (90, 100, -0.10),
    (100, 100, 0.0),
    # Sans deux relevés exploitables, la croissance n'existe pas : au premier
    # sondage, partir de zéro afficherait une croissance infinie.
    (100, 0, None),
    (0, 100, None),
    (0, 0, None),
])
def test_croissance_des_viewers(horloge, viewers, precedent, attendu):
    info = _CellInfo(0, "zerator", None)
    info.viewers, info.prev_viewers = viewers, precedent
    obtenu = info.viewers_growth()
    if attendu is None:
        assert obtenu is None
    else:
        assert obtenu == pytest.approx(attendu)


# ── _CellInfo : cooldown ─────────────────────────────────────────────────────

def test_le_cooldown_court_a_partir_de_la_derniere_alerte(horloge):
    """Une chaîne ne doit pas monopoliser le plafond horaire à elle seule."""
    info = _CellInfo(0, "zerator", None)
    info.streak = 5
    assert info.cooldown_ok(_COOLDOWN_S_DEFAULT) is True, "jamais alertée"

    info.mark_alerted()
    assert info.streak == 0, "la confirmation repart de zéro après une alerte"
    assert info.cooldown_ok(_COOLDOWN_S_DEFAULT) is False

    horloge["t"] += _COOLDOWN_S_DEFAULT - 1
    assert info.cooldown_ok(_COOLDOWN_S_DEFAULT) is False
    horloge["t"] += 1
    assert info.cooldown_ok(_COOLDOWN_S_DEFAULT) is True


# ── _score : fusion pondérée ─────────────────────────────────────────────────

def test_pas_de_score_tant_que_les_lignes_de_base_se_constituent(watcher, horloge):
    """Sans normale connue, un écart à la normale n'a pas de sens."""
    info = _CellInfo(0, "zerator", None)
    _remplit_chat(info, 20)
    assert watcher._score(info, horloge["t"], 2.0) is None


def test_avec_le_seul_chat_disponible_il_porte_tout_le_score(watcher, horloge):
    """Un signal seul n'est pas dilué par les poids des signaux absents."""
    info = _CellInfo(0, "zerator", None)      # pas de mpv, pas de viewers
    _base_prete(info.base_chat, 0.0)
    _remplit_chat(info, 6)                    # 6 personnes / 6 s = 1 msg/s
    assert watcher._score(info, horloge["t"], 2.0) == pytest.approx(1.0)


def test_un_signal_absent_est_retire_et_les_poids_renormalises(watcher, horloge):
    """Chat au plafond, audio à sa normale, viewers inconnus.

    Les viewers manquants ne valent pas zéro : leur poids est redistribué, sans
    quoi une cellule sans compteur de viewers serait durablement pénalisée.
    """
    info = _CellInfo(0, "zerator", _MpvFactice(-20.0))
    _base_prete(info.base_chat, 0.0)
    _base_prete(info.base_audio, -20.0)
    _remplit_chat(info, 6)
    attendu = _W_CHAT / (_W_CHAT + _W_AUDIO)
    assert watcher._score(info, horloge["t"], 2.0) == pytest.approx(attendu)


def test_les_trois_signaux_se_fondent_selon_leurs_poids(watcher, horloge):
    info = _CellInfo(0, "zerator", _MpvFactice(-20.0))
    info.prev_viewers, info.viewers = 100, 110      # +10 %
    _base_prete(info.base_chat, 0.0)
    _base_prete(info.base_audio, -20.0)
    _base_prete(info.base_viewers, 0.0)
    _remplit_chat(info, 6)
    # chat saturé (1.0), audio pile sur sa normale (0.0), viewers à 0.25 :
    # +10 % contre un plancher d'écart-type de 0.05 et une saturation à 8 σ.
    attendu = (_W_CHAT * 1.0 + _W_AUDIO * 0.0 + _W_VIEWERS * 0.25) / (
        _W_CHAT + _W_AUDIO + _W_VIEWERS)
    assert watcher._score(info, horloge["t"], 2.0) == pytest.approx(attendu)


def test_un_lecteur_en_panne_retire_l_audio_de_la_fusion(watcher, horloge):
    """Même traitement qu'un signal absent : on ne fabrique pas de valeur."""
    info = _CellInfo(0, "zerator", _MpvFactice(casse=True))
    _base_prete(info.base_chat, 0.0)
    _base_prete(info.base_audio, -20.0)
    _remplit_chat(info, 6)
    assert watcher._score(info, horloge["t"], 2.0) == pytest.approx(1.0)


def test_le_score_alimente_les_lignes_de_base_meme_sans_verdict(watcher, horloge):
    """C'est pendant la chauffe que les lignes de base se constituent :
    ne pas les alimenter tant qu'elles sont muettes les rendrait muettes à vie."""
    info = _CellInfo(0, "zerator", _MpvFactice(-20.0))
    for _ in range(3):
        watcher._score(info, horloge["t"], 2.0)
    assert info.base_chat.n == 3
    assert info.base_audio.n == 3


# ── _score_retenu : seuils, debounce, cooldown ───────────────────────────────

@pytest.fixture
def cellule_chaude(horloge):
    """Cellule sortie de chauffe, avec du chat à montrer."""
    info = _CellInfo(0, "zerator", None)
    horloge["t"] += _CELL_WARMUP_S
    _remplit_chat(info, 5, "lul")
    return info


def _score_fixe(watcher, valeur):
    """Neutralise la fusion des signaux pour n'éprouver que la décision."""
    watcher._score = lambda info, now, dt: valeur


def test_pas_d_alerte_pendant_la_chauffe_meme_a_score_maximal(watcher, horloge):
    info = _CellInfo(0, "zerator", None)
    _remplit_chat(info, 5)
    _score_fixe(watcher, 1.0)
    assert watcher._score_retenu(info, horloge["t"], 0.5, 0.7, 600.0) is None


def test_un_score_sous_le_seuil_moyen_annule_la_confirmation_en_cours(
        watcher, horloge, cellule_chaude):
    """Sinon deux salves séparées par une accalmie s'additionneraient."""
    cellule_chaude.streak = _DEBOUNCE_HITS - 1
    _score_fixe(watcher, 0.10)
    assert watcher._score_retenu(cellule_chaude, horloge["t"], 0.5, 0.7, 600.0) is None
    assert cellule_chaude.streak == 0


def test_un_score_moyen_doit_persister_pour_alerter(watcher, horloge, cellule_chaude):
    """Une salve de deux secondes — raid, bot, spam d'emotes — ne suffit pas."""
    _score_fixe(watcher, 0.60)
    verdicts = [
        watcher._score_retenu(cellule_chaude, horloge["t"], 0.5, 0.7, 600.0)
        for _ in range(_DEBOUNCE_HITS)
    ]
    assert verdicts[:-1] == [None] * (_DEBOUNCE_HITS - 1)
    assert verdicts[-1] == pytest.approx(0.60)


def test_un_score_tres_eleve_se_passe_de_confirmation(watcher, horloge, cellule_chaude):
    _score_fixe(watcher, 0.90)
    assert watcher._score_retenu(
        cellule_chaude, horloge["t"], 0.5, 0.7, 600.0) == pytest.approx(0.90)


def test_le_cooldown_prime_sur_le_score(watcher, horloge, cellule_chaude):
    _score_fixe(watcher, 0.95)
    cellule_chaude.mark_alerted()
    assert watcher._score_retenu(cellule_chaude, horloge["t"], 0.5, 0.7, 600.0) is None
    horloge["t"] += 600.0
    assert watcher._score_retenu(
        cellule_chaude, horloge["t"], 0.5, 0.7, 600.0) == pytest.approx(0.95)


def test_sans_message_recent_il_n_y_a_rien_a_annoncer(watcher, horloge):
    """L'audio seul peut faire monter le score, mais l'alerte doit citer le chat :
    sans message, on ne saurait ni la qualifier ni l'illustrer."""
    info = _CellInfo(0, "zerator", None)
    horloge["t"] += _CELL_WARMUP_S
    _score_fixe(watcher, 0.95)
    assert watcher._score_retenu(info, horloge["t"], 0.5, 0.7, 600.0) is None


# ── _place_apres_montee : la montée générale ─────────────────────────────────

def _candidats(*scores: float) -> list[tuple[float, _CellInfo]]:
    """Candidats déjà triés du meilleur au moins bon, comme _evaluate_all les passe."""
    couples = [(s, _CellInfo(i, f"c{i}", None)) for i, s in enumerate(scores)]
    return sorted(couples, key=lambda c: c[0], reverse=True)


def test_peu_de_candidats_laissent_le_budget_intact(watcher, horloge):
    """Trois chaînes qui s'emballent, ce n'est pas encore un mouvement d'ensemble."""
    candidats = _candidats(*[0.9] * (_SURGE_MIN_CELLS - 1))
    assert watcher._place_apres_montee(candidats, 5) == 5


def test_si_toute_la_grille_monte_personne_ne_se_detache(watcher, horloge):
    """Le score mesure l'écart de CHAQUE chaîne à SA normale.

    Pendant un palier de cagnotte, tous les chats s'emballent : chacun dépasse
    sa normale et le score les déclare tous remarquables. C'est ce mécanisme
    qui produit le déluge d'alertes qu'on veut éviter.
    """
    # Médiane 0.58, marge de 25 % → il faudrait 0.725 pour se distinguer.
    assert watcher._place_apres_montee(_candidats(0.60, 0.58, 0.55, 0.52), 5) == 0


def test_une_chaine_qui_se_detache_est_annoncee_seule(watcher, horloge):
    """Même avec du budget, la montée d'ensemble ne mérite qu'un seul signalement."""
    assert watcher._place_apres_montee(_candidats(0.95, 0.55, 0.52, 0.50), 5) == 1


def test_une_mediane_nulle_desactive_le_garde_fou(watcher, horloge):
    """Aucun multiple de zéro ne permet de « se détacher » : la comparaison
    relative n'a alors plus de sens et on retombe sur une alerte au plus."""
    assert watcher._place_apres_montee(_candidats(0.0, 0.0, 0.0, 0.0), 5) == 1


# ── _rule_for_token ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,libelle", [
    ("dono", "Donation 💸"),
    ("cagnotte", "Donation 💸"),
    ("lul", "Moment drôle 💀"),
    ("wr", "World Record 🏆"),
    ("pogchamp", "Hype 🔥"),
    ("gg", "Bravo 🎉"),
    ("rip", "Moment tendu 😬"),
])
def test_un_token_qui_est_un_mot_cle_donne_sa_regle(token, libelle):
    regle = _rule_for_token(token)
    assert regle is not None
    assert regle[0] == libelle


def test_la_regle_porte_aussi_la_couleur():
    """Le libellé et la couleur voyagent ensemble jusqu'à la bannière."""
    assert _rule_for_token("lul") == ("Moment drôle 💀", _C_FUNNY)


@pytest.mark.parametrize("token", [
    "zerator", "bonjour", "",
    # « world record » est un mot-clé, « world » seul n'en est pas un : la
    # correspondance est EXACTE, un token ne peut pas valoir pour la moitié
    # d'une expression.
    "world", "record", "monde",
    # Les tokens arrivent déjà en minuscules de _dominant_token.
    "LUL", "Pogchamp",
])
def test_aucune_regle_pour_ce_qui_n_est_pas_un_mot_cle(token):
    assert _rule_for_token(token) is None


# ── gestion des canaux ───────────────────────────────────────────────────────

def test_les_canaux_suivis_epousent_la_grille(watcher, horloge):
    watcher.update_cells([(0, "zerator", None), (1, "mistermv", None)])
    assert set(watcher._cells) == {"zerator", "mistermv"}
    watcher.update_cells([(3, "zerator", None)])
    assert set(watcher._cells) == {"zerator"}, "une chaîne retirée n'est plus mesurée"
    assert watcher._cells["zerator"].cell_idx == 3


def test_une_chaine_qui_reste_conserve_son_observation(watcher, horloge):
    """Recréer la cellule à chaque redisposition de la grille repartirait à
    zéro : quatre-vingt-dix secondes d'observation perdues, donc aucune alerte
    possible pour un moment fort qui survient juste après."""
    watcher.update_cells([(0, "zerator", None)])
    info = watcher._cells["zerator"]
    info.base_chat.update(1.0, 2.0)
    info.record_msg("u1", "lul")
    mpv = _MpvFactice(-30.0)

    watcher.update_cells([(2, "zerator", mpv)])

    assert watcher._cells["zerator"] is info
    assert (info.cell_idx, info.mpv_widget) == (2, mpv)
    assert info.base_chat.n == 1
    assert info.recent() == [("u1", "lul")]


def test_une_cellule_sans_login_est_ignoree(watcher, horloge):
    """Une case vide de la grille n'a pas de chat à rejoindre."""
    watcher.update_cells([(0, "", None), (1, None, None), (2, "zerator", None)])
    assert set(watcher._cells) == {"zerator"}


def test_le_changement_de_canaux_reveille_le_thread_irc(watcher, horloge):
    """C'est ce drapeau qui provoque la reconnexion sur la nouvelle liste."""
    assert not watcher._channels_dirty.is_set()
    watcher.update_cells([(0, "zerator", None)])
    assert watcher._channels_dirty.is_set()


def test_les_viewers_gardent_le_releve_precedent(watcher, horloge):
    """La croissance se calcule entre deux relevés : il faut donc les deux."""
    watcher.update_cells([(0, "zerator", None)])
    info = watcher._cells["zerator"]
    watcher.update_viewers({"zerator": 1000})
    watcher.update_viewers({"zerator": 1200})
    assert (info.prev_viewers, info.viewers) == (1000, 1200)
    assert info.viewers_growth() == pytest.approx(0.2)


def test_un_releve_identique_n_efface_pas_l_historique(watcher, horloge):
    """L'API est sondée plus souvent qu'elle ne bouge : sans ce garde-fou,
    deux sondages identiques ramèneraient la croissance à zéro."""
    watcher.update_cells([(0, "zerator", None)])
    info = watcher._cells["zerator"]
    for compte in (1000, 1200, 1200, 1200):
        watcher.update_viewers({"zerator": compte})
    assert (info.prev_viewers, info.viewers) == (1000, 1200)


@pytest.mark.parametrize("releve", [
    {"chaine_inconnue": 5000},   # pas dans la grille
    {"zerator": 0},              # l'API ne sait pas / hors ligne
    {"zerator": -5},
    {},
])
def test_releves_de_viewers_sans_effet(watcher, horloge, releve):
    watcher.update_cells([(0, "zerator", None)])
    info = watcher._cells["zerator"]
    watcher.update_viewers(releve)
    assert (info.prev_viewers, info.viewers) == (0, 0)


# ── _evaluate_all ────────────────────────────────────────────────────────────

@pytest.fixture
def grille(watcher, horloge, monkeypatch):
    """Watcher prêt à évaluer : famille d'alertes active, config figée.

    La config est figée sur l'instance : la vraie lecture de config.json est
    éprouvée à part, et la faire intervenir ici rendrait ces tests dépendants
    d'un fichier.
    """
    monkeypatch.setattr("core.alerts.enabled", lambda famille: True)
    watcher._hype_config = lambda: {}
    recues: list[tuple] = []
    watcher.alert_triggered.connect(lambda *a: recues.append(a))
    return watcher, recues


def test_une_famille_coupee_ne_calcule_rien(watcher, horloge, monkeypatch):
    """Le contrôle se fait À LA SOURCE : un détecteur éteint ne produit pas
    d'événement qu'on jetterait ensuite."""
    monkeypatch.setattr("core.alerts.enabled", lambda famille: False)
    watcher.update_cells([(0, "zerator", None)])
    watcher._score_retenu = lambda *a: pytest.fail("ne doit pas être évalué")
    recues: list[tuple] = []
    watcher.alert_triggered.connect(lambda *a: recues.append(a))
    watcher._evaluate_all()
    assert recues == []


def test_aucun_candidat_aucune_alerte(grille):
    watcher, recues = grille
    watcher.update_cells([(0, "zerator", None)])
    watcher._score_retenu = lambda *a: None
    watcher._evaluate_all()
    assert recues == []
    assert not watcher._alert_times


def test_l_alerte_porte_la_couleur_le_libelle_et_l_extrait(grille, horloge):
    watcher, recues = grille
    watcher.update_cells([(4, "zerator", None)])
    info = watcher._cells["zerator"]
    _remplit_chat(info, 5, "lul")
    watcher._score_retenu = lambda *a: 0.82

    watcher._evaluate_all()

    assert len(recues) == 1
    cell_idx, packed, score = recues[0]
    assert cell_idx == 4, "l'alerte doit viser la case de la grille, pas la chaîne"
    couleur, libelle, extrait = packed.split("|")
    assert (couleur, libelle, extrait) == (_C_FUNNY, "Moment drôle 💀", "« lul » ×5")
    assert score == pytest.approx(0.82)
    assert info.last_alert == horloge["t"], "le cooldown de la chaîne repart"
    assert list(watcher._alert_times) == [horloge["t"]]


def test_un_extrait_ne_peut_pas_casser_le_decoupage(grille):
    """Les trois champs sont séparés par « | » et l'extrait vient du chat :
    un spectateur pourrait sinon fabriquer un faux libellé ou une fausse couleur."""
    watcher, recues = grille
    watcher.update_cells([(0, "zerator", None)])
    watcher._cells["zerator"].record_msg("pirate", "don de 500 euros | merci")
    watcher._score_retenu = lambda *a: 0.9

    watcher._evaluate_all()

    _idx, packed, _score = recues[0]
    couleur, libelle, extrait = packed.split("|")
    assert couleur == _C_DONO
    assert libelle == "Donation 💸"
    assert extrait == "don de 500 euros / merci"


def test_sans_chat_exploitable_le_libelle_reste_generique(grille):
    watcher, recues = grille
    watcher.update_cells([(0, "zerator", None)])
    watcher._cells["zerator"].record_msg("u1", "hmm")
    watcher._score_retenu = lambda *a: 0.9
    watcher._evaluate_all()
    _idx, packed, _score = recues[0]
    assert packed == f"{hw._C_GENERAL}|{_LIBELLE_MOMENT_FORT}|"


def test_le_plafond_horaire_ferme_le_robinet(grille, horloge):
    """Trois alertes par minute sont tenables ; le plafond se raisonne à
    l'heure, échelle à laquelle une alerte reste un événement qu'on regarde."""
    watcher, recues = grille
    watcher._hype_config = lambda: {"alerts_per_hour": 2}
    watcher._alert_times.extend([horloge["t"] - 600.0, horloge["t"] - 10.0])
    watcher.update_cells([(0, "zerator", None)])
    watcher._cells["zerator"].record_msg("u1", "lul")
    watcher._score_retenu = lambda *a: 0.99

    watcher._evaluate_all()

    assert recues == []


def test_les_alertes_d_il_y_a_plus_d_une_heure_ne_comptent_plus(grille, horloge):
    """Le budget est une fenêtre glissante, pas un quota définitif."""
    watcher, recues = grille
    watcher._hype_config = lambda: {"alerts_per_hour": 2}
    watcher._alert_times.extend([
        horloge["t"] - _ALERT_BUDGET_WINDOW_S - 1.0,
        horloge["t"] - _ALERT_BUDGET_WINDOW_S - 0.5,
    ])
    watcher.update_cells([(0, "zerator", None)])
    watcher._cells["zerator"].record_msg("u1", "lul")
    watcher._score_retenu = lambda *a: 0.99

    watcher._evaluate_all()

    assert len(recues) == 1
    assert list(watcher._alert_times) == [horloge["t"]], "les périmées sont purgées"


def test_une_montee_generale_n_alerte_pas(grille, horloge):
    watcher, recues = grille
    scores = {"c0": 0.60, "c1": 0.58, "c2": 0.55, "c3": 0.52}
    watcher.update_cells([(i, f"c{i}", None) for i in range(4)])
    for info in watcher._cells.values():
        info.record_msg("u1", "lul")
    watcher._score_retenu = lambda info, *a: scores[info.login]

    watcher._evaluate_all()

    assert recues == []
    assert not watcher._alert_times, "une alerte écartée ne consomme pas de budget"


def test_sous_montee_generale_seule_la_plus_forte_est_annoncee(grille, horloge):
    watcher, recues = grille
    scores = {"c0": 0.55, "c1": 0.95, "c2": 0.52, "c3": 0.50}
    watcher.update_cells([(i, f"c{i}", None) for i in range(4)])
    for info in watcher._cells.values():
        info.record_msg("u1", "lul")
    watcher._score_retenu = lambda info, *a: scores[info.login]

    watcher._evaluate_all()

    assert len(recues) == 1
    assert recues[0][0] == 1, "c1, la seule qui se détache"


# ── _hype_config ─────────────────────────────────────────────────────────────

def test_la_config_n_est_relue_que_lorsque_le_fichier_change(watcher, tmp_path,
                                                             monkeypatch):
    """L'ancienne version parsait config.json toutes les deux secondes."""
    chemin = tmp_path / "config.json"
    chemin.write_text(json.dumps({"hypewatcher": {"cooldown_s": 5}}), encoding="utf-8")
    monkeypatch.setattr(hw, "_CONFIG_PATH", chemin)
    horodatage = chemin.stat()

    assert watcher._hype_config() == {"cooldown_s": 5}

    # Contenu changé mais mtime restauré : le cache doit tenir.
    chemin.write_text(json.dumps({"hypewatcher": {"cooldown_s": 99}}), encoding="utf-8")
    os.utime(chemin, (horodatage.st_atime, horodatage.st_mtime))
    assert watcher._hype_config() == {"cooldown_s": 5}

    # mtime différent : relecture.
    os.utime(chemin, (horodatage.st_atime, horodatage.st_mtime + 10))
    assert watcher._hype_config() == {"cooldown_s": 99}


@pytest.mark.parametrize("contenu", [
    "{ceci n'est pas du json",
    "",
    json.dumps({"hypewatcher": None}),
    json.dumps({"autre_chose": {"cooldown_s": 5}}),
])
def test_une_config_sans_reglage_exploitable_donne_un_dictionnaire_vide(
        watcher, tmp_path, monkeypatch, contenu):
    """Les défauts du module prennent alors le relais, sans planter la boucle."""
    chemin = tmp_path / "config.json"
    chemin.write_text(contenu, encoding="utf-8")
    monkeypatch.setattr(hw, "_CONFIG_PATH", chemin)
    assert watcher._hype_config() == {}


def test_une_config_absente_conserve_le_dernier_reglage_connu(watcher, tmp_path,
                                                              monkeypatch):
    """Un fichier momentanément illisible — réécriture en cours — ne doit pas
    faire retomber la détection sur des valeurs qu'on n'a pas choisies."""
    monkeypatch.setattr(hw, "_CONFIG_PATH", tmp_path / "jamais_ecrit.json")
    watcher._cfg_cache = {"cooldown_s": 42}
    assert watcher._hype_config() == {"cooldown_s": 42}
