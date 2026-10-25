"""summarize's content check: numbers, dates and capitalised names in a summary must occur in the source,
and the summary must keep to max_words (with a small tolerance)."""
import json

import pytest

from minions import calllog, tools
from minions.config import Roster
from minions.errors import EmptyValue, Invented, NotInSource, SchemaError, TooLong
from minions.values import make_summary_check, word_limit

REPORT = ("The harbour board of Port Vellmar met on 3 November 2026. It approved 1,250,000 EUR for a new crane "
          "and hired Dana Okafor as project lead. Work starts in March; 40% of the cost comes from the city.")


def summary(fake, s, model=None):
    (fake.by_model.setdefault(model, []) if model else fake.replies).append(json.dumps({"summary": s}))


def with_judge(fake, meta=False):
    models = {"f1": {"backend": "lmstudio", "base_url": fake.url, "model": "f1", "timeout": 5},
              "j": {"backend": "lmstudio", "base_url": fake.url, "model": "j", "kind": "reasoning", "timeout": 5}}
    return Roster.from_dict({"models": models, "routes": {"summarize": {"fast": ["f1"], "judge": "j"}}}, meta=meta)


def passes(s, source=REPORT, max_words=60):
    make_summary_check(source, max_words)({"summary": s})
    return True


def fails(s, source=REPORT, max_words=60):
    with pytest.raises(SchemaError) as e:
        make_summary_check(source, max_words)({"summary": s})
    return e.value


# ---------- through the tool ----------
def test_invented_number_is_minion_invented(fake, cfg):
    summary(fake, "Port Vellmar approved 2 million EUR for a crane.")
    with pytest.raises(Invented, match="MINION_INVENTED: .*'2'"):
        tools.summarize(cfg, REPORT, 30)


def test_invented_is_a_not_in_source_and_a_schema_failure():
    assert issubclass(Invented, NotInSource) and issubclass(TooLong, SchemaError)


def test_faithful_summary_passes(fake, cfg):
    s = "Port Vellmar's harbour board approved 1,250,000 EUR for a crane on 2026-11-03, led by Dana Okafor."
    summary(fake, s)
    assert tools.summarize(cfg, REPORT, 30) == {"summary": s}


def test_task_asks_for_the_source_forms(fake, cfg):
    summary(fake, "A crane was approved.")
    tools.summarize(cfg, REPORT, 30)
    assert "only numbers, dates and names that the data states" in fake.requests[0][1]["messages"][1]["content"]


def test_summarize_file_runs_the_same_check(fake, cfg, tmp_path):
    f = tmp_path / "report.txt"
    f.write_text(REPORT, encoding="utf-8")
    summary(fake, "Dana Okafor and Lee Park lead the crane project.")
    with pytest.raises(Invented, match="'Lee', 'Park'"):
        tools.summarize(cfg, tools.read_text_file(cfg, str(f)), 30)


def test_empty_summary_is_minion_empty(fake, cfg):
    summary(fake, "   ")
    with pytest.raises(EmptyValue):
        tools.summarize(cfg, REPORT, 30)


def test_too_long_is_minion_too_long(fake, cfg):
    summary(fake, " ".join(["crane"] * 13))
    with pytest.raises(TooLong, match="13 words; max_words is 10 \\(up to 12 accepted\\)"):
        tools.summarize(cfg, REPORT, 10)


@pytest.mark.parametrize("max_words,limit", [(5, 7), (10, 12), (20, 22), (25, 28), (100, 110), (1000, 1100)])
def test_word_limit(max_words, limit):
    assert word_limit(max_words) == limit


# ---------- what is checked ----------
@pytest.mark.parametrize("s", [
    "Approved 1250000 EUR.",                          # grouping dropped
    "Approved 1.25 million EUR.",                     # scaled by a word
    "The board met on Nov 3, 2026.",                  # the same date, another form
    "Work starts in March, with 40 percent from the city.",
    "Overall, the board hired Dana Okafor.",          # sentence-initial common word
    "Crane approved. Funding is shared: City pays 40%.",   # capitals after a full stop or a colon
    "- Crane approved\n- Okafor leads\n- Starts in March",  # list items
    "Okafor's project starts in March.",              # possessive
    "The Vellmar harbour board met. I think it's a crane deal.",
    "The board approved three things.",               # number words are not checked
])
def test_faithful_forms_pass(s):
    assert passes(s)


@pytest.mark.parametrize("s,bad", [
    ("Approved 1,300,000 EUR.", "1,300,000"),
    ("The board met on 4 November 2026.", "'4'"),
    ("Work starts in April.", "April"),
    ("The board hired Dana Smith.", "Smith"),
    ("Funding comes from NASA.", "NASA"),             # all capitals are checked even at a sentence start
    ("Approved 1.3 million EUR.", "1.3"),
    ("About 45% comes from the city.", "45"),         # a rounded or changed number fails
])
def test_invented_details_fail(s, bad):
    e = fails(s)
    assert isinstance(e, Invented) and bad in str(e)


def test_codes_with_letters_are_checked_by_their_digits():
    assert passes("Classes 4B and 5A run the relay.", source="Classes 4B and 5A will run the relay on Monday.")
    assert "'6'" in str(fails("Class 6B runs the relay.", source="Class 4B runs the relay."))


def test_related_word_forms_pass():
    assert passes("Swedish ports grew.", source="Ports in Sweden grew.")
    assert passes("The Okafors met.", source="Dana Okafor met the board.")


def test_limit_each_token_is_checked_alone():
    # documented limit: March and 2026 both occur, so a wrong pairing of them is not caught
    assert passes("Work starts in March 2026.")


# ---------- routing ----------
def test_judge_repairs_an_invented_number_and_the_log_counts_it(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("MINION_LOG", str(tmp_path / "calls.jsonl"))
    summary(fake, "The board approved 2 million EUR.", "f1")
    summary(fake, "The board approved 1,250,000 EUR.", "j")
    out = tools.summarize(with_judge(fake, meta=True), REPORT, 30)
    assert out["summary"] == "The board approved 1,250,000 EUR."
    assert out["_minion"]["judge"] == "j" and out["_minion"]["check_failures"] == ["MINION_INVENTED"]
    assert "MINION_INVENTED" in fake.calls("j")[0]["messages"][1]["content"]
    assert calllog.report(1)["check_failures_repaired"] == {"MINION_INVENTED": 1}


def test_judge_repair_must_pass_the_check_too(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("MINION_LOG", str(tmp_path / "calls.jsonl"))
    bad = "The board approved 2 million EUR."
    summary(fake, bad, "f1")
    summary(fake, bad, "j")
    summary(fake, bad, "j")
    with pytest.raises(Invented, match="failed validation after one retry"):
        tools.summarize(with_judge(fake), REPORT, 30)
    assert calllog.report(1)["errors"] == {"MINION_INVENTED": 1}
