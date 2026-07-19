"""llms.txt drift gate: the root AI-discovery index must list every shipped doc."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src" / "pf_core" / "docs"


def test_llms_txt_lists_every_shipped_doc():
    text = (ROOT / "llms.txt").read_text()
    missing = [
        str(p.relative_to(ROOT))
        for p in sorted(DOCS.rglob("*.md"))
        if f"/src/pf_core/docs/{p.relative_to(DOCS)}" not in text
    ]
    assert not missing, "docs missing from llms.txt:\n" + "\n".join(missing)


def test_llms_txt_links_are_absolute():
    for line in (ROOT / "llms.txt").read_text().splitlines():
        if "](" in line:
            url = line.split("](", 1)[1].split(")", 1)[0]
            assert url.startswith("https://"), f"non-absolute link: {url}"
