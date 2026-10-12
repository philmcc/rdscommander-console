from pathlib import Path

from fastapi.testclient import TestClient

from rdscommander.main import create_application


def test_unknown_non_api_paths_serve_the_frontend_index(
    session_factory, operator_settings, console_config, tmp_path: Path, monkeypatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>console</body></html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr("rdscommander.main.frontend_dist_directory", lambda: dist)
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    assert client.get("/resources/abc").text == "<html><body>console</body></html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/v1/nope").status_code == 404


def test_files_in_the_dist_root_are_served_and_paths_outside_it_are_not(
    session_factory, operator_settings, console_config, tmp_path: Path, monkeypatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>console</body></html>")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("not for the browser")
    monkeypatch.setattr("rdscommander.main.frontend_dist_directory", lambda: dist)
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/..%2fsecret.txt").text == "<html><body>console</body></html>"
    assert client.get("/api/v1/bootstrap").json()["company_display_name"] == "Test Co"


def test_a_path_with_a_null_byte_answers_404_instead_of_failing(
    session_factory, operator_settings, console_config, tmp_path: Path, monkeypatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>console</body></html>")
    monkeypatch.setattr("rdscommander.main.frontend_dist_directory", lambda: dist)
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        ),
        raise_server_exceptions=False,
    )
    assert client.get("/%00").status_code == 404
    assert client.get("/assets%00/app.js").status_code == 404
    assert client.get("/resources/abc").text == "<html><body>console</body></html>"
