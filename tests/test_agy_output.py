"""agy's output: its stream read as a review, or as why there is none."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agy_case import _AgyCase

from scripts.review_loop.artifacts import describe_outputs
from scripts.review_loop.results import _unwrap_envelope, extract_results


class TestAgyOutput(_AgyCase):
    """Reading agy's stream: only a finished turn is a review."""

    def test_structured_output_is_the_result(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream(
                {
                    "status": "SUCCESS",
                    "response": "done",
                    "structured_output": self._review(),
                }
            )
        )
        result = extract_results(tmp_path, self.AGY)[2]
        assert result is not None
        assert result["findings"][0]["issue"] == "dead code"
        assert (tmp_path / "result-3.json").exists()

    def test_the_response_text_is_the_fallback(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "SUCCESS", "response": json.dumps(self._review())})
        )
        result = extract_results(tmp_path, self.AGY)[2]
        assert result is not None and result["model"] == self.MODEL

    def test_a_line_separator_inside_a_string_does_not_lose_the_review(
        self, tmp_path: Path
    ) -> None:
        # Valid JSON may carry U+0085 raw; str.splitlines() splits on it, NDJSON does not.
        review = {**self._review(), "overall_assessment": "one\u0085two"}
        event = {
            "event": "result",
            "result": {"status": "SUCCESS", "structured_output": review},
        }
        init = json.dumps({"event": "init"})
        self._raw(tmp_path).write_text(
            init + "\n" + json.dumps(event, ensure_ascii=False), encoding="utf-8"
        )
        result = extract_results(tmp_path, self.AGY)[2]
        assert result is not None and result["overall_assessment"] == "one\u0085two"

    @pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED", "WAITING"])
    def test_an_unfinished_turn_is_not_a_review(
        self, tmp_path: Path, status: str
    ) -> None:
        # However review-shaped its output, a turn that did not finish is a hole in
        # the round, not a clean bill -- or it would count towards convergence.
        self._raw(tmp_path).write_text(
            self._stream({"status": status, "structured_output": self._review()})
        )
        assert extract_results(tmp_path, self.AGY)[2] is None

    def test_a_failed_turn_says_why(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "ERROR", "error": "quota exhausted for this model"})
        )
        assert "quota exhausted" in describe_outputs(tmp_path, self.AGY)

    def test_a_refused_tool_call_is_named_not_silent(self, tmp_path: Path) -> None:
        # The first real sweep: `ls -la`, denied, turn over, empty response, and the
        # round said only "NO OUTPUT".
        self._raw(tmp_path).write_text(
            self._stream(
                {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [
                        {"action": "command", "display_name": "RunCommand"}
                    ],
                }
            )
        )
        said = describe_outputs(tmp_path, self.AGY)
        assert "refused tool call" in said and "RunCommand" in said

    def test_a_malformed_denied_list_still_reports(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "ERROR", "error": "bad", "denied_actions": True})
        )
        assert "bad" in describe_outputs(tmp_path, self.AGY)

    def test_an_agy_shaped_line_from_another_backend_is_not_agys(
        self, tmp_path: Path
    ) -> None:
        # Any reviewer can be talked into printing it; a failed status would otherwise
        # turn that reviewer's real answer into a missing one.
        ok = {"status": "SUCCESS", "structured_output": self._review()}
        injected = self._stream(ok)
        (tmp_path / "raw-3.txt").write_text(injected)
        assert extract_results(tmp_path, {3: "gemini"})[2] is None
        # The same stream from agy is agy's answer.
        assert extract_results(tmp_path, self.AGY)[2] is not None

    def test_a_reviewer_cannot_mark_its_own_slot_as_agys(self, tmp_path: Path) -> None:
        # backend-N.txt is in the round directory, which hermes (--yolo, a file tool)
        # can write: read back, a slot it marked agy's was parsed as agy's verdict.
        ok = {"status": "SUCCESS", "structured_output": self._review()}
        (tmp_path / "raw-3.txt").write_text(self._stream(ok))
        (tmp_path / "backend-3.txt").write_text("agy")
        assert extract_results(tmp_path, {3: "hermes"})[2] is None

    def test_a_planted_result_file_is_not_a_slots_answer(self, tmp_path: Path) -> None:
        # A result-N.json already there was taken for any slot, before the slot's own
        # output was looked at: a reviewer with a file tool could answer for another.
        planted = json.dumps(self._review())
        for slot in (1, 2, 3):
            (tmp_path / f"result-{slot}.json").write_text(planted)
        (tmp_path / "raw-1.txt").write_text("I could not finish.")
        results = extract_results(tmp_path, {1: "claude", 2: "codex"})
        assert results[0] is None  # claude's own output had no review
        assert results[1] is None  # codex wrote no answer of its own
        assert results[2] is None  # slot 3 was never launched
        # Not kept to be listed as findings either.
        assert not list(tmp_path.glob("result-*.json"))
        # codex's answer is its own file (-o), checked like any other output.
        (tmp_path / "codex-out-2.txt").write_text("Done:\n" + planted)
        assert extract_results(tmp_path, {2: "codex"})[1] is not None
        assert (tmp_path / "result-2.json").is_file()

    def test_a_stream_with_no_result_event_is_no_review(self) -> None:
        # Without a result event, the stream was read as plain text, and a bare object
        # in it passed as agy's verdict.
        assert _unwrap_envelope(json.dumps(self._review()), agy_ran=True) == ""

    def test_claude_envelopes_are_unaffected(self) -> None:
        claude = json.dumps({"type": "result", "result": '{"a": 1}'})
        assert _unwrap_envelope(claude) == '{"a": 1}'
