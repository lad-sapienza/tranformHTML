# tranformHTML → groma2ojs

Dall'export HTML di InDesign di un intero volume di **GROMA** a un'importazione in **OJS 3.3**:
una cartella per articolo, galley HTML pulita, immagini ottimizzate, metadati estratti e un file XML per l'import nativo di OJS.

`groma2ojs.py` è la riscrittura in Python dello script JavaScript originale di questa repository (ora in `legacy/`), adattata al nuovo export di InDesign e con molte funzioni in più. Le convenzioni della galley sono rimaste identiche (`div#newContainer.article-text`, `a.ftpopover`, `p.footnote-foot`, `ul.bibliography`, `ul.gallery` con fancybox), quindi lo stile del sito non cambia.

Testato su GROMA 10 (2025, 7 articoli) e GROMA 11 (2026, 9 articoli), con OJS 3.3.0-20.

## Cosa fa

| Funzione | Dettaglio |
|---|---|
| Divide il volume | un articolo per ogni `h1.Chapter-heading`; sezione dedotta da `Section-Header` (Articles / Notes / Reviews) |
| Estrae i metadati | titolo, autori (nome, cognome, email, affiliazione), abstract, parole chiave, sezione |
| Converte il testo | titoli, paragrafi, tabelle, bibliografia; grassetto/corsivo risolti dal CSS dell'export |
| Note a piè di pagina | da `section._idFootnotes` ai popover `a.ftpopover` + elenco `p.footnote-foot` |
| Figure e didascalie | associazione immagine↔didascalia, galleria con lightbox, link "Fig. N" nel testo che aprono la lightbox |
| Immagini | cartella per articolo, **lato maggiore ≤ 2000 px, WebP lossy qualità 80**, nomi `cognome-fig-NN.webp`; usa gli originali ad alta qualità se indicati |
| Immagini incorporate | opzione per mettere le immagini direttamente nell'HTML (un solo file da caricare) |
| XML per OJS | `ojs-import.xml`, validato contro lo schema nativo 3.3 |
| Report | `report.md` con tutto ciò che va controllato a mano |

## Installazione

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Serve Python ≥ 3.10.

## Uso rapido

Gli export di InDesign sono due elementi: il file `.html` e la cartella `*-web-resources/` accanto (con `css/` e `image/`). Devono restare vicini.

```bash
# 1. Quali articoli trova?
python3 groma2ojs.py "GROMA 11 2026 text.html" --list

# 2. Prova su uno o due articoli
python3 groma2ojs.py "GROMA 11 2026 text.html" -o output/groma11-prova --only 1,3

# 3. Volume completo, con originali, immagini incorporate e XML solo metadati
python3 groma2ojs.py "GROMA 11 2026 text.html" -o output/groma11 \
    --images-dir "/percorso/Dropbox/vol 11" \
    --inline-images --max-side 1600 --quality 75 \
    --xml --xml-no-images \
    --issue-volume 11 --issue-year 2026
```

`--only` accetta numeri (`1,4`) o parti di titolo/cognome (`boschi`).

### Output

```
output/groma11/
├── 01-cognome-parole-del-titolo/
│   ├── article.html        galley da caricare in OJS
│   ├── metadata.json       titolo, autori, abstract, keywords, sezione
│   ├── references.txt      una voce per riga
│   └── images/             solo le immagini di questo articolo
├── …
├── ojs-import.xml          import nativo OJS (volume intero)
├── volume-metadata.json / .csv
└── report.md               avvisi da controllare
```

**Leggi sempre `report.md`**: segnala immagini non trovate, didascalie senza figura, cartelle di originali ambigue, note orfane.

## Immagini

**Routine permanente:** ogni immagine ha lato maggiore ≤ 2000 px (mai ingrandita) ed è salvata in WebP lossy qualità 80. Cambia solo con `--max-side`, `--quality`, `--format webp|jpeg|keep`.

**Originali (`--images-dir`, ripetibile):** gli export di InDesign riducono le immagini, quindi si indica la cartella con gli originali (ricerca ricorsiva). Per ogni articolo la cartella giusta si sceglie così:

1. cartelle il cui percorso contiene il **cognome del primo autore**, con più figure corrispondenti per nome (ignorando spazi e punteggiatura) o per numero (`Figure_3.png` ↔ `Fig. 3`), con confronto visivo di riserva;
2. altrimenti solo cartelle con nomi file identici all'export.

A parità vince la più recente. Le alternative sono nel `report.md`. Se non si trova nulla, si usa l'immagine dell'export. Escluse di default: `GROMA * HTML`, `*-web-resources`, `BOZZE*`, `drafts*`.

> **Buona pratica per il futuro:** una cartella per articolo con il cognome del primo autore nel nome e file chiamati `Fig_01.tif`, `Fig_02.tif`… rende l'abbinamento banale e senza ambiguità.

## Importare in OJS 3.3

Il flusso che ha funzionato, e quello consigliato:

1. In OJS creare **prima il fascicolo** (volume, numero, anno) e verificare che esistano le sezioni con le abbreviazioni di `--section-map` (default `Articles=ART,Notes=Notes,Reviews=Reviews`; per altre sezioni `--section-map` e `--default-section`).
2. Generare l'XML **solo metadati** (`--xml --xml-no-images`).
3. *Management → Tools → Import/Export → Native XML Plugin* e caricare `ojs-import.xml`. Provare prima con 1 articolo (`--only`).
4. Per ogni articolo importato: *Workflow → Production* → caricare `article.html` come galley HTML (e, se c'è, il PDF), aggiungere alt text della copertina, controllare l'anno di copyright.
5. Programmare/pubblicare il fascicolo.

Perché non importare anche la galley dall'XML? Su OJS 3.3.0-20 di Archaeopress l'import di file incorporati fallisce lato server (scheda *Results* vuota, nessun log accessibile). Lo script può comunque generarli (togliere `--xml-no-images`): su un'altra installazione potrebbe funzionare.

### Vincoli scoperti con OJS 3.3.0-20 (importanti)

Sono la parte più preziosa di questa documentazione: ognuno è costato ore.

* **Niente `<script>` nella galley.** Un blocco script da diversi MB blocca il salvataggio. Per questo il link "Fig. N" usa un `onclick` inline che si appoggia a jQuery e fancybox già presenti nel tema.
* **Immagini incorporate = data URI nell'attributo.** Testato fino a 637.000 caratteri per attributo; lo script limita a 600.000 (`--inline-max-chars`) abbassando qualità e dimensione se serve. Il plugin della galley HTML esegue espressioni regolari sull'intero file: tag enormi non sono un problema fino a quella soglia, oltre non è testato.
* **Niente citazioni importate.** I record importati con `<citations>` e una galley HTML hanno dato errore 500 alla pagina dell'articolo. Le references non sono esposte nella UI e le pubblicazioni storiche della rivista non le hanno: l'opzione `--xml-citations` esiste ma è disattivata di proposito. `references.txt` serve per il caso in cui le si voglia incollare a mano.
* **`current_publication_id` obbligatorio.** Nell'XML l'`<article>` deve avere `current_publication_id` uguale all'`<id type="internal">` della pubblicazione, altrimenti l'articolo risulta senza pubblicazione corrente: compare nell'indice ma la pagina dà 500. Lo script lo scrive già.
* **Scheda *Results* vuota** dopo l'import = errore fatale lato server nascosto da OJS. Non significa "nessun articolo".
* Gli stati OJS: 1 in coda, 3 pubblicato, 5 programmato.
* **Non c'è modo di cancellare le submission dall'interfaccia** per l'editor. Se si importa per sbaglio, i record vanno tolti da chi ha accesso al database (Archaeopress): **provare sempre con pochi articoli**.

## Opzioni (riepilogo)

```
-o, --out               cartella di output
--only                  articoli: numeri o parti di titolo/autore
--list                  elenca gli articoli e basta
--format/--quality/--max-side   routine immagini (webp / 80 / 2000)
--images-dir            cartella degli originali (ripetibile)
--keep-image-names      non rinominare in cognome-fig-NN
--inline-images         immagini incorporate come data URI
--inline-max-chars      soglia per attributo (600000)
--strict-css            grassetto/corsivo solo dal CSS, ignora i nomi stile
--xml                   scrive ojs-import.xml
--xml-no-images         XML solo metadati (consigliato)
--xml-citations         include le references come citazioni (sconsigliato)
--xml-split             un XML anche dentro ogni cartella articolo
--section-map           "Articles=ART,Notes=Notes,Reviews=Reviews"
--default-section       sezione per articoli senza sezione (es. Premises)
--issue-volume/--issue-number/--issue-year
--license-url           default CC BY-NC-ND 4.0
--locale                default en_US
```

## Manutenzione: quando cambia l'export di InDesign

L'export di InDesign cambia quando cambiano il template, gli stili di paragrafo o la versione del programma. Il codice è organizzato in modo che i ritocchi siano localizzati. Procedura:

1. Esegui `--list` e leggi `report.md`: la maggior parte dei cambiamenti si manifesta lì (articoli mancanti, autori vuoti, note orfane).
2. Confronta un articolo `article.html` con la pagina originale in browser.
3. Trova la funzione da toccare:

| Sintomo | Dove guardare in `groma2ojs.py` |
|---|---|
| Articoli non divisi / titoli sbagliati | `split_volume`, `extract_metadata` (classi `Chapter-heading`, `Section-Header`, `Author`, `Author-info`, `Abstract`, `Key-Words`) |
| Grassetto/corsivo persi o sbagliati | classe `Styles`, `convert_inline` (`CharOverride-N` risolti da `idGeneratedStyles.css`; nomi stile in `BOLD_NAMES`/`ITALIC_NAMES`) |
| Note a piè di pagina assenti | `collect_footnotes`, `build_article` (`section._idFootnotes`, `a._idFootnoteLink`) |
| Figure/didascalie spaiate | `match_images_to_captions`, `build_article` |
| Tabelle brutte | `clean_table`, `sanitize` |
| Originali non abbinati | `index_images`, `pick_group`, `visual_match` |
| Nuove sezioni | `--section-map` (nessun codice) |
| XML rifiutato da OJS | `write_ojs_xml`; validare con `native.xsd` di PKP |

I nomi di classe di InDesign derivano dagli **stili di paragrafo e carattere** del file `.indd`: se si rinominano, cambiano anche qui. Il modo più economico di evitare rotture è mantenere stabili i nomi degli stili.

### Validare l'XML

```bash
xmllint --noout --schema native.xsd ojs-import.xml
```
`native.xsd` e gli schemi collegati sono in `pkp/ojs` (`plugins/importexport/native/`) e `pkp/pkp-lib` (`plugins/importexport/native/`), ramo `stable-3_3_0`.

## Struttura della repository

```
groma2ojs.py        lo script (un solo file, nessuna installazione)
requirements.txt
docs/               note aggiuntive
legacy/             lo script JavaScript originale (index.html + modules/), non più usato; il suo README è legacy/README.md
```

## Limiti noti

* Il codice è tarato su due volumi: ogni nuovo volume può portare casi nuovi. Guardare sempre `report.md` e provare con pochi articoli.
* Il lato OJS non è stato riprodotto su un'installazione pulita; i vincoli sopra sono osservazioni empiriche su 3.3.0-20.
* Le immagini incorporate appesantiscono l'HTML (fino a ~5 MB per articolo).

## Licenza e autori

Licenza della repository originale. Riscrittura in Python realizzata con Claude Code (Anthropic) nel novembre 2026, a partire dallo script JavaScript di LAD – Sapienza.
