#!/usr/bin/env python3
"""Render reviewed ledger text. No translation calls, OCR, or automatic layout inference."""
import argparse
from collections import defaultdict
import html
import json
import math
import os
from pathlib import Path
import sys
import uuid

import translation_project as ledger
from check_environment import configure_stdio

VERSION = 1
MAX_UNITS = 10000
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
PDF_COPY_BATCH_PAGES = 64


def safe_file(root, relative, expected_hash=None):
    ledger.portable_sample_path(relative)
    path = (root / relative).resolve()
    ledger.need(path.is_relative_to(root) and path.is_file(), "资源必须位于项目内：" + relative)
    if expected_hash:
        ledger.need(ledger.file_hash(path) == expected_hash, "资源哈希变化：" + relative)
    return path


def normalized(text):
    # Do not casefold, dehyphenate, or normalize mathematical Unicode.
    return " ".join(text.split())


def rectangle(values):
    ledger.need(isinstance(values, list) and len(values) == 4 and
                all(type(x) in (int, float) and math.isfinite(x) for x in values), "bbox 必须是4个有限数值")
    ledger.need(values[0] < values[2] and values[1] < values[3], "bbox 必须有正面积")
    return values


def style(value):
    from document_model import validate_style
    validate_style(value)
    return value


def validate_runs(runs):
    ledger.need(isinstance(runs, list) and all(isinstance(r, dict) and isinstance(r.get("text"), str)
                and set(r) <= {"text", "style"} for r in runs), "runs 格式错误或含未知字段")


def snapshot(root, sample):
    db = ledger.connection(root)
    try:
        with ledger.transaction(db):
            ledger.verify_source(db, full=True)
            ledger.verify_snapshots(db)
            ledger.verify_assets(db, root)
            ledger.refresh_reviews(db)
            if not sample:
                ledger.need(ledger.gate(db, root) in ("approved", "not_required"), "先确认有效样本")
            where = " WHERE sample=1" if sample else ""
            count, pending, size = db.execute("SELECT COUNT(*),SUM(state!='reviewed'),SUM(length(CAST(source AS BLOB))+COALESCE(length(CAST(result AS BLOB)),0)) FROM units" + where).fetchone()
            ledger.need(count and not pending, "输出范围内的所有块必须先审校通过")
            ledger.need(count <= MAX_UNITS and size <= MAX_PAYLOAD_BYTES,
                        "超出内置输出器的 10000 块/32 MiB 载荷界限；请用从统一台账读取的分卷适配器合成并登记完整成果")
            units = {r["id"]: {"unit": json.loads(r["source"]), "result": json.loads(r["result"])}
                     for r in db.execute("SELECT id,source,result FROM units" + where + " ORDER BY ordinal")}
            role = "sample" if sample else "full"
            return {"units": units, "role": role, "source": ledger.get_meta(db, "source"),
                    "content_signature": ledger.content_signature(db, role),
                    "output_signature": ledger.output_signature(db),
                    "formats": ledger.output_policy(db)["formats"]}
    finally:
        db.close()


def bindings(block, units):
    uid = block.get("unit_id")
    ledger.need(uid in units, "计划引用了输出范围外的块：" + str(uid))
    unit, result = units[uid]["unit"], units[uid]["result"]
    ledger.need(not unit.get("inline_placeholders"), "含行内占位符的块需使用专用公式/富文本适配器")
    if "segments" in unit:
        ledger.need(block["type"] == "table", "逐格块须由 table 计划输出，不能压平成段落")
        structure = unit.get("structure", {})
        ledger.need(structure.get("schema") == "table" and structure.get("version") == 1,
                    "表格需使用经过拓扑验证的 table v1 结构")
        data = {s["id"]: {"source": s["source"], "target": t["target"]}
                for s, t in zip(unit["segments"], result["segments"])}
        ledger.need(not any(s.get("inline_placeholders") for s in unit["segments"]), "含行内占位符的单元格需专用适配器")
        return unit, data
    ledger.need(block["type"] == "paragraph", "非表格文字块使用 paragraph 输出")
    return unit, result["target"]


def validate_plan(plan, snap):
    ledger.need(isinstance(plan, dict) and type(plan.get("version")) is int and plan["version"] == VERSION, "输出计划版本必须为1")
    ledger.need(plan.get("source_sha256") == snap["source"]["sha256"], "计划绑定的源文件不匹配")
    ledger.need(not (set(plan) - {"version", "source_sha256", "title", "blocks", "pdf_pages", "min_scale", "exclude_text"}), "输出计划存在未知字段")
    ledger.need("title" not in plan or isinstance(plan["title"], str), "title 必须为字符串")
    ledger.need(isinstance(plan.get("blocks"), list) and plan["blocks"], "计划缺少 blocks")
    seen, ordered = set(), []
    for block in plan["blocks"]:
        ledger.need(isinstance(block, dict) and block.get("type") in ("paragraph", "table", "image", "section"), "未知输出块类型")
        fields = {"paragraph": {"type", "unit_id", "style", "heading_level", "page_break_before", "runs", "pdf"},
                  "table": {"type", "unit_id", "style", "page_break_before", "column_widths", "header_rows", "cell_runs", "pdf_cells"},
                  "image": {"type", "asset", "width", "preservation_note", "page_break_before"},
                  "section": {"type", "columns", "new_page", "page_size"}}
        ledger.need(set(block) <= fields[block["type"]], "输出块存在未知或不适用字段：" + block["type"])
        for flag in ("page_break_before", "new_page"):
            ledger.need(flag not in block or type(block[flag]) is bool, flag + " 必须为布尔值")
        ledger.need("pdf_cells" not in block or isinstance(block["pdf_cells"], dict), "pdf_cells 必须为对象")
        for location in ([block["pdf"]] if "pdf" in block else block.get("pdf_cells", {}).values()):
            ledger.need(isinstance(location, dict) and set(location) <= {"page", "bbox", "source_text", "mapping_note"}, "PDF 位置存在未知字段")
        style(block.get("style", {}))
        if block["type"] in ("paragraph", "table"):
            uid = block.get("unit_id")
            ledger.need(uid not in seen, "重复输出块：" + str(uid))
            seen.add(uid)
            ordered.append(uid)
            _, data = bindings(block, snap["units"])
            if "runs" in block:
                validate_runs(block["runs"])
            if "cell_runs" in block:
                ledger.need(isinstance(block["cell_runs"], dict) and set(block["cell_runs"]) <= set(data), "cell_runs 引用了不存在的单元格")
                for runs in block["cell_runs"].values():
                    validate_runs(runs)
        else:
            ledger.need("unit_id" not in block, "图片或分节不能代替已审文字块")
    ledger.need(seen == set(snap["units"]), "输出计划必须完整覆盖本次全部已审块，不得遗漏或混入非样本块")
    ledger.need(ordered == list(snap["units"]), "输出计划须遵循台账的逻辑阅读顺序")
    ledger.need(isinstance(plan.get("exclude_text", []), list), "exclude_text 必须为数组")
    for item in plan.get("exclude_text", []):
        ledger.need(isinstance(item, dict) and set(item) <= {"id", "page", "bbox", "source_text", "classification", "evidence"}, "文字排除存在未知字段")


def add_runs(paragraph, text, config, runs=None):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    style(config)
    ledger.need(not (set(config) & {"width", "height", "custom"}), "原生 Word 不支持此尺寸/custom 样式；使用专门适配器")
    paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                           "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[config.get("align", "left")]
    if runs is None:
        runs = [{"text": text}]
    validate_runs(runs)
    ledger.need("".join(r["text"] for r in runs) == text, "runs 必须与台账审定译文逐字一致")
    for part in runs:
        fmt = dict(config, **part.get("style", {}))
        style(fmt)
        ledger.need(not (set(part.get("style", {})) - {"font_name", "font_size", "bold", "italic", "color", "background"}),
                    "局部 runs 仅支持字体/字号/粗斜体/前景和背景色")
        run = paragraph.add_run(part["text"])
        run.bold, run.italic = fmt.get("bold", False), fmt.get("italic", False)
        run.font.size = Pt(fmt.get("font_size", 10.5))
        run.font.name = fmt.get("font_name", "Noto Sans CJK SC")
        fonts = run._element.get_or_add_rPr().find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            run._element.get_or_add_rPr().append(fonts)
        fonts.set(qn("w:eastAsia"), run.font.name)
        if "color" in fmt:
            run.font.color.rgb = RGBColor.from_string(fmt["color"].lstrip("#"))
        if "background" in fmt:
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), fmt["background"].lstrip("#"))
            run._element.get_or_add_rPr().append(shd)


def make_docx(path, plan, snap, root):
    from docx import Document
    from docx.enum.section import WD_SECTION_START
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    doc = Document()
    doc.core_properties.title = plan.get("title", "非正式中文译本")
    grid_total = 0
    for block in plan["blocks"]:
        kind = block["type"]
        if kind == "section":
            section = doc.add_section(WD_SECTION_START.NEW_PAGE if block.get("new_page") else WD_SECTION_START.CONTINUOUS)
            cols = block.get("columns", 1)
            ledger.need(type(cols) is int and 1 <= cols <= 3, "columns 仅支持1至3")
            node = section._sectPr.find(qn("w:cols"))
            if node is None:
                node = OxmlElement("w:cols"); section._sectPr.append(node)
            node.set(qn("w:num"), str(cols))
            if "page_size" in block:
                w, h = block["page_size"]
                ledger.need(all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (w, h)), "page_size 必须为正数")
                section.page_width, section.page_height = Pt(w), Pt(h)
            continue
        if block.get("page_break_before"):
            doc.add_page_break()
        if kind == "image":
            asset = block["asset"]
            image_path = safe_file(root, asset["path"], asset["sha256"])
            ledger.need(isinstance(block.get("preservation_note"), str) and block["preservation_note"].strip(), "保留原图需说明图内文字/水印处置")
            width = block.get("width", 430)
            ledger.need(type(width) in (int, float) and math.isfinite(width) and width > 0, "图片 width 须为有限正数")
            doc.add_picture(str(image_path), width=Pt(width))
            continue
        unit, data = bindings(block, snap["units"])
        config = dict(unit.get("style_meta", {}), **block.get("style", {}))
        if kind == "paragraph":
            ledger.need(not (set(config) & {"borders", "padding", "vertical_align"}),
                        "段落不支持单元格边框/内边距/垂直对齐；使用专门适配器")
            level = block.get("heading_level")
            ledger.need(level is None or type(level) is int and 1 <= level <= 9, "heading_level 必须为1至9")
            p = doc.add_paragraph(style="Heading " + str(level) if level else None)
            add_runs(p, data, config, block.get("runs"))
        else:
            struct = unit["structure"]
            grid_total += struct["rows"] * struct["columns"]
            ledger.need(struct["rows"] * struct["columns"] <= 20000,
                        "原生 Word 单表网格超过 20000 格；先按语义规划续表或使用分卷适配器")
            ledger.need(grid_total <= 50000, "原生 Word 总表格网格超过 50000 格；使用分卷适配器")
            table = doc.add_table(rows=struct["rows"], cols=struct["columns"])
            table.autofit = False
            widths = block.get("column_widths", [430 / struct["columns"]] * struct["columns"])
            ledger.need(len(widths) == struct["columns"] and all(type(w) in (int, float) and math.isfinite(w) and w > 0 for w in widths), "column_widths 与列数不符")
            for column, width in zip(table.columns, widths):
                column.width = Pt(width)
            for cell in struct["cells"]:
                row, col = cell["row"], cell["col"]
                rs, cs = cell.get("rowspan", 1), cell.get("colspan", 1)
                tc = table.cell(row, col)
                if rs > 1 or cs > 1:
                    tc = tc.merge(table.cell(row + rs - 1, col + cs - 1))
                tc.width = Pt(sum(widths[col:col + cs]))
                fmt = dict(config, **cell.get("style", {}))
                style(fmt)
                tc.vertical_alignment = {"top": WD_CELL_VERTICAL_ALIGNMENT.TOP, "center": WD_CELL_VERTICAL_ALIGNMENT.CENTER,
                                         "bottom": WD_CELL_VERTICAL_ALIGNMENT.BOTTOM}[fmt.get("vertical_align", "top")]
                props = tc._tc.get_or_add_tcPr()
                if "padding" in fmt:
                    margins = OxmlElement("w:tcMar")
                    for side, value in fmt["padding"].items():
                        element = OxmlElement("w:" + side)
                        element.set(qn("w:w"), str(round(value * 20)))
                        element.set(qn("w:type"), "dxa"); margins.append(element)
                    props.append(margins)
                if "borders" in fmt:
                    borders = OxmlElement("w:tcBorders")
                    for side, value in fmt["borders"].items():
                        element = OxmlElement("w:" + side)
                        element.set(qn("w:val"), "nil" if value.get("style") == "none" or value.get("width") == 0 else value.get("style", "single"))
                        element.set(qn("w:sz"), str(round(value.get("width", 0.5) * 8)))
                        element.set(qn("w:color"), value.get("color", "#000000").lstrip("#"))
                        borders.append(element)
                    props.append(borders)
                if "background" in fmt:
                    shading = OxmlElement("w:shd"); shading.set(qn("w:val"), "clear")
                    shading.set(qn("w:fill"), fmt["background"].lstrip("#")); props.append(shading)
                tc.text = ""
                add_runs(tc.paragraphs[0], data[cell["id"]]["target"], fmt, block.get("cell_runs", {}).get(cell["id"]))
            count = block.get("header_rows", 0)
            ledger.need(type(count) is int and 0 <= count <= struct["rows"], "header_rows 越界")
            for row in table.rows[:count]:
                tag = OxmlElement("w:tblHeader"); row._tr.get_or_add_trPr().append(tag)
    doc.save(path)
    return {"engine": "python-docx", "visual_review": "required", "note": "字体替代、分栏、合并格与分页须用实际Word渲染器检查"}


def pdf_entries(plan, snap):
    entries = []
    for block in plan["blocks"]:
        if block["type"] in ("image", "section"):
            continue  # Original PDF already contains these visual objects.
        unit, data = bindings(block, snap["units"])
        ledger.need(len(unit["pdf_pages"]) == 1, "内置 PDF 不支持跨页块；按可核验子块拆分或使用多区域适配器")
        ledger.need(unit.get("kind") not in ("code", "algorithm", "preformatted"), "代码/算法/预格式块需要保留空白及语法的专门 PDF 适配器")
        if block["type"] == "paragraph":
            entries.append(dict(block.get("pdf", {}), id=unit["id"], target=data, source=unit["source"],
                                style=dict(unit.get("style_meta", {}), **block.get("style", {})), runs=block.get("runs")))
        else:
            locations = block.get("pdf_cells", {})
            ledger.need(set(locations) == set(data), "pdf_cells 必须逐格覆盖，包含空单元格")
            for cell in unit["structure"]["cells"]:
                sid = cell["id"]
                entries.append(dict(locations[sid], id=unit["id"] + "/" + sid,
                                    target=data[sid]["target"], source=data[sid]["source"],
                                    style={**unit.get("style_meta", {}), **block.get("style", {}), **cell.get("style", {})},
                                    runs=block.get("cell_runs", {}).get(sid)))
    for item in plan.get("exclude_text", []):
        ledger.need(item.get("classification") in ("watermark", "header", "footer", "page_number"), "排除类别不支持")
        ledger.need(isinstance(item.get("evidence"), str) and item["evidence"].strip(), "文字排除需要核验证据")
        entries.append(dict(item, id="exclude/" + item["id"], target="", source=item["source_text"], style={}))
    return entries


def pdf_markup(entry, font_path):
    ledger.need("\t" not in entry["target"] and "  " not in entry["target"],
                "内置 PDF 流排不支持 Tab 或连续空格；使用保留空白的专门适配器，不能修改审定文本")
    fmt = style(entry["style"])
    ledger.need(not (set(fmt) & {"width", "height", "padding", "custom"}),
                "PDF 回填不支持尺寸/padding/custom 样式；请在 bbox 中体现留白")
    # Backgrounds and borders stay in the original page graphics.
    css = ("@font-face {font-family: translated; src: url('" + font_path.name.replace("'", "\\'") + "');} "
           "* {margin:0;padding:0;} body {font-family:translated;font-size:" + str(fmt.get("font_size", 10.5)) +
           "pt; color:" + fmt.get("color", "#000000") + ";text-align:" + fmt.get("align", "left") +
           ";font-weight:" + ("bold" if fmt.get("bold") else "normal") +
           ";font-style:" + ("italic" if fmt.get("italic") else "normal") + ";}")
    runs = entry.get("runs")
    if runs is None:
        runs = [{"text": entry["target"]}]
    validate_runs(runs)
    ledger.need("".join(x["text"] for x in runs) == entry["target"], "runs 必须与台账审定译文逐字一致")
    fragments = []
    for run in runs:
        local = style(run.get("style", {}))
        ledger.need(not (set(local) - {"font_size", "bold", "italic", "color"}), "PDF 局部 runs 仅支持字号/粗斜体/颜色")
        declarations = []
        for key, value in local.items():
            declarations.append({"font_size": "font-size:" + str(value) + "pt", "bold": "font-weight:" + ("bold" if value else "normal"),
                                 "italic": "font-style:" + ("italic" if value else "normal"), "color": "color:" + str(value)}[key])
        fragments.append('<span style="' + ";".join(declarations) + '">' + html.escape(run["text"]).replace("\n", "<br>") + "</span>")
    return "<div>" + "".join(fragments) + "</div>", css


def pdf_copy_ranges(page_numbers):
    """Bounded contiguous source ranges; public page numbers are one-based."""
    start = previous = page_numbers[0] - 1
    for number in page_numbers[1:]:
        current = number - 1
        if current != previous + 1 or current - start >= PDF_COPY_BATCH_PAGES:
            yield start, previous
            start = current
        previous = current
    yield start, previous


def pdf_navigation_source(source, page_numbers, fitz):
    """Read navigation before any mutation; reject features we cannot safely copy."""
    catalog = source.pdf_catalog()
    present = lambda key: source.xref_get_key(catalog, key)[0] != "null"
    ledger.need(not present("OCProperties"), "源 PDF 含可选内容图层 OCG，内置输出不能安全保留其显示状态；使用专门适配器")
    navigation = {"links": {}, "toc": source.get_toc(simple=False), "labels": source.get_page_labels(),
                  "source_named_destinations_present": present("Dests") or present("Names/Dests"),
                  "resolved_named_actions": 0,
                  "source_tag_tree_present": present("StructTreeRoot"),
                  "source_embedded_file_count": source.embfile_count()}
    ledger.need(len(source.get_outline_xrefs()) == len(navigation["toc"]),
                "源 PDF 存在无法完整解析的书签；使用专门导航适配器")
    named_targets = None

    def check_action(destination):
        xref = destination.get("xref")
        ledger.need(type(xref) is int and xref > 0, "源导航缺少可核验对象引用")
        action_type, action = source.xref_get_key(xref, "A/S")
        ledger.need(action_type == "null" or action in ("/GoTo", "/URI", "/Launch"),
                    "源导航含未支持的动作（含 GoToR 外部 PDF 目标和命名查看器动作）：" + action)

    def resolve_named(destination):
        nonlocal named_targets
        check_action(destination)
        if destination.get("kind") != fitz.LINK_NAMED:
            return destination
        # resolve_names() reports PDF coordinates, unlike ordinary GoTo links.
        # Read its explicit XYZ target rather than assuming get_links() uses
        # the same coordinate convention for named and numeric destinations.
        if named_targets is None:
            named_targets = source.resolve_names()
        target = named_targets.get(destination.get("nameddest"), {})
        point = target.get("to")
        ledger.need(isinstance(destination.get("nameddest"), str) and destination["nameddest"] and
                    type(destination.get("page")) is int and 0 <= destination["page"] < len(source) and
                    destination["page"] == target.get("page") and "dest" not in target and
                    isinstance(point, (tuple, list)) and len(point) == 2 and
                    all(type(v) in (int, float) and math.isfinite(v) for v in point),
                    "源命名导航无法解析为明确的内部页与坐标；使用专门导航适配器")
        converted = {key: value for key, value in destination.items() if key not in ("nameddest", "name")}
        converted["kind"] = fitz.LINK_GOTO
        converted["to"] = fitz.Point(point) * source[target["page"]].transformation_matrix
        converted["zoom"] = target.get("zoom", 0)
        navigation["resolved_named_actions"] += 1
        return converted

    for number in page_numbers:
        page = source[number - 1]
        ledger.need(page.rotation == 0, "此适配器只接受未旋转页面；先用保留变换证据的布局适配器处理")
        kinds = [item[1] for item in page.annot_xrefs()]
        ledger.need(fitz.PDF_ANNOT_REDACT not in kinds,
                    "源页含尚未应用的删除注释 redaction，拒绝将其与翻译删除操作混用：" + str(number))
        ledger.need(next(page.widgets() or iter(()), None) is None,
                    "源页含交互表单，内置输出不能安全保留字段关系：" + str(number))
        links = [resolve_named(link) for link in page.get_links()]
        ledger.need(kinds.count(fitz.PDF_ANNOT_LINK) == len(links),
                    "源页存在无法完整解析的链接注释：" + str(number))
        for link in links:
            kind = link.get("kind")
            ledger.need(kind in (fitz.LINK_GOTO, fitz.LINK_URI, fitz.LINK_LAUNCH),
                        "源页含未支持的链接动作或命名链接：" + str(number))
            if kind == fitz.LINK_GOTO:
                ledger.need(type(link.get("page")) is int and link["page"] >= 0,
                            "源页含未解析的链接目的地：" + str(number))
            if kind == fitz.LINK_GOTO:
                ledger.need(link["page"] < len(source), "源页内部链接目标越界：" + str(number))
        navigation["links"][number - 1] = links
    for entry in navigation["toc"]:
        destination = entry[3] = resolve_named(entry[3])
        kind = destination.get("kind", fitz.LINK_NONE)
        ledger.need(kind in (fitz.LINK_NONE, fitz.LINK_GOTO, fitz.LINK_URI, fitz.LINK_LAUNCH),
                    "源书签含未支持的动作或命名目的地")
        if kind == fitz.LINK_GOTO:
            ledger.need(type(destination.get("page")) is int and destination["page"] >= 0,
                        "源书签目的地无法解析")
        if kind == fitz.LINK_GOTO:
            ledger.need(destination["page"] < len(source), "源书签内部目标越界")
    return navigation


def pdf_restore_navigation(output, navigation, page_map, fitz):
    """Rebuild links after redaction so translated anchors do not lose hyperlinks."""
    preserved, omitted, disabled = 0, [], []
    allowed = {fitz.LINK_GOTO: {"kind", "from", "page", "to", "zoom"},
               fitz.LINK_URI: {"kind", "from", "uri"},
               fitz.LINK_LAUNCH: {"kind", "from", "file"}}
    for source_page, links in navigation["links"].items():
        target_page = output[page_map[source_page]]
        for link in links:
            kind = link["kind"]
            if kind == fitz.LINK_GOTO and link["page"] not in page_map:
                omitted.append({"source_page": source_page + 1, "target_source_page": link["page"] + 1,
                                "reason": "destination_outside_selected_pages"})
                continue
            copied = {key: value for key, value in link.items() if key in allowed[kind]}
            if kind == fitz.LINK_GOTO:
                copied["page"] = page_map[link["page"]]
            target_page.insert_link(copied)
            preserved += 1
    toc = []
    for level, title, number, original in navigation["toc"]:
        destination = {key: value for key, value in original.items() if key != "xref"}
        if destination.get("kind") == fitz.LINK_GOTO:
            source_page = destination["page"]
            if source_page in page_map:
                number = page_map[source_page] + 1
                destination["page"] = number - 1
            else:
                disabled.append({"title": title, "target_source_page": source_page + 1})
                number = -1
                destination = {key: value for key, value in destination.items()
                               if key in ("color", "bold", "italic", "collapse")}
                destination["kind"] = fitz.LINK_NONE
        toc.append([level, title, number, destination])
    if toc:
        skeleton = []
        for level, title, number, destination in toc:
            if destination.get("kind") == fitz.LINK_GOTO:
                appearance = {key: value for key, value in destination.items()
                              if key in ("color", "bold", "italic", "collapse")}
                skeleton.append([level, title, -1, dict(appearance, kind=fitz.LINK_NONE)])
            else:
                skeleton.append([level, title, number, destination])
        output.set_toc(skeleton)
        # In 1.26.7 set_toc() inverts Y using cropbox.height but omits its
        # offset. Write internal XYZ actions with the complete inverse page
        # transform, as insert_link() does, rather than guessing an offset.
        for entry, xref in zip(toc, output.get_outline_xrefs()):
            destination = entry[3]
            kind = destination.get("kind")
            if kind != fitz.LINK_GOTO:
                continue
            point = fitz.Point(destination.get("to", (72, 36)))
            page = output[destination["page"]]
            point = point * ~page.transformation_matrix
            zoom = destination.get("zoom", 0)
            ledger.need(all(math.isfinite(v) for v in point) and type(zoom) in (int, float) and
                        math.isfinite(zoom) and zoom >= 0, "书签目的地坐标或缩放值无效")
            action = "<</S/GoTo/D[%d 0 R /XYZ %.12g %.12g %.12g]>>" % (page.xref, point.x, point.y, zoom)
            output.xref_set_key(xref, "A", action)
    # Preserve printed labels even for discontiguous selections. A new rule is
    # needed at every discontinuity, not merely at every source rule boundary.
    source_rules = sorted(navigation["labels"], key=lambda rule: rule["startpage"])
    rules, index = [], 0
    active = {"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 1}
    for source_page, target_page in page_map.items():
        while index < len(source_rules) and source_rules[index]["startpage"] <= source_page:
            active = source_rules[index]
            index += 1
        prefix, label_style = active.get("prefix", ""), active.get("style", "")
        number = active.get("firstpagenum", 1) + source_page - active["startpage"]
        rule = {"startpage": target_page, "prefix": prefix, "style": label_style, "firstpagenum": number}
        previous = rules[-1] if rules else None
        if previous and prefix == previous["prefix"] and label_style == previous["style"]:
            if not label_style or number == previous["firstpagenum"] + target_page - previous["startpage"]:
                continue
        rules.append(rule)
    output.set_page_labels(rules)
    return {"preserved_links": preserved, "omitted_links": omitted,
            "toc_entries": len(toc), "disabled_toc_destinations": disabled, "page_label_rules": rules,
            "source_named_destinations_present": navigation["source_named_destinations_present"],
            "resolved_named_actions": navigation["resolved_named_actions"], "named_destinations_copied": False,
            "source_tag_tree_present": navigation["source_tag_tree_present"], "tag_tree_rebuilt": False,
            "source_embedded_file_count": navigation["source_embedded_file_count"], "embedded_files_copied": False,
            "note": "书签标题保留原文，链接热区保留源坐标；已解析命名导航转为数字页跳转，命名目录/标签树/嵌入附件未复制。外部文件目标保留原路径，未复制外部文件，相对路径须另行核验。"}


def make_pdf(path, plan, snap, font_path):
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise ValueError("此可选适配器需要 PyMuPDF；可使用宿主其他经过验证的PDF排版能力") from exc
    ledger.need(font_path and Path(font_path).is_file(), "PDF 回填需明确 --font 本地中文字体文件")
    font_path = Path(font_path).resolve()
    font = fitz.Font(fontfile=str(font_path))
    archive = fitz.Archive(str(font_path.parent))
    entries = pdf_entries(plan, snap)
    entries_by_page = defaultdict(list)
    for entry in entries:
        ledger.need(type(entry.get("page")) is int, "每个回填区域必须指定源页序")
        entries_by_page[entry["page"]].append(entry)
    page_numbers = plan.get("pdf_pages")
    ledger.need(isinstance(page_numbers, list) and page_numbers and all(type(p) is int for p in page_numbers), "pdf_pages 必须为源页序列表")
    ledger.need(page_numbers == sorted(set(page_numbers)), "pdf_pages 须唯一且按源页序递增")
    required_pages = {p for item in snap["units"].values() for p in item["unit"]["pdf_pages"]}
    ledger.need(required_pages <= set(page_numbers), "计划遗漏已审内容所在源页")
    source_path = snap["source"]["path"]
    scale_min = plan.get("min_scale", 0.9)
    ledger.need(type(scale_min) in (int, float) and 0.5 <= scale_min <= 1, "min_scale 必须在0.5至1之间")
    reports = []
    with fitz.open(source_path) as source, fitz.open() as output:
        ledger.need(all(1 <= p <= len(source) for p in page_numbers), "PDF 页序越界")
        ledger.need(all(e.get("page") in page_numbers for e in entries), "存在没有输出的回填页面")
        page_map = {number - 1: index for index, number in enumerate(page_numbers)}
        navigation = pdf_navigation_source(source, page_numbers, fitz)
        for start, end in pdf_copy_ranges(page_numbers):
            output.insert_pdf(source, from_page=start, to_page=end, links=False, widgets=False,
                              final=int(end == page_numbers[-1] - 1))
        for pno in page_numbers:
            page = source[pno - 1]
            ledger.need(page.rotation == 0, "此适配器只接受未旋转页面；先用保留变换证据的布局适配器处理")
            selected = entries_by_page[pno]
            regions = []
            chars = [ch for b in page.get_text("rawdict")["blocks"] if b["type"] == 0 for line in b["lines"] for span in line["spans"] for ch in span["chars"]]
            for e in selected:
                if not e["id"].startswith("exclude/"):
                    ledger.need(e["page"] in snap["units"][e["id"].split("/")[0]]["unit"]["pdf_pages"],
                                "回填源页与对应块登记页序不符：" + e["id"])
                rect = fitz.Rect(rectangle(e.get("bbox")))
                ledger.need(page.rect.contains(rect), "回填区域越过页面：" + e["id"])
                ledger.need(not any(rect.intersects(other) for other in regions), "回填区域重叠；需精确分离：" + e["id"])
                regions.append(rect)
                observed = page.get_text("text", clip=rect)
                expected = e.get("source_text", e["source"])
                ledger.need(normalized(observed) == normalized(expected), "区域原文不匹配，拒绝覆盖：" + e["id"])
                if normalized(expected) != normalized(e["source"]):
                    ledger.need(isinstance(e.get("mapping_note"), str) and e["mapping_note"].strip(), "源文本校正需 mapping_note：" + e["id"])
                for ch in chars:
                    box = fitz.Rect(ch["bbox"])
                    if rect.intersects(box):
                        ledger.need(rect.contains(box) or not ch["c"].strip(), "区域切过邻接字形，拒绝删除：" + e["id"])
                ledger.need(all(font.has_glyph(ord(c)) for c in e["target"] if not c.isspace()), "字体缺少目标字形：" + e["id"])
                markup, css = pdf_markup(e, font_path)
                if e["target"]:
                    with fitz.open() as trial:
                        probe = trial.new_page(width=page.rect.width, height=page.rect.height)
                        spare, scale = probe.insert_htmlbox(rect, markup, css=css, archive=archive, scale_low=scale_min)
                    ledger.need(spare >= 0, "译文溢出，需调整布局，不能缩译：" + e["id"])
                else:
                    spare, scale = 0, 1
                e.update(_rect=rect, _markup=markup, _css=css, _spare=spare, _scale=scale)
            target_page = output[page_map[pno - 1]]
            for e in selected:
                target_page.add_redact_annot(e["_rect"], fill=False, cross_out=False)
            if selected:
                target_page.apply_redactions(images=0, graphics=0, text=0)
            for e in selected:
                if e["target"]:
                    r = fitz.Rect(e["_rect"])
                    align = e["style"].get("vertical_align", "top")
                    r.y0 += e["_spare"] * ({"top": 0, "center": 0.5, "bottom": 1}[align])
                    spare, scale = target_page.insert_htmlbox(r, e["_markup"], css=e["_css"], archive=archive, scale_low=scale_min)
                    ledger.need(spare >= 0, "实际回填溢出：" + e["id"])
                reports.append({"id": e["id"], "source_page": pno, "bbox": list(e["_rect"]), "scale": e["_scale"]})
        navigation_report = pdf_restore_navigation(output, navigation, page_map, fitz)
        output.set_metadata({"title": plan.get("title", "非正式中文译本"), "creator": "protocol-en2zh reviewed-text adapter"})
        output.save(path, garbage=4, deflate=True)
    return {"engine": "PyMuPDF", "version": fitz.VersionBind, "font_sha256": ledger.file_hash(font_path),
            "style_policy": "--font 决定实际嵌入字体；font_name 是 Word 字体设置。PDF 的 background/borders 始终保留原图形，不重画或改变。",
            "source_to_output_pages": [{"source_page": source + 1, "output_page": target + 1}
                                       for source, target in page_map.items()],
            "navigation": navigation_report,
            "regions": reports, "visual_review": "required", "text_layer_review": "required",
            "limitations": ["只回填经过位置/原文核验的原生文字；扫描图/重叠水印/复杂行内公式需其他适配器",
                            "保留源页面图形；未重建标签树，须核验搜索复制及阅读序", "未登记为回填区域的内容保持原样，最终覆盖验收仍必需"]}


def publish_new_file(staged, destination):
    """Same-directory hard linking publishes atomically without replacing a path."""
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise ValueError("输出或回执路径已存在；保留原文件并使用新路径：" + str(destination)) from exc
    except OSError as exc:
        raise ValueError("无法安全发布文件；输出文件系统须支持同目录硬链接，请换到本地支持的目录：" + str(destination)) from exc


def write_staged_receipt(path, receipt):
    # This unique file is not published yet; avoid a second long temp suffix.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def render(root, plan_path, output, fmt, sample=False, font=None, artifact_id=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    ledger.need(output.is_relative_to(root) and output.suffix.lower() == "." + fmt, "输出须位于项目内且扩展名匹配")
    ledger.portable_sample_path(output.relative_to(root).as_posix())
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    ledger.need(not output.exists() and not receipt_path.exists(), "输出或回执已存在；为新版本使用新路径")
    snap = snapshot(root, sample)
    ledger.need(fmt in snap["formats"], "输出格式不在当前输出约定中")
    plan = ledger.read_json(plan_path)
    validate_plan(plan, snap)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(uuid.uuid4().hex + ".tmp" + output.suffix)
    receipt_temp = temp.with_suffix(".json")
    published = []
    failed = False
    try:
        detail = make_docx(temp, plan, snap, root) if fmt == "docx" else make_pdf(temp, plan, snap, font)
        with temp.open("rb+") as completed:
            os.fsync(completed.fileno())
        generated_hash = ledger.file_hash(temp)
        db = ledger.connection(root)
        try:
            with ledger.transaction(db):
                ledger.verify_source(db, full=True)
                ledger.need(snap["content_signature"] == ledger.content_signature(db, snap["role"]) and
                            snap["output_signature"] == ledger.output_signature(db), "生成期间译文或输出约定变化，请重新生成")
                receipt = dict(detail, plan_sha256=ledger.digest(plan), content_signature=snap["content_signature"],
                               output_signature=snap["output_signature"], artifact_sha256=generated_hash, role=snap["role"])
                write_staged_receipt(receipt_temp, receipt)
                receipt_hash = ledger.file_hash(receipt_temp)
                for staged, destination, expected in ((temp, output, generated_hash), (receipt_temp, receipt_path, receipt_hash)):
                    publish_new_file(staged, destination)
                    published.append((staged, destination, expected))
                registered = ledger.register_artifact(db, root, artifact_id or (snap["role"] + "-" + fmt), snap["role"], output)
                ledger.need(registered["sha256"] == generated_hash and ledger.file_hash(receipt_path) == receipt_hash,
                            "发布期间产物或回执发生变化；拒绝绑定不同版本")
        finally:
            db.close()
        return {"output": str(output), "artifact": registered, "receipt": receipt, "note": "生成不等于验收；仍需渲染、内容对账和视觉检查"}
    except Exception:
        failed = True
        raise
    finally:
        # Compensate ordinary failures only. Forced termination/KeyboardInterrupt
        # can land at a commit boundary: preserve published evidence for resume.
        if failed:
            for staged, destination, expected in reversed(published):
                try:
                    if (destination.exists() and staged.exists() and os.path.samefile(staged, destination)
                            and ledger.file_hash(destination) == expected):
                        destination.unlink()
                except OSError:
                    print("未能清理本次失败输出，请先核对台账并保留该文件：" + str(destination), file=sys.stderr)
        for staged in (receipt_temp, temp):
            try:
                if staged.exists():
                    staged.unlink()
            except OSError:
                print("临时文件清理未完成：" + str(staged), file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--format", choices=("docx", "pdf"), required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--sample", action="store_true")
    p.add_argument("--font")
    p.add_argument("--artifact-id")
    args = p.parse_args()
    print(json.dumps(render(args.project, args.plan, args.output, args.format, args.sample, args.font, args.artifact_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    configure_stdio()
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
