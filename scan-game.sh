#!/usr/bin/env bash
#
# scan-game.sh - scarica (o usa un file già locale) e scansiona con
# ClamAV + verifica su VirusTotal. Supporta:
#   - URL diretti (http/https)      -> scaricati con curl
#   - Link Mega.nz (mega.nz/...)    -> scaricati con megadl (megatools)
#   - Percorsi a file già locali     -> usati direttamente, nessun download
#
# Richiede la variabile d'ambiente VT_API_KEY per il controllo VirusTotal.
#
# Uso: ./scan-game.sh <url_o_percorso_locale> [password_mega]
#
# Il secondo argomento (opzionale) è la password aggiuntiva di un link
# Mega protetto - va usato SOLO se il link mega richiede una password
# extra oltre alla chiave già incorporata nell'URL.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Uso: $0 <url|link_mega|percorso_file_locale> [password_mega]" >&2
    exit 1
fi

INPUT="$1"
MEGA_PASSWORD="${2:-}"
WORKDIR="$(mktemp -d)"

echo "== 1/4: Individuazione sorgente e download (se necessario) =="

if [ -f "$INPUT" ]; then
    # Caso 1: file già locale, nessun download
    echo "Rilevato file locale: $INPUT"
    FILENAME="$(basename "$INPUT")"
    cp "$INPUT" "$WORKDIR/$FILENAME"

elif [[ "$INPUT" == *"mega.nz"* || "$INPUT" == *"mega.co.nz"* ]]; then
    # Caso 2: link Mega.nz, serve megatools
    echo "Rilevato link Mega.nz"
    if ! command -v megadl &>/dev/null; then
        echo "megatools non installato, installo..."
        sudo apt-get update -qq && sudo apt-get install -y -qq megatools
    fi
    cd "$WORKDIR"
    if [ -n "$MEGA_PASSWORD" ]; then
        echo "Uso password fornita per il link Mega protetto"
        megadl --password "$MEGA_PASSWORD" "$INPUT"
    else
        megadl "$INPUT"
    fi
    FILENAME="$(ls "$WORKDIR" | head -1)"
    if [ -z "$FILENAME" ]; then
        echo "Errore: il download da Mega non ha prodotto alcun file." >&2
        if [ -z "$MEGA_PASSWORD" ]; then
            echo "Se il link richiede una password aggiuntiva, riprova con:" >&2
            echo "  $0 \"$INPUT\" \"la_tua_password\"" >&2
        fi
        exit 1
    fi
    cd - > /dev/null

elif [[ "$INPUT" == http://* || "$INPUT" == https://* ]]; then
    # Caso 3: URL diretto normale
    echo "Rilevato URL diretto"
    cd "$WORKDIR"
    FILENAME="$(basename "$INPUT" | sed 's/%20/ /g; s/%28/(/g; s/%29/)/g')"
    curl -sSL -o "$FILENAME" "$INPUT"
    cd - > /dev/null

else
    echo "Errore: input non riconosciuto. Deve essere un URL http(s), un link mega.nz, o un percorso a un file esistente." >&2
    exit 1
fi

echo "File pronto: $WORKDIR/$FILENAME ($(du -h "$WORKDIR/$FILENAME" | cut -f1))"

echo ""
echo "== 2/4: Scansione ClamAV locale =="
if ! command -v clamscan &>/dev/null; then
    echo "ClamAV non installato, installo..."
    sudo apt-get update -qq && sudo apt-get install -y -qq clamav
    sudo freshclam --quiet
fi
CLAMAV_RESULT="$(clamscan --no-summary "$WORKDIR/$FILENAME" 2>&1 || true)"
echo "$CLAMAV_RESULT"

echo ""
echo "== 3/4: Calcolo hash SHA256 =="
SHA256="$(sha256sum "$WORKDIR/$FILENAME" | cut -d' ' -f1)"
echo "SHA256: $SHA256"

echo ""
echo "== 4/4: Verifica su VirusTotal =="
if [ -z "${VT_API_KEY:-}" ]; then
    echo "ATTENZIONE: VT_API_KEY non impostata, salto il controllo VirusTotal."
else
    VT_RESPONSE="$(curl -sS --request GET \
        --url "https://www.virustotal.com/api/v3/files/${SHA256}" \
        --header "x-apikey: ${VT_API_KEY}")"

    if echo "$VT_RESPONSE" | grep -q '"error"'; then
        echo "Hash non trovato su VirusTotal, carico il file (può richiedere qualche minuto)..."
        UPLOAD_RESPONSE="$(curl -sS --request POST \
            --url "https://www.virustotal.com/api/v3/files" \
            --header "x-apikey: ${VT_API_KEY}" \
            --form "file=@${WORKDIR}/${FILENAME}")"
        ANALYSIS_ID="$(echo "$UPLOAD_RESPONSE" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)"
        echo "Analisi in corso (id: $ANALYSIS_ID). Ricontrolla tra 1-2 minuti con:"
        echo "curl -sS --url https://www.virustotal.com/api/v3/analyses/${ANALYSIS_ID} --header \"x-apikey: \$VT_API_KEY\""
    else
        MALICIOUS="$(echo "$VT_RESPONSE" | grep -o '"malicious":[0-9]*' | head -1 | cut -d':' -f2)"
        SUSPICIOUS="$(echo "$VT_RESPONSE" | grep -o '"suspicious":[0-9]*' | head -1 | cut -d':' -f2)"
        HARMLESS="$(echo "$VT_RESPONSE" | grep -o '"harmless":[0-9]*' | head -1 | cut -d':' -f2)"
        echo "Risultato VirusTotal (file già analizzato in precedenza):"
        echo "  Motori che segnalano MALEVOLO:  ${MALICIOUS:-0}"
        echo "  Motori che segnalano SOSPETTO:  ${SUSPICIOUS:-0}"
        echo "  Motori che segnalano PULITO:    ${HARMLESS:-0}"
    fi
fi

echo ""
echo "== RIEPILOGO =="
echo "File: $WORKDIR/$FILENAME"
echo "ClamAV: $([ -z "$CLAMAV_RESULT" ] && echo 'nessuna minaccia rilevata' || echo "$CLAMAV_RESULT")"
echo ""
echo "Se tutto pulito, scarica il file dal Codespace con l'esploratore VS Code (tasto destro > Download)."
