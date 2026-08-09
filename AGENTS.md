# AbandonScan — guida per agenti AI

## Cos'è

Toolchain Linux per giocare giochi retro/abandonware in modo sicuro ed efficiente:
scaricare → scansionare (malware) → eseguire dentro un prefix Wine isolato con
**bubblewrap** (`bwrap`). Principio fondamentale: **massima sicurezza di default,
compatibilità solo se opt-in esplicito** da parte dell'utente.

Tutto il progetto è in **italiano**: commenti, UI, messaggi di log. Mantieni questa
convenzione.

## Due progetti nello stesso repo

Questo repository contiene **due progetti indipendenti** (condividono solo il
repo e la lingua italiana). Non confonderli: hanno scope, dipendenze e scope di
modifica diversi.

### 1) AbandonScan — sandbox giochi (progetto principale)

| File | Ruolo |
|---|---|
| `wine-sandbox` | Wrapper bash che lancia `wine`/`winetricks` dentro un prefix Wine isolato con `bwrap`. Interfaccia a riga di comando del sandboxing. |
| `wine-sandbox-gui.py` | GUI PySide6 (monolite, ~3400 righe) con 5 tab: **Giochi**, **Immagini ottiche**, **Prefix Wine**, **Scansione**, **Sistema**. Traduce i toggle in env `SANDBOX_*` e gestisce i processi in modo asincrono con `QProcess`. |

### 2) scan-game — scansione malware (progetto separato)

| File | Ruolo |
|---|---|
| `scan-game.sh` | Download (URL/Mega/locale), estrazione ricorsiva di archivi e immagini ottiche, scansione ClamAV + VirusTotal + Falcon Sandbox. |
| `scan-game.md` | Documentazione del flusso di scan. |
| `.opencode/command/scan-game.md` | Custom command opencode `/scan-game` che invoca `scan-game.sh` e riassume l'esito. |
| `.devcontainer/` | Container per il flusso di scan (clamav, unzip, p7zip, unrar, megatools, bchunk). |

**`scan-game` è un progetto a sé stante**: non dipende da `wine-sandbox` né dalla
GUI. Può essere usato, modificato e verificato in modo del tutto indipendente.
Le sezioni sotto relative al modello di sicurezza, ai toggle `SANDBOX_*` e ai
gotcha riguardano SOLO AbandonScan; scan-game è disciplinato dalla sua sezione
dedicata più in basso.

## Componenti e flusso (AbandonScan)

- La GUI costruisce l'ambiente con i toggle correnti in `_sandbox_env()`
  (wine-sandbox-gui.py:1733) e li passa come variabili `SANDBOX_*` allo script.
- Lo script `wine-sandbox` legge quelle stesse variabili nell'header (righe 25-68).
  **Queste due fonti di verità devono restare in sincrono.**
- **Chi esegue cosa:**
  - Gioco vero e proprio → modalità default (`wine-sandbox <prefix> <exe>`).
  - Installer (`setup.exe`) → `--install` (Z: abilitata ro, tutto in sola lettura).
  - Creazione prefix (`wineboot -u`) → `--init` (sandbox attiva).
  - `winetricks` → `--setup` (rete ABILITATA, unico caso).
  - `winecfg` e `regedit` → lanciati **senza sandbox** perché non eseguono mai il
    file di gioco non fidato; la GUI li usa per configurare i prefix.
- Processi: un solo `QProcess` alla volta per wine-sandbox; le operazioni di rete
  (Lutris, pagine di riferimento, dgVoodoo, scan) usano `QThread` dedicati.

## Modello di sicurezza

Toggle `SANDBOX_*` (default indicato tra parentesi):

| Variabile | Default | Effetto |
|---|---|---|
| `SANDBOX_HIDE_HOME` | 1 | Home nascosta (tmpfs vuota) invece di bind |
| `SANDBOX_CAP_DROP` | 1 | Droppa tutte le capability Linux |
| `SANDBOX_UNSHARE_PID` | 1 | Isola la visibilità dei processi |
| `SANDBOX_WAYLAND_ONLY` | **1** | Espone SOLO Wayland nativo, X11 mai (vedi sotto) |
| `SANDBOX_UNSHARE_IPC` | 1 con Wayland / 0 in X11 | Isola i segmenti SysV; OFF se si usa X11 |
| `SANDBOX_DRI` | 1 | Passa i render node GPU (`/dev/dri/renderD*`) |
| `SANDBOX_GPU_CAP_SYSADMIN` | 0 | Espone `/dev/dri/card*` intero + CAP_SYS_ADMIN (compatibilità) |
| `SANDBOX_AUDIO` | 1 | Passa i socket PulseAudio/PipeWire |
| `SANDBOX_ALLOW_LOOPBACK` | 0 | Loopback locale, resta isolati da internet |
| `SANDBOX_ALLOW_NETWORK` | 0 | ⚠️ Disattiva l'isolamento di rete (solo compatibilità) |
| `SANDBOX_DISABLE_ZDRIVE` | 1 | Rimuove l'unità Z: dal prefix |
| `SANDBOX_EXE_RW` | 0 | Scrittura nella cartella del gioco (salvataggi) |

Hardening sempre attivo (non disattivabile): `--unshare-cgroup-try`, `/etc/ssh`
mascherato, `/sys` intero in sola lettura, `--unshare-uts`, `--new-session`,
`--die-with-parent`.

**Wayland nativo è il default.** Con `SANDBOX_WAYLAND_ONLY=1` (default sia nello
script sia in `sec_wayland_only: True` nei DEFAULT_SETTINGS) `DISPLAY` viene
svuotato, `winex11.drv=d` (Wine 10+, driver nativo `winewayland.drv`), nessun
socket X11 e nessuna `XAUTHORITY` esposti. X11/XWayland è un fallback opt-in
(`SANDBOX_WAYLAND_ONLY=0`) solo per giochi legacy non supportati dal driver
nativo.

## Convenzioni di codice (AbandonScan)

- Lingua italiana per commenti, stringhe UI, log (vale per entrambi i progetti).
- Nessun framework di test né lint configurati; nessun packaging/setup.py.
- GUI monolitica in un unico file; mantieni lo stile PySide6 esistente
  (QProcess singolo, QThread per rete, helper `load_json`/`save_json`, salvataggio
  impostazioni immediato ad ogni modifica).
- Config in `~/.config/wine-sandbox-gui/` (games.json, settings.json,
  prefixes.json, custom-game-profiles.json); log in
  `~/.local/share/wine-sandbox-gui/`.
- Ogni esecuzione viene tracciata in `launch-history.log` (audit trail) e il
  prefix viene snapshot-diffato prima/dopo (`_snapshot_prefix` /
  `_diff_and_report_integrity`) per verificare l'integrità.

## Verifica

Non esistono test automatizzati né lint. Prima di consegnare modifiche:

```bash
python3 -m py_compile wine-sandbox-gui.py   # AbandonScan (GUI)
bash -n wine-sandbox                         # AbandonScan (script sandbox)
bash -n scan-game.sh                         # scan-game
```

Se introduci un sistema di test/lint, aggiorna questa sezione.

## Gotcha critici (regressioni già avvenute — NON romperli)

- **Ordine dei mount in `bwrap`**: il `--bind $WINEPFX` (read-write) deve venire
  DOPO i ro-bind generici (`/run/media`, `/sys`, `/etc`), perché i path più
  specifici dichiarati dopo vincono su quelli generici. Critico quando il prefix è
  su un disco USB sotto `/run/media`.
- **`--unshare-ipc` OFF in modalità X11**: isolare i segmenti SysV spezza la
  memoria condivisa X11 MIT-SHM (`X_ShmPutImage` → "Unhandled page fault") e fa
  crashare tutti i giochi X11. Con il driver nativo Wayland (default) invece va
  bene ed è attivo.
- **`/sys` intero in sola lettura è obbligatorio**: Wine/Mesa lo usano per
  enumerare CPU e GPU. Senza, Wine casca su llvmpipe (rendering software) e i
  giochi 3D crashano. `--ro-bind /sys /sys` + `--tmpfs /sys/kernel/security`.
- **Z: rimossa di default** (`SANDBOX_DISABLE_ZDRIVE`); ricreata solo in `--install`
  (sola lettura). La GUI la ricrea se l'utente apre `winecfg` (toggle
  `winecfg_ensure_zdrive`).
- **`--init`**: crea la directory prima di `realpath`; `WINEARCH` va impostato
  SOLO al bootstrap del prefix (mai su prefix esistenti: rompe i build
  WoW64-only di Wine 10+).
- **`--disable-userns` NON è attivo** (richiede `--unshare-user`, che fallisce su
  sistemi con `kernel.unprivileged_userns_clone=0`): il rischio di rompere la
  sandbox ovunque supera il beneficio marginale.
- GPU: di default solo i render node (`/dev/dri/renderD*`), nessun
  `CAP_SYS_ADMIN`. card0 intero + CAP_SYS_ADMIN è opt-in per driver legacy.

## scan-game (progetto separato)

Progetto autonomo per la **scansione malware** di giochi prima dell'installazione.
Non tocca `wine-sandbox`, `wine-sandbox-gui.py` né il modello di sicurezza sopra.

**Flusso (`scan-game.sh`)**: rileva la sorgente (file locale, URL http/https via
`curl`, link `mega.nz` via `megadl` con password opzionale) → estrae ricorsivamente
archivi (.zip/.rar/.7z) e immagini ottiche (.iso/.img/.nrg via 7z, .bin/.cue via
bchunk; .mdf/.mds rilevati ma solo scansionati come file singoli, massima
profondità `MAX_DEPTH=5`) → `clamscan -r` ricorsivo su tutto → SHA256 del file
originale → VirusTotal (hash, upload se sconosciuto) → se positivi, analisi
comportamentale Falcon Sandbox (Hybrid Analysis, env Windows 7 32-bit).

- Dipende dalle API key in env: `VT_API_KEY`, `FALCON_API_KEY` (opzionali,
  senza di esse quei passaggi vengono saltati), `FALCON_ENV_ID` (default 100).
- Configurazione dipendenze: `.devcontainer/devcontainer.json` installa
  clamav, unzip, p7zip-full, unrar-free, megatools, bchunk.
- Verifica: `bash -n scan-game.sh`.
- **Regola d'oro**: mai eseguire il file scaricato con wine o altri interpreti.
  Lo scopo è solo scaricare, scansionare e riportare l'esito.
- Uso da opencode: `/scan-game <url|link_mega|percorso> [password_mega]`.

## Esecuzione (AbandonScan)

```bash
python3 wine-sandbox-gui.py
```
