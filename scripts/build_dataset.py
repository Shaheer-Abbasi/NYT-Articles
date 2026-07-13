#!/usr/bin/env python3
"""Build NYT article manifest and chunked dataset files."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA_DIR = ROOT / "metadata"
PERMALINK_DIR = ROOT / "permalinks"
TEXT_DIR = ROOT / "txts"
OUT_DIR = ROOT / "processed"

MIN_ARTICLE_WORDS = 300
MIN_CHUNK_WORDS = 80
TARGET_CHUNK_WORDS = 140
MAX_CHUNK_WORDS = 240


def parse_ris(path: Path) -> dict:
    fields: dict[str, list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if len(raw_line) < 6 or raw_line[2:6] != "  - ":
            continue
        tag = raw_line[:2]
        value = raw_line[6:].strip()
        fields.setdefault(tag, []).append(value)

    return {
        "authors": fields.get("AU", []),
        "date": fields.get("DA", [""])[0].strip("/").replace("/", "-"),
        "year": fields.get("PY", [""])[0].strip("/"),
        "title": fields.get("TI", [""])[0],
        "publication": fields.get("T2", [""])[0],
        "page": fields.get("SP", [""])[0],
        "database": fields.get("DB", [""])[0],
        "provider": fields.get("DP", [""])[0],
        "ris_url": fields.get("UR", [""])[0],
        "keywords": fields.get("KW", []),
        "language": fields.get("LA", [""])[0],
        "material_type": fields.get("M3", [""])[0],
    }


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)

    stop_patterns = [
        r"\nCAPTION\(S\):.*",
        r"\nCopyright:.*",
        r"\nhttp://www\.nytimes\.com.*",
        r"\nhttps://www\.nytimes\.com/.*",
    ]
    for pattern in stop_patterns:
        text = re.sub(pattern, "", text, flags=re.DOTALL | re.IGNORECASE)

    paragraphs = []
    last = None
    for para in text.split("\n\n"):
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        if para == last:
            continue
        paragraphs.append(para)
        last = para
    return "\n\n".join(paragraphs).strip()


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def chunk_article(article_id: str, text: str) -> list[dict]:
    chunks = []
    current: list[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current_words >= MIN_CHUNK_WORDS:
            chunk_index = len(chunks) + 1
            chunk_text = "\n\n".join(current).strip()
            chunks.append(
                {
                    "chunk_id": f"{article_id}_chunk_{chunk_index:03d}",
                    "article_id": article_id,
                    "chunk_index": chunk_index,
                    "word_count": word_count(chunk_text),
                    "text": chunk_text,
                }
            )
        current = []
        current_words = 0

    for para in split_paragraphs(text):
        para_words = word_count(para)
        if para_words > MAX_CHUNK_WORDS:
            flush()
            words = para.split()
            for start in range(0, len(words), TARGET_CHUNK_WORDS):
                part = " ".join(words[start : start + TARGET_CHUNK_WORDS])
                if word_count(part) >= MIN_CHUNK_WORDS:
                    chunk_index = len(chunks) + 1
                    chunks.append(
                        {
                            "chunk_id": f"{article_id}_chunk_{chunk_index:03d}",
                            "article_id": article_id,
                            "chunk_index": chunk_index,
                            "word_count": word_count(part),
                            "text": part,
                        }
                    )
            continue

        if current_words and current_words + para_words > MAX_CHUNK_WORDS:
            flush()
        current.append(para)
        current_words += para_words
        if current_words >= TARGET_CHUNK_WORDS:
            flush()

    flush()
    return chunks


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    articles = []
    chunks = []
    manifest_rows = []

    for ris_path in sorted(METADATA_DIR.glob("*.ris")):
        article_id = ris_path.stem
        txt_path = TEXT_DIR / f"{article_id}.txt"
        permalink_path = PERMALINK_DIR / f"{article_id}.txt"

        meta = parse_ris(ris_path)
        raw_text = txt_path.read_text(encoding="utf-8", errors="ignore") if txt_path.exists() else ""
        text = clean_text(raw_text)
        wc = word_count(text)

        exclude_reason = ""
        if wc < MIN_ARTICLE_WORDS:
            exclude_reason = "short_or_non_article"
        if meta["title"].lower().startswith("share your story"):
            exclude_reason = "reader_callout"

        article = {
            "article_id": article_id,
            "title": meta["title"],
            "authors": meta["authors"],
            "date": meta["date"],
            "year": meta["year"],
            "publication": meta["publication"],
            "page": meta["page"],
            "source_database": meta["database"],
            "provider": meta["provider"],
            "ris_url": meta["ris_url"],
            "permalink": permalink_path.read_text(encoding="utf-8", errors="ignore").strip()
            if permalink_path.exists()
            else "",
            "keywords": meta["keywords"],
            "language": meta["language"],
            "material_type": meta["material_type"],
            "word_count": wc,
            "included_by_default": not bool(exclude_reason),
            "exclude_reason": exclude_reason,
            "text_path": str(txt_path.relative_to(ROOT)),
            "metadata_path": str(ris_path.relative_to(ROOT)),
            "permalink_path": str(permalink_path.relative_to(ROOT)) if permalink_path.exists() else "",
            "text": text,
        }
        articles.append(article)

        article_chunks = []
        if article["included_by_default"]:
            article_chunks = chunk_article(article_id, text)
            for chunk in article_chunks:
                chunk.update(
                    {
                        "title": article["title"],
                        "authors": article["authors"],
                        "date": article["date"],
                        "publication": article["publication"],
                        "source_database": article["source_database"],
                        "permalink": article["permalink"],
                    }
                )
            chunks.extend(article_chunks)

        manifest_rows.append(
            {
                "article_id": article_id,
                "title": article["title"],
                "authors": "; ".join(article["authors"]),
                "date": article["date"],
                "publication": article["publication"],
                "page": article["page"],
                "word_count": wc,
                "num_chunks": len(article_chunks),
                "included_by_default": article["included_by_default"],
                "exclude_reason": exclude_reason,
                "text_path": article["text_path"],
                "metadata_path": article["metadata_path"],
                "permalink_path": article["permalink_path"],
                "ris_url": article["ris_url"],
            }
        )

    with (OUT_DIR / "manifest.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        writer.writeheader()
        writer.writerows(manifest_rows)

    write_jsonl(OUT_DIR / "articles.jsonl", articles)
    write_jsonl(OUT_DIR / "chunks.jsonl", chunks)

    print(f"Wrote {OUT_DIR / 'manifest.csv'}")
    print(f"Wrote {OUT_DIR / 'articles.jsonl'} ({len(articles)} articles)")
    print(f"Wrote {OUT_DIR / 'chunks.jsonl'} ({len(chunks)} chunks)")


if __name__ == "__main__":
    main()
