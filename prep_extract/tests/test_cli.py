import json
import subprocess
import sys

import forms
from conftest import BACKEND

from prep_extract import cli


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "prep_extract", *args], cwd=str(BACKEND),
                          capture_output=True, timeout=180)


def test_cli_writes_form_fields_and_a_summary_line(site, tmp_path):
    site.pages["/fr"] = forms.page('<div><label for="q">Pourquoi Résumé?</label><input id="q"></div>')
    out = tmp_path / "forms" / "f.json"
    cp = run_cli("--url", site.url("/fr"), "--out", str(out), "--timeout", "60")
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")
    text = out.read_text(encoding="utf-8")
    assert "Pourquoi Résumé?" in text                      # ensure_ascii=False
    data = json.loads(text)
    assert data["questions_visible"] is True and data["adapter"] == "generic_dom"
    summary = json.loads(cp.stdout.decode("utf-8").strip().splitlines()[-1])
    assert summary == {"ats": None, "adapter": "generic_dom", "questions_visible": True, "sign_in_required": False,
                       "pages": 1, "fields": 1, "clicks": 0, "unseen_pages": False, "errors": 0}
    assert list(out.parent.glob("*.tmp")) == []


def test_cli_unreachable_url_still_writes_a_file(tmp_path):
    out = tmp_path / "f.json"
    cp = run_cli("--url", "http://127.0.0.1:9/nothing", "--out", str(out), "--timeout", "30")
    assert cp.returncode == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["questions_visible"] is False and data["errors"]


def test_cli_crash_still_writes_a_stub(tmp_path, monkeypatch):
    def crash(*a, **k):
        raise KeyError("kaboom")
    monkeypatch.setattr(cli.pipeline, "extract", crash)
    out = tmp_path / "f.json"
    assert cli.main(["--url", "https://jobs.example.com/a", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["questions_visible"] is False and data["errors"][0].startswith("extractor crashed: KeyError")


def test_watchdog_writes_the_stub_first_then_kills(tmp_path, monkeypatch):
    import threading
    order = []
    forever = threading.Event()
    monkeypatch.setattr(cli.pipeline, "extract", lambda *a, **k: forever.wait())
    out = tmp_path / "f.json"

    def kill():
        order.append("kill")
        order.append(out.exists())      # the stub must already be on disk when the kill happens
    try:
        rc = cli.run(["--url", "https://jobs.example.com/a", "--out", str(out), "--timeout", "10"],
                     kill=kill, join_s=0.3)
    finally:
        forever.set()
    assert rc == 0 and order == ["kill", True]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["questions_visible"] is False and data["errors"] == ["extraction timed out after 10 s"]


def test_watchdog_not_triggered_when_extract_finishes(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli.pipeline, "extract", lambda url, timeout_s: cli.schema.stub_result(url, "x"))
    out = tmp_path / "f.json"
    assert cli.run(["--url", "https://a.example/x", "--out", str(out)], kill=lambda: called.append(1), join_s=5) == 0
    assert called == [] and out.exists()


def test_grace_constant_is_exposed():
    assert cli.GRACE_S == 15


def test_unwritable_out_exits_3_with_a_stderr_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.pipeline, "extract", lambda url, timeout_s: cli.schema.stub_result(url, "x"))
    d = tmp_path / "adir"
    d.mkdir()
    assert cli.main(["--url", "https://a.example/x", "--out", str(d)]) == 3
    assert "cannot write" in capsys.readouterr().err
