#!/usr/bin/env bash
#
# scan-game.sh - scarica (o usa un file già locale), estrae ricorsivamente
# archivi e immagini ottiche, verifica su VirusTotal (70+ motori) e,
# se trovati positivi, analisi comportamentale Falcon Sandbox.
#
# Sorgenti supportate:
#   - URL diretti (http/https)      -> scaricati con curl
#   - Link Mega.nz                  -> scaricati con megadl (megatools)
#   - Percorsi a file già locali     -> usati direttamente
#
# Formati estratti ricorsivamente:
#   - Archivi:        .zip .rar .7z
#   - Immagini ottiche: .iso .img .nrg (via 7z, che legge ISO9660/UDF)
#   - .bin/.cue        (convertiti in ISO con bchunk, poi estratti)
#   - .mdf/.mds        rilevati ma non estratti automaticamente (formato
#                      proprietario Alcohol 120%, tool poco standard su
#                      Linux) - vengono comunque scansionati come file
#
# Uso: ./scan-game.sh <url|link_mega|percorso_file_locale> [password_mega]

set -euo pipefail

MAX_DEPTH=5

if [ $# -lt 1 ]; then
    echo "Uso: $0 <url|link_mega|percorso_file_locale> [password_mega]" >&2
    exit 1
fi

INPUT="$1"
MEGA_PASSWORD="${2:-}"
WORKDIR="$(mktemp -d)"

# ---------------------------------------------------------------------
# Estrazione ricorsiva: dato un file, se è un archivio o immagine ottica
# nota, lo estrae in una cartella accanto a sé e richiama se stessa su
# ogni file appena estratto, fino a MAX_DEPTH livelli.
# ---------------------------------------------------------------------
extract_recursive() {
    local src="$1"
    local depth="$2"

    if [ "$depth" -gt "$MAX_DEPTH" ]; then
        echo "  [profondità massima raggiunta, salto: $src]"
        return
    fi

    local base_lower
    base_lower="$(basename "$src" | tr '[:upper:]' '[:lower:]')"
    local outdir="${src}.estratto"

    case "$base_lower" in
        *.zip)
            command -v unzip &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq unzip; }
            mkdir -p "$outdir"
            unzip -o -q "$src" -d "$outdir" 2>/dev/null && echo "  Estratto (zip): $src"
            ;;
        *.rar)
            command -v unrar-free &>/dev/null || command -v unrar &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq unrar-free; }
            mkdir -p "$outdir"
            if command -v unrar-free &>/dev/null; then
                unrar-free x -y "$src" "$outdir/" 2>/dev/null && echo "  Estratto (rar): $src"
            elif command -v unrar &>/dev/null; then
                unrar x -y "$src" "$outdir/" 2>/dev/null && echo "  Estratto (rar): $src"
            fi
            ;;
        *.7z)
            command -v 7z &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq p7zip-full; }
            mkdir -p "$outdir"
            7z x -y "$src" -o"$outdir" > /dev/null 2>&1 && echo "  Estratto (7z): $src"
            ;;
        *.iso|*.img|*.nrg)
            command -v 7z &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq p7zip-full; }
            mkdir -p "$outdir"
            if 7z x -y "$src" -o"$outdir" > /dev/null 2>&1; then
                echo "  Estratto (immagine ottica ISO9660/UDF): $src"
            else
                echo "  ATTENZIONE: impossibile estrarre $src come immagine ottica standard (potrebbe usare un formato non ISO9660)"
                rmdir "$outdir" 2>/dev/null || true
                return
            fi
            ;;
        *.bin)
            local cue="${src%.bin}.cue"
            local cue_alt="${src%.[bB][iI][nN]}.cue"
            [ -f "$cue" ] || cue="$cue_alt"
            if [ -f "$cue" ]; then
                command -v bchunk &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq bchunk; }
                mkdir -p "$outdir"
                bchunk -v "$src" "$cue" "$outdir/track" > /dev/null 2>&1
                local first_iso
                first_iso="$(ls "$outdir"/track01.iso 2>/dev/null | head -1)"
                if [ -n "$first_iso" ]; then
                    echo "  Convertito bin/cue in ISO: $src"
                    extract_recursive "$first_iso" $((depth + 1))
                else
                    echo "  ATTENZIONE: conversione bin/cue fallita per $src"
                fi
            else
                echo "  File .bin senza .cue corrispondente, non estraibile: $src"
            fi
            return
            ;;
        *.mdf|*.mds)
            echo "  Rilevato formato MDF/MDS ($src): estrazione automatica non supportata, verrà comunque scansionato come file singolo."
            return
            ;;
        *)
            return
            ;;
    esac

    # Ricorsione: processa ogni file appena estratto
    if [ -d "$outdir" ]; then
        find "$outdir" -type f | while read -r f; do
            extract_recursive "$f" $((depth + 1))
        done
    fi
}

echo "== 1/5: Individuazione sorgente e download (se necessario) =="

if [ -f "$INPUT" ]; then
    echo "Rilevato file locale: $INPUT"
    FILENAME="$(basename "$INPUT")"
    cp "$INPUT" "$WORKDIR/$FILENAME"

elif [[ "$INPUT" == *"mega.nz"* || "$INPUT" == *"mega.co.nz"* ]]; then
    echo "Rilevato link Mega.nz"
    command -v megadl &>/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq megatools; }
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
echo "== 2/5: Estrazione ricorsiva (archivi e immagini ottiche) =="
extract_recursive "$WORKDIR/$FILENAME" 0

FOUND_EXTRACTED="$(find "$WORKDIR" -type f ! -path "$WORKDIR/$FILENAME" 2>/dev/null | wc -l)"
if [ "$FOUND_EXTRACTED" -gt 0 ]; then
    echo ""
    echo "File estratti (${FOUND_EXTRACTED} totali):"
    find "$WORKDIR" -type f ! -path "$WORKDIR/$FILENAME" | sed 's/^/  /'
else
    echo "Nessuna estrazione effettuata (formato non compresso o non riconosciuto)."
fi

echo ""
echo "== 3/5: Calcolo hash SHA256 (file originale) =="
SHA256="$(sha256sum "$WORKDIR/$FILENAME" | cut -d' ' -f1)"
echo "SHA256: $SHA256"

echo ""
echo "== 4/5: Verifica su VirusTotal (70+ motori) =="
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

        if [ "${MALICIOUS:-0}" -gt 0 ] || [ "${SUSPICIOUS:-0}" -gt 0 ]; then
            echo ""
            echo "== 5/5: VirusTotal ha segnalato positivi, avvio analisi comportamentale Falcon Sandbox =="
            if [ -z "${FALCON_API_KEY:-}" ]; then
                echo "ATTENZIONE: FALCON_API_KEY non impostata, salto l'analisi Falcon Sandbox."
                echo "Imposta il secret nel Codespace per abilitarla in questi casi."
            else
                FALCON_ENV_ID="${FALCON_ENV_ID:-100}"  # 100 = Windows 7 32-bit, adatto a Win9x/XP-era
                FALCON_RESPONSE="$(curl -sS --request POST \
                    --url "https://www.hybrid-analysis.com/api/v2/submit/file" \
                    --header "api-key: ${FALCON_API_KEY}" \
                    --header "user-agent: Falcon Sandbox" \
                    --form "file=@${WORKDIR}/${FILENAME}" \
                    --form "environment_id=${FALCON_ENV_ID}")"

                FALCON_JOB_ID="$(echo "$FALCON_RESPONSE" | grep -o '"job_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
                FALCON_SHA256="$(echo "$FALCON_RESPONSE" | grep -o '"sha256":"[^"]*"' | head -1 | cut -d'"' -f4)"

                if [ -n "$FALCON_JOB_ID" ]; then
                    echo "Analisi comportamentale avviata (può richiedere fino a ~15 minuti)."
                    echo "Job ID: $FALCON_JOB_ID"
                    echo "Risultati (quando pronti) su:"
                    echo "  https://www.hybrid-analysis.com/sample/${FALCON_SHA256}/${FALCON_JOB_ID}"
                    echo "Oppure controlla da terminale con:"
                    echo "  curl -sS --url https://www.hybrid-analysis.com/api/v2/report/${FALCON_JOB_ID}/summary --header \"api-key: \$FALCON_API_KEY\" --header \"user-agent: Falcon Sandbox\""
                else
                    echo "ATTENZIONE: invio a Falcon Sandbox non riuscito. Risposta ricevuta:"
                    echo "$FALCON_RESPONSE"
                fi
            fi
        fi
    fi
fi

echo ""
echo "== RIEPILOGO =="
echo "Archivio/file originale: $WORKDIR/$FILENAME"
if [ "$FOUND_EXTRACTED" -gt 0 ]; then
    echo "File estratti: $FOUND_EXTRACTED (vedi elenco sopra)"
fi
echo ""
echo "Se tutto pulito, scarica l'intera cartella $WORKDIR dal Codespace con l'esploratore VS Code (tasto destro > Download)."
