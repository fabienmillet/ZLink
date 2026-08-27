# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Palette de commandes — la recherche au clavier, dans n'importe quelle fenêtre.

Elle vivait dans le panel, et Ctrl+K n'y répondait donc que là. C'est pourtant
en plein écran ou devant la grille qu'on veut changer de chaîne sans lâcher le
clavier — l'endroit où l'on ne dispose ni des onglets ni de la liste des
participants.

Le widget se superpose à son parent et ne connaît personne : il émet ce qu'on a
choisi, chaque fenêtre y branche ce qu'elle sait faire.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import favorites
from core.api_client import StreamerInfo

#: Mêmes familles que le reste de l'interface. Dupliquées plutôt qu'importées
#: du panel : c'est lui qui utilise la palette, l'inverse ferait un cycle.
_FONT_SEGOE = "Segoe UI Variable"
_FONT_MONO = "Cascadia Mono"


class CommandPalette(QWidget):
    """Recherche instantanée au clavier (Ctrl+K).

    Avec près de 300 participants, atteindre un streamer précis imposait
    d'ouvrir l'onglet Streamers et de faire défiler. Ici on tape trois lettres
    et on ouvre le flux, l'ajoute à la grille ou saute à un onglet.
    """

    stream_requested = pyqtSignal(str)   # twitch_login
    grid_requested   = pyqtSignal(str)   # twitch_login
    tab_requested    = pyqtSignal(str)   # nom d'onglet
    action_requested = pyqtSignal(str)   # identifiant d'action

    #: Actions atteignables au clavier. Les mêmes gestes que les raccourcis du
    #: plein écran, pour qui préfère les chercher par leur nom.
    _ACTIONS = [
        ("clip",   "Garder le moment en cours"),
        ("replay", "Revoir les dernières secondes"),
        ("recap",  "Récapitulatif de session"),
    ]

    _MAX_RESULTS = 9

    #: Hauteur d'une ligne de résultat, avatar compris.
    _LIGNE = 34
    _AVATAR = 24
    #: Largeur de la boîte. Assez pour un pseudo, un jeu et une audience.
    _LARGEUR = 620

    def __init__(self, parent: QWidget, tab_names: list[str],
                 actions: "list[str] | None" = None) -> None:
        super().__init__(parent)
        # Les actions que l'HÔTE sait exécuter, et elles seules.
        #
        # La liste `_ACTIONS` est commune aux deux palettes, mais le plein
        # écran ne sait pas montrer le récapitulatif de session — seul le panel
        # le peut. Sans ce filtre, la palette du plein écran proposait une
        # commande que `run_action` laissait tomber dans un `logger.debug` :
        # listée, cliquable, sans effet.
        self._actions = [(cle, libelle) for cle, libelle in self._ACTIONS
                         if actions is None or cle in actions]
        # La palette EST sa boîte : plus de voile plein écran, qui tournait au
        # noir opaque au-dessus de la vidéo.
        #
        # Et elle garde WA_StyledBackground plutôt que WA_TranslucentBackground.
        # La distinction n'est pas cosmétique : la vidéo est une fenêtre NATIVE,
        # posée par-dessus le rendu Qt. Qt la découpe sous les widgets frères
        # qui la recouvrent — mais un widget déclaré translucide n'a rien à
        # découper, alors la vidéo restait devant et la palette n'apparaissait
        # qu'au panel, seule fenêtre sans vidéo. Les autres surcouches de
        # l'application peignent toutes de cette façon, transparence comprise.
        self.setObjectName("commandPalette")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self._LARGEUR)
        self.setStyleSheet(
            "QWidget#commandPalette { background-color: rgba(18, 18, 18, 235);"
            " border: 1px solid #2f2f2f; border-radius: 8px; }"
        )
        self._streamers: list[StreamerInfo] = []
        self._tab_names = tab_names
        self._results: list[tuple[str, str, str]] = []   # (type, clé, libellé)
        self._build()
        self.hide()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        box = QFrame()
        box.setFixedWidth(self._LARGEUR)
        box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        # Le cadre n'est plus qu'un conteneur : le fond et la bordure sont
        # portés par la palette elle-même.
        box.setStyleSheet("QFrame { background: transparent; border: none; }")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(6)

        self._input = QLineEdit()
        self._input.setFont(QFont(_FONT_SEGOE, 14))
        self._input.setPlaceholderText("Rechercher un streamer, un onglet…")
        self._input.setStyleSheet(
            "QLineEdit { background: #0d0d0d; color: #ffffff; border: none; "
            "border-radius: 5px; padding: 8px 10px; }"
        )
        self._input.textChanged.connect(self._refilter)
        self._input.installEventFilter(self)
        bl.addWidget(self._input)

        self._list = QListWidget()
        self._list.setFont(QFont(_FONT_SEGOE, 12))
        self._list.setIconSize(QSize(self._AVATAR, self._AVATAR))
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Hauteur recalculée à chaque recherche : une boîte de neuf lignes pour
        # deux résultats laissait un grand vide sous eux.
        self._list.setFixedHeight(self._MAX_RESULTS * self._LIGNE)
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; color: #cccccc; }"
            "QListWidget::item { padding: 4px 8px; border-radius: 4px; }"
            "QListWidget::item:selected { background: #0d1f16; color: #00ff87; }"
        )
        self._list.itemActivated.connect(lambda _i: self._activate())
        bl.addWidget(self._list)

        hint = QLabel("↑↓ naviguer · Entrée ouvrir · Ctrl+Entrée ajouter à la grille · Échap fermer")
        hint.setFont(QFont(_FONT_SEGOE, 9))
        hint.setStyleSheet("color: #555555; background: transparent; border: none;")
        bl.addWidget(hint)

        root.addWidget(box)
        self._boite = box

    # -- données ---------------------------------------------------------------

    def set_streamers(self, streamers: list[StreamerInfo]) -> None:
        self._streamers = streamers

    # -- ouverture / fermeture -------------------------------------------------

    def open(self) -> None:
        self._input.clear()
        self._refilter("")
        self._se_placer()
        self.show()
        self.raise_()
        self._input.setFocus()

    def _se_placer(self) -> None:
        """Ajuste la hauteur au contenu, puis centre la boîte horizontalement.

        `setFixedHeight` sur la liste marque bien la mise en page à refaire,
        mais Qt ne la refait qu'au traitement d'un événement POSTÉ. Or
        `adjustSize` est appelé dans la foulée : il lisait encore l'ancienne
        taille, et la palette gardait celle qu'elle avait à son tout premier
        affichage.

        Les deux symptômes en découlaient : un grand vide au-dessus du champ
        quand les résultats se raréfiaient — la boîte flottait au milieu d'une
        palette trop haute — et des lignes peintes par-dessus l'aide quand ils
        se multipliaient, la palette étant restée trop basse.

        On force donc les deux mises en page à se refaire AVANT de mesurer.
        """
        interieur, exterieur = self._boite.layout(), self.layout()
        for mise_en_page in (interieur, exterieur):
            if mise_en_page is not None:
                mise_en_page.activate()
        self.adjustSize()
        # Une seconde fois APRÈS la mesure : `adjustSize` change la taille de
        # la palette, et le cadre ne s'y conforme qu'à la mise en page
        # suivante. Sans ce tour, il gardait la géométrie calculée pour
        # l'ancienne taille — c'est lui qu'on voyait déborder ou flotter.
        if exterieur is not None:
            exterieur.activate()
        parent = self.parentWidget()
        if parent is None:
            return
        x = max(0, (parent.width() - self.width()) // 2)
        y = max(0, min(120, (parent.height() - self.height()) // 2))
        self.move(x, y)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self._list.currentRow()
                row += 1 if key == Qt.Key.Key_Down else -1
                self._list.setCurrentRow(max(0, min(self._list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                to_grid = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self._activate(to_grid)
                return True
        return super().eventFilter(obj, event)

    # -- filtrage --------------------------------------------------------------

    def _refilter(self, text: str) -> None:
        q = text.strip().lower()
        self._results = []
        # Les onglets ne remontent que sur une recherche explicite : à vide, la
        # liste doit proposer ce qu'on cherche neuf fois sur dix, des streamers.
        if q:
            self._ajouter_commandes(q)
        self._ajouter_streamers(q)
        self._results = self._results[:self._MAX_RESULTS]
        self._afficher_resultats()

    def _ajouter_commandes(self, q: str) -> None:
        """Onglets et actions dont le libellé contient la recherche."""
        for name in self._tab_names:
            if q in name.lower():
                self._results.append(("tab", name, f"Onglet · {name}"))
        for cle, libelle in self._actions:
            if q in libelle.lower() or q in cle:
                self._results.append(("action", cle, f"Action · {libelle}"))

    def _ajouter_streamers(self, q: str) -> None:
        """Streamers correspondants : favoris, puis en direct, puis audience."""
        # Favoris d'abord, puis les live, puis les plus suivis.
        favs = favorites.get()
        ordered = sorted(
            self._streamers,
            key=lambda x: (x.twitch_login.lower() not in favs, not x.online, -x.viewers),
        )
        for s in ordered:
            if len(self._results) >= self._MAX_RESULTS:
                break
            hay = f"{s.twitch_login} {s.display}".lower()
            if q and q not in hay:
                continue
            state = f"{s.viewers:,}".replace(",", "\u202f") + " viewers" if s.online else "hors ligne"
            game = f" · {s.game}" if s.game else ""
            star = "★ " if s.twitch_login.lower() in favs else ""
            self._results.append(
                ("streamer", s.twitch_login, f"{star}{s.display}{game} — {state}")
            )

    def _afficher_resultats(self) -> None:
        """Repeuple la liste et présélectionne la première ligne."""
        self._list.clear()
        for kind, key, label in self._results:
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, self._LIGNE))
            if kind == "streamer":
                item.setIcon(QIcon(self._photo(key)))
            self._list.addItem(item)
        if self._results:
            self._list.setCurrentRow(0)
        # La boîte suit le nombre de résultats plutôt que l'inverse.
        lignes = max(1, len(self._results))
        self._list.setFixedHeight(lignes * self._LIGNE + 4)
        # Sans condition de visibilité : `isVisible` est faux tant que la
        # fenêtre hôte n'est pas affichée, et la palette gardait alors la
        # taille de son premier calcul. Redimensionner une palette cachée ne
        # coûte rien, et la faire dépendre de son état rendait le défaut
        # intermittent — donc introuvable.
        self._se_placer()

    def _photo(self, login: str) -> QPixmap:
        """Photo du streamer, ou un pixmap vide tant qu'elle n'est pas arrivée.

        Le rappel redessine la ligne dès que l'image est là : une palette
        s'ouvre et se referme en quelques secondes, attendre le prochain
        rafraîchissement des données serait trop tard.
        """
        s = next((x for x in self._streamers if x.twitch_login == login), None)
        if s is None:
            return QPixmap()
        from widgets.bigscreen_widget import _avatar_cache
        av = _avatar_cache.get(login, s.display, self._AVATAR,
                               self._redessiner, s.profile_url)
        return av if av is not None else QPixmap()

    def _redessiner(self) -> None:
        """Une photo vient d'arriver : on repose les icônes."""
        if not self.isVisible():
            return
        for rang, (kind, key, _label) in enumerate(self._results):
            item = self._list.item(rang)
            if item is not None and kind == "streamer":
                item.setIcon(QIcon(self._photo(key)))

    def _activate(self, to_grid: bool = False) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._results)):
            return
        kind, key, _label = self._results[row]
        self.hide()
        if kind == "tab":
            self.tab_requested.emit(key)
        elif kind == "action":
            self.action_requested.emit(key)
        elif to_grid:
            self.grid_requested.emit(key)
        else:
            self.stream_requested.emit(key)
