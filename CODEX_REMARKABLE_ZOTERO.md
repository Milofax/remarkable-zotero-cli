# Codex-Anweisung: reMarkable -> Zotero

Wenn ich dich bitte, diese Datei zu lesen, arbeite bitte genau nach diesem Ablauf:

## Ziel

Verwende den `remarkable-zotero`-Workflow fuer ein Paar aus:

1. Originaldatei (`.epub` oder `.pdf`)
2. passender reMarkable-Export-PDF

Ziel ist eine annotierte Datei im Originalformat, die in Zotero moeglichst
fehlerfrei zu einer Note mit allen Anmerkungen weiterverarbeitet werden kann.

Fuer `EPUB` gilt dabei ab jetzt die harte Regel:

- Wenn in der reMarkable-PDF `Text A` markiert ist, muss im EPUB genau
  derselbe sichtbare `Text A` markiert sein.
- Nicht mehr Text.
- Nicht weniger Text.
- Nicht nur eine ungefaehr passende Passage.

## Projektentscheidung

Prioritaet ist nicht strikt vendor-neutrales Verhalten, sondern:

- vollstaendige Zotero-Uebernahme
- korrekte Texte
- Bild-/Flaechenannotations soweit fuer Zotero technisch moeglich
- Erhalt der Markierungsfarben

Das Output soll also weiterhin ein normales `PDF` oder `EPUB` sein, darf aber
die minimal noetigen Kompatibilitaetsmetadaten enthalten, die fuer den
fehlerfreien Import in Zotero aktuell noetig sind.

## Wichtige Formatregeln

### Wenn das Original ein PDF ist

- Ausgabe ist wieder ein annotiertes `PDF`
- Text-Highlights werden als echte PDF-Highlight-Annotations geschrieben
- Bild-/Flaechenhighlights werden als PDF-Rechteckannotation geschrieben
- wenn fuer den Zotero-Import noetig, duerfen an einzelnen PDF-Annotations-
  objekten Zotero-kompatible Keys gesetzt werden
- reMarkable-Farben muessen aus den Export-Farbwerten erkannt werden; nicht
  annehmen, dass jedes Dokument dieselben RGB-Werte nutzt
- wenn reMarkable mehrere getrennte Markerbalken in einem Drawing-Pfad
  speichert, muessen die echten Teilrechtecke verwendet werden, nicht nur die
  grosse Bounding-Box

### Wenn das Original ein EPUB ist

- Ausgabe ist wieder ein annotiertes `EPUB`
- visuelle Highlights werden direkt im EPUB-Markup eingefuegt
- zusaetzlich werden E-Book-Annotationsmetadaten fuer den Zotero-Import
  geschrieben
- EPUB bekommt keine Zotero-IDs als allgemeines Dateikonzept
- wenn EPUB-seitig etwas nicht robust genug importierbar ist, ist die
  `*.annotated.notes.md` ein verpflichtender Fallback
- Fussnotenmarker, Superscript-Ziffern und aehnliche Randzeichen duerfen nicht
  mit markiert werden, wenn sie in der reMarkable-PDF nicht Teil der
  Markierung waren
- wenn eine reMarkable-Markierung im EPUB wegen Fussnote/Superscript/Markup
  nicht als ein einziger sauberer Textlauf existiert, darf sie in mehrere
  exakte EPUB-Highlights aufgeteilt werden, solange der sichtbar markierte
  Text in Summe genau dem reMarkable-Inhalt entspricht
- Bild-/Grafikmarkierungen aus der reMarkable-PDF koennen bei EPUB nicht als
  echte seitenbasierte Zotero-Image-Annotations an derselben visuellen Stelle
  garantiert werden; dafuer muss mindestens der Markdown-Fallback bzw. ein
  expliziter Review-Hinweis entstehen

## Bild- und Grafikmarkierungen

### PDF

Bei PDF sind Bild-, Grafik- und Flaechenmarkierungen grundsaetzlich
uebertragbar, weil Original-PDF und reMarkable-Export feste Seitenkoordinaten
haben.

- Wenn eine farbige Flaeche Textwoerter trifft, wird daraus ein Text-Highlight.
- Wenn eine farbige Flaeche keine Textwoerter trifft und gross genug ist, wird
  daraus eine PDF-Rechteckannotation.
- Winzige Farbnaehte oder Ueberlappungsreste duerfen nicht als Bildannotation
  ausgegeben werden.
- Bei Diagrammen, Tabellen, Marginalien und kurzen Markierungen muessen
  Beispielseiten gerendert und visuell gegen die reMarkable-PDF geprueft
  werden.

### EPUB

Bei EPUB gibt es keine stabile 1:1-Seitengeometrie fuer Grafikstellen aus der
reMarkable-PDF.

- Textmarkierungen werden in den EPUB-Text uebertragen.
- Echte Bild-/Grafikmarkierungen sind nicht als echte Zotero-Image-Annotation
  an derselben visuellen Stelle garantiert.
- Wenn eine solche Markierung wichtig ist, muss sie als echter Review-Fall
  behandelt und im Fallback dokumentiert werden.
- Nicht behaupten, EPUB-Bildmarkierungen seien gleichwertig zu PDF-
  Rechteckannotations.

## Lokaler Einstiegspunkt

Bevorzuge immer diesen Wrapper:

```bash
/Volumes/DATEN/Coding/remarkable-zotero-cli/remarkable-zotero "<original.epub|pdf>" "<remarkable-export.pdf>"
```

Arbeitsverzeichnis:

```bash
/Volumes/DATEN/Coding/remarkable-zotero-cli
```

## Was du zuerst tun sollst

Wenn ich die Dateipfade noch nicht genannt habe, frage mich in einer einzigen
kurzen Nachricht nach genau diesen zwei Dateien:

1. Welche Originaldatei soll verwendet werden?
2. Welche zugehoerige reMarkable-Export-PDF soll verwendet werden?

Nicht raten.

## Danach

Sobald die Dateien klar sind:

1. Fuehre den `remarkable-zotero`-Workflow aus.
2. Pruefe immer die erzeugte `*.annotated.review.json`.
3. Wenn dort `status = needs_review` steht, mache den KI-Review-Schritt
   verpflichtend weiter und stoppe nicht nach dem ersten Lauf.
4. Nutze fuer Review-Korrekturen nur exakt belegbare Stellen aus dem Original.
5. Wenn ein Fall nicht sicher aufloesbar ist, sage das klar und rate nicht.
6. Bei `EPUB` reicht `review.json` allein nicht als Abschlusskriterium:
   pruefe danach zusaetzlich stichprobenartig bzw. gezielt die tatsaechlich
   markierten Textgrenzen gegen den Originaltext.
7. Bei `PDF` reicht `review.json` bei neuen Dokumenttypen ebenfalls nicht als
   einziges Vertrauenssignal: rendere relevante Beispielseiten und pruefe
   Farben, kurze Markierungen und Bild-/Flaechenmarkierungen visuell.

## Review-Regeln

- `*.annotated.review.json` ist die Quelle der Wahrheit dafuer, ob der Lauf
  technisch noch offene Restfaelle hat
- `status = final` bedeutet: keine offenen technischen Restfaelle mehr
- `status = needs_review` bedeutet: nicht aufhoeren, sondern weiterpruefen
- bei `EPUB` ist nach `status = final` trotzdem noch zu pruefen, ob die
  markierten Textgrenzen exakt stimmen
- bei `PDF` ist nach groesseren Aenderungen oder bei neuen Dokumenten visuell
  gegen die reMarkable-PDF zu pruefen, besonders bei Grafik-/Flaechen-
  markierungen und sehr kurzen Texttreffern
- wenn noetig, zuerst voll extrahieren:

```bash
/Volumes/DATEN/Coding/remarkable-zotero-cli/remarkable-zotero "<original>" "<remarkable.pdf>" --extract-json /tmp/remarkable-zotero.extract.json
```

- wenn korrigierte Highlights erneut eingespielt werden, verwende:

```bash
/Volumes/DATEN/Coding/remarkable-zotero-cli/remarkable-zotero "<original>" ignored.pdf --extract-in "<reviewed.extract.json>"
```

- speichere die bereinigte Datei stabil als `*.reviewed.extract.json`
- veraendere Highlight-Texte nie frei nach Gefuehl
- uebernehme Korrekturen nur, wenn sie direkt aus dem Original belegbar sind
- repariere OCR-/Ligatur-Schaeden aus der reMarkable-PDF auf den exakten
  EPUB-Wortlaut, wenn der Kontext das eindeutig belegt
- wenn ein einzelner RM-Extrakt wegen Fussnote/Superscript sichtbar zwei
  getrennte EPUB-Textlaeufe betrifft, ist ein Split in mehrere Highlights
  erlaubt und oft noetig

## Was als Erfolg gilt

Am Ende soll moeglichst folgendes vorliegen:

- `*.annotated.pdf` oder `*.annotated.epub`
- `*.annotated.review.json`
- falls EPUB: `*.annotated.notes.md`
- falls noch offen: `*.annotated.unmatched.json`

Und inhaltlich:

- Zotero kann aus dem Ergebnis moeglichst fehlerfrei eine Note aus den
  Anmerkungen erzeugen
- bei `EPUB` muss der markierte sichtbare Text exakt zum markierten
  reMarkable-Text passen
- Texte sollen nicht kaputt sein
- Farben sollen erhalten bleiben
- Bild-/Flaechenmarkierungen sollen, soweit technisch moeglich, nicht verloren
  gehen

## Ausgabe

Gib mir am Ende knapp:

- den Status: `final` oder `needs_review`
- die Pfade der erzeugten Artefakte
- falls noch offen: die verbleibenden echten Restfaelle

## Kurzform fuer den Aufruf

Wenn ich nur schreibe:

`Lies CODEX_REMARKABLE_ZOTERO.md`

dann sollst du zuerst nach den zwei benoetigten Dateien fragen, falls sie noch
nicht im Chat genannt wurden.
