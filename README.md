# reMarkable -> Zotero

Dieses Projekt nimmt:

1. die saubere Originaldatei (`.epub` oder `.pdf`)
2. die passende reMarkable-Export-PDF

und baut daraus wieder eine annotierte Datei im Originalformat, die sich in
Zotero moeglichst sauber weiterverarbeiten laesst.

## Was das Projekt loest

Der uebliche Schmerzpunkt ist:

- auf dem reMarkable ist ein Text gelb markiert
- in Zotero will man am Ende genau diese Markierung wiedersehen
- und daraus eine Note aus den Anmerkungen bauen

Genau dafuer ist das Skript da.

## Was herauskommt

### Wenn das Original ein PDF ist

- Ausgabe: `*.annotated.pdf`
- Textmarkierungen werden als echte PDF-Highlights geschrieben
- Annotationstexte werden aus den sichtbaren Worttreffern im Original-PDF
  rekonstruiert, nicht aus dem normalisierten Suchstring
- Kontext vor/nach dem reMarkable-Highlight wird genutzt, um angeschnittene
  Anfangs- oder Endwoerter zu korrigieren
- Bild-/Flaechenmarkierungen werden als PDF-Rechtecke geschrieben
- wo Zotero es fuer den Import braucht, setzt das Skript kompatible
  Annotation-Keys auf die einzelnen PDF-Annotationsobjekte
- reMarkable-Farbmarkierungen werden aus den tatsaechlichen Export-Farbwerten
  erkannt; unterstuetzt sind aktuell gelb, gruen, pink, orange sowie einfache
  Fallbacks fuer blau/rot
- wenn reMarkable mehrere getrennte Markerbalken in einem PDF-Drawing-Pfad
  speichert, werden diese in echte Teilrechtecke zerlegt, damit unmarkierter
  Text dazwischen nicht versehentlich mitannotiert wird

### Wenn das Original ein EPUB ist

- Ausgabe: `*.annotated.epub`
- visuelle Highlights werden direkt ins EPUB geschrieben
- zusaetzlich wird `META-INF/calibre_bookmarks.txt` erzeugt, damit Zotero die
  E-Book-Anmerkungen ueber den Calibre-/KOReader-Importpfad lesen kann
- als Fallback entsteht immer auch `*.annotated.notes.md`
- Textmarkierungen profitieren von derselben reMarkable-PDF-Extraktion wie PDF
- Bild-/Grafikmarkierungen koennen bei EPUB nicht als echte seitenbasierte
  Zotero-Image-Annotations rekonstruiert werden, weil EPUB reflowable ist und
  keine stabile PDF-Seitengeometrie besitzt

## Harte Regel fuer EPUB

Bei EPUB gilt nicht mehr: "ungefaehr dieselbe Passage".

Es gilt:

- Wenn in der reMarkable-PDF `Text A` markiert ist, muss im EPUB derselbe
  sichtbare `Text A` markiert sein.
- Nicht mehr Text.
- Nicht weniger Text.
- Fussnotenmarker und Superscript-Ziffern nur dann, wenn sie im
  reMarkable-Highlight wirklich dabei waren.
- Ueberlappende Farben bleiben als ueberlappende Annotationen erhalten. Wenn
  ein ganzer Satz gelb markiert ist und ein Begriff darin gruen, sollen beide
  Bedeutungsebenen erhalten bleiben.

Wenn ein Highlight im EPUB wegen Markup, Fussnote oder Split ueber mehrere
Textlaeufe verteilt ist, darf das Ergebnis intern in mehrere exakte
Teil-Highlights zerlegt werden. Wichtig ist der sichtbare Endzustand.

## Projektentscheidung

Dieses Projekt priorisiert:

- korrekte Annotations in Zotero
- korrekte Farben
- exakte Textgrenzen
- Bild-/Flaechenmarkierungen soweit technisch moeglich

vor strikt vendor-neutralem Verhalten.

Das heisst:

- das Output bleibt ein normales `PDF` oder `EPUB`
- aber dort, wo Zotero fuer den Import zusaetzliche Kompatibilitaet erwartet,
  schreibt das Skript diese bewusst mit

## Installation

```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

## Schnellstart

Mit dem Wrapper:

```bash
./remarkable-zotero "<original.epub|pdf>" "<remarkable-export.pdf>"
```

Oder direkt:

```bash
python3 rm-highlights-to-annotations.py "<original.epub|pdf>" "<remarkable-export.pdf>"
```

Beispiel:

```bash
./remarkable-zotero \
  "Better, Simpler Strategy_ A Value-Based Gu - Felix Oberholzer-Gee.epub" \
  "oberholzer-gee_better_2021.backup.pdf"
```

## Was danach neben der Datei liegt

Je nach Lauf entstehen:

- `*.annotated.epub` oder `*.annotated.pdf`
- `*.annotated.review.json`
- bei EPUB zusaetzlich `*.annotated.notes.md`
- falls noch etwas offen bleibt: `*.annotated.unmatched.json`

In `*.annotated.unmatched.json` stehen jetzt auch Reason-Codes wie:

- `empty_highlight_text`
- `context_too_short`
- `no_candidate_windows`
- `no_fuzzy_match`

## Der Review-Schritt ist Pflicht

`*.annotated.review.json` ist das Pflicht-Artefakt.

- `status = final` bedeutet: keine offenen technischen Restfaelle mehr
- `status = needs_review` bedeutet: nicht fertig

Wichtig:

- Bei `EPUB` reicht `status = final` allein nicht als Vertrauenssignal.
- Danach sollte man gezielt noch die tatsaechlichen Textgrenzen einzelner
  Highlights gegen das Original pruefen.
- Bei `PDF` sollte man nach groesseren Aenderungen oder bei neuen Dokumenttypen
  ebenfalls Beispielseiten rendern und visuell gegen die reMarkable-PDF
  pruefen, besonders fuer kurze Highlights und Bild-/Flaechenmarkierungen.

## Review-Workflow

### 1. Nur extrahieren

```bash
./remarkable-zotero "<original>" "<remarkable.pdf>" --extract-json highlights.json
```

### 2. Bereinigte Highlights wieder einspielen

```bash
./remarkable-zotero "<original>" ignored.pdf --extract-in reviewed.extract.json
```

Die Regel dabei:

- nie raten
- OCR-/Ligatur-Schaeden nur dann korrigieren, wenn der exakte Zieltext im
  Original belegbar ist
- lieber offen lassen als falsch setzen

## Bild- und Grafikmarkierungen

### PDF

Bei PDF kann das Skript Bild-, Grafik- oder Flaechenmarkierungen grundsaetzlich
erhalten, weil Original-PDF und reMarkable-Export beide eine feste
Seitengeometrie haben.

- Texttreffer werden als echte Highlight-Annotations geschrieben.
- Flaechen ohne getroffene Textwoerter werden als PDF-Rechteckannotation
  geschrieben.
- Kleine Farbnaehte zwischen ueberlappenden Markierungen werden verworfen,
  damit daraus keine falschen Bildannotations entstehen.
- Ergebnis muss visuell geprueft werden, besonders bei Diagrammen, Tabellen,
  Marginalien und sehr kurzen Markierungen.

### EPUB

Bei EPUB gibt es keine verlaessliche 1:1-Seitenposition fuer Grafikstellen aus
einer reMarkable-PDF. Darum gilt:

- Textmarkierungen werden in den EPUB-Text uebertragen.
- Echte Bild-/Grafikmarkierungen werden nicht als echte Zotero-Image-
  Annotation an derselben visuellen Stelle garantiert.
- Wenn eine Grafikmarkierung wichtig ist, muss sie im Review explizit geprueft
  und ggf. als Fallback-Notiz dokumentiert werden.
- `*.annotated.notes.md` ist hier der Sicherheitsanker, falls Zotero/EPUB die
  Annotation nicht nativ genug abbilden kann.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Zotero-Import

### PDF in Zotero

1. PDF in Zotero oeffnen oder neu laden
2. externe Annotationen importieren lassen
3. `Notiz aus Anmerkungen hinzufuegen`

### EPUB in Zotero

1. EPUB in Zotero
2. `Datei -> E-Book-Anmerkungen importieren...`
3. danach `Notiz aus Anmerkungen hinzufuegen`

## Grenzen

- kein OCR fuer Bildseiten ohne Textschicht
- EPUB-Bildmarkierungen werden nicht zu echten Zotero-Image-Annotations
- stark kaputte reMarkable-Textschichten brauchen den Review-Schritt
- wenn der Text im EPUB gar nicht als Text existiert, kann kein echtes
  Text-Highlight erzwungen werden

## Wichtige Dateien in diesem Repo

- [rm-highlights-to-annotations.py](rm-highlights-to-annotations.py):
  Hauptskript
- [remarkable-zotero](remarkable-zotero):
  kleiner Wrapper fuer den Standardaufruf
- [CODEX_REMARKABLE_ZOTERO.md](CODEX_REMARKABLE_ZOTERO.md):
  Arbeitsanweisung fuer Codex

## Fuer Codex / KI-Workflow

Wenn du in Codex nur schreibst:

```text
Lies CODEX_REMARKABLE_ZOTERO.md
```

dann soll zuerst nach den zwei benoetigten Dateien gefragt und danach der
komplette Workflow inklusive Pflicht-Review ausgefuehrt werden.
