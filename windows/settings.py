# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Settings panel — vue plein-panel façon Discord.

Intégré comme overlay dans PanelWindow (même pattern que BigScreenWidget).
Signal settings_changed(dict) émis à chaque sauvegarde.
Signal close_requested()      émis quand l'utilisateur ferme le panel.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:
    import qtawesome as qta
    _QTA_OK = True
except Exception:  # noqa: BLE001
    # Pas seulement ImportError : qtawesome charge des polices et peut
    # échouer autrement. Un except trop étroit laissait _QTA_OK non
    # défini, et le démarrage plantait par NameError une fois sur six.
    qta = None  # type: ignore[assignment]
    _QTA_OK = False

from core.version import display_version
from core import config_store, domotique, streamdeck_install
from widgets.screen_picker import (
    PLAN_NOTES,
    ROLE_LABELS,
    ROLES,
    ScreenPicker,
)

logger = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────

_C_BG       = "#111111"
_C_SIDEBAR  = "#0d0d0d"
_C_SURFACE  = "#1a1a1a"
_C_BORDER   = "#2a2a2a"
_C_TEXT     = "#cccccc"
_FOND_TRANSPARENT = "background: transparent;"
_LICENCE_BSD = "BSD 3-Clause"
_TITRE_ECRANS = "Écrans"
#: Répété dans la barre de navigation et dans la page : une constante plutôt
#: que deux littéraux, pour que renommer l'onglet ne casse pas la sélection.
_TITRE_DECK = "Stream Deck"
#: « Domotique » décrivait le domaine, pas ce que fait l'onglet : personne qui
#: cherche Home Assistant ne l'y reconnaissait. Le jour où une autre box est
#: gérée, le nom générique redeviendra le bon.
_TITRE_DOMOTIQUE = "Home Assistant"
_C_MUTED    = "#555555"
_C_GREEN    = "#00ff87"
_C_DANGER   = "#ff4444"
_C_ACCENT   = "#5865f2"
_C_HOVER    = "#1e1e1e"

_FONT_UI    = "Segoe UI Variable"
_FONT_MONO  = "Cascadia Code"

_SS_BASE = f"""
    QWidget {{ font-family: '{_FONT_UI}'; }}
    QLabel  {{ color: {_C_TEXT}; background: transparent; border: none; }}
    QLineEdit {{
        background: {_C_SURFACE}; border: 1px solid {_C_BORDER};
        border-radius: 4px; color: {_C_TEXT}; padding: 6px 10px;
        font-family: '{_FONT_MONO}'; font-size: 12px;
    }}
    QLineEdit:focus {{ border-color: {_C_ACCENT}; }}
    /* Sans cette règle, un champ désactivé garde la couleur ci-dessus : une
       feuille de style qui fixe `color` l'emporte sur la palette « disabled »
       de Qt, et rien ne distingue plus l'inerte de l'actif. */
    QLineEdit:disabled {{
        background: #151515; color: {_C_MUTED}; border-color: #1f1f1f;
    }}
    QComboBox {{
        background: {_C_SURFACE}; border: 1px solid {_C_BORDER};
        border-radius: 4px; color: {_C_TEXT}; padding: 6px 10px; font-size: 12px;
        min-height: 28px;
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: #1a1a1a; color: {_C_TEXT}; border: 1px solid {_C_BORDER};
        selection-background-color: #2a2a2a;
    }}
    QSpinBox {{
        background: {_C_SURFACE}; border: 1px solid {_C_BORDER};
        border-radius: 4px; color: {_C_TEXT}; padding: 6px 10px; font-size: 12px;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background: #222222; border: none; width: 18px;
    }}
    QCheckBox {{ color: {_C_TEXT}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px;
        background: {_C_SURFACE}; border: 1px solid {_C_BORDER}; border-radius: 4px;
    }}
    QCheckBox::indicator:checked {{
        background: {_C_GREEN}; border-color: {_C_GREEN};
    }}
    QSlider::groove:horizontal {{
        background: {_C_BORDER}; height: 4px; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {_C_GREEN}; width: 14px; height: 14px;
        border-radius: 7px; margin: -5px 0;
    }}
    QSlider::sub-page:horizontal {{ background: {_C_GREEN}; border-radius: 2px; }}
    QScrollBar:vertical {{
        background: transparent; width: 6px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #333333; border-radius: 3px; min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# ── Helpers UI ────────────────────────────────────────────────────────────────

def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"border: none; background: {_C_BORDER}; max-height: 1px;")
    f.setFixedHeight(1)
    return f


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setFont(QFont(_FONT_UI, 9, QFont.Weight.Bold))
    lbl.setStyleSheet(f"color: {_C_MUTED}; letter-spacing: 2px;")
    return lbl


def _h2(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(_FONT_UI, 14, QFont.Weight.Bold))
    lbl.setStyleSheet("color: #ffffff;")
    return lbl


def _entier(brut: object, defaut: int) -> int:
    """Entier lu depuis config.json, ou `defaut` si la valeur est inutilisable.

    Le fichier s'edite a la main : une valeur en texte, decimale ou absurde
    ne doit pas empecher la fenetre de reglages de s'ouvrir.
    """
    try:
        return int(brut)
    except (TypeError, ValueError):
        return defaut


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(_FONT_UI, 10))
    lbl.setStyleSheet(f"color: {_C_MUTED};")
    lbl.setWordWrap(True)
    return lbl


# ── NavItem ───────────────────────────────────────────────────────────────────

class _NavItem(QWidget):
    clicked = pyqtSignal()

    def __init__(self, label: str, icon_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._active = False

        h = QHBoxLayout(self)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(10)

        self._icon_lbl: QLabel | None = None
        self._icon_name = icon_name
        if _QTA_OK and icon_name:
            icon_lbl = QLabel()
            icon_lbl.setFixedSize(18, 18)
            icon_lbl.setPixmap(qta.icon(icon_name, color=_C_MUTED).pixmap(QSize(18, 18)))
            self._icon_lbl = icon_lbl
            h.addWidget(icon_lbl)

        self._text_lbl = QLabel(label)
        self._text_lbl.setFont(QFont(_FONT_UI, 12))
        self._text_lbl.setStyleSheet(f"color: {_C_MUTED};")
        h.addWidget(self._text_lbl)
        h.addStretch()

        self._refresh_style()

    def set_active(self, on: bool) -> None:
        self._active = on
        self._refresh_style()

    def _refresh_style(self) -> None:
        color = "#ffffff" if self._active else _C_MUTED
        if self._active:
            self.setStyleSheet("background: #222222; border-radius: 6px; border: none;")
        else:
            self.setStyleSheet("background: transparent; border-radius: 6px; border: none;")
        self._text_lbl.setStyleSheet(f"color: {color};")
        if _QTA_OK and self._icon_lbl and self._icon_name:
            self._icon_lbl.setPixmap(
                qta.icon(self._icon_name, color=color).pixmap(QSize(18, 18))
            )

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── Pages ─────────────────────────────────────────────────────────────────────

def _scroll_wrap(inner: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet("background: transparent; border: none;")
    scroll.viewport().setStyleSheet(_FOND_TRANSPARENT)
    scroll.setWidget(inner)
    return scroll


class _PageBase(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Appliquer _SS_BASE directement pour éviter la rupture de cascade
        # introduite par le viewport de QScrollArea.
        self.setStyleSheet(_SS_BASE)
        self._vl = QVBoxLayout(self)
        self._vl.setContentsMargins(40, 32, 40, 32)
        self._vl.setSpacing(20)

    def _form(self) -> QFormLayout:
        f = QFormLayout()
        f.setSpacing(12)
        f.setVerticalSpacing(12)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return f


class _PageStreams(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._vl.addWidget(_h2("Streams"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Qualité vidéo"))

        self._adaptive = QCheckBox("Qualité adaptative selon le nombre de flux")
        self._adaptive.setChecked(bool(config.get("grid_adaptive", True)))
        self._vl.addWidget(self._adaptive)
        self._vl.addWidget(_hint(
            "1 flux → 1080p · 2 à 4 → 720p · 5 à 9 → 480p · 10 et plus → 360p. "
            "Les paliers sont modifiables dans config.json (grid_adaptive_tiers)."
        ))

        f = self._form()
        self._grid_quality = QComboBox()
        # Rendus réels de Twitch : « 360p », « 480p » et « 720p » n'existent pas
        # et retombaient silencieusement sur « worst », soit 284x160.
        self._grid_quality.addItems([
            # Chaque entrée liste les DEUX graphies de Twitch — « 360p » et
            # « 360p30 » selon la chaîne — et finit par un repli garanti.
            "160p,160p30,worst",
            "360p,360p30,160p,160p30,worst",
            "480p,480p30,360p,360p30,160p,160p30,worst",
            "720p60,720p,720p30,480p,480p30,360p,360p30,worst",
        ])
        from core.stream_manager import QUALITY_GRID, migrate_quality
        idx = self._grid_quality.findText(
            migrate_quality(config.get("grid_quality", QUALITY_GRID)))
        if idx >= 0:
            self._grid_quality.setCurrentIndex(idx)
        f.addRow("Qualité grille :", self._grid_quality)
        # La qualité fixe n'a de sens que si l'adaptatif est désactivé.
        self._grid_quality.setEnabled(not self._adaptive.isChecked())
        self._adaptive.toggled.connect(
            lambda checked: self._grid_quality.setEnabled(not checked)
        )

        self._fs_quality = QComboBox()
        self._fs_quality.addItems(["best", "1080p60,1080p,best", "720p60,720p,best"])
        idx2 = self._fs_quality.findText(config.get("fullscreen_quality", "best"))
        if idx2 >= 0:
            self._fs_quality.setCurrentIndex(idx2)
        f.addRow("Qualité fullscreen :", self._fs_quality)
        self._vl.addLayout(f)

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Grille"))

        f2 = self._form()
        self._max_streams = QSpinBox()
        self._max_streams.setRange(1, 25)
        # int() comme partout ailleurs sur cette page : un "20" en texte dans
        # config.json faisait lever TypeError a setValue() et rendait la
        # fenetre de reglages INOUVRABLE — donc le seul endroit d'ou on
        # aurait pu reparer. Le texte d'aide invite pourtant a editer le
        # fichier a la main.
        self._max_streams.setValue(_entier(config.get("max_active_streams", 20), 20))
        self._max_streams.setFixedWidth(100)
        f2.addRow("Max streams actifs :", self._max_streams)

        # La valeur enregistrée est portée par itemData : elle ne dépend donc
        # pas de l'ordre des entrées ni de leur libellé.
        self._grid_sort = QComboBox()
        for label, value in (
            ("Par viewers", "viewers"),
            ("Manuel (glisser-déposer)", "manual"),
            ("Favoris puis manuel", "favorites"),
        ):
            self._grid_sort.addItem(label, value)
        _cur = self._grid_sort.findData(config.get("grid_sort", "viewers"))
        self._grid_sort.setCurrentIndex(_cur if _cur >= 0 else 0)
        f2.addRow("Disposition :", self._grid_sort)
        self._vl.addLayout(f2)
        self._vl.addWidget(_hint(
            "Nombre maximum de streams lancés simultanément dans la grille.\n"
            "En disposition « par viewers », les cellules se réordonnent seules "
            "selon l'audience, favoris en tête. En « manuel », vous les glissez "
            "où vous voulez et l'ordre est conservé tel quel. En « favoris puis "
            "manuel », vos favoris restent en tête et le reste se glisse "
            "librement."
        ))
        self._vl.addStretch()

    def collect(self, config: dict) -> None:
        config["grid_adaptive"] = self._adaptive.isChecked()
        config["grid_quality"] = self._grid_quality.currentText()
        config["fullscreen_quality"] = self._fs_quality.currentText()
        config["max_active_streams"] = self._max_streams.value()
        config["grid_sort"] = self._grid_sort.currentData() or "viewers"


class _PageScreens(_PageBase):
    """Quel écran affiche quoi.

    C'était une liste de menus déroulants, un par moniteur : il fallait
    retenir lequel de « Écran 2 » et « Écran 3 » est celui de droite, alors
    que l'assistant, lui, les DESSINE. Les deux endroits proposent désormais
    le même schéma — et le même choix de rôles, qui manquait à l'assistant.
    """

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        screens = sorted(
            QApplication.instance().screens(),  # type: ignore[union-attr]
            key=lambda s: s.geometry().x(),
        )
        geos = [(sc.geometry().x(), sc.geometry().y(),
                 sc.geometry().width(), sc.geometry().height())
                for sc in screens]

        self._vl.addWidget(_h2(_TITRE_ECRANS))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Attribution des moniteurs"))
        self._vl.addWidget(_hint(
            "Cliquez sur un écran pour choisir son rôle. Le plein écran ne "
            "s'éteint pas : le donner à un autre moniteur les échange."
        ))

        self._picker = ScreenPicker(geos)
        self._picker.setMinimumHeight(230)
        self._picker.definir_assignments(config.get("screen_assignments") or {})
        self._picker.changed.connect(self._rafraichir)
        self._vl.addWidget(self._picker, stretch=1)

        self._recap = QLabel("")
        self._recap.setFont(QFont(_FONT_UI, 10))
        self._recap.setWordWrap(True)
        self._vl.addWidget(self._recap)

        self._note = _hint("")
        self._vl.addWidget(self._note)

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet(f"color: {_C_DANGER};")
        self._vl.addWidget(self._error_lbl)
        self._vl.addWidget(_hint(
            "↺ Les modifications d'écrans sont prises en compte au prochain démarrage."
        ))
        self._vl.addStretch()
        self._rafraichir()

    def _rafraichir(self) -> None:
        """Redit en toutes lettres ce que le schéma montre.

        Le rectangle porte « DIRECT » faute de place ; la ligne dessous donne
        le nom complet, dans un ordre fixe — sinon on ne remarque pas qu'un
        rôle n'est attribué à personne.
        """
        roles = self._picker.roles()
        morceaux = []
        for role in ROLES:
            libelle = ROLE_LABELS[role].split(" (")[0]
            if role in roles:
                morceaux.append(
                    f"<span style='color:{_C_GREEN}'>Écran "
                    f"{roles.index(role) + 1}</span> {libelle}")
            else:
                morceaux.append(
                    f"<span style='color:{_C_MUTED}'>— {libelle}</span>")
        self._recap.setText("   ·   ".join(morceaux))
        self._note.setText(PLAN_NOTES.get(len(self._picker.enabled_indexes()), ""))

    def collect(self, config: dict) -> bool:
        assignments = self._picker.assignments()
        if "fullscreen" not in assignments.values():
            # Le sélecteur l'interdit. Si ça arrive quand même, mieux vaut ne
            # rien écrire que d'enregistrer une disposition qui ne démarre pas.
            logger.warning("Attribution sans plein ecran — refus d'enregistrer")
            self._error_lbl.setText("⚠ Un écran doit afficher le plein écran.")
            return False
        self._error_lbl.setText("")
        config["screen_assignments"] = assignments
        return True


class _PageHype(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        hw = config.get("hypewatcher", {})

        self._vl.addWidget(_h2("Alertes"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Ce que ZLink vous signale"))

        # Une case par famille : une alerte qu'on ne peut pas éteindre finit
        # par être subie, et toutes n'intéressent pas tout le monde.
        from core.alerts import FAMILLES
        etats = (config.get("alerts") or {})
        self._alert_boxes: dict[str, QCheckBox] = {}
        for cle, libelle, defaut, aide in FAMILLES:
            cb = QCheckBox(libelle)
            cb.setFont(QFont(_FONT_UI, 11))
            cb.setChecked(bool(etats.get(cle, defaut)))
            cb.setToolTip(aide)
            self._alert_boxes[cle] = cb
            self._vl.addWidget(cb)
        self._vl.addWidget(_hint(
            "Chaque famille se coupe indépendamment. Une alerte désactivée "
            "n'est pas seulement masquée : elle n'est plus calculée du tout."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("HypeWatcher"))

        row = QHBoxLayout()
        self._enabled_cb = QCheckBox("Activer la détection de moments forts")
        self._enabled_cb.setFont(QFont(_FONT_UI, 12))
        self._enabled_cb.setChecked(hw.get("enabled", True))
        self._enabled_cb.stateChanged.connect(self._on_toggle)
        row.addWidget(self._enabled_cb)
        row.addStretch()
        self._vl.addLayout(row)
        self._vl.addWidget(_hint(
            "Surveille les bursts de chat IRC + l'activité des streams pour détecter "
            "les moments forts. Une alerte pulse la cellule et affiche un toast."
        ))

        self._vl.addWidget(_sep())

        self._advanced = QWidget()
        self._advanced.setStyleSheet(_FOND_TRANSPARENT)
        adv_vl = QVBoxLayout(self._advanced)
        adv_vl.setContentsMargins(0, 0, 0, 0)
        adv_vl.setSpacing(20)

        adv_vl.addWidget(_section_title("Seuils de détection"))
        f = self._form()

        score_high_row = QHBoxLayout()
        self._score_high = QSlider(Qt.Orientation.Horizontal)
        self._score_high.setRange(50, 95)
        self._score_high.setValue(int(hw.get("score_high", 0.70) * 100))
        self._score_high_lbl = QLabel(f"{self._score_high.value()}%")
        self._score_high_lbl.setFixedWidth(36)
        self._score_high_lbl.setFont(QFont(_FONT_MONO, 11, QFont.Weight.Bold))
        self._score_high_lbl.setStyleSheet(f"color: {_C_GREEN};")
        self._score_high.valueChanged.connect(lambda v: self._score_high_lbl.setText(f"{v}%"))
        score_high_row.addWidget(self._score_high, stretch=1)
        score_high_row.addWidget(self._score_high_lbl)
        f.addRow("Alerte immédiate ≥ :", score_high_row)

        score_med_row = QHBoxLayout()
        self._score_med = QSlider(Qt.Orientation.Horizontal)
        self._score_med.setRange(20, 70)
        self._score_med.setValue(int(hw.get("score_medium", 0.50) * 100))
        self._score_med_lbl = QLabel(f"{self._score_med.value()}%")
        self._score_med_lbl.setFixedWidth(36)
        self._score_med_lbl.setFont(QFont(_FONT_MONO, 11, QFont.Weight.Bold))
        self._score_med_lbl.setStyleSheet("color: #f59e0b;")
        self._score_med.valueChanged.connect(lambda v: self._score_med_lbl.setText(f"{v}%"))
        score_med_row.addWidget(self._score_med, stretch=1)
        score_med_row.addWidget(self._score_med_lbl)
        f.addRow("Alerte confirmée ≥ :", score_med_row)
        adv_vl.addLayout(f)
        adv_vl.addWidget(_hint(
            "Le score mesure l'écart à l'activité habituelle de la chaîne, pas "
            "un débit absolu : une petite chaîne et une grosse sont traitées de "
            "la même façon. Au-dessus du seuil « confirmée », l'alerte demande "
            "deux mesures consécutives ; au-dessus d'« immédiate », elle part "
            "sans attendre."
        ))

        adv_vl.addWidget(_sep())
        adv_vl.addWidget(_section_title("Cooldown"))
        f2 = self._form()
        self._cooldown = QSpinBox()
        self._cooldown.setRange(30, 3600)
        self._cooldown.setSuffix(" s")
        self._cooldown.setValue(int(hw.get("cooldown_s", 600)))
        self._cooldown.setFixedWidth(100)
        f2.addRow("Délai entre deux alertes d'une même chaîne :", self._cooldown)

        # Plafond global. Sans lui, un temps fort du ZEvent — où les vingt-cinq
        # chats s'emballent ensemble — produisait des alertes en continu.
        self._alerts_hour = QSpinBox()
        self._alerts_hour.setRange(1, 60)
        self._alerts_hour.setValue(int(hw.get("alerts_per_hour", 8)))
        self._alerts_hour.setFixedWidth(100)
        f2.addRow("Alertes maximum par heure :", self._alerts_hour)
        adv_vl.addLayout(f2)
        adv_vl.addWidget(_hint(
            "Le délai empêche une même chaîne de monopoliser les alertes ; le "
            "plafond horaire vaut pour l'ensemble de la grille. Pendant un "
            "temps fort du ZEvent, tous les chats s'emballent en même temps : "
            "seule la chaîne qui se détache nettement des autres déclenche "
            "alors une alerte."
        ))

        self._vl.addWidget(self._advanced)

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Dons"))
        f3 = self._form()
        dons = config.get("donations") or {}
        self._don_threshold = QSpinBox()
        self._don_threshold.setRange(50, 100_000)
        self._don_threshold.setSingleStep(100)
        self._don_threshold.setSuffix(" €")
        self._don_threshold.setValue(int(dons.get("threshold", 1000)))
        self._don_threshold.setFixedWidth(120)
        f3.addRow("Signaler à partir de :", self._don_threshold)

        self._don_per_hour = QSpinBox()
        self._don_per_hour.setRange(1, 120)
        self._don_per_hour.setValue(int(dons.get("per_hour", 12)))
        self._don_per_hour.setFixedWidth(120)
        f3.addRow("Alertes maximum par heure :", self._don_per_hour)
        self._vl.addLayout(f3)
        self._vl.addWidget(_hint(
            "Le seuil porte sur ce qu'une chaîne reçoit ENTRE DEUX RELEVÉS "
            "(toutes les 30 s), pas sur un don unique : l'API du ZEvent ne "
            "publie qu'un cumul par streamer. Une même chaîne ne peut pas "
            "déclencher deux alertes à moins de cinq minutes d'écart."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Son"))

        sons = config.get("sounds") or {}
        self._son_actif = QCheckBox(
            "Jouer un son bref sur un palier de cagnotte ou un objectif atteint")
        self._son_actif.setFont(QFont(_FONT_UI, 12))
        # Désactivé par défaut : un son qu'on n'a pas demandé est une intrusion,
        # et il se superpose au direct qu'on est en train d'écouter.
        self._son_actif.setChecked(bool(sons.get("enabled", False)))
        self._son_actif.stateChanged.connect(self._on_son_toggle)
        self._vl.addWidget(self._son_actif)

        son_row = QHBoxLayout()
        son_row.setSpacing(8)
        self._son_volume = QSlider(Qt.Orientation.Horizontal)
        self._son_volume.setRange(10, 100)
        self._son_volume.setValue(int(sons.get("volume", 60)))
        self._son_vol_lbl = QLabel(f"{self._son_volume.value()} %")
        self._son_vol_lbl.setFixedWidth(44)
        self._son_vol_lbl.setFont(QFont(_FONT_MONO, 11, QFont.Weight.Bold))
        self._son_vol_lbl.setStyleSheet(f"color: {_C_GREEN};")
        self._son_volume.valueChanged.connect(
            lambda v: self._son_vol_lbl.setText(f"{v} %"))
        son_row.addWidget(self._son_volume, stretch=1)
        son_row.addWidget(self._son_vol_lbl)

        self._son_test = QPushButton("Écouter")
        self._son_test.setFixedHeight(28)
        self._son_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self._son_test.setStyleSheet(
            f"QPushButton {{ background: {_C_SURFACE}; color: {_C_TEXT};"
            f" border: 1px solid {_C_BORDER}; border-radius: 6px; padding: 0 14px; }}"
            f"QPushButton:hover {{ background: #232323; }}")
        self._son_test.clicked.connect(self._on_son_test)
        son_row.addWidget(self._son_test)
        self._son_row_widget = QWidget()
        self._son_row_widget.setStyleSheet(_FOND_TRANSPARENT)
        self._son_row_widget.setLayout(son_row)
        self._vl.addWidget(self._son_row_widget)
        self._vl.addWidget(_hint(
            "Deux timbres distincts, qu'on reconnaît sans regarder l'écran : "
            "un arpège montant pour un palier de cagnotte, deux notes plus "
            "brèves pour un objectif atteint."
        ))
        self._son_row_widget.setEnabled(self._son_actif.isChecked())

        self._vl.addStretch()

        self._on_toggle(None)

    def _on_son_toggle(self) -> None:
        self._son_row_widget.setEnabled(self._son_actif.isChecked())

    def _on_son_test(self) -> None:
        """Fait entendre les deux sons au volume choisi, même si coupés."""
        from core import sounds
        sounds.configure({"sounds": {"enabled": True,
                                     "volume": self._son_volume.value()}})
        sounds.play("milestone", force=True)
        QTimer.singleShot(1100, lambda: sounds.play("goal", force=True))

    def _on_toggle(self, _state: object) -> None:
        self._advanced.setVisible(self._enabled_cb.isChecked())

    def collect(self, config: dict) -> None:
        config["alerts"] = {
            cle: cb.isChecked() for cle, cb in self._alert_boxes.items()}
        hw = config.setdefault("hypewatcher", {})
        hw["enabled"] = self._enabled_cb.isChecked()
        hw["score_high"] = self._score_high.value() / 100.0
        hw["score_medium"] = self._score_med.value() / 100.0
        hw["cooldown_s"] = self._cooldown.value()
        hw["alerts_per_hour"] = self._alerts_hour.value()
        dons = config.setdefault("donations", {})
        dons["threshold"] = self._don_threshold.value()
        dons["per_hour"] = self._don_per_hour.value()
        sons = config.setdefault("sounds", {})
        sons["enabled"] = self._son_actif.isChecked()
        sons["volume"] = self._son_volume.value()


class _PageClips(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        clips = config.get("clips", {})

        self._vl.addWidget(_h2("Clips"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Enregistrement"))

        f = self._form()

        # Durée
        dur_row = QHBoxLayout()
        self._duration = QSpinBox()
        self._duration.setRange(10, 300)
        # Meme defaut, meme consequence : voir _PageStreams.
        self._duration.setValue(_entier(clips.get("duration_secs", 60), 60))
        self._duration.setSuffix(" s")
        self._duration.setFixedWidth(100)
        dur_row.addWidget(self._duration)
        dur_row.addStretch()
        f.addRow("Durée du clip :", dur_row)
        self._vl_clip_rows = f

        # Dossier
        dir_row = QHBoxLayout()
        from core.paths import CLIPS_DEFAUT
        default_dir = str(CLIPS_DEFAUT)
        self._directory = QLineEdit(clips.get("directory", "") or default_dir)
        self._directory.setPlaceholderText(default_dir)
        dir_row.addWidget(self._directory, stretch=1)
        browse_btn = QPushButton("…")
        browse_btn.setFixedSize(30, 30)
        browse_btn.setStyleSheet(
            f"QPushButton {{ background: {_C_SURFACE}; color: {_C_TEXT};"
            f" border: 1px solid {_C_BORDER}; border-radius: 4px; }}"
            f"QPushButton:hover {{ border-color: {_C_GREEN}; }}"
        )
        browse_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(browse_btn)
        f.addRow("Dossier :", dir_row)

        self._vl.addLayout(f)
        self._vl.addWidget(_hint(
            "Le bouton ⏺ Clip dans le fullscreen enregistre les N dernières secondes "
            "du stream dans le dossier choisi. Format : .ts (Transport Stream)."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Cache MPV"))
        self._vl.addWidget(_hint(
            "Le cache demuxer MPV est fixé à max(durée + 30 s, 90 s) au démarrage. "
            "Les modifications de durée ne s'appliquent qu'au prochain lancement."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Depuis la grille"))

        self._grid_clips = QCheckBox(
            "Permettre de garder un moment depuis une cellule de la grille")
        self._grid_clips.setFont(QFont(_FONT_UI, 12))
        self._grid_clips.setChecked(bool(clips.get("grid_enabled", True)))
        self._vl.addWidget(self._grid_clips)
        self._vl.addWidget(_hint(
            "Chaque flux conserve alors les dernières secondes en mémoire. "
            "C'est un plafond, pas une réservation : à la qualité d'une grille, "
            "une minute pèse environ 2,5 Mo par flux."
        ))

        self._auto_clip = QCheckBox(
            "Enregistrer automatiquement quand HypeWatcher signale un moment")
        self._auto_clip.setFont(QFont(_FONT_UI, 12))
        # DÉSACTIVÉ par défaut, et c'est délibéré : une alerte n'est pas
        # nécessairement un moment qu'on veut garder, et un event génère
        # largement de quoi remplir un disque sans qu'on l'ait demandé.
        self._auto_clip.setChecked(bool(clips.get("auto_on_alert", False)))
        self._auto_clip.stateChanged.connect(self._on_auto_toggle)
        self._vl.addWidget(self._auto_clip)

        auto_row = self._form()
        self._auto_max = QSpinBox()
        self._auto_max.setRange(1, 60)
        self._auto_max.setValue(int(clips.get("auto_max_per_hour", 6)))
        self._auto_max.setFixedWidth(100)
        self._auto_max.setEnabled(self._auto_clip.isChecked())
        auto_row.addRow("Clips automatiques maximum par heure :", self._auto_max)
        self._vl.addLayout(auto_row)
        self._vl.addWidget(_hint(
            "Sans ce plafond, un soir d'affluence remplirait le dossier de "
            "fichiers que personne n'a demandés."
        ))

        self._vl.addStretch()

    def _on_auto_toggle(self) -> None:
        self._auto_max.setEnabled(self._auto_clip.isChecked())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier des clips", self._directory.text()
        )
        if folder:
            self._directory.setText(folder)

    def collect(self, config: dict) -> None:
        config.setdefault("clips", {})
        config["clips"]["duration_secs"] = self._duration.value()
        config["clips"]["directory"] = self._directory.text().strip()
        config["clips"]["grid_enabled"] = self._grid_clips.isChecked()
        config["clips"]["auto_on_alert"] = self._auto_clip.isChecked()
        config["clips"]["auto_max_per_hour"] = self._auto_max.value()


# ── SettingsPanel ─────────────────────────────────────────────────────────────

# Licences relevées sur les paquets réellement installés (dist-info) et sur
# le mpv du système, pas de mémoire.
_THIRD_PARTY: list[tuple[str, str]] = [
    ("Qt 6",                      "LGPL v3"),
    ("PyQt6 · PyQt6-WebEngine",   "GPL v3"),
    ("PyQt6-sip",                 "BSD 2-Clause"),
    ("mpv / libmpv",              "GPL v2+ et LGPL v2.1+"),
    ("python-mpv",                "GPL v2+ ou LGPL v2.1+"),
    ("Streamlink",                "BSD 2-Clause"),
    ("httpx",                     _LICENCE_BSD),
    ("QtAwesome",                 "MIT"),
    ("Material Design Icons",     "Apache 2.0"),
    ("PyQtGraph",                 "MIT"),
    ("NumPy",                     _LICENCE_BSD),
    ("lxml",                      _LICENCE_BSD),
    ("python-dotenv",             _LICENCE_BSD),
    ("pycryptodome",              "BSD et domaine public"),
    ("Chart.js",                  "MIT"),
]


class _PageDomotique(_PageBase):
    """Page Home Assistant — une URL de webhook, et ce qu'on lui envoie.

    ZLink repère déjà les paliers, les grosses donations, les objectifs
    imminents et les moments forts : il ne manquait qu'une sortie. Le
    clignotement, lui, reste chez Home Assistant — vingt requêtes en dix
    secondes depuis ici laisseraient les lampes éteintes à la première
    coupure, et personne d'autre que l'utilisateur ne sait quelles lampes.
    """

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config

        self._vl.addWidget(_h2(_TITRE_DOMOTIQUE))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_hint(
            "Faire réagir un éclairage — ou n'importe quoi d'autre — aux "
            "événements du ZEvent. ZLink envoie "
            "un message par événement sur un webhook Home Assistant ; ce que "
            "les lampes en font — clignoter dix secondes, changer de couleur, "
            "revenir comme avant — s'écrit dans une automatisation, côté Home "
            "Assistant."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Webhook"))
        conf = domotique.reglages(config)
        formulaire = self._form()
        # Le webhook D'ABORD : selon la version de Home Assistant, son éditeur
        # donne l'URL entière ou le seul identifiant. Le champ prend les deux,
        # et l'adresse en dessous ne sert qu'au second cas.
        self._webhook = QLineEdit(conf["webhook_id"] or conf["url"])
        self._webhook.setPlaceholderText(
            "https://homeassistant.local:8123/api/webhook/-XyZ123  ou  -XyZ123")
        self._webhook.setClearButtonEnabled(True)
        formulaire.addRow(_hint("URL ou ID du webhook"), self._webhook)
        self._base = QLineEdit(conf["base"])
        self._base.setPlaceholderText(domotique.BASE_DEFAUT)
        self._base.setClearButtonEnabled(True)
        formulaire.addRow(_hint("Adresse de Home Assistant"), self._base)
        #: La rangée se MASQUE quand une URL entière est collée. La griser ne
        #: suffisait pas : la feuille de style fixe `color` sans variante
        #: `:disabled`, ce qui annule le grisé de Qt — le champ paraissait
        #: actif tout en étant inerte.
        self._formulaire = formulaire
        # `_form` rend une disposition NUE : sans cet ajout, les deux champs
        # existent, sont bien remplis, et ne s'affichent nulle part.
        self._vl.addLayout(formulaire)
        self._vl.addWidget(_hint(
            "Dans Home Assistant : Paramètres → Automatisations → créer une "
            "automatisation, déclencheur « Webhook ». Recopiez ce qu'il "
            "affiche — l'URL entière selon les versions, l'identifiant seul "
            "sinon. L'adresse ne sert que dans ce second cas. Aucun jeton à "
            "créer : ce webhook tient lieu de secret, ne le publiez pas."
        ))
        self._adresse = _hint("")
        self._vl.addWidget(self._adresse)
        self._base.textChanged.connect(self._montrer_adresse)
        self._webhook.textChanged.connect(self._montrer_adresse)
        self._montrer_adresse()

        rangee = QWidget()
        hl = QHBoxLayout(rangee)
        hl.setContentsMargins(0, 0, 0, 0)
        self._essai = QPushButton("Envoyer un essai")
        self._essai.setFixedHeight(32)
        self._essai.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_C_MUTED}; "
            f"border: 1px solid {_C_BORDER}; border-radius: 4px; "
            f"padding: 0 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ color: #ffffff; border-color: {_C_GREEN}; }}"
        )
        self._essai.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._essai.clicked.connect(self._sur_essai)
        hl.addWidget(self._essai)
        hl.addStretch()
        self._vl.addWidget(rangee)
        self._message = _hint("")
        self._vl.addWidget(self._message)

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Ce qu'on annonce"))
        self._cases: dict[str, QCheckBox] = {}
        for cle, libelle in domotique.EVENEMENTS.items():
            case = QCheckBox(libelle)
            case.setChecked(cle in conf["evenements"])
            self._cases[cle] = case
            self._vl.addWidget(case)
        self._vl.addWidget(_hint(
            "Chaque message porte son type — « palier », « don », "
            "« objectif », « hype » — et de quoi le décrire : le montant et "
            "son libellé pour un palier, la chaîne concernée pour le reste. "
            "L'automatisation s'y branche pour ne réagir qu'à ce qui "
            "l'intéresse."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("L'automatisation à coller"))
        self._vl.addWidget(_hint(
            "Dans Home Assistant, sur l'écran de l'automatisation : le menu ⋮ "
            "TOUT EN HAUT de la page — pas celui de la carte du déclencheur — "
            "puis « Modifier en YAML ». Remplacez tout par ceci, en mettant "
            "vos lampes à la place de « light.salon »."
        ))

        formulaire_lampe = self._form()
        # VIDE au départ, pas pré-rempli : un exemple plausible se colle sans
        # qu'on y pense, et donne une automatisation qui se déclenche sans
        # rien allumer.
        self._lampe = QLineEdit(str(conf.get("lampes") or ""))
        self._lampe.setPlaceholderText("light.salon, light.bureau…")
        formulaire_lampe.addRow(_hint("Vos lampes"), self._lampe)
        self._vl.addLayout(formulaire_lampe)

        self._avertissement = _hint("")
        self._avertissement.setStyleSheet("color: #f5c518;")
        self._avertissement.setWordWrap(True)
        self._vl.addWidget(self._avertissement)

        self._yaml = QPlainTextEdit()
        self._yaml.setReadOnly(True)
        self._yaml.setFixedHeight(230)
        self._yaml.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._yaml.setStyleSheet(
            f"QPlainTextEdit {{ background: #0d0d0d; color: {_C_TEXT}; "
            f"border: 1px solid {_C_BORDER}; border-radius: 6px; padding: 8px; "
            f"font-family: '{_FONT_MONO}'; font-size: 11px; }}"
        )
        self._vl.addWidget(self._yaml)

        rangee_yaml = QWidget()
        hy = QHBoxLayout(rangee_yaml)
        hy.setContentsMargins(0, 0, 0, 0)
        self._copier = QPushButton("Copier l'automatisation")
        self._copier.setFixedHeight(32)
        self._copier.setStyleSheet(
            f"QPushButton {{ background: {_C_GREEN}; color: #000000; "
            f"border: none; border-radius: 4px; padding: 0 18px; "
            f"font-weight: bold; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #00cc6a; }}"
        )
        self._copier.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._copier.clicked.connect(self._sur_copie)
        hy.addWidget(self._copier)
        hy.addStretch()
        self._vl.addWidget(rangee_yaml)

        self._vl.addWidget(_hint(
            "Home Assistant affichera « scene.zlink_avant : entité non "
            "trouvée », en rouge. Celui-là est NORMAL : cette scène est créée "
            "par l'automatisation elle-même, à son exécution, pour "
            "photographier vos lampes avant de les faire clignoter. "
            "L'avertissement disparaît après le premier déclenchement.\n\n"
            "En revanche, le même message sur une entité « light.… » veut "
            "dire que ce nom de lampe n'existe pas : là, il faut corriger. "
            "L'automatisation se déclencherait sans rien allumer."
        ))
        self._vl.addWidget(_hint(
            "Enregistrez, puis revenez cliquer « Envoyer un essai » : les "
            "lampes doivent clignoter dix secondes et revenir comme avant. "
            "Cette version réagit à TOUT, essai compris — c'est ce qui permet "
            "de vérifier la chaîne entière. Pour ne garder que les paliers, "
            "remplacez « conditions: [] » par :"
        ))
        filtre = QLineEdit(
            "conditions: [{condition: template, "
            "value_template: \"{{ trigger.json.type == 'palier' }}\"}]")
        filtre.setReadOnly(True)
        self._vl.addWidget(filtre)
        self._vl.addWidget(_hint(
            "Les variantes — une couleur par montant, un effet par type "
            "d'événement, et quoi faire quand rien ne s'allume — sont dans "
            "« docs/homeassistant.md »."
        ))
        self._lampe.textChanged.connect(self._montrer_yaml)
        self._montrer_yaml()
        self._vl.addStretch()

    def _montrer_yaml(self) -> None:
        """Régénère l'automatisation, et prévient si l'adresse sort du réseau."""
        url = self._url()
        self._yaml.setPlainText(domotique.automatisation(
            self._webhook.text(), self._lampe.text(), url))
        local = domotique.est_local(url)
        # Le clair sur Internet passe AVANT : c'est le seul des deux qui
        # expose un secret, l'autre ne fait qu'empêcher le déclenchement.
        en_clair = domotique.avertissement_clair(url)
        if en_clair:
            self._avertissement.setText(en_clair)
            self._avertissement.setVisible(True)
            return
        self._avertissement.setText("" if local or not url else (
            "⚠ Cette adresse passe par Internet : le déclencheur est donc "
            "réglé sur « local_only: false », sans quoi Home Assistant "
            "écarterait la requête EN RÉPONDANT 200 — d'où un essai qui "
            "réussit sans que rien ne s'allume. L'identifiant du webhook "
            "devient alors un secret exposé à Internet ; l'adresse locale de "
            "la box est plus sûre si les deux machines sont sur le même "
            "réseau."))
        self._avertissement.setVisible(bool(self._avertissement.text()))

    def _sur_copie(self) -> None:
        QApplication.clipboard().setText(self._yaml.toPlainText())
        self._copier.setText("✓ Copié")
        QTimer.singleShot(1500,
                          lambda: self._copier.setText("Copier l'automatisation"))

    def _url(self) -> str:
        return domotique.composer(self._base.text(), self._webhook.text())

    def _montrer_adresse(self) -> None:
        """Affiche l'adresse retenue, et grise ce qui ne sert pas.

        Une URL entière collée en haut rend l'adresse inutile : la laisser
        active laisserait croire qu'elle compte, et on chercherait longtemps
        pourquoi la corriger ne change rien.
        """
        entier = self._webhook.text().strip().lower().startswith(
            ("http://", "https://"))
        self._formulaire.setRowVisible(1, not entier)
        url = self._url()
        self._adresse.setText(f"→ {url}" if url else "")
        if hasattr(self, "_yaml"):
            self._montrer_yaml()

    def _sur_essai(self) -> None:
        self._essai.setEnabled(False)
        reussi, message = domotique.essayer(self._url())
        self._message.setText(message)
        self._message.setStyleSheet(
            f"color: {_C_GREEN if reussi else _C_DANGER};")
        self._essai.setEnabled(True)

    def collect(self, config: dict) -> None:
        config["domotique"] = {
            "lampes": self._lampe.text().strip(),
            "base": self._base.text().strip(),
            "webhook_id": self._webhook.text().strip(),
            "evenements": [c for c, case in self._cases.items()
                           if case.isChecked()],
        }


class _PageStreamDeck(_PageBase):
    """Page Stream Deck — pose l'extension chez Elgato en un bouton.

    Sans elle, installer l'extension supposerait de trouver un dossier caché
    dans %APPDATA% et de savoir qu'il faut redémarrer le logiciel Elgato :
    deux choses qu'on ne peut pas demander à quelqu'un qui veut juste regarder
    des streams.
    """

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._vl.addWidget(_h2(_TITRE_DECK))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_hint(
            "Piloter ZLink depuis un boîtier Elgato : choisir le flux affiché "
            "en grand, clipper, ouvrir le chat, faire un don, et régler le son "
            "aux molettes. L'extension parle à ZLink sur la machine "
            "uniquement — rien ne sort sur le réseau."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("État"))
        self._etat_lbl = _hint("")
        self._vl.addWidget(self._etat_lbl)

        self._bouton = QPushButton("Installer l'extension")
        self._bouton.setFixedHeight(34)
        self._bouton.setStyleSheet(
            f"QPushButton {{ background: {_C_GREEN}; color: #000000; "
            f"border: none; border-radius: 4px; padding: 0 20px; "
            f"font-weight: bold; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #00cc6a; }}"
            f"QPushButton:disabled {{ background: #2a2a2a; color: #666666; }}"
        )
        self._bouton.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._bouton.clicked.connect(self._installer)
        self._bouton_profils = QPushButton("Ouvrir les profils")
        self._bouton_profils.setFixedHeight(34)
        self._bouton_profils.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_C_MUTED}; "
            f"border: 1px solid {_C_BORDER}; border-radius: 4px; "
            f"padding: 0 16px; font-size: 12px; }}"
            f"QPushButton:hover {{ color: #ffffff; border-color: {_C_GREEN}; }}"
            f"QPushButton:disabled {{ color: #4a4a4a; border-color: #2a2a2a; }}"
        )
        self._bouton_profils.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._bouton_profils.clicked.connect(self._ouvrir_profils)

        rangee = QWidget()
        hl = QHBoxLayout(rangee)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        hl.addWidget(self._bouton)
        hl.addWidget(self._bouton_profils)
        hl.addStretch()
        self._vl.addWidget(rangee)

        self._message = _hint("")
        self._message.setWordWrap(True)
        self._vl.addWidget(self._message)

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Une fois installée"))
        self._vl.addWidget(_hint(
            "Quitter puis relancer le logiciel Stream Deck : il ne relit ses "
            "extensions qu'au démarrage. ZLink apparaît ensuite dans la liste "
            "des actions, catégorie « ZLink ». Quatre actions à faire glisser "
            "sur les touches : Flux, Action, Navigation, et Mixage pour les "
            "molettes."
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Profils tout faits"))
        self._vl.addWidget(_hint(
            "Deux dispositions prêtes, plutôt que vingt touches à régler une "
            "à une : « ZLink — Grille » pour un Stream Deck 5×3 (treize flux "
            "et deux flèches de page), « ZLink — Régie » pour un Stream Deck + "
            "(chat, don, clip, revoir, muet, favori, précédent, suivant, et "
            "les quatre molettes du mixage).\n\n"
            "« Ouvrir les profils » montre les deux fichiers : un double-clic "
            "les importe. Un profil ne s'installe pas comme l'extension — le "
            "logiciel Stream Deck réécrit ses profils en se fermant, et un "
            "fichier posé pendant qu'il tourne disparaîtrait à sa sortie."
        ))
        self._vl.addStretch()

        self._rafraichir()

    def _rafraichir(self) -> None:
        situation = streamdeck_install.etat()
        if not situation["logiciel"]:
            texte = "Logiciel Stream Deck non détecté sur cette machine."
        elif situation["a_jour"]:
            texte = (f"Extension installée, version {situation['installee']} — "
                     "à jour.")
        elif situation["installee"]:
            texte = (f"Extension installée en version "
                     f"{situation['installee']}, ZLink en apporte la "
                     f"{situation['disponible']}.")
        else:
            texte = "Extension pas encore installée."
        self._etat_lbl.setText(texte)

        self._bouton.setEnabled(situation["possible"])
        self._bouton_profils.setEnabled(
            streamdeck_install.dossier_profils() is not None)
        self._bouton.setText("Réinstaller l'extension" if situation["installee"]
                             else "Installer l'extension")
        if situation["raison"]:
            self._message.setText(situation["raison"])
            self._message.setStyleSheet(f"color: {_C_MUTED};")

    def _ouvrir_profils(self) -> None:
        dossier = streamdeck_install.dossier_profils()
        if dossier is None:
            self._message.setText("Aucun profil livré avec cette copie de ZLink.")
            self._message.setStyleSheet(f"color: {_C_MUTED};")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(dossier)))

    def _installer(self) -> None:
        self._bouton.setEnabled(False)
        reussi, message = streamdeck_install.installer()
        self._message.setText(message)
        self._message.setStyleSheet(
            f"color: {_C_GREEN if reussi else _C_DANGER};")
        self._rafraichir()

    def collect(self, config: dict) -> None:
        """Page d'action : rien à enregistrer."""


class _PageCredits(_PageBase):
    """Page Crédits — sources de données, auteur, licence."""

    _LINK_STYLE = "color: #00ff87; text-decoration: none;"

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._vl.addWidget(_h2(f"Crédits — ZLink {display_version()}"))
        self._vl.addWidget(_sep())

        self._vl.addWidget(_section_title("Données"))
        self._vl.addWidget(self._link(
            "InGDoc — gdoc.fr",
            "https://gdoc.fr",
            "Programme, participations, objectifs de dons et avatars. "
            "Merci à l'équipe InGDoc, sans qui ce panel n'aurait rien à afficher.",
        ))
        self._vl.addWidget(self._link(
            "ZEvent — API officielle",
            "https://zevent.fr",
            "Cagnotte globale, viewers et état des lives.",
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Projet"))
        self._vl.addWidget(self._link(
            "Fabien Millet — fabienmillet",
            "https://github.com/fabienmillet",
            "Conception et développement de ZLink.",
        ))
        self._vl.addWidget(self._link(
            "Licence GNU GPL v3 ou ultérieure",
            "https://github.com/fabienmillet/ZLink/blob/main/LICENSE",
            "Copyright (C) 2026 Fabien MILLET. Logiciel libre : utilisation, "
            "étude, modification et redistribution garanties, à condition que "
            "les versions dérivées restent sous la même licence et que leurs "
            "sources soient fournies.",
        ))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Logiciels tiers"))
        self._vl.addWidget(_hint(
            "ZLink s'appuie sur ces projets. Plusieurs de leurs licences "
            "imposent de conserver l'avis de copyright en cas de "
            "redistribution. C'est PyQt6, en GPL v3, qui détermine la licence "
            "de l'ensemble distribué."
        ))
        for name, lic in _THIRD_PARTY:
            self._vl.addWidget(self._credit_line(name, lic))

        self._vl.addWidget(_sep())
        self._vl.addWidget(_hint(
            "ZLink n'est pas affilié à l'organisation du ZEvent. "
            "Les dons se font exclusivement sur zevent.fr."
        ))
        self._vl.addStretch()

    def _credit_line(self, name: str, lic: str) -> QWidget:
        """Une ligne « projet — licence »."""
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        n = QLabel(name)
        n.setFont(QFont(_FONT_UI, 11))
        n.setStyleSheet("color: #cccccc;")
        hl.addWidget(n)
        l = QLabel(lic)
        l.setFont(QFont(_FONT_UI, 10))
        l.setStyleSheet(f"color: {_C_MUTED};")
        hl.addWidget(l)
        hl.addStretch()
        return row

    def _link(self, title: str, url: str, desc: str) -> QWidget:
        """Bloc titre cliquable + description."""
        box = QWidget()
        vl = QVBoxLayout(box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)
        lbl = QLabel(f'<a href="{url}" style="{self._LINK_STYLE}">{title}</a>')
        lbl.setFont(QFont(_FONT_UI, 12, QFont.Weight.Bold))
        lbl.setTextFormat(Qt.TextFormat.RichText)
        # Ouverture dans le navigateur du système, pas dans l'application.
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        vl.addWidget(lbl)
        vl.addWidget(_hint(desc))
        return box

    def collect(self, config: dict) -> None:
        """Page informative : rien à enregistrer."""


_NAV_ITEMS = [
    ("Streams",      "mdi6.play-circle-outline"),
    (_TITRE_ECRANS,       "mdi6.monitor-multiple"),
    ("Alertes",      "mdi6.bell-outline"),
    ("Clips",        "mdi6.record-circle-outline"),
    (_TITRE_DECK,    "mdi6.view-grid-outline"),
    (_TITRE_DOMOTIQUE, "mdi6.home-automation"),
    ("Crédits",      "mdi6.heart-outline"),
]


class SettingsPanel(QWidget):
    """Panel paramètres plein-écran, superposé sur PanelWindow.

    Usage::
        panel = SettingsPanel(parent=central_widget)
        panel.setGeometry(0, 0, cw.width(), cw.height())
        panel.settings_changed.connect(handler)
        panel.close_requested.connect(_close_settings)
        panel.show(); panel.raise_()
    """

    settings_changed = pyqtSignal(dict)
    close_requested  = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            _SS_BASE + "SettingsPanel { background: rgba(0,0,0,210); }"
        )
        self._config = self._load_config()
        self._build()

    # ── persistence ──────────────────────────────────────────────────

    def _load_config(self) -> dict:
        return config_store.load()

    def _save_config(self) -> None:
        """Écrit les réglages en FUSIONNANT avec le fichier actuel.

        `self._config` date de l'ouverture de la fenêtre. Le réécrire tel quel
        rétablissait tout ce qui avait bougé depuis par un autre chemin :
        favoris, rappels du programme, choix de l'assistant.
        """
        config_store.save_merge(self._config)

    # ── build ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setStyleSheet(f"background: {_C_SIDEBAR};")
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(12, 20, 12, 20)
        sl.setSpacing(4)

        lbl_title = QLabel("Paramètres")
        lbl_title.setFont(QFont(_FONT_UI, 13, QFont.Weight.Bold))
        lbl_title.setStyleSheet("color: #ffffff; padding: 4px 4px 12px 4px;")
        sl.addWidget(lbl_title)

        self._nav_items: list[_NavItem] = []
        for label, icon in _NAV_ITEMS:
            item = _NavItem(label, icon)
            item.clicked.connect(lambda l=label: self._switch_page(l))
            sl.addWidget(item)
            self._nav_items.append(item)

        sl.addStretch()
        sl.addWidget(_sep())

        close_btn = QPushButton("  Fermer")
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_C_MUTED}; "
            f"border: none; border-radius: 4px; text-align: left; "
            f"padding-left: 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {_C_HOVER}; color: #ffffff; }}"
        )
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        if _QTA_OK:
            close_btn.setIcon(qta.icon("mdi6.close", color=_C_MUTED))
        close_btn.clicked.connect(self.close_requested)
        sl.addWidget(close_btn)

        root.addWidget(sidebar)

        # ── Content ───────────────────────────────────────────────────
        content_area = QWidget()
        content_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        content_area.setStyleSheet(f"background: {_C_BG};")
        cl = QVBoxLayout(content_area)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self._pages_stack = QStackedWidget()
        self._pages_stack.setStyleSheet(_FOND_TRANSPARENT)

        self._page_streams = _PageStreams(self._config)
        self._page_screens = _PageScreens(self._config)
        self._page_hype    = _PageHype(self._config)
        self._page_clips   = _PageClips(self._config)
        self._page_deck    = _PageStreamDeck(self._config)
        self._page_domo    = _PageDomotique(self._config)
        self._page_credits = _PageCredits(self._config)

        for page in (self._page_streams, self._page_screens, self._page_hype,
                     self._page_clips, self._page_deck, self._page_domo,
                     self._page_credits):
            self._pages_stack.addWidget(_scroll_wrap(page))

        cl.addWidget(self._pages_stack, stretch=1)

        # Footer
        footer = QWidget()
        footer.setFixedHeight(60)
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        footer.setStyleSheet(
            f"background: {_C_SIDEBAR}; border-top: 1px solid {_C_BORDER};"
        )
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(32, 0, 32, 0)
        fl.setSpacing(12)
        fl.addStretch()

        self._footer_error = QLabel("")
        self._footer_error.setStyleSheet(f"color: {_C_DANGER};")
        fl.addWidget(self._footer_error)

        self._save_btn = QPushButton("Sauvegarder")
        self._save_btn.setFixedHeight(34)
        self._save_btn.setStyleSheet(
            f"QPushButton {{ background: {_C_GREEN}; color: #000000; "
            f"border: none; border-radius: 4px; padding: 0 20px; "
            f"font-weight: bold; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #00cc6a; }}"
            f"QPushButton:disabled {{ background: #003322; color: #005533; }}"
        )
        self._save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._save_btn.clicked.connect(self._on_save)
        fl.addWidget(self._save_btn)

        cl.addWidget(footer)
        root.addWidget(content_area, stretch=1)

        self._switch_page(_NAV_ITEMS[0][0])

    # ── navigation ────────────────────────────────────────────────────

    def _switch_page(self, label: str) -> None:
        for i, (lbl, _icon) in enumerate(_NAV_ITEMS):
            active = lbl == label
            self._nav_items[i].set_active(active)
            if active:
                self._pages_stack.setCurrentIndex(i)

    # ── save ──────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        self._page_streams.collect(self._config)
        self._page_hype.collect(self._config)
        self._page_clips.collect(self._config)
        self._page_domo.collect(self._config)

        if not self._page_screens.collect(self._config):
            self._switch_page(_TITRE_ECRANS)
            self._footer_error.setText("⚠ Corriger les écrans avant de sauvegarder.")
            return

        self._footer_error.setText("")
        self._save_config()
        self.settings_changed.emit(dict(self._config))

        self._save_btn.setText("✓ Sauvegardé !")
        self._save_btn.setEnabled(False)
        QTimer.singleShot(1500, self._reset_save_btn)

    def _reset_save_btn(self) -> None:
        self._save_btn.setText("Sauvegarder")
        self._save_btn.setEnabled(True)

    # ── public ────────────────────────────────────────────────────────

    def refresh_config(self) -> None:
        """Recharge config.json (utile si le panel se rouvre après une modif externe)."""
        self._config = self._load_config()


# Alias backward-compat
SettingsDialog = SettingsPanel
