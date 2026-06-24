"""Tests for pf_core.utils.vocab."""

from __future__ import annotations

import pytest

from pf_core.utils.vocab import SlugNormalizer


@pytest.fixture
def basic_normalizer() -> SlugNormalizer:
    """A small normalizer covering the common shapes a real project would
    use: pass-through, synonym, and explicit-reject."""
    return SlugNormalizer(
        canonical_slugs={"article", "memo", "post", "press_release", "firing"},
        synonyms={
            "blog article": "article",
            "blog articles": "article",
            "social media post": "post",
            "press statement": "press_release",
            "release": "press_release",
            "termination": "firing",
        },
        explicit_rejects={
            "public statement",
            "news report",
            "market reaction",
        },
    )


class TestCanonicalPassThrough:
    """Inputs already in the canonical set return the slug unchanged."""

    def test_known_slug(self, basic_normalizer):
        assert basic_normalizer.normalize("article") == "article"
        assert basic_normalizer.normalize("memo") == "memo"
        assert basic_normalizer.normalize("press_release") == "press_release"

    def test_case_insensitive(self, basic_normalizer):
        assert basic_normalizer.normalize("ARTICLE") == "article"
        assert basic_normalizer.normalize("Memo") == "memo"
        assert basic_normalizer.normalize("Press_Release") == "press_release"

    def test_whitespace_tolerant(self, basic_normalizer):
        assert basic_normalizer.normalize("  article  ") == "article"
        assert basic_normalizer.normalize("article\n") == "article"
        assert basic_normalizer.normalize("\tmemo\t") == "memo"


class TestSynonymLookup:
    """Free-text variants map to the canonical slug they're aliased to."""

    def test_basic_synonym(self, basic_normalizer):
        assert basic_normalizer.normalize("blog article") == "article"
        assert basic_normalizer.normalize("social media post") == "post"
        assert basic_normalizer.normalize("press statement") == "press_release"
        assert basic_normalizer.normalize("termination") == "firing"

    def test_synonym_case_insensitive(self, basic_normalizer):
        assert basic_normalizer.normalize("Blog Article") == "article"
        assert basic_normalizer.normalize("BLOG ARTICLE") == "article"
        assert basic_normalizer.normalize("Social Media Post") == "post"

    def test_synonym_whitespace_tolerant(self, basic_normalizer):
        assert basic_normalizer.normalize("  blog   article  ") == "article"
        assert basic_normalizer.normalize("press\tstatement") == "press_release"

    def test_underscore_variant_of_canonical(self, basic_normalizer):
        """``press release`` (with space) auto-converts to underscored slug."""
        assert basic_normalizer.normalize("press release") == "press_release"


class TestExplicitReject:
    """Categories in the reject set return None and are reported as
    explicit (not just unknown)."""

    def test_reject_returns_none(self, basic_normalizer):
        assert basic_normalizer.normalize("public statement") is None
        assert basic_normalizer.normalize("news report") is None
        assert basic_normalizer.normalize("market reaction") is None

    def test_reject_case_insensitive(self, basic_normalizer):
        assert basic_normalizer.normalize("Public Statement") is None
        assert basic_normalizer.normalize("NEWS REPORT") is None

    def test_is_explicit_reject_true(self, basic_normalizer):
        assert basic_normalizer.is_explicit_reject("public statement") is True
        assert basic_normalizer.is_explicit_reject("Public Statement") is True
        assert basic_normalizer.is_explicit_reject("  market reaction  ") is True

    def test_is_explicit_reject_false_for_canonical(self, basic_normalizer):
        assert basic_normalizer.is_explicit_reject("article") is False
        assert basic_normalizer.is_explicit_reject("press_release") is False

    def test_is_explicit_reject_false_for_synonym(self, basic_normalizer):
        """Synonyms are NOT rejects — they map to a canonical slug."""
        assert basic_normalizer.is_explicit_reject("blog article") is False

    def test_is_explicit_reject_false_for_unknown(self, basic_normalizer):
        """An unknown free-text string is not the same as a deliberate reject."""
        assert basic_normalizer.is_explicit_reject("kerfuffle") is False


class TestUnknownInput:
    """Unknown free-text returns None but is NOT an explicit reject."""

    def test_unknown_string(self, basic_normalizer):
        assert basic_normalizer.normalize("kerfuffle") is None
        assert basic_normalizer.normalize("twitter feud") is None
        assert basic_normalizer.normalize("random category") is None


class TestEmptyInput:
    """Empty / None / whitespace inputs return None."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t"])
    def test_returns_none(self, basic_normalizer, raw):
        assert basic_normalizer.normalize(raw) is None

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_explicit_reject_false_on_empty(self, basic_normalizer, raw):
        assert basic_normalizer.is_explicit_reject(raw) is False


class TestPrecedenceRules:
    """When values conflict between sets, the documented precedence holds."""

    def test_canonical_beats_reject_collision(self):
        """If a slug somehow appears in both ``canonical_slugs`` and
        ``explicit_rejects`` (a project-config bug), canonical wins
        because the canonical-pass-through branch runs first."""
        n = SlugNormalizer(
            canonical_slugs={"weird"},
            explicit_rejects={"weird"},
        )
        assert n.normalize("weird") == "weird"
        # The reject set still reports True here — that's a config-bug
        # signal worth flagging but not a runtime crash.
        assert n.is_explicit_reject("weird") is True

    def test_reject_beats_synonym_collision(self):
        """If a value is in both ``synonyms`` and ``explicit_rejects``,
        the reject wins so the project's "drop this" intent is honored."""
        n = SlugNormalizer(
            canonical_slugs={"x"},
            synonyms={"both": "x"},
            explicit_rejects={"both"},
        )
        assert n.normalize("both") is None
        assert n.is_explicit_reject("both") is True


class TestSynonymsCanonicalIntegrity:
    """Synonym values pointing at slugs not in the canonical set are
    returned anyway — the class trusts the caller to keep config in sync.
    This is a deliberate non-feature we lock in to avoid surprising
    behavior changes down the line."""

    def test_synonym_to_unknown_slug_passes_through(self):
        n = SlugNormalizer(
            canonical_slugs={"a"},
            synonyms={"alt": "b"},  # "b" not in canonical set
        )
        assert n.normalize("alt") == "b"


class TestEmptyConfig:
    """An empty SlugNormalizer is well-defined: everything returns None."""

    def test_empty_normalizer(self):
        n = SlugNormalizer(canonical_slugs=set())
        assert n.normalize("anything") is None
        assert n.normalize("") is None
        assert n.is_explicit_reject("anything") is False

    def test_only_canonical_no_synonyms_no_rejects(self):
        n = SlugNormalizer(canonical_slugs={"a", "b"})
        assert n.normalize("a") == "a"
        assert n.normalize("c") is None
        assert n.is_explicit_reject("c") is False
