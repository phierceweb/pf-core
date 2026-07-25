"""Tests for pf_core.fetch.images — remote-image localizer.

Hermetic: every download goes through a recording fake fetcher; an autouse
tripwire fakes DNS and fails loudly if a real request is ever attempted.
"""

from __future__ import annotations

import dataclasses
import socket
import urllib.error
from email.message import Message

import pytest

from pf_core.exceptions import ClientError, InvalidInputError
from pf_core.fetch import Fetcher
from pf_core.fetch import images as images_mod
from pf_core.fetch.images import (
    LocalizeResult,
    count_remote_images,
    default_namer,
    localize_file,
    localize_images,
    sniff_image_ext,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload"
JPG = b"\xff\xd8\xff\xe0" + b"jpg-payload"
GIF = b"GIF89a" + b"gif-payload"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"webp-payload"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fake DNS resolution and make any real request attempt fail the test."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def no_open(self, request, timeout_s):
        raise AssertionError("real network request attempted")

    monkeypatch.setattr(Fetcher, "_open", no_open)


class FakeFetcher:
    """Recording fetcher: serves ``responses[url]`` (bytes or exception), else ``default``."""

    def __init__(
        self,
        responses: dict[str, bytes | Exception] | None = None,
        default: bytes | Exception = PNG,
    ) -> None:
        self.calls: list[str] = []
        self.responses = dict(responses or {})
        self.default = default

    def get_bytes(self, url: str, *, timeout_s: float = 30.0) -> tuple[str, bytes]:
        self.calls.append(url)
        item = self.responses.get(url, self.default)
        if isinstance(item, Exception):
            raise item
        return url, item


def _http_error(code: int = 404) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.com/docs/x", code, f"status {code}", Message(), None
    )


class TestDefaultNamer:
    def test_images_segment_dash_join(self):
        url = "https://example.com/images/getting-started/interface.png"
        assert default_namer(url) == "getting-started-interface.png"

    def test_no_images_segment_falls_back_to_last_two_segments(self):
        assert default_namer("https://example.com/assets/figure.png") == "assets-figure.png"

    def test_cross_subpath_names_do_not_collide(self):
        assert default_namer("https://example.com/images/setup/diagram.png") == "setup-diagram.png"
        assert (
            default_namer("https://example.com/images/advanced/diagram.png")
            == "advanced-diagram.png"
        )

    def test_percent_encoding_preserved(self):
        url = "https://example.com/images/user%20guide/setup%20figure.png"
        assert default_namer(url) == "user%20guide-setup%20figure.png"


class TestSniffImageExt:
    @pytest.mark.parametrize(
        ("data", "ext"),
        [
            (PNG, ".png"),
            (JPG, ".jpg"),
            (b"GIF87a-payload", ".gif"),
            (GIF, ".gif"),
            (WEBP, ".webp"),
            (SVG, ".svg"),
            (b'  <?xml version="1.0"?><svg/>', ".svg"),
            (b"unrecognized-bytes", ".png"),
        ],
    )
    def test_magic_bytes(self, data, ext):
        assert sniff_image_ext(data) == ext


class TestLocalizeImages:
    def test_markdown_ref_downloaded_and_retargeted(self, tmp_path):
        fetcher = FakeFetcher()
        md = "Intro\n\n![ui](https://example.com/images/getting-started/interface.png)\n"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.markdown == "Intro\n\n![ui](images/getting-started-interface.png)\n"
        assert result.failed == 0
        assert [p.name for p in result.saved] == ["getting-started-interface.png"]
        assert (tmp_path / "images" / "getting-started-interface.png").read_bytes() == PNG

    def test_result_fields(self, tmp_path):
        names = [f.name for f in dataclasses.fields(LocalizeResult)]
        assert names == ["markdown", "saved", "failed"]
        result = localize_images("no refs here", tmp_path / "images")
        assert result == LocalizeResult(markdown="no refs here", saved=[], failed=0)

    def test_no_refs_is_noop_without_fetcher_or_dir(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("Fetcher constructed for a no-op input")

        monkeypatch.setattr(images_mod, "Fetcher", boom)
        images_dir = tmp_path / "images"
        md = "plain prose, https://example.com/docs/page in text"
        result = localize_images(md, images_dir)
        assert result == LocalizeResult(markdown=md, saved=[], failed=0)
        assert not images_dir.exists()

    def test_extensionless_url_sniffs_png(self, tmp_path):
        fetcher = FakeFetcher()
        md = "![chart](https://cdn.example.com/assets/v2/chart0042)"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.markdown == "![chart](images/v2-chart0042.png)"
        assert (tmp_path / "images" / "v2-chart0042.png").read_bytes() == PNG

    def test_extensionless_url_sniffs_jpg(self, tmp_path):
        fetcher = FakeFetcher(default=JPG)
        md = "![photo](https://cdn.example.com/assets/v2/photo0043)"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.markdown == "![photo](images/v2-photo0043.jpg)"
        assert (tmp_path / "images" / "v2-photo0043.jpg").read_bytes() == JPG

    def test_reuse_existing_skips_fetch(self, tmp_path):
        images_dir = tmp_path / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "getting-started-interface.png").write_bytes(b"already here")
        fetcher = FakeFetcher()
        md = "![ui](https://example.com/images/getting-started/interface.png)"
        result = localize_images(md, images_dir, fetcher=fetcher)
        assert fetcher.calls == []
        assert result.markdown == "![ui](images/getting-started-interface.png)"
        assert result.saved == [images_dir / "getting-started-interface.png"]
        assert (images_dir / "getting-started-interface.png").read_bytes() == b"already here"

    def test_reuse_probes_extensions_for_extensionless_url(self, tmp_path):
        images_dir = tmp_path / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "v2-chart0042.jpg").write_bytes(b"prior sniffed jpg")
        fetcher = FakeFetcher()
        md = "![chart](https://cdn.example.com/assets/v2/chart0042)"
        result = localize_images(md, images_dir, fetcher=fetcher)
        assert fetcher.calls == []
        assert result.markdown == "![chart](images/v2-chart0042.jpg)"

    def test_reuse_false_skips_probe_and_suffixes(self, tmp_path):
        images_dir = tmp_path / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "assets-pic.png").write_bytes(b"prior run file")
        fetcher = FakeFetcher()
        md = "![p](https://example.com/assets/pic.png)"
        result = localize_images(md, images_dir, fetcher=fetcher, reuse_existing=False)
        assert fetcher.calls == ["https://example.com/assets/pic.png"]
        assert result.markdown == "![p](images/assets-pic-2.png)"
        assert (images_dir / "assets-pic.png").read_bytes() == b"prior run file"
        assert (images_dir / "assets-pic-2.png").read_bytes() == PNG

    def test_reuse_false_collision_suffix_within_run(self, tmp_path):
        first = "https://one.example.com/assets/pic.png"
        second = "https://two.example.com/assets/pic.png"
        fetcher = FakeFetcher(responses={first: PNG, second: JPG})
        md = f"![a]({first})\n![b]({second})\n"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher, reuse_existing=False)
        assert result.markdown == "![a](images/assets-pic.png)\n![b](images/assets-pic-2.png)\n"
        assert (tmp_path / "images" / "assets-pic.png").read_bytes() == PNG
        assert (tmp_path / "images" / "assets-pic-2.png").read_bytes() == JPG

    @pytest.mark.parametrize(
        "exc",
        [
            _http_error(404),
            urllib.error.URLError("connection reset"),
            OSError("disk full"),
            InvalidInputError("URL host is not public"),
        ],
    )
    def test_failure_keeps_ref_and_counts(self, tmp_path, exc):
        url = "https://example.com/images/getting-started/interface.png"
        fetcher = FakeFetcher(responses={url: exc})
        md = f"![ui]({url})"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.markdown == md
        assert result.failed == 1
        assert result.saved == []

    def test_size_cap_client_error_counts_as_failed(self, tmp_path):
        url = "https://example.com/images/a/huge.png"
        exc = ClientError("response exceeded max_bytes", context={"url": url, "max_bytes": 10})
        fetcher = FakeFetcher(responses={url: exc})
        md = f"![big]({url})"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.markdown == md
        assert result.failed == 1

    def test_partial_failure_localizes_the_rest(self, tmp_path):
        ok = "https://example.com/images/a/one.png"
        bad = "https://example.com/images/b/two.png"
        fetcher = FakeFetcher(responses={bad: _http_error(500)})
        md = f"![one]({ok})\n![two]({bad})\n"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert result.failed == 1
        assert "![one](images/a-one.png)" in result.markdown
        assert f"![two]({bad})" in result.markdown

    def test_non_image_extension_skipped(self, tmp_path):
        fetcher = FakeFetcher()
        md = "![doc](https://example.com/docs/page.html)"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert fetcher.calls == []
        assert result == LocalizeResult(markdown=md, saved=[], failed=0)

    def test_duplicate_ref_fetched_once_all_occurrences_retargeted(self, tmp_path):
        url = "https://example.com/images/a/one.png"
        fetcher = FakeFetcher()
        doc = f'![first]({url})\ntext\n![second]({url})\n<img src="{url}">\n'
        result = localize_images(doc, tmp_path / "images", fetcher=fetcher)
        assert fetcher.calls == [url]
        assert result.markdown == (
            "![first](images/a-one.png)\ntext\n![second](images/a-one.png)\n"
            '<img src="images/a-one.png">\n'
        )
        assert len(result.saved) == 1

    def test_prose_url_untouched_when_ref_rewritten(self, tmp_path):
        url = "https://example.com/images/a/one.png"
        md = f"See {url} for the original.\n![fig]({url})\n"
        result = localize_images(md, tmp_path / "images", fetcher=FakeFetcher())
        assert f"See {url} for the original." in result.markdown
        assert "![fig](images/a-one.png)" in result.markdown

    def test_percent_encoded_name_preserved(self, tmp_path):
        url = "https://example.com/images/user%20guide/setup%20figure.png"
        result = localize_images(f"![fig]({url})", tmp_path / "images", fetcher=FakeFetcher())
        assert result.markdown == "![fig](images/user%20guide-setup%20figure.png)"
        assert (tmp_path / "images" / "user%20guide-setup%20figure.png").exists()

    def test_link_prefix_customizes_rewritten_refs(self, tmp_path):
        md = "![fig](https://example.com/images/a/one.png)"
        result = localize_images(
            md, tmp_path / "assets", fetcher=FakeFetcher(), link_prefix="assets/"
        )
        assert result.markdown == "![fig](assets/a-one.png)"

    def test_custom_namer_used(self, tmp_path):
        def basename_namer(url: str) -> str:
            return url.rsplit("/", 1)[-1]

        md = "![fig](https://example.com/images/deep/nested/one.png)"
        result = localize_images(
            md, tmp_path / "images", fetcher=FakeFetcher(), namer=basename_namer
        )
        assert result.markdown == "![fig](images/one.png)"
        assert (tmp_path / "images" / "one.png").exists()

    def test_default_fetcher_built_once_per_call(self, tmp_path, monkeypatch):
        built: list[FakeFetcher] = []

        def factory(*args, **kwargs):
            fetcher = FakeFetcher()
            built.append(fetcher)
            return fetcher

        monkeypatch.setattr(images_mod, "Fetcher", factory)
        md = (
            "![a](https://example.com/images/a/one.png)\n"
            "![b](https://example.com/images/b/two.png)\n"
        )
        result = localize_images(md, tmp_path / "images")
        assert len(built) == 1
        assert len(built[0].calls) == 2
        assert result.failed == 0


class TestRelativeRefs:
    def test_resolved_against_base_url_with_parent_climb(self, tmp_path):
        fetcher = FakeFetcher()
        md = "![fig](../assets/climb-figure.png)"
        result = localize_images(
            md,
            tmp_path / "images",
            base_url="https://example.com/docs/guide/page.html",
            fetcher=fetcher,
        )
        assert fetcher.calls == ["https://example.com/docs/assets/climb-figure.png"]
        assert result.markdown == "![fig](images/assets-climb-figure.png)"

    def test_simple_relative_ref_resolved(self, tmp_path):
        fetcher = FakeFetcher()
        md = "![fig](assets/inline-figure.png)"
        result = localize_images(
            md, tmp_path / "images", base_url="https://example.com/docs/guide/", fetcher=fetcher
        )
        assert fetcher.calls == ["https://example.com/docs/guide/assets/inline-figure.png"]
        assert result.markdown == "![fig](images/assets-inline-figure.png)"

    def test_untouched_without_base_url(self, tmp_path):
        fetcher = FakeFetcher()
        md = "![fig](../assets/climb-figure.png)"
        result = localize_images(md, tmp_path / "images", fetcher=fetcher)
        assert fetcher.calls == []
        assert result == LocalizeResult(markdown=md, saved=[], failed=0)

    @pytest.mark.parametrize(
        "ref",
        [
            "images/already-local.png",
            "/absolute/path/figure.png",
            "data:image/png;base64,AAAA",
        ],
    )
    def test_excluded_refs_never_rewritten(self, tmp_path, ref):
        fetcher = FakeFetcher()
        md = f"![fig]({ref})"
        result = localize_images(
            md, tmp_path / "images", base_url="https://example.com/docs/", fetcher=fetcher
        )
        assert fetcher.calls == []
        assert result.markdown == md


class TestHtmlRefs:
    def test_double_quoted_src_rewritten(self, tmp_path):
        doc = '<img src="https://example.com/images/a/one.png" alt="one">'
        result = localize_images(doc, tmp_path / "images", fetcher=FakeFetcher())
        assert result.markdown == '<img src="images/a-one.png" alt="one">'

    def test_single_quoted_src_rewritten(self, tmp_path):
        doc = "<img class='inline' src='https://example.com/images/b/two.png'>"
        result = localize_images(doc, tmp_path / "images", fetcher=FakeFetcher())
        assert result.markdown == "<img class='inline' src='images/b-two.png'>"

    def test_mixed_markdown_and_html_doc(self, tmp_path):
        doc = (
            "![fig](https://example.com/images/a/one.png)\n"
            '<img src="https://example.com/images/b/two.png">\n'
        )
        result = localize_images(doc, tmp_path / "images", fetcher=FakeFetcher())
        assert result.markdown == (
            "![fig](images/a-one.png)\n<img src=\"images/b-two.png\">\n"
        )
        assert sorted(p.name for p in result.saved) == ["a-one.png", "b-two.png"]


class TestCountRemoteImages:
    def test_counts_distinct_localizable_refs(self):
        text = (
            "![a](https://example.com/images/a/one.png)\n"
            "![again](https://example.com/images/a/one.png)\n"
            '<img src="https://example.com/images/b/two.png">\n'
            "![local](images/three.png)\n"
            "![page](https://example.com/docs/page.html)\n"
        )
        assert count_remote_images(text) == 2

    def test_zero_when_fully_localized(self):
        assert count_remote_images("![a](images/one.png) and prose") == 0


class TestLocalizeFile:
    def test_localizes_and_returns_saved_count(self, tmp_path):
        doc = tmp_path / "guide.md"
        doc.write_text(
            "![a](https://example.com/images/a/one.png)\n"
            '<img src="https://example.com/images/b/two.png">\n',
            encoding="utf-8",
        )
        saved = localize_file(doc, tmp_path / "images", fetcher=FakeFetcher())
        assert saved == 2
        text = doc.read_text(encoding="utf-8")
        assert count_remote_images(text) == 0
        assert "![a](images/a-one.png)" in text
        assert '<img src="images/b-two.png">' in text

    def test_checkpoint_cadence(self, tmp_path, monkeypatch):
        writes: list[str] = []
        real = images_mod.atomic_write_text

        def recording(path, content, **kwargs):
            writes.append(content)
            real(path, content, **kwargs)

        monkeypatch.setattr(images_mod, "atomic_write_text", recording)
        doc = tmp_path / "guide.md"
        doc.write_text(
            "\n".join(f"![f{i}](https://example.com/images/a/fig-{i}.png)" for i in range(5)),
            encoding="utf-8",
        )
        saved = localize_file(doc, tmp_path / "images", checkpoint_every=2, fetcher=FakeFetcher())
        assert saved == 5
        assert [count_remote_images(w) for w in writes] == [3, 1, 0]
        assert count_remote_images(doc.read_text(encoding="utf-8")) == 0

    def test_resume_converges_on_second_run(self, tmp_path):
        ok = "https://example.com/images/a/one.png"
        flaky = "https://example.com/images/b/two.png"
        doc = tmp_path / "guide.md"
        doc.write_text(f"![a]({ok})\n![b]({flaky})\n", encoding="utf-8")

        first = localize_file(
            doc, tmp_path / "images", fetcher=FakeFetcher(responses={flaky: _http_error(503)})
        )
        assert first == 1
        assert count_remote_images(doc.read_text(encoding="utf-8")) == 1

        second_fetcher = FakeFetcher()
        second = localize_file(doc, tmp_path / "images", fetcher=second_fetcher)
        assert second == 1
        assert second_fetcher.calls == [flaky]
        assert count_remote_images(doc.read_text(encoding="utf-8")) == 0

    def test_resume_used_seeding_prevents_clobber(self, tmp_path):
        first_url = "https://one.example.com/assets/pic.png"
        second_url = "https://two.example.com/assets/pic.png"
        doc = tmp_path / "guide.md"
        doc.write_text(f"![a]({first_url})\n![b]({second_url})\n", encoding="utf-8")
        images_dir = tmp_path / "images"

        localize_file(
            doc,
            images_dir,
            fetcher=FakeFetcher(responses={first_url: PNG, second_url: _http_error(500)}),
        )
        assert (images_dir / "assets-pic.png").read_bytes() == PNG

        localize_file(doc, images_dir, fetcher=FakeFetcher(responses={second_url: JPG}))
        assert (images_dir / "assets-pic.png").read_bytes() == PNG
        assert (images_dir / "assets-pic-2.png").read_bytes() == JPG
        text = doc.read_text(encoding="utf-8")
        assert "![a](images/assets-pic.png)" in text
        assert "![b](images/assets-pic-2.png)" in text

    def test_no_remote_refs_returns_zero_without_writes(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("Fetcher constructed for a no-op input")

        monkeypatch.setattr(images_mod, "Fetcher", boom)
        doc = tmp_path / "guide.md"
        original = "![local](images/one.png)\nprose\n"
        doc.write_text(original, encoding="utf-8")
        images_dir = tmp_path / "images"
        assert localize_file(doc, images_dir) == 0
        assert doc.read_text(encoding="utf-8") == original
        assert not images_dir.exists()
