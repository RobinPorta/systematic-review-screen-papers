"""Self-hosted endpoints: settings, and a full stage-1 run against a fake CLM server.

The fake server answers the way `clm-serve` does — the TypeSafe wire schema, plus CLM's
`billing_units` in `usage`, and an error for any model it does not have — so the whole
path from settings through the SDK to decisions is exercised without a GPU.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from screener import report
from screener.config import (
    DEFAULT_CONTEXT_LIMIT,
    JEV_STATE_BUDGET,
    InvalidEndpoint,
    MissingCredential,
    Settings,
)
from screener.endpoint import EndpointUnavailable, list_models, open_client
from screener.fulltext.sections import ParsedSection, ParsedSections
from screener.protocol import Section
from screener.questions import stage1_questions, stage2_bundles
from screener.records import Record
from screener.screen import questions_fingerprint, screen_records
from screener.screen_fulltext import build_parts


def settings(**overrides) -> Settings:
    base = dict(typesafe_api_key=None, typesafe_model=None)
    return Settings(**{**base, **overrides})


# ------------------------------------------------------------------ settings


class TestSettings:
    def test_typesafe_without_a_key_is_a_setup_error(self):
        with pytest.raises(MissingCredential):
            settings().client_options()

    def test_typesafe_is_the_default_and_keeps_the_jev_budget(self):
        s = settings(typesafe_api_key="ts_x")
        assert not s.custom_endpoint
        assert s.client_options() == {"api_key": "ts_x", "model": None}
        assert s.state_budget == JEV_STATE_BUDGET

    def test_a_custom_endpoint_needs_no_key(self):
        options = settings(endpoint_url="http://127.0.0.1:8700/").client_options()
        assert options["base_url"] == "http://127.0.0.1:8700"
        assert options["api_key"] == "none"
        assert options["timeout"] > 10

    @pytest.mark.parametrize("url", ["", "127.0.0.1:8700", "ftp://host"])
    def test_a_custom_endpoint_needs_an_http_url(self, url):
        with pytest.raises(InvalidEndpoint):
            settings(endpoint_url=url).client_options()

    def test_the_stage2_budget_leaves_headroom_under_the_context_limit(self):
        s = settings(endpoint_url="http://x")
        assert s.state_budget == int(DEFAULT_CONTEXT_LIMIT * 0.8)
        assert settings(endpoint_url="http://x", context_limit=8192).state_budget == 6553


class TestCacheSalt:
    def test_typesafe_hashes_are_unchanged(self, protocol):
        questions = stage1_questions(protocol)
        salt = settings(typesafe_api_key="k").cache_salt
        assert questions_fingerprint(questions, salt=salt) == questions_fingerprint(questions)

    def test_backends_never_share_cached_answers(self, protocol):
        questions = stage1_questions(protocol)
        jev = questions_fingerprint(questions)
        clm = questions_fingerprint(questions, salt=settings(endpoint_url="http://a").cache_salt)
        other = questions_fingerprint(questions, salt=settings(endpoint_url="http://b").cache_salt)
        assert len({jev, clm, other}) == 3


def test_a_small_budget_splits_stage2_into_parts_that_fit(protocol):
    budget = settings(endpoint_url="http://x").state_budget
    bundle = next(b for b in stage2_bundles(protocol) if b.key == "methods+participants")
    paper = ParsedSections(
        title="A title", abstract="An abstract. " * 20,
        sections=[
            ParsedSection(title="Methods", text="word " * 1500, canonical=Section.METHODS),
            ParsedSection(title="Participants", text="word " * 1500, canonical=Section.PARTICIPANTS),
        ],
    )
    parts = build_parts(bundle, paper, budget=budget)
    assert len(parts) > 1
    assert all(part.tokens <= budget for part in parts)


# ------------------------------------------------------------------ a fake clm-serve


class FakeCLM(BaseHTTPRequestHandler):
    models = ["clm-latest"]
    api_key: str | None = None
    requests: list[dict] = []

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        if self.api_key and self.headers.get("Authorization") != f"Bearer {self.api_key}":
            self._send(401, {"detail": "bad key"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorised():
            return
        if self.path == "/v1/models":
            self._send(200, {"models": [
                {"name": n, "description": "head", "release_date": "2026-09-01"} for n in self.models
            ]})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self._authorised():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeCLM.requests.append({"body": body, "auth": self.headers.get("Authorization")})
        if body.get("model") not in self.models:
            self._send(404, {"detail": f"unknown model {body.get('model')!r}"})
            return
        answers = {}
        for qid, question in body["questions"].items():
            if question["type"] == "noul":
                # Fire nothing: inclusions met, exclusions not — an include.
                answers[qid] = {"type": "noul", "noul": 0.05 if qid.startswith("exc") else 0.9}
            elif question["type"] == "choice":
                options = list(question["criteria"])
                answers[qid] = {"type": "choice", "choice": options[0], "confidence": 0.8,
                                "probabilities": {o: 1 / len(options) for o in options}}
            else:
                levels = question["criteria"]
                answers[qid] = {"type": "score", "score": len(levels) - 1, "confidence": 0.7,
                                "legend": {str(i): str(level) for i, level in enumerate(levels)},
                                "probabilities": {str(len(levels) - 1): 1.0}}
        self._send(200, {"model": body["model"], "answers": answers,
                         "usage": {"billing_units": len(answers), "input_tokens": 100,
                                   "output_tokens": 0}})


@pytest.fixture
def clm():
    FakeCLM.models, FakeCLM.api_key, FakeCLM.requests = ["clm-latest"], None, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


RECORDS = [
    Record(id="r1", title="Board games for older adults", abstract="A trial of weekly play."),
    Record(id="r2", title="Chess in schools", abstract="Pupils played chess for a year."),
]


class TestAgainstAFakeCLM:
    def test_list_models(self, clm):
        assert list_models(settings(endpoint_url=clm)) == ["clm-latest"]

    async def test_stage1_runs_end_to_end_with_the_listed_model(self, clm, protocol, tmp_path):
        s = settings(endpoint_url=clm)
        run = await screen_records(protocol, RECORDS, settings=s, use_cache=False)

        assert [r.error for r in run.results] == [None, None]
        assert {r.decision.label.value for r in run.results} == {"include"}
        assert {req["body"]["model"] for req in FakeCLM.requests} == {"clm-latest"}
        assert {req["auth"] for req in FakeCLM.requests} == {"Bearer none"}
        assert run.usage.input_tokens == 200
        line = report.format_usage(run.usage, title="Custom endpoint usage", priced=False)
        assert "no per-token price" in line and "$" not in line

    async def test_the_api_key_is_sent_when_the_server_requires_one(self, clm, protocol):
        FakeCLM.api_key = "secret"
        with pytest.raises(EndpointUnavailable):
            async with open_client(settings(endpoint_url=clm)):
                pass
        s = settings(endpoint_url=clm, endpoint_api_key="secret")
        run = await screen_records(protocol, RECORDS[:1], settings=s, use_cache=False)
        assert run.results[0].error is None
        assert FakeCLM.requests[-1]["auth"] == "Bearer secret"

    async def test_an_unknown_model_fails_the_record_not_the_run(self, clm, protocol):
        s = settings(endpoint_url=clm, endpoint_model="jev-latest")
        run = await screen_records(protocol, RECORDS[:1], settings=s, use_cache=False)
        assert run.results[0].decision.label.value == "maybe"
        assert "unknown model" in (run.results[0].error or "")

    async def test_an_unreachable_endpoint_is_a_setup_error(self):
        with pytest.raises(EndpointUnavailable):
            async with open_client(settings(endpoint_url="http://127.0.0.1:9")):
                pass

    async def test_cached_answers_are_kept_apart_per_backend(self, clm, protocol, tmp_path):
        s = settings(endpoint_url=clm)
        await screen_records(protocol, RECORDS[:1], settings=s, cache_dir=tmp_path)
        again = await screen_records(protocol, RECORDS[:1], settings=s, cache_dir=tmp_path)
        assert again.results[0].cached
        slash = settings(endpoint_url=clm + "/")
        same = await screen_records(protocol, RECORDS[:1], settings=slash, cache_dir=tmp_path)
        assert same.results[0].cached  # the same server, however the URL is written
        pinned = settings(endpoint_url=clm, endpoint_model="clm-latest")
        other = await screen_records(protocol, RECORDS[:1], settings=pinned, cache_dir=tmp_path)
        assert not other.results[0].cached
