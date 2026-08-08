#!/usr/bin/env bash
#
# scan-game.sh - scarica un file, lo scansiona con ClamAV e ne verifica
# l'hash su VirusTotal (70+ motori). Pensato per girare dentro un
# GitHub Codespace dedicato.
#
# Richiede la variabile d'ambiente VT_API_KEY (VirusTotal API key gratuita,
# da impostare come Codespace secret: Settings > Codespaces > Secrets).
#
# Uso: ./scan-game.sh <url_del_file>

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Uso: $0 <url>" >&2
    exit 1
fi

URL="$1"
WORKDIR="$(mktemp -d)"
cd "$WORKDIR"

echo "== 1/4: Download =="
FILENAME="$(basename "$URL")"
curl -sSL -o "$FILENAME" "$URL"
echo "Scaricato: $FILENAME ($(du -h "$FILENAME" | cut -f1))"

echo ""
echo "== 2/4: Scansione ClamAV locale =="
if ! command -v clamscan &>/dev/null; then
    echo "ClamAV non installato, installo..."
    sudo apt-get update -qq && sudo apt-get install -y -qq clamav
    sudo freshclam --quiet
fi
CLAMAV_RESULT="$(clamscan --no-summary "$FILENAME" 2>&1 || true)"
echo "$CLAMAV_RESULT"

echo ""
echo "== 3/4: Calcolo hash SHA256 =="
SHA256="$(sha256sum "$FILENAME" | cut -d' ' -f1)"
echo "SHA256: $SHA256"

echo ""
echo "== 4/4: Verifica su VirusTotal =="
if [ -z "${VT_API_KEY:-}" ]; then
    echo "ATTENZIONE: VT_API_KEY non impostata, salto il controllo VirusTotal."
    echo "Imposta il secret nel Codespace per abilitarlo."
else
    # Prima prova con l'hash (veloce, non serve ricaricare il file se già noto a VT)
    VT_RESPONSE="$(curl -sS --request GET \
        --url "https://www.virustotal.com/api/v3/files/${SHA256}" \
        --header "x-apikey: ${VT_API_KEY}")"

    if echo "$VT_RESPONSE" | grep -q '"error"'; then
        echo "Hash non trovato su VirusTotal, carico il file (può richiedere qualche minuto)..."
        UPLOAD_RESPONSE="$(curl -sS --request POST \
            --url "https://www.virustotal.com/api/v3/files" \
            --header "x-apikey: ${VT_API_KEY}" \
            --form "file=@${FILENAME}")"
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
