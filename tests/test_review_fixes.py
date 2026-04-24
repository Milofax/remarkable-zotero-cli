import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Volumes/DATEN/Coding/remarkable-zotero-cli")
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


if __name__ == "__main__":
    unittest.main()
