#!/usr/bin/env python3
"""Checkpointed PDF text extraction; tables/OCR/reading order require separate review."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from check_environment import configure_stdio
from translation_project import atomic_json, digest, file_hash, need, now, read_json

VERSION = 4


def page_fingerprint(record):
    """Bind geometry/diagnostics and status, not just the extracted text."""
    return digest({key: value for key, value in record.items() if key != "record_sha256"})


@contextmanager
def source_snapshot(source, expected, manifest_path, signer=None):
    """Invalidate mixed-source caches even when a parser raises mid-extraction."""
    try:
        yield
    finally:
        try:
            unchanged = file_hash(source) == expected
        except OSError:
            unchanged = False
        if not unchanged:
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                manifest.pop("sha256", None)
                manifest.update(source_invalidated=True, updated_at=now())
                atomic_json(manifest_path, signer(manifest) if signer else manifest)
            raise ValueError("提取期间源 PDF 发生变化或不可读；缓存已失效，请固定源文件后使用新的缓存目录")


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
        need(isinstance(manifest, dict), "缓存清单必须是对象；请使用新的缓存目录")
        need(not manifest.get("source_invalidated"), "上次提取期间源文件变化，缓存已失效；请使用新的缓存目录")
        need(isinstance(manifest, dict) and manifest.get("source_sha256") == source_hash and manifest.get("engine") == engine, "缓存属于不同源文件或提取器版本；请使用新的缓存目录")
    else:
        need(not any(output.iterdir()), "输出目录已有其他文件，请使用新的缓存目录")
    cached, extracted = 0, 0
    failed, attention = [], []
    with source_snapshot(source, source_hash, manifest_path), source.open("rb") as f:
        reader = PdfReader(f)
        need(not reader.is_encrypted, "PDF 加密，需可读取副本")
        count = len(reader.pages)
        last = count if end is None else end
        need(1 <= start <= last <= count, "页范围越界")
        catalog = reader.trailer["/Root"]
        atomic_json(manifest_path, {"source": str(source), "source_sha256": source_hash, "engine": engine, "page_count": count,
                    "structure_tree_present": "/StructTreeRoot" in catalog,
                    "sample_required": count >= 20, "updated_at": now(), "warning": "未经水印/页眉/页脚分离的原始文本，禁止直接作为翻译正文；结构树存在不证明标签或阅读顺序正确，局部 MCID 仍需独立核查"})
        for index in ([] if metadata_only else range(start - 1, last)):
            path = output / "pages" / (f"{index + 1:06d}.json")
            if path.exists():
                try:
                    saved = read_json(path)
                    valid = (isinstance(saved, dict) and saved.get("record_sha256") == page_fingerprint(saved) and saved["source_sha256"] == source_hash and saved["pdf_page"] == index + 1 and saved.get("engine") == engine and isinstance(saved["text"], str) and saved["text_sha256"] == digest(saved["text"]) and saved["status"] in ("text_extracted", "needs_visual_or_ocr"))
                except (ValueError, KeyError, TypeError):
                    valid = False
                if valid:
                    cached += 1
                    if saved["status"] == "needs_visual_or_ocr":
                        attention.append(index + 1)
                    continue
            try:
                page = reader.pages[index]
                text = page.extract_text() or ""
                diagnostics = {"replacement_characters": text.count("\ufffd"),
                               "null_characters": text.count("\x00"),
                               "unmapped_cid_markers": text.count("(cid:")}
                suspect = any(diagnostics.values())
                record = {"status": "text_extracted" if text.strip() and not suspect else "needs_visual_or_ocr", "text": text,
                          "text_diagnostics": diagnostics, "rotation": page.rotation,
                          "media_box": [float(v) for v in page.mediabox], "crop_box": [float(v) for v in page.cropbox],
                          "struct_parents": page.get("/StructParents")}
            except Exception as exc:
                record = {"status": "error", "text": "", "error": str(exc)}
            record.update(pdf_page=index + 1, source_sha256=source_hash, engine=engine, text_sha256=digest(record["text"]), at=now(), furniture_review_required=True)
            record["record_sha256"] = page_fingerprint(record)
            atomic_json(path, record)
            extracted += 1
            if record["status"] == "error":
                failed.append(index + 1)
            elif record["status"] == "needs_visual_or_ocr":
                attention.append(index + 1)
    if metadata_only:
        return read_json(manifest_path)
    return {"pages": count, "range": [start, last], "extracted": extracted, "reused": cached,
            "extraction_complete": not failed, "failed_pages": failed, "needs_visual_or_ocr_pages": attention,
            "output": str(output), "note": "完成仅指请求范围的提取操作，不代表原文可靠或正文完整。先核验水印、页眉、页脚；空文本和异常字符页须检查图像或补 OCR。失败页重跑时自动重试。"}


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
        result = probe(a.source, a.output, a.start, a.end, a.metadata_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("failed_pages"):
            sys.exit(2)
    except (ImportError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
