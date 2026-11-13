# Note OJS 3.3.0-20 (per segnalazioni a PKP)

Osservazioni empiriche, da confermare con il log PHP e una installazione pulita prima di aprire issue su pkp/pkp-lib o pkp/ojs.

1. Import nativo senza `current_publication_id` (e `<id type="internal">`): articolo senza pubblicazione corrente, indice ok, pagina articolo HTTP 500.
2. Record importati con `<citations>` + galley HTML: HTTP 500 (correlazione su ~10 record).
3. Galley HTML con `<script>` di alcuni MB: il salvataggio si blocca.
4. Import con `<submission_file><embed>`: scheda Results vuota, nessun errore visibile.
5. Data URI fino a 637.000 caratteri in un attributo: funzionano; oltre non testato.
