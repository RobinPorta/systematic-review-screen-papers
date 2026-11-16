"""ERIC: parsing its CSV into records, the code-side filters, and the paging client.

The fixture is a real page of ERIC's CSV, chosen for its awkward rows: authors whose names
contain escaped commas, a record in two languages, one with no language, one with no link.
The client is exercised against a fake transport, so nothing here touches the network.
"""

from __future__ import annotations

import csv
import io
import json

import httpx
import pytest

from screener.eric import FIELDS, EricClient, EricError
from screener.protocol import Filters
from screener.records import Record, apply_filters, dedupe, eric_url, from_eric_row, split_multi

from .conftest import FIXTURES


@pytest.fixture(scope="module")
def rows() -> dict[str, dict[str, str]]:
    text = (FIXTURES / "eric_page.csv").read_text(encoding="utf-8")
    return {row["id"]: row for row in csv.DictReader(io.StringIO(text, newline=""))}


@pytest.fixture(scope="module")
def records(rows) -> dict[str, Record]:
    return {eric_id: from_eric_row(row) for eric_id, row in rows.items()}


class TestMultiValuedCells:
    def test_values_are_split_on_commas(self):
        assert split_multi("Games,Scores,Clubs") == ["Games", "Scores", "Clubs"]

    def test_an_escaped_comma_belongs_to_the_value(self):
        assert split_multi(r"Poston\, David I.,Vandenkieboom\, Kathryn K.") == [
            "Poston, David I.", "Vandenkieboom, Kathryn K.",
        ]

    def test_a_value_may_hold_several_escaped_commas(self):
        assert split_multi(r"Tweedie\, Karen\, Ed.") == ["Tweedie, Karen, Ed."]

    def test_empty_gives_an_empty_list(self):
        assert split_multi("") == [] and split_multi(None) == [] and split_multi("  ") == []


class TestNormalisation:
    def test_core_fields_are_unpacked(self, records):
        record = records["EJ1230215"]
        assert record.title and record.has_abstract
        assert record.year == 2019
        assert record.journal == "SAGE Open"
        assert record.authors == "Poston, David I.; Vandenkieboom, Kathryn K."
        assert record.publication_types == ["Journal Articles", "Reports - Research"]
        assert record.languages == ["English"]
        assert record.peer_reviewed is True
        assert record.citation == "v9 n3 Jul 2019"

    def test_the_doi_is_read_out_of_the_publisher_link(self, records):
        assert records["EJ1230215"].doi == "10.1177/2158244019870787"

    def test_a_link_that_is_not_a_doi_gives_no_doi(self, records):
        record = records["EJ1119039"]
        assert record.doi is None and record.url == "http://www.bookstoread.com/etp"

    def test_a_record_with_no_link_points_at_its_eric_page(self, records):
        assert records["EJ486450"].url == eric_url("EJ486450") == "https://eric.ed.gov/?id=EJ486450"

    def test_the_full_text_flag_is_carried_through_for_stage_two(self, records):
        assert records["EJ1148470"].eric_fulltext is True
        assert records["EJ1230215"].eric_fulltext is False

    def test_several_languages_and_none_at_all(self, records):
        assert records["EJ352579"].languages == ["French", "English"]
        assert records["ED091898"].languages == []

    def test_descriptors_become_keywords_capped(self, records):
        keywords = records["EJ1230215"].keywords
        assert "Games" in keywords and len(keywords) <= 12

    def test_html_entities_are_decoded(self):
        record = from_eric_row({"id": "EJ1", "title": "Dungeons &amp; Dragons &amp; Dewey"})
        assert record.title == "Dungeons & Dragons & Dewey"

    def test_an_almost_empty_row_does_not_raise(self):
        record = from_eric_row({"id": "ED1"})
        assert record.id == "ED1" and record.year is None and record.peer_reviewed is None
        assert from_eric_row({}).id == "unknown"


class TestState:
    def test_state_carries_what_the_judgement_needs(self, records):
        state = records["EJ1230215"].state()
        assert set(state) >= {"title", "abstract", "keywords", "journal", "publication_type"}
        assert state["education_level"] == ["Elementary Education"]

    def test_state_omits_the_year_identifiers_and_authors(self, records):
        """Year is enforced by the query and by code. Putting it in the state would only
        invite Jev to reason about dates, which it is documented as bad at."""
        blob = json.dumps(records["EJ1230215"].state())
        assert "2019" not in blob.replace("v9 n3 Jul 2019", "")
        assert "Poston" not in blob and "EJ1230215" not in blob and "doi" not in blob


class TestFilters:
    def test_year_filter_drops_an_out_of_range_record(self, records):
        outcome = apply_filters(records.values(), Filters(year_from=2000))
        assert {r.id for r in outcome.kept} == {"EJ1230215", "EJ1148470", "EJ1119039"}
        assert outcome.drop_reasons == {"published before 2000": 3}

    def test_one_matching_publication_type_is_enough(self, records):
        outcome = apply_filters(records.values(), Filters(types=["Reports - Research"]))
        assert {r.id for r in outcome.kept} == {"EJ1230215", "EJ1148470"}

    def test_language_is_rechecked_by_name_from_a_code(self):
        french = Record(id="a", languages=["French"])
        both = Record(id="b", languages=["French", "English"])
        outcome = apply_filters([french, both], Filters(languages=["en"]))
        assert [r.id for r in outcome.kept] == ["b"]
        assert outcome.drop_reasons == {"language French": 1}

    def test_a_record_with_no_language_is_kept(self, records):
        outcome = apply_filters([records["ED091898"]], Filters(languages=["it"]))
        assert len(outcome.kept) == 1

    def test_peer_review_can_be_required(self, records):
        outcome = apply_filters(records.values(), Filters(peer_reviewed_only=True))
        assert "EJ1119039" not in {r.id for r in outcome.kept}
        assert outcome.drop_reasons == {"not peer reviewed": 2}

    def test_records_without_abstracts_are_kept_unless_the_protocol_says_otherwise(self):
        bare = Record(id="x", title="A title only")
        assert apply_filters([bare], Filters()).kept == [bare]
        assert apply_filters([bare], Filters(require_abstract=True)).drop_reasons == {"no abstract": 1}

    def test_duplicates_by_eric_number_are_dropped(self, records):
        record = records["EJ486450"]
        assert dedupe([record, record.model_copy()]) == [record]


# ------------------------------------------------------------------ the client


def fake_eric(total: int, *, error: str | None = None):
    """A transport answering like ERIC: a JSON count, then CSV pages of `id`-only rows."""
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if params["format"] == "json":
            if error:
                # ERIC reports a query it cannot parse with HTTP 200 and an error body.
                return httpx.Response(200, json={"error": {"msg": error, "code": 400}})
            return httpx.Response(200, json={"response": {"numFound": total, "docs": []}})
        start, count = int(params["start"]), int(params["rows"])
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(FIELDS), lineterminator="\n")
        writer.writeheader()
        for n in range(start, min(start + count, total)):
            writer.writerow({"id": f"EJ{n:04d}", "title": f"Title {n}", "author": r"Doe\, Jane"})
        return httpx.Response(200, text=out.getvalue())

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


class TestClient:
    def test_count_reads_the_total(self, tmp_path):
        http, _ = fake_eric(1234)
        assert EricClient(client=http, cache_dir=tmp_path).count("chess") == 1234

    def test_a_query_eric_cannot_parse_is_an_error_not_an_empty_result(self, tmp_path):
        http, _ = fake_eric(0, error="org.apache.solr.search.SyntaxError: Cannot parse 'title:((': EOF")
        client = EricClient(client=http, cache_dir=tmp_path)
        with pytest.raises(EricError, match="Cannot parse"):
            client.count("title:((")
        with pytest.raises(EricError):
            list(client.search("title:(("))

    def test_records_are_requested_as_csv_with_the_fields_screening_needs(self, tmp_path):
        http, calls = fake_eric(3)
        list(EricClient(client=http, cache_dir=tmp_path).search("chess"))
        page = calls[-1]
        assert page["format"] == "csv"
        requested = page["fields"].split(",")
        assert requested[:6] == ["id", "title", "author", "publicationdateyear", "language", "url"]
        assert {"description", "subject", "publicationtype", "e_fulltextauth"} <= set(requested)

    def test_it_pages_until_the_total(self, tmp_path):
        http, calls = fake_eric(25)
        progress: list[tuple[int, int]] = []
        found = list(EricClient(client=http, cache_dir=tmp_path).search(
            "chess", page_size=10, on_page=lambda done, total: progress.append((done, total))
        ))
        assert [r.id for r in found] == [f"EJ{n:04d}" for n in range(25)]
        assert [c["start"] for c in calls if c["format"] == "csv"] == ["0", "10", "20"]
        assert progress == [(10, 25), (20, 25), (25, 25)]
        assert found[0].authors == "Doe, Jane"

    def test_the_limit_stops_early(self, tmp_path):
        http, calls = fake_eric(1000)
        found = list(EricClient(client=http, cache_dir=tmp_path).search("chess", limit=15, page_size=10))
        assert len(found) == 15
        assert len([c for c in calls if c["format"] == "csv"]) == 2

    def test_no_results_makes_no_page_request(self, tmp_path):
        http, calls = fake_eric(0)
        assert list(EricClient(client=http, cache_dir=tmp_path).search("zzz")) == []
        assert [c["format"] for c in calls] == ["json"]

    def test_pages_are_cached_on_disk(self, tmp_path):
        http, calls = fake_eric(5)
        client = EricClient(client=http, cache_dir=tmp_path)
        first = list(client.search("chess"))
        second = list(client.search("chess"))
        assert first == second
        assert len([c for c in calls if c["format"] == "csv"]) == 1
        list(client.search("chess", use_cache=False))
        assert len([c for c in calls if c["format"] == "csv"]) == 2

    def test_a_server_error_is_reported(self, tmp_path):
        http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403, text="no")))
        with pytest.raises(EricError, match="403"):
            EricClient(client=http, cache_dir=tmp_path).count("chess")
