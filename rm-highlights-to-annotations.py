#!/usr/bin/env python3
"""
rm-highlights-to-annotations.py
-------------------------------

Überträgt Highlights aus einem Remarkable-Export-PDF auf die Original-Datei
(EPUB oder PDF), so dass Zotero "Notiz aus Anmerkungen hinzufügen" sauber
funktioniert.

Input:
  1. Original (EPUB oder PDF) — die saubere Quelle
  2. Remarkable-Export-PDF — mit gemalten Highlights

Output:
  - Original-Format annotiert:
      * EPUB → <name>.annotated.epub  (+ META-INF/calibre_bookmarks.txt
        → Zotero: Rechtsklick Item → Datei → "E-Book-Anmerkungen
        importieren…"), plus <name>.annotated.notes.md als Fallback.
      * PDF  → <name>.annotated.pdf mit echten /Highlight-Annotationen.

Projektentscheidung:
    Dieses Skript priorisiert die vollstaendige Uebernahme in Zotero
    ("Notiz aus Anmerkungen hinzufügen") vor strikt vendor-neutralem
    Verhalten. Das Output bleibt ein gueltiges PDF bzw. EPUB im
    Originalformat, darf aber die minimal noetigen Kompatibilitaets-
    Metadaten enthalten, die Zotero fuer fehlerfreie Extraktion von
    Text-, Bild- und Farb-Annotationen aktuell erwartet.

Usage:
    python3 rm-highlights-to-annotations.py <original> <remarkable.pdf>
    python3 rm-highlights-to-annotations.py <original> <remarkable.pdf> -o out

Setup:
    pip3 install --break-system-packages -r requirements.txt
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterator, Optional

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Fehlt: PyMuPDF. pip3 install --break-system-packages -r requirements.txt")
    sys.exit(1)

try:
    from spellchecker import SpellChecker
except ImportError:
    print("Fehlt: pyspellchecker. pip3 install --break-system-packages -r requirements.txt")
    sys.exit(1)

try:
    from lxml import etree, html as lxml_html
except ImportError:
    print("Fehlt: lxml. pip3 install --break-system-packages -r requirements.txt")
    sys.exit(1)

fitz.TOOLS.mupdf_display_errors(False)


# ============================================================
# KONSTANTEN
# ============================================================

HIGHLIGHT_COLORS = {
    "yellow": ((1.0, 0.99, 0.38), (0.05, 0.05, 0.10)),
    "pink":   ((1.0, 0.33, 0.81), (0.05, 0.05, 0.10)),
}
ANNOT_COLOR_MAP = {
    "yellow": (1.0, 0.99, 0.38),
    "pink":   (1.0, 0.33, 0.81),
    "green":  (0.5, 1.0, 0.5),
    "blue":   (0.5, 0.7, 1.0),
    "red":    (1.0, 0.4, 0.4),
}
# Calibre/Zotero akzeptiert: yellow, green, blue, pink, purple
CALIBRE_COLOR_MAP = {
    "yellow": "yellow",
    "pink":   "pink",
    "green":  "green",
    "blue":   "blue",
    "red":    "purple",
}
# CSS-Hex für das Visual-Span-Overlay (zum direkten Lesen im EPUB-Reader)
CSS_HIGHLIGHT_STYLE = {
    "yellow": "background-color: rgba(255,245,120,0.6);",
    "pink":   "background-color: rgba(255,170,210,0.5);",
    "green":  "background-color: rgba(170,240,170,0.5);",
    "blue":   "background-color: rgba(170,200,255,0.5);",
    "red":    "background-color: rgba(255,170,170,0.5);",
}

LINE_GAP_TOLERANCE = 8.0
# A reMarkable export highlight often covers only the lower part of a glyph box.
# 0.15 keeps those partial hits while still rejecting most neighboring words.
# See tests for footnote-adjacent / partial-overlap edge cases.
WORD_HIT_OVERLAP_THRESHOLD = 0.15
# Highlights with the same color but a gap of 4+ words should be treated as
# separate passages. This avoids merging nearby snippets like a sentence and a
# later quote in the same paragraph.
WORD_GROUP_GAP_THRESHOLD = 3
CONTEXT_WINDOW = 160
MATCH_SCORE_THRESHOLD = 0.58
CONFIDENT_MATCH_SCORE = 0.86
MAX_MATCHES_PER_METHOD = 8
BACKTRACK_CONTAINER_PENALTY = 0.06
BACKTRACK_OFFSET_PENALTY = 0.03
FORWARD_ORDER_BONUS = 0.02


# ============================================================
# LIGATUR-REPARATUR
# ============================================================

DEFAULT_SUBSTITUTIONS = {
    "�": ["fi", "ff", "fl", "ffi", "ffl"],
    "\x00": ["fi", "ff", "fl", "ffi", "ffl"],
    "\x14": ["fi", "ff", "fl", "ffi", "ffl"],
    "\x17": ["fi", "ff", "fl", "ffi", "ffl"],
    "\x10": ["fi", "ff", "fl"],
    "\x12": ["ff", "fi", "fl"],
    "\x13": ["fl", "fi", "ff"],
    "\x15": ["ffi", "fi"],
    "\x16": ["ffl", "fl"],
    "\x18": ["fi"], "\x19": ["fi"], "\x1A": ["fi"], "\x1B": ["fi"],
    "\x1C": ["fi"], "\x1D": ["fi"], "\x1E": ["fi"], "\x1F": ["fi"],
    "6":    ["fi", "ff", "fl", "ffi", "ffl"],
    "5":    ["fi", "fl", "ffi"],
    "I":    ["ff", "f", "ffi"],
    "M":    ["ffi", "ff", "fi"],
    "T":    ["ffl", "fl"],
    "X":    ["fl", "ffl"],
    "/":    ["Fl", "fl"],
}
MATCH_ONLY_SUBSTITUTIONS = {
}
MATCH_SUBSTITUTIONS = dict(DEFAULT_SUBSTITUTIONS)
for _char, _replacements in MATCH_ONLY_SUBSTITUTIONS.items():
    existing = MATCH_SUBSTITUTIONS.setdefault(_char, [])
    for _replacement in _replacements:
        if _replacement not in existing:
            existing.append(_replacement)

ALWAYS_REPAIR_CONTROL = set(chr(c) for c in range(0x00, 0x20)
                             if chr(c) not in "\n\r\t ")
ALWAYS_REPAIR_CONTROL.add("�")


class LigatureRepairer:
    def __init__(self, custom_dict_path=None, substitutions=None, verbose=False):
        self.spell_en = SpellChecker(language="en")
        self.spell_de = SpellChecker(language="de")
        self.substitutions = substitutions or DEFAULT_SUBSTITUTIONS
        self.custom_dict_path = custom_dict_path
        self.custom_words = self._load_custom_dict()
        self.verbose = verbose
        self.repaired_count = 0
        self.unresolved: list[tuple[str, str]] = []
        self._unresolved_seen: set[str] = set()

    def _load_custom_dict(self):
        if not self.custom_dict_path or not os.path.exists(self.custom_dict_path):
            return {}
        try:
            with open(self.custom_dict_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_custom_dict(self):
        if not self.custom_dict_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.custom_dict_path)), exist_ok=True)
        with open(self.custom_dict_path, "w") as f:
            json.dump(self.custom_words, f, indent=2, ensure_ascii=False)

    def _dict_check(self, w):
        if not w:
            return False
        if w in self.custom_words or w.lower() in self.custom_words:
            return True
        wl = w.lower()
        return (wl in self.spell_en or wl in self.spell_de
                or w in self.spell_en or w in self.spell_de)

    def is_real_word(self, w):
        if not w:
            return False
        if not w.replace("-", "").replace("'", "").replace("’", "").isalpha():
            return False
        if self._dict_check(w):
            return True
        for suffix in ["'s", "’s"]:
            if w.endswith(suffix) and self._dict_check(w[:-len(suffix)]):
                return True
        if w.endswith("s") and len(w) > 3 and self._dict_check(w[:-1]):
            return True
        if "-" in w:
            parts = [p for p in w.split("-") if p]
            if len(parts) >= 2 and all(
                self._dict_check(p) or
                (p.endswith("s") and self._dict_check(p[:-1])) or
                self._dict_check(p.rstrip("s"))
                for p in parts
            ):
                return True
        # German compound split: NUR für typisch-deutsche Wörter
        # (capitalized OR mit Umlauten/ß). Verhindert False-Positives wie
        # "proffit" = "prof" + "fit".
        is_german_ish = (w[:1].isupper() and not w.isupper()) or \
                        any(c in w for c in "äöüÄÖÜß")
        if is_german_ish and len(w) >= 7:
            for split_at in range(4, len(w) - 3):
                left, right = w[:split_at], w[split_at:]
                if ((self._dict_check(left) or self._dict_check(left.lower()))
                        and (self._dict_check(right) or self._dict_check(right.lower()))):
                    return True
        return False

    def _is_truly_broken(self, word):
        if word.isdigit():
            return False
        if word.isupper():
            return False
        if word.endswith("s") and len(word) > 1 and word[:-1].isupper():
            return False
        n_digits = sum(c.isdigit() for c in word)
        if n_digits > 0 and n_digits >= len(word) - 2:
            return False
        for i, c in enumerate(word):
            if c not in self.substitutions:
                continue
            if c in ALWAYS_REPAIR_CONTROL:
                return True
            if c.isdigit() or c.isalpha():
                left_ok = i > 0 and word[i-1].islower()
                right_ok = i < len(word) - 1 and word[i+1].islower()
                leading_bug = (
                    i == 0
                    and c in {"X"}
                    and right_ok
                )
                titlecase_bug = (
                    i == 1
                    and len(word) > 3
                    and word[0].isupper()
                    and right_ok
                    and not any(ch.isupper() for ch in word[2:])
                )
                if right_ok and (left_ok or titlecase_bug or leading_bug):
                    return True
            else:
                boundary_bug = (
                    i == 0
                    and i < len(word) - 1
                    and word[i+1].islower()
                ) or (
                    0 < i < len(word) - 1
                    and word[i-1] in "-/("
                    and word[i+1].islower()
                )
                if boundary_bug:
                    return True
        return False

    def repair_word(self, word, context=""):
        if not word:
            return word
        has_suspect = any(c in word for c in self.substitutions)
        if not has_suspect:
            return word
        if not self._is_truly_broken(word):
            return word
        if self.is_real_word(word):
            return word
        if word in self.custom_words:
            self.repaired_count += 1
            return self.custom_words[word]
        # Generiere Kandidaten, aber BEWAHRE die Substitutions-Reihenfolge
        # (Priorität: fi vor ffi, 2-Char-Ersatz vor 3-Char-Ersatz).
        candidates = [word]
        for broken, replacements in self.substitutions.items():
            new_candidates = []
            for cand in candidates:
                new_candidates.append(cand)
                if broken in cand:
                    for rep in replacements:
                        new_candidates.append(cand.replace(broken, rep))
            # Deduplizieren, Reihenfolge erhalten
            seen = set()
            candidates = [c for c in new_candidates if not (c in seen or seen.add(c))]
        valid = [c for c in candidates if self.is_real_word(c)]
        if valid:
            self.repaired_count += 1
            # Bevorzuge kürzeste Substitution (fi > ffi), bei Gleichstand
            # Reihenfolge nach Substitutions-Priorität.
            return min(valid, key=len)
        if any(c in word for c in ALWAYS_REPAIR_CONTROL):
            result = word
            for broken in ALWAYS_REPAIR_CONTROL:
                if broken in result and broken in self.substitutions:
                    result = result.replace(broken, self.substitutions[broken][0])
            if word not in self._unresolved_seen:
                self._unresolved_seen.add(word)
                self.unresolved.append((word, context))
            return result
        if word not in self._unresolved_seen:
            self._unresolved_seen.add(word)
            self.unresolved.append((word, context))
        return word

    def repair_text(self, text):
        if not text:
            return text
        bug_chars_escaped = re.escape("".join(self.substitutions.keys()))
        word_re = re.compile(
            rf"[A-Za-zÀ-ɏ0-9{bug_chars_escaped}'’\-]+",
            re.UNICODE
        )
        return word_re.sub(lambda m: self.repair_word(m.group(0), context=text[:80]), text)


# ============================================================
# HIGHLIGHT-EXTRAKTION AUS REMARKABLE-PDF
# ============================================================

@dataclass
class Highlight:
    color: str
    text: str              # repariert (für Display/Fallback), leer wenn Bild
    raw_text: str          # Remarkable-Raw (mit Bug-Chars) — Basis für robuste Suche
    is_image: bool
    rm_page: int           # 0-indexiert
    rm_bbox: tuple         # (x0, y0, x1, y1) auf der Remarkable-Seite
    context_before: str = ""
    context_after: str = ""


def classify_color(color):
    if not color or len(color) < 3:
        return None
    r, g, b = color[:3]
    for name, ((tr, tg, tb), (dr, dg, db)) in HIGHLIGHT_COLORS.items():
        if abs(r - tr) < dr and abs(g - tg) < dg and abs(b - tb) < db:
            return name
    brightness = (r + g + b) / 3
    if brightness < 0.5 or brightness > 0.95:
        return None
    if g > 0.7 and r < 0.7 and b < 0.7:
        return "green"
    if b > 0.7 and r < 0.7 and g < 0.9:
        return "blue"
    if r > 0.7 and g < 0.5 and b < 0.5:
        return "red"
    return None


def group_rects_into_passages(rects):
    by_color = {}
    for rect, color in rects:
        by_color.setdefault(color, []).append(rect)
    passages = []
    for color, color_rects in by_color.items():
        color_rects.sort(key=lambda r: (r.y0, r.x0))
        current = []
        prev_max_y = None
        for r in color_rects:
            if not current:
                current = [r]
                prev_max_y = r.y1
                continue
            if r.y0 - prev_max_y < LINE_GAP_TOLERANCE:
                current.append(r)
                prev_max_y = max(prev_max_y, r.y1)
            else:
                passages.append((color, current))
                current = [r]
                prev_max_y = r.y1
        if current:
            passages.append((color, current))
    return passages


def _group_color_rects_into_word_passages(words, color_rects):
    """Split same-color highlight rects by actual word continuity.

    Rectangles that are close on the page but hit word ranges with a larger gap
    should not be merged into one passage. This is critical when a user marks
    two separate snippets in the same color near each other.
    """
    rect_hits = []
    image_rects = []
    for rect in sorted(color_rects, key=lambda r: (r.y0, r.x0)):
        hit_indices = _passage_word_hit_indices(words, [rect])
        if hit_indices:
            rect_hits.append((rect, sorted(set(hit_indices))))
        else:
            image_rects.append(rect)

    passages = []
    current_rects = []
    current_indices = []
    current_max_idx = None
    for rect, hit_indices in rect_hits:
        hit_min = hit_indices[0]
        hit_max = hit_indices[-1]
        if (
            current_rects
            and current_max_idx is not None
            and hit_min - current_max_idx > WORD_GROUP_GAP_THRESHOLD
        ):
            passages.append((current_rects, sorted(set(current_indices))))
            current_rects = []
            current_indices = []
            current_max_idx = None
        current_rects.append(rect)
        current_indices.extend(hit_indices)
        current_max_idx = hit_max if current_max_idx is None else max(current_max_idx, hit_max)
    if current_rects:
        passages.append((current_rects, sorted(set(current_indices))))

    image_passages = [passage for _color, passage in group_rects_into_passages(
        [(rect, "__image__") for rect in image_rects]
    )]
    return passages, image_passages


def passage_text(page, rects):
    """Extrahiere exakt die Wörter, die von Highlight-Rechtecken getroffen werden.

    Die alte bbox-basierte Clip-Extraktion zog oft unmarkierte Wörter links/rechts
    oder auf benachbarten Zeilen mit hinein. Für den EPUB-Match brauchen wir den
    tatsächlich markierten Wortlauf, nicht nur irgendeinen Text innerhalb der
    umschließenden Bounding-Box.
    """
    hits = _passage_word_hits(page, rects)
    if not hits:
        return ""
    return " ".join(word_info[4] for word_info in hits)


def passage_context(page, passage_rects, window=120):
    """Return (before, after) context around the actual highlighted word run."""
    if not passage_rects:
        return "", ""
    words = page.get_text("words")
    hit_indices = _passage_word_hit_indices(words, passage_rects)
    if not hit_indices:
        return "", ""
    start_idx = min(hit_indices)
    end_idx = max(hit_indices)
    before = [w[4] for w in words[max(0, start_idx - 20):start_idx]]
    after = [w[4] for w in words[end_idx + 1:end_idx + 21]]
    return (" ".join(before), " ".join(after))


def _text_from_word_indices(words, hit_indices):
    if not hit_indices:
        return ""
    return " ".join(words[idx][4] for idx in hit_indices)


def _context_from_word_indices(words, hit_indices, window=20):
    if not hit_indices:
        return "", ""
    start_idx = min(hit_indices)
    end_idx = max(hit_indices)
    before = [w[4] for w in words[max(0, start_idx - window):start_idx]]
    after = [w[4] for w in words[end_idx + 1:end_idx + 1 + window]]
    return " ".join(before), " ".join(after)


def _rect_overlap_ratio(word_rect, highlight_rect) -> float:
    inter = word_rect & highlight_rect
    if inter.is_empty:
        return 0.0
    inter_area = (inter.x1 - inter.x0) * (inter.y1 - inter.y0)
    word_area = max((word_rect.x1 - word_rect.x0) * (word_rect.y1 - word_rect.y0), 1e-6)
    return inter_area / word_area


def _passage_word_hit_indices(words, rects):
    hit_indices = []
    for idx, word_info in enumerate(words):
        word_rect = fitz.Rect(word_info[0], word_info[1], word_info[2], word_info[3])
        best_overlap = max(
            (_rect_overlap_ratio(word_rect, rect) for rect in rects),
            default=0.0,
        )
        if best_overlap >= WORD_HIT_OVERLAP_THRESHOLD:
            hit_indices.append(idx)
    return hit_indices


def _passage_word_hits(page, rects):
    words = page.get_text("words")
    hit_indices = _passage_word_hit_indices(words, rects)
    return [words[idx] for idx in hit_indices]


def scan_unknown_bug_chars(doc):
    from collections import Counter
    found = Counter()
    for page in doc:
        for c in page.get_text():
            cp = ord(c)
            if 0x00 <= cp <= 0x1F and c not in "\n\r\t ":
                if c not in DEFAULT_SUBSTITUTIONS:
                    found[c] += 1
    return found


def extract_highlights_from_rm(rm_pdf_path, repairer, verbose=True):
    doc = fitz.open(rm_pdf_path)
    unknown = scan_unknown_bug_chars(doc)
    if unknown and verbose:
        print(f"  ⚠ Unbekannte Bug-Chars: "
              f"{[(hex(ord(c)), n) for c, n in unknown.most_common()]}")

    highlights: list[Highlight] = []
    stats = {"raw_rects": 0, "by_color": {}, "image": 0, "text": 0}

    for page_idx, page in enumerate(doc):
        words = page.get_text("words")
        by_color = {}
        for d in page.get_drawings():
            color = classify_color(d.get("fill"))
            if not color:
                continue
            r = d.get("rect")
            if not r or r.is_empty:
                continue
            by_color.setdefault(color, []).append(r)
        if not by_color:
            continue
        stats["raw_rects"] += sum(len(rects) for rects in by_color.values())
        for color, color_rects in by_color.items():
            word_passages, image_passages = _group_color_rects_into_word_passages(words, color_rects)
            for passage_rects, hit_indices in word_passages:
                text = _text_from_word_indices(words, hit_indices)
                bbox = fitz.Rect(passage_rects[0])
                for r in passage_rects[1:]:
                    bbox |= r
                bbox_tuple = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
                repaired = repairer.repair_text(text)
                before, after = _context_from_word_indices(words, hit_indices)
                highlights.append(Highlight(
                    color=color, text=repaired, raw_text=text, is_image=False,
                    rm_page=page_idx, rm_bbox=bbox_tuple,
                    context_before=repairer.repair_text(before),
                    context_after=repairer.repair_text(after)))
                stats["text"] += 1
                stats["by_color"][color] = stats["by_color"].get(color, 0) + 1

            for passage_rects in image_passages:
                bbox = fitz.Rect(passage_rects[0])
                for r in passage_rects[1:]:
                    bbox |= r
                bbox_tuple = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
                highlights.append(Highlight(
                    color=color, text="", raw_text="", is_image=True,
                    rm_page=page_idx, rm_bbox=bbox_tuple))
                stats["image"] += 1
                stats["by_color"][color] = stats["by_color"].get(color, 0) + 1

    doc.close()
    if verbose:
        print(f"  Remarkable-PDF: {stats['raw_rects']} Rechtecke → "
              f"{stats['text']} Text-Highlights + {stats['image']} Bild-Highlights "
              f"({dict(stats['by_color'])})")
        print(f"  Reparierte Wörter: {repairer.repaired_count}")
        print(f"  Nicht aufgelöste Wörter: {len(repairer.unresolved)}")
    return highlights, stats


# ============================================================
# TEXT-NORMALISIERUNG (für Matching Original ↔ Remarkable-Text)
# ============================================================

_APOS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "­": "",  # soft hyphen
})


def normalize_for_match(s: str) -> str:
    """Normalisierung für fuzzy Text-Matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_APOS)
    # Remove stray line-break hyphens: "hy-\nphenated" → "hyphenated"
    s = re.sub(r"-\s*\n\s*", "", s)
    # Space around hyphens collapse: "less- expensive" → "less-expensive",
    # "country -specific" → "country-specific"
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


LIGATURE_OPTIONS = ["ffi", "ffl", "fi", "fl", "ff"]  # long first


def build_flexible_needle_pattern(raw_text: str, substitutions: dict) -> str:
    """
    Baue ein Regex-Pattern aus dem ROHEN Remarkable-Text (mit Bug-Chars).
    - Jeder Bug-Char wird zu einer Alternative aller möglichen Ligaturen
    - Normale f/fi/fl/ff-Sequenzen werden TOLERANT gemacht (weil die Repair
      evtl. falsche Ligatur gewählt hat).
    - Whitespace → \\s+
    - Hyphens mit optionalem Whitespace
    - Dashes (-, —, –) äquivalent
    """
    s = unicodedata.normalize("NFKC", raw_text)
    s = s.translate(_APOS)
    s = re.sub(r"-\s*\n\s*", "", s)  # remove line-break hyphens
    s = re.sub(r"\s+", " ", s).strip()

    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        # Bug char?
        if c in substitutions:
            # Alle möglichen Ligaturen (plus den bug-char selbst um
            # Identitäts-Match zu erlauben)
            opts = substitutions[c] + LIGATURE_OPTIONS
            seen = []
            for o in opts:
                if o not in seen:
                    seen.append(o)
            out.append("(?:" + "|".join(re.escape(o) for o in seen) + ")")
            i += 1
            continue
        # Normale "fi", "fl", "ff", "ffi", "ffl" Sequenzen tolerant matchen
        matched = False
        for lig in LIGATURE_OPTIONS:  # longest first
            if s[i:i+len(lig)] == lig:
                # irgendeine ligature/variante
                alts = LIGATURE_OPTIONS  # alle Varianten
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i += len(lig)
                matched = True
                break
        if matched:
            continue
        # Whitespace
        if c.isspace():
            out.append(r"\s+")
            # consume any additional whitespace
            while i + 1 < n and s[i+1].isspace():
                i += 1
            i += 1
            continue
        # Hyphen / dash: erlaube ANY dash char, optional whitespace um herum
        if c in "-–—":
            # bereits collapsed, aber toleriere EPUB-Varianten
            out.append(r"\s*[-–—−]\s*")
            i += 1
            continue
        # Apostrophes: auch nach Normalisierung tolerant
        if c == "'":
            out.append(r"[’'‘]")
            i += 1
            continue
        # Quotes
        if c == '"':
            out.append(r"[“”\"]")
            i += 1
            continue
        # Normaler Char
        out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass
class TextMatch:
    container_index: int
    start: int
    end: int
    score: float
    matched_text: str
    method: str


@dataclass
class MatchFailure:
    reason: str
    candidate_count: int = 0
    best_score: Optional[float] = None
    best_method: Optional[str] = None


@dataclass
class PdfMatchResult:
    quads: list
    page: object
    matched_text: str
    last_location: tuple[int, int]


def _build_unmatched_entry(highlight: Highlight,
                           failure: Optional[MatchFailure] = None) -> dict:
    entry = {
        "color": highlight.color,
        "rm_page": highlight.rm_page + 1,
        "raw_text": highlight.raw_text,
        "repaired_text": highlight.text,
        "context_before": highlight.context_before,
        "context_after": highlight.context_after,
        "reason": failure.reason if failure else "unknown",
    }
    if failure and failure.candidate_count:
        entry["candidate_count"] = failure.candidate_count
    if failure and failure.best_score is not None:
        entry["best_score"] = round(failure.best_score, 3)
    if failure and failure.best_method:
        entry["best_method"] = failure.best_method
    return entry


def _iter_literal_spans(text: str, needle: str,
                        max_matches: int = MAX_MATCHES_PER_METHOD):
    if not text or not needle:
        return
    text_cmp = text.lower()
    needle_cmp = needle.lower()
    start = 0
    found = 0
    while found < max_matches:
        idx = text_cmp.find(needle_cmp, start)
        if idx < 0:
            break
        yield idx, idx + len(needle)
        found += 1
        start = idx + 1


def _iter_regex_spans(text: str, regex: re.Pattern,
                      max_matches: int = MAX_MATCHES_PER_METHOD):
    if not text or regex is None:
        return
    for found, match in enumerate(regex.finditer(text), start=1):
        yield match.start(), match.end()
        if found >= max_matches:
            break


def _sequence_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_cmp = left.lower()
    right_cmp = right.lower()
    if left_cmp == right_cmp:
        return 1.0
    return SequenceMatcher(None, left_cmp, right_cmp).ratio()


def _context_ratio(expected: str, actual: str, *, tail: bool) -> float:
    expected_norm = normalize_for_match(expected)
    actual_norm = normalize_for_match(actual)
    if not expected_norm:
        return 0.5
    if not actual_norm:
        return 0.0
    expected_slice = expected_norm[-CONTEXT_WINDOW:] if tail else expected_norm[:CONTEXT_WINDOW]
    actual_slice = actual_norm[-CONTEXT_WINDOW:] if tail else actual_norm[:CONTEXT_WINDOW]
    expected_slice_cmp = expected_slice.lower()
    actual_slice_cmp = actual_slice.lower()
    if not actual_slice:
        return 0.0
    if expected_slice_cmp in actual_slice_cmp or actual_slice_cmp in expected_slice_cmp:
        return 1.0
    return _sequence_ratio(expected_slice, actual_slice)


def _looks_broken_token(token: str) -> bool:
    if not token or len(token) < 3:
        return True
    if any(ch in ALWAYS_REPAIR_CONTROL for ch in token):
        return True
    if re.search(r"[a-zäöüß][A-Z0-9/][a-zäöüß]", token):
        return True
    return "/" in token


def _segment_anchor_variants(norm_text: str, segment: list[tuple[str, int, int]]):
    if not segment:
        return []
    token_windows = []
    segment_len = len(segment)
    if segment_len <= 8:
        token_windows.append(segment)
    else:
        token_windows.extend([
            segment[:8],
            segment[max(0, segment_len // 2 - 4):min(segment_len, segment_len // 2 + 4)],
            segment[-8:],
        ])
    anchors = []
    seen = set()
    for window in token_windows:
        if not window:
            continue
        start = window[0][1]
        end = window[-1][2]
        anchor = norm_text[start:end].strip()
        if len(anchor) < 16 or anchor in seen:
            continue
        seen.add(anchor)
        anchors.append((anchor, start))
    return anchors


def _extract_anchor_segments(text: str):
    norm_text = normalize_for_match(text)
    if not norm_text:
        return []
    token_infos = [
        (match.group(0), match.start(), match.end())
        for match in re.finditer(r"\S+", norm_text)
    ]
    segments = []
    current = []
    for token_info in token_infos:
        token = token_info[0]
        if _looks_broken_token(token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token_info)
    if current:
        segments.append(current)
    if not segments:
        return []

    ordered_segments = [segments[0]]
    longest = max(segments, key=lambda seg: seg[-1][2] - seg[0][1])
    if longest is not ordered_segments[0]:
        ordered_segments.append(longest)
    if segments[-1] not in ordered_segments:
        ordered_segments.append(segments[-1])

    anchors = []
    seen = set()
    for segment in ordered_segments:
        for anchor in _segment_anchor_variants(norm_text, segment):
            if anchor[0] in seen:
                continue
            seen.add(anchor[0])
            anchors.append(anchor)
    return anchors


def _score_match_window(container_text: str, start: int, end: int,
                        target_norm: str, context_before: str,
                        context_after: str, container_index: int,
                        hint_index: int, last_location: Optional[tuple[int, int]],
                        method: str) -> float:
    snippet = container_text[max(0, start):min(len(container_text), end)].strip()
    min_required = len(target_norm) if len(target_norm) < 4 else max(4, min(len(target_norm), 16))
    if len(snippet) < min_required:
        return -1.0

    text_score = _sequence_ratio(target_norm, snippet)
    before_score = _context_ratio(
        context_before, container_text[max(0, start - CONTEXT_WINDOW):start], tail=True)
    after_score = _context_ratio(
        context_after, container_text[end:end + CONTEXT_WINDOW], tail=False)
    length_score = min(len(snippet), len(target_norm)) / max(len(snippet), len(target_norm))
    proximity_score = 1.0 / (1.0 + abs(container_index - hint_index))

    order_score = 0.0
    if last_location is not None:
        last_container, last_start = last_location
        if container_index < last_container:
            # Readers do not necessarily annotate in document order, so this
            # is only a soft preference, not a hard anti-backtracking bias.
            order_score -= BACKTRACK_CONTAINER_PENALTY
        elif container_index == last_container and start < last_start:
            order_score -= BACKTRACK_OFFSET_PENALTY
        else:
            order_score += FORWARD_ORDER_BONUS

    method_bonus = {
        "exact": 0.14,
        "raw_regex": 0.10,
        "repaired_regex": 0.08,
        "anchor": 0.05,
    }.get(method, 0.0)

    return (
        text_score * 0.56
        + before_score * 0.16
        + after_score * 0.16
        + length_score * 0.06
        + proximity_score * 0.06
        + order_score
        + method_bonus
    )


def _build_candidate_windows(container_text: str, repaired_text: str,
                             raw_text: str, substitutions: dict):
    target_norm = normalize_for_match(repaired_text or raw_text)
    allow_short = len(target_norm) >= 2
    if len(target_norm) < 4 and not allow_short:
        return []

    candidates = {}

    def remember(start: int, end: int, method: str):
        start = max(0, start)
        end = min(len(container_text), end)
        if end <= start:
            return
        key = (start, end)
        existing = candidates.get(key)
        if existing is None or method == "exact":
            candidates[key] = method

    for start, end in _iter_literal_spans(container_text, target_norm):
        remember(start, end, "exact")

    pattern_sources = [
        (raw_text, "raw_regex"),
        (repaired_text, "repaired_regex"),
    ]
    for source, method in pattern_sources:
        if not source:
            continue
        try:
            regex = re.compile(
                build_flexible_needle_pattern(source, substitutions),
                re.IGNORECASE,
            )
        except re.error:
            continue
        for start, end in _iter_regex_spans(container_text, regex):
            remember(start, end, method)

    for anchor_text, anchor_offset in _extract_anchor_segments(repaired_text or raw_text):
        for anchor_start, _anchor_end in _iter_literal_spans(container_text, anchor_text):
            start = max(0, anchor_start - anchor_offset)
            remember(start, start + len(target_norm), "anchor")

    return [
        (start, end, method)
        for (start, end), method in candidates.items()
    ]


def _find_best_text_match(container_text: str, container_index: int,
                          repaired_text: str, raw_text: str,
                          context_before: str, context_after: str,
                          hint_index: int,
                          last_location: Optional[tuple[int, int]],
                          substitutions: dict) -> Optional[TextMatch]:
    match, _failure = _find_best_text_match_with_reason(
        container_text,
        container_index,
        repaired_text,
        raw_text,
        context_before,
        context_after,
        hint_index,
        last_location,
        substitutions,
    )
    return match


def _find_best_text_match_with_reason(container_text: str, container_index: int,
                                      repaired_text: str, raw_text: str,
                                      context_before: str, context_after: str,
                                      hint_index: int,
                                      last_location: Optional[tuple[int, int]],
                                      substitutions: dict) -> tuple[Optional[TextMatch], Optional[MatchFailure]]:
    target_norm = normalize_for_match(repaired_text or raw_text)
    has_strong_context = (
        len(normalize_for_match(context_before)) >= 24
        or len(normalize_for_match(context_after)) >= 24
    )
    if len(target_norm) < 4 and not has_strong_context:
        return None, MatchFailure(reason="context_too_short")

    candidates = list(_build_candidate_windows(
        container_text, repaired_text, raw_text, substitutions
    ))
    if not candidates:
        return None, MatchFailure(reason="no_candidate_windows")

    best_match = None
    best_rejected_score = None
    best_rejected_method = None
    for start, end, method in candidates:
        score = _score_match_window(
            container_text, start, end, target_norm,
            context_before, context_after,
            container_index, hint_index, last_location, method)
        if score < MATCH_SCORE_THRESHOLD:
            if best_rejected_score is None or score > best_rejected_score:
                best_rejected_score = score
                best_rejected_method = method
            continue
        match = TextMatch(
            container_index=container_index,
            start=start,
            end=end,
            score=score,
            matched_text=container_text[start:end].strip(),
            method=method,
        )
        if best_match is None or match.score > best_match.score:
            best_match = match
    if best_match is not None:
        return best_match, None
    return None, MatchFailure(
        reason="no_fuzzy_match",
        candidate_count=len(candidates),
        best_score=best_rejected_score,
        best_method=best_rejected_method,
    )


def _broken_output_score(text: str) -> int:
    if not text:
        return 0
    score = 0
    score += len(re.findall(r"\s+[.,;:!?)]", text))
    score += len(re.findall(r"[(„“\"']\s+", text))
    score += len(re.findall(r"\b[A-Za-zÀ-ɏÄÖÜäöüß]\s+[A-Za-zÀ-ɏÄÖÜäöüß]{2,}", text))
    score += len(re.findall(r"\b[A-Za-zÀ-ɏÄÖÜäöüß]\s+[A-Za-zÀ-ɏÄÖÜäöüß]\s+[A-Za-zÀ-ɏÄÖÜäöüß]", text))
    return score


def _preferred_output_text(preferred_text: str, matched_text: str) -> str:
    preferred = (preferred_text or "").strip()
    matched = (matched_text or "").strip()
    if not preferred:
        return matched
    if not matched:
        return preferred

    preferred_norm = normalize_for_match(preferred)
    matched_norm = normalize_for_match(matched)
    if preferred_norm.lower() == matched_norm.lower():
        return preferred if preferred != preferred.lower() else matched

    similarity = _sequence_ratio(preferred_norm, matched_norm)
    if similarity >= 0.72 and _broken_output_score(preferred) < _broken_output_score(matched):
        return preferred
    return matched


_DISPLAY_TEXT_TRANS = str.maketrans({
    " ": " ", " ": " ", " ": " ", "​": "",
    "­": "",  # soft hyphen
})


def _clean_pdf_word_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_DISPLAY_TEXT_TRANS)
    text = "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_pdf_line_break(previous_word, current_word) -> bool:
    return (
        current_word[0] < previous_word[0]
        or current_word[1] - previous_word[1] > 2.0
    )


def _should_merge_pdf_line_hyphen(previous_text: str, current_text: str,
                                  previous_word, current_word) -> bool:
    if not previous_text.endswith("-") or previous_text.endswith(("–", "—", "−")):
        return False
    if not current_text[:1].islower():
        return False
    if not _is_pdf_line_break(previous_word, current_word):
        return False
    prefix = previous_text[:-1].lower()
    # These are visible compounds/prefix constructions, not broken words.
    if prefix in {"in", "out", "buy", "sell", "call", "put", "long", "short"}:
        return False
    return len(prefix) >= 4 or prefix in {"vol"}


def _join_pdf_word_text(hit_words) -> str:
    pieces = []
    previous_word = None
    for word_info in hit_words:
        word = _clean_pdf_word_text(word_info[4])
        if not word:
            continue
        if (
            pieces
            and previous_word is not None
            and _should_merge_pdf_line_hyphen(pieces[-1], word, previous_word, word_info)
        ):
            pieces[-1] = pieces[-1][:-1] + word
        else:
            pieces.append(word)
        previous_word = word_info
    return " ".join(pieces)


def _match_boundary_score(text: str) -> float:
    if not text:
        return 0.0
    score = 0.0
    if text[0].isalnum() or text[0] in "\"“‘([":
        score += 0.5
    if text[-1].isalnum() or text[-1] in ".!?\"”’)*]":
        score += 0.5
    return score


def _match_candidate_score(target_norm: str, candidate_text: str) -> float:
    cand_norm = normalize_for_match(candidate_text)
    if not cand_norm:
        return -1.0
    similarity = _sequence_ratio(target_norm, cand_norm)
    length_score = min(len(cand_norm), len(target_norm)) / max(len(cand_norm), len(target_norm))
    prefix = _sequence_ratio(
        target_norm[:min(24, len(target_norm))],
        cand_norm[:min(24, len(cand_norm))],
    ) if target_norm and cand_norm else 0.0
    suffix = _sequence_ratio(
        target_norm[-min(24, len(target_norm)):],
        cand_norm[-min(24, len(cand_norm)):],
    ) if target_norm and cand_norm else 0.0
    return (
        similarity * 0.65
        + length_score * 0.15
        + prefix * 0.10
        + suffix * 0.05
        + _match_boundary_score(candidate_text) * 0.05
    )


def _trim_match_range_to_target(state: "EpubSpineState", target_text: str,
                                start: int, end: int) -> tuple[int, int, str]:
    target = (target_text or "").strip()
    allow_trailing_note = bool(re.search(r"(?:\d+|\*+)$", target))
    allow_leading_note = bool(re.match(r"^(?:\d+|\*+)", target))

    while start < end:
        candidate_text = _text_from_state_range(state, start, end).strip()
        if not candidate_text:
            break
        if not allow_trailing_note and re.search(r'[.!?…"”’"]\d+$', candidate_text):
            end -= 1
            continue
        if not allow_trailing_note and re.search(r'[.!?…"”’"]\*+$', candidate_text):
            end -= 1
            continue
        if not allow_leading_note and re.match(r'^\d+\s*', candidate_text):
            start += 1
            continue
        return start, end, candidate_text
    return start, end, _text_from_state_range(state, start, end).strip()


def _context_anchor_variants(text: str, tail: bool) -> list[str]:
    norm = normalize_for_match(text)
    tokens = [
        match.group(0)
        for match in re.finditer(r"\S+", norm)
        if not _looks_broken_token(match.group(0))
    ]
    variants = []
    seen = set()
    for size in (8, 6, 5, 4, 3):
        if len(tokens) < size:
            continue
        part = tokens[-size:] if tail else tokens[:size]
        anchor = " ".join(part).strip()
        if len(anchor) < 12 or anchor in seen:
            continue
        seen.add(anchor)
        variants.append(anchor)
    return variants


def _pdf_context_anchor_variants(text: str, tail: bool) -> list[str]:
    norm = normalize_for_match(text)
    tokens = [
        match.group(0)
        for match in re.finditer(r"\S+", norm)
    ]
    variants = []
    seen = set()
    for size in (8, 6, 5, 4, 3, 2):
        if len(tokens) < size:
            continue
        part = tokens[-size:] if tail else tokens[:size]
        anchor = " ".join(part).strip()
        if len(anchor) < 6 or anchor in seen:
            continue
        seen.add(anchor)
        variants.append(anchor)
    return variants


def _local_refine_epub_match(state: "EpubSpineState", target_text: str,
                             match: TextMatch, radius: int = 20) -> Optional[TextMatch]:
    target_norm = normalize_for_match(target_text)
    if not target_norm:
        return None

    best = None
    for delta_start in range(-radius, radius + 1):
        for delta_end in range(-radius, radius + 1):
            start = match.start + delta_start
            end = match.end + delta_end
            if start < 0 or end <= start or end > len(state.char_map):
                continue
            start, end, candidate_text = _trim_match_range_to_target(
                state, target_text, start, end)
            candidate_norm = normalize_for_match(candidate_text)
            if len(candidate_norm) < 2:
                continue
            if len(candidate_norm) > max(len(target_norm) * 2.5, len(target_norm) + 150):
                continue
            score = _match_candidate_score(target_norm, candidate_text)
            if best is None or score > best.score:
                best = TextMatch(
                    container_index=match.container_index,
                    start=start,
                    end=end,
                    score=score,
                    matched_text=candidate_text,
                    method=f"{match.method}+local",
                )
    return best


def _context_refine_epub_match(state: "EpubSpineState", highlight: "Highlight",
                               match: TextMatch, window: int = 900) -> Optional[TextMatch]:
    target_text = highlight.text or highlight.raw_text
    target_norm = normalize_for_match(target_text)
    if not target_norm:
        return None

    text = state.norm_text
    window_start = max(0, match.start - window)
    window_end = min(len(text), match.end + window)
    snippet = text[window_start:window_end]

    start_candidates = []
    end_candidates = []

    before_variants = _context_anchor_variants(highlight.context_before, tail=True)
    for anchor in before_variants:
        search_end = max(0, match.start - window_start + 160)
        pos = snippet.rfind(anchor, 0, search_end)
        if pos < 0:
            continue
        start = window_start + pos + len(anchor)
        while start < len(text) and text[start] == " ":
            start += 1
        start_candidates.append(start)

    after_variants = _context_anchor_variants(highlight.context_after, tail=False)
    for anchor in after_variants:
        search_start = max(0, match.end - window_start - 160)
        pos = snippet.find(anchor, search_start)
        if pos < 0:
            continue
        end = window_start + pos
        while end > 0 and text[end - 1] == " ":
            end -= 1
        end_candidates.append(end)

    if not start_candidates and not end_candidates:
        return None

    if start_candidates and end_candidates:
        pairs = [
            (start, end)
            for start in start_candidates
            for end in end_candidates
            if end > start
        ]
    elif start_candidates:
        pairs = [(start, match.end) for start in start_candidates]
    else:
        pairs = [(match.start, end) for end in end_candidates]

    best = None
    for start, end in pairs:
        start, end, candidate_text = _trim_match_range_to_target(
            state, target_text, start, end)
        candidate_norm = normalize_for_match(candidate_text)
        if len(candidate_norm) < 2:
            continue
        if len(candidate_norm) > max(len(target_norm) * 2.5, len(target_norm) + 180):
            continue
        score = _match_candidate_score(target_norm, candidate_text) + 0.08
        if best is None or score > best.score:
            best = TextMatch(
                container_index=match.container_index,
                start=start,
                end=end,
                score=score,
                matched_text=candidate_text,
                method=f"{match.method}+context",
            )
    return best


def _refine_epub_match(state: "EpubSpineState", highlight: "Highlight",
                       match: TextMatch) -> TextMatch:
    target_text = highlight.text or highlight.raw_text
    target_norm = normalize_for_match(target_text)
    if not target_norm:
        return match

    start, end, current_text = _trim_match_range_to_target(
        state, target_text, match.start, match.end)
    if current_text:
        match = TextMatch(
            container_index=match.container_index,
            start=start,
            end=end,
            score=match.score,
            matched_text=current_text,
            method=match.method,
        )
    current_text = current_text or match.matched_text
    current_norm = normalize_for_match(current_text)
    if current_norm == target_norm:
        return TextMatch(
            container_index=match.container_index,
            start=match.start,
            end=match.end,
            score=_match_candidate_score(target_norm, current_text),
            matched_text=current_text,
            method=match.method,
        )

    best = TextMatch(
        container_index=match.container_index,
        start=match.start,
        end=match.end,
        score=_match_candidate_score(target_norm, current_text),
        matched_text=current_text,
        method=match.method,
    )

    for candidate in (
        _local_refine_epub_match(state, target_text, match),
        _context_refine_epub_match(state, highlight, match),
    ):
        if candidate is None:
            continue
        if candidate.score > best.score + 0.01:
            best = candidate
    return best


def build_norm_map(original: str) -> tuple[str, list[int]]:
    """
    Normalisiert `original` und gibt (normalized, idx_map) zurück, wo
    idx_map[i] der Original-Index des Chars an Position i im Normalisierten ist.
    idx_map hat Länge len(normalized)+1 (Sentinel für end-offset).

    Wichtig: bewahrt führende/endende Whitespaces (kollabiert zu einem
    Space). Das ist kritisch für Konkatenation über DOM-Knoten-Grenzen
    hinweg — Trailing-Strip wäre hier falsch, weil die Whitespace zur
    nächsten Node-Grenze gehört.
    """
    norm_chars = []
    idx_map = []
    prev_was_ws = False
    i = 0
    n = len(original)
    while i < n:
        c = original[i]
        if c in ("­", "​"):
            i += 1
            continue
        if c == "-":
            j = i + 1
            while j < n and original[j] in " \t":
                j += 1
            if j < n and original[j] == "\n":
                k = j + 1
                while k < n and original[k] in " \t":
                    k += 1
                i = k
                continue
        nfkc = unicodedata.normalize("NFKC", c)
        translated = nfkc.translate(_APOS)
        for out_c in translated:
            if out_c.isspace() or out_c == "":
                if prev_was_ws:
                    continue
                norm_chars.append(" ")
                idx_map.append(i)
                prev_was_ws = True
            else:
                norm_chars.append(out_c.lower())
                idx_map.append(i)
                prev_was_ws = False
        i += 1
    idx_map.append(n)
    return "".join(norm_chars), idx_map


# ============================================================
# PDF → ANNOTIERTES PDF
# ============================================================

def annotate_pdf(original_pdf: str, highlights: list[Highlight],
                 output_path: str, substitutions: dict, verbose=True,
                 unmatched_out: Optional[str] = None):
    doc = fitz.open(original_pdf)
    stats = {"text_matched": 0, "text_unmatched": 0,
             "image_placed": 0, "image_skipped": 0}
    unmatched_entries = []
    page_cache = {}
    last_match_location = None

    for hl in highlights:
        if hl.is_image:
            if 0 <= hl.rm_page < doc.page_count:
                page = doc[hl.rm_page]
                rect = fitz.Rect(*hl.rm_bbox)
                rect &= page.rect
                if rect.is_empty:
                    stats["image_skipped"] += 1
                    continue
                annot = page.add_rect_annot(rect)
                annot.set_colors(stroke=ANNOT_COLOR_MAP.get(hl.color, (1, 1, 0)))
                annot.set_border(width=2.0)
                annot.set_info(title="Remarkable Import",
                               content=f"[Bild-Highlight {hl.color}]")
                annot.set_opacity(0.5)
                annot.update()
                _assign_zotero_annotation_id(
                    doc,
                    annot,
                    key_source={
                        "kind": "image",
                        "page_index": page.number,
                        "color": hl.color,
                        "text": "",
                        "geometry": _rect_geometry_key(rect),
                    },
                )
                stats["image_placed"] += 1
            else:
                stats["image_skipped"] += 1
            continue

        # Text-Highlight: suche im Original-PDF
        pdf_match, match_failure = _search_text_in_pdf(
            doc, hl.text, hl.raw_text, hl.context_before, hl.context_after,
            substitutions, hint_page=hl.rm_page,
            last_location=last_match_location, page_cache=page_cache)
        if pdf_match:
            last_match_location = pdf_match.last_location
            output_text = _preferred_output_text(
                hl.text or hl.raw_text,
                pdf_match.matched_text,
            )
            annot = pdf_match.page.add_highlight_annot(quads=pdf_match.quads)
            annot.set_colors(stroke=ANNOT_COLOR_MAP.get(hl.color, (1, 1, 0)))
            annot.set_info(title="Remarkable Import",
                           content=output_text)
            annot.update()
            _assign_zotero_annotation_id(
                doc,
                annot,
                key_source={
                    "kind": "text",
                    "page_index": pdf_match.page.number,
                    "color": hl.color,
                    "text": output_text,
                    "geometry": _quad_geometry_key(pdf_match.quads),
                },
            )
            stats["text_matched"] += 1
        else:
            stats["text_unmatched"] += 1
            unmatched_entries.append(_build_unmatched_entry(hl, match_failure))

    doc.save(output_path, garbage=3, deflate=True)
    doc.close()

    if unmatched_out and unmatched_entries:
        with open(unmatched_out, "w", encoding="utf-8") as f:
            json.dump(unmatched_entries, f, indent=2, ensure_ascii=False)
    elif unmatched_out and os.path.exists(unmatched_out):
        os.remove(unmatched_out)

    if verbose:
        print(f"  PDF Output: {stats['text_matched']} Text-Annotations, "
              f"{stats['image_placed']} Bild-Annotations, "
              f"{stats['text_unmatched']} nicht gefunden")
        if unmatched_out and unmatched_entries:
            print(f"  Unmatched-Kontext: {unmatched_out} "
                  f"({len(unmatched_entries)} Einträge — für Review)")
    return stats


def _quad_geometry_key(quads):
    geometry = []
    for quad in quads:
        geometry.append([
            round(quad.ul.x, 2), round(quad.ul.y, 2),
            round(quad.ur.x, 2), round(quad.ur.y, 2),
            round(quad.ll.x, 2), round(quad.ll.y, 2),
            round(quad.lr.x, 2), round(quad.lr.y, 2),
        ])
    return geometry


def _rect_geometry_key(rect: fitz.Rect):
    return [round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)]


def _stable_annotation_key(key_source: dict) -> str:
    payload = json.dumps(key_source, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _assign_zotero_annotation_id(doc, annot, key_source: Optional[dict] = None):
    """Give the PDF annotation a Zotero-compatible ID.

    Zotero's PDF worker treats square/image, ink, and free-text annotations as
    importable only when they carry a Zotero-style key in /NM or /Zotero:Key.
    """
    key = _stable_annotation_key(key_source) if key_source else uuid.uuid4().hex
    doc.xref_set_key(annot.xref, "NM", f"(Zotero-{key})")
    doc.xref_set_key(annot.xref, "Zotero:Key", f"({key})")
    return key


def _build_pdf_page_state(page):
    words = page.get_text("words")
    page_text_parts = []
    word_spans = []
    cursor = 0
    last_block = last_line = None

    for word_info in words:
        raw_word = word_info[4]
        norm_word = normalize_for_match(raw_word)
        if not norm_word:
            continue
        block, line = word_info[5], word_info[6]
        if last_block is not None:
            page_text_parts.append(" ")
            cursor += 1
        start_idx = cursor
        page_text_parts.append(norm_word)
        cursor += len(norm_word)
        word_spans.append((start_idx, cursor, word_info))
        last_block, last_line = block, line

    return "".join(page_text_parts), word_spans


def _quads_from_word_hits(hit_words):
    return [
        fitz.Quad(
            fitz.Point(word_info[0], word_info[1]),
            fitz.Point(word_info[2], word_info[1]),
            fitz.Point(word_info[0], word_info[3]),
            fitz.Point(word_info[2], word_info[3]),
        )
        for word_info in hit_words
    ]


def _refine_pdf_match_range(page_text: str, match: TextMatch,
                            repaired_text: str, raw_text: str,
                            context_before: str, context_after: str,
                            window: int = 900) -> TextMatch:
    target_norm = normalize_for_match(repaired_text or raw_text)
    if not target_norm:
        return match

    start_candidates = []
    end_candidates = []
    search_start = max(0, match.start - window)
    search_end = min(len(page_text), match.end + window)
    snippet = page_text[search_start:search_end]

    for anchor in _pdf_context_anchor_variants(context_before, tail=True):
        anchor_end = max(0, match.start - search_start + 160)
        pos = snippet.rfind(anchor, 0, anchor_end)
        if pos < 0:
            continue
        start = search_start + pos + len(anchor)
        while start < len(page_text) and page_text[start] == " ":
            start += 1
        start_candidates.append(start)

    for anchor in _pdf_context_anchor_variants(context_after, tail=False):
        anchor_start = max(0, match.end - search_start - 160)
        pos = snippet.find(anchor, anchor_start)
        if pos < 0:
            continue
        end = search_start + pos
        while end > 0 and page_text[end - 1] == " ":
            end -= 1
        end_candidates.append(end)

    pairs = [(match.start, match.end)]
    pairs.extend((start, match.end) for start in start_candidates)
    pairs.extend((match.start, end) for end in end_candidates)
    pairs.extend(
        (start, end)
        for start in start_candidates
        for end in end_candidates
    )

    best = match
    for start, end in pairs:
        if start < 0 or end <= start or end > len(page_text):
            continue
        candidate_text = page_text[start:end].strip()
        candidate_norm = normalize_for_match(candidate_text)
        if len(candidate_norm) < 2:
            continue
        if len(candidate_norm) > max(len(target_norm) * 2.5, len(target_norm) + 180):
            continue
        score = _match_candidate_score(target_norm, candidate_text)
        if start != match.start or end != match.end:
            score += 0.08
        context_adjusted = start != match.start or end != match.end
        if score > best.score + 0.01 or (context_adjusted and score >= best.score - 0.02):
            best = TextMatch(
                container_index=match.container_index,
                start=start,
                end=end,
                score=score,
                matched_text=candidate_text,
                method=f"{match.method}+context",
            )
    return best


def _search_text_in_pdf(doc, repaired_text: str, raw_text: str,
                        context_before: str, context_after: str,
                        substitutions: dict, hint_page: int = 0,
                        last_location: Optional[tuple[int, int]] = None,
                        page_cache: Optional[dict] = None):
    target_norm = normalize_for_match(repaired_text or raw_text)
    if len(target_norm) < 4:
        return None, MatchFailure(reason="context_too_short")

    page_order = list(range(max(0, hint_page - 3), min(doc.page_count, hint_page + 4)))
    page_order += [i for i in range(doc.page_count) if i not in page_order]

    best_match = None
    best_page = None
    best_quads = None
    best_text = ""
    best_failure = None

    for pi in page_order:
        page = doc[pi]
        if page_cache is not None and pi in page_cache:
            page_text, word_spans = page_cache[pi]
        else:
            page_text, word_spans = _build_pdf_page_state(page)
            if page_cache is not None:
                page_cache[pi] = (page_text, word_spans)
        if not page_text:
            continue

        match, failure = _find_best_text_match_with_reason(
            page_text, pi, repaired_text, raw_text,
            context_before, context_after,
            hint_page, last_location, substitutions)
        if match is None:
            if failure is not None and (
                best_failure is None
                or (
                    failure.best_score is not None
                    and (
                        best_failure.best_score is None
                        or failure.best_score > best_failure.best_score
                    )
                )
                or (
                    best_failure.reason == "no_candidate_windows"
                    and failure.reason != "no_candidate_windows"
                )
            ):
                best_failure = failure
            continue

        match = _refine_pdf_match_range(
            page_text, match, repaired_text, raw_text,
            context_before, context_after)
        hit_words = [
            word_info for start, end, word_info in word_spans
            if start < match.end and end > match.start
        ]
        if not hit_words:
            continue
        quads = _quads_from_word_hits(hit_words)
        matched_word_text = _join_pdf_word_text(hit_words)

        if best_match is None or match.score > best_match.score:
            best_match = match
            best_page = page
            best_quads = quads
            best_text = matched_word_text
            if match.score >= CONFIDENT_MATCH_SCORE:
                break

    if best_match is None or best_page is None or best_quads is None:
        return None, best_failure

    return (
        PdfMatchResult(
            quads=best_quads,
            page=best_page,
            matched_text=best_text or best_match.matched_text,
            last_location=(best_match.container_index, best_match.start),
        ),
        None,
    )


# ============================================================
# EPUB → ANNOTIERTES EPUB
# ============================================================

EPUB_HIGHLIGHT_CSS = """
/* Remarkable-Import Highlights */
.rm-highlight { background-color: rgba(255,245,120,0.6); }
.rm-highlight-yellow { background-color: rgba(255,245,120,0.6); }
.rm-highlight-pink   { background-color: rgba(255,170,210,0.5); }
.rm-highlight-green  { background-color: rgba(170,240,170,0.5); }
.rm-highlight-blue   { background-color: rgba(170,200,255,0.5); }
.rm-highlight-red    { background-color: rgba(255,170,170,0.5); }
"""


@dataclass
class EpubMatch:
    highlight: Highlight
    spine_index: int
    xhtml_path: str
    start: int
    end: int
    matched_text: str
    section_label: str


@dataclass
class EpubSpineState:
    spine_index: int
    path: str
    tree: etree._Element
    label: str
    norm_text: str = ""
    char_map: list[tuple[object, str, int]] = field(default_factory=list)
    dirty: bool = True


def _body_from_tree(tree):
    body = tree.find(".//{http://www.w3.org/1999/xhtml}body")
    if body is None:
        body = tree.find(".//body")
    return body


def _section_label_from_tree(tree, fallback_path: str) -> str:
    heading_tags = [
        ".//{http://www.w3.org/1999/xhtml}h1",
        ".//{http://www.w3.org/1999/xhtml}h2",
        ".//{http://www.w3.org/1999/xhtml}h3",
        ".//h1",
        ".//h2",
        ".//h3",
        ".//{http://www.w3.org/1999/xhtml}title",
        ".//title",
    ]
    for selector in heading_tags:
        heading = tree.find(selector)
        if heading is None:
            continue
        text = normalize_for_match(" ".join(heading.itertext()))
        if text:
            return text[:120]
    return Path(fallback_path).stem


def _refresh_spine_state(state: EpubSpineState):
    body = _body_from_tree(state.tree)
    if body is None:
        state.norm_text = ""
        state.char_map = []
        state.dirty = False
        return state
    state.norm_text, state.char_map = _build_xhtml_text_map(body)
    state.dirty = False
    return state


def _is_heading_like_highlight(highlight: Highlight) -> bool:
    source = (highlight.text or highlight.raw_text or "").strip()
    norm = normalize_for_match(source)
    if not norm or len(norm) > 120:
        return False
    if source.endswith((".", "!", "?", ";", ",")):
        return False
    words = re.findall(r"[0-9A-Za-zÀ-ɏÄÖÜäöüß]+", source)
    if len(words) < 2 or len(words) > 12:
        return False
    letters = [c for c in source if c.isalpha()]
    if not letters:
        return False
    uppercase_ratio = sum(c.isupper() for c in letters) / len(letters)
    title_like = all(
        token[:1].isdigit() or token[:1].isupper()
        for token in re.findall(r"\S+", source)
    )
    return uppercase_ratio >= 0.72 or title_like


def _state_label_matches_target(state: EpubSpineState, target_norm: str) -> bool:
    label_norm = normalize_for_match(state.label).lower()
    target_norm = normalize_for_match(target_norm).lower()
    if not label_norm or not target_norm:
        return False
    return (
        label_norm == target_norm
        or label_norm.endswith(target_norm)
        or target_norm.endswith(label_norm)
        or target_norm in label_norm
    )


def _iter_text_nodes(elem) -> Iterator[tuple[object, str, str]]:
    """Yield (owner_element, 'text'|'tail', text_string) in document order.

    - 'text' is elem.text (appears inside elem, before first child)
    - 'tail' is elem.tail (appears AFTER elem, inside its parent)
    """
    if elem.text:
        yield (elem, "text", elem.text)
    for child in elem:
        yield from _iter_text_nodes(child)
        if child.tail:
            yield (child, "tail", child.tail)


def _local_tag_name(node) -> str:
    if node is None or not etree.iselement(node):
        return ""
    try:
        return etree.QName(node).localname.lower()
    except ValueError:
        tag = getattr(node, "tag", "")
        return tag.lower() if isinstance(tag, str) else ""


def _needs_synthetic_separator(node, attr: str) -> bool:
    """Only inject visual separators for real layout boundaries like <br/>.

    Generic DOM boundaries are not safe here: once we inject highlight spans,
    every wrapped range would start adding bogus spaces and later matches would
    drift or fail. We therefore keep separators narrowly scoped to explicit
    line-break boundaries in the source document.
    """
    if _local_tag_name(node) == "br":
        return True
    if attr == "text":
        prev = node.getprevious() if etree.iselement(node) else None
        if _local_tag_name(prev) == "br":
            return True
    return False


def _build_xhtml_text_map(body_elem) -> tuple[str, list[tuple[object, str, int, int]]]:
    """
    Build normalized concatenated text from body, plus a mapping of
    normalized-char-index → (owner_node, attr, original_char_off_in_node).

    Returns (norm_text, segments), where segments is a list of
    (node, attr, norm_start, norm_end_exclusive, orig_start_for_seg).
    For simplicity we return a full per-char map instead.

    Actually: returns (norm_text, char_map) where char_map[i] =
    (node, attr, orig_off_in_node_text).
    """
    norm_parts = []
    char_map = []  # per normalized char
    for (node, attr, text) in _iter_text_nodes(body_elem):
        norm_local, idx_map_local = build_norm_map(text)
        if not norm_local:
            continue
        if (
            norm_parts
            and _needs_synthetic_separator(node, attr)
            and not norm_parts[-1].endswith(" ")
            and not norm_local.startswith(" ")
        ):
            norm_parts.append(" ")
            char_map.append((node, attr, 0))
        for i, c in enumerate(norm_local):
            char_map.append((node, attr, idx_map_local[i]))
        norm_parts.append(norm_local)
    return "".join(norm_parts), char_map


def _cfi_child_step(parent, child) -> int:
    """
    Compute the EPUB CFI step index of `child` within `parent`.
    Interleaved rule:
      - parent.text (if present)    → step 1
      - children[0]                 → step 2
      - children[0].tail (if present)→ step 3
      - children[1]                 → step 4
      - children[1].tail            → step 5
      - ...
    Element children get even steps; text nodes (text/tail) get odd.
    """
    step = 0
    if parent.text is not None and parent.text != "":
        step = 1
    for el in parent:
        if step % 2 == 0:
            step += 2
        else:
            step += 1  # from odd text-slot to next even element-slot
        if el is child:
            return step
        # el.tail (if present) occupies the next odd step
        if el.tail is not None and el.tail != "":
            step += 1
    raise ValueError("child not found in parent")


def _cfi_path_to_element(html_root, element) -> list[int]:
    """Compute the CFI element-step path from html_root down to `element`."""
    path = []
    node = element
    while node is not html_root:
        parent = node.getparent()
        if parent is None:
            raise ValueError("element not under html_root")
        path.append(_cfi_child_step(parent, node))
        node = parent
    path.reverse()
    return path


def _cfi_for_position(html_root, node, attr: str, char_offset: int) -> str:
    """
    Produce a Calibre-style CFI string (starting with '/2/') for the position
    in the given text node (attr='text' → node.text, attr='tail' → node.tail)
    at char_offset.
    """
    if attr == "text":
        # the text-slot of `node` is step 1 inside node (since node.text
        # occupies node's own first odd position)
        path = _cfi_path_to_element(html_root, node)
        text_step = 1
    elif attr == "tail":
        # tail is the odd step right after the element's even step in the parent
        parent = node.getparent()
        elem_step = _cfi_child_step(parent, node)
        path = _cfi_path_to_element(html_root, parent)
        path.append(elem_step)
        text_step = elem_step + 1
        # But tail lives in the parent's sequence, so the path goes to parent,
        # then ONE step pointing to the tail slot (which is elem_step+1).
        # Adjust: we don't append elem_step, we replace with elem_step+1.
        path.pop()
        path.append(text_step)
        text_step = None  # already included
    else:
        raise ValueError(attr)
    if text_step is None:
        return "/2/" + "/".join(str(s) for s in path[:-1]) + f"/{path[-1]}:{char_offset}"
    return "/2/" + "/".join(str(s) for s in path) + f"/{text_step}:{char_offset}"


def _text_from_state_range(state: EpubSpineState, start: int, end: int) -> str:
    pieces = []
    for node, attr, local_off in state.char_map[start:end]:
        source = node.text if attr == "text" else node.tail
        if not source or local_off >= len(source):
            continue
        pieces.append(source[local_off])
    return "".join(pieces).strip()


def _remove_empty_highlight_spans(tree) -> int:
    removed = 0
    spans = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " rm-highlight ")]')
    for span in list(spans):
        if "".join(span.itertext()).strip():
            continue
        parent = span.getparent()
        if parent is None:
            continue
        idx = parent.index(span)
        preserved_text = (span.text or "") + (span.tail or "")
        if idx > 0:
            previous = parent[idx - 1]
            previous.tail = (previous.tail or "") + preserved_text
        else:
            parent.text = (parent.text or "") + preserved_text
        parent.remove(span)
        removed += 1
    return removed


def _epub_wrap_order_key(match: EpubMatch):
    return (-(match.end - match.start), match.start, match.end)


NSMAP = {
    "opf":  "http://www.idpf.org/2007/opf",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _parse_opf(epub: zipfile.ZipFile):
    """Return (opf_path, opf_tree, spine_hrefs) — hrefs are absolute inside zip."""
    container = epub.read("META-INF/container.xml")
    c_tree = etree.fromstring(container)
    rootfile = c_tree.find(".//container:rootfile", NSMAP)
    opf_path = rootfile.get("full-path")
    opf_dir = os.path.dirname(opf_path)
    opf_tree = etree.fromstring(epub.read(opf_path))

    manifest = {}
    for item in opf_tree.findall(".//opf:manifest/opf:item", NSMAP):
        manifest[item.get("id")] = item.get("href")
    spine_hrefs = []
    for itemref in opf_tree.findall(".//opf:spine/opf:itemref", NSMAP):
        idref = itemref.get("idref")
        href = manifest.get(idref)
        if href:
            full = os.path.normpath(os.path.join(opf_dir, href)) if opf_dir else href
            full = full.replace(os.sep, "/")
            spine_hrefs.append(full)
    return opf_path, opf_tree, spine_hrefs


def _find_match_in_xhtml(xhtml_bytes: bytes, highlight_text: str) -> Optional[tuple]:
    """
    Find highlight_text inside the XHTML body.
    Returns (tree, start_node, start_attr, start_local, end_node, end_attr, end_local)
    with the local offsets pointing into the ORIGINAL (non-normalized) text of
    the nodes. Returns None if not found.
    """
    try:
        tree = etree.fromstring(xhtml_bytes)
    except etree.XMLSyntaxError:
        # Fallback: lxml.html (HTML-tolerant)
        tree = lxml_html.fromstring(xhtml_bytes)

    # find body
    body = tree.find(".//{http://www.w3.org/1999/xhtml}body")
    if body is None:
        body = tree.find(".//body")
    if body is None:
        return None

    norm_text, char_map = _build_xhtml_text_map(body)
    if not norm_text:
        return None

    needle = normalize_for_match(highlight_text)
    if not needle or len(needle) < 4:
        return None

    # First try: exact substring
    idx = norm_text.find(needle)
    if idx < 0:
        # Try looser: allow any whitespace run between words
        escaped = re.escape(needle).replace(r"\ ", r"\s+")
        m = re.search(escaped, norm_text)
        if not m:
            # Last resort: anchor on first 60% of text
            anchor_len = max(20, int(len(needle) * 0.6))
            anchor = needle[:anchor_len]
            idx2 = norm_text.find(anchor)
            if idx2 < 0:
                return None
            idx, end_idx = idx2, idx2 + len(anchor)
        else:
            idx, end_idx = m.start(), m.end()
    else:
        end_idx = idx + len(needle)

    if end_idx - 1 >= len(char_map) or idx >= len(char_map):
        return None

    start_node, start_attr, start_local = char_map[idx]
    end_node,   end_attr,   end_local   = char_map[end_idx - 1]
    # end_local is the index of the LAST matched char in the original text
    # → exclusive end is end_local + len of the original char. Safest: +1.
    end_local_excl = end_local + 1

    return (tree, start_node, start_attr, start_local,
            end_node, end_attr, end_local_excl)


def _wrap_range_with_span(tree, start_node, start_attr, start_local,
                          end_node, end_attr, end_local_excl, color: str):
    """
    Wrap the character range [start, end) across (possibly multiple) text
    nodes in <span class="rm-highlight rm-highlight-{color}">. Modifies tree
    in place.

    Strategy:
      - Collect the text-node sequence in document order from start to end.
      - For each segment, build a span wrapping the portion in-range.
      - Mutate the DOM: replace the text on the owning node/attr with
        (before_span_text, span_element_with_span_text_as_text,
        plus residual after_span_text carried as span.tail or node.text).
    """
    # First: find the ordered list of (node, attr, text) segments that span
    # from start to end.
    # We re-iterate _iter_text_nodes over body and capture the sub-range.
    body = None
    for ancestor in start_node.iterancestors():
        tag = etree.QName(ancestor).localname if etree.iselement(ancestor) else ""
        if tag == "body":
            body = ancestor
            break
    if body is None:
        # try by walking up until the root
        parent = start_node.getparent()
        while parent is not None:
            body = parent
            parent = parent.getparent()

    segments = []   # list of (node, attr, text)
    started = False
    finished = False
    for (node, attr, text) in _iter_text_nodes(body):
        if not started:
            if node is start_node and attr == start_attr:
                started = True
                # If also the end: single segment
                if node is end_node and attr == end_attr:
                    segments.append((node, attr, text))
                    finished = True
                    break
                segments.append((node, attr, text))
                continue
        else:
            segments.append((node, attr, text))
            if node is end_node and attr == end_attr:
                finished = True
                break

    if not segments or not finished:
        return False

    xhtml_ns = "{http://www.w3.org/1999/xhtml}"

    def make_span(text):
        # Use the html namespace if the root uses it
        span = etree.Element(f"{xhtml_ns}span") \
               if etree.QName(body).namespace else etree.Element("span")
        span.set("class", f"rm-highlight rm-highlight-{color}")
        span.set("data-rm-highlight-color", color)
        span.set("style", CSS_HIGHLIGHT_STYLE.get(
            color, CSS_HIGHLIGHT_STYLE["yellow"]))
        span.text = text
        return span

    # One-segment case (easy)
    if len(segments) == 1:
        node, attr, text = segments[0]
        before = text[:start_local]
        middle = text[start_local:end_local_excl]
        after  = text[end_local_excl:]
        if not middle:
            return False
        span = make_span(middle)
        if attr == "text":
            node.text = before
            span.tail = after
            node.insert(0, span)
        else:  # 'tail' — node.tail sits inside node.getparent(), after node
            parent = node.getparent()
            node.tail = before
            span.tail = after
            idx = list(parent).index(node) + 1
            parent.insert(idx, span)
        return True

    # Multi-segment case: process first, middle, last
    # First segment: from start_local to end of text
    node, attr, text = segments[0]
    before = text[:start_local]
    middle = text[start_local:]
    span = make_span(middle)
    if attr == "text":
        node.text = before
        # insert span at beginning of node's children, tail=None (keep original tail chain)
        # but we must preserve the existing children AFTER the span:
        # span.tail should be empty (text was 'text', not 'tail')
        node.insert(0, span)
    else:
        parent = node.getparent()
        node.tail = before
        # span goes right after node in parent
        idx = list(parent).index(node) + 1
        parent.insert(idx, span)

    # Last segment
    node, attr, text = segments[-1]
    middle = text[:end_local_excl]
    after  = text[end_local_excl:]
    span_last = make_span(middle)
    span_last.tail = after
    if attr == "text":
        # node.text contained the middle; replace with empty and prepend span
        node.text = ""
        # existing first child of node now shifts; span becomes first child
        node.insert(0, span_last)
    else:
        parent = node.getparent()
        # node.tail contained the matched text portion; replace with empty,
        # insert span AFTER node
        node.tail = ""
        idx = list(parent).index(node) + 1
        parent.insert(idx, span_last)

    # Middle segments: wrap entire text
    for (node, attr, text) in segments[1:-1]:
        if not text:
            continue
        span_mid = make_span(text)
        if attr == "text":
            node.text = ""
            node.insert(0, span_mid)
        else:
            parent = node.getparent()
            node.tail = ""
            idx = list(parent).index(node) + 1
            parent.insert(idx, span_mid)

    return True


def annotate_epub(original_epub: str, highlights: list[Highlight],
                  output_path: str, substitutions: dict, verbose=True,
                  unmatched_out: Optional[str] = None,
                  notes_out: Optional[str] = None,
                  original_name: Optional[str] = None):
    """
    Erzeugt eine annotierte EPUB mit:
      - visuellen <span class="rm-highlight">-Markierungen in den XHTML-Dateien
      - META-INF/calibre_bookmarks.txt (für Zotero "E-Book-Anmerkungen
        importieren…")

    Wenn Highlights nicht im EPUB gefunden werden und unmatched_out
    gesetzt ist, wird eine JSON mit Kontext geschrieben (für manuelles/
    AI-Review).
    """
    stats = {"matched": 0, "unmatched": 0, "image_listed": 0}
    unmatched_entries = []
    notes_entries = []

    # Read EPUB into memory
    with zipfile.ZipFile(original_epub, "r") as zin:
        entries = {n: zin.read(n) for n in zin.namelist()}
    opf_path = None
    opf_dir = ""
    # parse spine
    container = entries["META-INF/container.xml"]
    c_tree = etree.fromstring(container)
    rootfile = c_tree.find(".//container:rootfile", NSMAP)
    opf_path = rootfile.get("full-path")
    opf_dir = os.path.dirname(opf_path)
    opf_tree = etree.fromstring(entries[opf_path])
    manifest_id_to_href = {}
    for item in opf_tree.findall(".//opf:manifest/opf:item", NSMAP):
        manifest_id_to_href[item.get("id")] = item.get("href")
    spine_hrefs = []
    for itemref in opf_tree.findall(".//opf:spine/opf:itemref", NSMAP):
        idref = itemref.get("idref")
        href = manifest_id_to_href.get(idref)
        if href:
            full = os.path.normpath(os.path.join(opf_dir, href)) if opf_dir else href
            full = full.replace(os.sep, "/")
            spine_hrefs.append(full)

    bookmarks: list[dict] = []
    pending_matches: list[EpubMatch] = []
    spine_states: dict[int, EpubSpineState] = {}

    def get_state(spine_idx: int) -> EpubSpineState:
        state = spine_states.get(spine_idx)
        if state is None:
            path = spine_hrefs[spine_idx]
            data = entries[path]
            try:
                tree = etree.fromstring(data)
            except etree.XMLSyntaxError:
                tree = lxml_html.fromstring(data)
            state = EpubSpineState(
                spine_index=spine_idx,
                path=path,
                tree=tree,
                label=_section_label_from_tree(tree, path),
            )
            spine_states[spine_idx] = state
        if state.dirty:
            _refresh_spine_state(state)
        return state

    last_spine = 0
    last_location = None

    for hl in highlights:
        if hl.is_image:
            stats["image_listed"] += 1
            notes_entries.append({
                "section": f"Remarkable p.{hl.rm_page + 1}",
                "location": f"Bild-Highlight bbox={_fmt_bbox(hl.rm_bbox)}",
                "text": "",
                "color": hl.color,
                "is_image": True,
                "matched": False,
            })
            continue

        has_strong_context = (
            len(normalize_for_match(hl.context_before)) >= 24
            or len(normalize_for_match(hl.context_after)) >= 24
        )
        if not hl.raw_text and not hl.text:
            stats["unmatched"] += 1
            unmatched_entries.append(
                _build_unmatched_entry(hl, MatchFailure(reason="empty_highlight_text"))
            )
            notes_entries.append({
                "section": f"Remarkable p.{hl.rm_page + 1}",
                "location": "nicht sicher zugeordnet",
                "text": hl.text or hl.raw_text,
                "color": hl.color,
                "is_image": False,
                "matched": False,
            })
            continue

        # Sehr kurze Highlights wie "GTD" sind nur dann sinnvoll matchbar,
        # wenn genügend Kontext aus dem Remarkable-Export vorliegt.
        if len(hl.text) < 4 and len(hl.raw_text) < 4 and not has_strong_context:
            stats["unmatched"] += 1
            unmatched_entries.append(
                _build_unmatched_entry(hl, MatchFailure(reason="context_too_short"))
            )
            notes_entries.append({
                "section": f"Remarkable p.{hl.rm_page + 1}",
                "location": "nicht sicher zugeordnet",
                "text": hl.text or hl.raw_text,
                "color": hl.color,
                "is_image": False,
                "matched": False,
            })
            continue

        target_norm = normalize_for_match(hl.text or hl.raw_text)
        order = list(range(last_spine, len(spine_hrefs))) + \
                list(range(0, last_spine))
        if _is_heading_like_highlight(hl):
            label_hits = []
            for si in order:
                state = get_state(si)
                if _state_label_matches_target(state, target_norm):
                    label_hits.append(si)
            if label_hits:
                order = label_hits

        best_state = None
        best_match = None
        best_failure = None
        for si in order:
            state = get_state(si)
            if not state.norm_text:
                continue
            match, failure = _find_best_text_match_with_reason(
                state.norm_text,
                si,
                hl.text,
                hl.raw_text,
                hl.context_before,
                hl.context_after,
                last_spine,
                last_location,
                substitutions,
            )
            if match is None:
                if failure is not None and (
                    best_failure is None
                    or (
                        failure.best_score is not None
                        and (
                            best_failure.best_score is None
                            or failure.best_score > best_failure.best_score
                        )
                    )
                    or (
                        best_failure.reason == "no_candidate_windows"
                        and failure.reason != "no_candidate_windows"
                    )
                ):
                    best_failure = failure
                continue
            if best_match is None or match.score > best_match.score:
                best_state = state
                best_match = match
                if match.score >= CONFIDENT_MATCH_SCORE:
                    break

        if best_state is None or best_match is None:
            stats["unmatched"] += 1
            unmatched_entries.append(_build_unmatched_entry(hl, best_failure))
            notes_entries.append({
                "section": f"Remarkable p.{hl.rm_page + 1}",
                "location": "nicht sicher zugeordnet",
                "text": hl.text or hl.raw_text,
                "color": hl.color,
                "is_image": False,
                "matched": False,
            })
            continue

        best_match = _refine_epub_match(best_state, hl, best_match)
        matched_text = _text_from_state_range(
            best_state,
            best_match.start,
            best_match.end,
        ).strip() or best_match.matched_text
        output_text = _preferred_output_text(
            hl.text or hl.raw_text,
            matched_text,
        )
        pending_matches.append(EpubMatch(
            highlight=hl,
            spine_index=best_state.spine_index,
            xhtml_path=best_state.path,
            start=best_match.start,
            end=best_match.end,
            matched_text=output_text,
            section_label=best_state.label,
        ))

        notes_entries.append({
            "section": best_state.label,
            "location": f"Spine {best_state.spine_index + 1}",
            "text": output_text,
            "color": hl.color,
            "is_image": False,
            "matched": True,
        })
        last_spine = best_state.spine_index
        last_location = (best_state.spine_index, best_match.start)
        stats["matched"] += 1

    matches_by_spine: dict[int, list[EpubMatch]] = {}
    for match in pending_matches:
        matches_by_spine.setdefault(match.spine_index, []).append(match)

    # Important: Calibre/Zotero CFIs must be computed against the FINAL DOM.
    # If we generate them before inserting highlight spans, later DOM splits
    # can shift the CFI target and Zotero will import highlights at the wrong
    # text positions.
    for si in sorted(matches_by_spine):
        spine_matches = matches_by_spine[si]
        # Broader ranges are wrapped first so a contained, more specific color
        # remains visually on top, e.g. a green term inside a yellow sentence.
        for match in sorted(spine_matches, key=_epub_wrap_order_key):
            state = get_state(si)
            if match.end - 1 >= len(state.char_map):
                raise RuntimeError(
                    f"EPUB wrap range invalid for {state.path}: "
                    f"{match.start}-{match.end} vs {len(state.char_map)} chars"
                )
            start_node, start_attr, start_local = state.char_map[match.start]
            end_node, end_attr, end_local = state.char_map[match.end - 1]
            wrapped = _wrap_range_with_span(
                state.tree,
                start_node, start_attr, start_local,
                end_node, end_attr, end_local + 1,
                match.highlight.color,
            )
            if not wrapped:
                raise RuntimeError(
                    f"DOM wrap failed for {state.path}: "
                    f"{match.matched_text[:120]!r}"
                )
            state.dirty = True

        state = spine_states[si]
        if _remove_empty_highlight_spans(state.tree):
            state.dirty = True
        state = get_state(si)
        html_root = state.tree
        while html_root.getparent() is not None:
            html_root = html_root.getparent()
        for match in sorted(spine_matches, key=lambda m: (m.start, m.end)):
            if match.end - 1 >= len(state.char_map):
                raise RuntimeError(
                    f"EPUB bookmark range invalid for {state.path}: "
                    f"{match.start}-{match.end} vs {len(state.char_map)} chars"
                )
            start_node, start_attr, start_local = state.char_map[match.start]
            end_node, end_attr, end_local = state.char_map[match.end - 1]
            start_cfi = _cfi_for_position(html_root, start_node, start_attr, start_local)
            end_cfi = _cfi_for_position(html_root, end_node, end_attr, end_local + 1)
            final_text = _text_from_state_range(state, match.start, match.end) or match.matched_text
            bookmarks.append({
                "type": "highlight",
                "spine_index": match.spine_index,
                "start_cfi": start_cfi,
                "end_cfi": end_cfi,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "uuid": str(uuid.uuid4()),
                "highlighted_text": final_text,
                "style": {"kind": "color",
                          "which": CALIBRE_COLOR_MAP.get(match.highlight.color, "yellow")},
                "notes": "",
            })

    # Serialize modified XHTMLs back into entries
    for si, state in spine_states.items():
        tree = state.tree
        path = spine_hrefs[si]
        # Add CSS link in <head> if we inserted a highlight span into this tree
        _ensure_css_link(tree, opf_dir, path)
        # Preserve XML declaration + UTF-8
        entries[path] = etree.tostring(
            tree, xml_declaration=True, encoding="utf-8", pretty_print=False)

    # Add stylesheet CSS file
    css_path = os.path.normpath(os.path.join(opf_dir, "Styles", "rm-highlights.css")) \
        if opf_dir else "Styles/rm-highlights.css"
    css_path = css_path.replace(os.sep, "/")
    entries[css_path] = EPUB_HIGHLIGHT_CSS.encode("utf-8")

    # Register CSS in manifest
    _register_css_in_manifest(opf_tree, css_path, opf_dir)
    entries[opf_path] = etree.tostring(
        opf_tree, xml_declaration=True, encoding="utf-8", pretty_print=False)

    # Write calibre_bookmarks.txt
    if bookmarks:
        payload = json.dumps(bookmarks, ensure_ascii=False).encode("utf-8")
        b64 = base64.b64encode(payload)
        # Wrap at 76 chars (Calibre style)
        wrapped = b"\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        entries["META-INF/calibre_bookmarks.txt"] = (
            b"encoding=json+base64:\n" + wrapped)

    # Write out new EPUB zip with mimetype first, uncompressed
    with zipfile.ZipFile(output_path, "w") as zout:
        # mimetype MUST be first and uncompressed
        if "mimetype" in entries:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zout.writestr(info, entries["mimetype"])
        for name, data in entries.items():
            if name == "mimetype":
                continue
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(info, data)

    # Optional: schreibe unmatched als JSON (für AI/manuelles Review)
    if unmatched_out and unmatched_entries:
        with open(unmatched_out, "w", encoding="utf-8") as f:
            json.dump(unmatched_entries, f, indent=2, ensure_ascii=False)
    elif unmatched_out and os.path.exists(unmatched_out):
        os.remove(unmatched_out)

    if notes_out:
        generate_notes_md(
            highlights,
            notes_out,
            original_name or Path(original_epub).name,
            notes_entries=notes_entries,
        )

    if verbose:
        print(f"  EPUB Output: {stats['matched']} Highlights eingefügt, "
              f"{stats['unmatched']} nicht gefunden im EPUB-Text")
        if stats["image_listed"]:
            print(f"  ({stats['image_listed']} Bild-Highlights übersprungen — "
                  f"Remarkable-Highlight lag auf Bild/Figur ohne Textschicht)")
        if bookmarks:
            print(f"  META-INF/calibre_bookmarks.txt: {len(bookmarks)} Einträge "
                  f"(Zotero: Rechtsklick → Datei → 'E-Book-Anmerkungen importieren…')")
        if unmatched_out and unmatched_entries:
            print(f"  Unmatched-Kontext: {unmatched_out} "
                  f"({len(unmatched_entries)} Einträge — für Review)")
        if notes_out:
            print(f"  Markdown-Fallback: {notes_out}")
    return stats


def _loose_ascii(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _ensure_css_link(tree, opf_dir: str, xhtml_path: str):
    """Insert <link rel=stylesheet href=...> into <head> if not already there."""
    head = tree.find(".//{http://www.w3.org/1999/xhtml}head")
    if head is None:
        head = tree.find(".//head")
    if head is None:
        return
    # Relative path from xhtml_path to Styles/rm-highlights.css (in opf_dir)
    css_abs = os.path.normpath(os.path.join(opf_dir, "Styles", "rm-highlights.css")) \
        if opf_dir else "Styles/rm-highlights.css"
    css_abs = css_abs.replace(os.sep, "/")
    xhtml_dir = os.path.dirname(xhtml_path).replace(os.sep, "/")
    rel = os.path.relpath(css_abs, xhtml_dir).replace(os.sep, "/")
    # check existing
    for link in head.findall("{http://www.w3.org/1999/xhtml}link"):
        if link.get("href") == rel:
            return
    for link in head.findall("link"):
        if link.get("href") == rel:
            return
    xhtml_ns = "{http://www.w3.org/1999/xhtml}"
    if etree.QName(head).namespace:
        link = etree.SubElement(head, f"{xhtml_ns}link")
    else:
        link = etree.SubElement(head, "link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", rel)


def _register_css_in_manifest(opf_tree, css_abs_path: str, opf_dir: str):
    manifest = opf_tree.find(".//opf:manifest", NSMAP)
    if manifest is None:
        return
    css_href = os.path.relpath(css_abs_path, opf_dir).replace(os.sep, "/") \
        if opf_dir else css_abs_path
    # Already present?
    for item in manifest.findall("opf:item", NSMAP):
        if item.get("href") == css_href:
            return
    # Add new item
    item = etree.SubElement(manifest, "{http://www.idpf.org/2007/opf}item")
    item.set("id", "rm-highlights-css")
    item.set("href", css_href)
    item.set("media-type", "text/css")


# ============================================================
# NOTES.MD SIDECAR
# ============================================================

def generate_notes_md(highlights: list[Highlight], output_path: str,
                      original_name: str, notes_entries: Optional[list[dict]] = None):
    """
    Schreibt eine Markdown-Datei mit allen Highlights als Blockquotes,
    gruppiert nach Remarkable-Seitenzahl. Zum manuellen Import in
    Zotero/Obsidian.
    """
    lines = [
        f"# Highlights: {original_name}",
        "",
        f"_Quelle: Remarkable-Export. Extrahiert mit "
        f"rm-highlights-to-annotations.py._",
        "",
    ]
    if notes_entries:
        grouped_entries = {}
        for entry in notes_entries:
            grouped_entries.setdefault(entry["section"], []).append(entry)
        for section, entries_in_section in grouped_entries.items():
            lines.append(f"## {section}")
            lines.append("")
            for entry in entries_in_section:
                if entry.get("is_image"):
                    lines.append(
                        f"- Bild-Highlight _({entry['color']})_ — {entry['location']}"
                    )
                    continue
                prefix = "> "
                if not entry.get("matched", True):
                    prefix = "> [Nicht sicher gemappt] "
                color_tag = f" _({entry['color']})_" if entry["color"] != "yellow" else ""
                location = f" [{entry['location']}]" if entry.get("location") else ""
                lines.append(f"{prefix}{entry['text']}{color_tag}{location}")
                lines.append("")
            lines.append("")
    else:
        # Group by rm_page
        by_page: dict[int, list[Highlight]] = {}
        for h in highlights:
            by_page.setdefault(h.rm_page, []).append(h)

        for page_idx in sorted(by_page):
            lines.append(f"## Remarkable p.{page_idx + 1}")
            lines.append("")
            for h in by_page[page_idx]:
                if h.is_image:
                    lines.append(f"- [Bild-Highlight, {h.color}, "
                                 f"bbox={_fmt_bbox(h.rm_bbox)}]")
                else:
                    color_tag = f" _({h.color})_" if h.color != "yellow" else ""
                    lines.append(f"> {h.text}{color_tag}")
                    lines.append("")
            lines.append("")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _fmt_bbox(bbox):
    return "(" + ", ".join(f"{c:.0f}" for c in bbox) + ")"


# ============================================================
# INTERACTIVE REVIEW (wie bisher)
# ============================================================

def interactive_review(repairer):
    if not repairer.unresolved:
        print("\n✓ Alle Wörter wurden automatisch aufgelöst.")
        return
    print(f"\n{len(repairer.unresolved)} nicht aufgelöste Wörter:\n")
    print("Befehle: ENTER = überspringen, 'q' = beenden, sonst Korrektur eintippen")
    print("-" * 60)
    for i, (word, context) in enumerate(repairer.unresolved):
        word_visible = "".join(f"⟦{hex(ord(c))}⟧" if ord(c) < 0x20 else c for c in word)
        ctx_visible = "".join(f"⟦{hex(ord(c))}⟧" if ord(c) < 0x20 else c
                               for c in context[:80])
        print(f"\n[{i+1}/{len(repairer.unresolved)}] '{word_visible}'")
        print(f"  Kontext: ...{ctx_visible}...")
        try:
            answer = input("  → ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAbgebrochen.")
            break
        if answer == "q":
            print("Beendet.")
            break
        if answer:
            repairer.custom_words[word] = answer
            print(f"  ✓ Gespeichert: {word!r} → {answer!r}")
    if repairer.custom_dict_path:
        repairer.save_custom_dict()
        print(f"\nCustom-Dict gespeichert: {repairer.custom_dict_path}")


# ============================================================
# CLI
# ============================================================

def _default_output_path(original_path: Path) -> Path:
    return original_path.with_name(original_path.stem + ".annotated" + original_path.suffix)


def _sidecar_path(base_path: Path, suffix: str) -> Path:
    return base_path.with_name(base_path.stem + suffix)


def _write_review_bundle(review_path: Path, original: Path, remarkable: Path,
                         output: Path, ext: str, unmatched_out: Optional[str],
                         unresolved: list[tuple[str, str]],
                         notes_out: Optional[Path] = None):
    unmatched_entries = []
    if unmatched_out and os.path.exists(unmatched_out):
        with open(unmatched_out, encoding="utf-8") as f:
            unmatched_entries = json.load(f)

    unresolved_entries = [
        {"word": word, "context": context, "reason": "ligature_unresolved"}
        for word, context in unresolved
    ]
    review_bundle = {
        "status": "needs_review" if unmatched_entries or unresolved_entries else "final",
        "requires_review": bool(unmatched_entries or unresolved_entries),
        "original": str(original),
        "remarkable": str(remarkable),
        "output": str(output),
        "output_format": ext.lstrip("."),
        "notes_out": str(notes_out) if notes_out else None,
        "unmatched_out": unmatched_out if unmatched_entries else None,
        "unmatched_count": len(unmatched_entries),
        "unmatched": unmatched_entries,
        "unresolved_count": len(unresolved_entries),
        "unresolved": unresolved_entries,
    }
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(review_bundle, f, indent=2, ensure_ascii=False)
    return review_bundle


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original", help="Original-Datei (EPUB oder PDF)")
    parser.add_argument("remarkable", help="Remarkable-Export-PDF")
    parser.add_argument("-o", "--output",
                        help="Output-Pfad (default: <original>.annotated.<ext>)")
    parser.add_argument("--extract-json",
                        help="Phase 1: Nur extrahieren und als JSON schreiben "
                             "(kein Output-File)")
    parser.add_argument("--extract-in",
                        help="Phase 3: Extrahierte Highlights aus JSON laden "
                             "statt sie neu aus Remarkable-PDF zu ziehen")
    parser.add_argument("--custom-dict",
                        default=os.path.expanduser("~/.rm_repair_dict.json"),
                        help="Custom-Wörterbuch (default: ~/.rm_repair_dict.json)")
    parser.add_argument("--unresolved-out",
                        help="Schreibt nicht-aufgelöste Ligatur-Wörter als JSON")
    parser.add_argument("--unmatched-out",
                        help="Schreibt Highlights die nicht im Original "
                             "platziert werden konnten (Kontext für AI-Review)")
    parser.add_argument("--interactive", action="store_true",
                        help="Nach Konvertierung nach unaufgelösten Wörtern fragen")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    original = Path(args.original)
    remarkable = Path(args.remarkable)

    if not original.exists():
        print(f"Original nicht gefunden: {original}", file=sys.stderr)
        sys.exit(1)
    if not args.extract_in and not remarkable.exists():
        print(f"Remarkable-Export nicht gefunden: {remarkable}", file=sys.stderr)
        sys.exit(1)

    ext = original.suffix.lower()
    if ext not in (".epub", ".pdf"):
        print(f"Original-Format nicht unterstützt: {ext}", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else _default_output_path(original)
    unmatched_out = args.unmatched_out or str(_sidecar_path(output, ".unmatched.json"))
    notes_out = _sidecar_path(output, ".notes.md") if ext == ".epub" else None
    review_out = _sidecar_path(output, ".review.json")

    verbose = not args.quiet
    if verbose:
        print(f"→ Original:   {original.name}")
        print(f"  Remarkable: {remarkable.name}")

    # === Phase 1: Extrahiere Highlights aus Remarkable-PDF ===
    repairer = LigatureRepairer(custom_dict_path=args.custom_dict, verbose=verbose)

    if args.extract_in:
        highlights = _load_highlights_json(args.extract_in)
        if verbose:
            print(f"  Lade Highlights aus {args.extract_in}: "
                  f"{len(highlights)} Einträge")
    else:
        highlights, _extract_stats = extract_highlights_from_rm(
            str(remarkable), repairer, verbose=verbose)

    if not highlights:
        print("Keine Highlights gefunden.")
        sys.exit(0)

    # Phase 1b: Optional nur extrahieren, nicht applyen
    if args.extract_json:
        _dump_highlights_json(highlights, args.extract_json)
        if verbose:
            print(f"→ Extract-JSON: {args.extract_json} "
                  f"({len(highlights)} Einträge)")
        sys.exit(0)

    # === Phase 2: Annotiere Original ===
    if ext == ".pdf":
        if verbose:
            print(f"\n→ Schreibe annotiertes PDF: {output.name}")
        annotate_pdf(str(original), highlights, str(output),
                     MATCH_SUBSTITUTIONS, verbose=verbose,
                     unmatched_out=unmatched_out)
    else:  # .epub
        if verbose:
            print(f"\n→ Schreibe annotiertes EPUB: {output.name}")
        annotate_epub(str(original), highlights, str(output),
                      MATCH_SUBSTITUTIONS, verbose=verbose,
                      unmatched_out=unmatched_out,
                      notes_out=str(notes_out) if notes_out else None,
                      original_name=original.name)

    # === Phase 3: Unresolved & Interactive ===
    if args.unresolved_out:
        unresolved_data = [{"word": w, "context": ctx}
                           for w, ctx in repairer.unresolved]
        with open(args.unresolved_out, "w", encoding="utf-8") as f:
            json.dump(unresolved_data, f, indent=2, ensure_ascii=False)
        if verbose:
            print(f"  Unresolved-Liste: {args.unresolved_out} "
                  f"({len(unresolved_data)} Wörter)")

    review_bundle = _write_review_bundle(
        review_out,
        original,
        remarkable,
        output,
        ext,
        unmatched_out,
        repairer.unresolved,
        notes_out,
    )
    if verbose:
        print(f"  Review-Paket: {review_out} "
              f"({review_bundle['status']})")
    if args.interactive:
        interactive_review(repairer)

    if verbose:
        print("\nFertig.")
        if ext == ".epub":
            print("→ In Zotero: Rechtsklick auf das Item → Datei → "
                  "'E-Book-Anmerkungen importieren…' (englisch: "
                  "'Import Ebook Annotations…'). Danach: 'Notiz aus "
                  "Anmerkungen hinzufügen'. Wenn Zotero die eingebetteten "
                  "E-Book-Annotations nicht sauber übernimmt, liegt ein "
                  "Markdown-Fallback neben der EPUB.")
        else:
            print("→ In Zotero: PDF re-loaden (Rechtsklick → Reload). "
                  "Externe Annotations müssen einmalig 'importiert' werden "
                  "(Schloss-Symbol bis zum Import). Dann: 'Notiz aus "
                  "Anmerkungen hinzufügen'.")


def _dump_highlights_json(highlights: list[Highlight], path: str):
    data = [
        {
            "color": h.color,
            "text": h.text,
            "raw_text": h.raw_text,
            "is_image": h.is_image,
            "rm_page": h.rm_page,
            "rm_bbox": list(h.rm_bbox),
            "context_before": h.context_before,
            "context_after": h.context_after,
        }
        for h in highlights
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_highlights_json(path: str) -> list[Highlight]:
    with open(path) as f:
        data = json.load(f)
    return [
        Highlight(
            color=d["color"],
            text=d.get("text", ""),
            raw_text=d.get("raw_text", ""),
            is_image=d.get("is_image", False),
            rm_page=d["rm_page"],
            rm_bbox=tuple(d["rm_bbox"]),
            context_before=d.get("context_before", ""),
            context_after=d.get("context_after", ""),
        )
        for d in data
    ]


if __name__ == "__main__":
    main()
