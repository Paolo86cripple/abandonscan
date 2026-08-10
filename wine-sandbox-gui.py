#!/usr/bin/env python3
"""
wine-sandbox-gui.py - Frontend grafico per gestire ed eseguire giochi
abandonware attraverso wine-sandbox (isolamento bwrap: rete disabilitata,
capability Linux disattivate, filesystem in gran parte read-only).

Sezioni:
  - Giochi: libreria di giochi installati, installazione nuovi, avvio in sandbox
  - Immagini ottiche: montaggio ISO/IMG/NRG/BIN+CUE senza sudo (udisksctl)
  - Prefix Wine: creazione prefix, versione Windows, winecfg/regedit/winetricks,
    esecuzione di eseguibili standalone in sandbox
  - Scansione: scansione malware opzionale di un file (ClamAV locale, VirusTotal,
    Hybrid Analysis/Falcon Sandbox) a piacimento
  - Sistema: dipendenze, integrazione desktop, backup

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
import hashlib
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import html
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QProcess, QUrl, QProcessEnvironment, QThread, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QPlainTextEdit, QFileDialog, QMessageBox, QInputDialog, QSplitter,
    QGroupBox, QFormLayout, QTabWidget, QComboBox, QCheckBox, QGridLayout,
    QScrollArea, QDialog
)

CONFIG_DIR = Path.home() / ".config" / "wine-sandbox-gui"
GAMES_FILE = CONFIG_DIR / "games.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PREFIXES_FILE = CONFIG_DIR / "prefixes.json"
CUSTOM_PROFILES_FILE = CONFIG_DIR / "custom-game-profiles.json"

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
    "sec_unshare_ipc": False,
    "sec_wayland_only": True,
    "sec_x11_fallback": False,
    "sec_dri": True,
    "sec_gpu_cap_sysadmin": False,
    "sec_audio": True,
    "sec_loopback": False,
    "sec_allow_network": False,
    "sec_disable_zdrive": True,
    "sec_exe_rw": False,
    "winecfg_ensure_zdrive": True,
    "scan_use_clamav": True,
    "scan_use_virustotal": False,
    "scan_use_falcon": False,
    "virustotal_api_key": "",
    "falcon_api_key": "",
    "sec_verify_integrity": True,
    "sec_resource_limits": False,
    "sec_memory_limit": "2G",
    "sec_cpu_limit": "200",
    "enable_desktop_launcher_creation": False,
    "unmount_on_exit": True,
    "bchunk_output_dir": "",
    "dgvoodoo_version": "v2.52",
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

# Database curato di profili di configurazione per titoli noti (abandonware
# spesso presenti su Collection Chamber). Fonti: WineHQ AppDB, PCGamingWiki,
# Lutris install scripts (dati raccolti manualmente, non via API live - le
# API pubbliche di questi siti non sono adatte a query automatiche affidabili
# per nome libero). Aggiorna/estendi questo dizionario quando trovi note utili.
# Chiavi normalizzate: minuscolo, senza punteggiatura, spazi singoli.
#
# Campi per profilo:
#   winetricks: lista di verbi winetricks consigliati
#   windows_version: codice da WINDOWS_VERSIONS (es. "win98"), o None
#   dgvoodoo: True se consigliato dgVoodoo2 invece di dxvk
#   cpu_limit_pct: percentuale CPU consigliata (systemd-run), None se non serve
#   notes: note testuali (bug noti, avvertenze)
#   sources: lista di fonti consultate
GAME_PROFILES = {
    "shadow of destiny": {
        "display_name": "Shadow of Destiny (Konami, 2002)",
        "winetricks": ["corefonts", "vcrun6", "quartz", "directmusic"],
        "windows_version": "winxp",
        "dgvoodoo": True,
        "cpu_limit_pct": 30,
        "notes": (
            "Bug noto: su CPU moderne il gioco gira a velocità doppia/tripla, "
            "rompendo il timing dei puzzle. Limitare la CPU (~25-30%) aiuta a "
            "renderlo giocabile. Usa DirectX 8; dgVoodoo2 più stabile di dxvk "
            "per questo titolo. quartz/directmusic servono per FMV e colonna sonora."
        ),
        "sources": ["PCGamingWiki", "WineHQ AppDB"],
    },
    "diablo": {
        "display_name": "Diablo (Blizzard, 1996)",
        "winetricks": ["corefonts", "vcrun6"],
        "windows_version": "win98",
        "dgvoodoo": False,
        "cpu_limit_pct": None,
        "notes": "DirectDraw/software rendering, generalmente stabile con WineD3D nativo.",
        "sources": ["WineHQ AppDB", "PCGamingWiki"],
    },
    "system shock 2": {
        "display_name": "System Shock 2 (Looking Glass, 1999)",
        "winetricks": ["corefonts", "vcrun6", "directmusic"],
        "windows_version": "win98",
        "dgvoodoo": True,
        "cpu_limit_pct": None,
        "notes": (
            "Motore Dark Engine sensibile a versione Windows e refresh rate. "
            "dgVoodoo2 consigliato per compatibilità Glide/D3D6-7."
        ),
        "sources": ["PCGamingWiki", "Lutris"],
    },
    "grim fandango": {
        "display_name": "Grim Fandango (LucasArts, 1998)",
        "winetricks": ["corefonts", "quartz"],
        "windows_version": "win98",
        "dgvoodoo": False,
        "cpu_limit_pct": None,
        "notes": "Usa Residual/ScummVM su sistemi moderni è spesso preferibile a Wine nativo.",
        "sources": ["PCGamingWiki"],
    },
    "discworld noir": {
        "display_name": "Discworld Noir (Perfect Entertainment, 1999)",
        "winetricks": ["corefonts", "vcrun6", "quartz", "directmusic"],
        "windows_version": "win98",
        "dgvoodoo": False,
        "cpu_limit_pct": None,
        "notes": (
            "Il gioco originale è incompatibile con XP e versioni successive nativamente "
            "(problema noto anche su Windows reale). Cerca il 'Fix by Loma' (patch community "
            "PCGamingWiki) da applicare nella cartella del gioco se crasha all'avvio. "
            "Usa SafeDisc DRM: se hai la versione originale su CD potrebbe non partire senza "
            "crack no-CD; le versioni re-release (GOG/Steam) sono DRM-free e non serve."
        ),
        "sources": ["PCGamingWiki"],
    },
    "mechwarrior 3": {
        "display_name": "MechWarrior 3 (MicroProse, 1999)",
        "winetricks": ["corefonts", "vcrun6", "directplay", "directshow", "l3codecx", "avifil32"],
        "windows_version": "win98",
        "dgvoodoo": True,
        "cpu_limit_pct": None,
        "notes": (
            "WineHQ AppDB richiede esplicitamente un prefix a 32-bit in modalità "
            "compatibilità Win95/Win98 (crea il prefix con architettura win32, non "
            "WoW64/win64). Serve il CD originale montato o un'immagine ISO (usa la tab "
            "Montaggio immagini). Applica la patch ufficiale 1.2 prima di giocare. "
            "dgVoodoo2 consigliato per compatibilità DirectX 6.1/Glide e risoluzioni moderne "
            "(vedi anche Hi-Res Patch + Widescreen Fix su PCGamingWiki per multi-monitor)."
        ),
        "sources": ["WineHQ AppDB", "PCGamingWiki"],
    },
}


class _HTMLTextExtractor(HTMLParser):
    """Estrae testo leggibile da HTML grezzo, saltando script/style/nav
    e inserendo newline dopo blocchi comuni, per rendere leggibili pagine
    come PCGamingWiki/WineHQ AppDB senza dipendenze esterne (no bs4)."""
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "table"}
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text + " ")

    def get_text(self):
        text = html.unescape("".join(self.chunks))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def fetch_url_as_text(url, timeout=15, max_chars=20000):
    """Scarica una URL e ne estrae il testo leggibile (rimuovendo tag HTML).
    Usato per far leggere all'utente pagine come PCGamingWiki/WineHQ AppDB
    dentro l'app, senza parsing strutturato (troppo fragile per prosa libera)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wine-sandbox-gui)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode(errors="replace")
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    text = parser.get_text()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... testo troncato ...]"
    return text


def _normalize_game_name(name):
    """Normalizza un nome gioco per il matching col database profili:
    minuscolo, rimuove punteggiatura/edizioni comuni, spazi singoli."""
    n = name.lower()
    n = re.sub(r"[™®©]", "", n)
    n = re.sub(r"\b(goty|game of the year|gold edition|deluxe edition|"
               r"remastered|directors cut|director's cut|edition|complete)\b", "", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def find_game_profile(name, custom_profiles=None):
    """Cerca un profilo per nome: prima nei profili personalizzati
    dell'utente (match esatto), poi nel database curato (match esatto
    normalizzato, poi contenimento parziale).
    Ritorna (chiave, profilo, is_custom) o (None, None, False)."""
    normalized = _normalize_game_name(name)
    custom_profiles = custom_profiles or {}
    if normalized in custom_profiles:
        return normalized, custom_profiles[normalized], True
    if normalized in GAME_PROFILES:
        return normalized, GAME_PROFILES[normalized], False
    for key, profile in GAME_PROFILES.items():
        if key in normalized or normalized in key:
            return key, profile, False
    return None, None, False

CODEC_WINETRICKS_VERBS = [
    "allcodecs", "ffdshow", "xvid", "l3codecx", "cinepak", "dirac",
    "icodecs", "wmp9", "wmp11",
]

# Versione dgVoodoo2 bloccata (pinning) alla 2.52: le release 2.53+ hanno
# rimosso l'output D3D9 e le 2.6x-2.8x su Wine causano crash in WineD3D
# durante la traduzione degli shader SM4 (vedi WineHQ bug 58731).
# La 2.52 è l'ultima versione con output D3D9 (utile su Wine dove D3D11
# crasha). L'utente può scegliere "latest" dalla GUI se vuole l'ultima release.
DGVOODOO_REPO_API_STABLE = "https://api.github.com/repos/dege-diosg/dgVoodoo2/releases/tags/v2.52"
DGVOODOO_REPO_API_LATEST = "https://api.github.com/repos/dege-diosg/dgVoodoo2/releases/latest"
DGVOODOO_ZIP_PASSWORD = "dege"  # password dgVoodoo2 per archivi crittati da dege.fw.hu

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
    """Scarica ed estrae una release di dgVoodoo2 da GitHub, in background
    per non bloccare l'interfaccia durante il download."""
    log = Signal(str)
    finished_ok = Signal(str)   # percorso cartella estratta
    finished_error = Signal(str)

    def __init__(self, api_url):
        super().__init__()
        self.api_url = api_url

    def run(self):
        try:
            self.log.emit(f"Interrogo l'API GitHub: {self.api_url}")
            req = urllib.request.Request(
                self.api_url, headers={"User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                release_data = json.loads(resp.read().decode())

            zip_asset = next(
                (a for a in release_data.get("assets", []) if a["name"].lower().endswith(".zip")),
                None
            )
            if not zip_asset:
                self.finished_error.emit("Nessun asset .zip trovato nella release di dgVoodoo2.")
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
                zf.setpassword(DGVOODOO_ZIP_PASSWORD.encode())
                zf.extractall(extract_dir)

            self.finished_ok.emit(extract_dir)

        except Exception as e:
            self.finished_error.emit(f"Errore durante il download di dgVoodoo2: {e}")


LUTRIS_API_SEARCH = "https://lutris.net/api/games?search="
LUTRIS_API_GAME = "https://lutris.net/api/games/"


class PageFetchThread(QThread):
    """Scarica una pagina web e ne estrae il testo leggibile, in background
    per non bloccare la UI. Usato per consultare PCGamingWiki/WineHQ AppDB/ecc.
    dentro l'app quando l'utente fornisce un link durante la creazione di un
    profilo personalizzato."""
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            text = fetch_url_as_text(self.url)
            self.finished_ok.emit(text or "(nessun testo estraibile da questa pagina)")
        except Exception as e:
            self.finished_error.emit(
                f"Errore nello scaricare/leggere la pagina: {e}\n\n"
                "Alcuni siti (es. WineHQ AppDB) bloccano il download automatico con "
                "protezioni anti-bot: usa il pulsante 'Apri nel browser' in quel caso."
            )


class LutrisLookupThread(QThread):
    """Cerca un gioco sull'API pubblica di Lutris ed estrae informazioni
    utili (verbi winetricks, versione Windows, note) dagli install script
    strutturati. Query on-demand, solo quando l'utente lo richiede
    esplicitamente (nessuna chiamata automatica in background)."""
    log = Signal(str)
    finished_ok = Signal(dict)   # profilo estratto
    finished_error = Signal(str)

    def __init__(self, game_name):
        super().__init__()
        self.game_name = game_name

    def run(self):
        try:
            query = urllib.parse.quote(self.game_name)
            search_url = LUTRIS_API_SEARCH + query
            self.log.emit(f"Cerco su Lutris: {search_url}")
            req = urllib.request.Request(search_url, headers={"User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            results = data.get("results", [])
            # Preferisci risultati per piattaforma Windows e match di nome più stretto
            windows_results = [
                r for r in results
                if any(p.get("name") == "Windows" for p in r.get("platforms", []))
            ]
            candidates = windows_results or results
            if not candidates:
                self.finished_error.emit(f"Nessun risultato su Lutris per '{self.game_name}'.")
                return

            normalized_query = _normalize_game_name(self.game_name)
            best = None
            for r in candidates:
                if _normalize_game_name(r.get("name", "")) == normalized_query:
                    best = r
                    break
            if not best:
                best = candidates[0]

            slug = best["slug"]
            self.log.emit(f"Trovato: {best['name']} ({best.get('year', '?')}) - slug: {slug}")

            detail_url = LUTRIS_API_GAME + slug
            req2 = urllib.request.Request(detail_url, headers={"User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                detail = json.loads(resp2.read().decode())

            installers = detail.get("installers", [])
            wine_installers = [i for i in installers if i.get("runner") == "wine"]
            chosen = wine_installers[0] if wine_installers else (installers[0] if installers else None)

            if not chosen:
                self.finished_error.emit(
                    f"'{best['name']}' trovato su Lutris ma senza install script disponibili.")
                return

            profile = self._extract_profile(best, chosen)
            self.finished_ok.emit(profile)

        except urllib.error.URLError as e:
            self.finished_error.emit(f"Errore di rete contattando Lutris: {e}")
        except Exception as e:
            self.finished_error.emit(f"Errore durante la ricerca su Lutris: {e}")

    @staticmethod
    def _extract_profile(game_info, installer):
        """Estrae verbi winetricks, indizi sulla versione Windows e note
        dallo script installer Lutris (formato JSON strutturato)."""
        script = installer.get("script", {}) or {}
        steps = script.get("installer", []) or []

        winetricks_verbs = []
        for step in steps:
            task = step.get("task") if isinstance(step, dict) else None
            if isinstance(task, dict) and task.get("name") == "winetricks":
                app = task.get("app", "")
                winetricks_verbs.extend(app.split())

        # Indizio versione Windows: cerca "win98"/"winxp"/ecc. nei file referenziati
        # (es. reg_file con nome tipo "win98.reg") o nelle note testuali.
        text_blob = json.dumps(script).lower() + " " + (installer.get("notes") or "").lower()
        windows_version = None
        for label, code in WINDOWS_VERSIONS:
            if code in text_blob or label.lower() in text_blob:
                windows_version = code
                break

        dgvoodoo = "dgvoodoo" in text_blob

        notes_parts = []
        if installer.get("notes"):
            notes_parts.append(installer["notes"].strip())
        if installer.get("description"):
            notes_parts.append(installer["description"].strip())
        notes_parts.append(
            f"Estratto automaticamente dall'install script Lutris '{installer.get('slug', '')}' "
            f"(runner: {installer.get('runner', '?')}, versione: {installer.get('version', '?')}). "
            "Verifica manualmente prima di applicare: gli script Lutris spesso includono anche "
            "download di file di gioco/patch che questa GUI non gestisce automaticamente."
        )

        return {
            "display_name": f"{game_info.get('name', '')} ({game_info.get('year', '?')})",
            "winetricks": sorted(set(winetricks_verbs)),
            "windows_version": windows_version,
            "dgvoodoo": dgvoodoo,
            "cpu_limit_pct": None,
            "notes": "\n\n".join(notes_parts),
            "sources": [f"Lutris (install script '{installer.get('slug', '')}')"],
        }


VT_API_FILES = "https://www.virustotal.com/api/v3/files/"
VT_API_UPLOAD = "https://www.virustotal.com/api/v3/files"
FALCON_API_SUBMIT = "https://www.hybrid-analysis.com/api/v2/submit/file"
FALCON_API_REPORT = "https://www.hybrid-analysis.com/api/v2/report/"

# Costanti VirusTotal/Falcon per i risultati (riadattate dal flusso di scan-game.sh)
SCAN_STATUS_SKIPPED = "skipped"


class ScanThread(QThread):
    """Scansiona un file (es. un installer) a piacimento, in background:
    1) ClamAV locale (se installato), 2) VirusTotal (hash o upload),
    3) Hybrid Analysis/Falcon Sandbox (solo se VirusTotal segnala positivi).
    Nessuna scansione è automatica o bloccante."""
    log = Signal(str)
    finished = Signal(dict)

    def __init__(self, filepath, use_clamav=True, use_virustotal=False,
                 use_falcon=False, vt_api_key="", falcon_api_key=""):
        super().__init__()
        self.filepath = filepath
        self.use_clamav = use_clamav
        self.use_virustotal = use_virustotal
        self.use_falcon = use_falcon
        self.vt_api_key = vt_api_key
        self.falcon_api_key = falcon_api_key

    def run(self):
        result = {
            "file": self.filepath,
            "sha256": None,
            "clamav": {"status": SCAN_STATUS_SKIPPED, "detail": ""},
            "virustotal": {"status": SCAN_STATUS_SKIPPED, "detail": "",
                           "malicious": 0, "suspicious": 0, "harmless": 0,
                           "undetected": 0, "report_url": None, "analysis_id": None},
            "falcon": {"status": SCAN_STATUS_SKIPPED, "detail": "",
                       "report_url": None},
        }
        try:
            self.log.emit(f"Calcolo SHA256 di: {self.filepath}")
            sha256 = self._compute_sha256(self.filepath)
            result["sha256"] = sha256
            self.log.emit(f"SHA256: {sha256}")

            if self.use_clamav:
                self._run_clamav(result)

            if self.use_virustotal and self.vt_api_key:
                self._run_virustotal(result)

            if (self.use_falcon and self.falcon_api_key
                    and result["virustotal"]["status"] in ("flagged", "uploaded")
                    and (result["virustotal"]["malicious"] > 0
                         or result["virustotal"]["suspicious"] > 0)):
                self._run_falcon(result)

            self.finished.emit(result)
        except Exception as e:
            result["clamav"]["status"] = "error"
            self.log.emit(f"ERRORE durante la scansione: {e}")
            self.finished.emit(result)

    @staticmethod
    def _compute_sha256(filepath):
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _run_clamav(self, result):
        clamscan = shutil.which("clamscan")
        if not clamscan:
            result["clamav"]["status"] = "skipped"
            result["clamav"]["detail"] = "clamscan non trovato sul sistema, ClamAV saltato."
            self.log.emit(result["clamav"]["detail"])
            return
        self.log.emit(f"Avvio clamscan su: {self.filepath}")
        proc = subprocess.run(
            [clamscan, "--no-summary", "--infected", self.filepath],
            capture_output=True, text=True)
        output = (proc.stdout + proc.stderr).strip()
        found = [line for line in output.splitlines() if line.strip().endswith("FOUND")]
        if found:
            result["clamav"]["status"] = "infected"
            result["clamav"]["detail"] = "\n".join(found)
        elif "OK" in output or proc.returncode in (0,):
            result["clamav"]["status"] = "ok"
            result["clamav"]["detail"] = "Nessuna minaccia rilevata."
        else:
            result["clamav"]["status"] = "ok"
            result["clamav"]["detail"] = output or "Nessuna minaccia rilevata."
        self.log.emit(f"ClamAV: {result['clamav']['detail']}")

    def _run_virustotal(self, result):
        self.log.emit("Interrogo VirusTotal (hash)...")
        try:
            req = urllib.request.Request(
                VT_API_FILES + result["sha256"],
                headers={"x-apikey": self.vt_api_key, "User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 404:
                result["virustotal"]["status"] = "error"
                result["virustotal"]["detail"] = f"Errore VirusTotal: {e.code} {e.reason}"
                self.log.emit(result["virustotal"]["detail"])
                return
            result["virustotal"]["status"] = "uploaded"
            result["virustotal"]["detail"] = "Hash non presente su VirusTotal, avvio upload..."
            self.log.emit(result["virustotal"]["detail"])
            self._vt_upload(result)
            return
        except Exception as e:
            result["virustotal"]["status"] = "error"
            result["virustotal"]["detail"] = f"Errore di rete VirusTotal: {e}"
            self.log.emit(result["virustotal"]["detail"])
            return

        stats = (data.get("data") or {}).get("attributes", {}).get("last_analysis_stats", {})
        result["virustotal"]["malicious"] = stats.get("malicious", 0)
        result["virustotal"]["suspicious"] = stats.get("suspicious", 0)
        result["virustotal"]["harmless"] = stats.get("harmless", 0)
        result["virustotal"]["undetected"] = stats.get("undetected", 0)
        result["virustotal"]["report_url"] = (
            "https://www.virustotal.com/gui/file/" + result["sha256"])
        if result["virustotal"]["malicious"] > 0 or result["virustotal"]["suspicious"] > 0:
            result["virustotal"]["status"] = "flagged"
        else:
            result["virustotal"]["status"] = "clean"
        self.log.emit(
            f"VirusTotal: malevoli={result['virustotal']['malicious']}, "
            f"sospetti={result['virustotal']['suspicious']}, "
            f"puliti={result['virustotal']['harmless']}")

    def _vt_upload(self, result):
        self.log.emit("Upload del file a VirusTotal (può richiedere qualche minuto)...")
        try:
            boundary = "----wineSandboxGUI" + hashlib.sha1(os.urandom(16)).hexdigest()
            body = _build_multipart_body("file", self.filepath, boundary)
            req = urllib.request.Request(
                VT_API_UPLOAD, data=body,
                headers={"x-apikey": self.vt_api_key,
                         "Content-Type": f"multipart/form-data; boundary={boundary}",
                         "User-Agent": "wine-sandbox-gui"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            analysis_id = (data.get("data") or {}).get("id")
            if analysis_id:
                result["virustotal"]["analysis_id"] = analysis_id
                result["virustotal"]["detail"] = (
                    f"Upload avviato (analysis id: {analysis_id}). "
                    "L'analisi può richiedere 1-2 minuti: il file risulterà in analisi.")
            else:
                result["virustotal"]["detail"] = "Upload avviato ma senza analysis id nella risposta."
            self.log.emit(result["virustotal"]["detail"])
        except Exception as e:
            result["virustotal"]["status"] = "error"
            result["virustotal"]["detail"] = f"Errore durante l'upload a VirusTotal: {e}"
            self.log.emit(result["virustotal"]["detail"])

    def _run_falcon(self, result):
        self.log.emit("VirusTotal ha segnalato positivi: avvio analisi comportamentale Falcon Sandbox...")
        try:
            boundary = "----wineSandboxGUI" + hashlib.sha1(os.urandom(16)).hexdigest()
            body = _build_multipart_body("file", self.filepath, boundary)
            req = urllib.request.Request(
                FALCON_API_SUBMIT, data=body,
                headers={"api-key": self.falcon_api_key,
                         "user-agent": "Falcon Sandbox",
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            job_id = data.get("job_id")
            sha256 = data.get("sha256")
            if job_id:
                result["falcon"]["status"] = "submitted"
                result["falcon"]["report_url"] = (
                    f"https://www.hybrid-analysis.com/sample/{sha256}/{job_id}")
                result["falcon"]["detail"] = (
                    f"Analisi comportamentale avviata (può richiedere fino a ~15 minuti). "
                    f"Job ID: {job_id}")
            else:
                result["falcon"]["status"] = "error"
                result["falcon"]["detail"] = f"Falcon Sandbox: invio non riuscito. {data}"
            self.log.emit(result["falcon"]["detail"])
        except Exception as e:
            result["falcon"]["status"] = "error"
            result["falcon"]["detail"] = f"Errore Falcon Sandbox: {e}"
            self.log.emit(result["falcon"]["detail"])


def _build_multipart_body(field_name, filepath, boundary):
    """Costruisce il body multipart/form-data per un upload di un singolo file."""
    filename = os.path.basename(filepath)
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n').encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    with open(filepath, "rb") as f:
        parts.append(f.read())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Wine Sandbox - Libreria giochi abandonware")
        self.resize(1050, 700)

        self.games = load_json(GAMES_FILE, [])
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self._raw_settings = load_json(SETTINGS_FILE, {})
        self.prefixes = load_json(PREFIXES_FILE, [])
        self.custom_profiles = load_json(CUSTOM_PROFILES_FILE, {})
        self.mounted_images = []  # [{device, path, mount_point}]

        self.process = None          # processo wine-sandbox (giochi/installer)
        self.wine_tool_process = None  # processo per winecfg/wineboot/winetricks diretti
        self.dgvoodoo_thread = None
        self.lutris_thread = None
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
        tabs.addTab(self._build_scan_tab(), "Scansione")
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

        self.btn_suggest_config = QPushButton("💡 Suggerisci configurazione")
        self.btn_suggest_config.clicked.connect(self._on_suggest_config_clicked)
        left_layout.addWidget(self.btn_suggest_config)

        self.game_x11_fallback_cb = QCheckBox(
            "Fallback X11/XWayland per questo gioco (legacy, meno sicuro)")
        self.game_x11_fallback_cb.stateChanged.connect(self._on_game_x11_fallback_changed)
        left_layout.addWidget(self.game_x11_fallback_cb)

        self.btn_scan_file_tab = QPushButton("🛡 Scansiona file...")
        self.btn_scan_file_tab.clicked.connect(self._on_scan_file_clicked)
        left_layout.addWidget(self.btn_scan_file_tab)

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

        self.sec_unshare_ipc = QCheckBox(
            "  Isola i segmenti IPC condivisi (attivo di default col driver Wayland; "
            "rompe solo X11 MIT-SHM, quindi va disattivato se passi a X11)")
        self.sec_unshare_ipc.setChecked(True)
        self.sec_unshare_ipc.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_unshare_ipc)

        self.sec_wayland_only = QCheckBox(
            "  🟢 Solo Wayland, MAI X11 (molto più sicuro - richiede Wine ≥ 10 col driver "
            "nativo Wayland e sessione Wayland; alcuni giochi legacy non compatibili)")
        self.sec_wayland_only.setChecked(True)
        self.sec_wayland_only.stateChanged.connect(self._on_wayland_toggle_changed)
        security_layout.addWidget(self.sec_wayland_only)

        self.sec_x11_fallback = QCheckBox(
            "  🟠 Fallback X11/XWayland per giochi legacy non supportati dal driver "
            "Wayland nativo (meno sicuro: un client X11 può intercettare tastiera/"
            "screenshot di altre app X11 - la sandbox bwrap resta comunque attiva)")
        self.sec_x11_fallback.setChecked(False)
        self.sec_x11_fallback.stateChanged.connect(self._on_x11_fallback_toggle_changed)
        security_layout.addWidget(self.sec_x11_fallback)

        self.sec_dri = QCheckBox("Consenti GPU/accelerazione 3D (serve per la maggior parte dei giochi)")
        self.sec_dri.setChecked(True)
        self.sec_dri.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_dri)

        self.sec_gpu_cap_sysadmin = QCheckBox(
            "  ⚠️ Usa /dev/dri completo + CAP_SYS_ADMIN invece del solo render node "
            "(di default si usa solo renderD1XX, senza privilegi - attiva solo se "
            "la GPU non funziona con driver legacy)")
        self.sec_gpu_cap_sysadmin.setChecked(False)
        self.sec_gpu_cap_sysadmin.stateChanged.connect(self._save_settings_from_ui)
        security_layout.addWidget(self.sec_gpu_cap_sysadmin)

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

        # Il box sicurezza ha molti toggle e schiaccerebbe la lista giochi:
        # vive in una scroll area (stesso approccio del tab Sistema).
        security_scroll = QScrollArea()
        security_scroll.setWidgetResizable(True)
        security_scroll.setFrameShape(QScrollArea.NoFrame)
        security_scroll.setWidget(security_box)
        security_scroll.setMinimumHeight(160)
        left_layout.addWidget(security_scroll)

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

        optical_box = QGroupBox("Dischi ottici fisici (lettore CD/DVD reale)")
        optical_layout = QVBoxLayout(optical_box)
        optical_info = QLabel(
            "Rileva unità ottiche fisiche del sistema e i dischi eventualmente inseriti. "
            "Un disco rilevato qui può essere montato con udisksctl come le immagini."
        )
        optical_info.setWordWrap(True)
        optical_layout.addWidget(optical_info)

        optical_btn_row = QHBoxLayout()
        self.btn_scan_optical = QPushButton("🔄 Rileva dischi ottici")
        self.btn_scan_optical.clicked.connect(self._on_scan_optical_clicked)
        optical_btn_row.addWidget(self.btn_scan_optical)
        optical_layout.addLayout(optical_btn_row)

        self.optical_list = QListWidget()
        self.optical_list.setMaximumHeight(100)
        optical_layout.addWidget(self.optical_list)

        optical_btn_row2 = QHBoxLayout()
        self.btn_mount_optical = QPushButton("💿 Monta disco selezionato")
        self.btn_mount_optical.clicked.connect(self._on_mount_optical_clicked)
        self.btn_mount_optical.setEnabled(False)
        optical_btn_row2.addWidget(self.btn_mount_optical)
        optical_layout.addLayout(optical_btn_row2)
        self.optical_list.itemSelectionChanged.connect(
            lambda: self.btn_mount_optical.setEnabled(self.optical_list.currentItem() is not None))

        layout.addWidget(optical_box)

        layout.addWidget(QLabel("Immagini/dischi attualmente montati:"))
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

        self.btn_open_winetricks_gui = QPushButton("🧩 Apri winetricks (interfaccia grafica)")
        self.btn_open_winetricks_gui.clicked.connect(self._on_open_winetricks_gui)
        tools_btn_row2 = QHBoxLayout()
        tools_btn_row2.addWidget(self.btn_open_winetricks_gui)
        tools_layout.addLayout(tools_btn_row2)

        self.winecfg_ensure_zdrive_cb = QCheckBox(
            "Ricrea automaticamente l'unità Z: (accesso alla radice del filesystem reale) "
            "prima di aprire winecfg/regedit - necessario per accedere a file esterni "
            "(es. installer su USB) mentre configuri il prefix")
        self.winecfg_ensure_zdrive_cb.setChecked(True)
        self.winecfg_ensure_zdrive_cb.stateChanged.connect(self._save_settings_from_ui)
        tools_layout.addWidget(self.winecfg_ensure_zdrive_cb)

        scroll_layout.addWidget(tools_box)

        run_box = QGroupBox("Esegui eseguibile standalone nel prefix")
        run_layout = QVBoxLayout(run_box)
        run_info = QLabel(
            "Lancia un eseguibile Windows (es. un tool di patch, un .bat, un file "
            "scaricato a parte) con questo prefix, DENTRO la sandbox (rete disabilitata, "
            "home nascosta, filesystem in sola lettura)."
        )
        run_info.setWordWrap(True)
        run_layout.addWidget(run_info)
        self.btn_run_standalone = QPushButton("▶ Esegui un eseguibile nel prefix...")
        self.btn_run_standalone.clicked.connect(self._on_run_standalone)
        run_layout.addWidget(self.btn_run_standalone)
        scroll_layout.addWidget(run_box)

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

        # Selettore versione dgVoodoo2
        dgvoodoo_version_row = QHBoxLayout()
        dgvoodoo_version_row.addWidget(QLabel("Versione:"))
        self.dgvoodoo_version_combo = QComboBox()
        self.dgvoodoo_version_combo.addItem("v2.52 (compatibile Wine, output D3D9)", "v2.52")
        self.dgvoodoo_version_combo.addItem("Ultima release (potrebbe non funzionare su Wine)", "latest")
        self.dgvoodoo_version_combo.setCurrentText(
            "v2.52 (compatibile Wine, output D3D9)" if self.settings.get("dgvoodoo_version", "v2.52") == "v2.52"
            else "Ultima release (potrebbe non funzionare su Wine)")
        self.dgvoodoo_version_combo.currentIndexChanged.connect(self._on_dgvoodoo_version_changed)
        dgvoodoo_version_row.addWidget(self.dgvoodoo_version_combo)
        dgvoodoo_version_row.addStretch()
        dgvoodoo_layout.addLayout(dgvoodoo_version_row)

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
    # Tab Scansione
    # ------------------------------------------------------------------
    def _build_scan_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

        intro_box = QGroupBox("Cos'è")
        intro_layout = QVBoxLayout(intro_box)
        intro_info = QLabel(
            "Scansiona un file (es. un installer scaricato) prima di eseguirlo, "
            "a piacimento. Nessuna scansione è automatica o obbligatoria: puoi "
            "anche usarla durante l'installazione di un nuovo gioco (ti verrà "
            "chiesto se vuoi scansionare il setup)."
        )
        intro_info.setWordWrap(True)
        intro_layout.addWidget(intro_info)
        container_layout.addWidget(intro_box)

        tools_box = QGroupBox("Tool di scansione")
        tools_layout = QVBoxLayout(tools_box)

        self.scan_use_clamav_cb = QCheckBox(
            "ClamAV locale (se installato - scansione istantanea, offline, gratuita)")
        self.scan_use_clamav_cb.setChecked(True)
        self.scan_use_clamav_cb.stateChanged.connect(self._save_settings_from_ui)
        tools_layout.addWidget(self.scan_use_clamav_cb)

        self.scan_use_vt_cb = QCheckBox("VirusTotal (hash + upload, richiede API key gratuita)")
        self.scan_use_vt_cb.setChecked(False)
        self.scan_use_vt_cb.stateChanged.connect(self._save_settings_from_ui)
        tools_layout.addWidget(self.scan_use_vt_cb)

        vt_key_row = QHBoxLayout()
        vt_key_row.addWidget(QLabel("  VirusTotal API key:"))
        self.vt_api_key_edit = QLineEdit()
        self.vt_api_key_edit.setEchoMode(QLineEdit.Password)
        self.vt_api_key_edit.editingFinished.connect(self._save_settings_from_ui)
        vt_key_row.addWidget(self.vt_api_key_edit)
        vt_link = QLabel("<a href='https://www.virustotal.com/gui/join-us'>Ottieni una API key gratuita</a>")
        vt_link.setOpenExternalLinks(True)
        vt_key_row.addWidget(vt_link)
        tools_layout.addLayout(vt_key_row)

        self.scan_use_falcon_cb = QCheckBox(
            "Hybrid Analysis / Falcon Sandbox (analisi comportamentale completa, ~15 min, "
            "richiede API key gratuita separata - usata solo se VirusTotal segnala qualcosa)")
        self.scan_use_falcon_cb.setChecked(False)
        self.scan_use_falcon_cb.stateChanged.connect(self._save_settings_from_ui)
        tools_layout.addWidget(self.scan_use_falcon_cb)

        falcon_key_row = QHBoxLayout()
        falcon_key_row.addWidget(QLabel("  Hybrid Analysis API key:"))
        self.falcon_api_key_edit = QLineEdit()
        self.falcon_api_key_edit.setEchoMode(QLineEdit.Password)
        self.falcon_api_key_edit.editingFinished.connect(self._save_settings_from_ui)
        falcon_key_row.addWidget(self.falcon_api_key_edit)
        falcon_link = QLabel("<a href='https://www.hybrid-analysis.com/signup'>Ottieni una API key gratuita</a>")
        falcon_link.setOpenExternalLinks(True)
        falcon_key_row.addWidget(falcon_link)
        tools_layout.addLayout(falcon_key_row)

        container_layout.addWidget(tools_box)

        scan_btn_box = QGroupBox("Esegui una scansione")
        scan_btn_layout = QVBoxLayout(scan_btn_box)
        scan_btn_layout.addWidget(QLabel(
            "Scegli un qualsiasi file e scansionalo con i tool abilitati qui sopra. "
            "Il file non viene eseguito, solo analizzato."))
        self.btn_scan_file = QPushButton("🛡 Scansiona file...")
        self.btn_scan_file.clicked.connect(self._on_scan_file_clicked)
        scan_btn_layout.addWidget(self.btn_scan_file)
        container_layout.addWidget(scan_btn_box)

        layout.addWidget(QLabel("Storico scansioni (questa sessione):"))
        self.scan_history_list = QListWidget()
        layout.addWidget(self.scan_history_list)

        return tab

    # ------------------------------------------------------------------
    # Tab Sistema
    # ------------------------------------------------------------------
    def _build_system_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Le sezioni superiori vivono in una scroll area: la tab ha troppo
        # contenuto per stare in un layout verticale piatto (le group box
        # verrebbero schiacciate). Il log resta sotto, fisso, col suo stretch.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        scroll.setWidget(container)
        scroll.setMinimumHeight(200)
        layout.addWidget(scroll, stretch=1)

        deps_box = QGroupBox("Dipendenze di sistema")
        deps_layout = QVBoxLayout(deps_box)

        self.deps_status_list = QListWidget()
        deps_layout.addWidget(self.deps_status_list)

        self.btn_check_deps = QPushButton("🔍 Verifica dipendenze")
        self.btn_check_deps.clicked.connect(self._on_check_dependencies)
        deps_layout.addWidget(self.btn_check_deps)

        container_layout.addWidget(deps_box)

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

        container_layout.addWidget(launcher_box)

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

        container_layout.addWidget(backup_box)

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

        container_layout.addWidget(config_box)

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

        clamav_path = shutil.which("clamscan")
        if clamav_path:
            item = QListWidgetItem(f"✅ trovato ({clamav_path})  —  clamscan: scansione malware locale (opzionale)")
        else:
            item = QListWidgetItem(
                "⚪ opzionale (non installato)  —  clamscan: scansione malware locale. "
                "Su CachyOS/Arch: sudo pacman -S clamav && sudo freshclam")
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
            self._raw_settings = load_json(SETTINGS_FILE, {})
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
        self.settings["sec_unshare_ipc"] = self.sec_unshare_ipc.isChecked()
        self.settings["sec_wayland_only"] = self.sec_wayland_only.isChecked()
        self.settings["sec_x11_fallback"] = self.sec_x11_fallback.isChecked()
        self.settings["sec_dri"] = self.sec_dri.isChecked()
        self.settings["sec_gpu_cap_sysadmin"] = self.sec_gpu_cap_sysadmin.isChecked()
        self.settings["sec_audio"] = self.sec_audio.isChecked()
        self.settings["sec_loopback"] = self.sec_loopback.isChecked()
        self.settings["sec_allow_network"] = self.sec_allow_network.isChecked()
        self.settings["sec_disable_zdrive"] = self.sec_disable_zdrive.isChecked()
        self.settings["sec_exe_rw"] = self.sec_exe_rw.isChecked()
        self.settings["winecfg_ensure_zdrive"] = self.winecfg_ensure_zdrive_cb.isChecked()
        self.settings["scan_use_clamav"] = self.scan_use_clamav_cb.isChecked()
        self.settings["scan_use_virustotal"] = self.scan_use_vt_cb.isChecked()
        self.settings["scan_use_falcon"] = self.scan_use_falcon_cb.isChecked()
        self.settings["virustotal_api_key"] = self.vt_api_key_edit.text().strip()
        self.settings["falcon_api_key"] = self.falcon_api_key_edit.text().strip()
        self.settings["sec_verify_integrity"] = self.sec_verify_integrity.isChecked()
        self.settings["sec_resource_limits"] = self.sec_resource_limits.isChecked()
        self.settings["sec_memory_limit"] = self.sec_memory_limit_edit.text().strip() or "2G"
        self.settings["sec_cpu_limit"] = self.sec_cpu_limit_edit.text().strip() or "200"
        self.settings["enable_desktop_launcher_creation"] = self.enable_launcher_creation_checkbox.isChecked()
        self.settings["unmount_on_exit"] = self.unmount_on_exit_cb.isChecked()
        self.settings["bchunk_output_dir"] = self.bchunk_output_edit.text().strip()
        self._raw_settings = dict(self.settings)
        save_json(SETTINGS_FILE, self.settings)

    def _load_security_settings_into_ui(self):
        self.sec_hide_home.setChecked(self.settings.get("sec_hide_home", True))
        self.sec_cap_drop.setChecked(self.settings.get("sec_cap_drop", True))
        self.sec_unshare_pid.setChecked(self.settings.get("sec_unshare_pid", True))
        self.sec_wayland_only.setChecked(self.settings.get("sec_wayland_only", True))
        self.sec_x11_fallback.setChecked(self.settings.get("sec_x11_fallback", False))
        # sec_unshare_ipc eredita lo stato di wayland_only se l'utente non l'ha mai
        # salvato esplicitamente: con il driver nativo Wayland non c'è X11/MIT-SHM,
        # quindi l'isolamento IPC è sicuro e viene abilitato di default.
        if "sec_unshare_ipc" not in self._raw_settings:
            self.sec_unshare_ipc.setChecked(self.sec_wayland_only.isChecked())
        else:
            self.sec_unshare_ipc.setChecked(self.settings.get("sec_unshare_ipc", False))
        self.sec_dri.setChecked(self.settings.get("sec_dri", True))
        self.sec_gpu_cap_sysadmin.setChecked(self.settings.get("sec_gpu_cap_sysadmin", False))
        self.sec_audio.setChecked(self.settings.get("sec_audio", True))
        self.sec_loopback.setChecked(self.settings.get("sec_loopback", False))
        self.sec_allow_network.setChecked(self.settings.get("sec_allow_network", False))
        self.sec_disable_zdrive.setChecked(self.settings.get("sec_disable_zdrive", True))
        self.sec_exe_rw.setChecked(self.settings.get("sec_exe_rw", False))
        self.winecfg_ensure_zdrive_cb.setChecked(self.settings.get("winecfg_ensure_zdrive", True))
        self.scan_use_clamav_cb.setChecked(self.settings.get("scan_use_clamav", True))
        self.scan_use_vt_cb.setChecked(self.settings.get("scan_use_virustotal", False))
        self.scan_use_falcon_cb.setChecked(self.settings.get("scan_use_falcon", False))
        self.vt_api_key_edit.setText(self.settings.get("virustotal_api_key", ""))
        self.falcon_api_key_edit.setText(self.settings.get("falcon_api_key", ""))
        self.sec_verify_integrity.setChecked(self.settings.get("sec_verify_integrity", True))
        self.sec_resource_limits.setChecked(self.settings.get("sec_resource_limits", False))
        self.sec_memory_limit_edit.setText(self.settings.get("sec_memory_limit", "2G"))
        self.sec_cpu_limit_edit.setText(self.settings.get("sec_cpu_limit", "200"))
        self.enable_launcher_creation_checkbox.setChecked(
            self.settings.get("enable_desktop_launcher_creation", False))
        self._update_launcher_button_state()

    def _on_wayland_toggle_changed(self):
        if self.sec_wayland_only.isChecked():
            reply = QMessageBox.warning(
                self, "Passare solo a Wayland?",
                "In modalità 'solo Wayland' la sandbox NON espone più X11: il gioco "
                "potrà connettersi esclusivamente al compositor Wayland.\n\n"
                "Vantaggi: X11 è il vettore di sicurezza peggiore (un qualsiasi client "
                "X11 può intercettare tastiera/screenshot di tutte le finestre): "
                "escluderlo elimina questa classe di attacchi.\n\n"
                "Requisiti: Wine ≥ 10 con driver nativo Wayland (winewayland.drv) e "
                "sessione Wayland attiva. Alcuni giochi legacy potrebbero non partire.\n\n"
                "Confermi?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                self.sec_wayland_only.blockSignals(True)
                self.sec_wayland_only.setChecked(False)
                self.sec_wayland_only.blockSignals(False)
                return
        # Se l'utente non ha mai forzato sec_unshare_ipc, lo fa seguire a wayland:
        # attivo col driver Wayland (sicuro, niente X11 MIT-SHM), disattivo in X11.
        if "sec_unshare_ipc" not in self._raw_settings:
            self.sec_unshare_ipc.blockSignals(True)
            self.sec_unshare_ipc.setChecked(self.sec_wayland_only.isChecked())
            self.sec_unshare_ipc.blockSignals(False)
        # Mutua esclusione col fallback X11: se riattivo Wayland, spengo X11.
        if self.sec_wayland_only.isChecked() and self.sec_x11_fallback.isChecked():
            self.sec_x11_fallback.blockSignals(True)
            self.sec_x11_fallback.setChecked(False)
            self.sec_x11_fallback.blockSignals(False)
        self._save_settings_from_ui()

    def _on_x11_fallback_toggle_changed(self):
        if self.sec_x11_fallback.isChecked():
            reply = QMessageBox.warning(
                self, "Usare il fallback X11/XWayland?",
                "Stai attivando il fallback X11/XWayland per giochi legacy non supportati "
                "dal driver Wayland nativo (finestre GDI/GL, es. titoli DirectX8-era).\n\n"
                "⚠️ Meno sicuro di Wayland: un client X11 può intercettare tastiera/"
                "screenshot di tutte le finestre X11 (incluse altre app in XWayland). "
                "La sandbox bwrap resta comunque attiva (rete off, capability droppate, "
                "home nascosta).\n\nUsalo SOLO per titoli che col driver nativo non partono.\n\n"
                "Confermi?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                self.sec_x11_fallback.blockSignals(True)
                self.sec_x11_fallback.setChecked(False)
                self.sec_x11_fallback.blockSignals(False)
                return
        # Fallback X11 attivo: Wayland e l'isolamento IPC vanno spenti (in X11
        # --unshare-ipc spezzerebbe MIT-SHM e farebbe crashare i giochi).
        self.sec_wayland_only.blockSignals(True)
        self.sec_wayland_only.setChecked(False)
        self.sec_wayland_only.blockSignals(False)
        if "sec_unshare_ipc" not in self._raw_settings:
            self.sec_unshare_ipc.blockSignals(True)
            self.sec_unshare_ipc.setChecked(False)
            self.sec_unshare_ipc.blockSignals(False)
        self._save_settings_from_ui()

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

    def _on_dgvoodoo_version_changed(self):
        data = self.dgvoodoo_version_combo.currentData()
        self.settings["dgvoodoo_version"] = data
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
        env.insert("SANDBOX_UNSHARE_IPC", _val("SANDBOX_UNSHARE_IPC", self.sec_unshare_ipc))
        env.insert("SANDBOX_WAYLAND_ONLY", _val("SANDBOX_WAYLAND_ONLY", self.sec_wayland_only))
        env.insert("SANDBOX_X11_FALLBACK", _val("SANDBOX_X11_FALLBACK", self.sec_x11_fallback))
        env.insert("SANDBOX_DRI", _val("SANDBOX_DRI", self.sec_dri))
        env.insert("SANDBOX_GPU_CAP_SYSADMIN", _val("SANDBOX_GPU_CAP_SYSADMIN", self.sec_gpu_cap_sysadmin))
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
        self.btn_suggest_config.setEnabled(has_selection)
        self.game_x11_fallback_cb.setEnabled(has_selection)
        self.game_x11_fallback_cb.blockSignals(True)
        if has_selection:
            game = self.game_list.currentItem().data(Qt.UserRole)
            self.game_x11_fallback_cb.setChecked(bool(game.get("x11_fallback", False)))
        else:
            self.game_x11_fallback_cb.setChecked(False)
        self.game_x11_fallback_cb.blockSignals(False)

    def _on_game_x11_fallback_changed(self):
        item = self.game_list.currentItem()
        if not item:
            self.game_x11_fallback_cb.blockSignals(True)
            self.game_x11_fallback_cb.setChecked(False)
            self.game_x11_fallback_cb.blockSignals(False)
            return
        game = item.data(Qt.UserRole)
        enable = self.game_x11_fallback_cb.isChecked()
        if enable:
            reply = QMessageBox.warning(
                self, "Fallback X11/XWayland per questo gioco?",
                f"'{game['name']}' verrà avviato con il fallback X11/XWayland.\n\n"
                "⚠️ Meno sicuro di Wayland: un client X11 può intercettare tastiera/"
                "screenshot di altre app X11. La sandbox bwrap resta comunque attiva.\n\n"
                "Usalo SOLO se il gioco non parte col driver Wayland nativo.\n\nConfermi?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                self.game_x11_fallback_cb.blockSignals(True)
                self.game_x11_fallback_cb.setChecked(False)
                self.game_x11_fallback_cb.blockSignals(False)
                return
        game["x11_fallback"] = enable
        save_json(GAMES_FILE, self.games)

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
            "unshare_ipc": self.sec_unshare_ipc.isChecked(),
            "wayland_only": self.sec_wayland_only.isChecked(),
            "x11_fallback": self.sec_x11_fallback.isChecked(),
            "dri": self.sec_dri.isChecked(),
            "gpu_cap_sysadmin": self.sec_gpu_cap_sysadmin.isChecked(),
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

        env_overrides = {}
        if game.get("x11_fallback"):
            env_overrides["SANDBOX_X11_FALLBACK"] = "1"
        self._run_process([prefix, exe], env_overrides=env_overrides)

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

    def _on_suggest_config_clicked(self):
        item = self.game_list.currentItem()
        if not item:
            return
        game = item.data(Qt.UserRole)
        game_name = game["name"]

        key, profile, is_custom = find_game_profile(game_name, self.custom_profiles)

        if not profile:
            self._show_no_profile_dialog(game_name, game)
            return

        self._show_profile_dialog(game, key, profile, is_custom)

    def _show_no_profile_dialog(self, game_name, game):
        box = QMessageBox(self)
        box.setWindowTitle("Nessun profilo trovato")
        box.setTextFormat(Qt.RichText)
        box.setTextInteractionFlags(Qt.TextBrowserInteraction)
        label = box.findChild(QLabel, "qt_msgbox_label")
        if label:
            label.setOpenExternalLinks(True)
        box.setText(
            f"Non ho un profilo di configurazione per '{game_name}' nel database locale.<br><br>"
            "Posso cercare automaticamente sull'API pubblica di Lutris (richiede connessione "
            "a lutris.net solo per questa ricerca), oppure puoi cercare manualmente su:<br>"
            f"• <a href='https://www.pcgamingwiki.com/w/index.php?search={game_name.replace(' ', '+')}'>PCGamingWiki</a><br>"
            f"• <a href='https://appdb.winehq.org/objectManager.php?sClass=application&"
            f"iId=&bIsMaintainer=&sTitle={game_name.replace(' ', '+')}'>WineHQ AppDB</a><br>"
            f"• <a href='https://lutris.net/games?q={game_name.replace(' ', '+')}'>Lutris</a><br><br>"
            "Dopo aver configurato il gioco manualmente (winetricks, versione Windows), "
            "puoi salvare un profilo personalizzato per riusarlo in futuro."
        )
        box.setStandardButtons(QMessageBox.Ok)
        lutris_btn = box.addButton("🔍 Cerca automaticamente su Lutris", QMessageBox.ActionRole)
        save_btn = box.addButton("Salva profilo personalizzato...", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() == lutris_btn:
            self._lookup_lutris(game_name, game)
        elif box.clickedButton() == save_btn:
            self._save_custom_profile_dialog(game)

    def _lookup_lutris(self, game_name, game):
        if self.lutris_thread is not None and self.lutris_thread.isRunning():
            QMessageBox.warning(self, "Ricerca in corso", "Attendi che la ricerca corrente finisca.")
            return

        self._wine_log(f"\nCerco '{game_name}' su Lutris...")
        self.lutris_thread = LutrisLookupThread(game_name)
        self.lutris_thread.log.connect(self._wine_log)

        def on_ok(profile):
            self._wine_log("Trovato su Lutris.")
            self._show_profile_dialog(game, None, profile, is_custom=False,
                                       offer_save_as_custom=True)

        def on_error(msg):
            self._wine_log(f"Lutris: {msg}")
            QMessageBox.information(self, "Nessun risultato utilizzabile",
                                     f"{msg}\n\nPuoi provare i link manuali o salvare un profilo "
                                     "personalizzato dopo aver configurato il gioco a mano.")

        self.lutris_thread.finished_ok.connect(on_ok)
        self.lutris_thread.finished_error.connect(on_error)
        self.lutris_thread.start()

    def _show_profile_dialog(self, game, key, profile, is_custom, offer_save_as_custom=False):
        source_label = "Profilo personalizzato" if is_custom else "Database curato (" + ", ".join(profile.get("sources", [])) + ")"
        display_name = profile.get("display_name", game["name"])

        details = f"<b>{display_name}</b><br><i>{source_label}</i><br><br>"
        details += f"<b>Winetricks:</b> {', '.join(profile.get('winetricks', [])) or 'nessuno'}<br>"
        wv = profile.get("windows_version")
        wv_label = next((label for label, code in WINDOWS_VERSIONS if code == wv), wv) if wv else "predefinita"
        details += f"<b>Versione Windows:</b> {wv_label}<br>"
        details += f"<b>dgVoodoo2 consigliato:</b> {'sì' if profile.get('dgvoodoo') else 'no'}<br>"
        cpu = profile.get("cpu_limit_pct")
        details += f"<b>Limite CPU consigliato:</b> {f'{cpu}%' if cpu else 'nessuno'}<br>"
        if profile.get("notes"):
            notes_html = profile['notes'].replace("\n", "<br>")
            details += f"<br><b>Note:</b> {notes_html}<br>"

        box = QMessageBox(self)
        box.setWindowTitle("Configurazione suggerita")
        box.setTextFormat(Qt.RichText)
        box.setText(details)
        apply_btn = box.addButton("Applica al prefix", QMessageBox.AcceptRole)
        save_btn = None
        if offer_save_as_custom:
            save_btn = box.addButton("Salva come profilo personalizzato", QMessageBox.ActionRole)
        box.addButton("Chiudi", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked == apply_btn:
            self._apply_game_profile(game, profile)
        elif save_btn is not None and clicked == save_btn:
            profile_key = _normalize_game_name(game["name"])
            self.custom_profiles[profile_key] = profile
            save_json(CUSTOM_PROFILES_FILE, self.custom_profiles)
            QMessageBox.information(self, "Profilo salvato",
                                     f"Profilo Lutris salvato come personalizzato per '{game['name']}'.")

    def _apply_game_profile(self, game, profile):
        prefix_path = game["prefix"]
        if not os.path.isdir(prefix_path):
            QMessageBox.critical(self, "Errore", f"Il prefix non esiste più:\n{prefix_path}")
            return

        arch = ""
        prefix_entry = next((p for p in self.prefixes if p["path"] == prefix_path), None)
        if prefix_entry:
            arch = prefix_entry.get("arch", "")

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso",
                                 "C'è già un'operazione sul prefix in esecuzione. Attendi che finisca.")
            return

        steps = []

        version_code = profile.get("windows_version")
        if version_code:
            steps.append(("version", version_code))

        verbs = profile.get("winetricks", [])
        if verbs:
            steps.append(("winetricks", verbs))

        cpu = profile.get("cpu_limit_pct")
        if cpu:
            self.sec_resource_limits.setChecked(True)
            self.sec_cpu_limit_edit.setText(str(cpu))
            self._save_settings_from_ui()

        if not steps:
            QMessageBox.information(self, "Niente da applicare",
                                     "Il profilo non specifica winetricks o versione Windows da applicare.")
            return

        self._wine_log(f"\n=== Applico profilo suggerito per '{game['name']}' ===")

        def run_next():
            if not steps:
                self._wine_log("=== Profilo applicato completamente ===\n")
                if profile.get("dgvoodoo"):
                    QMessageBox.information(
                        self, "dgVoodoo2 consigliato",
                        "Questo profilo consiglia dgVoodoo2. Usa il pulsante dedicato nella "
                        "tab Prefix Wine per scaricarlo e installarlo nella cartella del gioco.")
                return
            kind, payload = steps.pop(0)
            if kind == "version":
                self._wine_log(f"Imposto versione Windows: {payload}")
                self._apply_windows_version(prefix_path, arch, payload,
                                            on_done=lambda ok: run_next())
            elif kind == "winetricks":
                self._wine_log(f"Installo winetricks: {', '.join(payload)}")
                self._run_winetricks_setup(prefix_path, payload, on_done=lambda: run_next())

        run_next()

    def _run_winetricks_setup(self, prefix_path, verbs, on_done=None):
        setup_args = ["--setup", prefix_path] + verbs
        ws_program, ws_args = self._wine_sandbox_launch_cmd(setup_args)
        self._wine_log(f"$ {ws_program} {' '.join(ws_args)}")

        self.wine_tool_process = QProcess(self)
        self.wine_tool_process.setProcessEnvironment(self._sandbox_env())
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))

        def finished(code, status):
            self._wine_log(f"\n[winetricks terminato con codice {code}]\n")
            if on_done:
                on_done()

        self.wine_tool_process.finished.connect(finished)
        self.wine_tool_process.start(ws_program, ws_args)

    def _show_reference_page(self, url):
        """Scarica una pagina (PCGamingWiki/WineHQ AppDB/ecc.) e ne mostra il
        testo estratto in una finestra non modale, così l'utente può
        consultarla mentre compila i campi del profilo personalizzato."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Contenuto pagina: {url}")
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"<a href='{url}'>{url}</a> (apri nel browser per link/immagini)"))
        text_view = QPlainTextEdit()
        text_view.setReadOnly(True)
        text_view.setPlainText("Caricamento in corso...")
        layout.addWidget(text_view, stretch=1)

        open_browser_btn = QPushButton("Apri nel browser")
        open_browser_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        layout.addWidget(open_browser_btn)

        dialog.setModal(False)
        dialog.show()

        # Manteniamo un riferimento al thread e alla dialog sull'istanza
        # principale, altrimenti verrebbero garbage-collected appena la
        # funzione ritorna (dialog non modale + thread asincrono).
        if not hasattr(self, "_page_fetch_threads"):
            self._page_fetch_threads = []
        thread = PageFetchThread(url)
        thread.finished_ok.connect(text_view.setPlainText)
        thread.finished_error.connect(text_view.setPlainText)
        thread.finished.connect(lambda: self._page_fetch_threads.remove(thread))
        self._page_fetch_threads.append(thread)
        thread.start()

    def _save_custom_profile_dialog(self, game):
        link, ok = QInputDialog.getText(
            self, "Profilo personalizzato - Link di riferimento (opzionale)",
            "Incolla un link a PCGamingWiki, WineHQ AppDB, Lutris, ecc. per consultarne il "
            "contenuto qui dentro prima di compilare i campi (lascia vuoto per saltare):")
        if not ok:
            return
        link = link.strip()
        if link:
            self._show_reference_page(link)

        verbs_text, ok = QInputDialog.getText(
            self, "Profilo personalizzato - Winetricks",
            "Verbi winetricks separati da spazio (lascia vuoto se nessuno):")
        if not ok:
            return
        verbs = verbs_text.split() if verbs_text.strip() else []

        version_labels = ["(nessuna - non cambiare)"] + [label for label, _ in WINDOWS_VERSIONS]
        version_label, ok = QInputDialog.getItem(
            self, "Profilo personalizzato - Versione Windows",
            "Versione Windows consigliata per questo gioco:", version_labels, 0, False)
        if not ok:
            return
        version_code = None
        if version_label != "(nessuna - non cambiare)":
            version_code = next(code for label, code in WINDOWS_VERSIONS if label == version_label)

        cpu_text, ok = QInputDialog.getText(
            self, "Profilo personalizzato - Limite CPU",
            "Percentuale CPU consigliata (vuoto se nessun limite, es. 30):")
        if not ok:
            return
        cpu_limit = None
        if cpu_text.strip():
            try:
                cpu_limit = int(cpu_text.strip())
            except ValueError:
                cpu_limit = None

        notes, ok = QInputDialog.getMultiLineText(
            self, "Profilo personalizzato - Note", "Note aggiuntive (opzionale):")
        if not ok:
            notes = ""

        key = _normalize_game_name(game["name"])
        self.custom_profiles[key] = {
            "display_name": game["name"],
            "winetricks": verbs,
            "windows_version": version_code,
            "dgvoodoo": False,
            "cpu_limit_pct": cpu_limit,
            "notes": notes.strip(),
            "sources": ["Profilo utente"],
        }
        save_json(CUSTOM_PROFILES_FILE, self.custom_profiles)
        QMessageBox.information(self, "Profilo salvato",
                                 f"Profilo personalizzato salvato per '{game['name']}'.")

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

    def _on_scan_file_clicked(self):
        """Pulsante indipendente 'Scansiona file...': sceglie un qualsiasi file
        (es. un installer scaricato) e lo scansiona a piacimento con i tool
        abilitati nelle impostazioni. Nessuna scansione automatica."""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Seleziona il file da scansionare",
            self.settings["games_root"], "Tutti i file (*)")
        if not filepath:
            return
        self._start_scan(filepath)

    def _start_scan(self, filepath, on_finished=None):
        """Avvia la scansione in background mostrando un dialog non modale di
        avanzamento. Al termine mostra il riepilogo dei risultati e, se fornito,
        invoca on_finished per continuare il flusso (es. installazione)."""
        scan_dialog = QDialog(self)
        scan_dialog.setWindowTitle("Scansione in corso...")
        scan_dialog.resize(650, 420)
        scan_layout = QVBoxLayout(scan_dialog)
        scan_layout.addWidget(QLabel(
            f"Scansione di:\n{filepath}\n\n"
            "Puoi chiudere questa finestra quando vuoi: la scansione "
            "continua in background e il risultato comparirà alla fine."))
        log_view = QPlainTextEdit()
        log_view.setReadOnly(True)
        log_view.setStyleSheet("font-family: monospace; font-size: 10pt;")
        scan_layout.addWidget(log_view, stretch=1)
        close_btn = QPushButton("Chiudi")
        close_btn.clicked.connect(scan_dialog.close)
        scan_layout.addWidget(close_btn)

        scan_dialog.setModal(False)
        scan_dialog.show()

        if not hasattr(self, "_scan_threads"):
            self._scan_threads = []

        def on_scan_finished(result):
            log_view.appendPlainText("\n[scansione completata]")
            self._show_scan_results(result)
            self._add_scan_history_entry(result)
            if on_finished:
                on_finished(result)
            try:
                self._scan_threads.remove(thread)
            except ValueError:
                pass

        thread = ScanThread(
            filepath,
            use_clamav=self.settings.get("scan_use_clamav", True),
            use_virustotal=self.settings.get("scan_use_virustotal", False),
            use_falcon=self.settings.get("scan_use_falcon", False),
            vt_api_key=self.settings.get("virustotal_api_key", ""),
            falcon_api_key=self.settings.get("falcon_api_key", ""),
        )
        thread.log.connect(log_view.appendPlainText)
        thread.finished.connect(on_scan_finished)
        self._scan_threads.append(thread)
        thread.start()

    def _show_scan_results(self, result):
        """Mostra un riepilogo dei risultati della scansione, con un pulsante
        per aprire i report online quando disponibili."""
        filepath = result["file"]
        sha256 = result["sha256"] or "?"
        lines = []
        lines.append(f"File: {os.path.basename(filepath)}")
        lines.append(f"SHA256: {sha256}")
        lines.append("")

        clamav = result["clamav"]
        clamav_label = {
            "ok": "✅ Pulito",
            "infected": "⚠️ MINACCIA RILEVATA",
            "error": "❌ Errore",
            "skipped": "⚪ Non eseguita",
        }.get(clamav["status"], clamav["status"])
        lines.append(f"ClamAV: {clamav_label}")
        if clamav["detail"]:
            lines.append(f"  {clamav['detail']}")

        vt = result["virustotal"]
        if vt["status"] == "clean":
            vt_label = f"✅ Pulito ({vt['malicious']} malevoli, {vt['suspicious']} sospetti)"
        elif vt["status"] == "flagged":
            vt_label = f"⚠️ SEGNALATO ({vt['malicious']} malevoli, {vt['suspicious']} sospetti)"
        elif vt["status"] == "uploaded":
            vt_label = "🔄 In analisi (upload inviato)"
        elif vt["status"] == "error":
            vt_label = "❌ Errore"
        else:
            vt_label = "⚪ Non eseguita"
        lines.append(f"VirusTotal: {vt_label}")
        if vt["detail"] and vt["status"] not in ("clean", "flagged"):
            lines.append(f"  {vt['detail']}")

        falcon = result["falcon"]
        if falcon["status"] == "submitted":
            lines.append("")
            lines.append(f"Hybrid Analysis: 🔄 analisi comportamentale avviata")
            if falcon["detail"]:
                lines.append(f"  {falcon['detail']}")
        elif falcon["status"] == "error":
            lines.append("")
            lines.append(f"Hybrid Analysis: ❌ {falcon['detail']}")

        is_threat = (clamav.get("status") == "infected"
                     or vt.get("status") == "flagged")
        msg = QMessageBox(self)
        msg.setWindowTitle("Risultato scansione")
        msg.setText("\n".join(lines))
        msg.setIcon(QMessageBox.Critical if is_threat else QMessageBox.Information)
        if not is_threat:
            msg.setInformativeText(
                "Se tutte le scansioni sono pulite o non sono state eseguite, "
                "puoi procedere in sicurezza secondo il tuo giudizio.")
        else:
            msg.setInformativeText(
                "La scansione ha rilevato una potenziale minaccia: sconsigliato "
                "procedere. Puoi comunque decidere di continuare a tuo rischio.")
        open_btn = None
        report_url = None
        if vt.get("report_url"):
            report_url = vt["report_url"]
        elif vt.get("analysis_id"):
            report_url = f"https://www.virustotal.com/gui/analysis/{vt['analysis_id']}"
        elif falcon.get("report_url"):
            report_url = falcon["report_url"]
        if report_url:
            open_btn = msg.addButton("Apri report online", QMessageBox.ActionRole)
            open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(report_url)))
        msg.addButton(QMessageBox.Close)
        msg.exec()

    def _add_scan_history_entry(self, result):
        """Aggiunge una voce allo storico scansioni della tab Scansione."""
        if not hasattr(self, "scan_history_list"):
            return
        clamav = result.get("clamav", {})
        vt = result.get("virustotal", {})
        if clamav.get("status") == "infected" or vt.get("status") == "flagged":
            badge = "⚠️"
        elif clamav.get("status") == "ok" or vt.get("status") == "clean":
            badge = "✅"
        else:
            badge = "⚪"
        filename = os.path.basename(result.get("file", ""))
        self.scan_history_list.insertItem(0, f"{badge}  {filename}  ({result.get('sha256', '?')[:12]}…)")

    def _on_install_clicked(self):
        prefix = self._choose_prefix_path("Prefix per l'installazione")
        if not prefix:
            return

        setup_exe, _ = QFileDialog.getOpenFileName(
            self, "Seleziona il file di installazione (setup.exe)",
            self.settings["games_root"], "Eseguibili Windows (*.exe *.EXE)")
        if not setup_exe:
            return

        # Scansione malware opzionale (a piacimento, non bloccante): se abilitata
        # nelle impostazioni, chiede se si vuole scansionare il file prima di
        # installarlo; l'utente può sempre saltare e procedere.
        if (self.settings.get("scan_use_clamav", True)
                or self.settings.get("scan_use_virustotal", False)
                or self.settings.get("scan_use_falcon", False)):
            reply = QMessageBox.question(
                self, "Scansione malware (opzionale)",
                f"Vuoi scansionare '{os.path.basename(setup_exe)}' prima di installarlo?\n\n"
                "La scansione è facoltativa e non blocca nulla: puoi anche saltarla.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                self._start_scan(setup_exe, on_finished=lambda result: self._proceed_install(prefix, setup_exe))
                return
        self._proceed_install(prefix, setup_exe)

    def _proceed_install(self, prefix, setup_exe):
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

    def _on_scan_optical_clicked(self):
        """Rileva unità ottiche fisiche (lettori CD/DVD) e verifica se
        hanno un disco inserito, usando lsblk (dati dal kernel via sysfs,
        nessun accesso diretto al device necessario per la sola rilevazione)."""
        self.optical_list.clear()
        if not shutil.which("lsblk"):
            QMessageBox.critical(
                self, "lsblk non trovato",
                "Il comando 'lsblk' non è disponibile (fa parte di util-linux, "
                "normalmente preinstallato).")
            return

        try:
            result = subprocess.run(
                ["lsblk", "-o", "NAME,TYPE,SIZE,LABEL,MOUNTPOINT", "-J", "-p"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self._log(f"lsblk fallito: {result.stderr.strip()}")
                QMessageBox.critical(self, "Errore", "lsblk ha restituito un errore. Vedi il log.")
                return

            data = json.loads(result.stdout)
            optical_devices = [d for d in data.get("blockdevices", []) if d.get("type") == "rom"]

            if not optical_devices:
                self._log("Nessuna unità ottica fisica rilevata sul sistema.")
                self.optical_list.addItem("Nessuna unità ottica fisica rilevata")
                return

            for dev in optical_devices:
                name = dev.get("name", "?")
                size = dev.get("size") or "0B"
                has_disc = size not in ("0B", "0", None, "")
                label = dev.get("label") or ""
                mountpoint = dev.get("mountpoint")

                if has_disc:
                    status = f"💿 Disco presente" + (f" ({label})" if label else "")
                    if mountpoint:
                        status += f" - già montato su {mountpoint}"
                else:
                    status = "⚪ Vuoto (nessun disco inserito)"

                item_label = f"{name} — {status}"
                item = QListWidgetItem(item_label)
                item.setData(Qt.UserRole, {"device": name, "has_disc": has_disc, "mountpoint": mountpoint})
                if not has_disc:
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                self.optical_list.addItem(item)

            self._log(f"Rilevate {len(optical_devices)} unità ottiche fisiche.")

        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante il rilevamento: {e}")

    def _on_mount_optical_clicked(self):
        item = self.optical_list.currentItem()
        if not item:
            return
        info = item.data(Qt.UserRole)
        if not info or not info.get("has_disc"):
            QMessageBox.information(self, "Nessun disco", "Questa unità non ha un disco inserito.")
            return

        device = info["device"]
        if info.get("mountpoint"):
            QMessageBox.information(self, "Già montato",
                                     f"Il disco è già montato su:\n{info['mountpoint']}")
            return

        if not shutil.which("udisksctl"):
            QMessageBox.critical(self, "udisksctl non trovato",
                                  "Il comando 'udisksctl' non è disponibile.")
            return

        try:
            mount_result = subprocess.run(
                ["udisksctl", "mount", "-b", device],
                capture_output=True, text=True, timeout=15
            )
            self._log(mount_result.stdout.strip())
            if mount_result.returncode != 0:
                self._log(mount_result.stderr.strip())
                QMessageBox.critical(self, "Errore", "Impossibile montare il disco. Vedi il log.")
                return

            mount_match = re.search(r"at (.+)\.?$", mount_result.stdout.strip())
            mount_point = mount_match.group(1).rstrip(".") if mount_match else "(sconosciuto)"

            self.mounted_images.append({"device": device, "path": device, "mount_point": mount_point})
            self._refresh_mounted_list()
            self._log(f"Disco ottico montato su: {mount_point}")
            self._on_scan_optical_clicked()  # aggiorna stato "già montato"

        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante il montaggio: {e}")

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

            # loop-delete si applica solo ai loop device (immagini file);
            # i dischi ottici fisici (/dev/sr*) restano, si smonta solo il filesystem.
            if "/loop" in device:
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
            # giochi sandboxed, ma winecfg needs accesso ai file esterni),
            # solo se il toggle è attivo.
            if self.winecfg_ensure_zdrive_cb.isChecked():
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

        version_labels = [label for label, _ in WINDOWS_VERSIONS]
        default_idx = version_labels.index("Windows 10") if "Windows 10" in version_labels else 0
        version_label, ok = QInputDialog.getItem(
            self, "Versione Windows",
            "Versione Windows da impostare nel prefix (applicata subito dopo la creazione):",
            version_labels, default_idx, False)
        if not ok:
            return
        version_code = next(code for label, code in WINDOWS_VERSIONS if label == version_label)

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

                self._wine_log(f"Imposto versione Windows '{version_label}' ({version_code})...")

                def after_version(success):
                    if not success:
                        self._wine_log("ATTENZIONE: impostazione versione Windows fallita "
                                       "(il prefix resta comunque utilizzabile con la versione predefinita).")

                self._apply_windows_version(prefix_path, arch, version_code, on_done=after_version)
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

    def _apply_windows_version(self, prefix_path, arch, version_code, on_done=None):
        """Imposta la versione Windows scrivendo direttamente nel registry
        (winecfg /v non è affidabile su Wine 10+), poi wineboot -u per
        applicare. on_done(success: bool) viene chiamato al termine."""
        if self.winecfg_ensure_zdrive_cb.isChecked():
            self._ensure_z_drive(prefix_path)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("WINEPREFIX", prefix_path)
        if arch:
            env.insert("WINEARCH", arch)

        reg_cmd = ["reg", "add", "HKCU\\Software\\Wine",
                   "/v", "Version", "/t", "REG_SZ", "/d", version_code, "/f"]

        self._wine_log(f"$ wine {' '.join(reg_cmd)}")
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

                def after_boot_reg(code, st):
                    ok = (code == 0)
                    self._wine_log(
                        f"\n[wineboot terminato con codice {code}]\n"
                        + ("Versione Windows applicata con successo." if ok else
                           "ATTENZIONE: wineboot ha restituito un errore."))
                    if on_done:
                        on_done(ok)

                self.wine_tool_process.finished.connect(after_boot_reg)
                self.wine_tool_process.start("wineboot", ["-u"])
            else:
                self._wine_log(f"ERRORE: reg add ha fallito (codice {exit_code}).")
                if on_done:
                    on_done(False)

        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(after_reg)
        self.wine_tool_process.start("wine", reg_cmd)

    def _on_apply_windows_version(self):
        entry = self._selected_prefix()
        if not entry:
            return
        version_label = self.windows_version_combo.currentText()
        version_code = next(code for label, code in WINDOWS_VERSIONS if label == version_label)

        if self.wine_tool_process is not None and self.wine_tool_process.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Operazione in corso",
                                 "C'è già un'operazione sul prefix in esecuzione. Attendi che finisca.")
            return

        self._wine_log(f"Imposto versione Windows '{version_label}' ({version_code}) su {entry['path']}")
        self._apply_windows_version(entry["path"], entry.get("arch", ""), version_code)

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

    def _on_open_winetricks_gui(self):
        """Apre la GUI di winetricks sul prefix selezionato, in modalità
        --setup (rete abilitata per il download dei verbi, come il flusso
        di installazione dipendenze)."""
        entry = self._selected_prefix()
        if not entry:
            return
        confirm = QMessageBox.question(
            self, "Rete abilitata",
            "La GUI di winetricks deve poter scaricare i componenti richiesti, "
            "quindi verrà eseguita in modalità --setup con la rete ABILITATA "
            "(solo per questa operazione).\n\nProcedere?")
        if confirm != QMessageBox.Yes:
            return
        ws_program, ws_args = self._wine_sandbox_launch_cmd(["--setup", entry["path"], "--gui"])
        self._wine_log(f"\n$ {ws_program} {' '.join(ws_args)}\n")
        self._run_wine_tool(entry["path"], entry.get("arch", ""), ws_program, ws_args[1:], detached=True)

    def _on_run_standalone(self):
        """Esegue un eseguibile Windows scelto dall'utente con il prefix
        selezionato, DENTRO la sandbox (modalità gioco, rete disabilitata)."""
        entry = self._selected_prefix()
        if not entry:
            return
        drive_c = os.path.join(entry["path"], "drive_c")
        start_dir = drive_c if os.path.isdir(drive_c) else entry["path"]
        exe_path, _ = QFileDialog.getOpenFileName(
            self, "Seleziona l'eseguibile da eseguire nel prefix",
            start_dir, "Eseguibili Windows (*.exe *.EXE *.bat *.cmd)")
        if not exe_path:
            return
        if not os.path.isfile(exe_path):
            QMessageBox.critical(self, "Errore", f"File non trovato:\n{exe_path}")
            return
        self._wine_log(f"Avvio eseguibile standalone (sandbox): {exe_path} nel prefix {entry['path']}")
        self._run_process([entry["path"], exe_path])

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
        self.wine_tool_process.setProcessEnvironment(self._sandbox_env())
        self.wine_tool_process.setProcessChannelMode(QProcess.MergedChannels)
        self.wine_tool_process.readyReadStandardOutput.connect(self._on_wine_tool_output)
        self.wine_tool_process.errorOccurred.connect(
            lambda err: self._wine_log(
                f"\n[ERRORE QProcess: {self.wine_tool_process.errorString()}]\n"))
        self.wine_tool_process.finished.connect(
            lambda code, status: self._wine_log(f"\n[winetricks terminato con codice {code}]\n"))
        self.wine_tool_process.start(ws_program, ws_args)

    # ------------------------------------------------------------------
    # dgVoodoo2 (download: v2.52 stabile compatibile Wine / ultima release)
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

        # Scegli versione in base al setting
        version_key = self.settings.get("dgvoodoo_version", "v2.52")
        api_url = DGVOODOO_REPO_API_STABLE if version_key == "v2.52" else DGVOODOO_REPO_API_LATEST
        version_label = "v2.52 (stabile)" if version_key == "v2.52" else "ultima release"

        self._wine_log(f"\nAvvio download dgVoodoo2 {version_label} (architettura {arch_folder}) verso: {target_folder}")

        self.dgvoodoo_thread = DgVoodooDownloadThread(api_url)
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
                if "/loop" in device:
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
