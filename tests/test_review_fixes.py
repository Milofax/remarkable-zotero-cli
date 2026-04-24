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
