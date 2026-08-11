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
        *.mdf)
            command -v mdf2iso &>/dev/null || {
                echo "  mdf2iso non installato, conversione MDF non disponibile: $src"
                echo "  Installa con: sudo pacman -S mdf2iso"
                return
            }
            local iso_out="${src}.iso"
            mkdir -p "$outdir"
            if mdf2iso "$src" "$iso_out" 2>/dev/null; then
                echo "  Convertito MDF -> ISO: $src"
                extract_recursive "$iso_out" $((depth + 1))
            else
                echo "  ATTENZIONE: conversione MDF fallita per $src (potrebbe essere corrotto o non standard)"
                rmdir "$outdir" 2>/dev/null || true
            fi
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
    curl -# -L -o "$FILENAME" "$INPUT"
    echo ""
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
echo "== 3/5: Calcolo hash SHA256 di tutti i file =="
# Raccogli tutti i file (originale + estratti) con hash deduplicato
ALL_FILES="$(find "$WORKDIR" -type f | sort)"
TOTAL_FILES=$(echo "$ALL_FILES" | wc -l)
echo "File totali da analizzare: $TOTAL_FILES"
# Mappa hash -> percorsi (hash duplicati = file identici, una sola richiesta VT)
declare -A HASH_MAP
declare -A HASH_FILES  # hash -> elenco file (separati da newline)
while IFS= read -r f; do
    h=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)
    [ -z "$h" ] && continue
    HASH_MAP["$h"]=1
    HASH_FILES["$h"]="${HASH_FILES[$h]:-}${f}
"
done <<< "$ALL_FILES"
UNIQUE_HASHES="${#HASH_MAP[@]}"
echo "Hash unici da verificare: $UNIQUE_HASHES"

echo ""
echo "== 4/5: Verifica su VirusTotal (70+ motori, ricorsivo) =="
VT_ANY_FLAGGED=0
VT_CHECKED=0
VT_SKIPPED=0
VT_CLEAN=0
VT_FLAGGED=0

# Limite upload VT: file oltre questa dimensione vengono solo hash-ati, non caricati
VT_UPLOAD_MAX_SIZE=52428800  # 50 MB

check_vt() {
    local sha256="$1"
    local files_list="$2"
    VT_CHECKED=$((VT_CHECKED + 1))
    local resp
    resp="$(curl -sS --request GET \
        --url "https://www.virustotal.com/api/v3/files/${sha256}" \
        --header "x-apikey: ${VT_API_KEY}" 2>/dev/null)"

    if echo "$resp" | grep -q '"error"'; then
        echo "  [${VT_CHECKED}/${UNIQUE_HASHES}] ${sha256:0:12}… → non trovato su VT"
        VT_SKIPPED=$((VT_SKIPPED + 1))
        # Se non trovato, carica il primo file con questo hash
        local first_file
        first_file="$(echo "$files_list" | head -1)"
        if [ -n "$first_file" ] && [ -f "$first_file" ]; then
            local fsize
            fsize=$(stat -c%s "$first_file" 2>/dev/null || echo 0)
            if [ "$fsize" -gt "$VT_UPLOAD_MAX_SIZE" ]; then
                echo "    Upload saltato: file troppo grande ($(( fsize / 1048576 )) MB > $(( VT_UPLOAD_MAX_SIZE / 1048576 )) MB), solo hash verificato"
            else
                echo "    Upload in corso: $(basename "$first_file")"
                curl -# -S --request POST \
                    --url "https://www.virustotal.com/api/v3/files" \
                    --header "x-apikey: ${VT_API_KEY}" \
                    --form "file=@${first_file}" > /dev/null
                echo ""
            fi
        fi
        return 0
    fi

    local malicious suspicious harmless filename
    malicious="$(echo "$resp" | grep -o '"malicious":[0-9]*' | head -1 | cut -d':' -f2)"
    suspicious="$(echo "$resp" | grep -o '"suspicious":[0-9]*' | head -1 | cut -d':' -f2)"
    harmless="$(echo "$resp" | grep -o '"harmless":[0-9]*' | head -1 | cut -d':' -f2)"
    filename="$(echo "$files_list" | head -1 | xargs basename)"

    if [ "${malicious:-0}" -gt 0 ] || [ "${suspicious:-0}" -gt 0 ]; then
        echo "  [${VT_CHECKED}/${UNIQUE_HASHES}] ${sha256:0:12}… ⚠️ ${malicious:-0} malevoli, ${suspicious:-0} sospetti"
        echo "    File: $(echo "$files_list" | tr '\n' ' ')"
        VT_FLAGGED=$((VT_FLAGGED + 1))
        VT_ANY_FLAGGED=1
    else
        echo "  [${VT_CHECKED}/${UNIQUE_HASHES}] ${sha256:0:12}… ✅ pulito (${harmless:-0} motori)"
        VT_CLEAN=$((VT_CLEAN + 1))
    fi
}

if [ -z "${VT_API_KEY:-}" ]; then
    echo "ATTENZIONE: VT_API_KEY non impostata, salto il controllo VirusTotal."
else
    for hash in "${!HASH_MAP[@]}"; do
        check_vt "$hash" "${HASH_FILES[$hash]}"
        # Rispetta il rate limit gratuito (4 richieste/minuto)
        if [ $VT_CHECKED -lt "$UNIQUE_HASHES" ]; then
            sleep 1
        fi
    done
    echo ""
    echo "VT Riepilogo: $VT_CHECKED verificati, $VT_CLEAN puliti, $VT_FLAGGED sospetti, $VT_SKIPPED non trovati"

    if [ "$VT_ANY_FLAGGED" -eq 1 ]; then
        echo ""
        echo "== 5/5: VirusTotal ha segnalato positivi, avvio analisi comportamentale Falcon Sandbox =="
        if [ -z "${FALCON_API_KEY:-}" ]; then
            echo "ATTENZIONE: FALCON_API_KEY non impostata, salto l'analisi Falcon Sandbox."
        else
            FALCON_ENV_ID="${FALCON_ENV_ID:-100}"
            FALCON_RESPONSE="$(curl -sS --request POST \
                --url "https://www.hybrid-analysis.com/api/v2/submit/file" \
                --header "api-key: ${FALCON_API_KEY}" \
                --header "user-agent: Falcon Sandbox" \
                --form "file=@${WORKDIR}/${FILENAME}" \
                --form "environment_id=${FALCON_ENV_ID}")"
            FALCON_JOB_ID="$(echo "$FALCON_RESPONSE" | grep -o '"job_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
            FALCON_SHA256="$(echo "$FALCON_RESPONSE" | grep -o '"sha256":"[^"]*"' | head -1 | cut -d'"' -f4)"
            if [ -n "$FALCON_JOB_ID" ]; then
                echo "Analisi comportamentale avviata su: $(basename "$WORKDIR/$FILENAME")"
                echo "Job ID: $FALCON_JOB_ID"
                echo "Risultati: https://www.hybrid-analysis.com/sample/${FALCON_SHA256}/${FALCON_JOB_ID}"
            else
                echo "ATTENZIONE: invio a Falcon Sandbox non riuscito."
            fi
        fi
    fi
fi

echo ""
echo "== RIEPILOGO =="
echo "Archivio/file originale: $WORKDIR/$FILENAME"
if [ "$FOUND_EXTRACTED" -gt 0 ]; then
    echo "File estratti: $FOUND_EXTRACTED"
fi
if [ -n "${VT_API_KEY:-}" ]; then
    if [ "$VT_ANY_FLAGGED" -eq 1 ]; then
        echo "⚠️  VirusTotal: $VT_FLAGGED file sospetti su $VT_CHECKED verificati"
    else
        echo "✅ VirusTotal: tutti $VT_CHECKED file puliti"
    fi
fi
echo ""
echo "Se tutto pulito, scarica l'intera cartella $WORKDIR dal Codespace con l'esploratore VS Code (tasto destro > Download)."
