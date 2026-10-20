import pytest

from minions import init
from minions.config import Roster


def test_starter_is_a_valid_config(tmp_path):
    p = init.write_starter(str(tmp_path / "sub" / "m.toml"), "ollama", "qwen3:8b")
    r = Roster.from_env({"MINION_CONFIG": str(p)})
    assert r.models["main"].backend == "ollama" and r.models["main"].model == "qwen3:8b"
    assert r.route("classify").fast[0].name == "main"


def test_default_placeholder_also_loads(tmp_path):
    p = init.write_starter(str(tmp_path / "m.toml"))
    assert Roster.from_env({"MINION_CONFIG": str(p)}).models["main"].model == "<your-model>"


def test_never_overwrites_without_force(tmp_path, capsys):
    p = tmp_path / "m.toml"
    p.write_text("mine")
    assert init.main(["--path", str(p)]) == 1 and p.read_text() == "mine"
    assert "--force" in capsys.readouterr().err
    assert init.main(["--path", str(p), "--force"]) == 0 and "[models.main]" in p.read_text()


def test_no_keys_and_no_personal_paths_in_starter():
    assert "/Users/" not in init.STARTER and "sk-" not in init.STARTER


def test_cli_dispatch(tmp_path, monkeypatch):
    from minions import server
    monkeypatch.setattr("sys.argv", ["mcp-minions", "init", "--path", str(tmp_path / "c.toml")])
    with pytest.raises(SystemExit) as e:
        server.main()
    assert e.value.code == 0 and (tmp_path / "c.toml").exists()
