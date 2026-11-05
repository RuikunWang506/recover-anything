import json
import os
import subprocess

import pytest

from conftest import SMALL
from recover_anything import hunt
from recover_anything.cli import main
from recover_anything.devices import output_is_on_source
from recover_anything.simcard import scenarios


def test_recover_writes_report_and_sorted_folders(workdir, capsys):
    pytest.importorskip("PIL")
    sc = scenarios.deleted(workdir, 5, **SMALL)
    out = os.path.join(workdir, "Recovered")
    assert main(["recover", sc.image, "-o", out, "--json", "--workers", "1"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["files"] == 6
    assert os.path.exists(os.path.join(out, "report.html"))
    man = json.load(open(os.path.join(out, "recovery.json")))
    names = sorted(os.path.basename(f["file"]) for f in man["files"])
    assert "IMG_0001.JPG" in names
    assert any(f["file"].startswith("Photos" + os.sep + "2026-07") for f in man["files"])
    assert any(f["file"].startswith("Videos") for f in man["files"])
    html = open(os.path.join(out, "report.html"), encoding="utf-8").read()
    assert "5 photos and 1 video recovered" in html


def test_refuses_to_write_onto_source(workdir):
    img = os.path.join(workdir, "x.img")
    open(img, "wb").write(b"\0" * 4096)
    assert output_is_on_source(img, img)
    assert not output_is_on_source(img, os.path.join(workdir, "out"))


def test_triage_on_formatted_card(workdir):
    pytest.importorskip("PIL")
    from recover_anything.triage import triage
    sc = scenarios.formatted(workdir, 30, **SMALL)
    t = triage(sc.image)
    assert t["verdict"] == "recoverable"
    assert t["volumes"][0]["filesystem"] == "FAT32"


def test_triage_on_blank_card(workdir):
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.triage import triage
    img = os.path.join(workdir, "blank.img")
    with Fat32Card(img, **SMALL):
        pass
    t = triage(img)
    assert t["odds"] == "low" and t["verdict"] == "empty"


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e")
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env)


def test_hunt_git_reset_and_staged(workdir):
    repo = os.path.join(workdir, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    open(os.path.join(repo, "a.txt"), "w").write("first\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    open(os.path.join(repo, "thesis.md"), "w").write("chapter one, very important\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "two")
    open(os.path.join(repo, "notes.md"), "w").write("staged but never committed: unique-marker-42\n")
    _git(repo, "add", "notes.md")
    _git(repo, "reset", "--hard", "HEAD~1")  # thesis.md commit and staged notes are both gone
    assert not os.path.exists(os.path.join(repo, "thesis.md"))
    leads = hunt.git("thesis", repo)
    assert any(l.content == b"chapter one, very important\n" for l in leads)
    leads = hunt.git("unique-marker-42", repo)
    assert any(l.content and b"unique-marker-42" in l.content for l in leads)


def test_hunt_rebuilds_files_from_claude_code_log(workdir, monkeypatch):
    cfg = os.path.join(workdir, "claude")
    proj = os.path.join(cfg, "projects", "-home-me-app")
    os.makedirs(proj)
    target = os.path.join(workdir, "app", "server.py")
    lines = [
        {"type": "assistant", "timestamp": "2026-11-01T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": target, "content": "print('v1')\nport = 80\n"}}]}},
        {"type": "assistant", "timestamp": "2026-11-01T10:05:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": target, "old_string": "port = 80",
                                                           "new_string": "port = 8080"}}]}},
        {"type": "assistant", "timestamp": "2026-11-01T10:09:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf app"}}]}},
    ]
    with open(os.path.join(proj, "s.jsonl"), "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
    leads = hunt.claude_code(None, os.path.join(workdir, "app"))
    assert len(leads) == 1
    assert leads[0].content == b"print('v1')\nport = 8080\n"
    saved = hunt.save(leads, os.path.join(workdir, "found"))
    assert open(saved[0]).read() == "print('v1')\nport = 8080\n"


def test_hunt_vscode_history(workdir, monkeypatch):
    import sys
    if sys.platform != "linux":
        pytest.skip("path layout differs")
    cfg = os.path.join(workdir, "xdg")
    hist = os.path.join(cfg, "Code", "User", "History", "abc123")
    os.makedirs(hist)
    lost = os.path.join(workdir, "proj", "index.ts")
    json.dump({"version": 1, "resource": "file://" + lost,
               "entries": [{"id": "x1.ts", "timestamp": 1760000000000}, {"id": "x2.ts", "timestamp": 1760000100000}]},
              open(os.path.join(hist, "entries.json"), "w"))
    open(os.path.join(hist, "x1.ts"), "w").write("old")
    open(os.path.join(hist, "x2.ts"), "w").write("newest")
    monkeypatch.setenv("XDG_CONFIG_HOME", cfg)
    leads = hunt.editor_history("index.ts", None)
    assert leads and open(leads[0].copy_from).read() == "newest"
