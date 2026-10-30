"""Tests for the trial tools (docs/tool-trials.md): a good reply passes, and every planted bad reply is caught."""
import json

import pytest

from minions import tools
from minions.config import Roster
from minions.errors import BadInput, Invented, NotInSource, SchemaError


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
