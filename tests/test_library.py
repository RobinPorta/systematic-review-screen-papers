"""The protocol library, in-memory report output, and the editor's shape helpers."""

from __future__ import annotations

import pytest

from screener import library, paths, report
from screener.protocol import Protocol
from screener.screen import ScreenRun
from screener.ui.editor import join_described, split_described

from .conftest import TINY


class TestNaming:
    def test_punctuation_and_accents_become_dashes(self):
        assert library.slugify("Sleep & Screens: Età evolutiva") == "sleep-screens-eta-evolutiva"

    def test_an_empty_name_still_gives_an_id(self):
        assert library.slugify("!!!") == "protocol"

    def test_a_taken_id_is_suffixed(self):
        assert library.unique_slug("Board games", {"board-games", "board-games-2"}) == "board-games-3"


class TestLibrary:
    def test_the_bundled_example_and_the_starter_both_validate(self):
        assert "boardgames" in library.bundled()
        assert library.starter().inclusions

    def test_yaml_round_trips(self):
        spec = Protocol.model_validate(TINY)
        assert library.parse_yaml(library.to_yaml(spec)) == spec

    def test_import_rejects_something_that_is_not_a_protocol(self):
        with pytest.raises(ValueError):
            library.parse_yaml("name: only a name\n")
        with pytest.raises(ValueError, match="mapping"):
            library.parse_yaml("- a list\n")

    def test_invalid_files_in_a_directory_are_skipped(self, tmp_path):
        Protocol.model_validate(TINY).save(tmp_path / "good.yaml")
        (tmp_path / "bad.yaml").write_text("name: broken\n")
        assert list(library.from_directory(tmp_path)) == ["good"]

    def test_run_directories_are_per_protocol(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "RUNS_DIR", tmp_path)
        assert paths.run_paths("a.yaml").records != paths.run_paths("b.yaml").records


class TestInMemoryReports:
    def test_a_csv_rendered_in_memory_matches_the_file(self, tmp_path):
        on_disk = tmp_path / "labels.csv"
        report.write_label_template(ScreenRun(), [], on_disk)
        in_memory = report.as_text(report.write_label_template, ScreenRun(), [])
        assert in_memory == on_disk.read_bytes().decode("utf-8")

    def test_gold_labels_ignore_blank_rows(self):
        rows = [
            {"record_id": "a", "gold_label": "Include"},
            {"record_id": "b", "gold_label": ""},
            {"record_id": "", "gold_label": "exclude"},
        ]
        assert report.gold_labels(rows) == {"a": "include"}


class TestDescribedValues:
    @pytest.mark.parametrize("value", [
        "plain text",
        {"what": "a thing", "examples": ["one", "two"]},
    ])
    def test_round_trip(self, value):
        assert join_described(*split_described(value)) == value

    def test_examples_collapse_to_a_plain_string_when_empty(self):
        assert join_described("text", ["", "  "]) == "text"

    def test_richer_structures_are_left_alone(self):
        assert split_described({"what": "x", "extra": 1}) is None


class TestRanking:
    def test_a_named_ranking_score_must_exist(self):
        with pytest.raises(ValueError, match="ranking_score"):
            Protocol.model_validate({**TINY, "ranking_score": "nope"})

    def test_first_score_is_used_when_none_is_named(self):
        spec = Protocol.model_validate({
            **TINY,
            "scores": [{"id": "relevance", "instructions": "?", "levels": ["low", "high"]}],
        })
        assert spec.ranking_spec().id == "relevance"
