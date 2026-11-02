"""Optional, versioned document structure and terminology checks (stdlib only).

These checks validate recorded relationships, not extraction or translation quality.
Legacy inventory objects remain valid when the optional extensions are absent.
"""
from collections import Counter, defaultdict
from bisect import bisect_right
from functools import lru_cache
import math
from pathlib import PurePosixPath, PureWindowsPath
import re


DOCUMENT_TYPES = frozenset(("protocol", "technical", "paper"))
TERM_ACTIONS = frozenset(("translate", "retain", "first_use"))
_ID = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
MACHINE_KINDS = frozenset(("url", "doi", "email", "path", "cli_option", "escape"))
# Conservative discovery only: these are not a general URI/path/parser grammar.
_MACHINE_PATTERNS = (
    ("url", re.compile(r"(?<![A-Za-z0-9_])(?:https?|ftp)://[A-Za-z0-9:/?#@!$&'()*+,;=%._~\-\[\]]+", re.I)),
    ("doi", re.compile(r"(?<![A-Za-z0-9_])10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")),
    ("email", re.compile(r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")),
    ("cli_option", re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z][A-Za-z0-9_-]*")),
    ("escape", re.compile(r"\\(?:[abfnrtv\\'\"](?![A-Za-z0-9])|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})")),
)
_UNICODE_WATCH = frozenset("\u00ad\u061c\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")
_UNICODE_CORRUPT = frozenset("\x00\ufffd")


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name):
    _need(isinstance(value, str) and bool(value.strip()), name + " 必须为非空字符串")


def _id(value, name):
    _need(isinstance(value, str) and bool(_ID.fullmatch(value)), name + " ID 无效")


def _number(value, name, *, positive=False):
    _need(type(value) is int or type(value) is float and math.isfinite(value), name + " 必须为有限数值")
    _need(value > 0 if positive else value >= 0, name + " 数值越界")


def _json_value(value, name):
    if isinstance(value, dict):
        _need(all(isinstance(k, str) for k in value), name + " 键必须是字符串")
        for key, child in value.items():
            _json_value(child, name + "." + key)
    elif isinstance(value, list):
        for child in value:
            _json_value(child, name)
    else:
        _need(value is None or type(value) in (str, bool, int, float), name + " 必须是 JSON 值")
        if type(value) is float:
            _need(math.isfinite(value), name + " 不允许非有限数值")


def _choice(value, choices, name):
    _need(isinstance(value, str) and value in choices, name + " 无效")


def _object(value, allowed, name):
    _need(isinstance(value, dict), name + " 必须是对象")
    _need(set(value) <= allowed, name + " 存在未知字段；额外说明放 custom 对象")
    if "custom" in value:
        _need(isinstance(value["custom"], dict), name + ".custom 必须是对象")
        _json_value(value["custom"], name + ".custom")


def portable_asset_path(value):
    """Validate a portable relative reference; the caller must also check realpath."""
    _text(value, "资产路径")
    path = PurePosixPath(value)
    _need(path.parts and not path.is_absolute() and not PureWindowsPath(value).drive
          and "\\" not in value and value == path.as_posix() and ".." not in path.parts,
          "资产路径必须是项目内规范相对路径，使用 / 分隔")
    for part in path.parts:
        _need(not re.search(r'[<>:"|?*\x00-\x1f]', part) and not part.endswith((".", " ")),
              "资产路径含跨平台非法字符")
        stem = part.split(".", 1)[0].upper()
        _need(stem not in ("CON", "PRN", "AUX", "NUL")
              and not re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem), "资产路径含 Windows 保留名称")
    return path


def _ids(value, name):
    _need(isinstance(value, list), name + " 必须为数组")
    for item in value:
        _id(item, name)
    _need(len(set(value)) == len(value), name + " 有重复 ID")


def validate_style(style, name="style"):
    """Known style fields have constraints; custom carries engine-specific detail."""
    allowed = {"align", "vertical_align", "background", "color", "bold", "italic",
               "font_size", "font_name", "width", "height", "padding", "borders", "custom"}
    _object(style, allowed, name)
    for key, options in (("align", {"left", "center", "right", "justify"}),
                         ("vertical_align", {"top", "center", "bottom"})):
        if key in style:
            _choice(style[key], options, name + "." + key)
    for key in ("background", "color"):
        if key in style:
            _need(isinstance(style[key], str) and bool(_COLOR.fullmatch(style[key])),
                  name + "." + key + " 必须为 #RRGGBB")
    for key in ("bold", "italic"):
        if key in style:
            _need(type(style[key]) is bool, name + "." + key + " 必须是布尔值")
    for key in ("font_size", "width", "height"):
        if key in style:
            _number(style[key], name + "." + key, positive=True)
    if "font_name" in style:
        _text(style["font_name"], name + ".font_name")
    if "padding" in style:
        _object(style["padding"], {"top", "right", "bottom", "left"}, name + ".padding")
        for key, value in style["padding"].items():
            _number(value, name + ".padding." + key)
    if "borders" in style:
        _object(style["borders"], {"top", "right", "bottom", "left"}, name + ".borders")
        for side, border in style["borders"].items():
            _object(border, {"width", "color", "style"}, name + ".borders." + side)
            if "width" in border:
                _number(border["width"], "border.width")
            if "color" in border:
                _need(isinstance(border["color"], str) and bool(_COLOR.fullmatch(border["color"])), "border.color 无效")
            if "style" in border:
                _choice(border["style"], ("none", "single", "double", "dashed", "dotted"), "border.style")


def _source_meta(meta, pages):
    _object(meta, {"document_id", "source_sha256", "source_version", "processing_sha256",
                   "regions", "assets", "custom"}, "source_meta")
    for key in ("document_id", "source_version"):
        if key in meta:
            _text(meta[key], "source_meta." + key)
    for key in ("source_sha256", "processing_sha256"):
        if key in meta:
            _need(isinstance(meta[key], str) and bool(_SHA.fullmatch(meta[key])), key + " 必须为 SHA-256")
    if "regions" in meta:
        _need(isinstance(meta["regions"], list), "regions 必须为数组")
        for region in meta["regions"]:
            _object(region, {"page", "bbox", "page_size", "transform", "custom"}, "region")
            _need(type(region.get("page")) is int and region["page"] >= 1, "region.page 无效")
            if pages is not None:
                _need(region["page"] in pages, "region.page 必须属于当前块 pdf_pages")
            size, box = region.get("page_size"), region.get("bbox")
            _need(isinstance(size, list) and len(size) == 2, "region.page_size 需要 [宽,高]")
            _need(isinstance(box, list) and len(box) == 4, "region.bbox 需要 [x0,y0,x1,y1]")
            for value in size:
                _number(value, "page_size", positive=True)
            for value in box:
                _number(value, "bbox")
            _need(box[0] < box[2] <= size[0] and box[1] < box[3] <= size[1], "region.bbox 越界或无面积")
            if "transform" in region:
                transform = region["transform"]
                _need(isinstance(transform, list) and len(transform) == 6
                      and all(type(x) is int or type(x) is float and math.isfinite(x) for x in transform),
                      "region.transform 需要 6 个有限数值")
    if "assets" in meta:
        _need(isinstance(meta["assets"], list), "assets 必须为数组")
        seen = set()
        for asset in meta["assets"]:
            _object(asset, {"id", "path", "sha256", "media_type", "custom"}, "asset")
            _id(asset.get("id"), "asset")
            _need(asset["id"] not in seen, "asset.id 重复")
            seen.add(asset["id"])
            portable_asset_path(asset.get("path"))
            _need(isinstance(asset.get("sha256"), str) and bool(_SHA.fullmatch(asset["sha256"])), "asset.sha256 无效")
            if "media_type" in asset:
                _text(asset["media_type"], "asset.media_type")


def _rectangles_overlap(rectangles):
    """O(n log n) row sweep over compressed columns, not a full table grid."""
    columns = sorted({c for _, _, c0, c1 in rectangles for c in (c0, c1)})
    positions = {value: index for index, value in enumerate(columns)}
    intervals = len(columns) - 1
    maximum, lazy = [0] * (4 * intervals), [0] * (4 * intervals)

    def update(node, left, right, first, last, delta):
        if first <= left and right <= last:
            maximum[node] += delta
            lazy[node] += delta
            return
        middle = (left + right) // 2
        if first < middle:
            update(node * 2 + 1, left, middle, first, last, delta)
        if last > middle:
            update(node * 2 + 2, middle, right, first, last, delta)
        maximum[node] = lazy[node] + max(maximum[node * 2 + 1], maximum[node * 2 + 2])

    events = [(r0, 1, positions[c0], positions[c1]) for r0, _, c0, c1 in rectangles]
    events += [(r1, -1, positions[c0], positions[c1]) for _, r1, c0, c1 in rectangles]
    for _, delta, first, last in sorted(events):  # End before start at a shared edge.
        update(0, 0, intervals, first, last, delta)
        if maximum[0] > 1:
            return True
    return False


def _table(unit, structure):
    # Absence of a schema is explicitly legacy; no topology claim is made.
    if not isinstance(structure, dict) or "schema" not in structure:
        return
    _object(structure, {"schema", "version", "rows", "columns", "cells", "custom"}, "structure")
    _need(structure["schema"] == "table" and type(structure.get("version")) is int
          and structure["version"] == 1, "不支持的 structure schema/version")
    rows, cols = structure.get("rows"), structure.get("columns")
    _need(type(rows) is int and rows > 0 and type(cols) is int and cols > 0, "表格行列必须为正整数")
    cells = structure.get("cells")
    _need(isinstance(cells, list) and cells, "表格 cells 必须非空")
    ids, rectangles, area, last = set(), [], 0, (-1, -1)
    for cell in cells:
        _object(cell, {"id", "row", "col", "rowspan", "colspan", "style", "custom"}, "cell")
        _id(cell.get("id"), "cell")
        _need(cell["id"] not in ids, "cell.id 重复")
        ids.add(cell["id"])
        r, c, rs, cs = (cell.get(k) for k in ("row", "col", "rowspan", "colspan"))
        _need(all(type(x) is int for x in (r, c, rs, cs)), "单元格坐标/跨度必须为整数")
        _need(r >= 0 and c >= 0 and rs > 0 and cs > 0 and r + rs <= rows and c + cs <= cols,
              "单元格坐标/跨度越界")
        _need((r, c) > last, "cells 必须按起点行、列排序")
        last = (r, c)
        rectangles.append((r, r + rs, c, c + cs))
        area += rs * cs
        if "style" in cell:
            validate_style(cell["style"], "cell.style")
    _need(not _rectangles_overlap(rectangles), "表格合并格发生重叠")
    _need(area == rows * cols, "表格 cells 未完整覆盖；空格也必须明确列出")
    segments = unit.get("segments")
    _need(isinstance(segments, list)
          and [s.get("id") for s in segments if isinstance(s, dict)] == [c["id"] for c in cells],
          "新表格 schema 必须有按 cells 顺序逐格对应的 segments")


def _occurrences(unit):
    occurrences = unit.get("term_occurrences", [])
    _need(isinstance(occurrences, list), "term_occurrences 必须为数组")
    source = unit.get("source", "")
    last_end = 0
    for item in occurrences:
        _object(item, {"start", "end", "term_id", "action"}, "term_occurrence")
        start, end = item.get("start"), item.get("end")
        _need(type(start) is int and type(end) is int and 0 <= start < end <= len(source), "术语实例位置越界")
        _need(start >= last_end, "术语实例须按位置排序且不重叠；完整短语优先")
        last_end = end
        _id(item.get("term_id"), "term_occurrence.term_id")
        _choice(item.get("action"), TERM_ACTIONS, "术语实例 action")


def _placeholder_events(text, placeholders):
    events = []
    for item in placeholders:
        # Lookahead also exposes self-overlapping occurrences such as AA in AAA.
        for match in re.finditer("(?=" + re.escape(item["token"]) + ")", text):
            events.append((match.start(), item))
    return sorted(events, key=lambda event: event[0])


def _placeholder_overlap(events):
    previous_end = -1
    for start, item in events:
        if start < previous_end:
            return True
        previous_end = start + len(item["token"])
    return False


def _paired(events):
    stack = []
    for _, item in events:
        constraints = item.get("constraints", {})
        pair = constraints.get("pair_id")
        if not pair:
            continue
        if constraints["role"] == "start":
            stack.append(pair)
        elif not stack or stack.pop() != pair:
            return False
    return not stack


def _placeholders(unit):
    items = unit.get("inline_placeholders", [])
    _need(isinstance(items, list), "inline_placeholders 必须为数组")
    ids, tokens, pairs = set(), [], defaultdict(list)
    for item in items:
        _object(item, {"id", "token", "source", "constraints"}, "placeholder")
        _id(item.get("id"), "placeholder")
        _need(item["id"] not in ids, "placeholder.id 重复")
        ids.add(item["id"])
        _text(item.get("token"), "placeholder.token")
        _text(item.get("source"), "placeholder.source")
        token = item["token"]
        _need(token in unit.get("source", ""), "placeholder.token 不在 source 中")
        _need(not any(token in old or old in token for old in tokens), "placeholder token 重复或相互包含")
        tokens.append(token)
        constraints = item.get("constraints", {})
        _object(constraints, {"reorder", "pair_id", "role"}, "placeholder.constraints")
        if "reorder" in constraints:
            _need(type(constraints["reorder"]) is bool, "placeholder.reorder 必须为布尔值")
        _need(("pair_id" in constraints) == ("role" in constraints), "placeholder pair_id/role 必须成对提供")
        if "pair_id" in constraints:
            _id(constraints["pair_id"], "placeholder.pair_id")
            _choice(constraints["role"], ("start", "end"), "placeholder.role")
            pairs[constraints["pair_id"]].append(constraints["role"])
    _need(all(sorted(roles) == ["end", "start"] for roles in pairs.values()), "placeholder 每个 pair_id 需唯一 start/end")
    events = _placeholder_events(unit.get("source", ""), items)
    _need(not _placeholder_overlap(events), "原文 placeholder token 实例区间重叠，无法独立恢复对象")
    _need(_paired(events), "原文 placeholder 配对顺序无效")


def _trim_machine(text):
    text = text.rstrip(".,;:!?")
    for left, right in (("(", ")"), ("[", "]")):
        while text.endswith(right) and text.count(right) > text.count(left):
            text = text[:-1]
    return text


def discover_machine_spans(text):
    """Return heuristic candidates, never bindings. No path/short-flag guessing."""
    candidates = []
    # Windows paths and LaTeX commands must not create spurious \n / \t candidates.
    path_regions = [m.span() for m in re.finditer(r"(?:[A-Za-z]:\\|\\\\)[^\r\n\t\"<>]+", text)]
    path_starts = [span[0] for span in path_regions]
    for kind, pattern in _MACHINE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            value = _trim_machine(match.group()) if kind in ("url", "doi", "email") else match.group()
            end = start + len(value)
            if not value:
                continue
            if kind == "escape":
                path_index = bisect_right(path_starts, start) - 1
                if path_index >= 0 and start < path_regions[path_index][1] or start and text[start - 1] == "\\":
                    continue
            candidates.append({"kind": kind, "start": start, "end": end, "text": value})
    found, previous_end = [], -1
    for item in sorted(candidates, key=lambda entry: (entry["start"], -entry["end"])):
        if item["start"] >= previous_end:
            found.append(item)
            previous_end = item["end"]
    return found


def _machine_positions(text, literal, kind):
    """Exact spelling with conservative token boundaries; no normalization."""
    continuation = {
        "url": r"[A-Za-z0-9:/?#@!$&'()*+,;=%._~\-\[\]]",
        "doi": r"[-._;()/:A-Za-z0-9]",
        "email": r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]",
        "path": r"[A-Za-z0-9_./\\~-]",
        "cli_option": r"[A-Za-z0-9_-]",
        "escape": r"(?!)",
    }[kind]
    for match in re.finditer(re.escape(literal), text):
        start, end = match.span()
        if start and re.match(r"[A-Za-z0-9_]", text[start - 1]) and kind != "escape":
            continue
        if kind == "escape":
            previous = start
            while previous and text[previous - 1] == "\\":
                previous -= 1
            if (start - previous) % 2:
                continue
        if end < len(text) and re.match(continuation, text[end]):
            tail = text[end:]
            # A sentence's trailing punctuation is not a URL/path extension.
            suffix = _trim_machine(literal + tail.split(None, 1)[0])
            if suffix != literal:
                continue
        yield start


def _machine_spans(unit):
    spans, source = unit.get("machine_spans", []), unit.get("source", "")
    _need(isinstance(spans, list), "machine_spans 必须为数组")
    ids, regions = set(), []
    for item in spans:
        _object(item, {"id", "start", "end", "kind"}, "machine_span")
        _id(item.get("id"), "machine_span")
        _need(item["id"] not in ids, "machine_span.id 重复")
        ids.add(item["id"])
        _choice(item.get("kind"), MACHINE_KINDS, "machine_span.kind")
        start, end = item.get("start"), item.get("end")
        _need(type(start) is int and type(end) is int and 0 <= start < end <= len(source),
              "machine_span 区间必须为有效半开字符区间")
        _need(not any(start < b and end > a for a, b in regions), "machine_span 区间重叠")
        value = source[start:end]
        _need(value.strip() == value and not any(c in value for c in "\r\n\x00"), "machine_span 含首尾空白/换行/NUL")
        _need(start in _machine_positions(source, value, item["kind"]), "machine_span 截断机器字符串边界")
        regions.append((start, end))


def check_machine_content(unit, target):
    """Bound-token invariants plus unbound differences requiring human diagnosis."""
    source, findings = unit.get("source", ""), []
    bound = {}
    for item in unit.get("machine_spans", []):
        value = source[item["start"]:item["end"]]
        bound.setdefault((item["kind"], value), []).append(item["id"])
    for (kind, value), ids in bound.items():
        before, after = list(_machine_positions(source, value, kind)), list(_machine_positions(target, value, kind))
        if len(before) != len(after):
            findings.append({"kind": "machine_bound_changed", "detail": {
                "type": kind, "text": value, "binding_ids": ids,
                "source_count": len(before), "target_count": len(after),
                "source_positions": before[:8], "target_positions": after[:8]}})
    # A parent segment bundle is checked per child by the ledger; do not duplicate.
    if not unit.get("segments") and unit.get("kind") != "literal":
        before, after = discover_machine_spans(source), discover_machine_spans(target)
        counts = [Counter((s["kind"], s["text"]) for s in side) for side in (before, after)]
        positions = []
        for side in (before, after):
            index = defaultdict(list)
            for span in side:
                key = span["kind"], span["text"]
                if len(index[key]) < 8:
                    index[key].append(span["start"])
            positions.append(index)
        protected = set(unit.get("protected", []))
        for kind, value in sorted(set(counts[0]) | set(counts[1])):
            if counts[0][kind, value] == counts[1][kind, value] or (kind, value) in bound or value in protected:
                continue
            findings.append({"kind": "machine_candidate_changed", "detail": {
                "type": kind, "text": value, "source_count": counts[0][kind, value],
                "target_count": counts[1][kind, value],
                "source_positions": positions[0][kind, value], "target_positions": positions[1][kind, value],
                "status": "candidate", "note": "启发式差异，核对解析、语境和保留约定；不自动改译或绑定"}})
    if not unit.get("segments"):
        for char in sorted(_UNICODE_CORRUPT | _UNICODE_WATCH):
            before, after = source.count(char), target.count(char)
            if not (before or after) or char not in _UNICODE_CORRUPT and before == after:
                continue
            findings.append({"kind": "unicode_source_or_target_corrupt" if char in _UNICODE_CORRUPT else "unicode_control_candidate",
                "detail": {"codepoint": "U+%04X" % ord(char), "source_count": before, "target_count": after,
                           "source_positions": [i for i, c in enumerate(source) if c == char][:8],
                           "target_positions": [i for i, c in enumerate(target) if c == char][:8],
                           "status": "candidate", "risk": "high" if char in _UNICODE_CORRUPT else "review",
                           "note": "定位后核对源字形/文本层与目标；不得自动删除或Unicode归一化"}})
    return findings


def validate_unit_extensions(unit):
    """Validate optional fields including child segments; raises ValueError."""
    _need(isinstance(unit, dict), "unit 必须是对象")
    def validate(node, pages):
        if "document_type" in node:
            _choice(node["document_type"], DOCUMENT_TYPES, "document_type")
        if "source_meta" in node:
            _source_meta(node["source_meta"], pages)
        if "style_meta" in node:
            validate_style(node["style_meta"], "style_meta")
        if "output_meta" in node:
            meta = node["output_meta"]
            _object(meta, {"role", "caption_id", "reference_ids", "custom"}, "output_meta")
            if "role" in meta:
                _text(meta["role"], "output_meta.role")
            if "caption_id" in meta:
                _id(meta["caption_id"], "output_meta.caption_id")
            if "reference_ids" in meta:
                _ids(meta["reference_ids"], "output_meta.reference_ids")
        if "dependencies" in node:
            _ids(node["dependencies"], "dependencies")
            _need(node.get("id") not in node["dependencies"], "块不能依赖自身")
        _occurrences(node)
        _placeholders(node)
        _machine_spans(node)
        if "structure" in node:
            _table(node, node["structure"])
    validate(unit, unit.get("pdf_pages"))
    for segment in unit.get("segments", []):
        validate(segment, unit.get("pdf_pages"))


def _case_sensitive(term):
    if "case_sensitive" in term:
        return term["case_sensitive"]
    letters = "".join(c for c in term["en"] if c.isalpha())
    return bool(letters and letters.isupper())


@lru_cache(maxsize=8192)
def _pattern(forms, sensitive):
    alternatives = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + alternatives + r")(?![A-Za-z0-9_])",
                      0 if sensitive else re.IGNORECASE)


def term_pattern(term):
    """Return a pattern for candidate discovery; a surface hit is not a binding."""
    if isinstance(term, str):  # For legacy callers only; pass the full term for DNT/case.
        term = {"en": term}
    return _pattern(tuple([term["en"]] + term.get("aliases", [])), _case_sensitive(term))


def validate_policy_extensions(policy):
    if "document_type" in policy:
        _choice(policy["document_type"], DOCUMENT_TYPES, "policy.document_type")
    for term in policy.get("glossary", []):
        _choice(term.get("strategy", "translate"), TERM_ACTIONS, "术语 strategy")
        if "case_sensitive" in term:
            _need(type(term["case_sensitive"]) is bool, "term.case_sensitive 必须为布尔值")
        aliases = term.get("aliases", [])
        _need(isinstance(aliases, list) and all(isinstance(a, str) and a.strip() for a in aliases), "term.aliases 必须为非空字符串数组")
        _need(len(set(aliases)) == len(aliases) and term["en"] not in aliases, "term.aliases 重复")


def term_matches(unit, term):
    """True only for explicit concept bindings, including child segments."""
    return (term["id"] in unit.get("term_ids", [])
            or any(o.get("term_id") == term["id"] for o in unit.get("term_occurrences", []))
            or any(term_matches(s, term) for s in unit.get("segments", [])))


def validate_term_bindings(unit, policy):
    terms = {t["id"]: t for t in policy["glossary"]}
    for node in [unit] + unit.get("segments", []):
        for term_id in node.get("term_ids", []):
            _need(term_id in terms, "未知绑定术语：" + term_id)
        for occurrence in node.get("term_occurrences", []):
            term_id = occurrence["term_id"]
            _need(term_id in terms, "未知实例术语：" + term_id)
            text = node["source"][occurrence["start"]:occurrence["end"]]
            _need(term_pattern(terms[term_id]).fullmatch(text) is not None,
                  "术语实例原文不匹配 en/aliases：" + term_id)


def check_term_usage(unit, target, policy):
    """Check explicitly bound concepts locally; callers check each segment too."""
    findings = []
    for term in policy["glossary"]:
        occurrences = [o for o in unit.get("term_occurrences", []) if o["term_id"] == term["id"]]
        if not occurrences and term["id"] not in unit.get("term_ids", []):
            continue
        actions = Counter(o["action"] for o in occurrences)
        if not occurrences:
            strategy = term.get("strategy", "translate")
            if strategy == "first_use":
                findings.append({"kind": "term_first_use_unresolved", "detail": term["id"]})
                continue
            actions[strategy] = 1
        required_zh = actions["translate"] + actions["first_use"]
        if required_zh and target.count(term["zh"]) < required_zh:
            findings.append({"kind": "term_missing", "detail": {"id": term["id"], "minimum": required_zh}})
        retained = Counter(unit["source"][o["start"]:o["end"]] for o in occurrences
                           if o["action"] in ("retain", "first_use"))
        if not occurrences and actions["retain"]:
            retained.update(m.group() for m in term_pattern(term).finditer(unit["source"]))
            if not retained:
                findings.append({"kind": "term_source_missing", "detail": term["id"]})
        for literal, count in retained.items():
            # Exact case is intentional for retained text, even if discovery folds case.
            pattern = _pattern((literal,), True)
            if len(pattern.findall(target)) < count:
                findings.append({"kind": "term_retained_changed", "detail": {"id": term["id"], "text": literal, "minimum": count}})
        for forbidden in term.get("forbidden", []):
            if forbidden in target:
                findings.append({"kind": "term_forbidden", "detail": {"id": term["id"], "text": forbidden}})
    return findings


def check_unit_extensions(unit, result):
    """Check placeholder integrity. Metadata is owned by the source inventory."""
    findings = []
    for key in ("source_meta", "style_meta", "output_meta", "dependencies", "term_occurrences", "inline_placeholders", "machine_spans", "document_type"):
        if key in result and result[key] != unit.get(key):
            findings.append({"kind": "metadata_changed", "detail": key})
    items = unit.get("inline_placeholders", [])
    source, target = unit.get("source", ""), result.get("target", "")
    source_events = _placeholder_events(source, items)
    target_events = _placeholder_events(target, items)
    if _placeholder_overlap(source_events):
        findings.append({"kind": "source_placeholder_overlap", "detail": "旧原文行内 token 实例区间重叠；须建立源修订项目重建映射，不能仅改译文掩盖"})
    for item in items:
        if source.count(item["token"]) != target.count(item["token"]):
            findings.append({"kind": "placeholder_count", "detail": item["id"]})
    fixed = {p["id"] for p in items if not p.get("constraints", {}).get("reorder", True)}
    if [p["id"] for _, p in source_events if p["id"] in fixed] != [p["id"] for _, p in target_events if p["id"] in fixed]:
        findings.append({"kind": "placeholder_order", "detail": "不可重排的行内对象顺序改变"})
    if _placeholder_overlap(target_events):
        findings.append({"kind": "placeholder_overlap", "detail": "译文行内 token 实例区间重叠，无法独立恢复对象"})
    if not _paired(target_events):
        findings.append({"kind": "placeholder_pair", "detail": "行内标记配对/嵌套损坏"})
    findings.extend(check_machine_content(unit, target))
    return findings
