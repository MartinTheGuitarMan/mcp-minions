"""extract's value check: required values must be non-empty and occur in the source; optional ones are flagged."""
import json

import pytest

from minions import calllog, tools
from minions.config import Roster
from minions.errors import EmptyValue, NotInSource, SchemaError
from minions.values import Source

ORDER = "Order 5521 ships on 2026-11-03 to Malmö, 3 boxes."
ORDER_SCHEMA = {"type": "object", "properties": {"order": {"type": "string"}, "date": {"type": "string"},
                                                 "city": {"type": "string"}, "boxes": {"type": "integer"}},
                "required": ["order", "date", "city", "boxes"]}


def reply(fake, obj):
    fake.replies.append(json.dumps(obj))


def with_judge(fake, meta=False):
    models = {"f1": {"backend": "lmstudio", "base_url": fake.url, "model": "f1", "timeout": 5},
              "j": {"backend": "lmstudio", "base_url": fake.url, "model": "j", "kind": "reasoning", "timeout": 5}}
    return Roster.from_dict({"models": models, "routes": {"extract": {"fast": ["f1"], "judge": "j"}}}, meta=meta)


# ---------- required fields ----------
def test_all_empty_answer_is_minion_empty(fake, cfg):
    reply(fake, {"order": "", "date": "", "city": "", "boxes": 0})
    with pytest.raises(EmptyValue, match="MINION_EMPTY: required field 'order' is empty"):
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_whitespace_string_counts_as_empty(fake, cfg):
    reply(fake, {"order": "5521", "date": "2026-11-03", "city": "  ", "boxes": 3})
    with pytest.raises(EmptyValue, match="'city' is empty"):
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_zero_with_no_zero_in_source_is_empty(fake, cfg):
    reply(fake, {"order": "5521", "date": "2026-11-03", "city": "Malmö", "boxes": 0})
    with pytest.raises(EmptyValue, match="'boxes' is 0 and the source has no 0"):
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_zero_stated_in_source_passes(fake, cfg):
    schema = {"type": "object", "properties": {"errors": {"type": "integer"}}, "required": ["errors"]}
    reply(fake, {"errors": 0})
    assert tools.extract(cfg, "The nightly run finished with 0 errors.", schema) == {"errors": 0}


def test_made_up_value_is_not_in_source(fake, cfg):
    reply(fake, {"order": "5521", "date": "2026-11-03", "city": "Stockholm", "boxes": 3})
    with pytest.raises(NotInSource, match="MINION_NOT_IN_SOURCE: required field 'city' 'Stockholm'"):
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_made_up_number_is_not_in_source(fake, cfg):
    reply(fake, {"order": "5521", "date": "2026-11-03", "city": "Malmö", "boxes": 4})
    with pytest.raises(NotInSource, match="'boxes' 4 is not in the source"):
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_value_check_errors_are_schema_errors(fake, cfg):
    reply(fake, {"order": "", "date": "", "city": "", "boxes": 0})
    with pytest.raises(SchemaError):   # callers that catch MINION_SCHEMA keep working
        tools.extract(cfg, ORDER, ORDER_SCHEMA)


def test_correct_answer_passes(fake, cfg):
    reply(fake, {"order": "5521", "date": "2026-11-03", "city": "Malmö", "boxes": 3})
    assert tools.extract(cfg, ORDER, ORDER_SCHEMA) == {"order": "5521", "date": "2026-11-03", "city": "Malmö", "boxes": 3}


def test_case_accents_and_punctuation_are_normalised(fake, cfg):
    reply(fake, {"order": "order 5521", "date": "2026-11-03", "city": "MALMO", "boxes": 3})
    assert tools.extract(cfg, ORDER, ORDER_SCHEMA)["city"] == "MALMO"


def test_whole_words_only(fake, cfg):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    reply(fake, {"city": "Lund"})
    with pytest.raises(NotInSource):
        tools.extract(cfg, "The parcel went to Lundby.", schema)


def test_enum_boolean_and_null_are_not_checked(fake, cfg):
    schema = {"type": "object", "properties": {"mood": {"type": "string", "enum": ["calm", "upset"]},
                                               "urgent": {"type": "boolean"}, "ref": {"type": ["string", "null"]}},
              "required": ["mood", "urgent", "ref"]}
    reply(fake, {"mood": "upset", "urgent": False, "ref": None})
    assert tools.extract(cfg, "Why has nobody answered my letter?", schema)["mood"] == "upset"


def test_array_items_are_checked(fake, cfg):
    schema = {"type": "object", "properties": {"towns": {"type": "array", "items": {"type": "string"}}},
              "required": ["towns"]}
    reply(fake, {"towns": ["Lund", "Ystad"]})
    assert tools.extract(cfg, "Stops: Lund, Ystad.", schema) == {"towns": ["Lund", "Ystad"]}
    reply(fake, {"towns": ["Lund", "Visby"]})
    with pytest.raises(NotInSource, match="'Visby'"):
        tools.extract(cfg, "Stops: Lund, Ystad.", schema)


# ---------- optional fields ----------
OPT_SCHEMA = {"type": "object", "properties": {"order": {"type": "string"}, "phone": {"type": "string"},
                                               "weight_kg": {"type": "number"}}, "required": ["order"]}


def test_optional_field_missing_passes_without_flag(fake, cfg):
    reply(fake, {"order": "5521"})
    assert tools.extract(cfg, ORDER, OPT_SCHEMA) == {"order": "5521"}


def test_optional_field_empty_passes_without_flag(fake, cfg):
    reply(fake, {"order": "5521", "phone": ""})
    assert tools.extract(cfg, ORDER, OPT_SCHEMA) == {"order": "5521", "phone": ""}


def test_optional_field_not_in_source_is_flagged(fake, cfg):
    reply(fake, {"order": "5521", "phone": "555-0100", "weight_kg": 12.5})
    out = tools.extract(cfg, ORDER, OPT_SCHEMA)
    assert out == {"order": "5521", "phone": "555-0100", "weight_kg": 12.5, "_unverified": ["phone", "weight_kg"]}


# ---------- number and date forms ----------
@pytest.mark.parametrize("source, value", [
    ("Invoice total: 1,250.50 EUR", 1250.5),
    ("Rechnung: 1.250,50 EUR", 1250.5),
    ("The ship weighs 12 400 tonnes.", 12400),
    ("Unit price 19.99 USD", 19.99),
    ("He is forty-two.", 42),
    ("three boxes", 3),
    ("Reading: -4 degrees", -4),
])
def test_number_forms_found(source, value):
    assert Source(source).has_number(value)


@pytest.mark.parametrize("source, value", [
    ("Invoice total: 1,250.50 EUR", 125.05),
    ("3 boxes", 30),
    ("Order 5521", 552),
])
def test_number_forms_not_found(source, value):
    assert not Source(source).has_number(value)


@pytest.mark.parametrize("source, value", [
    ("Ships on 3 November 2026.", "2026-11-03"),
    ("Ships on Nov 3, 2026.", "2026-11-03"),
    ("Ships on the 3rd of November 2026", "3 Nov 2026"),
    ("Ships on 03/11/2026.", "2026-11-03"),
    ("Ships on 2026-11-03.", "November 3, 2026"),
    ("Total 1,250.50 EUR", "1250.50"),
])
def test_string_date_and_number_forms_found(source, value):
    assert Source(source).has_string(value)


def test_wrong_date_not_found():
    assert not Source("Ships on 3 November 2026.").has_string("2026-11-04")


def test_extract_with_normalised_forms(fake, cfg):
    schema = {"type": "object", "properties": {"due": {"type": "string"}, "total": {"type": "number"}},
              "required": ["due", "total"]}
    reply(fake, {"due": "2026-11-30", "total": 1250.5})
    assert tools.extract(cfg, "Due 30 November 2026, total 1,250.50 EUR.", schema) == {"due": "2026-11-30", "total": 1250.5}


def test_extract_file_runs_the_same_check(fake, cfg, tmp_path):
    f = tmp_path / "order.txt"
    f.write_text(ORDER, encoding="utf-8")
    reply(fake, {"order": "", "date": "", "city": "", "boxes": 0})
    with pytest.raises(EmptyValue):
        tools.extract(cfg, tools.read_text_file(cfg, str(f)), ORDER_SCHEMA)


# ---------- routing and logging ----------
def test_judge_repairs_an_empty_answer_and_the_log_counts_it(fake, tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MINION_LOG", str(log))
    good = {"order": "5521", "date": "2026-11-03", "city": "Malmö", "boxes": 3}
    fake.by_model.update(f1=[json.dumps({"order": "", "date": "", "city": "", "boxes": 0})], j=[json.dumps(good)])
    out = tools.extract(with_judge(fake, meta=True), ORDER, ORDER_SCHEMA)
    assert {k: out[k] for k in good} == good
    assert out["_minion"]["judge"] == "j" and out["_minion"]["check_failures"] == ["MINION_EMPTY"]
    assert "MINION_EMPTY" in fake.calls("j")[0]["messages"][1]["content"]
    assert calllog.report(1)["check_failures_repaired"] == {"MINION_EMPTY": 1}


def test_judge_gets_one_retry_and_the_kind_is_kept(fake, tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MINION_LOG", str(log))
    invented = json.dumps({"order": "5521", "date": "2026-11-03", "city": "Stockholm", "boxes": 3})
    fake.by_model.update(f1=[invented], j=[invented, invented])
    with pytest.raises(NotInSource, match="failed validation after one retry"):
        tools.extract(with_judge(fake), ORDER, ORDER_SCHEMA)
    assert len(fake.calls("j")) == 2
    assert "rejected: MINION_NOT_IN_SOURCE" in fake.calls("j")[1]["messages"][-1]["content"]
    assert calllog.report(1)["errors"] == {"MINION_NOT_IN_SOURCE": 1}
