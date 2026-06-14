"""Tests for pf_core.web.templates — Jinja2 template setup."""

from __future__ import annotations

from fastapi import FastAPI

from pf_core.web.templates import setup_templates


class TestSetupTemplates:
    def test_returns_jinja2_templates(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "test.html").write_text("hello")
        templates = setup_templates(app, tdir)
        assert templates is not None

    def test_stores_on_app_state(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        templates = setup_templates(app, tdir)
        assert app.state.templates is templates

    def test_extra_globals(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        templates = setup_templates(
            app, tdir, extra_globals={"app_name": "TestApp", "version": "1.0"}
        )
        assert templates.env.globals["app_name"] == "TestApp"
        assert templates.env.globals["version"] == "1.0"

    def test_extra_filters(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        templates = setup_templates(
            app, tdir, extra_filters={"upper": str.upper}
        )
        assert "upper" in templates.env.filters

    def test_no_extras(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        templates = setup_templates(app, tdir)
        assert templates is not None

    def test_string_path(self, tmp_path):
        app = FastAPI()
        tdir = tmp_path / "templates"
        tdir.mkdir()
        templates = setup_templates(app, str(tdir))
        assert templates is not None
