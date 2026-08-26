# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""HypeWatcher — détection de moments forts par burst chat IRC + proxy MPV.

Architecture :
    - Un QThread exécute la boucle d'évaluation toutes les 2 s.
    - Un threading.Thread séparé gère la connexion IRC Twitch (I/O bloquant).
    - Zéro dépendance : socket/ssl de la stdlib, aucun appel réseau tiers.
    - Trois signaux fusionnés — débit d'utilisateurs uniques dans le chat,
      niveau audio RMS, croissance des viewers — chacun mesuré en écart à la
      ligne de base de la chaîne, et non à un seuil absolu.
    - Les signaux indisponibles sont retirés de la fusion, jamais remplacés.

Signal émis :
    alert_triggered(cell_idx: int, packed: str, score: float)
    packed = "<couleur_hex>|<label>|<extrait_chat>" — parsé dans
    GridWindow._on_hype_alert().
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# Libellé de l'alerte, repris à l'identique dans les trois cas de figure.
_LIBELLE_MOMENT_FORT = "Moment fort 🔥"


from core.api_client import _safe_login
from core.paths import CONFIG_PATH as _CONFIG_PATH

# ── Paramètres ────────────────────────────────────────────────────────────────

_CHAT_WINDOW_S: float   = 6.0   # fenêtre glissante de mesure du chat
_EVAL_INTERVAL_S: float = 2.0   # période d'évaluation du score
# (valeur par défaut plus bas, _COOLDOWN_S_DEFAULT)
# Buffer de messages servant à qualifier l'alerte.
#
# Quarante ne permettait pas de VOIR un moment fort : sur une chaîne à cent
# mille spectateurs, c'est une seconde de chat. Un vrai emballement, c'est
# quarante à cent personnes qui écrivent la même chose — impossible à compter
# dans un échantillon de quarante messages, dont la moitié parle d'autre
# chose. Le coût est modeste : vingt-cinq cellules × quatre cents messages.
_MAX_RECENT_MSGS: int   = 400
_MSG_TTL_S: float       = 120.0 # au-delà, un message ne décrit plus le moment courant

# Ligne de base par canal. Un moment fort est un ÉCART à la normale de CETTE
# chaîne : un seuil absolu ferait alerter en continu les grosses audiences et
# jamais les petites.
_BASELINE_HALFLIFE_S: float = 240.0  # demi-vie de la moyenne mobile
# 8 échantillons = 16 s : bien trop court. La variance était encore quasi nulle,
# donc le moindre écart valait des dizaines de sigma et tout déclenchait. Une
# chaîne doit être observée une minute et demie avant de pouvoir alerter.
_BASELINE_MIN_SAMPLES: int  = 45     # 45 x 2 s = 90 s d'observation
# Calibré par mesure : sur un chat bruité (±60 % autour de sa moyenne), 3.0
# déclenchait 18,7 % du temps sur du bruit pur, 8.0 tombe à 1,7 % en détectant
# toujours les bursts x2 et x3. Le seuil moyen correspond ainsi à 4 sigma.
_Z_SATURATION: float        = 8.0    # écarts-types → score 1.0

# Au démarrage d'une cellule, chat, audio et viewers montent tous de zéro à
# leur régime normal : c'est la plus grosse « anomalie » que la chaîne
# connaîtra jamais. On n'alerte donc pas pendant ce temps-là.
_CELL_WARMUP_S: float = 120.0

# Une salve de 2 s (raid, bot, spam d'emotes) ne doit pas suffire.
_DEBOUNCE_HITS: int = 3

# Plafond global. Trois alertes par minute font cent quatre-vingts par heure :
# tenable sur un chat calme, intenable un soir de ZEvent où les vingt-cinq
# chaînes s'emballent en même temps. Le plafond se raisonne à l'heure, échelle
# à laquelle une alerte reste un événement qu'on va effectivement regarder.
_ALERTS_PER_HOUR: int         = 8
_ALERT_BUDGET_WINDOW_S: float = 3600.0

# Une chaîne ne doit pas monopoliser le plafond : dix minutes entre deux de ses
# alertes. C'était quatre-vingt-dix secondes, de quoi en produire quarante par
# heure à elle seule.
_COOLDOWN_S_DEFAULT: float = 600.0

# Montée GÉNÉRALE. Pendant un temps fort du ZEvent — un palier de cagnotte, un
# lancement — tous les chats s'emballent ensemble. Chaque chaîne dépasse alors
# sa propre normale, et le score, qui mesure justement cet écart, les déclare
# toutes remarquables : c'est le mécanisme même qui produit le déluge.
# Au-delà de _SURGE_MIN_CELLS candidats simultanés, on n'alerte donc que si le
# meilleur se détache nettement de ses pairs de l'instant : si tout le monde
# monte, personne ne se distingue, et il n'y a rien à signaler.
_SURGE_MIN_CELLS: int = 4
_SURGE_MARGIN: float  = 1.25

# La règle ci-dessus ne voyait rien, car elle ne comparait que les candidats
# d'un MÊME tick de deux secondes. Un temps fort du ZEvent s'étale sur une
# minute : les chaînes y culminent à quelques secondes d'écart et ne sont
# jamais candidates ensemble. Le mouvement d'ensemble se juge donc sur les
# alertes RÉCENTES, pas sur l'instant.
_MONTEE_FENETRE_S: float = 180.0

# Le plafond horaire ne dit rien de la RÉPARTITION. Huit alertes tenaient en
# deux minutes — le fil devenait illisible — puis plus rien pendant une heure.
_ESPACEMENT_MIN_S: float = 60.0

# Poids relatifs des signaux. Un signal indisponible est retiré et les autres
# sont renormalisés, plutôt que remplacé par une valeur inventée.
_W_CHAT    = 0.45
_W_AUDIO   = 0.35
_W_VIEWERS = 0.20

_SCORE_HIGH: float = 0.70   # au-dessus : alerte immédiate, sans confirmation
_SCORE_MED:  float = 0.50   # au-dessus : alerte après confirmation (debounce)

# Comptes de bots courants sur Twitch : ils publient en continu et gonflent le
# débit sans rien dire de l'ambiance.
_BOT_NICKS = frozenset({
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot",
    "wizebot", "botisimo", "own3d", "sery_bot", "kofistreambot",
    "commanderroot", "anotherttvviewer", "soundalerts", "pokemoncommunitygame",
})

_C_GENERAL = "#ff6b00"
_C_DONO    = "#00ff87"
_C_FUNNY   = "#a855f7"

# Il n'y a plus de règle « factuelle » déclenchée par UN seul message.
#
# « Donation 💸 » et « World Record 🏆 » l'étaient : un unique message
# contenant « € », « don » ou « cagnotte » suffisait à étiqueter le moment et
# à se citer lui-même. Pendant un ZEvent, le chat parle d'argent en
# permanence — « euh, 85 % du revenu c'est 15 M€ ? » devenait ainsi une
# donation. Le raccourci court-circuitait en outre toute la mesure de
# convergence, seule chose qui distingue un mouvement de chat d'un message
# isolé.
#
# Une donation est un FAIT, et ZLink le tient de l'API, exact et chiffré
# (`DataManager.big_donation`). Le déduire du texte du chat, c'est remplacer
# une source sûre par une devinette. Les mots-clés servent toujours, mais
# seulement pour qualifier ce sur quoi le chat a CONVERGÉ — voir
# `_rule_for_token`.

def _kw_matcher(keywords: list[str]):
    """Construit le test d'appartenance d'une règle, sur des MOTS ENTIERS.

    Une simple recherche de sous-chaîne classait « donc », « donner »,
    « pardon » et « abandonne » en Donation — catégorie factuelle, donc
    prioritaire sur tout le reste. « mortel » devenait un moment tendu,
    « wrong » un record du monde, « suggestions » un bravo.

    Les symboles (€, emoji) n'ont pas de limite de mot au sens des regex :
    on les cherche tels quels.
    """
    words = [k for k in keywords if k.replace(" ", "").isalnum()]
    symbols = [k for k in keywords if k not in words]
    pattern = (
        re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")
        if words else None
    )

    def matches(text: str) -> bool:
        if pattern is not None and pattern.search(text):
            return True
        return any(sym in text for sym in symbols)

    return matches


_KEYWORD_RULES: list[tuple[list[str], str, str]] = [
    (
        ["dono", "donation", "don", "euros", "€", "cagnotte"],
        "Donation 💸", _C_DONO,
    ),
    (
        ["omegalul", "lul", "lmao", "mdr", "ptdr", "xd", "💀", "😂"],
        "Moment drôle 💀", _C_FUNNY,
    ),
    (
        ["wr", "world record", "record du monde", "recordé"],
        "World Record 🏆", _C_GENERAL,
    ),
    (
        ["pogchamp", "pog", "poggers", "hype", "lets go", "allons-y", "go go"],
        "Hype 🔥", _C_GENERAL,
    ),
    (
        ["gg", "gege", "incroyable", "magnifique", "excellent"],
        "Bravo 🎉", _C_GENERAL,
    ),
    (
        ["f dans le chat", "rip", "nooo", "mort"],
        "Moment tendu 😬", _C_GENERAL,
    ),
]

_IRC_HOST = "irc.chat.twitch.tv"
_IRC_PORT = 6697   # IRC over TLS — en clair, le chat lu est falsifiable en MITM


# ---------------------------------------------------------------------------
# _CellInfo  (état par cellule surveillée)
# ---------------------------------------------------------------------------

class _Ewma:
    """Moyenne et variance glissantes à décroissance exponentielle.

    Sert de ligne de base par canal : on compare une mesure à l'ordinaire de
    CETTE chaîne, pas à une constante globale.
    """

    __slots__ = ("mean", "var", "n")

    def __init__(self) -> None:
        self.mean = 0.0
        self.var  = 0.0
        self.n    = 0

    def update(self, x: float, dt: float) -> None:
        # alpha dérivé d'une demi-vie : indépendant de la période d'évaluation.
        alpha = 1.0 - 0.5 ** (max(dt, 1e-3) / _BASELINE_HALFLIFE_S)
        self.n += 1
        if self.n == 1:
            self.mean = x
            return
        delta = x - self.mean
        self.mean += alpha * delta
        self.var = (1.0 - alpha) * (self.var + alpha * delta * delta)

    def deviation(self, x: float) -> float | None:
        """Écart au-dessus de la normale, borné à [0, 1]. None si pas encore fiable."""
        if self.n < _BASELINE_MIN_SAMPLES:
            return None
        # Plancher sur l'écart-type : sur une chaîne très régulière il tend vers
        # zéro et le moindre frémissement saturerait le score.
        # Plancher relatif à 25 % de la moyenne : en dessous, une fluctuation
        # ordinaire se retrouvait à plusieurs sigma d'une base trop lisse.
        sigma = max(math.sqrt(max(self.var, 0.0)), 0.25 * abs(self.mean), 0.05)
        return max(0.0, min(1.0, (x - self.mean) / (_Z_SATURATION * sigma)))


# ---------------------------------------------------------------------------
# _CellInfo  (état par cellule surveillée)
# ---------------------------------------------------------------------------

class _CellInfo:
    __slots__ = (
        "cell_idx", "login", "mpv_widget",
        "chat_events", "recent_msgs",
        "last_alert", "streak",
        "base_chat", "base_audio",
        "viewers", "prev_viewers", "base_viewers",
        "_last_eval", "created_at",
    )

    def __init__(self, cell_idx: int, login: str, mpv_widget: object | None) -> None:
        self.cell_idx   = cell_idx
        self.login      = login
        self.mpv_widget = mpv_widget
        # (timestamp, pseudo) — le pseudo permet de compter des PERSONNES et non
        # des messages : 200 lignes d'un spammeur ne valent pas 200 spectateurs.
        self.chat_events: deque[tuple[float, str]] = deque()
        self.recent_msgs: deque[tuple[float, str, str]] = deque()
        self.last_alert: float = 0.0
        self.streak: int       = 0
        self.base_chat  = _Ewma()
        self.base_audio = _Ewma()
        self.base_viewers = _Ewma()
        self.viewers: int      = 0
        self.prev_viewers: int = 0
        self._last_eval: float = 0.0
        self.created_at: float = time.monotonic()

    def warmed_up(self) -> bool:
        return (time.monotonic() - self.created_at) >= _CELL_WARMUP_S

    # -- chat ------------------------------------------------------------------

    def record_msg(self, nick: str, text: str) -> None:
        now = time.monotonic()
        self.chat_events.append((now, nick))
        self.recent_msgs.append((now, nick, text))
        while len(self.recent_msgs) > _MAX_RECENT_MSGS:
            self.recent_msgs.popleft()

    def _prune(self, now: float) -> None:
        cutoff = now - _CHAT_WINDOW_S
        while self.chat_events and self.chat_events[0][0] < cutoff:
            self.chat_events.popleft()
        # Les messages servant à qualifier l'alerte doivent décrire le moment
        # présent : sans TTL, un message vieux de dix minutes pesait encore.
        msg_cutoff = now - _MSG_TTL_S
        while self.recent_msgs and self.recent_msgs[0][0] < msg_cutoff:
            self.recent_msgs.popleft()

    def chat_rate(self, now: float) -> float:
        """Utilisateurs DISTINCTS par seconde sur la fenêtre glissante."""
        self._prune(now)
        if not self.chat_events:
            return 0.0
        return len({nick for _ts, nick in self.chat_events}) / _CHAT_WINDOW_S

    def recent(self) -> list[tuple[str, str]]:
        """(pseudo, message) de la fenêtre courante."""
        return [(n, t) for _ts, n, t in self.recent_msgs]

    # -- audio -----------------------------------------------------------------

    def audio_level(self) -> float | None:
        """Niveau RMS en dBFS, ou None si la mesure n'est pas disponible.

        On renvoie None plutôt qu'une valeur de repli : injecter une constante
        biaisait le score total sans qu'on puisse distinguer un vrai signal
        d'une valeur inventée.
        """
        if self.mpv_widget is None:
            return None
        try:
            return self.mpv_widget.get_audio_rms_db()  # type: ignore[union-attr]
        except Exception:
            return None

    # -- viewers ---------------------------------------------------------------

    def viewers_growth(self) -> float | None:
        """Croissance relative des viewers depuis le dernier relevé."""
        if self.viewers <= 0 or self.prev_viewers <= 0:
            return None
        return (self.viewers - self.prev_viewers) / self.prev_viewers

    # -- cooldown  -------------------------------------------------------------

    def cooldown_ok(self, cooldown_s: float) -> bool:
        return (time.monotonic() - self.last_alert) >= cooldown_s

    def mark_alerted(self) -> None:
        self.last_alert = time.monotonic()
        self.streak = 0


# ---------------------------------------------------------------------------
# HypeWatcher
# ---------------------------------------------------------------------------

class HypeWatcher(QThread):
    """Thread de détection de moments forts.

    Usage ::

        watcher = HypeWatcher(config_dict)
        watcher.alert_triggered.connect(handler)
        watcher.start()
        # plus tard :
        watcher.update_cells([(idx, login, mpv_player), ...])
        # à l'arrêt :
        watcher.stop()
    """

    # cell_idx, "color|label", score
    alert_triggered = pyqtSignal(int, str, float)
    #: (chaîne source, chaîne cible, spectateurs amenés) — raid reçu.
    raid_detected   = pyqtSignal(str, str, int)

    def __init__(self, config: dict, parent: object | None = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._config           = dict(config)
        self._lock             = threading.Lock()
        self._cells: dict[str, _CellInfo] = {}
        self._stop_event       = threading.Event()
        self._channels_dirty   = threading.Event()
        #: (instant, score) des alertes émises. Le score est gardé parce que
        #: c'est à celui des alertes récentes qu'une nouvelle se compare.
        self._alert_times: deque[tuple[float, float]] = deque()
        self._cfg_cache: dict  = {}
        self._cfg_mtime: float = -1.0

    # ── API main thread ───────────────────────────────────────────────────────

    def update_cells(self, infos: list[tuple[int, str, object | None]]) -> None:
        """Met à jour les cellules surveillées (appelé depuis le main thread).

        infos = [(cell_idx, twitch_login, mpv_widget_or_None), ...]
        """
        with self._lock:
            new_logins = {lg for _, lg, _ in infos if lg}
            # Copie obligatoire : la boucle supprime des clés au passage.
            for login in list(self._cells):  # NOSONAR
                if login not in new_logins:
                    del self._cells[login]
            for cell_idx, login, mpv_widget in infos:
                if not login:
                    continue
                if login in self._cells:
                    self._cells[login].cell_idx   = cell_idx
                    self._cells[login].mpv_widget = mpv_widget
                else:
                    self._cells[login] = _CellInfo(cell_idx, login, mpv_widget)
        self._channels_dirty.set()
        logger.debug("HypeWatcher: %d canaux actifs", len(new_logins))

    def update_viewers(self, counts: dict[str, int]) -> None:
        """Renseigne le nombre de viewers par login (depuis le main thread).

        Troisième signal, décorrélé du chat et de l'audio, et gratuit en I/O :
        l'application connaît déjà ces valeurs. Il prend le relais quand l'IRC
        décroche.
        """
        with self._lock:
            for login, n in counts.items():
                info = self._cells.get(login)
                if info is None or n <= 0:
                    continue
                if info.viewers != n:
                    info.prev_viewers = info.viewers
                    info.viewers = n

    def stop(self) -> None:
        """Arrêt propre — attend la fin du thread (max 3 s)."""
        self._stop_event.set()
        self.wait(3000)

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("HypeWatcher: démarrage")
        irc_t = threading.Thread(
            target=self._irc_loop, daemon=True, name="hype-irc",
        )
        irc_t.start()
        while not self._stop_event.is_set():
            self._stop_event.wait(_EVAL_INTERVAL_S)
            if not self._stop_event.is_set():
                self._evaluate_all()
        self._stop_event.set()
        irc_t.join(timeout=2.0)
        logger.info("HypeWatcher: arrêté")

    # ── Évaluation ────────────────────────────────────────────────────────────

    def _evaluate_all(self) -> None:
        from core import alerts
        if not alerts.enabled("hype"):
            return
        hw_cfg = self._hype_config()
        score_high = float(hw_cfg.get("score_high", _SCORE_HIGH))
        score_med  = float(hw_cfg.get("score_medium", _SCORE_MED))
        cooldown_s = float(hw_cfg.get("cooldown_s", _COOLDOWN_S_DEFAULT))
        per_hour = max(1, int(hw_cfg.get("alerts_per_hour", _ALERTS_PER_HOUR)))

        with self._lock:
            snapshot = list(self._cells.values())

        now = time.monotonic()
        candidates: list[tuple[float, _CellInfo]] = []

        for info in snapshot:
            score = self._score_retenu(info, now, score_med, score_high, cooldown_s)
            if score is not None:
                candidates.append((score, info))

        if not candidates:
            return

        # Budget global : sous une montée générale, on ne garde que les plus
        # forts plutôt que d'inonder la grille d'alertes simultanées.
        while self._alert_times and now - self._alert_times[0][0] > _ALERT_BUDGET_WINDOW_S:
            self._alert_times.popleft()
        room = max(0, per_hour - len(self._alert_times))
        if room == 0:
            logger.debug("HypeWatcher: plafond horaire atteint, %d candidat(s) ignoré(s)",
                         len(candidates))
            return
        espacement = float(hw_cfg.get("espacement_min_s", _ESPACEMENT_MIN_S))
        if self._alert_times and now - self._alert_times[-1][0] < espacement:
            # Trop tôt après la précédente — sauf pour ce qui la DÉPASSE
            # nettement. Un espacement aveugle donne la place au premier
            # arrivé, et un moment ordinaire muselait celui, bien plus fort,
            # qui suivait trente secondes plus tard. Mais laisser passer tout
            # ce qui franchit le seuil haut revenait à supprimer
            # l'espacement : ces scores-là sont monnaie courante un soir
            # d'affluence, et deux alertes tombaient dans la même minute.
            precedent = self._alert_times[-1][1]
            barre = max(score_high, precedent * _SURGE_MARGIN)
            candidates = [c for c in candidates if c[0] >= barre]
            if not candidates:
                logger.debug(
                    "HypeWatcher: %.0f s depuis la dernière alerte, on attend",
                    now - self._alert_times[-1][0])
                return

        candidates.sort(key=lambda c: c[0], reverse=True)
        room = self._place_apres_montee(candidates, room, now)
        if room == 0:
            return
        for score, info in candidates[:room]:
            label, color, excerpt = _classify_local(info.recent(), info.viewers)
            if not excerpt:
                # Le chat s'est accéléré sans rien dire de particulier : il n'y
                # a rien à MONTRER, donc rien à annoncer. « Moment fort 🔥 »
                # tout seul n'apprend rien et ne se vérifie pas — c'est
                # précisément ce qui faisait passer le fil pour du bruit.
                #
                # Sans exception, même pour un score énorme : un emballement
                # que le chat ne commente pas est indistinguable d'un raid, du
                # spam d'un bot ou d'un pic de bruit. On préfère en manquer.
                logger.debug("HypeWatcher: %s écartée, aucun extrait (score %.2f)",
                             info.login, score)
                continue
            info.mark_alerted()
            self._alert_times.append((now, score))
            logger.info(
                "HypeWatcher: alerte %s score=%.2f label=%s extrait=%r",
                info.login, score, label, excerpt[:40],
            )
            # Le « | » sépare trois champs ; l'extrait vient du chat, on retire
            # donc ceux qu'il pourrait contenir pour ne pas casser le découpage.
            safe = excerpt.replace("|", "/")
            self.alert_triggered.emit(info.cell_idx, f"{color}|{label}|{safe}", score)

        if len(candidates) > room:
            logger.debug("HypeWatcher: %d alerte(s) écartée(s) faute de budget",
                         len(candidates) - room)

    def _score_retenu(self, info: "_CellInfo", now: float, score_med: float,
                      score_high: float, cooldown_s: float) -> "float | None":
        """Score de la cellule si elle mérite une alerte, None sinon.

        Un score très élevé se passe de confirmation ; un score moyen doit
        persister, sinon une salve de deux secondes suffirait à alerter.
        """
        dt = now - info._last_eval if info._last_eval else _EVAL_INTERVAL_S
        info._last_eval = now
        # On alimente quand même les lignes de base pendant la chauffe :
        # c'est justement là qu'elles se constituent.
        score = self._score(info, now, dt)
        if score is None or not info.warmed_up():
            return None
        if score < score_med:
            info.streak = 0
            return None
        info.streak += 1
        confirmed = score >= score_high or info.streak >= _DEBOUNCE_HITS
        if not confirmed or not info.cooldown_ok(cooldown_s):
            return None
        if not info.recent_msgs:
            return None
        return score

    def _place_apres_montee(self, candidates: list, room: int,
                            now: float = 0.0) -> int:
        """Corrige le budget quand toute la grille monte, même en décalé.

        Les candidats de l'instant NE SUFFISENT PAS à repérer un mouvement
        d'ensemble : pendant un palier de cagnotte, les chaînes culminent à
        quelques secondes d'écart et n'apparaissent jamais ensemble dans le
        même tick. On leur adjoint donc les alertes des dernières minutes.

        Si rien ne se détache de la médiane, il n'y a rien à signaler : on rend
        0. Si une chaîne se détache, elle seule est annoncée.
        """
        recentes = [score for instant, score in self._alert_times
                    if now - instant <= _MONTEE_FENETRE_S]
        if len(candidates) + len(recentes) < _SURGE_MIN_CELLS:
            return room
        scores = sorted([c[0] for c in candidates] + recentes)
        median = scores[len(scores) // 2]
        if median > 0 and candidates[0][0] < median * _SURGE_MARGIN:
            logger.info(
                "HypeWatcher: montée générale (%d en cours, %d récentes, "
                "médiane %.2f) — aucune ne se détache, pas d'alerte",
                len(candidates), len(recentes), median,
            )
            return 0
        return 1

    # ── Score ─────────────────────────────────────────────────────────────────

    def _score(self, info: _CellInfo, now: float, dt: float) -> float | None:
        """Fusionne les signaux disponibles en un score [0, 1], ou None.

        Chaque signal est mesuré en ÉCART à la ligne de base de la chaîne. Les
        signaux indisponibles sont retirés et les poids renormalisés sur ceux
        qui restent : aucune valeur de repli n'est inventée.
        """
        parts: list[tuple[float, float]] = []

        rate = info.chat_rate(now)
        dev = info.base_chat.deviation(rate)
        info.base_chat.update(rate, dt)
        if dev is not None:
            parts.append((_W_CHAT, dev))

        level = info.audio_level()
        if level is not None:
            dev = info.base_audio.deviation(level)
            info.base_audio.update(level, dt)
            if dev is not None:
                parts.append((_W_AUDIO, dev))

        growth = info.viewers_growth()
        if growth is not None:
            dev = info.base_viewers.deviation(growth)
            info.base_viewers.update(growth, dt)
            if dev is not None:
                parts.append((_W_VIEWERS, dev))

        if not parts:
            # Lignes de base encore en cours de constitution.
            return None
        total_w = sum(w for w, _ in parts)
        return sum(w * v for w, v in parts) / total_w

    def _hype_config(self) -> dict:
        """Config HypeWatcher, relue seulement quand le fichier change.

        L'ancienne version relisait et parsait config.json à chaque évaluation,
        soit toutes les deux secondes.
        """
        try:
            mtime = _CONFIG_PATH.stat().st_mtime
        except OSError:
            return self._cfg_cache
        if mtime != self._cfg_mtime:
            try:
                raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                self._cfg_cache = raw.get("hypewatcher", {}) or {}
            except Exception as exc:
                logger.debug("HypeWatcher: config illisible — %s", exc)
                self._cfg_cache = {}
            self._cfg_mtime = mtime
        return self._cfg_cache

    # ── IRC loop ──────────────────────────────────────────────────────────────

    def _irc_loop(self) -> None:
        """Thread bloquant — gère la connexion IRC avec reconnexion automatique."""
        while not self._stop_event.is_set():
            with self._lock:
                logins = list(self._cells.keys())
            self._channels_dirty.clear()

            if not logins:
                self._channels_dirty.wait(timeout=5.0)
                continue

            try:
                self._irc_session(logins)
            except Exception as exc:
                logger.debug("HypeWatcher IRC session perdue: %s", exc)
                if not self._stop_event.is_set():
                    time.sleep(5.0)

    def _irc_session(self, initial_logins: list[str]) -> None:
        """Une session IRC jusqu'au changement de canaux ou déconnexion."""
        # Pseudo anonyme justinfan attendu par l'IRC Twitch en lecture seule :
        # un simple identifiant de session, ni secret ni jeton.
        nick = f"justinfan{random.randint(10000, 99999)}"  # NOSONAR
        raw_sock = socket.create_connection((_IRC_HOST, _IRC_PORT), timeout=15)
        # create_default_context() vérifie déjà le certificat et refuse les
        # protocoles obsolètes ; le plancher est posé explicitement pour ne pas
        # dépendre du réglage OpenSSL de la machine, et pour que l'analyse
        # statique puisse le constater.
        contexte = ssl.create_default_context()
        contexte.minimum_version = ssl.TLSVersion.TLSv1_2
        sock = contexte.wrap_socket(raw_sock, server_hostname=_IRC_HOST)
        sock.settimeout(15.0)
        try:
            self._send(sock, "PASS SCHMOOPIIE")
            self._send(sock, f"NICK {nick}")
            # commands : donne accès aux USERNOTICE, qui transportent les raids.
            # tags : sans elles, un USERNOTICE ne dit pas de quel type il est.
            # ATTENTION — les tags préfixent AUSSI chaque PRIVMSG (« @a=b;... :nick!… »),
            # ce qui casse toute lecture qui suppose la ligne commençant par « : ».
            self._send(sock, "CAP REQ :twitch.tv/tags twitch.tv/commands")
            # Un login non validé contenant \r\n injecterait des commandes IRC.
            channels = [lg for lg in initial_logins if _safe_login(lg)]
            if len(channels) != len(initial_logins):
                logger.error(
                    "HypeWatcher IRC: %d login(s) écarté(s) (format inattendu)",
                    len(initial_logins) - len(channels),
                )
            if not channels:
                return
            self._send(sock, "JOIN #" + ",#".join(channels))
            logger.debug(
                "HypeWatcher IRC: connecté — %d canaux", len(initial_logins),
            )

            buf = ""
            while not self._stop_event.is_set():
                # Reconnexion si la liste de canaux a changé
                if self._channels_dirty.is_set():
                    with self._lock:
                        new_logins = set(self._cells)
                    if new_logins != set(initial_logins):
                        break  # quitter → _irc_loop() reconnectera

                try:
                    chunk = sock.recv(4096).decode("utf-8", errors="replace")
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    self._process_line(sock, line)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _send(sock: socket.socket, msg: str) -> None:
        try:
            sock.sendall((msg + "\r\n").encode("utf-8"))
        except Exception:
            pass

    def _process_line(self, sock: socket.socket, line: str) -> None:
        if line.startswith("PING"):
            tail = line[5:] if len(line) > 5 else ":tmi.twitch.tv"
            self._send(sock, f"PONG {tail}")
            return

        # L'aiguillage porte sur la COMMANDE IRC, pas sur la ligne brute :
        # `"USERNOTICE" in line` inspectait aussi le texte du message, et un
        # spectateur qui ecrivait « USERNOTICE » dans le chat voyait le sien
        # deroute vers _process_usernotice, qui le jetait. Il n'etait alors
        # compte ni dans le debit, ni dans le tampon de qualification.
        commande = re.search(r"(?:^|\s):[^\s]+\s+(\w+)\s", line)
        verbe = commande.group(1) if commande else ""
        if verbe == "USERNOTICE":
            self._process_usernotice(line)
            return

        if verbe != "PRIVMSG":
            return

        # Le pseudo est capturé : il permet de compter des personnes plutôt que
        # des lignes, et d'écarter les bots qui publient en continu.
        # Le préfixe de tags est optionnel dans l'expression : depuis qu'on
        # demande twitch.tv/tags, il est TOUJOURS présent, et un motif ancré sur
        # « : » ne reconnaîtrait plus une seule ligne de chat.
        m = re.match(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #(\w+) :(.+)$", line)
        if not m:
            return

        nick    = m.group(1).lower()
        channel = m.group(2).lower()
        text    = m.group(3)

        if nick in _BOT_NICKS:
            return

        with self._lock:
            info = self._cells.get(channel)
        if info is not None:
            info.record_msg(nick, text)

    @staticmethod
    def _parse_tags(line: str) -> dict[str, str]:
        """« @a=1;b=2 :reste » → {'a': '1', 'b': '2'}."""
        if not line.startswith("@"):
            return {}
        blob = line[1:].split(" ", 1)[0]
        tags: dict[str, str] = {}
        for part in blob.split(";"):
            cle, _, val = part.partition("=")
            if cle:
                tags[cle] = val
        return tags

    def _process_usernotice(self, line: str) -> None:
        """Détecte les raids reçus par une chaîne affichée.

        Un raid n'est annoncé que dans le chat de la chaîne QUI LE REÇOIT. On
        le voit donc pour les raids qui arrivent sur une cellule de la grille,
        pas pour ceux qui en partent : le chat de destination n'est pas suivi.
        """
        tags = self._parse_tags(line)
        if tags.get("msg-id") != "raid":
            return
        m = re.search(r"USERNOTICE #(\w+)", line)
        if not m:
            return
        cible = m.group(1).lower()
        with self._lock:
            connue = cible in self._cells
        if not connue:
            return
        source = (tags.get("msg-param-login")
                  or tags.get("login")
                  or tags.get("msg-param-displayName") or "").lower()
        try:
            viewers = int(tags.get("msg-param-viewerCount") or 0)
        except ValueError:
            viewers = 0
        if not source:
            return
        logger.info("Raid détecté : %s -> %s (%d viewers)", source, cible, viewers)
        self.raid_detected.emit(source, cible, viewers)


# ---------------------------------------------------------------------------
# Helpers module-level (utilisés aussi depuis GridWindow indirectement)
# ---------------------------------------------------------------------------

# Testeurs compilés une seule fois, dans l'ordre de _KEYWORD_RULES.


# Mots trop courants pour caractériser quoi que ce soit.
_STOPWORDS = frozenset("""
le la les un une des du de d au aux et ou ok oui non mais donc or ni car
je tu il elle on nous vous ils elles me te se ce cet cette ces mon ma mes
ton ta tes son sa ses qui que quoi dont ou a as ai est es sont suis sommes
etes etre avoir fait fais pas plus moins tres trop bien pour par sur sous
avec sans dans en y il-y-a cest c-est jai j-ai the you and for that this
""".split())

_MIN_TOKEN_LEN = 3
# « gg », « wp », « wr », « xd » font deux lettres et comptent parmi les tokens
# les plus significatifs d'un chat Twitch : on les laisse passer, sans ouvrir
# la porte à tout le bruit de deux caractères.
_SHORT_ALLOWED = frozenset(
    k.lower() for kws, _l, _c in _KEYWORD_RULES for k in kws
    if 2 <= len(k) < _MIN_TOKEN_LEN
)
# En dessous, ce n'est pas un mouvement de chat mais une coïncidence.
#: Plancher absolu de personnes reprenant le même terme. Trois n'était pas un
#: mouvement de chat : sur une chaîne assez suivie pour être dans la grille,
#: trois personnes qui écrivent le même mot arrivent en permanence.
_MIN_DOMINANT_USERS = 5

#: Plancher selon l'AUDIENCE, en (viewers, personnes exigées).
#:
#: Une part des locuteurs récents ne suffit pas seule : sur un million de
#: spectateurs, le chat défile si vite que l'échantillon observé reste petit,
#: et 30 % d'un petit échantillon peut valoir quatre personnes. Quatre kappa
#: sur un million de spectateurs, ce n'est pas un moment fort.
#:
#: Les valeurs viennent de l'observation d'un ZEvent : un vrai emballement,
#: c'est quarante à cent personnes qui écrivent la même chose.
_PLANCHERS_AUDIENCE: tuple[tuple[int, int], ...] = (
    (100_000, 40),
    (10_000, 20),
    (1_000, 10),
)

#: Et surtout, une PART des gens qui viennent de parler.
#:
#: C'est la définition même d'un moment fort : le chat CONVERGE. Quatre
#: personnes sur quarante qui reprennent l'emote de la chaîne, c'est la ligne
#: de base — cette emote est tapée toute la journée. Les mêmes quatre sur huit
#: locuteurs, c'est le chat entier qui dit la même chose.
_PART_DOMINANTE = 0.30

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _plancher_audience(viewers: int) -> int:
    """Nombre de personnes en deçà duquel rien n'est un mouvement, ici.

    Le chat d'une grosse chaîne défile trop vite pour qu'on en voie une part
    représentative : la seule mesure qui tienne est alors le nombre ABSOLU de
    gens qui disent la même chose.
    """
    for seuil, exige in _PLANCHERS_AUDIENCE:
        if viewers >= seuil:
            return exige
    return _MIN_DOMINANT_USERS


def _exigence(entries: list[tuple[str, str]], viewers: int = 0) -> int:
    """Combien de personnes doivent reprendre le même terme pour que ça compte.

    Deux mesures, et on retient la plus exigeante :

    - une PART des locuteurs récents, qui décrit la convergence — quatre
      personnes valent un mouvement sur un chat de huit, pas sur un de
      quarante ;
    - un PLANCHER selon l'audience, parce que sur un million de spectateurs
      le chat défile trop vite pour qu'on en voie une part représentative.
    """
    locuteurs = len({nick for nick, _texte in entries})
    return max(_plancher_audience(int(viewers or 0)),
               _MIN_DOMINANT_USERS,
               round(_PART_DOMINANTE * locuteurs))


#: Au-delà, ce n'est plus une réaction mais une phrase.
#:
#: Compter un mot pris N'IMPORTE OÙ dans un message laissait gagner les mots
#: de remplissage : « voir » présent dans douze phrases différentes n'apprend
#: rien, et se retrouvait pourtant cité comme la preuve d'un moment fort. Une
#: réaction de chat, c'est un message COURT — une emote, deux mots, un cri.
_MAX_MOTS_REACTION = 3

#: Longueur maximale d'une réaction faite de symboles seuls, une fois les
#: répétitions écrasées. Au-delà, ce n'est plus une emote mais du dessin ASCII.
_MAX_SYMBOLES_REACTION = 8

#: Écrase « aaa » en « a ». Sert aux réactions sans mot : « 💀💀💀 » et
#: « 💀 » sont la même chose et doivent compter ensemble.
_REPETITIONS = re.compile(r"(.)\1+", re.UNICODE)


def _reaction(texte: str) -> str:
    """Le message ramené à sa réaction, ou "" si c'en est une phrase.

    Les répétitions sont écrasées : « lul lul lul » et « lul » sont la même
    réaction, et doivent compter ensemble.
    """
    # Les mots DISTINCTS, avant tout filtrage. Deux écueils évités d'un coup :
    # « lul lul lul lul » reste une réaction — c'est même la forme typique —
    # tandis que « je pense que ce boss est dur » reste une phrase, alors
    # qu'il n'en survivrait que trois mots au retrait des mots vides.
    bruts = list(dict.fromkeys(_TOKEN_RE.findall(texte.lower())))
    if not bruts:
        # Aucun MOT : des symboles, des emoji. « € », « 💀💀💀 » sont pourtant
        # des réactions typiques, et le motif de découpage ne retient que les
        # caractères alphanumériques — ces messages étaient purement
        # invisibles. Les répétitions sont écrasées pour que « 💀 » et
        # « 💀💀💀 » comptent ensemble.
        nu = _REPETITIONS.sub(r"\1", " ".join(texte.split()))
        return nu if 0 < len(nu) <= _MAX_SYMBOLES_REACTION else ""
    if len(bruts) > _MAX_MOTS_REACTION:
        return ""
    mots = [t for t in bruts
            if t not in _STOPWORDS
            and (len(t) >= _MIN_TOKEN_LEN or t in _SHORT_ALLOWED)]
    return " ".join(mots) if mots else ""


def _dominant_token(entries: list[tuple[str, str]]) -> tuple[str, int]:
    """La réaction la plus reprise, comptée en UTILISATEURS DISTINCTS.

    C'est ça, un moment fort sur Twitch : trente personnes qui écrivent la même
    chose en même temps. Chercher un message individuel qui contient un mot-clé
    ne décrit pas le phénomène — au mieux il en attrape un exemplaire, au pire
    il pioche une phrase sans rapport.

    Compter les personnes et non les occurrences empêche un spammeur seul
    d'imposer son mot.
    """
    users: dict[str, set[str]] = {}
    for nick, text in entries:
        cle = _reaction(text)
        if cle:
            users.setdefault(cle, set()).add(nick)
    if not users:
        return "", 0
    tok, who = max(users.items(), key=lambda kv: (len(kv[1]), kv[0]))
    return tok, len(who)


def _rule_for_token(token: str) -> tuple[str, str] | None:
    """Règle dont le token EST un mot-clé (correspondance exacte)."""
    for keywords, label, color in _KEYWORD_RULES:
        if token in (k.lower() for k in keywords):
            return label, color
    return None


def _classify_local(entries: list[tuple[str, str]],
                    viewers: int = 0) -> tuple[str, str, str]:
    """Qualifie le moment à partir de ce que le chat répète collectivement.

    Renvoie (libellé, couleur, extrait). L'extrait décrit le mouvement de
    chat — « LUL ×34 » — et non un message pris isolément.
    """
    if not entries:
        return _LIBELLE_MOMENT_FORT, _C_GENERAL, ""

    token, n_users = _dominant_token(entries)
    if not token or n_users < _exigence(entries, viewers):
        return _LIBELLE_MOMENT_FORT, _C_GENERAL, ""

    excerpt = f"« {token} » ×{n_users}"
    rule = _rule_for_token(token)
    if rule is not None:
        return rule[0], rule[1], excerpt
    return _LIBELLE_MOMENT_FORT, _C_GENERAL, excerpt
