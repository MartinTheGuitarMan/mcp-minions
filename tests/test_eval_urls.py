"""scripts/eval_urls.py: verdicts for planted summaries and bad page lists (no network: summarize and source are injected)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from minions import fetch
from minions.errors import BadInput

scripts = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("eval_urls", scripts / "eval_urls.py")
eu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eu)

PAGE = "The Harbor Lights Festival ran from 12 to 14 June in Velmar. About 8,400 people attended. Entry was free."


def run(summary, keys=("festival",), source=PAGE, boom=None):
    def summarize(url):
        if boom:
            raise boom
        return {"summary": summary}
    return eu.score_page(None, "https://example.org/x", list(keys), 80, summarize=summarize, source=lambda u: source)


def test_a_good_summary_passes():
    r = run("The Harbor Lights Festival drew about 8,400 people to Velmar.")
    assert (r["ok"], r["verdict"]) == (True, "PASS")


def test_no_key_term_fails():
    r = run("A summer event drew a crowd.", keys=("festival", "harbor"))
    assert (r["ok"], r["verdict"]) == (False, "NO KEY TERM")


def test_key_terms_ignore_case():
    assert run("THE FESTIVAL went well.")["ok"]


def test_an_invented_number_fails_even_with_a_key_term():
    r = run("The festival drew about 9,100 people.")
    assert not r["ok"] and r["verdict"].startswith("INVENTED")


def test_an_invented_name_fails():
    r = run("The festival in Karlstad drew a crowd.")
    assert not r["ok"] and r["verdict"].startswith("INVENTED")


def test_a_minion_error_is_an_error_verdict():
    r = run("x", boom=BadInput("the page has no readable text"))
    assert not r["ok"] and r["verdict"].startswith("ERROR") and "no readable text" in r["verdict"]


def test_any_other_exception_is_an_error_verdict_too():
    r = run("x", boom=OSError("connection reset"))
    assert not r["ok"] and r["verdict"].startswith("ERROR OSError")


def test_the_built_in_list_is_the_old_twenty_and_every_url_passes_the_fetch_syntax_check():
    assert len(eu.DEFAULT_PAGES) == 20
    for url, keys in eu.DEFAULT_PAGES:
        fetch.check_url(url)          # syntax and host name only, no lookup
        assert keys
    assert eu.DEFAULT_PAGES[0][0] == "https://www.gnu.org/licenses/gpl-3.0.html"


def test_the_old_script_is_a_thin_caller():
    spec2 = importlib.util.spec_from_file_location("eval_summarize_url", scripts / "eval_summarize_url.py")
    sys.path.insert(0, str(scripts))
    m = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(m)
    assert m.main.__module__ == "eval_urls"


def test_page_list_formats(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps([{"url": "https://example.org/a", "keys": ["a"]}, ["https://example.org/b", ["b", "c"]]]))
    assert eu.load_pages(str(f)) == [("https://example.org/a", ["a"]), ("https://example.org/b", ["b", "c"])]


@pytest.mark.parametrize("content,msg", [
    ("not json", "cannot read"), ("[]", "non-empty list"), ('{"a": 1}', "non-empty list"),
    ('[{"keys": ["a"]}]', "no url"), ('[{"url": "https://example.org/a"}]', "key term"),
    ('[{"url": "https://example.org/a", "keys": []}]', "key term"), ('[{"url": "https://example.org/a", "keys": [""]}]', "key term"),
    ('[["https://example.org/a"]]', "no url"),
])
def test_bad_page_lists_are_named(tmp_path, content, msg):
    f = tmp_path / "p.json"
    f.write_text(content)
    with pytest.raises(ValueError, match=msg):
        eu.load_pages(str(f))


def test_cli_reports_a_bad_list_with_exit_2(tmp_path, capsys):
    f = tmp_path / "p.json"
    f.write_text("[]")
    assert eu.main(["--pages", str(f)]) == 2


def test_percentile():
    assert eu.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0 and eu.percentile([5.0], 0.95) == 5.0
