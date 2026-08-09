#!/usr/bin/env python3
"""
wine-sandbox-gui.py - Frontend grafico per gestire ed eseguire giochi
abandonware attraverso wine-sandbox (isolamento bwrap: rete disabilitata,
capability Linux disattivate, filesystem in gran parte read-only).

Tre sezioni:
  - Giochi: libreria di giochi installati, installazione nuovi, avvio in sandbox
  - Immagini ottiche: montaggio ISO/IMG/NRG/BIN+CUE senza sudo (udisksctl)
  - Prefix Wine: creazione prefix, versione Windows, winecfg, winetricks

Richiede:
  - PySide6:       pip install --break-system-packages PySide6
                    (o su Arch/CachyOS: sudo pacman -S pyside6)
  - wine-sandbox:   script già configurato (vedi campo impostazioni)
  - wine, winetricks, udisks2, bchunk installati sul sistema

Uso: python3 wine-sandbox-gui.py

Nota sulla sicurezza: winecfg e regedit sono strumenti Wine
legittimi e vengono lanciati DIRETTAMENTE (senza bwrap), perché non
eseguono mai il file di gioco non fidato. La creazione del prefix
(wineboot) passa invece attraverso wine-sandbox --init, con sandbox
attiva (rete disabilitata, home nascosta). Solo l'installer e il gioco
vero e proprio passano sempre attraverso wine-sandbox.
"""

import sys
import os
import re
import json
import shutil
import tempfile
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QProcess, QUrl, QProcessEnvironment, QThread, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QPlainTextEdit, QFileDialog, QMessageBox, QInputDialog, QSplitter,
    QGroupBox, QFormLayout, QTabWidget, QComboBox, QCheckBox, QGridLayout,
    QScrollArea
)

CONFIG_DIR = Path.home() / ".config" / "wine-sandbox-gui"
GAMES_FILE = CONFIG_DIR / "games.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PREFIXES_FILE = CONFIG_DIR / "prefixes.json"

DATA_DIR = Path.home() / ".local" / "share" / "wine-sandbox-gui"
LAUNCH_HISTORY_FILE = DATA_DIR / "launch-history.log"

FALLBACK_WINE_SANDBOX_PATH = str(Path.home() / ".local" / "bin" / "wine-sandbox")
FALLBACK_PREFIX_ROOT = str(Path.home())
FALLBACK_GAMES_ROOT = str(Path.home())

DEFAULT_SETTINGS = {
    "wine_sandbox_path": FALLBACK_WINE_SANDBOX_PATH,
    "prefix_root": FALLBACK_PREFIX_ROOT,
    "games_root": FALLBACK_GAMES_ROOT,
    "sec_hide_home": True,
    "sec_cap_drop": True,
    "sec_unshare_pid": True,
    "sec_dri": True,
    "sec_audio": True,
    "sec_loopback": False,
    "sec_allow_network": False,
    "sec_disable_zdrive": True,
    "sec_exe_rw": False,
    "sec_verify_integrity": True,
    "sec_resource_limits": False,
    "sec_memory_limit": "2G",
    "sec_cpu_limit": "200",
    "enable_desktop_launcher_creation": False,
    "unmount_on_exit": True,
    "bchunk_output_dir": "",
}

WINDOWS_VERSIONS = [
    ("Windows 3.1", "win31"),
    ("Windows 95", "win95"),
    ("Windows 98", "win98"),
    ("Windows 2000", "win2k"),
    ("Windows XP (32-bit)", "winxp"),
    ("Windows XP (64-bit)", "winxp64"),
    ("Windows Server 2003", "win2k3"),
    ("Windows Vista", "vista"),
    ("Windows 7", "win7"),
    ("Windows 8", "win8"),
    ("Windows 10", "win10"),
]

RUNTIME_WINETRICKS_VERBS = [
    "corefonts", "vcrun6", "vcrun2019", "dxvk", "d3dx9", "directmusic",
    "quartz", "gdiplus", "msxml3", "msxml6",
]

CODEC_WINETRICKS_VERBS = [
    "allcodecs", "ffdshow", "xvid", "l3codecx", "cinepak", "dirac",
    "icodecs", "wmp9", "wmp11",
]

DGVOODOO_REPO_API = "https://api.github.com/repos/dege-diosg/dgVoodoo2/releases/latest"

REQUIRED_TOOLS = [
    ("wine", "Esecuzione dei giochi Windows"),
    ("wineboot", "Creazione/inizializzazione dei prefix Wine"),
    ("winetricks", "Installazione componenti (dxvk, corefonts, ecc.)"),
    ("bwrap", "Isolamento sandbox (rete disabilitata, cap-drop)"),
    ("udisksctl", "Montaggio ISO/IMG/NRG senza sudo"),
    ("bchunk", "Conversione BIN/CUE in ISO"),
    ("7z", "Estrazione ISO/IMG/NRG/7z"),
    ("unzip", "Estrazione archivi ZIP"),
]


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


class DgVoodooDownloadThread(QThread):
    """Scarica ed estrae l'ultima release di dgVoodoo2 da GitHub, in background
    per non bloccare l'interfaccia durante il download."""
    log = Signal(str)
    finished_ok = Signal(str)   # percorso cartella estratta
    finished_error = Signal(str)

    def run(self):
        try:
            self.log.emit(f"Interrogo l'API GitHub: {DGVOODOO_REPO_API}")
            req = urllib.request.Request(
                DGVOODOO_REPO_API, headers={"User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                release_data = json.loads(resp.read().decode())

            zip_asset = next(
                (a for a in release_data.get("assets", []) if a["name"].lower().endswith(".zip")),
                None
            )
            if not zip_asset:
                self.finished_error.emit("Nessun asset .zip trovato nell'ultima release di dgVoodoo2.")
                return

            download_url = zip_asset["browser_download_url"]
            version_tag = release_data.get("tag_name", "sconosciuta")
            self.log.emit(f"Versione trovata: {version_tag} - scarico: {download_url}")

            tmp_dir = tempfile.mkdtemp(prefix="dgvoodoo2-")
            zip_path = os.path.join(tmp_dir, zip_asset["name"])

            req_dl = urllib.request.Request(
                download_url, headers={"User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req_dl, timeout=60) as resp, open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out)

            self.log.emit(f"Scaricato in: {zip_path}, estraggo...")

            import zipfile
            extract_dir = os.path.join(tmp_dir, "estratto")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            self.finished_ok.emit(extract_dir)

        except Exception as e:
            self.finished_error.emit(f"Errore durante il download di dgVoodoo2: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Wine Sandbox - Libreria giochi abandonware")
        self.resize(1050, 700)

        self.games = load_json(GAMES_FILE, [])
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self.prefixes = load_json(PREFIXES_FILE, [])
        self.mounted_images = []  # [{device, path, mount_point}]

        self.process = None          # processo wine-sandbox (giochi/installer)
        self.wine_tool_process = None  # processo per winecfg/wineboot/winetricks diretti
        self.dgvoodoo_thread = None
        self.winetricks_checkboxes = {}

        self._build_ui()
        self._load_security_settings_into_ui()
        self._refresh_game_list()
        self._refresh_mounted_list()
        self._refresh_prefix_list()

    # ------------------------------------------------------------------
    # Costruzione interfaccia
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        settings_box = QGroupBox("Impostazioni")
        settings_layout = QFormLayout(settings_box)

        ws_row = QHBoxLayout()
        self.wine_sandbox_path_edit = QLineEdit(self.settings["wine_sandbox_path"])
        self.wine_sandbox_path_edit.editingFinished.connect(self._save_settings_from_ui)
        ws_row.addWidget(self.wine_sandbox_path_edit)
        btn_browse_ws = QPushButton("Sfoglia...")
        btn_browse_ws.clicked.connect(self._browse_wine_sandbox)
        ws_row.addWidget(btn_browse_ws)
        settings_layout.addRow("Percorso script wine-sandbox:", ws_row)

        prefix_row = QHBoxLayout()
        self.prefix_root_edit = QLineEdit(self.settings["prefix_root"])
        self.prefix_root_edit.editingFinished.connect(self._save_settings_from_ui)
        prefix_row.addWidget(self.prefix_root_edit)
        btn_browse_prefix_root = QPushButton("Sfoglia...")
        btn_browse_prefix_root.clicked.connect(self._browse_prefix_root)
        prefix_row.addWidget(btn_browse_prefix_root)
        settings_layout.addRow("Cartella predefinita dei prefix:", prefix_row)

        games_row = QHBoxLayout()
        self.games_root_edit = QLineEdit(self.settings["games_root"])
        self.games_root_edit.editingFinished.connect(self._save_settings_from_ui)
        games_row.addWidget(self.games_root_edit)
        btn_browse_games_root = QPushButton("Sfoglia...")
        btn_browse_games_root.clicked.connect(self._browse_games_root)
        games_row.addWidget(btn_browse_games_root)
        settings_layout.addRow("Cartella predefinita dei giochi/ISO:", games_row)

        main_layout.addWidget(settings_box)

        tabs = QTabWidget()
        main_layout.addWidget(tabs, stretch=1)

        tabs.addTab(self._build_games_tab(), "Giochi")
        tabs.addTab(self._build_mount_tab(), "Immagini ottiche")
        tabs.addTab(self._build_prefix_tab(), "Prefix Wine")
        tabs.addTab(self._build_system_tab(), "Sistema")

    # ------------------------------------------------------------------
    # Tab Giochi
    # ------------------------------------------------------------------
    def _build_games_tab(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("Giochi configurati:"))

        self.game_list = QListWidget()
        self.game_list.itemSelectionChanged.connect(self._update_button_states)
        self.game_list.itemDoubleClicked.connect(lambda item: self._on_play_clicked())
        left_layout.addWidget(self.game_list, stretch=1)

        btn_row1 = QHBoxLayout()
        self.btn_play = QPushButton("▶ Gioca")
        self.btn_play.clicked.connect(self._on_play_clicked)
        btn_row1.addWidget(self.btn_play)

        self.btn_remove = QPushButton("Rimuovi dalla lista")
        self.btn_remove.clicked.connect(self._on_remove_clicked)
        btn_row1.addWidget(self.btn_remove)
        left_layout.addLayout(btn_row1)

        btn_row_folders = QHBoxLayout()
        self.btn_open_game_folder = QPushButton("📁 Apri cartella gioco")
        self.btn_open_game_folder.clicked.connect(self._on_open_game_folder)
        btn_row_folders.addWidget(self.btn_open_game_folder)

        self.btn_open_game_prefix = QPushButton("📁 Apri cartella prefix")
        self.btn_open_game_prefix.clicked.connect(self._on_open_game_prefix_folder)
        btn_row_folders.addWidget(self.btn_open_game_prefix)
        left_layout.addLayout(btn_row_folders)

        security_box = QGroupBox("Sicurezza sandbox (si applica a Gioca e Installa)")
        security_layout = QVBoxLayout(security_box)

        self.sec_hide_home = QCheckBox("Nascondi la home (consigliato)")
        self.sec_hide_home.setChecked(True)
        self.sec_hide_home.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_hide_home)

        self.sec_cap_drop = QCheckBox("Blocca tutte le capability Linux (consigliato)")
        self.sec_cap_drop.setChecked(True)
        self.sec_cap_drop.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_cap_drop)

        self.sec_unshare_pid = QCheckBox("Isola visibilità processi (consigliato)")
        self.sec_unshare_pid.setChecked(True)
        self.sec_unshare_pid.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_unshare_pid)

        self.sec_dri = QCheckBox("Consenti GPU/accelerazione 3D (serve per la maggior parte dei giochi)")
        self.sec_dri.setChecked(True)
        self.sec_dri.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_dri)

        self.sec_audio = QCheckBox("Consenti audio")
        self.sec_audio.setChecked(True)
        self.sec_audio.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_audio)

        self.sec_loopback = QCheckBox(
            "Attiva localhost/loopback (per giochi con componenti interni via socket locali, "
            "resta isolato da internet)")
        self.sec_loopback.setChecked(False)
        self.sec_loopback.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_loopback)

        self.sec_allow_network = QCheckBox(
            "⚠️ Abilita rete internet (disattiva la protezione principale, usa solo se necessario)")
        self.sec_allow_network.setChecked(False)
        self.sec_allow_network.stateChanged.connect(self._on_network_toggle_changed)
        security_layout.addWidget(self.sec_allow_network)

        self.sec_disable_zdrive = QCheckBox(
            "Blocca l'unità Z: (accesso alla radice del filesystem, consigliato)")
        self.sec_disable_zdrive.setChecked(True)
        self.sec_disable_zdrive.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_disable_zdrive)

        self.sec_exe_rw = QCheckBox(
            "Permetti scrittura nella cartella del gioco (salvataggi accanto all'exe, "
            "disattiva solo se necessario)")
        self.sec_exe_rw.setChecked(False)
        self.sec_exe_rw.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_exe_rw)

        self.sec_verify_integrity = QCheckBox(
            "Verifica integrità prefix dopo l'esecuzione (confronta file modificati)")
        self.sec_verify_integrity.setChecked(True)
        self.sec_verify_integrity.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_verify_integrity)

        resource_row = QHBoxLayout()
        self.sec_resource_limits = QCheckBox("Limita risorse (CPU/RAM) via systemd-run:")
        self.sec_resource_limits.setChecked(False)
        self.sec_resource_limits.stateChanged.connect(self._save_settings_from_ui)
        resource_row.addWidget(self.sec_resource_limits)

        resource_row.addWidget(QLabel("RAM max:"))
        self.sec_memory_limit_edit = QLineEdit("2G")
        self.sec_memory_limit_edit.setMaximumWidth(60)
        self.sec_memory_limit_edit.editingFinished.connect(self._save_settings_from_ui)
        resource_row.addWidget(self.sec_memory_limit_edit)

        resource_row.addWidget(QLabel("CPU max %:"))
        self.sec_cpu_limit_edit = QLineEdit("200")
        self.sec_cpu_limit_edit.setMaximumWidth(50)
        self.sec_cpu_limit_edit.editingFinished.connect(self._save_settings_from_ui)
        resource_row.addWidget(self.sec_cpu_limit_edit)

        security_layout.addLayout(resource_row)

        left_layout.addWidget(security_box)

        left_layout.addWidget(QLabel(""))
        left_layout.addWidget(QLabel("Nuova installazione:"))

        self.btn_install = QPushButton("📦 Installa nuovo gioco (esegui setup)")
        self.btn_install.clicked.connect(self._on_install_clicked)
        left_layout.addWidget(self.btn_install)

        self.btn_add_existing = QPushButton("➕ Aggiungi gioco già installato")
        self.btn_add_existing.clicked.connect(self._on_add_existing_clicked)
        left_layout.addWidget(self.btn_add_existing)

        splitter.addWidget(left_widget)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.addWidget(QLabel("Log:"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("font-family: monospace; font-size: 10pt;")
        right_layout.addWidget(self.log_output, stretch=1)

        self.btn_clear_log = QPushButton("Pulisci log")
        self.btn_clear_log.clicked.connect(self.log_output.clear)
        right_layout.addWidget(self.btn_clear_log)

        splitter.addWidget(right_widget)
        splitter.setSizes([380, 670])

        self._update_button_states()
        return tab

    # ------------------------------------------------------------------
    # Tab Montaggio immagini
    # ------------------------------------------------------------------
    def _build_mount_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        info = QLabel(
            "Monta ISO/IMG/NRG direttamente (via udisksctl, nessun sudo richiesto).\n"
            "I file BIN vengono prima convertiti in ISO con bchunk (serve il .cue corrispondente).\n"
            "I file MDF/MDS non sono supportati automaticamente (formato Alcohol 120% proprietario)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        self.btn_mount_iso = QPushButton("💿 Monta ISO / IMG / NRG...")
        self.btn_mount_iso.clicked.connect(self._on_mount_iso_clicked)
        btn_row.addWidget(self.btn_mount_iso)

        self.btn_mount_bincue = QPushButton("💿 Monta BIN/CUE...")
        self.btn_mount_bincue.clicked.connect(self._on_mount_bincue_clicked)
        btn_row.addWidget(self.btn_mount_bincue)

        self.btn_mount_mdf = QPushButton("ℹ️ Info su MDF/MDS")
        self.btn_mount_mdf.clicked.connect(self._on_mdf_info_clicked)
        btn_row.addWidget(self.btn_mount_mdf)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Immagini attualmente montate:"))
        self.mounted_list = QListWidget()
        layout.addWidget(self.mounted_list, stretch=1)

        btn_row2 = QHBoxLayout()
        self.btn_open_mounted = QPushButton("Apri nel file manager")
        self.btn_open_mounted.clicked.connect(self._on_open_mounted_clicked)
        btn_row2.addWidget(self.btn_open_mounted)

        self.btn_unmount = QPushButton("⏏ Smonta selezionata")
        self.btn_unmount.clicked.connect(self._on_unmount_clicked)
        btn_row2.addWidget(self.btn_unmount)
        layout.addLayout(btn_row2)

        # --- Impostazioni chiusura e conversione ---
        settings_box = QGroupBox("Impostazioni")
        settings_layout = QVBoxLayout(settings_box)

        self.unmount_on_exit_cb = QCheckBox(
            "Smonta tutte le immagini automaticamente alla chiusura della GUI")
        self.unmount_on_exit_cb.setChecked(self.settings.get("unmount_on_exit", True))
        self.unmount_on_exit_cb.stateChanged.connect(self._save_settings_from_ui)
        settings_layout.addWidget(self.unmount_on_exit_cb)

        bchunk_row = QHBoxLayout()
        bchunk_row.addWidget(QLabel("Cartella di output per ISO convertite (BIN/CUE):"))
        self.bchunk_output_edit = QLineEdit(self.settings.get("bchunk_output_dir", ""))
        bchunk_row.addWidget(self.bchunk_output_edit)
        self.btn_browse_bchunk = QPushButton("Sfoglia...")
        self.btn_browse_bchunk.clicked.connect(self._browse_bchunk_output)
        bchunk_row.addWidget(self.btn_browse_bchunk)
        settings_layout.addLayout(bchunk_row)

        layout.addWidget(settings_box)

        return tab

    # ------------------------------------------------------------------
    # Tab Prefix Wine
    # ------------------------------------------------------------------
    def _build_prefix_tab(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # --- Colonna sinistra: lista prefix ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("Prefix registrati:"))

        self.prefix_list = QListWidget()
        self.prefix_list.itemSelectionChanged.connect(self._update_prefix_button_states)
        left_layout.addWidget(self.prefix_list, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_new_prefix = QPushButton("➕ Crea nuovo prefix")
        self.btn_new_prefix.clicked.connect(self._on_create_prefix_clicked)
        btn_row.addWidget(self.btn_new_prefix)

        self.btn_add_prefix = QPushButton("Aggiungi esistente")
        self.btn_add_prefix.clicked.connect(self._on_add_existing_prefix_clicked)
        btn_row.addWidget(self.btn_add_prefix)
        left_layout.addLayout(btn_row)

        self.btn_remove_prefix = QPushButton("Rimuovi dalla lista")
        self.btn_remove_prefix.clicked.connect(self._on_remove_prefix_clicked)
        left_layout.addWidget(self.btn_remove_prefix)

        splitter.addWidget(left_widget)

        # --- Colonna destra: gestione del prefix selezionato ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Sezione strumenti scorrevole (version, tools, winetricks, dgvoodoo)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        version_box = QGroupBox("Versione Windows")
        version_layout = QHBoxLayout(version_box)
        self.windows_version_combo = QComboBox()
        for label, _ in WINDOWS_VERSIONS:
            self.windows_version_combo.addItem(label)
        version_layout.addWidget(self.windows_version_combo)

        self.btn_apply_version = QPushButton("Applica")
        self.btn_apply_version.clicked.connect(self._on_apply_windows_version)
        version_layout.addWidget(self.btn_apply_version)
        scroll_layout.addWidget(version_box)

        tools_box = QGroupBox("Strumenti Wine")
        tools_layout = QVBoxLayout(tools_box)

        tools_note = QLabel(
            "⚠️ winecfg e regedit girano FUORI dalla sandbox (rete/home/Z: come sul sistema reale) "
            "perché sono tool Wine fidati, non il gioco.\n"
            "La creazione del prefix (wineboot) usa wine-sandbox --init con sandbox attiva "
            "(rete disabilitata, home nascosta).\n"
            "Le protezioni si applicano a ▶ Gioca, 📦 Installa e ➕ Crea prefix."
        )
        tools_note.setWordWrap(True)
        tools_layout.addWidget(tools_note)

        tools_btn_row = QHBoxLayout()
        self.btn_open_winecfg = QPushButton("🛠 Apri winecfg (interfaccia completa)")
        self.btn_open_winecfg.clicked.connect(self._on_open_winecfg)
        tools_btn_row.addWidget(self.btn_open_winecfg)

        self.btn_open_regedit = QPushButton("Apri regedit")
        self.btn_open_regedit.clicked.connect(self._on_open_regedit)
        tools_btn_row.addWidget(self.btn_open_regedit)
        tools_layout.addLayout(tools_btn_row)

        scroll_layout.addWidget(tools_box)

        winetricks_box = QGroupBox(
            "Installa dipendenze (winetricks - ATTENZIONE: rete abilitata per il download)")
        winetricks_layout = QVBoxLayout(winetricks_box)

        winetricks_layout.addWidget(QLabel("Runtime e librerie:"))
        runtime_grid = QGridLayout()
        for i, verb in enumerate(RUNTIME_WINETRICKS_VERBS):
            cb = QCheckBox(verb)
            self.winetricks_checkboxes[verb] = cb
            runtime_grid.addWidget(cb, i // 3, i % 3)
        winetricks_layout.addLayout(runtime_grid)

        winetricks_layout.addWidget(QLabel("Codec audio/video legacy (per filmati/musica in-game):"))
        codec_grid = QGridLayout()
        for i, verb in enumerate(CODEC_WINETRICKS_VERBS):
            cb = QCheckBox(verb)
            self.winetricks_checkboxes[verb] = cb
            codec_grid.addWidget(cb, i // 3, i % 3)
        winetricks_layout.addLayout(codec_grid)

        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Altri verbi (separati da spazio):"))
        self.winetricks_custom_edit = QLineEdit()
        custom_row.addWidget(self.winetricks_custom_edit)
        winetricks_layout.addLayout(custom_row)

        self.btn_install_deps = QPushButton("Installa dipendenze selezionate")
        self.btn_install_deps.clicked.connect(self._on_install_dependencies)
        winetricks_layout.addWidget(self.btn_install_deps)

        scroll_layout.addWidget(winetricks_box)

        dgvoodoo_box = QGroupBox("Compatibilità grafica legacy (DirectX 1-9 / Glide)")
        dgvoodoo_layout = QVBoxLayout(dgvoodoo_box)
        dgvoodoo_info = QLabel(
            "dgVoodoo2 traduce DirectX/Direct3D 1-9 e Glide (3dfx) verso D3D11/12 moderno. "
            "Utile per giochi con rendering rotto o instabile sotto Wine (in particolare DX7/8). "
            "Va scaricato e copiato nella cartella del gioco (accanto all'eseguibile), non nel prefix."
        )
        dgvoodoo_info.setWordWrap(True)
        dgvoodoo_layout.addWidget(dgvoodoo_info)

        self.btn_download_dgvoodoo = QPushButton("⬇ Scarica e installa dgVoodoo2 in una cartella di gioco...")
        self.btn_download_dgvoodoo.clicked.connect(self._on_download_dgvoodoo)
        dgvoodoo_layout.addWidget(self.btn_download_dgvoodoo)

        scroll_layout.addWidget(dgvoodoo_box)
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        right_layout.addWidget(scroll, stretch=1)

        # Log fisso in fondo, sempre visibile
        right_layout.addWidget(QLabel("Log strumenti Wine:"))
        self.wine_tool_log = QPlainTextEdit()
        self.wine_tool_log.setReadOnly(True)
        self.wine_tool_log.setStyleSheet("font-family: monospace; font-size: 10pt;")
        self.wine_tool_log.setMaximumHeight(160)
        right_layout.addWidget(self.wine_tool_log)

        splitter.addWidget(right_widget)
        splitter.setSizes([320, 730])

        self._update_prefix_button_states()
        return tab

    # ------------------------------------------------------------------
    # Tab Sistema
    # ------------------------------------------------------------------
    def _build_system_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        deps_box = QGroupBox("Dipendenze di sistema")
        deps_layout = QVBoxLayout(deps_box)

        self.deps_status_list = QListWidget()
        deps_layout.addWidget(self.deps_status_list)

        self.btn_check_deps = QPushButton("🔍 Verifica dipendenze")
        self.btn_check_deps.clicked.connect(self._on_check_dependencies)
        deps_layout.addWidget(self.btn_check_deps)

        layout.addWidget(deps_box)

        launcher_box = QGroupBox("Integrazione desktop")
        launcher_layout = QVBoxLayout(launcher_box)
        launcher_info = QLabel(
            "Crea una voce nel menu applicazioni KDE per lanciare questa GUI "
            "senza passare dal terminale. Disattivato di default per non "
            "intasare il menu applicazioni con voci non richieste."
        )
        launcher_info.setWordWrap(True)
        launcher_layout.addWidget(launcher_info)

        self.enable_launcher_creation_checkbox = QCheckBox(
            "Abilita la creazione del lanciatore desktop (disattivato di default)")
        self.enable_launcher_creation_checkbox.setChecked(False)
        self.enable_launcher_creation_checkbox.stateChanged.connect(self._on_launcher_checkbox_changed)
        launcher_layout.addWidget(self.enable_launcher_creation_checkbox)

        self.btn_create_launcher = QPushButton("🖥 Crea lanciatore nel menu applicazioni")
        self.btn_create_launcher.clicked.connect(self._on_create_desktop_launcher)
        self.btn_create_launcher.setEnabled(False)
        launcher_layout.addWidget(self.btn_create_launcher)

        layout.addWidget(launcher_box)

        backup_box = QGroupBox("Backup prefix")
        backup_layout = QVBoxLayout(backup_box)
        backup_info = QLabel(
            "Comprime un prefix registrato in un archivio .tar.zst, utile per "
            "ripristinarlo velocemente se qualcosa si rompe."
        )
        backup_info.setWordWrap(True)
        backup_layout.addWidget(backup_info)

        self.btn_backup_prefix = QPushButton("💾 Backup di un prefix registrato...")
        self.btn_backup_prefix.clicked.connect(self._on_backup_prefix)
        backup_layout.addWidget(self.btn_backup_prefix)

        layout.addWidget(backup_box)

        config_box = QGroupBox("Backup/ripristino configurazione GUI")
        config_layout = QVBoxLayout(config_box)
        config_info = QLabel(
            "Esporta o importa l'intera configurazione (libreria giochi, prefix registrati, "
            "impostazioni) in un unico file, utile per un altro PC o dopo una reinstallazione."
        )
        config_info.setWordWrap(True)
        config_layout.addWidget(config_info)

        config_btn_row = QHBoxLayout()
        self.btn_export_config = QPushButton("⬆ Esporta configurazione...")
        self.btn_export_config.clicked.connect(self._on_export_config)
        config_btn_row.addWidget(self.btn_export_config)

        self.btn_import_config = QPushButton("⬇ Importa configurazione...")
        self.btn_import_config.clicked.connect(self._on_import_config)
        config_btn_row.addWidget(self.btn_import_config)
        config_layout.addLayout(config_btn_row)

        layout.addWidget(config_box)

        layout.addWidget(QLabel("Log operazioni di sistema:"))
        self.system_log = QPlainTextEdit()
        self.system_log.setReadOnly(True)
        self.system_log.setStyleSheet("font-family: monospace; font-size: 10pt;")
        layout.addWidget(self.system_log, stretch=1)

        return tab

    def _system_log(self, text):
        self.system_log.appendPlainText(text)

    def _update_launcher_button_state(self):
        self.btn_create_launcher.setEnabled(self.enable_launcher_creation_checkbox.isChecked())

    def _on_launcher_checkbox_changed(self):
        self._update_launcher_button_state()
        self._save_settings_from_ui()

    def _on_check_dependencies(self):
        self.deps_status_list.clear()
        for tool, description in REQUIRED_TOOLS:
            found_path = shutil.which(tool)
            status = "✅ trovato" if found_path else "❌ MANCANTE"
            location = f" ({found_path})" if found_path else ""
            item = QListWidgetItem(f"{status}  —  {tool}: {description}{location}")
            self.deps_status_list.addItem(item)

        missing = [tool for tool, _ in REQUIRED_TOOLS if not shutil.which(tool)]
        if missing:
            self._system_log(
                f"Mancano: {', '.join(missing)}. Su CachyOS/Arch installa con:\n"
                f"sudo pacman -S {' '.join(missing)}"
            )
        else:
            self._system_log("Tutte le dipendenze richieste sono presenti.")

    def _on_create_desktop_launcher(self):
        script_path = os.path.abspath(sys.argv[0])
        python_path = sys.executable

        applications_dir = Path.home() / ".local" / "share" / "applications"
        applications_dir.mkdir(parents=True, exist_ok=True)
        desktop_file = applications_dir / "wine-sandbox-gui.desktop"

        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Wine Sandbox - Libreria giochi\n"
            "Comment=Gestisci ed esegui giochi abandonware in sandbox isolata\n"
            f"Exec={python_path} {script_path}\n"
            "Icon=applications-games\n"
            "Categories=Game;Utility;\n"
            "Terminal=false\n"
        )
        try:
            desktop_file.write_text(content)
            os.chmod(desktop_file, 0o755)
            self._system_log(f"Lanciatore creato: {desktop_file}")

            if shutil.which("update-desktop-database"):
                subprocess.run(
                    ["update-desktop-database", str(applications_dir)],
                    capture_output=True, text=True
                )
                self._system_log("Database delle applicazioni aggiornato.")

            QMessageBox.information(
                self, "Lanciatore creato",
                "Dovresti trovare 'Wine Sandbox - Libreria giochi' nel menu applicazioni KDE "
                "a breve (potrebbe servire un aggiornamento della cache del menu)."
            )
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Impossibile creare il lanciatore: {e}")

    def _on_backup_prefix(self):
        if not self.prefixes:
            QMessageBox.information(self, "Nessun prefix registrato",
                                     "Registra prima un prefix nella tab 'Prefix Wine'.")
            return

        names = [p["name"] for p in self.prefixes]
        choice, ok = QInputDialog.getItem(
            self, "Backup prefix", "Scegli il prefix da comprimere:", names, 0, False)
        if not ok:
            return
        entry = next(p for p in self.prefixes if p["name"] == choice)

        dest_dir = QFileDialog.getExistingDirectory(
            self, "Cartella di destinazione del backup", self.settings["prefix_root"])
        if not dest_dir:
            return

        use_zstd = shutil.which("zstd") is not None
        ext = "tar.zst" if use_zstd else "tar.gz"
        safe_name = re.sub(r"[^\w\-]", "_", entry["name"])
        dest_file = os.path.join(dest_dir, f"{safe_name}-backup.{ext}")

        prefix_parent = os.path.dirname(entry["path"].rstrip("/"))
        prefix_basename = os.path.basename(entry["path"].rstrip("/"))

        if use_zstd:
            args = ["tar", "--zstd", "-cf", dest_file, "-C", prefix_parent, prefix_basename]
        else:
            args = ["tar", "-czf", dest_file, "-C", prefix_parent, prefix_basename]

        self._system_log(f"Avvio backup di '{entry['name']}' verso: {dest_file}")
        self._system_log(f"$ {' '.join(args)}")

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self._system_log(proc.readAllStandardOutput().data().decode(errors="replace")))
        proc.finished.connect(
            lambda code, status: self._system_log(
                f"Backup completato con codice {code}: {dest_file}" if code == 0
                else f"Backup fallito con codice {code}"))
        proc.start(args[0], args[1:])
        # manteniamo un riferimento per evitare garbage collection prematura
        self._backup_process = proc

    def _on_export_config(self):
        import zipfile

        dest_file, _ = QFileDialog.getSaveFileName(
            self, "Esporta configurazione", str(Path.home() / "wine-sandbox-gui-config.zip"),
            "Archivio ZIP (*.zip)")
        if not dest_file:
            return
        if not dest_file.endswith(".zip"):
            dest_file += ".zip"

        try:
            with zipfile.ZipFile(dest_file, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in (GAMES_FILE, SETTINGS_FILE, PREFIXES_FILE):
                    if f.exists():
                        zf.write(f, arcname=f.name)
            self._system_log(f"Configurazione esportata in: {dest_file}")
            QMessageBox.information(self, "Esportazione completata",
                                     f"Configurazione salvata in:\n{dest_file}")
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Esportazione fallita: {e}")

    def _on_import_config(self):
        import zipfile

        src_file, _ = QFileDialog.getOpenFileName(
            self, "Importa configurazione", str(Path.home()), "Archivio ZIP (*.zip)")
        if not src_file:
            return

        confirm = QMessageBox.question(
            self, "Sovrascrivere la configurazione attuale?",
            "L'importazione sovrascriverà libreria giochi, prefix registrati e impostazioni "
            "attuali. Continuare?"
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            with zipfile.ZipFile(src_file, "r") as zf:
                zf.extractall(CONFIG_DIR)

            self.games = load_json(GAMES_FILE, [])
            self.prefixes = load_json(PREFIXES_FILE, [])
            self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}

            self._refresh_game_list()
            self._refresh_prefix_list()
            self.wine_sandbox_path_edit.setText(self.settings["wine_sandbox_path"])
            self.prefix_root_edit.setText(self.settings["prefix_root"])
            self.games_root_edit.setText(self.settings["games_root"])
            self._load_security_settings_into_ui()

            self._system_log(f"Configurazione importata da: {src_file}")
            QMessageBox.information(self, "Importazione completata",
                                     "Configurazione ripristinata con successo.")
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Importazione fallita: {e}")

    # ------------------------------------------------------------------
    # Impostazioni
    # ------------------------------------------------------------------
    def _save_settings_from_ui(self):
        self.settings["wine_sandbox_path"] = self.wine_sandbox_path_edit.text().strip() or FALLBACK_WINE_SANDBOX_PATH
        self.settings["prefix_root"] = self.prefix_root_edit.text().strip() or FALLBACK_PREFIX_ROOT
        self.settings["games_root"] = self.games_root_edit.text().strip() or FALLBACK_GAMES_ROOT
        self.settings["sec_hide_home"] = self.sec_hide_home.isChecked()
        self.settings["sec_cap_drop"] = self.sec_cap_drop.isChecked()
        self.settings["sec_unshare_pid"] = self.sec_unshare_pid.isChecked()
        self.settings["sec_dri"] = self.sec_dri.isChecked()
        self.settings["sec_audio"] = self.sec_audio.isChecked()
        self.settings["sec_loopback"] = self.sec_loopback.isChecked()
        self.settings["sec_allow_network"] = self.sec_allow_network.isChecked()
        self.settings["sec_disable_zdrive"] = self.sec_disable_zdrive.isChecked()
        self.settings["sec_exe_rw"] = self.sec_exe_rw.isChecked()
        self.settings["sec_verify_integrity"] = self.sec_verify_integrity.isChecked()
        self.settings["sec_resource_limits"] = self.sec_resource_limits.isChecked()
        self.settings["sec_memory_limit"] = self.sec_memory_limit_edit.text().strip() or "2G"
        self.settings["sec_cpu_limit"] = self.sec_cpu_limit_edit.text().strip() or "200"
        self.settings["enable_desktop_launcher_creation"] = self.enable_launcher_creation_checkbox.isChecked()
        self.settings["unmount_on_exit"] = self.unmount_on_exit_cb.isChecked()
        self.settings["bchunk_output_dir"] = self.bchunk_output_edit.text().strip()
        save_json(SETTINGS_FILE, self.settings)

    def _load_security_settings_into_ui(self):
        self.sec_hide_home.setChecked(self.settings.get("sec_hide_home", True))
        self.sec_cap_drop.setChecked(self.settings.get("sec_cap_drop", True))
        self.sec_unshare_pid.setChecked(self.settings.get("sec_unshare_pid", True))
        self.sec_dri.setChecked(self.settings.get("sec_dri", True))
        self.sec_audio.setChecked(self.settings.get("sec_audio", True))
        self.sec_loopback.setChecked(self.settings.get("sec_loopback", False))
        self.sec_allow_network.setChecked(self.settings.get("sec_allow_network", False))
        self.sec_disable_zdrive.setChecked(self.settings.get("sec_disable_zdrive", True))
        self.sec_exe_rw.setChecked(self.settings.get("sec_exe_rw", False))
        self.sec_verify_integrity.setChecked(self.settings.get("sec_verify_integrity", True))
        self.sec_resource_limits.setChecked(self.settings.get("sec_resource_limits", False))
        self.sec_memory_limit_edit.setText(self.settings.get("sec_memory_limit", "2G"))
        self.sec_cpu_limit_edit.setText(self.settings.get("sec_cpu_limit", "200"))
        self.enable_launcher_creation_checkbox.setChecked(
            self.settings.get("enable_desktop_launcher_creation", False))
        self._update_launcher_button_state()

    def _on_network_toggle_changed(self):
        if self.sec_allow_network.isChecked():
            reply = QMessageBox.warning(
                self, "Disattivare l'isolamento di rete?",
                "Stai per disattivare la protezione principale della sandbox: il gioco/installer "
                "avrà accesso a internet come un programma normale.\n\n"
                "Usalo solo se sei certo che serve (es. un installer con attivazione online) "
                "e ti fidi della fonte del file.\n\nConfermi?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                self.sec_allow_network.blockSignals(True)
                self.sec_allow_network.setChecked(False)
                self.sec_allow_network.blockSignals(False)
                return
        self._save_settings_from_ui()

    def _sandbox_env(self, overrides=None):
        """Costruisce l'ambiente QProcess con i toggle di sicurezza correnti."""
        overrides = overrides or {}
        env = QProcessEnvironment.systemEnvironment()

        def _val(key, checkbox):
            return overrides.get(key) or ("1" if checkbox.isChecked() else "0")

        env.insert("SANDBOX_HIDE_HOME", _val("SANDBOX_HIDE_HOME", self.sec_hide_home))
        env.insert("SANDBOX_CAP_DROP", _val("SANDBOX_CAP_DROP", self.sec_cap_drop))
        env.insert("SANDBOX_UNSHARE_PID", _val("SANDBOX_UNSHARE_PID", self.sec_unshare_pid))
        env.insert("SANDBOX_DRI", _val("SANDBOX_DRI", self.sec_dri))
        env.insert("SANDBOX_AUDIO", _val("SANDBOX_AUDIO", self.sec_audio))
        env.insert("SANDBOX_ALLOW_LOOPBACK", _val("SANDBOX_ALLOW_LOOPBACK", self.sec_loopback))
        env.insert("SANDBOX_ALLOW_NETWORK", _val("SANDBOX_ALLOW_NETWORK", self.sec_allow_network))
        env.insert("SANDBOX_DISABLE_ZDRIVE", _val("SANDBOX_DISABLE_ZDRIVE", self.sec_disable_zdrive))
        env.insert("SANDBOX_EXE_RW", _val("SANDBOX_EXE_RW", self.sec_exe_rw))
        return env

    def _browse_bchunk_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella di output per le ISO convertite",
            self.settings.get("bchunk_output_dir", "") or self.settings["games_root"])
        if path:
            self.bchunk_output_edit.setText(path)
            self._save_settings_from_ui()

    def _browse_wine_sandbox(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Seleziona lo script wine-sandbox",
            os.path.dirname(self.settings.get("wine_sandbox_path", "")) or str(Path.home()),
            "Script shell (*.sh *.bash);;Tutti i file (*)")
        if path:
            self.wine_sandbox_path_edit.setText(path)
            self._save_settings_from_ui()

    def _browse_prefix_root(self):
        path = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella predefinita dei prefix", self.settings["prefix_root"])
        if path:
            self.prefix_root_edit.setText(path)
            self._save_settings_from_ui()

    def _browse_games_root(self):
        path = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella predefinita dei giochi/ISO", self.settings["games_root"])
        if path:
            self.games_root_edit.setText(path)
            self._save_settings_from_ui()

    # ------------------------------------------------------------------
    # Utility lista giochi
    # ------------------------------------------------------------------
    def _update_button_states(self):
        has_selection = self.game_list.currentItem() is not None
        self.btn_play.setEnabled(has_selection)
        self.btn_remove.setEnabled(has_selection)

    def _refresh_game_list(self):
        self.game_list.clear()
        for game in self.games:
            item = QListWidgetItem(game["name"])
            item.setData(Qt.UserRole, game)
            self.game_list.addItem(item)
        self._update_button_states()

    # ------------------------------------------------------------------
    # Log ed esecuzione wine-sandbox (asincrono, QProcess)
    # ------------------------------------------------------------------
    def _log(self, text):
        self.log_output.appendPlainText(text)

    def _wine_sandbox_path(self):
        return self.settings.get("wine_sandbox_path") or "wine-sandbox"

    def _wine_sandbox_launch_cmd(self, sandbox_args):
        """Ritorna (program, args) per lanciare wine-sandbox.
        Se lo script non ha il bit di esecuzione, lo wrapping con bash."""
        wine_sandbox = self._wine_sandbox_path()
        if os.path.isfile(wine_sandbox) and not os.access(wine_sandbox, os.X_OK):
            return "bash", [wine_sandbox] + sandbox_args
        return wine_sandbox, sandbox_args

    # ---- Log persistente delle esecuzioni (audit trail) ----
    def _log_launch_history(self, prefix, exe, toggles):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "prefix": prefix,
            "eseguibile": exe,
            "toggle_sicurezza": toggles,
        }
        with open(LAUNCH_HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _current_security_toggles(self):
        return {
            "hide_home": self.sec_hide_home.isChecked(),
            "cap_drop": self.sec_cap_drop.isChecked(),
            "unshare_pid": self.sec_unshare_pid.isChecked(),
            "dri": self.sec_dri.isChecked(),
            "audio": self.sec_audio.isChecked(),
            "loopback": self.sec_loopback.isChecked(),
            "allow_network": self.sec_allow_network.isChecked(),
            "disable_zdrive": self.sec_disable_zdrive.isChecked(),
            "exe_rw": self.sec_exe_rw.isChecked(),
            "resource_limits": self.sec_resource_limits.isChecked(),
        }

    # ---- Verifica integrità prefix (snapshot prima/dopo) ----
    def _snapshot_prefix(self, prefix_path):
        """Cattura (percorso relativo -> dimensione, mtime) per drive_c.
        Non calcola hash del contenuto per restare veloce anche su prefix grandi."""
        drive_c = os.path.join(prefix_path, "drive_c")
        snapshot = {}
        if not os.path.isdir(drive_c):
            return snapshot
        for root, _dirs, files in os.walk(drive_c):
            for fname in files:
                full = os.path.join(root, fname)
                try:
                    st = os.stat(full)
                    rel = os.path.relpath(full, drive_c)
                    snapshot[rel] = (st.st_size, int(st.st_mtime))
                except OSError:
                    continue
        return snapshot

    def _diff_and_report_integrity(self, prefix_path, before_snapshot, context_label):
        after_snapshot = self._snapshot_prefix(prefix_path)
        before_keys = set(before_snapshot.keys())
        after_keys = set(after_snapshot.keys())

        added = sorted(after_keys - before_keys)
        removed = sorted(before_keys - after_keys)
        modified = sorted(
            k for k in (before_keys & after_keys) if before_snapshot[k] != after_snapshot[k]
        )

        MAX_LIST = 25
        self._log(f"\n== Verifica integrità prefix ({context_label}) ==")
        self._log(f"File nuovi: {len(added)}  |  Modificati: {len(modified)}  |  Rimossi: {len(removed)}")

        if context_label == "gioco" and (len(added) > 50 or len(modified) > 100):
            self._log(
                "ATTENZIONE: un numero elevato di modifiche durante la sola esecuzione del gioco "
                "(non un'installazione) può essere normale per salvataggi/log, ma vale la pena "
                "controllare l'elenco sotto se non te lo aspettavi."
            )

        for label, items in (("Nuovi", added), ("Modificati", modified), ("Rimossi", removed)):
            if items:
                self._log(f"-- {label} (primi {min(len(items), MAX_LIST)} di {len(items)}) --")
                for item in items[:MAX_LIST]:
                    self._log(f"   {item}")

    # ---- Limiti di risorse (systemd-run) ----
    def _build_launch_argv(self, wine_sandbox, args, env_dict):
        """Ritorna (programma, argomenti) applicando systemd-run se richiesto."""
        if not self.sec_resource_limits.isChecked():
            return wine_sandbox, args

        if not shutil.which("systemd-run"):
            self._log("ATTENZIONE: 'systemd-run' non trovato, limiti di risorse ignorati per questa esecuzione.")
            return wine_sandbox, args

        mem = self.sec_memory_limit_edit.text().strip() or "2G"
        cpu = self.sec_cpu_limit_edit.text().strip() or "200"

        systemd_args = ["--user", "--scope", "-p", f"MemoryMax={mem}", "-p", f"CPUQuota={cpu}%"]
        for key, value in env_dict.items():
            systemd_args += ["--setenv", f"{key}={value}"]
        systemd_args += ["--", wine_sandbox] + args
        return "systemd-run", systemd_args

    def _run_process(self, args, on_finished=None, env_overrides=None):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Processo in corso",
                                 "C'è già un'operazione in esecuzione. Attendi che finisca.")
            return

        wine_program, wine_args = self._wine_sandbox_launch_cmd(args)
        # Salta --install/--init/--setup per estrarre prefix/exe
        offset = 1 if args and args[0] in ("--install", "--init", "--setup") else 0
        prefix_path = args[offset] if len(args) > offset else None
        exe_path = args[offset + 1] if len(args) > offset + 1 else None

        env = self._sandbox_env(env_overrides)
        env_dict = {key: env.value(key) for key in env.keys() if key.startswith("SANDBOX_")}

        program, full_args = self._build_launch_argv(wine_program, wine_args, env_dict)

        self._log(f"\n$ {program} {' '.join(full_args)}\n")

        if exe_path and prefix_path:
            self._log_launch_history(prefix_path, exe_path, self._current_security_toggles())

        pre_snapshot = {}
        do_integrity_check = self.sec_verify_integrity.isChecked() and prefix_path and os.path.isdir(prefix_path)
        if do_integrity_check:
            pre_snapshot = self._snapshot_prefix(prefix_path)

        self.process = QProcess(self)
        self.process.setProcessEnvironment(env)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_process_output)
        self.process.errorOccurred.connect(
            lambda err: self._log(
                f"\n[ERRORE QProcess: {self.process.errorString()}]\n"))

        context_label = "installazione" if (exe_path and "setup" in os.path.basename(exe_path).lower()) else "gioco"

        if on_finished:
            self.process.finished.connect(lambda code, status: on_finished(code))
        if do_integrity_check:
            self.process.finished.connect(
                lambda code, status: self._diff_and_report_integrity(prefix_path, pre_snapshot, context_label))
        self.process.finished.connect(lambda code, status: self._log(
            f"\n[processo terminato con codice {code}]\n"))

        self.process.start(program, full_args)

    def _on_process_output(self):
        data = self.process.readAllStandardOutput().data().decode(errors="replace")
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        self.log_output.insertPlainText(data)

    # ------------------------------------------------------------------
    # Azioni tab Giochi
    # ------------------------------------------------------------------
    def _on_play_clicked(self):
        item = self.game_list.currentItem()
        if not item:
            return
        game = item.data(Qt.UserRole)
        prefix = game["prefix"]
        exe = game["exe"]

        if not os.path.isdir(prefix):
            QMessageBox.critical(self, "Errore", f"Il prefix non esiste più:\n{prefix}")
            return
        if not os.path.isfile(exe):
            QMessageBox.critical(self, "Errore", f"L'eseguibile non esiste più:\n{exe}")
            return

        self._run_process([prefix, exe])

    def _on_remove_clicked(self):
        item = self.game_list.currentItem()
        if not item:
            return
        game = item.data(Qt.UserRole)
        confirm = QMessageBox.question(
            self, "Conferma rimozione",
            f"Rimuovere '{game['name']}' dalla lista?\n"
            "(Non elimina i file del gioco né il prefix, solo la voce dalla libreria)"
        )
        if confirm == QMessageBox.Yes:
            self.games = [g for g in self.games if g != game]
            save_json(GAMES_FILE, self.games)
            self._refresh_game_list()

    def _on_open_game_folder(self):
        item = self.game_list.currentItem()
        if not item:
            QMessageBox.information(self, "Nessuna selezione", "Seleziona prima un gioco dalla lista.")
            return
        game = item.data(Qt.UserRole)
        folder = os.path.dirname(game["exe"])
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.critical(self, "Errore", f"Cartella non trovata:\n{folder}")

    def _on_open_game_prefix_folder(self):
        item = self.game_list.currentItem()
        if not item:
            QMessageBox.information(self, "Nessuna selezione", "Seleziona prima un gioco dalla lista.")
            return
        game = item.data(Qt.UserRole)
        if os.path.isdir(game["prefix"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(game["prefix"]))
        else:
            QMessageBox.critical(self, "Errore", f"Prefix non trovato:\n{game['prefix']}")

    def _on_add_existing_clicked(self):
        prefix = self._choose_prefix_path("Prefix del gioco")
        if not prefix:
            return

        exe, _ = QFileDialog.getOpenFileName(
            self, "Seleziona l'eseguibile del gioco già installato",
            os.path.join(prefix, "drive_c"), "Eseguibili Windows (*.exe *.EXE)")
        if not exe:
            return

        name, ok = QInputDialog.getText(self, "Nome gioco", "Nome da mostrare nella lista:")
        if not ok or not name.strip():
            return

        self.games.append({"name": name.strip(), "prefix": prefix, "exe": exe})
        save_json(GAMES_FILE, self.games)
        self._refresh_game_list()
        self._log(f"Aggiunto '{name.strip()}' alla libreria.")

    def _on_install_clicked(self):
        prefix = self._choose_prefix_path("Prefix per l'installazione")
        if not prefix:
            return

        setup_exe, _ = QFileDialog.getOpenFileName(
            self, "Seleziona il file di installazione (setup.exe)",
            self.settings["games_root"], "Eseguibili Windows (*.exe *.EXE)")
        if not setup_exe:
            return

        self._log(f"Avvio installazione da: {setup_exe}")
        self._log("Al termine del setup, chiudi la finestra dell'installer per continuare.")

        # Avviso di sicurezza: spiegazione delle misure adottate
        QMessageBox.information(
            self, "Installazione sandboxed",
            "L'installer verrà eseguito in sandbox con le seguenti protezioni:\n\n"
            "• Rete: DISABILITATA (nessuna connessione internet)\n"
            "• Home: nascosta (tmpfs vuota)\n"
            "• Filesystem: SOLO LETTURA (/usr, /etc, /lib, /run/media)\n"
            "• Capability Linux: droppate (nessun accesso privilegiato)\n"
            "• PID isolati (l'installer non vede altri processi)\n"
            "• Z: temporaneamente visibile in sola lettura (per leggere il setup.exe)\n"
            "• Scrittura permessa SOLO dentro il prefix Wine\n\n"
            "Chiudi l'installer al termine per continuare."
        )

        def after_install(exit_code):
            reply = QMessageBox.question(
                self, "Installazione completata",
                "Il setup è terminato. Vuoi selezionare ora l'eseguibile del gioco "
                "appena installato per aggiungerlo alla libreria?"
            )
            if reply == QMessageBox.Yes:
                exe, _ = QFileDialog.getOpenFileName(
                    self, "Seleziona l'eseguibile del gioco",
                    os.path.join(prefix, "drive_c"), "Eseguibili Windows (*.exe *.EXE)")
                if exe:
                    name, ok = QInputDialog.getText(
                        self, "Nome gioco", "Nome da mostrare nella lista:")
                    if ok and name.strip():
                        self.games.append({"name": name.strip(), "prefix": prefix, "exe": exe})
                        save_json(GAMES_FILE, self.games)
                        self._refresh_game_list()

        # Modalità --install: Z: abilitata in sola lettura, EXE_DIR read-only,
        # tutto il filesystem in sola lettura, scrittura solo nel prefix.
        self._run_process(["--install", prefix, setup_exe], on_finished=after_install)

    # ------------------------------------------------------------------
    # Azioni tab Montaggio immagini
    # ------------------------------------------------------------------
    def _refresh_mounted_list(self):
        self.mounted_list.clear()
        for entry in self.mounted_images:
            label = f"{os.path.basename(entry['path'])}  →  {entry['mount_point']}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry)
            self.mounted_list.addItem(item)

    def _on_mount_iso_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Seleziona ISO/IMG/NRG da montare",
            self.settings["games_root"],
            "Immagini ottiche (*.iso *.img *.nrg *.ISO *.IMG *.NRG)")
        if not path:
            return
        self._mount_via_udisks(path)

    def _on_mount_bincue_clicked(self):
        cue_path, _ = QFileDialog.getOpenFileName(
            self, "Seleziona il file .cue corrispondente al .bin",
            self.settings["games_root"], "File CUE (*.cue *.CUE)")
        if not cue_path:
            return

        bin_path = re.sub(r"\.cue$", ".bin", cue_path, flags=re.IGNORECASE)
        if not os.path.isfile(bin_path):
            QMessageBox.critical(
                self, "File .bin non trovato",
                f"Non trovo il file .bin corrispondente:\n{bin_path}\n"
                "Assicurati che .bin e .cue abbiano lo stesso nome e siano nella stessa cartella."
            )
            return

        if not shutil.which("bchunk"):
            QMessageBox.critical(
                self, "bchunk non installato",
                "Serve il pacchetto 'bchunk' per convertire BIN/CUE in ISO.\n"
                "Installa con: sudo pacman -S bchunk"
            )
            return

        self._log(f"Conversione BIN/CUE in ISO tramite bchunk: {bin_path}")

        # Usa la cartella di output configurata, o una temporanea se non impostata
        output_dir = self.settings.get("bchunk_output_dir", "").strip()
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            base_name = Path(bin_path).stem
            output_prefix = os.path.join(output_dir, base_name)
        else:
            tmp_dir = tempfile.mkdtemp(prefix="wine-sandbox-gui-bincue-")
            output_prefix = os.path.join(tmp_dir, "track")

        try:
            result = subprocess.run(
                ["bchunk", "-v", bin_path, cue_path, output_prefix],
                capture_output=True, text=True
            )
            self._log(result.stdout)
            if result.returncode != 0:
                self._log(result.stderr)
                QMessageBox.critical(self, "Errore bchunk", "La conversione BIN/CUE è fallita. Vedi il log.")
                return

            iso_candidates = sorted(Path(output_prefix).parent.glob(Path(output_prefix).name + "*.iso"))
            if not iso_candidates:
                QMessageBox.critical(
                    self, "Nessuna ISO generata",
                    "bchunk non ha prodotto alcun file .iso. Il bin/cue potrebbe non contenere "
                    "una traccia dati standard (es. CD audio puro)."
                )
                return

            first_iso = str(iso_candidates[0])
            self._log(f"ISO generata: {first_iso}")
            if output_dir:
                self._log(f"ISO conservata in: {output_dir}")
            self._mount_via_udisks(first_iso)

        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante la conversione: {e}")

    def _on_mdf_info_clicked(self):
        QMessageBox.information(
            self, "Formato MDF/MDS non supportato automaticamente",
            "I file .mdf/.mds sono nel formato proprietario di Alcohol 120% e non hanno "
            "un supporto diretto e affidabile su Linux.\n\n"
            "Opzioni possibili:\n"
            "- Cerca un tool di conversione mdf2iso (non incluso in questa GUI)\n"
            "- Verifica se il file ha in realtà una struttura ISO9660 semplice: prova a "
            "rinominarlo in .iso e monta con il pulsante 'Monta ISO/IMG/NRG'\n"
            "- Se disponibile, riscarica il gioco in formato ISO o BIN/CUE invece che MDF"
        )

    def _mount_via_udisks(self, path):
        if not shutil.which("udisksctl"):
            QMessageBox.critical(
                self, "udisksctl non trovato",
                "Il comando 'udisksctl' non è disponibile. Su CachyOS/KDE dovrebbe essere "
                "già installato come parte di udisks2; verifica con: sudo pacman -S udisks2"
            )
            return

        try:
            self._log(f"Creo loop device per: {path}")
            loop_result = subprocess.run(
                ["udisksctl", "loop-setup", "-f", path],
                capture_output=True, text=True
            )
            self._log(loop_result.stdout.strip())
            if loop_result.returncode != 0:
                self._log(loop_result.stderr.strip())
                QMessageBox.critical(self, "Errore", "Impossibile creare il loop device. Vedi il log.")
                return

            match = re.search(r"as (/dev/loop\d+)", loop_result.stdout)
            if not match:
                QMessageBox.critical(self, "Errore", "Loop device creato ma non riesco a individuarne il percorso.")
                return
            device = match.group(1)

            mount_result = subprocess.run(
                ["udisksctl", "mount", "-b", device],
                capture_output=True, text=True
            )
            self._log(mount_result.stdout.strip())
            if mount_result.returncode != 0:
                self._log(mount_result.stderr.strip())
                QMessageBox.critical(self, "Errore", "Impossibile montare il loop device. Vedi il log.")
                return

            mount_match = re.search(r"at (.+)\.?$", mount_result.stdout.strip())
            mount_point = mount_match.group(1).rstrip(".") if mount_match else "(sconosciuto)"

            self.mounted_images.append({"device": device, "path": path, "mount_point": mount_point})
            self._refresh_mounted_list()
            self._log(f"Montato con successo su: {mount_point}")

        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante il montaggio: {e}")

    def _on_open_mounted_clicked(self):
        item = self.mounted_list.currentItem()
        if not item:
            QMessageBox.information(self, "Nessuna selezione", "Seleziona prima un'immagine montata dalla lista.")
            return
        entry = item.data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(entry["mount_point"]))

    def _on_unmount_clicked(self):
        item = self.mounted_list.currentItem()
        if not item:
            QMessageBox.information(self, "Nessuna selezione", "Seleziona prima un'immagine montata dalla lista.")
            return
        entry = item.data(Qt.UserRole)
        device = entry["device"]

        try:
            unmount_result = subprocess.run(
                ["udisksctl", "unmount", "-b", device], capture_output=True, text=True)
            self._log(unmount_result.stdout.strip())
            if unmount_result.returncode != 0:
                self._log(unmount_result.stderr.strip())

            delete_result = subprocess.run(
                ["udisksctl", "loop-delete", "-b", device], capture_output=True, text=True)
            self._log(delete_result.stdout.strip())
            if delete_result.returncode != 0:
                self._log(delete_result.stderr.strip())

            self.mounted_images = [e for e in self.mounted_images if e["device"] != device]
            self._refresh_mounted_list()

        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante lo smontaggio: {e}")

    # ------------------------------------------------------------------
    # Utility tab Prefix Wine
    # ------------------------------------------------------------------
    def _wine_log(self, text):
        self.wine_tool_log.appendPlainText(text)

    def _update_prefix_button_states(self):
        has_selection = self.prefix_list.currentItem() is not None
        self.btn_remove_prefix.setEnabled(has_selection)
        self.btn_apply_version.setEnabled(has_selection)
        self.btn_open_winecfg.setEnabled(has_selection)
        self.btn_open_regedit.setEnabled(has_selection)
        self.btn_install_deps.setEnabled(has_selection)

    def _refresh_prefix_list(self):
        self.prefix_list.clear()
        for prefix_entry in self.prefixes:
            label = f"{prefix_entry['name']}  ({prefix_entry['path']})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, prefix_entry)
            self.prefix_list.addItem(item)
        self._update_prefix_button_states()

    def _selected_prefix(self):
        item = self.prefix_list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def _choose_prefix_path(self, title):
        """Mostra i prefix già registrati (tab Prefix Wine) più l'opzione di
        sfogliarne uno manuale. Ritorna il percorso scelto o None."""
        browse_label = "📁 Sfoglia manualmente..."
        options = [p["name"] for p in self.prefixes] + [browse_label]

        choice, ok = QInputDialog.getItem(self, title, "Scegli un prefix:", options, 0, False)
        if not ok:
            return None

        if choice == browse_label:
            return QFileDialog.getExistingDirectory(
                self, "Seleziona la cartella del prefix", self.settings["prefix_root"]) or None

        entry = next((p for p in self.prefixes if p["name"] == choice), None)
        return entry["path"] if entry else None

    def _remove_z_drive(self, prefix_path):
        """Rimuove il symlink Z: da dosdevices (Wine lo ricrea a ogni wineboot)."""
        z_link = os.path.join(prefix_path, "dosdevices", "z:")
        if os.path.lexists(z_link):
            try:
                os.remove(z_link)
                self._wine_log("Unità Z: rimossa (accesso alla radice bloccato).")
            except OSError as e:
                self._wine_log(f"ATTENZIONE: impossibile rimuovere Z: {e}")

    def _ensure_z_drive(self, prefix_path):
        """Ricrea il symlink Z: -> / se mancante. Necessario per winecfg/regedit
        che girano fuori sandbox e devono accedere a file esterni (es. installer
        su disco USB). wine-sandbox rimuoverà Z: prima dell'esecuzione sandboxed."""
        z_link = os.path.join(prefix_path, "dosdevices", "z:")
        if not os.path.lexists(z_link):
            try:
                os.symlink("/", z_link)
                self._wine_log("Unità Z: ricreata per winecfg/regedit (accesso ai file esterni).")
            except OSError as e:
                self._wine_log(f"ATTENZIONE: impossibile ricreare Z: {e}")

    def _run_wine_tool(self, prefix_path, arch, program, args, blocking_log=True, detached=False):
        """Esegue un tool Wine (winecfg/wineboot/regedit) con WINEPREFIX (e
        WINEARCH solo se esplicitamente richiesto) impostati, DIRETTAMENTE
        senza bwrap (sono tool fidati, non il gioco)."""
        env = QProcessEnvironment.systemEnvironment()
        env.insert("WINEPREFIX", prefix_path)
        if arch:  # stringa vuota = WoW64 predefinito, non forzare nulla
            env.insert("WINEARCH", arch)

        if detached:
            # winecfg/regedit sono GUI interattive: lanciale su un QProcess
            # separato (non bloccano la finestra). Usiamo start() invece di
            # startDetached() per poter ricevere il segnale finished.
            # Ricreiamo Z: prima di avviare (wine-sandbox la rimuove per i
            # giochi sandboxed, ma winecfg needs accesso ai file esterni).
            self._ensure_z_drive(prefix_path)
            if not hasattr(self, "_detached_procs"):
                self._detached_procs = []
            proc = QProcess(self)
            proc.setProcessEnvironment(env)
            proc.setProcessChannelMode(QProcess.MergedChannels)
            proc.readyReadStandardOutput.connect(self._on_wine_tool_output)
            proc.errorOccurred.connect(
                lambda err: self._wine_log(f"\n[ERRORE avvio {program}: {proc.errorString()}]\n"))
            proc.finished.connect(
                lambda code, status: self._wine_log(f"\n[{program} terminato con codice {code}]\n"))
            proc.finished.connect(lambda code, status: self._detached_procs.remove(proc))
            self._detached_procs.append(proc)
            proc.start(program, args)
            self._wine_log(f"$ {program} {' '.join(args)}  (avviato, finestra separata)")
            return

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso",
                                 "C'è già un'operazione sul prefix in esecuzione. Attendi che finisca.")
            return

        self._wine_log(f"\n$ {program} {' '.join(args)}\n")
        self.wine_tool_process = QProcess(self)
        self.wine_tool_process.setProcessEnvironment(env)
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(
            lambda code, status: self._wine_log(f"\n[terminato con codice {code}]\n"))
        self.wine_tool_process.start(program, args)

    def _on_wine_tool_output(self):
        data = self.wine_tool_process.readAllStandardOutput().data().decode(errors="replace")
        self.wine_tool_log.moveCursor(QTextCursor.MoveOperation.End)
        self.wine_tool_log.insertPlainText(data)

    # ------------------------------------------------------------------
    # Azioni tab Prefix Wine
    # ------------------------------------------------------------------
    def _on_create_prefix_clicked(self):
        parent_dir = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella dove creare il nuovo prefix", self.settings["prefix_root"])
        if not parent_dir:
            return

        name, ok = QInputDialog.getText(self, "Nome del prefix", "Nome (verrà usato anche come nome cartella):")
        if not ok or not name.strip():
            return
        safe_name = re.sub(r"[^\w\-]", "_", name.strip())

        arch_choice, ok = QInputDialog.getItem(
            self, "Architettura", "Scegli l'architettura del prefix:",
            ["Predefinito (WoW64 - consigliato, gestisce anche app a 32-bit)", "win64 (esplicito)"], 0, False)
        if not ok:
            return
        arch = "" if arch_choice.startswith("Predefinito") else "win64"

        prefix_path = os.path.join(parent_dir, safe_name)
        if os.path.exists(prefix_path) and os.listdir(prefix_path):
            QMessageBox.critical(self, "Errore", f"La cartella esiste già e non è vuota:\n{prefix_path}")
            return

        wine_sandbox = self._wine_sandbox_path()
        if not shutil.which(wine_sandbox) and not os.path.isfile(wine_sandbox):
            QMessageBox.critical(
                self, "wine-sandbox non trovato",
                f"Lo script wine-sandbox non è trovato in:\n{wine_sandbox}\n"
                "Verifica il percorso nelle Impostazioni in alto.")
            return

        init_args = ["--init", prefix_path]
        if arch:
            init_args.append(arch)

        ws_program, ws_args = self._wine_sandbox_launch_cmd(init_args)

        self._wine_log(f"Creazione prefix in: {prefix_path} (architettura {arch or 'WoW64'})")
        self._wine_log(f"$ {ws_program} {' '.join(ws_args)}")

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso",
                                 "C'è già un'operazione sul prefix in esecuzione. Attendi che finisca.")
            return

        def after_boot(exit_code):
            if exit_code == 0:
                entry = {"name": name.strip(), "path": prefix_path, "arch": arch}
                self.prefixes.append(entry)
                save_json(PREFIXES_FILE, self.prefixes)
                self._refresh_prefix_list()
                self._wine_log("Prefix creato e registrato con successo.")
                # Z: è già rimossa da wine-sandbox --init, niente da fare qui
            else:
                self._wine_log(
                    f"ATTENZIONE: wineboot ha restituito codice {exit_code}. "
                    "Il prefix potrebbe non essere stato inizializzato correttamente.")
                if os.path.isdir(prefix_path) and not os.listdir(prefix_path):
                    try:
                        os.rmdir(prefix_path)
                        self._wine_log("Cartella prefix vuota rimossa per allow retry.")
                    except OSError:
                        pass
                QMessageBox.critical(
                    self, "Errore creazione prefix",
                    f"wineboot ha fallito (codice {exit_code}). Controlla il log per i dettagli.\n"
                    "Verifica che wine e bwrap siano installati e funzionanti.")

        self.wine_tool_process = QProcess(self)
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(lambda code, status: after_boot(code))
        self.wine_tool_process.finished.connect(
            lambda code, status: self._wine_log(f"\n[terminato con codice {code}]\n"))
        self.wine_tool_process.start(ws_program, ws_args)

    def _on_add_existing_prefix_clicked(self):
        path = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella di un prefix Wine esistente", self.settings["prefix_root"])
        if not path:
            return

        name, ok = QInputDialog.getText(self, "Nome del prefix", "Nome da mostrare nella lista:")
        if not ok or not name.strip():
            return

        arch_choice, ok = QInputDialog.getItem(
            self, "Architettura", "Architettura di questo prefix:",
            ["Predefinito (WoW64)", "win64 (esplicito)", "win32 (solo prefix storici pre-esistenti)"], 0, False)
        if not ok:
            return
        arch = "" if arch_choice.startswith("Predefinito") else ("win64" if arch_choice.startswith("win64") else "win32")

        entry = {"name": name.strip(), "path": path, "arch": arch}
        self.prefixes.append(entry)
        save_json(PREFIXES_FILE, self.prefixes)
        self._refresh_prefix_list()

    def _on_remove_prefix_clicked(self):
        entry = self._selected_prefix()
        if not entry:
            return
        confirm = QMessageBox.question(
            self, "Conferma rimozione",
            f"Rimuovere '{entry['name']}' dalla lista?\n(Non elimina i file del prefix su disco)")
        if confirm == QMessageBox.Yes:
            self.prefixes = [p for p in self.prefixes if p != entry]
            save_json(PREFIXES_FILE, self.prefixes)
            self._refresh_prefix_list()

    def _on_apply_windows_version(self):
        entry = self._selected_prefix()
        if not entry:
            return
        version_label = self.windows_version_combo.currentText()
        version_code = next(code for label, code in WINDOWS_VERSIONS if label == version_label)

        self._wine_log(f"Imposto versione Windows '{version_label}' ({version_code}) su {entry['path']}")

        # winecfg /v non imposta affidabilmente la versione in Wine 10+.
        # Scriviamo direttamente nel registry con reg add, poi wineboot -u
        # per applicare i cambiamenti.
        prefix_path = entry["path"]
        arch = entry.get("arch", "")

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso",
                                 "C'è già un'operazione sul prefix in esecuzione. Attendi che finisca.")
            return

        self._ensure_z_drive(prefix_path)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("WINEPREFIX", prefix_path)
        if arch:
            env.insert("WINEARCH", arch)

        reg_cmd = ["wine", "reg", "add", "HKCU\\Software\\Wine",
                   "/v", "Version", "/t", "REG_SZ", "/d", version_code, "/f"]

        self._wine_log(f"$ {' '.join(reg_cmd)}")
        self.wine_tool_process = QProcess(self)
        self.wine_tool_process.setProcessEnvironment(env)
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)

        def after_reg(exit_code, status):
            if exit_code == 0:
                self._wine_log(f"Versione Windows impostata a '{version_code}'. Eseguo wineboot -u per applicare...")
                boot_env = QProcessEnvironment.systemEnvironment()
                boot_env.insert("WINEPREFIX", prefix_path)
                if arch:
                    boot_env.insert("WINEARCH", arch)
                self.wine_tool_process = QProcess(self)
                self.wine_tool_process.setProcessEnvironment(boot_env)
                self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
                self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
                self.wine_tool_process.errorOccurred.connect(
                    lambda err: self._wine_log(
                        f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
                self.wine_tool_process.finished.connect(
                    lambda code, st: self._wine_log(
                        f"\n[wineboot terminato con codice {code}]\n"
                        + ("Versione Windows applicata con successo." if code == 0 else
                           "ATTENZIONE: wineboot ha restituito un errore.")))
                self.wine_tool_process.start("wineboot", ["-u"])
            else:
                self._wine_log(f"ERRORE: reg add ha fallito (codice {exit_code}).")

        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(after_reg)
        self.wine_tool_process.start("wine", reg_cmd[1:])

    def _on_open_winecfg(self):
        entry = self._selected_prefix()
        if not entry:
            return
        self._run_wine_tool(entry["path"], entry.get("arch", ""), "winecfg", [], detached=True)

    def _on_open_regedit(self):
        entry = self._selected_prefix()
        if not entry:
            return
        self._run_wine_tool(entry["path"], entry.get("arch", ""), "wine", ["regedit"], detached=True)

    def _on_install_dependencies(self):
        entry = self._selected_prefix()
        if not entry:
            return

        verbs = [verb for verb, cb in self.winetricks_checkboxes.items() if cb.isChecked()]
        custom = self.winetricks_custom_edit.text().strip()
        if custom:
            verbs.extend(custom.split())

        if not verbs:
            QMessageBox.information(self, "Nessun verbo selezionato",
                                     "Seleziona almeno una dipendenza o scrivine una nel campo personalizzato.")
            return

        confirm = QMessageBox.question(
            self, "Rete abilitata",
            f"Questa operazione scarica {', '.join(verbs)} da fonti esterne (rete abilitata "
            "solo per questa operazione, tramite wine-sandbox --setup). Procedere?"
        )
        if confirm != QMessageBox.Yes:
            return

        setup_args = ["--setup", entry["path"]] + verbs
        ws_program, ws_args = self._wine_sandbox_launch_cmd(setup_args)
        self._wine_log(f"\n$ {ws_program} {' '.join(ws_args)}\n")

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso", "Attendi che l'operazione corrente finisca.")
            return

        self.wine_tool_process = QProcess(self)
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(
            lambda code, status: self._wine_log(f"\n[winetricks terminato con codice {code}]\n"))
        self.wine_tool_process.start(ws_program, ws_args)

    # ------------------------------------------------------------------
    # dgVoodoo2 (download automatico dall'ultima release GitHub)
    # ------------------------------------------------------------------
    def _on_download_dgvoodoo(self):
        if self.dgvoodoo_thread is not None and self.dgvoodoo_thread.isRunning():
            QMessageBox.warning(self, "Download in corso", "Un download di dgVoodoo2 è già in corso.")
            return

        prefix_path = self._choose_prefix_path("In quale prefix si trova il gioco?")
        if not prefix_path:
            return

        drive_c = os.path.join(prefix_path, "drive_c")
        start_dir = drive_c if os.path.isdir(drive_c) else prefix_path

        target_folder = QFileDialog.getExistingDirectory(
            self, "Seleziona la cartella del gioco DENTRO al prefix (dove si trova l'eseguibile)",
            start_dir)
        if not target_folder:
            return

        if not target_folder.startswith(prefix_path):
            confirm = QMessageBox.question(
                self, "Cartella fuori dal prefix",
                "La cartella scelta non sembra stare dentro il prefix selezionato. "
                "Procedere comunque?"
            )
            if confirm != QMessageBox.Yes:
                return

        arch_choice, ok = QInputDialog.getItem(
            self, "Architettura del gioco",
            "Il gioco è a 32 o 64 bit? (la maggior parte dei giochi Win9x/XP è a 32 bit)",
            ["x86 (32-bit, consigliato per Win9x/XP)", "x64 (64-bit)"], 0, False)
        if not ok:
            return
        arch_folder = "x86" if arch_choice.startswith("x86") else "x64"

        self._wine_log(f"\nAvvio download dgVoodoo2 (architettura {arch_folder}) verso: {target_folder}")

        self.dgvoodoo_thread = DgVoodooDownloadThread()
        self.dgvoodoo_thread.log.connect(self._wine_log)
        self.dgvoodoo_thread.finished_ok.connect(
            lambda extract_dir: self._install_dgvoodoo_files(extract_dir, target_folder, arch_folder))
        self.dgvoodoo_thread.finished_error.connect(
            lambda msg: (self._wine_log(f"ERRORE: {msg}"), QMessageBox.critical(self, "Errore dgVoodoo2", msg)))
        self.dgvoodoo_thread.start()

    def _install_dgvoodoo_files(self, extract_dir, target_folder, arch_folder):
        try:
            arch_dir = None
            for root, dirs, _files in os.walk(extract_dir):
                for d in dirs:
                    if d.lower() == arch_folder and os.path.basename(root).upper() == "MS":
                        arch_dir = os.path.join(root, d)
                        break
                if arch_dir:
                    break

            # fallback: cerca qualunque cartella che contenga d3d9.dll e corrisponda all'arch
            if not arch_dir:
                for root, _dirs, files in os.walk(extract_dir):
                    if any(f.lower() == "d3d9.dll" for f in files) and arch_folder in root.lower():
                        arch_dir = root
                        break

            if not arch_dir:
                self._wine_log(
                    "ERRORE: non trovo la cartella DLL corrispondente nell'archivio dgVoodoo2. "
                    "La struttura dello zip potrebbe essere cambiata rispetto a quanto atteso."
                )
                QMessageBox.critical(
                    self, "Struttura non riconosciuta",
                    "Non trovo le DLL di dgVoodoo2 nell'archivio scaricato. Puoi estrarlo ed "
                    "copiare i file manualmente dalla cartella MS/{} nella cartella del gioco.".format(arch_folder)
                )
                return

            copied = []
            for fname in os.listdir(arch_dir):
                src = os.path.join(arch_dir, fname)
                if os.path.isfile(src):
                    dst = os.path.join(target_folder, fname)
                    shutil.copy2(src, dst)
                    copied.append(fname)

            # file di configurazione/control panel, di solito alla radice dell'archivio
            for extra_name in ("dgVoodoo.conf", "dgVoodooCpl.exe"):
                for root, _dirs, files in os.walk(extract_dir):
                    for f in files:
                        if f.lower() == extra_name.lower():
                            src = os.path.join(root, f)
                            dst = os.path.join(target_folder, f)
                            if not os.path.exists(dst):
                                shutil.copy2(src, dst)
                                copied.append(f)
                            break

            self._wine_log(f"dgVoodoo2 installato in: {target_folder}")
            self._wine_log("File copiati: " + ", ".join(copied))
            QMessageBox.information(
                self, "dgVoodoo2 installato",
                f"File copiati in:\n{target_folder}\n\n"
                "Se serve configurare risoluzione/output, lancia dgVoodooCpl.exe dalla stessa "
                "cartella (tramite wine-sandbox, come qualunque altro eseguibile)."
            )
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante la copia dei file: {e}")

    # ------------------------------------------------------------------
    # Chiusura applicazione
    # ------------------------------------------------------------------
    def _unmount_all(self):
        """Smonta tutte le immagini attualmente montate via udisksctl."""
        if not self.mounted_images:
            return
        self._log(f"\nSmontaggio di {len(self.mounted_images)} immagini alla chiusura...")
        for entry in list(self.mounted_images):
            device = entry["device"]
            try:
                subprocess.run(["udisksctl", "unmount", "-b", device],
                               capture_output=True, text=True, timeout=10)
                subprocess.run(["udisksctl", "loop-delete", "-b", device],
                               capture_output=True, text=True, timeout=10)
                self._log(f"  Smontato: {entry.get('path', device)}")
            except Exception as e:
                self._log(f"  ERRORE smontaggio {device}: {e}")
        self.mounted_images.clear()
        self._refresh_mounted_list()

    def closeEvent(self, event):
        if self.settings.get("unmount_on_exit", True) and self.mounted_images:
            self._unmount_all()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
