"""scripts/check_url_safe.py: real public urls pass, attack spellings are refused, and no network call is ever made."""
import importlib.util
import socket
import sys
from pathlib import Path

import pytest

from minions import fetch

scripts = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("check_url_safe", scripts / "check_url_safe.py")
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)
spec2 = importlib.util.spec_from_file_location("eval_urls", scripts / "eval_urls.py")
eu = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(eu)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("check_url_safe touched the network")
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "create_connection"):
        monkeypatch.setattr(socket, name, boom)
    monkeypatch.setattr(socket, "socket", boom)


REAL = [u for u, _ in eu.DEFAULT_PAGES] + [
    "https://example.org/", "http://example.com/a?b=1", "https://8.8.8.8/", "https://[2001:4860:4860::8888]/dns",
]


@pytest.mark.parametrize("url", REAL)
def test_real_public_urls_are_ok(url):
    r = cu.check(url)
    assert r["ok"] and r["reason"] is None and r["resolved"] is False and r["note"]


ATTACKS = [   # (url, a word the reason must contain)
    ("http://127.0.0.1/", "loopback"), ("http://127.0.0.1:80/", "loopback"), ("https://[::1]/", "loopback"),
    ("http://10.0.0.5/", "private"), ("http://172.16.4.1/", "private"), ("http://192.168.1.1/x", "private"),
    ("http://169.254.169.254/latest/meta-data/", "metadata"), ("http://[fd00:ec2::254]/", "metadata"),
    ("http://169.254.10.10/", "link-local"), ("http://224.0.0.1/", "multicast"), ("http://0.0.0.0/", "address"),
    ("http://127.1/", "loopback"), ("http://0x7f.0.0.1/", "loopback"), ("http://0177.0.0.1/", "loopback"),
    ("http://2130706433/", "internal"), ("http://017700000001/", "internal"), ("http://0x7f000001/", "internal"),
    ("http://[::ffff:127.0.0.1]/", "loopback"), ("http://[::ffff:10.1.2.3]/", "private"),
    ("http://localhost/", "internal"), ("http://printer.local/", "internal"), ("http://db.internal/", "internal"),
    ("http://intranet/", "internal"), ("http://user:pw@example.org/", "credentials"), ("http://example.org:8080/", "port"),
    ("https://example.org:444/", "port"), ("file:///etc/hosts", "http"), ("ftp://example.org/", "http"),
    ("javascript:alert(1)", "http"), ("", "non-empty"), ("   ", "non-empty"), ("not a url", "http"),
]


@pytest.mark.parametrize("url,word", ATTACKS)
def test_attack_forms_are_refused(url, word):
    r = cu.check(url)
    assert not r["ok"] and word in r["reason"].lower(), r


def test_the_reason_is_the_fetch_layers_own_for_what_it_checks_without_a_lookup():
    for url in ("http://localhost/", "http://example.org:8080/", "file:///etc/hosts"):
        with pytest.raises(Exception) as e:
            fetch.check_url(url)
        assert str(e.value) == cu.check(url)["reason"]


def test_every_address_class_the_fetch_layer_refuses_is_refused_here_too():
    import ipaddress
    for ip in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "224.1.1.1", "0.0.0.0", "240.0.0.1", "::1", "fe80::1", "ff02::1", "::"):
        host = f"[{ip}]" if ":" in ip else ip
        assert fetch.address_problem(ipaddress.ip_address(ip)), ip
        assert not cu.check(f"http://{host}/")["ok"], ip


def test_a_name_is_ok_but_says_it_was_not_resolved():
    r = cu.check("https://www.sqlite.org/about.html")
    assert r["ok"] and r["resolved"] is False and "not resolved" in r["note"]


def test_cli_exit_codes(capsys):
    assert cu.main(["https://example.org/"]) == 0
    assert cu.main(["https://example.org/", "http://127.0.0.1/"]) == 1
    out = capsys.readouterr().out
    assert "OK" in out and "REFUSED http://127.0.0.1/" in out
    assert cu.main([]) == 2


def test_fetch_py_is_untouched_by_this_tool():
    # the tool only imports the fetch layer's functions; it adds no option to it
    src = (scripts / "check_url_safe.py").read_text()
    assert "fetch.fetch(" not in src and "pick_address" not in src and "_request" not in src
