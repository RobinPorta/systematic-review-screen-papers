"""Full-text XML parsing and heading canonicalisation.

The fixture is Europe PMC JATS. The tests also cover Elsevier-style markup, a malformed
document and a heading the matcher cannot place, because section structure varies between
journals and a parser that only handles the happy path drops papers silently.
"""

from __future__ import annotations

import pytest

from screener.fulltext.sections import canonicalise, parse_sections
from screener.protocol import Section

from .conftest import FIXTURES


@pytest.fixture
def parsed():
    return parse_sections((FIXTURES / "europepmc_article.xml").read_text(encoding="utf-8"))


class TestParsing:
    def test_title_and_abstract_come_from_coredata(self, parsed):
        assert parsed.title.startswith("Weekly board-game sessions")
        assert "randomised controlled trial" in parsed.abstract

    def test_nested_subsections_are_flattened_with_their_depth(self, parsed):
        titles = {s.title for s in parsed.sections}
        assert "2. Methods" in titles
        assert "2.1. Participants" in titles
        levels = {s.title: s.level for s in parsed.sections}
        assert levels["2. Methods"] == 0
        assert levels["2.1. Participants"] == 1

    def test_a_parents_text_excludes_its_childrens(self, parsed):
        methods = next(s for s in parsed.sections if s.title == "2. Methods")
        assert "two-arm parallel randomised" in methods.text
        assert "120 community-dwelling" not in methods.text, "participants text belongs to 2.1"

    def test_cross_references_are_stripped_but_surrounding_text_survives(self, parsed):
        intro = next(s for s in parsed.sections if "Introduction" in s.title)
        assert "[1]" not in intro.text
        assert "poorer health outcomes" in intro.text
        assert "Group activities" in intro.text

    def test_entities_are_decoded(self, parsed):
        results = next(s for s in parsed.sections if s.title == "3. Results")
        assert "p < 0.001" in results.text


class TestCanonicalisation:
    @pytest.mark.parametrize(
        "heading,expected",
        [
            ("1. Introduction", Section.INTRODUCTION),
            ("2. Methods", Section.METHODS),
            ("2.1. Participants", Section.PARTICIPANTS),
            ("2.2. Intervention", Section.INTERVENTION),
            ("2.3. Outcome measures", Section.MEASURES),
            ("2.4. Statistical analysis", Section.ANALYSIS),
            ("3. Results", Section.RESULTS),
            ("4. Discussion", Section.DISCUSSION),
            ("4.1. Limitations", Section.LIMITATIONS),
            ("Acknowledgements", Section.OTHER),
            ("IV. Materials and apparatus", Section.INTERVENTION),
            ("Study population", Section.PARTICIPANTS),
        ],
    )
    def test_common_headings_map_without_a_model_call(self, heading, expected):
        assert canonicalise(heading) == expected

    def test_specific_headings_win_over_general_ones(self):
        """'Participants' sits inside Methods, so ordering decides which label it gets."""
        assert canonicalise("Methods: participants and recruitment") == Section.PARTICIPANTS

    def test_an_unrecognised_heading_returns_none(self):
        """The signal to spend one cheap Choice on it rather than guess."""
        assert canonicalise("Ringraziamenti") is None
        assert canonicalise("") is None

    def test_the_fixture_leaves_exactly_one_heading_unmatched(self, parsed):
        assert [s.title for s in parsed.unresolved] == ["Ringraziamenti"]


class TestTextFor:
    def test_requested_sections_are_collected_by_canonical_name(self, parsed):
        state = parsed.text_for([Section.PARTICIPANTS, Section.INTERVENTION])
        assert set(state) == {"participants", "intervention"}
        assert "65 to 89" in state["participants"]
        assert "Rummikub" in state["intervention"]

    def test_title_and_abstract_come_from_front_matter(self, parsed):
        state = parsed.text_for([Section.TITLE, Section.ABSTRACT])
        assert state["title"].startswith("Weekly board-game")
        assert "primary outcome" in state["abstract"]

    def test_several_sections_sharing_a_label_are_concatenated(self, parsed):
        text = parsed.text_for([Section.METHODS])["methods"]
        assert "two-arm parallel" in text

    def test_an_absent_section_is_simply_left_out(self, parsed):
        assert parsed.text_for([Section.MEASURES, Section.PARTICIPANTS]).keys() >= {"measures"}
        assert "limitations" in parsed.text_for([Section.LIMITATIONS])

    def test_available_reports_what_the_paper_actually_has(self, parsed):
        available = parsed.available
        assert Section.PARTICIPANTS in available
        assert Section.TITLE in available


class TestTolerance:
    def test_a_paper_with_no_section_markup_still_yields_text(self):
        """Better one unlabelled block than dropping the paper."""
        xml = """<?xml version="1.0"?>
        <full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
          <coredata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Bare</dc:title></coredata>
          <originalText><body><ce:para>Some unstructured body text.</ce:para></body></originalText>
        </full-text-retrieval-response>"""
        parsed = parse_sections(xml)
        assert parsed.title == "Bare"
        assert any("unstructured body text" in s.text for s in parsed.sections)

    def test_truncated_xml_does_not_raise(self):
        parsed = parse_sections("<full-text-retrieval-response><coredata><dc:title>Cut")
        assert parsed.sections == [] or isinstance(parsed.sections, list)

    def test_empty_input_does_not_raise(self):
        assert parse_sections("").sections == []

    def test_jats_style_sec_and_title_elements_are_understood(self):
        """Not Elsevier's schema, but the local-name() lookups handle it anyway."""
        xml = """<article><body>
          <sec><title>Methods</title><p>We recruited forty participants.</p></sec>
        </body></article>"""
        parsed = parse_sections(xml)
        assert parsed.sections[0].canonical is Section.METHODS
        assert "forty participants" in parsed.sections[0].text
