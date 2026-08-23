"""Settings panel — vue plein-panel façon Discord.

Intégré comme overlay dans PanelWindow (même pattern que BigScreenWidget).
Signal settings_changed(dict) émis à chaque sauvegarde.
Signal close_requested()      émis quand l'utilisateur ferme le panel.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont
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
except ImportError:
    _QTA_OK = False

from core.paths import CONFIG_PATH

logger = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────

_C_BG       = "#111111"
_C_SIDEBAR  = "#0d0d0d"
_C_SURFACE  = "#1a1a1a"
_C_BORDER   = "#2a2a2a"
_C_TEXT     = "#cccccc"
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
            self.setStyleSheet(f"background: #222222; border-radius: 6px; border: none;")
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
    scroll.viewport().setStyleSheet("background: transparent;")
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
        self._grid_quality.addItems(["360p,worst", "480p,360p,worst", "720p,480p,worst"])
        idx = self._grid_quality.findText(config.get("grid_quality", "360p,worst"))
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
        self._max_streams.setValue(config.get("max_active_streams", 20))
        self._max_streams.setFixedWidth(100)
        f2.addRow("Max streams actifs :", self._max_streams)
        self._vl.addLayout(f2)
        self._vl.addWidget(_hint(
            "Nombre maximum de streams lancés simultanément dans la grille."
        ))
        self._vl.addStretch()

    def collect(self, config: dict) -> None:
        config["grid_adaptive"] = self._adaptive.isChecked()
        config["grid_quality"] = self._grid_quality.currentText()
        config["fullscreen_quality"] = self._fs_quality.currentText()
        config["max_active_streams"] = self._max_streams.value()


class _PageScreens(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        screens = sorted(
            QApplication.instance().screens(),  # type: ignore[union-attr]
            key=lambda s: s.geometry().x(),
        )
        n = len(screens)
        _default: dict[int, str] = {}
        if not config.get("screen_assignments"):
            if n == 1:
                _default = {0: "fullscreen"}
            elif n == 2:
                _default = {0: "panel", 1: "fullscreen"}
            else:
                _default = {0: "panel", 1: "fullscreen", 2: "grid"}

        self._vl.addWidget(_h2("Écrans"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Attribution des moniteurs"))

        f = self._form()
        self._screen_combos: list[QComboBox] = []
        screen_cfg: dict[str, str] = config.get("screen_assignments") or {}
        _role_to_idx = {"disabled": 0, "panel": 1, "fullscreen": 2, "grid": 3}

        for i, screen in enumerate(screens):
            g = screen.geometry()
            combo = QComboBox()
            combo.addItems(["— Désactivé", "Panel", "Fullscreen", "Grille"])
            role_str = screen_cfg.get(str(i)) or _default.get(i, "disabled")
            combo.setCurrentIndex(_role_to_idx.get(role_str, 0))
            f.addRow(f"Écran {i + 1}  ({g.width()}×{g.height()}) :", combo)
            self._screen_combos.append(combo)

        self._vl.addLayout(f)

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet(f"color: {_C_DANGER};")
        self._vl.addWidget(self._error_lbl)
        self._vl.addWidget(_hint(
            "↺ Les modifications d'écrans sont prises en compte au prochain démarrage."
        ))
        self._vl.addStretch()

    def collect(self, config: dict) -> bool:
        _idx_to_role = {0: "disabled", 1: "panel", 2: "fullscreen", 3: "grid"}
        screen_assignments: dict[str, str] = {}
        has_fullscreen = False
        for i, combo in enumerate(self._screen_combos):
            role = _idx_to_role[combo.currentIndex()]
            if role != "disabled":
                screen_assignments[str(i)] = role
            if role == "fullscreen":
                has_fullscreen = True
        if not has_fullscreen:
            self._error_lbl.setText("⚠ Au moins un écran doit être en Fullscreen.")
            return False
        self._error_lbl.setText("")
        config["screen_assignments"] = screen_assignments
        return True


class _PageAPIs(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._vl.addWidget(_h2("APIs"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Intelligence artificielle"))

        f = self._form()

        _GEMINI_MODELS = [
            "gemini-2.5-flash-preview-04-17",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]
        _OPENAI_MODELS = [
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1",
        ]
        self._gemini_models = _GEMINI_MODELS
        self._openai_models = _OPENAI_MODELS

        self._ai_provider = QComboBox()
        self._ai_provider.addItems(["Gemini", "OpenAI"])
        current_provider = config.get("ai_provider", "gemini").lower()
        self._ai_provider.setCurrentIndex(0 if current_provider != "openai" else 1)
        f.addRow("Fournisseur IA :", self._ai_provider)

        self._ai_model = QComboBox()
        self._ai_model_row_label = QLabel("Modèle :")
        f.addRow(self._ai_model_row_label, self._ai_model)
        self._current_model_cfg = config.get("ai_model", "")

        self._gemini_key = QLineEdit(config.get("gemini_api_key", ""))
        self._gemini_key.setPlaceholderText("AIza…")
        self._gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._gemini_key_row_label = QLabel("Clé Gemini :")
        f.addRow(self._gemini_key_row_label, self._gemini_key)

        self._openai_key = QLineEdit(config.get("openai_api_key", ""))
        self._openai_key.setPlaceholderText("sk-…")
        self._openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._openai_key_row_label = QLabel("Clé OpenAI :")
        f.addRow(self._openai_key_row_label, self._openai_key)

        self._vl.addLayout(f)

        self._hint_ai = _hint("")
        self._vl.addWidget(self._hint_ai)

        self._ai_provider.currentIndexChanged.connect(self._on_provider_changed)
        self._on_provider_changed(self._ai_provider.currentIndex())

        self._vl.addStretch()

    def _on_provider_changed(self, index: int) -> None:
        is_gemini = index == 0
        self._gemini_key.setVisible(is_gemini)
        self._gemini_key_row_label.setVisible(is_gemini)
        self._openai_key.setVisible(not is_gemini)
        self._openai_key_row_label.setVisible(not is_gemini)

        models = self._gemini_models if is_gemini else self._openai_models
        self._ai_model.blockSignals(True)
        self._ai_model.clear()
        self._ai_model.addItems(models)
        idx = self._ai_model.findText(self._current_model_cfg)
        self._ai_model.setCurrentIndex(idx if idx >= 0 else 0)
        self._ai_model.blockSignals(False)

        if is_gemini:
            self._hint_ai.setText(
                "Gemini est utilisé pour classifier les moments forts "
                "quand le score est ambigu. Laisser vide pour désactiver."
            )
        else:
            self._hint_ai.setText(
                "OpenAI est utilisé pour classifier les moments forts "
                "quand le score est ambigu. Laisser vide pour désactiver."
            )

    def collect(self, config: dict) -> None:
        config["ai_provider"] = "gemini" if self._ai_provider.currentIndex() == 0 else "openai"
        config["ai_model"] = self._ai_model.currentText()
        config["gemini_api_key"] = self._gemini_key.text().strip()
        config["openai_api_key"] = self._openai_key.text().strip()


class _PageHype(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        hw = config.get("hypewatcher", {})

        self._vl.addWidget(_h2("HypeWatcher"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Activation"))

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
        self._advanced.setStyleSheet("background: transparent;")
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
        f.addRow("Alerte directe ≥ :", score_high_row)

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
        f.addRow("Appel IA ≥ :", score_med_row)
        adv_vl.addLayout(f)

        adv_vl.addWidget(_sep())
        adv_vl.addWidget(_section_title("Cooldown"))
        f2 = self._form()
        self._cooldown = QSpinBox()
        self._cooldown.setRange(10, 600)
        self._cooldown.setSuffix(" s")
        self._cooldown.setValue(int(hw.get("cooldown_s", 90)))
        self._cooldown.setFixedWidth(100)
        f2.addRow("Délai entre alertes :", self._cooldown)
        adv_vl.addLayout(f2)
        adv_vl.addWidget(_hint(
            "Délai minimum (en secondes) entre deux alertes pour le même streamer."
        ))

        self._vl.addWidget(self._advanced)
        self._vl.addStretch()

        self._on_toggle(None)

    def _on_toggle(self, _state: object) -> None:
        self._advanced.setVisible(self._enabled_cb.isChecked())

    def collect(self, config: dict) -> None:
        hw = config.setdefault("hypewatcher", {})
        hw["enabled"] = self._enabled_cb.isChecked()
        hw["score_high"] = self._score_high.value() / 100.0
        hw["score_medium"] = self._score_med.value() / 100.0
        hw["cooldown_s"] = self._cooldown.value()


class _PageClips(_PageBase):
    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        clips = config.get("clips", {})
        from pathlib import Path as _Path

        self._vl.addWidget(_h2("Clips"))
        self._vl.addWidget(_sep())
        self._vl.addWidget(_section_title("Enregistrement"))

        f = self._form()

        # Durée
        dur_row = QHBoxLayout()
        self._duration = QSpinBox()
        self._duration.setRange(10, 300)
        self._duration.setValue(clips.get("duration_secs", 60))
        self._duration.setSuffix(" s")
        self._duration.setFixedWidth(100)
        dur_row.addWidget(self._duration)
        dur_row.addStretch()
        f.addRow("Durée du clip :", dur_row)

        # Dossier
        dir_row = QHBoxLayout()
        default_dir = str(_Path.home() / "Videos" / "ZLink")
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

        self._vl.addStretch()

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


# ── SettingsPanel ─────────────────────────────────────────────────────────────

_NAV_ITEMS = [
    ("Streams",      "mdi6.play-circle-outline"),
    ("Écrans",       "mdi6.monitor-multiple"),
    ("APIs",         "mdi6.key-outline"),
    ("HypeWatcher",  "mdi6.bell-outline"),    ("Clips",        "mdi6.record-circle-outline"),]


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
        try:
            if CONFIG_PATH.exists():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_config(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(self._config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Le fichier peut contenir des clés API : lisible par le seul propriétaire.
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError as exc:
            logger.warning("Permissions de %s non restreintes : %s", CONFIG_PATH, exc)

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
        self._pages_stack.setStyleSheet("background: transparent;")

        self._page_streams = _PageStreams(self._config)
        self._page_screens = _PageScreens(self._config)
        self._page_apis    = _PageAPIs(self._config)
        self._page_hype    = _PageHype(self._config)
        self._page_clips   = _PageClips(self._config)

        for page in (self._page_streams, self._page_screens, self._page_apis, self._page_hype, self._page_clips):
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
        self._page_apis.collect(self._config)
        self._page_hype.collect(self._config)
        self._page_clips.collect(self._config)

        if not self._page_screens.collect(self._config):
            self._switch_page("Écrans")
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
