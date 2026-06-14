"""Tests for pf_core.web.pagination."""

from __future__ import annotations

from pf_core.web.pagination import paginate_params, paginate_result


class TestPaginateParams:
    def test_defaults(self):
        p = paginate_params(1, 50)
        assert p["page"] == 1
        assert p["per_page"] == 50
        assert p["offset"] == 0
        assert p["limit"] == 51
        assert p["sort"] == "id"
        assert p["dir"] == "desc"

    def test_page_offset(self):
        p = paginate_params(3, 25)
        assert p["offset"] == 50
        assert p["limit"] == 26

    def test_page_clamped_to_1(self):
        p = paginate_params(0, 25)
        assert p["page"] == 1
        assert p["offset"] == 0

    def test_per_page_clamped_to_default_max(self):
        p = paginate_params(1, 500)
        assert p["per_page"] == 200

    def test_per_page_clamped_to_env_max(self, monkeypatch):
        monkeypatch.setenv("MAX_PER_PAGE", "50")
        p = paginate_params(1, 500)
        assert p["per_page"] == 50

    def test_per_page_explicit_max_overrides_env(self, monkeypatch):
        monkeypatch.setenv("MAX_PER_PAGE", "50")
        p = paginate_params(1, 500, max_per_page=100)
        assert p["per_page"] == 100

    def test_per_page_clamped_to_1(self):
        p = paginate_params(1, 0)
        assert p["per_page"] == 1

    def test_sort_allowed(self):
        p = paginate_params(1, 50, "date", allowed_sorts={"date", "title"})
        assert p["sort"] == "date"

    def test_sort_rejected_falls_back(self):
        p = paginate_params(
            1, 50, "hacked",
            allowed_sorts={"date", "title"},
            default_sort="date",
        )
        assert p["sort"] == "date"

    def test_sort_none_uses_default(self):
        p = paginate_params(1, 50, None, default_sort="created_at")
        assert p["sort"] == "created_at"

    def test_sort_normalized_to_lowercase(self):
        p = paginate_params(1, 50, "  Date  ", allowed_sorts={"date"})
        assert p["sort"] == "date"

    def test_dir_asc(self):
        p = paginate_params(1, 50, dir="asc")
        assert p["dir"] == "asc"

    def test_dir_desc(self):
        p = paginate_params(1, 50, dir="desc")
        assert p["dir"] == "desc"

    def test_dir_case_insensitive(self):
        p = paginate_params(1, 50, dir="ASC")
        assert p["dir"] == "asc"

    def test_dir_invalid_falls_back(self):
        p = paginate_params(1, 50, dir="invalid", default_dir="desc")
        assert p["dir"] == "desc"

    def test_dir_none_uses_default(self):
        p = paginate_params(1, 50, dir=None, default_dir="asc")
        assert p["dir"] == "asc"

    def test_no_allowed_sorts_accepts_anything(self):
        p = paginate_params(1, 50, "anything")
        assert p["sort"] == "anything"


class TestPaginateResult:
    def test_single_page(self):
        items = ["a", "b", "c"]
        r = paginate_result(items, total=3, page=1, per_page=10)
        assert r["items"] == ["a", "b", "c"]
        assert r["total"] == 3
        assert r["total_pages"] == 1
        assert r["has_prev"] is False
        assert r["has_next"] is False

    def test_trims_extra_item(self):
        items = ["a", "b", "c", "d"]  # per_page=3, fetched 4
        r = paginate_result(items, total=10, page=1, per_page=3)
        assert r["items"] == ["a", "b", "c"]
        assert r["has_next"] is True

    def test_has_prev_on_page_2(self):
        r = paginate_result(["x"], total=5, page=2, per_page=3)
        assert r["has_prev"] is True

    def test_no_has_next_on_last_page(self):
        items = ["a", "b"]  # only 2 items, per_page=3
        r = paginate_result(items, total=5, page=2, per_page=3)
        assert r["has_next"] is False

    def test_total_pages_rounds_up(self):
        r = paginate_result([], total=7, page=1, per_page=3)
        assert r["total_pages"] == 3

    def test_total_pages_exact_division(self):
        r = paginate_result([], total=9, page=1, per_page=3)
        assert r["total_pages"] == 3

    def test_total_pages_minimum_1(self):
        r = paginate_result([], total=0, page=1, per_page=25)
        assert r["total_pages"] == 1

    def test_empty_items(self):
        r = paginate_result([], total=0, page=1, per_page=25)
        assert r["items"] == []
        assert r["has_prev"] is False
        assert r["has_next"] is False

    def test_page_and_per_page_in_result(self):
        r = paginate_result(["a"], total=1, page=3, per_page=25)
        assert r["page"] == 3
        assert r["per_page"] == 25
