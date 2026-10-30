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
    c = tools.check_claims(cfg, "About 8,400 people came.", SRC)["claims"][0]
    assert (c["supported"], c["quote"], c["flag"]) == (False, "", "quote_not_found")


def test_check_claims_a_planted_quote_loses_only_its_own_claim(fake, cfg):
    reply(fake, claims=[claim("About 8,400 people came.", True, "drew about 8,400 people"),
                        claim("Entry was free.", True, "Entry was free of charge and open to all")])
    a, b = tools.check_claims(cfg, "About 8,400 people came. Entry was free.", SRC)["claims"]
    assert a["supported"] and "flag" not in a
    assert not b["supported"] and b["flag"] == "quote_not_found"


@pytest.mark.parametrize("quote", ["With GPS: 1,712", "with gps 1712", "WITH GPS: 1712.", "Total files:\n10,174"])
def test_check_claims_quote_ignores_number_format_case_and_punctuation(fake, cfg, quote):
    src = "Total files: 10174\nWith GPS: 1712"
    reply(fake, claims=[claim("1,712 files have GPS.", True, quote)])
    assert tools.check_claims(cfg, "1,712 files have GPS.", src)["claims"][0]["supported"] is True


def test_check_claims_quote_stays_exact_about_words_and_numbers(fake, cfg):
    src = "Total files: 10174\nWith GPS: 1712"
    for bad in ("With GPS: 1,713", "With GPS: 17", "files 10", "Without GPS: 1712", "Missing GPS"):
        reply(fake, claims=[claim("1,712 files have GPS.", True, bad)])
        assert tools.check_claims(cfg, "1,712 files have GPS.", src)["claims"][0]["flag"] == "quote_not_found", bad


def test_check_claims_schema_failure_still_raises(fake, cfg):
    fake.replies.append(json.dumps({"claims": [{"claim": "Entry was free.", "quote": "Entry was free."}]}))
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, "Entry was free.", SRC)


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


# ---------------------------------------------------------------- score
def test_score_good_reply(fake, cfg):
    reply(fake, score=4, quote="Please send")
    assert tools.score(cfg, "Please send the report.", "politeness") == {"score": 4, "quote": "Please send"}


@pytest.mark.parametrize("bad,exc", [({"score": 0, "quote": "Please"}, SchemaError), ({"score": 6, "quote": "Please"}, SchemaError),
                                     ({"score": 3.5, "quote": "Please"}, SchemaError), ({"score": "4", "quote": "Please"}, SchemaError),
                                     ({"score": True, "quote": "Please"}, SchemaError),
                                     ({"score": 4, "quote": "Kindly forward"}, NotInSource), ({"score": 4, "quote": ""}, NotInSource),
                                     ({"score": 4}, SchemaError)])
def test_score_catches_bad_replies(fake, cfg, bad, exc):
    reply(fake, **bad)
    with pytest.raises(exc):
        tools.score(cfg, "Please send the report.", "politeness")


def test_score_input_checks(cfg):
    for kw in ({"text": ""}, {"criterion": ""}, {"criterion": "x" * 301}):
        with pytest.raises(BadInput):
            tools.score(cfg, **{"text": "x", "criterion": "y", **kw})


# ---------------------------------------------------------------- translate
def test_translate_good_replies(fake, cfg):
    reply(fake, translation="Tomas Reyes opened the library in Velmar at 9 on 4 May 2027.")
    out = tools.translate(cfg, "Tomas Reyes öppnade biblioteket i Velmar klockan 9 den 4 maj 2027.", "en")
    assert "Velmar" in out["translation"]
    reply(fake, translation="Vi sålde 3,5 ton potatis till Nordlys.")
    tools.translate(cfg, "We sold 3.5 tons of potatoes to Nordlys on Friday.", "sv")   # 3.5 / 3,5 and Friday are fine


@pytest.mark.parametrize("src,bad,exc", [
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas Reyes opened it at 10 in Velmar.", (NotInSource, Invented)),
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas Reyes opened it in Velmar.", NotInSource),
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas Reyes opened it at 9 in Velmar with 40 guests.", Invented),
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas Reyes opened it at 9 in Oskarsvik.", (NotInSource, Invented)),
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas Reyes and Anna Berg opened it at 9 in Velmar.", Invented),
    ("Tomas Reyes opened it at 9 in Velmar.", "Tomas opened it at 9 in Velmar.", (NotInSource, Invented)),
    ("Thanks.", "Thanks a lot for everything you have done for us, this really means a great deal to all of us.", TooLong),
    ("Tomas Reyes opened it at 9 in Velmar.", "  ", EmptyValue)])
def test_translate_catches_changed_added_or_dropped_facts(fake, cfg, src, bad, exc):
    reply(fake, translation=bad)
    with pytest.raises(exc):
        tools.translate(cfg, src, "en")


def test_translate_input_checks(cfg):
    with pytest.raises(BadInput):
        tools.translate(cfg, "hej", "de")
    with pytest.raises(BadInput):
        tools.translate(cfg, "", "en")
    with pytest.raises(BadInput):
        tools.translate(cfg, "x" * 6001, "en")


# ---------------------------------------------------------------- fixes found by trial run 1 (docs/tool-trials.md)
def test_quote_may_carry_a_closing_full_stop_or_quotation_marks(fake, cfg):
    reply(fake, claims=[claim("Entry was free.", True, "“Entry was free.”")])
    tools.check_claims(cfg, "Entry was free.", SRC)
    reply(fake, score=4, quote="Please send.")
    tools.score(cfg, "Please send the report", "politeness")
    reply(fake, score=4, quote="Please send the invoice.")   # still verbatim-or-nothing
    with pytest.raises(NotInSource):
        tools.score(cfg, "Please send the report", "politeness")


def test_commit_message_may_mention_a_directory_but_not_an_unknown_file(fake, cfg):
    reply(fake, subject="Back up the site", body="Archives /var/backups/site.tgz nightly.")
    tools.commit_message(cfg, DIFF)
    reply(fake, subject="Fix off-by-one", body="Writes to /var/backups nightly.")
    tools.commit_message(cfg, DIFF)
    reply(fake, subject="Fix off-by-one", body="Also changes setup.cfg.")
    with pytest.raises(Invented):
        tools.commit_message(cfg, DIFF)


def test_translate_accepts_plural_weekdays(fake, cfg):
    reply(fake, translation="The train does not run on Sundays.")
    tools.translate(cfg, "Tåget går inte på söndagar.", "en")


# ---------------------------------------------------------------- concurrency: two or more calls at once on one route
def test_every_trial_tool_is_safe_under_concurrent_calls(fake, cfg):
    """Each call must get its own answer back and be checked against its own input (no shared state between calls)."""
    from concurrent.futures import ThreadPoolExecutor

    def answer(body):
        user = body["messages"][-1]["content"]
        n = int(__import__("re").search(r"N=(\d+)", user).group(1))
        if "git commit message" in user:
            return json.dumps({"subject": f"Fix thing {n}", "body": ""})
        if "Rate the data" in user:
            return json.dumps({"score": 1 + n % 5, "quote": f"item N={n}"})
        if "Translate the data" in user:
            return json.dumps({"translation": f"Report N={n}."})
        return json.dumps({"claims": [{"claim": f"Claim N={n}.", "supported": True, "quote": f"N={n}"}]})

    fake.dynamic = answer
    diff = DIFF.replace("a:b", "a:b N=0")

    def one(n):
        return (tools.check_claims(cfg, f"Claim N={n}.", f"The source says N={n} here."),
                tools.commit_message(cfg, diff),
                tools.score(cfg, f"item N={n}", "length"),
                tools.translate(cfg, f"Rapport N={n}.", "en"))

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(one, range(1, 25)))
    for n, (c, m, s, t) in zip(range(1, 25), results):
        assert c["claims"][0]["claim"] == f"Claim N={n}." and m["subject"].startswith("Fix thing")
        assert s["score"] == 1 + n % 5 and t["translation"] == f"Report N={n}."


# ---------------------------------------------------------------- check_claims: number formats the model calls unsupported
_TABLE = "Photo scan\nTotal files: 10174\nWith GPS: 1712\nsame day as file date: 10174\nMissing title: 38\nUptime: 99.2%"


@pytest.mark.parametrize("text", ["1,712 files have GPS.", "10,174 are the same day as the file date.",
                                  "38 files have no title.", "Uptime was 99.2 percent."])
def test_check_claims_recheck_upgrades_a_true_claim_the_model_called_unsupported(fake, cfg, text):
    reply(fake, claims=[claim(text, False)])
    c = tools.check_claims(cfg, text, _TABLE)["claims"][0]
    assert c["supported"] is True and c["flag"] == "rechecked_in_code" and c["quote"] in _TABLE


@pytest.mark.parametrize("text", ["1,713 files have GPS.", "2,171 files have GPS.", "38 files have a title.",
                                  "Uptime was 99.3 percent.", "17 files have GPS.", "1,712 files have no GPS.",
                                  "1,712 cats live in Velmar.", "The scan found 10,174 files and 500 have GPS."])
def test_check_claims_recheck_never_upgrades_a_wrong_number_or_meaning(fake, cfg, text):
    reply(fake, claims=[claim(text, False)])
    c = tools.check_claims(cfg, text, _TABLE)["claims"][0]
    assert c["supported"] is False and "flag" not in c


def test_check_claims_recheck_leaves_claims_without_numbers_alone(fake, cfg):
    reply(fake, claims=[claim("Files have GPS.", False)])
    assert tools.check_claims(cfg, "Files have GPS.", _TABLE)["claims"][0]["supported"] is False


# ---------------------------------------------------------------- check_claims: claims that are not in the text
def test_check_claims_drops_claims_that_come_from_the_source_not_the_text(fake, cfg):
    text = "The export lists the LUNA Sessions folder."
    src = "LUNA Sessions folders were empty shells. Folder list: LUNA Sessions."
    reply(fake, claims=[claim("The export lists the LUNA Sessions folder.", True, "Folder list: LUNA Sessions"),
                        claim("LUNA Sessions folders were empty shells.", True, "LUNA Sessions folders were empty shells")])
    out = tools.check_claims(cfg, text, src)
    assert [c["claim"] for c in out["claims"]] == [text]
    assert out["dropped_not_in_text"] == ["LUNA Sessions folders were empty shells."]


def test_check_claims_keeps_condensed_and_reworded_claims(fake, cfg):
    text = "About 8,400 people came to the festival in Velmar, and entry was free."
    reply(fake, claims=[claim("About 8,400 people came to the festival.", True, "drew about 8,400 people"),
                        claim("The festival was in Velmar.", True, "in Velmar"), claim("Entry was free.", True, "Entry was free")])
    out = tools.check_claims(cfg, text, SRC)
    assert len(out["claims"]) == 3 and "dropped_not_in_text" not in out


def test_check_claims_all_claims_not_in_text_is_a_schema_failure(fake, cfg):
    reply(fake, claims=[claim("Velmar has a harbour museum.", False)])
    with pytest.raises(SchemaError):
        tools.check_claims(cfg, "Entry was free.", SRC)
