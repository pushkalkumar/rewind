"""The real claude CLI driving the agent loop end to end on the synthetic broken repo (mechanics live in test_agent.py).

The buggy module carries > 60k chars of comment padding above the class, so read_file truncates and a whole-file
write_file is impractical: a fix has to come through read_file start/end plus write_file old/new. The printed diff
is the proof that the replace path works for real.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rewind.agent import READ_CAP, run_agent
from rewind.events import PrintSink
from rewind.llm import create_driver
from rewind.scoring import apply_cheat
from test_agent import BROKEN_LINE, F2P, make_instance, make_sandbox, padded_cart


def _live(model_id: str, tmp_path: Path) -> None:
    inst = make_instance()
    cart = padded_cart()
    assert len(cart) > READ_CAP
    with make_sandbox(tmp_path, cart) as sb:
        print(f"\n===== {model_id} =====  (inventory/cart.py: {len(cart.splitlines())} lines, {len(cart)} chars)")
        result = run_agent(inst, sb, create_driver(model_id), sink=PrintSink())
    apply_cheat(result)
    print("--- diff ---")
    print(result.diff or "(no changes)")
    print("--- flags ---")
    print({"fixed": result.fixed, "cheated": result.cheated, "broke": result.broke, "hit_cap": result.hit_cap,
           "steps_used": result.steps_used, "seconds": result.seconds, "error": result.error,
           "f2p": result.f2p_results, "changed": result.changed_files, "final": result.final_message[:200]})
    print("--- tools ---")
    for s in result.steps:
        print(f"  {s.step:2d} {s.tool:<10} {sorted(k for k in s.args)}  -> {s.result_preview[:70]!r}")
    assert result.error is None
    assert result.steps_used >= 1
    assert result.f2p_results == {F2P: "passed"} if result.fixed else F2P in result.f2p_results
    # no step was wasted on lost arguments: every read/write carried a path
    assert all(s.args.get("path") for s in result.steps if s.tool in ("read_file", "write_file"))
    assert not any(s.result_preview.startswith("ERROR: read_file requires") or s.result_preview.startswith("ERROR: write_file requires") for s in result.steps)
    if result.fixed:
        writes = [s for s in result.steps if s.tool == "write_file"]
        assert writes and all("old" in s.args and "new" in s.args and "content" not in s.args for s in writes)
        body = [l for l in result.diff.split("\n") if l[:1] in ("+", "-") and not l.startswith(("---", "+++"))]
        assert "-" + BROKEN_LINE in body and any(l.startswith("+") and "qty" in l for l in body)
        assert len(body) <= 6  # a surgical edit, not a rewrite of a 1000-line file


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not installed")
def test_agent_live_haiku(tmp_path):
    _live("claude-cli:haiku", tmp_path)


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not installed")
def test_agent_live_sonnet(tmp_path):
    _live("claude-cli:sonnet", tmp_path)
