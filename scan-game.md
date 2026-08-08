---
description: Scarica, scansiona con ClamAV e verifica su VirusTotal un file da un URL
agent: build
---

Esegui lo script `./scan-game.sh $ARGUMENTS` nel terminale (usa il bash tool).

Nota: se $ARGUMENTS contiene due parti separate da spazio (url e poi una password),
lo script le userà come URL e password Mega rispettivamente - questo è il caso solo
per link mega.nz protetti da password aggiuntiva. Per URL normali o file locali,
passa solo il primo argomento.

Dopo l'esecuzione, riassumi in italiano in modo chiaro:
1. Se ClamAV ha rilevato qualcosa di sospetto
2. Il risultato di VirusTotal (quanti motori su quanti segnalano il file come malevolo/sospetto)
3. Una raccomandazione finale netta: "sembra sicuro procedere" oppure "attenzione, non procedere e valuta con Hybrid Analysis"

Non eseguire mai il file scaricato con wine o altri interpreti: il tuo unico compito è scaricarlo,
scansionarlo e riportare i risultati.
