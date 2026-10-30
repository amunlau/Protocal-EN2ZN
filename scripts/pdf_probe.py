#!/usr/bin/env python3
"""Checkpointed PDF text extraction; tables/OCR/reading order require separate review."""
import argparse
import json
from pathlib import Path
import sys
from check_environment import configure_stdio
from translation_project import atomic_json, digest, file_hash, need, now, read_json

VERSION = 2


def probe(source, output, start=1, end=None, metadata_only=False):
    from pypdf import PdfReader
    import pypdf
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_hash = file_hash(source)
    engine = {"adapter": VERSION, "pypdf": pypdf.__version__}
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        need(isinstance(manifest, dict) and manifest.get("source_sha256") == source_hash and manifest.get("engine") == engine, "缓存属于不同源文件或提取器版本；请使用新的缓存目录")
    else:
        need(not any(output.iterdir()), "输出目录已有其他文件，请使用新的缓存目录")
    cached, extracted = 0, 0
    with source.open("rb") as f:
        reader = PdfReader(f)
        need(not reader.is_encrypted, "PDF 加密，需可读取副本")
        count = len(reader.pages)
        last = count if end is None else end
        need(1 <= start <= last <= count, "页范围越界")
        atomic_json(manifest_path, {"source": str(source), "source_sha256": source_hash, "engine": engine, "page_count": count, "sample_required": count >= 20, "updated_at": now(), "warning": "未经水印/页眉/页脚分离的原始文本，禁止直接作为翻译正文；先执行布局识别和对象核验"})
        if metadata_only:
            return read_json(manifest_path)
        for index in range(start - 1, last):
            path = output / "pages" / (f"{index + 1:06d}.json")
            if path.exists():
                try:
                    saved = read_json(path)
                    valid = (isinstance(saved, dict) and saved["source_sha256"] == source_hash and saved["pdf_page"] == index + 1 and saved.get("engine") == engine and isinstance(saved["text"], str) and saved["text_sha256"] == digest(saved["text"]) and saved["status"] in ("text_extracted", "needs_visual_or_ocr"))
                except (ValueError, KeyError, TypeError):
                    valid = False
                if valid:
                    cached += 1
                    continue
            try:
                text = reader.pages[index].extract_text() or ""
                record = {"status": "text_extracted" if text.strip() else "needs_visual_or_ocr", "text": text}
            except Exception as exc:
                record = {"status": "error", "text": "", "error": str(exc)}
            record.update(pdf_page=index + 1, source_sha256=source_hash, engine=engine, text_sha256=digest(record["text"]), at=now(), furniture_review_required=True)
            atomic_json(path, record)
            extracted += 1
    return {"pages": count, "range": [start, last], "extracted": extracted, "reused": cached, "output": str(output), "note": "这是未清洗的原始文本；先识别水印、页眉、页脚，再建翻译正文。空文本页必须检查图像或执行 OCR。"}


if __name__ == "__main__":
    configure_stdio()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source")
    p.add_argument("--output", required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int)
    p.add_argument("--metadata-only", action="store_true")
    a = p.parse_args()
    try:
        print(json.dumps(probe(a.source, a.output, a.start, a.end, a.metadata_only), ensure_ascii=False, indent=2))
    except (ImportError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
