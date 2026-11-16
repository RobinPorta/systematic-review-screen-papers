"""Full text from ERIC's PDFs: finding sections in plain text, and the source itself.

PDF text has no markup, so the parser's job is to find the headings it can trust and to
never lose the body when it finds none. The source is tested against a fake transport and
a stand-in for the PDF reader, so nothing here touches the network or needs a real PDF.
"""

from __future__ import annotations

import httpx
import pytest

from screener.fulltext import base, eric_pdf
from screener.fulltext.base import RetrievalStatus
from screener.fulltext.eric_pdf import EricPdfSource
from screener.fulltext.sections import CHUNK_CHARS, parse_plain_text
from screener.pipeline import parse_full_text
from screener.protocol import Section
from screener.questions import stage2_bundles
from screener.records import Record
from screener.screen_fulltext import build_parts

PAPER = """Journal of Play Research  12
Chess and Attention in Primary Schools
Jane Doe, University of Somewhere
Abstract
This front-page abstract repeats what the record already says.
1. Introduction
Chess has long been proposed as a way to train atten-
tion in children. Earlier studies disagree.
2. Method
2.1 Participants
Forty pupils aged eight to ten took part.
Procedure
Pupils played chess twice a week for a term.
13
Results
Attention scores rose in the chess group.
Discussion
The effect was modest but reliable.
References
Doe, J. (2019). An earlier paper. Journal of Play.
Methods
This line sits inside the reference list and must not be read.
"""


class TestPlainTextSections:
    @pytest.fixture
    def parsed(self):
        return parse_plain_text(PAPER, title="Record title", abstract="Record abstract.")

    def test_title_and_abstract_come_from_the_record_not_the_pdf(self, parsed):
        assert parsed.title == "Record title" and parsed.abstract == "Record abstract."

    def test_headings_are_found_numbered_or_not(self, parsed):
        found = {s.title: s.canonical for s in parsed.sections if s.canonical}
        assert found == {
            "Introduction": Section.INTRODUCTION,
            "Participants": Section.PARTICIPANTS,
            "Procedure": Section.INTERVENTION,
            "Results": Section.RESULTS,
            "Discussion": Section.DISCUSSION,
        }

    def test_text_is_filed_under_its_heading(self, parsed):
        text = parsed.text_for([Section.PARTICIPANTS, Section.RESULTS])
        assert text["participants"] == "Forty pupils aged eight to ten took part."
        assert text["results"] == "Attention scores rose in the chess group."

    def test_words_hyphenated_across_lines_are_rejoined_and_page_numbers_dropped(self, parsed):
        intro = parsed.text_for([Section.INTRODUCTION])["introduction"]
        assert "train attention in children" in intro
        assert "13" not in parsed.text_for([Section.INTERVENTION])["intervention"]

    def test_everything_from_the_reference_list_on_is_dropped(self, parsed):
        assert all("earlier paper" not in s.text and "must not be read" not in s.text
                   for s in parsed.sections)

    def test_front_page_and_abstract_text_are_kept_as_body_never_sent_for_labelling(self, parsed):
        body = [s for s in parsed.sections if s.unlabelled_body]
        assert any("front-page abstract" in s.text for s in body)
        assert parsed.unresolved == []

    def test_a_sentence_is_not_mistaken_for_a_heading(self):
        parsed = parse_plain_text("The results were clear.\nresults of the game\nGame 3 results\n")
        assert [s.canonical for s in parsed.sections] == [None]

    def test_no_headings_at_all_still_yields_the_whole_body(self):
        text = "\n".join(f"Sentence number {n} of a paper with no headings at all." for n in range(400))
        parsed = parse_plain_text(text, title="T", abstract="A")
        assert len(parsed.sections) > 1
        assert all(s.unlabelled_body and len(s.text) < CHUNK_CHARS + 200 for s in parsed.sections)
        assert sum(len(s.text) for s in parsed.sections) >= len(text) - 400

    def test_a_headingless_paper_reaches_stage_two_even_under_a_small_budget(self, protocol):
        """The fallback skips any section larger than the budget, so a PDF with no headings
        must arrive in pieces or a self-hosted endpoint's small budget would drop it all."""
        text = "\n".join(f"Sentence number {n} about forty adults who played." for n in range(600))
        parsed = parse_plain_text(text, title="T", abstract="A")
        bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
        parts = build_parts(bundle, parsed, budget=1600)
        assert parts and all(part.tokens <= 1600 for part in parts)
        assert "forty adults" in parts[0].state["body"]

    def test_empty_text_gives_no_sections(self):
        assert parse_plain_text("", title="T").sections == []


# ------------------------------------------------------------------ the source


WITH_PDF = Record(id="EJ1148470", title="Chess", abstract="An abstract.", eric_fulltext=True)
WITHOUT_PDF = Record(id="EJ1230215", title="Chess", eric_fulltext=False)


def source(tmp_path, handler=None, calls=None):
    def default(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-fake")

    http = httpx.Client(transport=httpx.MockTransport(handler or default))
    return EricPdfSource(client=http, cache_dir=tmp_path)


@pytest.fixture
def readable(monkeypatch):
    monkeypatch.setattr(eric_pdf, "extract_text", lambda pdf: PAPER * 3)


class TestSource:
    def test_availability_is_read_off_the_record_without_a_request(self, tmp_path):
        calls: list[str] = []
        eric = source(tmp_path, calls=calls)
        assert eric.check(WITH_PDF) is RetrievalStatus.OK
        assert eric.check(WITHOUT_PDF) is RetrievalStatus.NOT_FOUND
        assert eric.check(Record(id="10.1000/x", eric_fulltext=True)) is RetrievalStatus.NO_IDENTIFIER
        assert calls == []

    def test_a_record_without_a_pdf_is_not_requested(self, tmp_path):
        calls: list[str] = []
        retrieval = source(tmp_path, calls=calls).fetch(WITHOUT_PDF)
        assert retrieval.status is RetrievalStatus.NOT_FOUND and calls == []

    def test_the_pdf_is_fetched_by_eric_number_and_returned_as_text(self, tmp_path, readable):
        calls: list[str] = []
        retrieval = source(tmp_path, calls=calls).fetch(WITH_PDF)
        assert calls == ["https://files.eric.ed.gov/fulltext/EJ1148470.pdf"]
        assert retrieval.ok and retrieval.source == "eric"
        assert retrieval.full_text.content_type == "text"
        parsed = parse_full_text(retrieval.full_text, WITH_PDF)
        assert parsed.abstract == "An abstract." and Section.RESULTS in parsed.available

    def test_only_the_text_is_cached_and_it_is_reused(self, tmp_path, readable):
        calls: list[str] = []
        eric = source(tmp_path, calls=calls)
        eric.fetch(WITH_PDF)
        assert [p.name for p in tmp_path.iterdir()] == ["eric-EJ1148470.txt"]
        assert eric.fetch(WITH_PDF).ok and len(calls) == 1
        eric.fetch(WITH_PDF, use_cache=False)
        assert len(calls) == 2

    def test_a_scan_with_no_text_layer_is_not_retrieved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eric_pdf, "extract_text", lambda pdf: "ED 091 898\n")
        retrieval = source(tmp_path).fetch(WITH_PDF)
        assert retrieval.status is RetrievalStatus.NOT_FOUND
        assert "no extractable text" in retrieval.detail
        assert list(tmp_path.iterdir()) == []

    def test_a_missing_pdf_and_a_server_error_are_told_apart(self, tmp_path):
        missing = source(tmp_path, lambda request: httpx.Response(404)).fetch(WITH_PDF)
        broken = source(tmp_path, lambda request: httpx.Response(503)).fetch(WITH_PDF)
        assert missing.status is RetrievalStatus.NOT_FOUND
        assert broken.status is RetrievalStatus.ERROR and broken.detail == "HTTP 503"

    def test_an_oversized_pdf_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eric_pdf, "MAX_PDF_BYTES", 10)
        big = source(tmp_path, lambda request: httpx.Response(200, content=b"x" * 50)).fetch(WITH_PDF)
        assert big.status is RetrievalStatus.NOT_FOUND and "larger than" in big.detail

    def test_bytes_that_are_not_a_pdf_give_no_text_rather_than_an_error(self):
        assert eric_pdf.extract_text(b"this is not a pdf") == ""


class _Stub:
    def __init__(self, name: str, status: RetrievalStatus) -> None:
        self.name, self.status, self.fetched = name, status, 0

    def check(self, record: Record) -> RetrievalStatus:
        return self.status

    def fetch(self, record: Record, *, use_cache: bool = True) -> base.Retrieval:
        self.fetched += 1
        text = base.FullText(record_id=record.id, source=self.name, content="x")
        return base.Retrieval(
            record_id=record.id, status=self.status, source=self.name,
            full_text=text if self.status is RetrievalStatus.OK else None,
        )


class TestSourceChain:
    def test_the_first_source_that_has_the_paper_wins(self):
        eric, epmc = _Stub("eric", RetrievalStatus.OK), _Stub("europepmc", RetrievalStatus.OK)
        assert base.resolve(WITH_PDF, [eric, epmc]).source == "eric"
        assert epmc.fetched == 0

    def test_the_second_source_is_tried_when_the_first_declines(self):
        eric, epmc = _Stub("eric", RetrievalStatus.NOT_FOUND), _Stub("europepmc", RetrievalStatus.OK)
        assert base.resolve(WITHOUT_PDF, [eric, epmc]).source == "europepmc"
        assert base.check(WITHOUT_PDF, [eric, epmc]) is RetrievalStatus.OK

    def test_when_both_decline_the_more_informative_refusal_is_reported(self):
        eric = _Stub("eric", RetrievalStatus.NOT_FOUND)
        epmc = _Stub("europepmc", RetrievalStatus.NO_IDENTIFIER)
        assert base.check(WITHOUT_PDF, [eric, epmc]) is RetrievalStatus.NOT_FOUND
        assert base.resolve(WITHOUT_PDF, [eric, epmc]).status is RetrievalStatus.NOT_FOUND
