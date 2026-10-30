#!/usr/bin/env python3
"""Reviewable text-layer header/footer/watermark candidates. Never edits a PDF."""
import argparse
from collections import defaultdict
import math
from pathlib import Path
import re
from statistics import median
import sys
from check_environment import configure_stdio
import json

from translation_project import atomic_json, digest, file_hash, need, read_json

VERSION = 2
EXCLUSION_CLASSES = {"watermark", "header", "footer", "page_number"}
PAGE_NUMBER = re.compile(r"^(?:page\s+)?(?:\d+(?:[-–]\d+)?|[ivxlcdm]+)(?:\s*(?:of|/)\s*(?:\d+|[ivxlcdm]+))?$", re.I)


def fingerprint(record):
    return digest({k: v for k, v in record.items() if k != "sha256"})


def signed(record):
    return dict(record, sha256=fingerprint(record))


def read_signed(path):
    value = read_json(path)
    need(isinstance(value, dict), "缓存或报告必须是对象：" + str(path))
    need(value.get("sha256") == fingerprint(value), "缓存或报告哈希不匹配：" + str(path))
    return value


def verify_manifest_source(manifest):
    source = Path(manifest["source"])
    need(source.is_file(), "源 PDF 不在记录位置；对相同源文件重新 extract 以更新位置")
    need(file_hash(source) == manifest["source_sha256"], "源 PDF 已变化，禁止套用旧布局和排除决定")


def char_features(char):
    matrix = char.get("matrix") or (1, 0, 0, 1, char["x0"], char["y0"])
    a, b, _, _, x, y = map(float, matrix)
    angle = math.degrees(math.atan2(b, a))
    rad = math.radians(angle)
    # Projection of text origin perpendicular to baseline; handles angled stamps.
    baseline = -x * math.sin(rad) + y * math.cos(rad)
    height = max(1.0, float(char.get("size", 10)))
    return angle, baseline, height, x, y


def make_spans(chars, page_number):
    """Conservative stream runs, not a semantic reading-order reconstruction."""
    groups, current = [], []
    previous = None
    for char in chars:
        angle, baseline, height, x, y = char_features(char)
        if previous is not None:
            prev, pa, pb, ph, px, py = previous
            separated = abs(angle) < 1 and float(char["x0"]) - float(prev["x1"]) > max(height, ph) * 0.25
            compatible = (abs(angle - pa) < 1 and abs(baseline - pb) < 2 and
                          (abs(angle) > 1 or abs(height - ph) < 0.1) and not separated and
                          char.get("fontname") == prev.get("fontname") and
                          char.get("tag") == prev.get("tag") and
                          char.get("mcid") == prev.get("mcid") and
                          char.get("non_stroking_color") == prev.get("non_stroking_color") and
                          math.hypot(x - px, y - py) < max(height, ph) * 4)
            if not compatible:
                groups.append(current)
                current = []
        current.append(char)
        previous = (char, angle, baseline, height, x, y)
    if current:
        groups.append(current)
    spans = []
    for group in groups:
        text = "".join(c["text"] for c in group).strip()
        if not text:
            continue
        angles = [char_features(c)[0] for c in group]
        spans.append({"id": f"p{page_number:06d}-s{len(spans) + 1:05d}", "text": text,
                      "bbox": [min(c["x0"] for c in group), min(c["top"] for c in group), max(c["x1"] for c in group), max(c["bottom"] for c in group)],
                      "angle": median(angles), "size": median(float(c["size"]) for c in group),
                      "font": group[0].get("fontname"), "color": group[0].get("non_stroking_color"),
                      "tag": group[0].get("tag"), "mcids": sorted({c["mcid"] for c in group if c.get("mcid") is not None})})
    return spans


def extract(source, cache, start=1, end=None):
    import pdfplumber
    source, cache = Path(source).resolve(), Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    hashed = file_hash(source)
    manifest_path = cache / "manifest.json"
    engine = {"adapter": VERSION, "pdfplumber": pdfplumber.__version__}
    if manifest_path.exists():
        old = read_signed(manifest_path)
        need(old["source_sha256"] == hashed and old["engine"] == engine, "源文件或提取器版本改变，请使用新的 layout-cache 目录")
    else:
        need(not any(cache.iterdir()), "布局缓存目录已有其他文件，请使用新目录")
    reused = extracted = 0
    with pdfplumber.open(source) as document:
        total = len(document.pages)
        last = total if end is None else end
        need(1 <= start <= last <= total, "页范围越界")
        atomic_json(manifest_path, signed({"source": str(source), "source_sha256": hashed, "page_count": total, "engine": engine,
                    "limitations": ["文字流片段，不保证语义阅读顺序", "不读取透明度和 Artifact 子类型", "不识别图片/扫描水印；需图像核验", "tag/mcid 属实验性提取字段，不能单独作为删除依据"]}))
        for index in range(start - 1, last):
            page = document.pages[index]
            path = cache / "pages" / f"{index + 1:06d}.json"
            if path.exists():
                try:
                    old = read_signed(path)
                    if old["source_sha256"] == hashed and old["pdf_page"] == index + 1 and old["engine"] == engine:
                        reused += 1
                        page.close()
                        continue
                except (ValueError, KeyError, TypeError):
                    pass
            record = {"source_sha256": hashed, "engine": engine, "pdf_page": index + 1, "width": float(page.width), "height": float(page.height),
                      "rotation": page.rotation, "spans": make_spans(page.chars, index + 1),
                      "image_count": len(page.images), "vector_count": len(page.curves) + len(page.rects) + len(page.lines)}
            record["visual_review_required"] = not record["spans"] or record["image_count"] > 0 or record["vector_count"] > 0
            atomic_json(path, signed(record))
            extracted += 1
            page.close()
    return {"cache": str(cache), "pages": total, "range": [start, last], "extracted": extracted, "reused": reused}


def normalize_text(text):
    value = re.sub(r"\s+", " ", text).strip().casefold()
    # Normalize only a whole page-number token; never erase numbers in clauses.
    return "<page-number>" if PAGE_NUMBER.fullmatch(value) else value


def candidate_info(page, span, margin=0.12, typical=None):
    width, height = page["width"], page["height"]
    x0, y0, x1, y1 = span["bbox"]
    edge = "top" if y1 <= height * margin else "bottom" if y0 >= height * (1 - margin) else None
    angle = ((span.get("angle", 0) + 90) % 180) - 90
    rotated = abs(angle) > 12
    if typical is None:
        typical = median([s.get("size", 10) for s in page["spans"]]) if page["spans"] else 10
    large = span.get("size", 10) > typical * 1.8
    artifact = str(span.get("tag", "")).lstrip("/").lower() == "artifact"
    label = span.get("role", "")  # Optional trusted-adapter input; still reviewed.
    if not (edge or rotated or large or artifact or label in EXCLUSION_CLASSES):
        return None
    evidence = []
    if edge: evidence.append("margin_" + edge)
    if rotated: evidence.append("rotated_text")
    if large: evidence.append("large_text")
    if artifact: evidence.append("artifact_tag")
    if label in EXCLUSION_CLASSES: evidence.append("layout_label:" + label)
    color = span.get("color")
    if isinstance(color, (int, float)) and color > 0.65:
        evidence.append("light_color")
    elif isinstance(color, (list, tuple)) and len(color) in (1, 3) and all(float(c) > 0.65 for c in color):
        evidence.append("light_color")
    possible = "watermark" if (rotated and large) else "header" if edge == "top" else "footer" if edge == "bottom" else "artifact_or_body"
    if edge and PAGE_NUMBER.fullmatch(span["text"].strip()):
        possible = "page_number"
    return {"id": span["id"], "pdf_page": page["pdf_page"], "text": span["text"], "bbox": span["bbox"],
            "possible_class": possible, "evidence": evidence,
            "group_key": [normalize_text(span["text"]), edge, round(((x0 + x1) / 2) / width, 1), round(y0 / height, 2), round(angle / 5), span.get("font"), round(span.get("size", 10)), round(width), round(height)]}


def analyze(cache, report_path, start=1, end=None):
    cache = Path(cache).resolve()
    report_path = Path(report_path).resolve()
    need(not report_path.is_relative_to(cache), "报告不能覆盖布局缓存内的文件")
    manifest = read_signed(cache / "manifest.json")
    verify_manifest_source(manifest)
    need(report_path != Path(manifest["source"]).resolve(), "报告不能覆盖源 PDF")
    last = manifest["page_count"] if end is None else end
    need(1 <= start <= last <= manifest["page_count"], "分析页范围越界")
    candidates, grouped, hashes, visual, counts = [], defaultdict(list), {}, [], {}
    seen_ids = set()
    for page_number in range(start, last + 1):
        path = cache / "pages" / f"{page_number:06d}.json"
        need(path.exists(), f"第 {page_number} 页尚未提取；先续提，或明确指定分析页范围")
        page = read_signed(path)
        need(page["source_sha256"] == manifest["source_sha256"] and page["pdf_page"] == page_number and page["engine"] == manifest["engine"], "页面缓存与源/提取器不匹配")
        ids = [s["id"] for s in page["spans"]]
        need(len(ids) == len(set(ids)), "页面片段 ID 重复")
        need(not seen_ids.intersection(ids), "不同页面使用了相同片段 ID")
        seen_ids.update(ids)
        hashes[str(page_number)] = page["sha256"]
        counts[str(page_number)] = len(ids)
        if page["visual_review_required"]:
            visual.append(page_number)
        typical = median([s.get("size", 10) for s in page["spans"]]) if page["spans"] else 10
        for span in page["spans"]:
            candidate = candidate_info(page, span, typical=typical)
            if candidate:
                key = candidate.pop("group_key")
                candidate["group_id"] = "g-" + digest(key)[:16]
                grouped[candidate["group_id"]].append(candidate)
                candidates.append(candidate)
    groups = []
    for group_id, items in grouped.items():
        pages = sorted({c["pdf_page"] for c in items})
        repeated = len(pages) >= 3
        for item in items:
            if repeated:
                item["evidence"].append("repeated_at_similar_position")
            item["priority"] = "high" if repeated and len(item["evidence"]) >= 2 else "review"
        groups.append({"id": group_id, "example": items[0]["text"], "pages": pages, "possible_class": items[0]["possible_class"],
                       "recommended_check_pages": sorted({pages[0], pages[len(pages) // 2], pages[-1]})})
    report = signed({"version": VERSION, "source_sha256": manifest["source_sha256"], "engine": manifest["engine"], "page_range": [start, last],
                     "page_count": manifest["page_count"], "page_hashes": hashes, "span_counts": counts,
                     "visual_review_required_pages": visual, "groups": groups, "candidates": candidates,
                     "note": "只列候选；位置、重复、字号、旋转、颜色、Artifact 标记均不能单独触发删除。无候选也不证明无水印。"})
    atomic_json(report_path, report)
    return {"report": str(Path(report_path).resolve()), "sha256": report["sha256"], "candidates": len(candidates), "groups": len(groups), "range": [start, last]}


def validate_decision(decision):
    need(isinstance(decision, dict), "每项处置必须是对象")
    need(decision.get("action") in ("keep", "exclude"), "处置 action 必须为 keep 或 exclude")
    need(isinstance(decision.get("reason"), str) and decision["reason"].strip(), "每项处置必须记录理由")
    if decision["action"] == "exclude":
        need(isinstance(decision.get("classification"), str) and decision["classification"] in EXCLUSION_CLASSES, "只允许排除已确认的水印、页眉、页脚或页码")


def apply(cache, report_path, decisions_path, output):
    cache, output = Path(cache).resolve(), Path(output).resolve()
    need(output != cache and cache not in output.parents and output not in cache.parents, "输出必须与布局缓存分开，禁止覆盖源缓存")
    report = read_signed(report_path)
    need(report.get("version") == VERSION, "报告版本已变化，请重新分析与审查")
    first, last = report["page_range"]
    need(set(report["page_hashes"]) == {str(n) for n in range(first, last + 1)}, "报告页范围与页面清单不一致")
    decisions = read_json(decisions_path)
    need(isinstance(decisions, dict), "处置意见必须是对象")
    need(decisions.get("report_sha256") == report["sha256"], "处置意见不属于当前报告")
    need(all(isinstance(decisions.get(k), str) and decisions[k].strip() for k in ("reviewer", "reviewed_at")), "需要记录实际审查者和时间")
    for key in ("groups", "items"):
        need(isinstance(decisions.get(key, []), list) and all(isinstance(d, dict) for d in decisions.get(key, [])), key + " 必须是对象数组")
    # This is the executor's visual/semantic review, not another user-approval gate.
    need(isinstance(decisions.get("page_screening"), str) and decisions["page_screening"].strip(), "记录全页筛查范围、抽查策略及异常页核验；无候选不等于无水印")
    group_map = {g["id"]: g for g in report["groups"]}
    candidates = {c["id"]: c for c in report["candidates"]}
    by_group, by_id = {}, {}
    for decision in decisions.get("groups", []):
        key = decision.get("group_id")
        need(isinstance(key, str) and key in group_map and key not in by_group, "未知或重复分组处置")
        validate_decision(decision)
        verified = decision.get("verified_pages", [])
        need(isinstance(verified, list) and all(type(p) is int for p in verified), "verified_pages 必须是整数页码数组")
        checked = set(verified)
        pages = set(group_map[key]["pages"])
        need(checked <= pages and len(checked) >= min(3, len(pages)), "分组处置需要实际核验至少 3 个代表页；不足 3 页时核验全部")
        by_group[key] = decision
    for decision in decisions.get("items", []):
        key = decision.get("id")
        need(isinstance(key, str) and key in candidates and key not in by_id, "未知或重复片段处置；重新生成含该对象的报告")
        validate_decision(decision)
        need(isinstance(decision.get("visual_check"), str) and decision["visual_check"].strip(), "逐项处置需要实际页图核验记录")
        by_id[key] = decision
    resolved = {}
    for key, item in candidates.items():
        decision = by_id.get(key, by_group.get(item["group_id"]))
        need(decision is not None, "存在未处置候选：" + key)
        resolved[key] = decision
    image_reviews = decisions.get("image_vector_reviews", {})
    need(isinstance(image_reviews, dict), "image_vector_reviews 必须是对象")
    for page_number in report["visual_review_required_pages"]:
        value = image_reviews.get(str(page_number))
        need(isinstance(value, dict) and value.get("disposition") in ("no_watermark", "handled_separately") and isinstance(value.get("evidence"), str) and value["evidence"].strip(), f"第 {page_number} 页有图片/矢量或无文本，须记录额外识别处置，不能当作已自动清理")
    # Validate every input before producing output; source/report changes invalidate decisions.
    manifest = read_signed(cache / "manifest.json")
    verify_manifest_source(manifest)
    need(manifest["source_sha256"] == report["source_sha256"] and manifest["engine"] == report["engine"], "报告属于其他布局缓存")
    for page_number, expected in report["page_hashes"].items():
        page = read_signed(cache / "pages" / f"{int(page_number):06d}.json")
        need(page["sha256"] == expected, "页面提取版本变化，须重新分析与审查")
    signature = digest([report["sha256"], decisions])
    if (output / "manifest.json").exists():
        need(read_signed(output / "manifest.json")["processing_sha256"] == signature, "排除规则已变；用新的输出目录，旧数据保留供回查")
    elif output.exists():
        need(not any(output.iterdir()), "输出目录已有其他文件，请指定新的正文候选目录")
    output.mkdir(parents=True, exist_ok=True)
    # A stale receipt must never survive a failed repair of previously complete output.
    receipt_path = output / "receipt.json"
    if receipt_path.exists():
        receipt_path.unlink()
    atomic_json(output / "manifest.json", signed({"processing_sha256": signature, "source_sha256": report["source_sha256"], "page_range": report["page_range"]}))
    exclusions, outputs = [], {}
    for page_number, source_page_hash in report["page_hashes"].items():
        page = read_signed(cache / "pages" / f"{int(page_number):06d}.json")
        retained, removed = [], []
        for span in page["spans"]:
            decision = resolved.get(span["id"])
            if decision and decision["action"] == "exclude":
                removed.append(span["id"])
                exclusions.append({"id": span["id"], "pdf_page": int(page_number), "text": span["text"], "bbox": span["bbox"], "classification": decision["classification"], "reason": decision["reason"], "source_page_sha256": source_page_hash})
            else:
                retained.append(span)
        need(len(retained) + len(removed) == len(page["spans"]), "保留/排除覆盖不完整")
        record = signed({"pdf_page": int(page_number), "processing_sha256": signature, "source_page_sha256": source_page_hash, "body_candidate_spans": retained, "excluded_ids": removed,
                         "note": "仅剔除已确认的文字层非正文对象；阅读顺序、图像、OCR、表格结构仍须核验。"})
        destination = output / "pages" / f"{int(page_number):06d}.json"
        # Re-running repairs a partial export without deleting previous valid outputs.
        unchanged = False
        if destination.exists():
            try:
                unchanged = read_signed(destination)["sha256"] == record["sha256"]
            except (ValueError, KeyError, TypeError):
                pass
        if not unchanged:
            atomic_json(destination, record)
        outputs[page_number] = record["sha256"]
    atomic_json(output / "exclusions.json", signed({"processing_sha256": signature, "items": exclusions}))
    atomic_json(output / "decisions.json", decisions)
    atomic_json(output / "receipt.json", signed({"processing_sha256": signature, "page_hashes": outputs, "excluded_count": len(exclusions), "exclusions_sha256": file_hash(output / "exclusions.json"), "decisions_sha256": file_hash(output / "decisions.json"), "scope": "reviewed_text_layer_only"}))
    return {"output": str(output), "excluded": len(exclusions), "pages": len(outputs), "processing_sha256": signature}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract")
    ext.add_argument("source")
    ext.add_argument("--cache", required=True)
    ana = sub.add_parser("analyze")
    ana.add_argument("--cache", required=True)
    ana.add_argument("--report", required=True)
    for command in (ext, ana):
        command.add_argument("--start", type=int, default=1)
        command.add_argument("--end", type=int)
    app = sub.add_parser("apply")
    for option in ("cache", "report", "decisions", "output"):
        app.add_argument("--" + option, required=True)
    a = p.parse_args()
    if a.command == "extract": result = extract(a.source, a.cache, a.start, a.end)
    elif a.command == "analyze": result = analyze(a.cache, a.report, a.start, a.end)
    else: result = apply(a.cache, a.report, a.decisions, a.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    configure_stdio()
    try:
        main()
    except (ValueError, KeyError, OSError, ImportError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
