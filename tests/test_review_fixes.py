import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "rm-highlights-to-annotations.py"


def load_module():
    spec = importlib.util.spec_from_file_location("rmha_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rmha = load_module()


def make_state(text: str):
    tree = rmha.etree.fromstring(
        f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{text}</p></body></html>'.encode("utf-8")
    )
    state = rmha.EpubSpineState(
        spine_index=0,
        path="test.xhtml",
        tree=tree,
        label="test",
    )
    rmha._refresh_spine_state(state)
    return state


class ReviewFixTests(unittest.TestCase):
    def test_trim_match_range_drops_trailing_footnote_digits(self):
        target = "“If your product is great, it doesn’t need to be good.”"
        state = make_state(target + "18")
        start, end, text = rmha._trim_match_range_to_target(
            state, target, 0, len(state.char_map)
        )
        self.assertLess(end, len(state.char_map))
        self.assertEqual(text, target)

    def test_find_best_text_match_reports_missing_candidates(self):
        match, failure = rmha._find_best_text_match_with_reason(
            "alpha beta gamma",
            0,
            "delta epsilon",
            "delta epsilon",
            "",
            "",
            0,
            None,
            rmha.MATCH_SUBSTITUTIONS,
        )
        self.assertIsNone(match)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.reason, "no_candidate_windows")

    def test_find_best_text_match_reports_short_text_without_context(self):
        match, failure = rmha._find_best_text_match_with_reason(
            "alpha beta gamma",
            0,
            "A",
            "A",
            "",
            "",
            0,
            None,
            rmha.MATCH_SUBSTITUTIONS,
        )
        self.assertIsNone(match)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.reason, "context_too_short")

    def test_flexible_regex_keeps_identity_for_ascii_bug_candidates(self):
        text = "this is referred to as the mean-or average- volatility level."
        target = "This is referred to as the mean—or average— volatility level."

        candidates = rmha._build_candidate_windows(
            text,
            target,
            target,
            rmha.MATCH_SUBSTITUTIONS,
        )

        self.assertTrue(candidates)

    def test_short_pdf_candidates_do_not_match_inside_words(self):
        candidates = rmha._build_candidate_windows(
            "otherwise, the covered call strategy",
            "The",
            "The",
            rmha.MATCH_SUBSTITUTIONS,
        )

        matched = ["otherwise, the covered call strategy"[start:end] for start, end, _ in candidates]
        self.assertIn("the", matched)
        self.assertNotIn("the", ["otherwise"[start:end] for start, end, _ in candidates if end <= len("otherwise")])

    def test_short_pdf_candidates_scan_beyond_first_eight_hits(self):
        text = " ".join(["the"] * 12)

        candidates = rmha._build_candidate_windows(
            text,
            "The",
            "The",
            rmha.MATCH_SUBSTITUTIONS,
        )

        self.assertGreater(len(candidates), 8)

    def test_single_word_candidates_match_pdf_line_hyphenation(self):
        candidates = rmha._build_candidate_windows(
            "the option's delta. mon- eyness describes the degree",
            "Moneyness",
            "Moneyness",
            rmha.MATCH_SUBSTITUTIONS,
        )

        matched = [
            "the option's delta. mon- eyness describes the degree"[start:end]
            for start, end, _ in candidates
        ]
        self.assertIn("mon- eyness", matched)

    def test_context_ratio_penalizes_delayed_after_context(self):
        expected = "describes the degree to which the option is in- or out-of-the-money."
        immediate = "describes the degree to which the option is in- or out-of-the-money."
        delayed = "on the option's delta. mon- eyness describes the degree to which the option is in- or out-of-the-money."

        self.assertGreater(
            rmha._context_ratio(expected, immediate, tail=False),
            rmha._context_ratio(expected, delayed, tail=False),
        )

    def test_line_hyphen_score_can_beat_earlier_exact_word_with_better_context(self):
        target = "moneyness"
        context_before = "the next observation is the effect of moneyness on the option's delta."
        context_after = "describes the degree to which the option is in- or out-of-the-money."
        earlier = "the next observation is the effect of moneyness on the option's delta. mon- eyness describes"
        line_hyphen = "the next observation is the effect of moneyness on the option's delta. mon- eyness describes"
        earlier_start = earlier.index("moneyness")
        earlier_end = earlier_start + len("moneyness")
        hyphen_start = line_hyphen.index("mon- eyness")
        hyphen_end = hyphen_start + len("mon- eyness")

        exact_score = rmha._score_match_window(
            earlier, earlier_start, earlier_end, target,
            context_before, context_after, 0, 0, None, "exact")
        hyphen_score = rmha._score_match_window(
            line_hyphen, hyphen_start, hyphen_end, target,
            context_before, context_after, 0, 0, None, "line_hyphen")

        self.assertGreater(hyphen_score, exact_score)

    def test_stable_annotation_key_is_deterministic(self):
        source = {
            "kind": "text",
            "page_index": 12,
            "color": "yellow",
            "text": "Clean annotation text",
            "geometry": [[1.0, 2.0, 3.0, 2.0, 1.0, 4.0, 3.0, 4.0]],
        }
        key1 = rmha._stable_annotation_key(source)
        key2 = rmha._stable_annotation_key(dict(source))
        changed = dict(source)
        changed["page_index"] = 13
        key3 = rmha._stable_annotation_key(changed)
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_classify_color_recognizes_remarkable_export_colors(self):
        self.assertEqual(rmha.classify_color((1.0, 0.929, 0.459)), "yellow")
        self.assertEqual(rmha.classify_color((0.675, 1.0, 0.522)), "green")
        self.assertEqual(rmha.classify_color((0.949, 0.62, 1.0)), "pink")
        self.assertEqual(rmha.classify_color((1.0, 0.765, 0.549)), "orange")
        self.assertIsNone(rmha.classify_color((1.0, 1.0, 1.0)))
        self.assertIsNone(rmha.classify_color((0.137, 0.122, 0.125)))

    def test_drawing_highlight_rects_splits_combined_remarkable_paths(self):
        drawing = {
            "items": [
                ("l", rmha.fitz.Point(10, 20), rmha.fitz.Point(30, 20)),
                ("l", rmha.fitz.Point(30, 20), rmha.fitz.Point(30, 32)),
                ("l", rmha.fitz.Point(30, 32), rmha.fitz.Point(10, 32)),
                ("l", rmha.fitz.Point(50, 80), rmha.fitz.Point(90, 80)),
                ("l", rmha.fitz.Point(90, 80), rmha.fitz.Point(90, 92)),
                ("l", rmha.fitz.Point(90, 92), rmha.fitz.Point(50, 92)),
            ],
            "rect": rmha.fitz.Rect(10, 20, 90, 92),
        }

        rects = rmha._drawing_highlight_rects(drawing)

        self.assertEqual(len(rects), 2)
        self.assertEqual(tuple(rects[0]), (10.0, 20.0, 30.0, 32.0))
        self.assertEqual(tuple(rects[1]), (50.0, 80.0, 90.0, 92.0))

    def test_word_passages_split_geometrically_separate_terms(self):
        words = [
            (10.0, 10.0, 40.0, 20.0, "naked", 0, 0, 0),
            (10.0, 100.0, 50.0, 110.0, "covered", 1, 0, 0),
        ]
        rects = [
            rmha.fitz.Rect(10.0, 10.0, 40.0, 20.0),
            rmha.fitz.Rect(10.0, 100.0, 50.0, 110.0),
        ]

        passages, image_passages = rmha._group_color_rects_into_word_passages(words, rects)

        self.assertEqual(len(passages), 2)
        self.assertEqual(image_passages, [])
        self.assertEqual(passages[0][1], [0])
        self.assertEqual(passages[1][1], [1])

    def test_word_passages_split_same_line_terms_with_large_gap(self):
        words = [
            (10.0, 10.0, 40.0, 20.0, "long", 0, 0, 0),
            (44.0, 10.0, 62.0, 20.0, "put", 0, 0, 1),
            (100.0, 10.0, 150.0, 20.0, "protective", 0, 0, 2),
            (154.0, 10.0, 172.0, 20.0, "put", 0, 0, 3),
        ]
        rects = [
            rmha.fitz.Rect(10.0, 10.0, 62.0, 20.0),
            rmha.fitz.Rect(100.0, 10.0, 172.0, 20.0),
        ]

        passages, image_passages = rmha._group_color_rects_into_word_passages(words, rects)

        self.assertEqual(len(passages), 2)
        self.assertEqual(image_passages, [])
        self.assertEqual(passages[0][1], [0, 1])
        self.assertEqual(passages[1][1], [2, 3])

    def test_word_passages_split_wrapped_terms_with_unmarked_line_prefix(self):
        words = [
            (320.0, 10.0, 370.0, 20.0, "long", 0, 0, 0),
            (374.0, 10.0, 392.0, 20.0, "put", 0, 0, 1),
            (10.0, 23.0, 34.0, 33.0, "and", 1, 0, 0),
            (38.0, 23.0, 58.0, 33.0, "the", 1, 0, 1),
            (90.0, 23.0, 140.0, 33.0, "protective", 1, 0, 2),
            (144.0, 23.0, 162.0, 33.0, "put", 1, 0, 3),
        ]
        rects = [
            rmha.fitz.Rect(320.0, 10.0, 392.0, 20.0),
            rmha.fitz.Rect(90.0, 23.0, 162.0, 33.0),
        ]

        passages, image_passages = rmha._group_color_rects_into_word_passages(words, rects)

        self.assertEqual(len(passages), 2)
        self.assertEqual(image_passages, [])
        self.assertEqual(passages[0][1], [0, 1])
        self.assertEqual(passages[1][1], [4, 5])

    def test_build_unmatched_entry_includes_reason_metadata(self):
        highlight = rmha.Highlight(
            color="yellow",
            text="foo",
            raw_text="foo",
            is_image=False,
            rm_page=4,
            rm_bbox=(0, 0, 10, 10),
            context_before="before",
            context_after="after",
        )
        failure = rmha.MatchFailure(
            reason="no_fuzzy_match",
            candidate_count=3,
            best_score=0.41,
            best_method="anchor",
        )
        entry = rmha._build_unmatched_entry(highlight, failure)
        self.assertEqual(entry["rm_page"], 5)
        self.assertEqual(entry["reason"], "no_fuzzy_match")
        self.assertEqual(entry["candidate_count"], 3)
        self.assertEqual(entry["best_method"], "anchor")
        self.assertEqual(entry["best_score"], 0.41)

    def test_join_pdf_word_text_preserves_human_text(self):
        words = [
            (0, 0, 10, 10, "Theoretical"),
            (12, 0, 20, 10, "value—what"),
            (22, 0, 30, 10, "a"),
            (32, 0, 40, 10, "concept!"),
            (42, 0, 50, 10, "Speciﬁcally,"),
        ]
        text = rmha._join_pdf_word_text(words)
        self.assertEqual(text, "Theoretical value—what a concept! Specifically,")

    def test_preferred_output_text_keeps_ai_reviewed_real_word_fix(self):
        preferred = "sell the stock at the strike price"
        matched = "sell the sock at the strike price"

        text = rmha._preferred_output_text(preferred, matched)

        self.assertEqual(text, preferred)

    def test_join_pdf_word_text_keeps_visible_compound_hyphens(self):
        words = [
            (300, 10, 330, 20, "and—"),
            (10, 24, 40, 34, "since"),
            (300, 40, 330, 50, "buy-"),
            (10, 54, 40, 64, "write"),
            (300, 70, 330, 80, "in-"),
            (10, 84, 40, 94, "or"),
        ]
        text = rmha._join_pdf_word_text(words)
        self.assertEqual(text, "and— since buy- write in- or")

    def test_join_pdf_word_text_merges_true_line_hyphenation(self):
        words = [
            (300, 10, 330, 20, "vol-"),
            (10, 24, 40, 34, "atility"),
        ]
        text = rmha._join_pdf_word_text(words)
        self.assertEqual(text, "volatility")

    def test_join_pdf_word_text_merges_capitalized_short_line_hyphenation(self):
        words = [
            (350, 10, 380, 20, "Mon-"),
            (10, 24, 50, 34, "eyness"),
        ]
        text = rmha._join_pdf_word_text(words)
        self.assertEqual(text, "Moneyness")

    def test_search_text_in_pdf_returns_original_word_text(self):
        doc = rmha.fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Option Type There are two types of options: Calls")
        highlight_text = "option type there are two types of options: c"
        result, failure = rmha._search_text_in_pdf(
            doc,
            highlight_text,
            highlight_text,
            "",
            "",
            rmha.MATCH_SUBSTITUTIONS,
            hint_page=0,
        )
        try:
            self.assertIsNone(failure)
            self.assertIsNotNone(result)
            self.assertEqual(
                result.matched_text,
                "Option Type There are two types of options: Calls",
            )
        finally:
            doc.close()

    def test_line_quads_merge_words_on_same_pdf_line(self):
        words = [
            (10.0, 20.0, 30.0, 30.0, "Option", 1, 2, 0),
            (34.0, 20.0, 50.0, 30.0, "class", 1, 2, 1),
            (55.0, 20.0, 80.0, 30.0, "means", 1, 2, 2),
        ]

        quads = rmha._line_quads_from_word_hits(words)

        self.assertEqual(len(quads), 1)
        self.assertEqual(quads[0].ul.x, 10.0)
        self.assertEqual(quads[0].ur.x, 80.0)
        self.assertEqual(quads[0].ul.y, 20.0)
        self.assertEqual(quads[0].ll.y, 30.0)

    def test_line_quads_keep_single_word_highlights_precise(self):
        words = [
            (10.0, 20.0, 42.0, 30.0, "volatility", 1, 2, 0),
        ]

        quads = rmha._line_quads_from_word_hits(words)

        self.assertEqual(len(quads), 1)
        self.assertEqual(quads[0].ul.x, 10.0)
        self.assertEqual(quads[0].ur.x, 42.0)

    def test_line_quads_split_large_gaps_and_different_lines(self):
        words = [
            (10.0, 20.0, 30.0, 30.0, "left", 1, 2, 0),
            (120.0, 20.0, 150.0, 30.0, "right", 1, 2, 1),
            (10.0, 40.0, 40.0, 50.0, "next", 1, 3, 0),
        ]

        quads = rmha._line_quads_from_word_hits(words)

        self.assertEqual(len(quads), 3)
        self.assertEqual(quads[0].ul.x, 10.0)
        self.assertEqual(quads[1].ul.x, 120.0)
        self.assertEqual(quads[2].ul.y, 40.0)

    def test_pdf_context_refine_drops_leading_context_words(self):
        page_text = rmha.normalize_for_match(
            "All models achieve the same end: the option's theoretical value. "
            "For American-exercise equity options, six inputs are entered."
        )
        match = rmha.TextMatch(
            container_index=0,
            start=page_text.index("end: the option"),
            end=len(page_text),
            score=0.7,
            matched_text=page_text[page_text.index("end: the option"):],
            method="anchor",
        )
        refined = rmha._refine_pdf_match_range(
            page_text,
            match,
            "y g y option's theoretical value. For American-exercise equity options, six inputs are entered.",
            "y g y option's theoretical value. For American-exercise equity options, six inputs are entered.",
            "All models achieve the same end: the",
            "",
        )
        self.assertEqual(
            refined.matched_text,
            "option's theoretical value. for american-exercise equity options, six inputs are entered.",
        )

    def test_pdf_context_refine_keeps_short_context_tokens(self):
        page_text = rmha.normalize_for_match(
            "Stock A has a higher historical volatility than Stock B. "
            "Historical volatility (HV) is the annualized standard deviation."
        )
        match = rmha.TextMatch(
            container_index=0,
            start=page_text.index("b. historical"),
            end=page_text.index(" is the annualized"),
            score=0.8,
            matched_text="b. historical volatility (hv)",
            method="anchor",
        )
        refined = rmha._refine_pdf_match_range(
            page_text,
            match,
            "p Historical volatility (HV)",
            "p Historical volatility (HV)",
            "Stock A has a higher historical volatility than Stock B.",
            "is the annualized standard deviation.",
        )
        self.assertEqual(refined.matched_text, "historical volatility (hv)")

    def test_remove_empty_highlight_spans_preserves_text(self):
        tree = rmha.etree.fromstring(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            b'A<span class="rm-highlight rm-highlight-yellow"> </span>B'
            b'</p></body></html>'
        )
        removed = rmha._remove_empty_highlight_spans(tree)
        self.assertEqual(removed, 1)
        self.assertEqual("".join(tree.itertext()), "A B")
        self.assertFalse(tree.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), " rm-highlight ")]'
        ))

    def test_epub_wrap_order_keeps_specific_color_visible(self):
        state = make_state("alpha beta gamma")
        yellow = rmha.Highlight(
            color="yellow", text="", raw_text="", is_image=False,
            rm_page=0, rm_bbox=(0, 0, 0, 0),
        )
        green = rmha.Highlight(
            color="green", text="", raw_text="", is_image=False,
            rm_page=0, rm_bbox=(0, 0, 0, 0),
        )
        beta_start = state.norm_text.index("beta")
        beta_end = beta_start + len("beta")
        matches = [
            rmha.EpubMatch(
                highlight=yellow,
                spine_index=0,
                xhtml_path="test.xhtml",
                start=0,
                end=len(state.char_map),
                matched_text="alpha beta gamma",
                section_label="test",
            ),
            rmha.EpubMatch(
                highlight=green,
                spine_index=0,
                xhtml_path="test.xhtml",
                start=beta_start,
                end=beta_end,
                matched_text="beta",
                section_label="test",
            ),
        ]

        for match in sorted(matches, key=rmha._epub_wrap_order_key):
            if state.dirty:
                rmha._refresh_spine_state(state)
            start_node, start_attr, start_local = state.char_map[match.start]
            end_node, end_attr, end_local = state.char_map[match.end - 1]
            self.assertTrue(rmha._wrap_range_with_span(
                state.tree,
                start_node, start_attr, start_local,
                end_node, end_attr, end_local + 1,
                match.highlight.color,
            ))
            state.dirty = True

        green_span = state.tree.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), " rm-highlight-green ")]'
        )[0]
        self.assertIn("rm-highlight-yellow", green_span.getparent().get("class", ""))
        self.assertFalse(green_span.xpath(
            './/*[contains(concat(" ", normalize-space(@class), " "), " rm-highlight-yellow ")]'
        ))

    def test_epub_member_validation_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.epub"
            with zipfile.ZipFile(path, "w") as zout:
                zout.writestr("mimetype", "application/epub+zip")
                zout.writestr("../evil.txt", "nope")

            with self.assertRaises(ValueError):
                rmha._read_epub_entries(str(path))

    def test_epub_href_resolution_allows_safe_parent_reference(self):
        resolved = rmha._epub_join_path("OPS/Text", "../Images/cover.xhtml")

        self.assertEqual(resolved, "OPS/Images/cover.xhtml")


if __name__ == "__main__":
    unittest.main()
