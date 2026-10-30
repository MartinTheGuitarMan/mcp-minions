"""Tests for the trial tools (docs/tool-trials.md): a good reply passes, and every planted bad reply is caught."""
import json

import pytest

from minions import tools
from minions.config import Roster
from minions.errors import BadInput, EmptyValue, Invented, NotInSource, SchemaError, TooLong


def reply(fake, **obj):
    fake.replies.append(json.dumps(obj))


# ---------------------------------------------------------------- check_claims
SRC = "The festival drew about 8,400 people in Velmar. Entry was free."


def claim(c, s, q=""):
    return {"claim": c, "supported": s, "quote": q}


def test_check_claims_good_reply(fake, cfg):
    reply(fake, claims=[claim("About 8,400 people came to the festival.", True, "drew about 8,400 people"),
                        claim("Entry cost 15 euros.", False, "")])
    out = tools.check_claims(cfg, "About 8,400 people came to the festival. Entry cost 15 euros.", SRC)
    assert [c["supported"] for c in out["claims"]] == [True, False]


def test_check_claims_catches_a_made_up_quote(fake, cfg):
    reply(fake, claims=[claim("About 8,400 people came.", True, "eight thousand four hundred came")])
    with pytest.raises(NotInSource):
        tools.check_claims(cfg, "About 8,400 people came.", SRC)


def test_check_claims_catches_supported_claim_with_unknown_name_or_number(fake, cfg):
    reply(fake, claims=[claim("Orla Vennesund opened the festival.", True, "The festival drew about")])
    with pytest.raises(Invented):
        tools.check_claims(cfg, "Orla Vennesund opened the festival.", SRC)
    reply(fake, claims=[claim("The festival drew 9,900 people.", True, "drew about 8,400 people")])
    with pytest.raises(Invented):
        tools.check_claims(cfg, "The festival drew 9,900 people.", SRC)


def test_check_claims_catches_unmarked_and_dropped_claims(fake, cfg):
    fake.replies.append(json.dumps({"claims": [{"claim": "Entry was free.", "quote": "Entry was free."}]}))   # no supported flag
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, "Entry was free.", SRC)
    fake.replies.append(json.dumps({"claims": [{"claim": "Entry was free.", "supported": None, "quote": ""}]}))
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, "Entry was free.", SRC)
    reply(fake, claims=[claim("Entry was free.", True, "Entry was free.")])   # the 8,400 of the text was dropped
    with pytest.raises(SchemaError, match="leave out"):
        tools.check_claims(cfg, "Entry was free. About 8,400 came.", SRC)
    reply(fake, claims=[])
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, "Entry was free.", SRC)


def test_check_claims_drops_a_quote_on_an_unsupported_claim(fake, cfg):
    reply(fake, claims=[claim("Entry cost 15 euros.", False, "Entry was free.")])
    assert tools.check_claims(cfg, "Entry cost 15 euros.", SRC)["claims"][0]["quote"] == ""


def test_check_claims_input_and_route(fake, cfg):
    with pytest.raises(BadInput):
        tools.check_claims(cfg, "", SRC)
    with pytest.raises(BadInput):
        tools.check_claims(cfg, "x" * 7000, SRC)


def test_check_claims_borrows_the_extract_route():
    raw = {"models": {"a": {"backend": "ollama", "model": "m1"}, "b": {"backend": "ollama", "model": "m2"}},
           "routes": {"extract": {"fast": ["b"]}}}
    r = Roster.from_dict(raw)
    assert r.route("check_claims") is r.route("extract") and r.route("check_claims").fast[0].name == "b"
    raw["routes"]["check_claims"] = {"fast": ["a"]}
    assert Roster.from_dict(raw).route("check_claims").fast[0].name == "a"


# ---------------------------------------------------------------- commit_message
DIFF = ("diff --git a/src/pager.py b/src/pager.py\nindex 1..2 100644\n--- a/src/pager.py\n+++ b/src/pager.py\n"
        "@@ -1,2 +1,2 @@\n-    return items[a:b + 1]\n+    return items[a:b]\n")


def test_commit_message_good_reply(fake, cfg):
    reply(fake, subject="Fix off-by-one in page slicing", body="The end index was one too high in src/pager.py.")
    out = tools.commit_message(cfg, DIFF)
    assert out["subject"] == "Fix off-by-one in page slicing" and "pager.py" in out["body"]


@pytest.mark.parametrize("subject,exc", [
    ("", EmptyValue), ("Fix " + "x" * 80, TooLong), ("Fixed the slicing", SchemaError), ("Fixes the slicing", SchemaError),
    ("Fixing the slicing", SchemaError), ("Fix the slicing.", SchemaError), ("Fix slicing\nand more", SchemaError)])
def test_commit_message_catches_bad_subjects(fake, cfg, subject, exc):
    reply(fake, subject=subject, body="")
    with pytest.raises(exc):
        tools.commit_message(cfg, DIFF)


def test_commit_message_catches_a_file_that_is_not_in_the_diff(fake, cfg):
    reply(fake, subject="Fix off-by-one in src/paging.py", body="")
    with pytest.raises(Invented):
        tools.commit_message(cfg, DIFF)
    reply(fake, subject="Fix off-by-one", body="Also touches utils/dates.py.")
    with pytest.raises(Invented):
        tools.commit_message(cfg, DIFF)


def test_commit_message_accepts_a_file_by_name_or_path(fake, cfg):
    for text in ("pager.py", "src/pager.py"):
        reply(fake, subject="Fix off-by-one", body=f"See {text}.")
        tools.commit_message(cfg, DIFF)


def test_commit_message_input_checks(cfg):
    with pytest.raises(BadInput):
        tools.commit_message(cfg, "just some text")
    with pytest.raises(BadInput):
        tools.commit_message(cfg, DIFF + "+" + "x" * 13000)


def test_commit_message_borrows_the_summarize_route():
    raw = {"models": {"a": {"backend": "ollama", "model": "m1"}, "b": {"backend": "ollama", "model": "m2"}},
           "routes": {"summarize": {"fast": ["b"]}}}
    assert Roster.from_dict(raw).route("commit_message").fast[0].name == "b"


# ---------------------------------------------------------------- dedupe
ITEMS = [{"id": "a", "text": "Meeting at 3 pm."}, {"id": "b", "text": "Lunch at noon."}, {"id": "c", "text": "The meeting is at 3 pm."}]


def test_dedupe_good_reply_is_put_in_input_order(fake, cfg):
    reply(fake, groups=[["b"], ["c", "a"]])
    assert tools.dedupe(cfg, ITEMS) == {"groups": [["a", "c"], ["b"]]}
    sent = fake.requests[0][1]["response_format"]["json_schema"]["schema"]
    assert sent["properties"]["groups"]["items"]["items"]["enum"] == ["a", "b", "c"]


@pytest.mark.parametrize("groups", [[["a", "c"]], [["a", "c"], ["b", "a"]], [["a", "c"], ["b"], ["z"]],
                                    [["a", "c"], ["b"], []], [["a", "c"], ["b", "b"]]])
def test_dedupe_catches_bad_partitions(fake, cfg, groups):
    reply(fake, groups=groups)
    with pytest.raises(SchemaError):
        tools.dedupe(cfg, ITEMS)


def test_dedupe_input_checks(cfg):
    for items in ([], [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}], [{"id": "a"}]):
        with pytest.raises(BadInput):
            tools.dedupe(cfg, items)
    with pytest.raises(BadInput):
        tools.dedupe(cfg, [{"id": str(n), "text": "x"} for n in range(51)])
