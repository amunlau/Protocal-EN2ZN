#!/usr/bin/env python3
"""Local translation ledger. No model/API calls; approvals must come from a real user."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from check_environment import configure_stdio
from document_model import (validate_unit_extensions, validate_policy_extensions,
                            validate_term_bindings, term_pattern, term_matches,
                            check_term_usage, check_unit_extensions)

DB_NAME = "translation.sqlite3"
SCHEMA = 4
CHECKS_REVISION = 6
STATES = ("pending", "in_progress", "translated", "needs_review", "reviewed")
TM_LIMIT = 5
TM_SCAN_LIMIT = 32
TM_TEXT_LIMIT = 2000
TM_CONTEXT_BUDGET = 12000
TM_CANDIDATE_BYTES = 512 * 1024
TM_SCAN_BYTES = 4 * 1024 * 1024
BATCH_LIMIT = 32
BATCH_BYTES = 8 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    atomic_json_stream(path, value=value)


def atomic_json_stream(path, *, value=None, items=None):
    """Write one object or a streamed array, with one atomic replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Extending a legal destination basename can exceed Windows path limits or
    # POSIX NAME_MAX, so use a short independent name in the same directory.
    descriptor, name = tempfile.mkstemp(prefix=".t-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None  # The stream now owns the descriptor.
        with stream as f:
            if items is None:
                json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            else:
                f.write("[\n")
                separator = ""
                for item in items:
                    f.write(separator)
                    f.write(json.dumps(item, ensure_ascii=False, allow_nan=False))
                    separator = ",\n"
                f.write("\n]")
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary.exists():
            temporary.unlink()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def page_count(source):
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as e:
        raise ValueError("PDF 初始化需要 pypdf；请在所选 Python 环境准备该依赖。") from e
    try:
        with open(source, "rb") as f:
            reader = PdfReader(f)
            need(not reader.is_encrypted, "源 PDF 加密；先取得可合法读取的副本，再建立项目。")
            return len(reader.pages)
    except PyPdfError as exc:
        raise ValueError("PDF 结构无法解析：" + str(source)) from exc


def validate_policy(policy):
    need(isinstance(policy, dict), "policy 必须是对象")
    for key in ("source_version", "scope", "normative", "style", "glossary"):
        need(key in policy, "policy 缺少 " + key)
    for key in ("source_version", "scope"):
        need(isinstance(policy[key], str) and policy[key].strip(), key + " 必须是非空字符串")
    for key in ("normative", "style"):
        need(isinstance(policy[key], dict), key + " 必须是对象")
    need(isinstance(policy["glossary"], list), "glossary 必须是数组")
    ids = set()
    for term in policy["glossary"]:
        need(isinstance(term, dict), "术语必须是对象")
        for key in ("id", "en", "zh", "sense", "scope", "status", "source"):
            need(isinstance(term.get(key), str) and term[key].strip(), "术语缺少 " + key)
        need(term["id"] not in ids, "术语 ID 重复")
        need(term["status"] in ("provisional", "confirmed"), "candidate 必须先核实，不能成为活动术语")
        need(isinstance(term.get("forbidden", []), list) and all(isinstance(x, str) and x.strip() for x in term.get("forbidden", [])), "forbidden 必须是非空字符串数组")
        ids.add(term["id"])
    need("outputs" not in policy and "output_policy" not in policy,
         "输出格式/排版约定请使用独立 output-policy，不放入语义 policy")
    validate_policy_extensions(policy)


def validate_unit(unit, pages):
    need(isinstance(unit, dict), "内容块必须是对象")
    for key in ("id", "section", "kind", "source"):
        need(isinstance(unit.get(key), str) and unit[key].strip(), "内容块缺少 " + key)
    need(re.fullmatch(r"[A-Za-z0-9_.-]+", unit["id"]), "块 ID 仅用字母、数字、点、下划线或连字符")
    need(type(unit.get("order")) is int and unit["order"] >= 0, "order 必须是非负整数")
    positions = unit.get("pdf_pages")
    need(isinstance(positions, list) and positions, "pdf_pages 必须非空")
    need(all(type(p) is int and 1 <= p <= pages for p in positions), "PDF 页序越界")
    need(isinstance(unit.get("printed_pages", []), list), "printed_pages 必须为数组")
    for key in ("protected", "term_ids"):
        need(isinstance(unit.get(key, []), list) and all(isinstance(x, str) for x in unit.get(key, [])), key + " 必须为字符串数组")
    for token in unit.get("protected", []):
        need(token and token in unit["source"], "保护串必须存在于 source：" + token)
    if "segments" in unit:
        segments = unit["segments"]
        need(isinstance(segments, list) and segments, "segments 必须是非空数组")
        ids = set()
        for segment in segments:
            need(isinstance(segment, dict), "segment 必须是对象")
            sid = segment.get("id")
            need(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", sid), "segment.id 无效")
            need(sid not in ids, "segment.id 重复")
            ids.add(sid)
            need(isinstance(segment.get("source"), str), "segment.source 必须是字符串；空单元格用空串")
            need("kind" not in segment or isinstance(segment["kind"], str) and segment["kind"].strip(), "segment.kind 必须是非空字符串")
            need("segments" not in segment, "segments 不允许嵌套")
            for key in ("protected", "term_ids"):
                need(isinstance(segment.get(key, []), list) and all(isinstance(x, str) for x in segment.get(key, [])), "segment." + key + " 必须为字符串数组")
            for token in segment.get("protected", []):
                need(token and token in segment["source"], "segment 保护串必须存在于 source")
        need(unit["source"] == "\n".join(s["source"] for s in segments), "source 必须按 segments 顺序用换行连接，防止双份原文不一致")
    validate_unit_extensions(unit)


def get_meta(db, key):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, encoded(value)))


def event(db, action, detail):
    db.execute("INSERT INTO events(at,action,detail) VALUES (?,?,?)", (now(), action, encoded(detail)))


def connection(root):
    path = Path(root).resolve() / DB_NAME
    need(path.is_file(), "未找到项目数据库；不要在恢复任务中重新初始化。")
    db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        version = get_meta(db, "schema")
        need(version in (1, 2, 3, SCHEMA), "数据库版本不支持；保留原库后迁移。")
        if version != SCHEMA:
            saved = backup(db, root)
            with transaction(db):
                need(get_meta(db, "schema") == version, "迁移期间项目被其他进程修改，请重试")
                if version == 1:
                    create_review_storage(db)
                    for row in db.execute("SELECT id,result_hash,review FROM units WHERE review IS NOT NULL"):
                        review = json.loads(row["review"])
                        archive_review(db, row["id"], row["result_hash"], review)
                if version < 3:
                    create_output_storage(db)
                    put_meta(db, "output_policy", {"formats": ["docx"]})
                    put_meta(db, "output_revision", 0)
                create_memory_storage(db)
                put_meta(db, "schema", SCHEMA)
                event(db, "schema_upgrade", {"from": version, "to": SCHEMA, "backup": saved["backup"]})
        return db
    except BaseException:
        db.close()
        raise


def create_review_storage(db):
    db.execute("CREATE TABLE review_history(id INTEGER PRIMARY KEY, unit_id TEXT NOT NULL, result_hash TEXT NOT NULL, at TEXT NOT NULL, review TEXT NOT NULL)")
    db.execute("CREATE INDEX review_history_unit ON review_history(unit_id,id)")
    db.execute("CREATE INDEX units_pending ON units(ordinal) WHERE state='pending'")
    db.execute("CREATE INDEX units_review_queue ON units(ordinal) WHERE state IN ('needs_review','translated','in_progress')")
    db.execute("CREATE INDEX units_sample ON units(ordinal) WHERE sample=1")


def create_output_storage(db):
    db.execute("CREATE TABLE artifacts(id TEXT PRIMARY KEY, role TEXT NOT NULL, format TEXT NOT NULL, record TEXT NOT NULL)")
    db.execute("CREATE INDEX artifacts_role_format ON artifacts(role,format)")


def create_memory_storage(db):
    """Derived retrieval keys only; source/review/sample identities never change."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(units)")}
    for name in ("text_hash", "anchor_hash", "match_kind", "match_mode"):
        if name not in columns:
            db.execute("ALTER TABLE units ADD COLUMN " + name + " TEXT NOT NULL DEFAULT ''")
    batch = []
    for row in db.execute("SELECT id,source FROM units"):
        batch.append((*memory_keys(json.loads(row["source"])), row["id"]))
        if len(batch) >= 512:
            db.executemany("UPDATE units SET text_hash=?,anchor_hash=?,match_kind=?,match_mode=? WHERE id=?", batch)
            batch.clear()
    if batch:
        db.executemany("UPDATE units SET text_hash=?,anchor_hash=?,match_kind=?,match_mode=? WHERE id=?", batch)
    for key in ("text_hash", "anchor_hash"):
        db.execute("CREATE INDEX IF NOT EXISTS memory_" + key + " ON units(match_kind,match_mode," + key + ",ordinal) WHERE state='reviewed'")


def archive_review(db, unit_id, result_hash, review):
    db.execute("INSERT INTO review_history(unit_id,result_hash,at,review) VALUES (?,?,?,?)", (unit_id, result_hash, now(), encoded(review)))


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def initialize(root, source, inventory, policy, require_sample=False, outputs=None):
    root, source = Path(root).resolve(), Path(source).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / DB_NAME
    need(not path.exists(), "项目已存在，请 resume，禁止覆盖。")
    validate_policy(policy)
    output_policy = validate_output_policy(outputs if outputs is not None else {"formats": ["docx"]})
    source_hash = file_hash(source)
    pages = page_count(source)
    need(pages > 0, "源 PDF 无页面")
    count = 0
    glossary_ids = {term["id"] for term in policy["glossary"]}
    # Reserve the name exclusively; an interrupted initialization is never overwritten.
    with path.open("xb"):
        pass
    db = sqlite3.connect(str(path), isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        with transaction(db):
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE units(id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, source TEXT NOT NULL, source_hash TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, result TEXT, result_hash TEXT, review TEXT, revision INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE history(id INTEGER PRIMARY KEY, unit_id TEXT, at TEXT, result TEXT, result_hash TEXT, policy_revision INTEGER)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, at TEXT, action TEXT, detail TEXT)")
            create_review_storage(db)
            create_output_storage(db)
            create_memory_storage(db)
            stat = source.stat()
            values = {"schema": SCHEMA, "created": now(), "source": {"path": str(source), "sha256": source_hash, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "pages": pages}, "policy": policy, "policy_revision": 1, "governance_revision": 1, "require_sample": pages >= 20 or require_sample, "sample": None, "inventory_sha256": file_hash(inventory)}
            for key, value in values.items():
                put_meta(db, key, value)
            put_meta(db, "output_policy", output_policy)
            put_meta(db, "output_revision", 1 if outputs is not None else 0)
            with Path(inventory).open(encoding="utf-8-sig") as f:
                for line in f:
                    if not line.strip():
                        continue
                    unit = json.loads(line)
                    validate_unit(unit, pages)
                    bound = set(unit.get("term_ids", []))
                    for segment in unit.get("segments", []):
                        bound.update(segment.get("term_ids", []))
                    need(bound <= glossary_ids, "内容块引用未知术语 ID：" + str(sorted(bound - glossary_ids)))
                    validate_term_bindings(unit, policy)
                    try:
                        db.execute("INSERT INTO units(id,ordinal,source,source_hash,state,text_hash,anchor_hash,match_kind,match_mode) VALUES (?,?,?,?, 'pending',?,?,?,?)", (unit["id"], unit["order"], encoded(unit), digest(unit), *memory_keys(unit)))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("内容块 ID 或 order 重复") from exc
                    count += 1
            need(count, "清单为空")
            for row in db.execute("SELECT id,source FROM units"):
                unit = json.loads(row["source"])
                for dependency in unit.get("dependencies", []):
                    need(isinstance(dependency, str) and dependency != row["id"] and
                         db.execute("SELECT 1 FROM units WHERE id=?", (dependency,)).fetchone(),
                         "依赖必须指向清单内其他块：" + str(dependency))
            need(file_hash(inventory) == values["inventory_sha256"], "初始化期间清单发生变化，请保留失败项目并在新目录重试")
            need(file_hash(source) == values["source"]["sha256"], "初始化期间源 PDF 发生变化")
            event(db, "init", {"units": count, "pages": pages})
    finally:
        db.close()
    return {"project": str(root), "pages": pages, "units": count, "sample_required": pages >= 20 or require_sample}


def verify_source(db, source=None, full=False):
    saved = get_meta(db, "source")
    path = Path(source).resolve() if source else Path(saved["path"])
    need(path.is_file(), "源 PDF 不在原位置；resume --source 指定相同内容的新路径。")
    stat = path.stat()
    if full or str(path) != saved["path"] or stat.st_size != saved["size"] or stat.st_mtime_ns != saved["mtime_ns"]:
        need(file_hash(path) == saved["sha256"], "源 PDF 内容已变化；禁止混入旧项目，需新建修订项目并核对迁移。")
        saved.update(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        put_meta(db, "source", saved)


def unit_row(db, unit_id):
    row = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    need(row is not None, "未知内容块：" + unit_id)
    return row


def legacy_term_matches(unit, term):
    """Only for old signatures and conservative change impact, not translation checks."""
    bound = term["id"] in unit.get("term_ids", []) or any(term["id"] in s.get("term_ids", []) for s in unit.get("segments", []))
    return bound or re.search(r"(?<![A-Za-z0-9_])" + re.escape(term["en"]) + r"(?![A-Za-z0-9_])", unit["source"], re.I) is not None


def sample_signature(db, legacy=False):
    rows = db.execute("SELECT id,source,result_hash FROM units WHERE sample=1 ORDER BY ordinal").fetchall()
    policy = get_meta(db, "policy")
    sources = [json.loads(r["source"]) for r in rows]
    matcher = legacy_term_matches if legacy else term_matches
    applicable = [t for t in policy["glossary"] if any(matcher(unit, t) for unit in sources)]
    baseline = {k: v for k, v in policy.items() if k != "glossary"}
    baseline["glossary"] = applicable
    value = {"source": get_meta(db, "source")["sha256"], "inventory": get_meta(db, "inventory_sha256"), "policy": baseline, "governance_revision": get_meta(db, "governance_revision"), "units": [(r["id"], r["result_hash"]) for r in rows]}
    if not legacy:
        value["assets"] = get_meta(db, "assets")
    return digest(value)


def portable_sample_path(value):
    """Keep saved relative paths safe to move between Windows and Linux."""
    need(isinstance(value, str) and value, "样本相对路径必须是非空字符串")
    path = PurePosixPath(value)
    need(bool(path.parts) and not path.is_absolute() and not PureWindowsPath(value).drive and
         "\\" not in value and value == path.as_posix() and ".." not in path.parts,
         "样本路径必须为项目内的规范相对路径，使用 / 分隔")
    for part in path.parts:
        need(not re.search(r'[<>:"|?*\x00-\x1f]', part) and not part.endswith((".", " ")),
             "样本路径含 Windows 不支持的字符或末尾点/空格")
        stem = part.split(".", 1)[0].upper()
        need(stem not in ("CON", "PRN", "AUX", "NUL") and
             not re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem), "样本路径使用 Windows 保留名称")
    return path


def project_file(root, relative):
    path = (Path(root).resolve() / portable_sample_path(relative)).resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError("附件路径越出项目目录") from exc
    need(path.is_file(), "项目附件不存在：" + str(relative))
    return path


def file_record(root, path):
    path = Path(path).resolve()
    try:
        relative = path.relative_to(Path(root).resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("产物/证据必须位于项目目录内") from exc
    project_file(root, relative)
    return {"path": relative, "sha256": file_hash(path)}


def verify_file_record(root, record):
    need(isinstance(record, dict) and isinstance(record.get("sha256"), str) and
         re.fullmatch(r"[0-9a-f]{64}", record["sha256"]), "附件缺少有效 sha256")
    path = project_file(root, record.get("path"))
    need(file_hash(path) == record["sha256"], "附件已改变：" + record["path"])
    return path


def validate_output_policy(value):
    need(isinstance(value, dict), "输出约定必须为对象")
    formats = value.get("formats")
    need(isinstance(formats, list) and formats and all(f in ("docx", "pdf") for f in formats) and
         len(set(formats)) == len(formats), "formats 必须为不重复的 docx/pdf 数组")
    for key in ("styles", "layout"):
        if key in value:
            need(isinstance(value[key], dict), key + " 必须为对象")
    return dict(value, formats=sorted(formats))


def output_policy(db):
    return get_meta(db, "output_policy") or {"formats": ["docx"]}


get_output_policy = output_policy


def output_signature(db):
    return digest({"policy": output_policy(db), "revision": get_meta(db, "output_revision") or 0})


def update_output_policy(db, value, reason):
    value = validate_output_policy(value)
    need(reason.strip(), "输出约定变更必须说明原因")
    old = output_policy(db)
    if old == value:
        return {"unchanged": True, "output_revision": get_meta(db, "output_revision") or 0}
    revision = (get_meta(db, "output_revision") or 0) + 1
    put_meta(db, "output_policy", value)
    put_meta(db, "output_revision", revision)
    # Keep approval/history as evidence; signatures make its old scope stale.
    event(db, "output_policy", {"old": old, "new": value, "revision": revision, "reason": reason})
    return {"output_revision": revision, "translation_reviews_unchanged": True,
            "sample_layout_reconfirmation": bool(get_meta(db, "require_sample")), "artifacts_stale": True}


def content_signature(db, role="full"):
    need(role in ("sample", "full"), "产物 role 必须为 sample/full")
    if role == "sample":
        return sample_signature(db)
    h = hashlib.sha256()
    h.update(encoded({"source": get_meta(db, "source")["sha256"],
                      "inventory": get_meta(db, "inventory_sha256"),
                      "policy": get_meta(db, "policy"), "assets": get_meta(db, "assets")}).encode("utf-8"))
    for row in db.execute("SELECT id,source_hash,result_hash FROM units ORDER BY ordinal"):
        h.update(b"\n")
        h.update(encoded(list(row)).encode("utf-8"))
    return h.hexdigest()


def inspected_artifact(root, path):
    record = file_record(root, path)
    path = project_file(root, record["path"])
    fmt = path.suffix.lower().lstrip(".")
    need(fmt in ("docx", "pdf"), "产物只支持真实 DOCX 或 PDF")
    try:
        if fmt == "docx":
            with zipfile.ZipFile(path) as z:
                for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                    info = z.getinfo(part)
                    need(info.file_size <= 64 * 1024 * 1024, "DOCX 核心 XML 过大，请分章处理")
                content_types = ET.fromstring(z.read("[Content_Types].xml"))
                relationships = ET.fromstring(z.read("_rels/.rels"))
                body = ET.fromstring(z.read("word/document.xml"))
                need(body.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document" and
                     body.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body") is not None,
                     "DOCX Word 正文无效")
                need(any(x.get("PartName") == "/word/document.xml" for x in content_types) and
                     any(x.get("Type", "").endswith("/officeDocument") and
                         x.get("Target", "").lstrip("/") == "word/document.xml" for x in relationships),
                     "DOCX 缺少正确的正文类型或包关系")
        else:
            record["page_count"] = page_count(path)
            need(record["page_count"] > 0, "PDF 产物无页面")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("产物结构无法解析：" + str(path)) from exc
    need(file_hash(path) == record["sha256"], "检查期间产物发生变化")
    return dict(record, format=fmt)


def verify_assets(db, root):
    manifest = get_meta(db, "assets")
    if not manifest:
        return
    verify_file_record(root, manifest["manifest"])
    for item in manifest["items"]:
        verify_file_record(root, item)


def register_assets(db, root, manifest_path):
    record = file_record(root, manifest_path)
    value = read_json(project_file(root, record["path"]))
    items = value.get("assets") if isinstance(value, dict) else None
    need(isinstance(items, list) and items, "资产清单需非空 assets 数组")
    ids = set()
    for item in items:
        need(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] and item["id"] not in ids,
             "资产 ID 缺失或重复")
        ids.add(item["id"])
        verify_file_record(root, item)
    saved = {"manifest": record, "items": items}
    old = get_meta(db, "assets")
    if old == saved:
        return {"unchanged": True}
    need(old is None and not db.execute("SELECT 1 FROM units WHERE result IS NOT NULL LIMIT 1").fetchone(),
         "源资产须在翻译前绑定；已用资产变化请建立源修订项目")
    put_meta(db, "assets", saved)
    verify_assets(db, root)
    event(db, "assets", saved)
    return {"assets": len(items)}


def gate(db, root):
    if not get_meta(db, "require_sample"):
        return "not_required"
    sample = get_meta(db, "sample")
    if not sample:
        return "sample_draft"
    if not sample_reviews_current(db):
        return "sample_draft"
    try:
        verify_assets(db, root)
        legacy = "artifacts" not in sample
        if legacy:
            need((get_meta(db, "output_revision") or 0) == 0 and output_policy(db)["formats"] == ["docx"],
                 "旧样本不覆盖新输出约定")
            verify_file_record(root, {"path": sample["artifact"], "sha256": sample["artifact_sha256"]})
        else:
            need(sample["output_signature"] == output_signature(db), "样本输出约定变化")
            need(sorted(a["format"] for a in sample["artifacts"]) == output_policy(db)["formats"], "样本格式不完整")
            for item in sample["artifacts"]:
                verify_file_record(root, item)
            need(checked_sample_files(db, root, sample["artifacts"]), "样本产物检查缺失或证据变化")
        need(sample["signature"] == sample_signature(db, legacy=legacy), "样本内容约定变化")
    except (ValueError, TypeError, KeyError, OSError):
        return "sample_changed"
    return "approved" if sample.get("approval") else "awaiting_approval"


def allow_unit(db, root, row):
    need(row["sample"] or gate(db, root) in ("approved", "not_required"), "样本尚未有效确认，禁止处理非样本译文。")


def select_sample(db, ids):
    need(ids and len(ids) == len(set(ids)), "样本 ID 为空或重复")
    for unit_id in ids:
        unit_row(db, unit_id)
    current = {r[0] for r in db.execute("SELECT id FROM units WHERE sample=1")}
    if current == set(ids):
        return {"unchanged": True}
    db.execute("UPDATE units SET sample=0")
    db.executemany("UPDATE units SET sample=1 WHERE id=?", [(i,) for i in ids])
    put_meta(db, "require_sample", True)
    put_meta(db, "sample", None)
    event(db, "select_sample", ids)
    return {"sample_ids": ids}


NUMBER = re.compile(r"(?<![A-Za-z0-9_])[+\-−－]?(?:0[xX][0-9A-Fa-f]+|(?:\d+(?:[.,]\d+)*|[.,]\d+)(?:[eE][+\-−－]?\d+)?)")
SCRIPT_NUMBER = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋]+")
TECH_SYMBOL = re.compile(r"<=|>=|!=|==|<<|>>|[<>=≤≥≠±∓×÷∈∉∧∨⊕¬→←↔]")
MODAL = re.compile(r"(?<![A-Za-z0-9_])(?:shall|should|must|may|might|can|could|will|would|ought)(?![A-Za-z0-9_])", re.I)
NEGATION = re.compile(r"(?<![A-Za-z0-9_])(?:not|never|no|neither|nor|without|unless|except|only)(?![A-Za-z0-9_])", re.I)


def critical_text(text):
    special = {"cannot": "can not", "can't": "can not", "won't": "will not", "shan't": "shall not"}
    text = re.sub(r"\b(?:cannot|can['’]t|won['’]t|shan['’]t)\b",
                  lambda m: special[m.group().lower().replace("’", "'")], text, flags=re.I)
    return re.sub(r"(?<=[A-Za-z])n['’]t\b", " not", text, flags=re.I)


def memory_keys(unit):
    """Exact text and a narrowly defined candidate key, never an equivalence test."""
    source = unit["source"]
    anchor = NEGATION.sub(" ", MODAL.sub(" ", SCRIPT_NUMBER.sub(" ", NUMBER.sub(" ", critical_text(source)))))
    return digest(source), digest(" ".join(anchor.split())), unit["kind"], "segments" if "segments" in unit else "plain"


def semantic_signature(unit, policy):
    value = {k: v for k, v in policy.items() if k != "glossary"}
    value["glossary"] = [term for term in policy.get("glossary", []) if term_matches(unit, term)]
    return digest(value)


def reviewed_payload(row, policy, policy_revision=None, *, allow_open_minor=False):
    """Read-only strong validation of one candidate; no mutation or review upgrade."""
    try:
        if row["state"] != "reviewed" or not row["result"] or not row["review"]:
            return None
        unit, result, review = json.loads(row["source"]), json.loads(row["result"]), json.loads(row["review"])
        if digest(unit) != row["source_hash"] or digest(result) != row["result_hash"] or not isinstance(review, dict):
            return None
        revision = review.get("policy_revision")
        if type(revision) is not int or revision < 1 or (policy_revision is not None and revision > policy_revision):
            return None
        signature = semantic_signature(unit, policy)
        if review.get("semantic_signature", signature) != signature:
            return None
        findings = check_result(unit, result, policy)
        resolutions = review.get("resolutions", {})
        if (review.get("result_sha256") != row["result_hash"] or
                not all(isinstance(review.get(k), str) and review[k].strip() for k in ("semantic", "chinese", "structure")) or
                not isinstance(resolutions, dict) or
                any(not isinstance(resolutions.get(f["id"]), str) or not resolutions[f["id"]].strip() for f in findings) or
                any(i["status"] == "open" and (not allow_open_minor or i["severity"] != "minor")
                    for i in result.get("issues", [])) or
                "〔待核" in result["target"]):
            return None
        return {"unit": unit, "result": result, "review": review, "semantic_signature": signature,
                "legacy_semantic_binding": "semantic_signature" not in review}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def sample_reviews_current(db):
    """Validate only selected sample reviews, including newly introduced checks."""
    policy, revision = get_meta(db, "policy"), get_meta(db, "policy_revision")
    found = False
    for row in db.execute("SELECT * FROM units WHERE sample=1"):
        found = True
        if reviewed_payload(row, policy, revision, allow_open_minor=True) is None:
            return False
    return found


def memory_differences(unit, candidate):
    def delta(left, right):
        return {"current_only": dict(left - right), "candidate_only": dict(right - left)}
    differences = {
        "numbers": delta(number_tokens(unit["source"]), number_tokens(candidate["source"])),
        "modal": delta(Counter(x.lower() for x in MODAL.findall(critical_text(unit["source"]))), Counter(x.lower() for x in MODAL.findall(critical_text(candidate["source"])))),
        "negation_or_condition": delta(Counter(x.lower() for x in NEGATION.findall(critical_text(unit["source"]))), Counter(x.lower() for x in NEGATION.findall(critical_text(candidate["source"])))),
        "technical_symbols": delta(Counter(TECH_SYMBOL.findall(unit["source"])), Counter(TECH_SYMBOL.findall(candidate["source"])))
    }
    differences = {key: value for key, value in differences.items() if any(value.values())}
    for key in ("protected", "term_ids", "term_occurrences", "inline_placeholders", "machine_spans", "structure", "document_type"):
        if unit.get(key) != candidate.get(key):
            differences[key] = "绑定/结构不同，须逐项核对，不能直接复用"
    if unit.get("segments") != candidate.get("segments"):
        differences["segments"] = "子段/单元格原文或绑定不同，不能照搬分段结果"
    return differences


def memory_candidates(db, row, policy, gate_state):
    """Bounded indexed suggestions. Always reference-only, including exact text."""
    unit = json.loads(row["source"])
    response = {"reference_only": True, "candidates": [], "conflicts": [], "scan_limit": TM_SCAN_LIMIT,
                "text_budget": TM_CONTEXT_BUDGET, "scope": "full" if gate_state in ("approved", "not_required") else "sample"}
    if response["scope"] == "sample" and not row["sample"]:
        return dict(response, blocked="样本未批准，不检索非样本翻译候选")
    exact, anchor, kind, mode = memory_keys(unit)
    revision = get_meta(db, "policy_revision")
    queries = [("text_hash", exact, "exact_source")]
    if kind not in ("code", "formula", "identifier", "literal"):
        queries.append(("anchor_hash", anchor, "critical_token_variant"))
    seen, valid, scanned, loaded_bytes, oversized = set(), [], 0, 0, 0
    for field, key, match in queries:
        scope = " AND sample=1" if response["scope"] == "sample" else ""
        extra, parameters = (" AND text_hash!=?", [exact]) if field == "anchor_hash" else ("", [])
        query = ("SELECT id,length(CAST(source AS BLOB))+coalesce(length(CAST(result AS BLOB)),0)+coalesce(length(CAST(review AS BLOB)),0) AS payload_bytes FROM units WHERE state='reviewed' AND match_kind=? AND match_mode=? AND " + field +
                 "=? AND id!=?" + scope + extra + " ORDER BY ordinal LIMIT ?")
        for selected in db.execute(query, (kind, mode, key, row["id"], *parameters, TM_SCAN_LIMIT - scanned)):
            if selected["id"] in seen:
                continue
            seen.add(selected["id"])
            scanned += 1
            if selected["payload_bytes"] > TM_CANDIDATE_BYTES or loaded_bytes + selected["payload_bytes"] > TM_SCAN_BYTES:
                oversized += 1
                continue
            loaded_bytes += selected["payload_bytes"]
            candidate = unit_row(db, selected["id"])
            checked = reviewed_payload(candidate, policy, revision)
            if not checked:
                continue
            other = checked["unit"]
            other_keys = memory_keys(other)
            if other_keys[2:] != (kind, mode) or other_keys[0 if field == "text_hash" else 1] != key:
                continue
            differences = memory_differences(unit, other)
            valid.append((candidate, checked, match, differences))
        if scanned >= TM_SCAN_LIMIT:
            break
    groups = {}
    for candidate, checked, match, differences in valid:
        if match == "exact_source":
            groups.setdefault(digest(checked["result"]["target"]), []).append(candidate["id"])
    if len(groups) > 1:
        response["conflicts"].append({"kind": "same_source_different_targets", "groups": list(groups.values()),
                                      "note": "可能是上下文差异或不一致；不能自动统一"})
    # Prefer matching context and representative translations without scanning beyond
    # the fixed SQL window. Conflicts still include all valid entries in that window.
    valid.sort(key=lambda item: (item[2] != "exact_source", bool(item[3]), item[1]["unit"]["section"] != unit["section"], item[0]["ordinal"]))
    representatives, duplicate_targets, target_hashes = [], [], set()
    for item in valid:
        key = digest(item[1]["result"]["target"])
        (duplicate_targets if key in target_hashes else representatives).append(item)
        target_hashes.add(key)
    budget = TM_CONTEXT_BUDGET
    for candidate, checked, match, differences in (representatives + duplicate_targets)[:TM_LIMIT]:
        other, result, review = checked["unit"], checked["result"], checked["review"]
        source = other["source"][:min(TM_TEXT_LIMIT, budget)]
        budget -= len(source)
        target = result["target"][:min(TM_TEXT_LIMIT, budget)]
        budget -= len(target)
        response["candidates"].append({"id": candidate["id"], "match": match, "source": source, "target": target,
            "truncated": len(source) != len(other["source"]) or len(target) != len(result["target"]),
            "section": other["section"], "pdf_pages": other["pdf_pages"], "kind": kind, "content_mode": mode,
            "differences": differences, "source_sha256": candidate["source_hash"], "result_sha256": candidate["result_hash"],
            "review_sha256": digest(review), "review_policy_revision": review["policy_revision"],
            "active_policy_revision": revision, "semantic_signature": checked["semantic_signature"],
            "legacy_semantic_binding": checked["legacy_semantic_binding"], "reviewed_at": review.get("at"),
            "review_checks_revision": review.get("checks_revision"), "candidate_checks_revision": CHECKS_REVISION,
            "reference_only": True})
        if budget <= 0:
            break
    response.update(scanned=scanned, loaded_bytes=loaded_bytes, oversized_omitted=oversized,
                    truncated=scanned >= TM_SCAN_LIMIT or len(valid) > len(response["candidates"]) or bool(oversized),
                    notice="精确文本也不保证上下文相同；候选不授予译文、审校或样本批准状态")
    while len(encoded(response).encode("utf-8")) > 64 * 1024:
        if response["candidates"]:
            response["candidates"].pop()
        elif response["conflicts"]:
            response["conflicts"].pop()
        else:
            break
        response["truncated"] = True
    return response


def number_tokens(text):
    # Keep spelling/sign/script; equivalent typographic changes need an explicit review.
    return Counter(NUMBER.findall(text) + SCRIPT_NUMBER.findall(text))


def check_result(unit, result, policy):
    need(isinstance(result, dict) and isinstance(result.get("target"), str) and result["target"].strip(), "result.target 必须是非空译文")
    need(isinstance(result.get("issues", []), list), "issues 必须为数组")
    for issue in result.get("issues", []):
        need(isinstance(issue, dict) and issue.get("severity") in ("critical", "major", "minor") and issue.get("status") in ("open", "resolved") and isinstance(issue.get("detail"), str) and issue["detail"].strip(), "issue 需要 severity/status/detail")
    target = result["target"]
    findings = []
    def add(kind, detail):
        findings.append({"id": digest([kind, detail])[:16], "kind": kind, "detail": detail})
    source_numbers, target_numbers = number_tokens(unit["source"]), number_tokens(target)
    if source_numbers != target_numbers:
        add("numbers", {"missing": dict(source_numbers - target_numbers), "extra": dict(target_numbers - source_numbers)})
    source_symbols, target_symbols = Counter(TECH_SYMBOL.findall(unit["source"])), Counter(TECH_SYMBOL.findall(target))
    if source_symbols != target_symbols:
        add("technical_symbols", {"missing": dict(source_symbols - target_symbols), "extra": dict(target_symbols - source_symbols)})
    if "segments" in unit:
        translated = result.get("segments")
        need(isinstance(translated, list) and all(isinstance(s, dict) for s in translated), "结果必须提供逐段/逐格 segments")
        need([s.get("id") for s in translated] == [s["id"] for s in unit["segments"]], "结果 segment ID、数量或顺序不符；不能遗漏、重复或新增")
        for src, dst in zip(unit["segments"], translated):
            need(set(dst) <= {"id", "target"}, "segment 结果只允许 id/target；问题放顶层 issues 并填写 location")
            need(isinstance(dst.get("target"), str), "segment.target 必须为字符串")
            if not src["source"].strip():
                need(dst["target"] == src["source"], "空单元格必须原样保留，不能补 0、横线或说明")
                continue
            child = dict(src, kind=src.get("kind", unit["kind"]))
            for finding in check_result(child, {"target": dst["target"]}, policy):
                findings.append(dict(finding, id=digest(["segment", src["id"], finding["id"]])[:16], location=src["id"]))
        need(target == "\n".join(s["target"] for s in translated), "target 必须按 segments 顺序用换行连接；排版须从同一份 segments 生成")
    else:
        need("segments" not in result, "原文未定义 segments，不能在结果中另建未绑定内容")
    for token in unit.get("protected", []):
        if unit["source"].count(token) != target.count(token):
            add("protected", token)
    if unit["kind"] == "literal" and target != unit["source"]:
        add("literal_changed", "原样保留字段被改写")
    if "structure" in unit and unit["structure"] != result.get("structure"):
        add("structure", "结构指纹不一致；检查行列、单元格 ID、合并及脚注关系")
    for finding in check_term_usage(unit, target, policy) + check_unit_extensions(unit, result):
        add(finding["kind"], finding["detail"])
    if "〔待核" in target:
        add("unresolved_marker", "译文存在待核标记")
    if unit["kind"] not in ("code", "formula", "identifier", "literal") and target.strip() == unit["source"].strip():
        add("unchanged_source", "译文与原文相同")
    return findings


def start_unit(db, root, unit_id):
    row = unit_row(db, unit_id)
    allow_unit(db, root, row)
    need(row["state"] != "reviewed", "内容块已审校通过；改译请直接提交新版本。")
    db.execute("UPDATE units SET state='in_progress' WHERE id=?", (unit_id,))
    event(db, "start", unit_id)
    return {"id": unit_id, "state": "in_progress"}


def commit_result(db, root, unit_id, result):
    row = unit_row(db, unit_id)
    allow_unit(db, root, row)
    return _commit_result_row(db, row, result)


def _commit_result_row(db, row, result):
    """Caller has validated scope and holds the write transaction."""
    unit_id = row["id"]
    findings = check_result(json.loads(row["source"]), result, get_meta(db, "policy"))
    hashed = digest(result)
    if row["result_hash"] == hashed:
        # A resumed identical commit does not discard a valid review.
        stale_review = row["state"] == "reviewed" and reviewed_payload(
            row, get_meta(db, "policy"), get_meta(db, "policy_revision"), allow_open_minor=True) is None
        if row["state"] == "in_progress":
            db.execute("UPDATE units SET state='translated' WHERE id=?", (unit_id,))
        elif stale_review:
            db.execute("UPDATE units SET state='needs_review' WHERE id=?", (unit_id,))
            event(db, "recheck", {"checks_revision": CHECKS_REVISION, "affected": [unit_id]})
        return {"id": unit_id, "unchanged": True, "findings": findings,
                "review_required": row["state"] != "reviewed" or stale_review}
    revision = row["revision"] + 1
    db.execute("UPDATE units SET result=?,result_hash=?,state='translated',review=NULL,revision=? WHERE id=?", (encoded(result), hashed, revision, unit_id))
    db.execute("INSERT INTO history(unit_id,at,result,result_hash,policy_revision) VALUES (?,?,?,?,?)", (unit_id, now(), encoded(result), hashed, get_meta(db, "policy_revision")))
    if row["sample"]:
        put_meta(db, "sample", None)
    event(db, "commit", {"id": unit_id, "revision": revision})
    return {"id": unit_id, "revision": revision, "findings": findings}


def commit_batch(db, root, items):
    """Atomic bounded submission, no automatic review or sample approval."""
    if not db.in_transaction:
        with transaction(db):
            return commit_batch(db, root, items)
    need(isinstance(items, list) and 1 <= len(items) <= BATCH_LIMIT, "批提交需 1 至 32 个内容块")
    need(len(encoded(items).encode("utf-8")) <= BATCH_BYTES, "批提交 JSON 不得超过 8 MiB")
    need(all(isinstance(item, dict) and set(item) == {"id", "result"} and isinstance(item["id"], str)
             for item in items), "批项必须只含 id/result")
    ids = [item["id"] for item in items]
    need(len(ids) == len(set(ids)), "批提交 ID 不能重复")
    rows = [unit_row(db, unit_id) for unit_id in ids]
    need(len({bool(row["sample"]) for row in rows}) == 1, "样本与非样本禁止混批")
    marker = "batch_" + uuid.uuid4().hex
    db.execute("SAVEPOINT " + marker)
    try:
        verify_source(db, full=True)
        verify_assets(db, root)
        for row in rows:
            need(digest(json.loads(row["source"])) == row["source_hash"], "原文快照损坏：" + row["id"])
            if row["result"]:
                need(digest(json.loads(row["result"])) == row["result_hash"], "译文快照损坏：" + row["id"])
        if not rows[0]["sample"]:
            need(gate(db, root) in ("approved", "not_required"), "样本尚未有效确认，禁止非样本批提交")
        results = [_commit_result_row(db, row, item["result"]) for row, item in zip(rows, items)]
        event(db, "batch_commit", {"ids": ids, "sample_only": bool(rows[0]["sample"])})
        db.execute("RELEASE SAVEPOINT " + marker)
        return {"count": len(results), "results": results,
                "review_required": any(result.get("review_required") or not (result.get("unchanged") and row["state"] == "reviewed")
                                       for row, result in zip(rows, results)),
                "note": "变更译文仍需逐块审校；有效旧审校及双格式样本批准关口保持有效"}
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK TO SAVEPOINT " + marker)
            db.execute("RELEASE SAVEPOINT " + marker)
        raise


def read_batch(path):
    need(Path(path).stat().st_size <= BATCH_BYTES, "批提交 JSON 文件不得超过 8 MiB")
    return read_json(path)


def review_unit(db, root, unit_id, review):
    row = unit_row(db, unit_id)
    allow_unit(db, root, row)
    need(row["result"], "先提交译文再审校")
    need(isinstance(review, dict), "review 必须是对象")
    need(review.get("result_sha256") == row["result_hash"], "审校绑定的译文版本不符")
    need(type(review.get("policy_revision")) is int and review["policy_revision"] > 0 and
         review["policy_revision"] == get_meta(db, "policy_revision"), "审校绑定的约定版本必须为当前正整数版本")
    for key in ("semantic", "chinese", "structure"):
        need(isinstance(review.get(key), str) and review[key].strip(), "缺少审校说明：" + key)
    result = json.loads(row["result"])
    need(not any(i["status"] == "open" and i["severity"] in ("critical", "major") for i in result.get("issues", [])), "存在未解决的关键或重大问题")
    need("〔待核" not in result["target"], "先解决译文中的待核标记")
    findings = check_result(json.loads(row["source"]), result, get_meta(db, "policy"))
    resolutions = review.get("resolutions", {})
    need(isinstance(resolutions, dict), "resolutions 必须是对象")
    for finding in findings:
        reason = resolutions.get(finding["id"])
        need(isinstance(reason, str) and reason.strip(), "未处置检查项：" + finding["id"])
    stored = dict(review, at=now(), findings=findings, checks_revision=CHECKS_REVISION,
                  semantic_signature=semantic_signature(json.loads(row["source"]), get_meta(db, "policy")))
    archive_review(db, unit_id, row["result_hash"], stored)
    db.execute("UPDATE units SET state='reviewed',review=? WHERE id=?", (encoded(stored), unit_id))
    event(db, "review", unit_id)
    return {"id": unit_id, "state": "reviewed", "result_sha256": row["result_hash"]}


def register_sample(db, root, artifact):
    need(sample_reviews_current(db), "先选择、翻译并按当前检查审校全部样本块；失效审校先 resume 核对")
    paths = artifact if isinstance(artifact, (list, tuple)) else [artifact]
    need(paths, "样本文件列表为空")
    old = get_meta(db, "sample")
    # Byte-identical relocation of a v1/v2 artifact retains its original approval.
    if old and "artifacts" not in old and len(paths) == 1 and (get_meta(db, "output_revision") or 0) == 0:
        moved_file = file_record(root, paths[0])
        if moved_file["sha256"] == old["artifact_sha256"] and old["signature"] == sample_signature(db, legacy=True):
            moved = old["artifact"] != moved_file["path"]
            old["artifact"] = moved_file["path"]
            put_meta(db, "sample", old)
            if moved:
                event(db, "relocate_sample", {"version": old["version"], "artifact": old["artifact"]})
            return {"version": old["version"], "state": gate(db, root), "unchanged": True, "relocated": moved}
    verify_assets(db, root)
    files = sorted([inspected_artifact(root, p) for p in paths], key=lambda x: x["format"])
    need([f["format"] for f in files] == output_policy(db)["formats"], "每种所需格式必须恰有一个样本文件")
    need(checked_sample_files(db, root, files), "先登记 sample 产物并完成 render/content/visual 检查，再交付样本")
    sample = {"signature": sample_signature(db), "artifacts": files, "output_signature": output_signature(db),
              "registered_at": now(), "approval": None}
    sample["version"] = digest([sample["signature"], sample["output_signature"], [(f["format"], f["sha256"]) for f in files]])
    if old and old["version"] == sample["version"]:
        # Moving identical bytes does not change what the user approved.
        moved = old["artifacts"] != files
        if moved:
            old["artifacts"] = files
            put_meta(db, "sample", old)
            event(db, "relocate_sample", {"version": old["version"], "artifacts": files})
        return {"version": old["version"], "state": gate(db, root), "unchanged": True, "relocated": moved}
    put_meta(db, "require_sample", True)
    put_meta(db, "sample", sample)
    event(db, "register_sample", {"version": sample["version"]})
    return {"version": sample["version"], "state": "awaiting_approval"}


def approve_sample(db, root, version, evidence):
    need(gate(db, root) in ("awaiting_approval", "approved"), "没有可确认的已审样本，或样本发生变化")
    sample = get_meta(db, "sample")
    need(version == sample["version"], "用户确认的样本版本不匹配")
    need(isinstance(evidence, dict), "批准证据必须是对象")
    for key in ("user_message", "message_ref", "approved_at"):
        need(isinstance(evidence.get(key), str) and evidence[key].strip(), "批准证据缺少 " + key)
    # This validates provenance fields, not human identity or the semantics of consent.
    sample["approval"] = evidence
    put_meta(db, "sample", sample)
    event(db, "approve", {"version": version, "evidence": evidence})
    return {"version": version, "state": "approved"}


def update_policy(db, policy, reason):
    validate_policy(policy)
    need(reason.strip(), "约定变更必须说明原因")
    old = get_meta(db, "policy")
    if old == policy:
        return {"unchanged": True}
    need(old["scope"] == policy["scope"] and old["source_version"] == policy["source_version"], "源版本或翻译范围变化需要新修订项目，不能只修改约定而复用旧清单。")
    old_terms = {t["id"]: t for t in old["glossary"]}
    new_terms = {t["id"]: t for t in policy["glossary"]}
    changed = {k for k in old_terms.keys() | new_terms.keys() if old_terms.get(k) != new_terms.get(k)}
    core_changed = {k: v for k, v in old.items() if k != "glossary"} != {k: v for k, v in policy.items() if k != "glossary"}
    affected = []
    sample_affected = False
    for row in db.execute("SELECT id,source,sample,result IS NOT NULL AS has_result FROM units"):
        unit = json.loads(row["source"])
        bound = set(unit.get("term_ids", []))
        for segment in unit.get("segments", []):
            bound.update(segment.get("term_ids", []))
        need(bound <= new_terms.keys(), "不能删除原文已绑定的术语 ID；保留 ID 修订词条，或建立新源修订项目")
        validate_term_bindings(unit, policy)
        if core_changed or any(term_matches(unit, t) or legacy_term_matches(unit, t) for k in changed for t in (old_terms.get(k), new_terms.get(k)) if t):
            affected.append(row["id"])
            sample_affected |= bool(row["sample"])
            if row["has_result"]:
                db.execute("UPDATE units SET state='needs_review',review=NULL WHERE id=?", (row["id"],))
    material = core_changed or bool(changed & old_terms.keys())
    if material:
        put_meta(db, "governance_revision", get_meta(db, "governance_revision") + 1)
    if material or sample_affected:
        put_meta(db, "sample", None)
    revision = get_meta(db, "policy_revision") + 1
    put_meta(db, "policy", policy)
    put_meta(db, "policy_revision", revision)
    event(db, "policy", {"revision": revision, "reason": reason, "old": old, "new": policy, "affected": affected})
    return {"revision": revision, "affected": affected, "sample_reconfirmation": material or sample_affected}


def reviewed_scope(db, role):
    where = " WHERE sample=1" if role == "sample" else ""
    count, pending = db.execute("SELECT COUNT(*),SUM(state!='reviewed') FROM units" + where).fetchone()
    return bool(count) and not pending


def register_artifact(db, root, artifact_id, role, path):
    need(isinstance(artifact_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", artifact_id), "产物 ID 无效")
    need(role in ("sample", "full"), "产物 role 必须为 sample/full")
    verify_source(db, full=True)
    verify_snapshots(db)
    refresh_reviews(db)
    need(reviewed_scope(db, role), "登记产物前，其范围内所有译文必须已审校")
    if role == "full":
        need(gate(db, root) in ("approved", "not_required"), "全文产物需要有效样本确认")
    verify_assets(db, root)
    record = inspected_artifact(root, path)
    need(record["format"] in output_policy(db)["formats"], "产物格式不在当前输出约定中")
    record.update(id=artifact_id, role=role, content_signature=content_signature(db, role),
                  output_signature=output_signature(db), output_revision=get_meta(db, "output_revision") or 0,
                  registered_at=now(), checks={})
    row = db.execute("SELECT record FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row:
        old = json.loads(row[0])
        same = all(old.get(k) == record.get(k) for k in ("role", "format", "sha256", "content_signature", "output_signature"))
        if same:
            if not artifact_state(db, root, dict(old, path=record["path"]))["stale_reasons"]:
                record["checks"] = old["checks"]
            record["registered_at"] = old["registered_at"]
    db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?)", (artifact_id, role, record["format"], encoded(record)))
    event(db, "artifact", {"id": artifact_id, "record": record})
    return record


def artifact_state(db, root, record, signatures=None):
    if signatures is None:
        signatures = {}
    reasons = []
    try:
        verify_file_record(root, record)
        verify_assets(db, root)
        role = record["role"]
        if role not in signatures:
            signatures[role] = content_signature(db, role)
        need(record["content_signature"] == signatures[role], "译文或原文版本变化")
        need(record["output_signature"] == output_signature(db), "输出约定变化")
        need(reviewed_scope(db, role), "存在待审译文")
    except (ValueError, KeyError, OSError, TypeError) as exc:
        reasons.append(str(exc))
    passed = []
    for stage, check in record.get("checks", {}).items():
        try:
            need(check["artifact_sha256"] == record["sha256"] and
                 check["content_signature"] == record["content_signature"] and
                 check["output_signature"] == record["output_signature"], "阶段证据绑定不一致")
            for proof in check.get("proof_files", []):
                verify_file_record(root, proof)
            passed.append(stage)
        except (ValueError, KeyError, OSError, TypeError) as exc:
            reasons.append(stage + ": " + str(exc))
    return {"id": record["id"], "role": record["role"], "format": record["format"],
            "path": record["path"], "passed": sorted(passed), "stale_reasons": reasons,
            "complete": not reasons and set(passed) == {"render", "content", "visual"}}


def artifact_statuses(db, root):
    signatures = {}
    return [artifact_state(db, root, json.loads(row[0]), signatures)
            for row in db.execute("SELECT record FROM artifacts ORDER BY id")]


def checked_sample_files(db, root, files):
    available = []
    signatures = {}
    for row in db.execute("SELECT record FROM artifacts WHERE role='sample'"):
        record = json.loads(row[0])
        if artifact_state(db, root, record, signatures)["complete"]:
            available.append(record)
    return all(any(r["format"] == f["format"] and r["sha256"] == f["sha256"] for r in available) for f in files)


def validate_check_evidence(record, evidence):
    need(isinstance(evidence, dict), "阶段证据必须为对象")
    for key in ("reviewer", "at", "details"):
        need(isinstance(evidence.get(key), str) and evidence[key].strip(), "阶段证据缺少 " + key)
    for key in ("artifact_sha256", "content_signature", "output_signature"):
        need(evidence.get(key) == record["sha256" if key == "artifact_sha256" else key], "阶段证据版本不匹配：" + key)
    need(evidence.get("passed") is True, "仅记录通过的阶段；未通过请先修复并保留问题单")


def verify_png(path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("render 阶段的页图校验需要 Pillow；其他台账命令不依赖它。") from exc
    try:
        with Image.open(path) as picture:
            need(picture.format == "PNG" and picture.width > 0 and picture.height > 0,
                 "渲染证明需真实 PNG 页图")
            picture.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("PNG 页图结构或校验和无效：" + str(path)) from exc


def content_check(db, root, role, evidence):
    proof = evidence.get("extraction")
    path = verify_file_record(root, proof)
    need(path.suffix.lower() == ".jsonl", "回抽记录须为逐块 JSONL")
    expected = db.execute("SELECT id,result FROM units" + (" WHERE sample=1" if role == "sample" else "") + " ORDER BY ordinal")
    def clean(text):
        # No NFKC, whitespace stripping or identifier normalization.
        return text.replace("\r\n", "\n")
    with path.open(encoding="utf-8-sig") as f:
        for row in expected:
            line = f.readline()
            need(bool(line.strip()), "回抽记录缺少块：" + row["id"])
            actual = json.loads(line)
            target = json.loads(row["result"])
            need(isinstance(actual, dict) and actual.get("id") == row["id"], "回抽块 ID 或顺序不一致")
            need(isinstance(actual.get("target"), str) and clean(actual["target"]) == clean(target["target"]),
                 "回抽正文与已审译文不一致：" + row["id"])
            if "segments" in target:
                parts = actual.get("segments")
                need(isinstance(parts, list) and len(parts) == len(target["segments"]), "回抽单元格数量不符")
                for a, b in zip(parts, target["segments"]):
                    need(isinstance(a, dict) and a.get("id") == b["id"] and isinstance(a.get("target"), str) and
                         clean(a["target"]) == clean(b["target"]), "回抽单元格内容或归属不符")
        need(not f.read().strip(), "回抽记录存在额外块")
    verify_file_record(root, proof)
    return [proof]


def check_artifact(db, root, artifact_id, stage, evidence):
    need(stage in ("render", "content", "visual"), "未知产物检查阶段")
    verify_source(db, full=True)
    verify_snapshots(db)
    refresh_reviews(db)
    row = db.execute("SELECT record FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    need(row is not None, "产物未登记")
    record = json.loads(row[0])
    state = artifact_state(db, root, record)
    need(not state["stale_reasons"], "产物或证据已陈旧；重新生成/登记后检查：" + str(state["stale_reasons"]))
    validate_check_evidence(record, evidence)
    proofs = []
    if stage == "render":
        pages = evidence.get("pages")
        need(isinstance(evidence.get("renderer"), str) and evidence["renderer"].strip(), "缺少实际渲染器及版本")
        need(isinstance(pages, list) and pages, "需完整渲染页图清单 pages")
        need([p.get("page") for p in pages if isinstance(p, dict)] == list(range(1, len(pages) + 1)), "渲染页序不完整")
        if record["format"] == "pdf":
            need(len(pages) == record["page_count"], "PDF 渲染页数不符")
        need(len({p.get("path") for p in pages}) == len(pages), "每页需独立页图")
        for page in pages:
            path = verify_file_record(root, page)
            verify_png(path)
            verify_file_record(root, page)
            proofs.append(page)
    elif stage == "content":
        proofs = content_check(db, root, record["role"], evidence)
    else:
        need(set(state["passed"]) >= {"render", "content"}, "视觉验收前先完成渲染及内容对账")
        need(evidence.get("unresolved") == [], "视觉验收仍有未解决问题")
        for key in ("screening", "detail_pages", "furniture", "tables_figures_formulas", "search_copy"):
            need(isinstance(evidence.get(key), str) and evidence[key].strip(), "视觉验收缺少实际检查范围/结论：" + key)
    saved = dict(evidence, proof_files=proofs, recorded_at=now())
    if stage in ("render", "content"):
        record["checks"].pop("visual", None)
    record["checks"][stage] = saved
    db.execute("UPDATE artifacts SET record=? WHERE id=?", (encoded(record), artifact_id))
    event(db, "artifact_check", {"id": artifact_id, "stage": stage, "evidence": saved})
    return artifact_state(db, root, record)


def register_coverage(db, root, evidence):
    need(isinstance(evidence, dict), "覆盖证据必须为对象")
    need(evidence.get("source_sha256") == get_meta(db, "source")["sha256"] and
         evidence.get("inventory_sha256") == get_meta(db, "inventory_sha256") and
         evidence.get("scope") == get_meta(db, "policy")["scope"], "覆盖证据源/清单/范围不匹配")
    for key in ("reviewer", "at", "details"):
        need(isinstance(evidence.get(key), str) and evidence[key].strip(), "覆盖证据缺少 " + key)
    need(evidence.get("unresolved") == [], "覆盖核验仍有未解决缺失/解析问题")
    pages = evidence.get("pages")
    count = get_meta(db, "source")["pages"]
    need(isinstance(pages, list) and len(pages) == count, "覆盖清单必须逐页处置整个源 PDF，包括范围外页面")
    expected = {}
    for row in db.execute("SELECT id,source FROM units"):
        for page in json.loads(row["source"])["pdf_pages"]:
            expected.setdefault(page, set()).add(row["id"])
    for number, item in enumerate(pages, 1):
        need(isinstance(item, dict) and type(item.get("page")) is int and item["page"] == number,
             "覆盖清单页序不完整")
        ids = item.get("unit_ids")
        need(isinstance(ids, list) and all(isinstance(x, str) for x in ids) and len(set(ids)) == len(ids) and
             set(ids) == expected.get(number, set()), "页面内容块覆盖不一致：" + str(number))
        need(item.get("disposition") in ("in_scope", "out_of_scope", "blank", "nontext") and
             isinstance(item.get("details"), str) and item["details"].strip(), "页面必须记录内容/保留/排除处置")
        need(not ids or item["disposition"] == "in_scope", "含已登记内容的页不能标为范围外/空白")
        need(item["disposition"] != "in_scope" or ids, "范围内实质页面缺少内容块")
    proof_files = evidence.get("proof_files", [])
    need(isinstance(proof_files, list), "proof_files 必须为数组")
    for proof in proof_files:
        verify_file_record(root, proof)
    saved = dict(evidence, recorded_at=now(), assets=get_meta(db, "assets"))
    put_meta(db, "coverage", saved)
    event(db, "coverage", saved)
    return {"coverage_verified": True, "pages": count}


def coverage_current(db, root):
    value = get_meta(db, "coverage")
    if not value:
        return False
    try:
        need(value["source_sha256"] == get_meta(db, "source")["sha256"] and
             value["inventory_sha256"] == get_meta(db, "inventory_sha256") and
             value["scope"] == get_meta(db, "policy")["scope"] and value.get("assets") == get_meta(db, "assets"), "覆盖版本改变")
        verify_assets(db, root)
        for proof in value.get("proof_files", []):
            verify_file_record(root, proof)
    except (ValueError, OSError, KeyError, TypeError):
        return False
    return True


def status(db, root):
    verify_source(db, full=True)
    verify_assets(db, root)
    verify_snapshots(db)
    refresh_reviews(db)
    return _status_snapshot(db, root)


def _status_snapshot(db, root):
    """Internal: source/snapshots/reviews were checked in the current transaction."""
    counts = {s: 0 for s in STATES}
    counts.update({r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM units GROUP BY state")})
    state = gate(db, root)
    sample = get_meta(db, "sample")
    reviewed = bool(counts["reviewed"]) and sum(counts.values()) == counts["reviewed"]
    outputs = artifact_statuses(db, root)
    coverage = coverage_current(db, root)
    completed = {item["format"] for item in outputs if item["role"] == "full" and item["complete"]}
    return {"source": get_meta(db, "source"), "counts": counts, "sample_state": state,
            "sample_version": sample["version"] if sample else None, "policy_revision": get_meta(db, "policy_revision"),
            "all_units_reviewed": reviewed, "output_policy": output_policy(db),
            "output_revision": get_meta(db, "output_revision") or 0, "artifacts": outputs, "coverage_verified": coverage,
            "delivery_complete": reviewed and coverage and state in ("approved", "not_required") and
                set(output_policy(db)["formats"]) <= completed,
            "note": "delivery_complete 表示绑定当前版本的检查记录齐全；不证明人工/模型实际完成语义及视觉审查"}


def next_unit(db, root, unit_id=None):
    state = gate(db, root)
    restriction = "" if state in ("approved", "not_required") else " AND sample=1"
    row = unit_row(db, unit_id) if unit_id else None
    if not unit_id:
        # Prefer saved drafts. Partial indexes avoid rescanning completed text blobs.
        for clause in ("state IN ('needs_review','translated','in_progress')", "state='pending'"):
            selected = db.execute("SELECT id FROM units WHERE " + clause + restriction + " ORDER BY ordinal LIMIT 1").fetchone()
            if selected:
                row = unit_row(db, selected[0])
                break
    if not row:
        return {"next": None, "sample_state": state, "message": "等待样本确认/准备，或所有块已审校；检查 status。"}
    unit = json.loads(row["source"])
    policy = get_meta(db, "policy")
    context = dict(policy, glossary=[t for t in policy["glossary"] if term_matches(unit, t)])
    neighbor_units = [json.loads(r[0]) for r in db.execute("SELECT source FROM units WHERE ordinal<? ORDER BY ordinal DESC LIMIT 1", (row["ordinal"],))]
    neighbor_units += [json.loads(r[0]) for r in db.execute("SELECT source FROM units WHERE ordinal>? ORDER BY ordinal LIMIT 1", (row["ordinal"],))]
    neighbors = [{"id": other["id"], "section": other["section"], "kind": other["kind"],
                  "source": other["source"][:4000], "truncated": len(other["source"]) > 4000}
                 for other in neighbor_units]
    result = json.loads(row["result"]) if row["result"] else None
    try:
        findings = check_result(unit, result, policy) if result else []
    except (ValueError, TypeError) as exc:
        findings = [{"id": digest(["invalid_payload", str(exc)])[:16], "kind": "invalid_payload", "detail": str(exc)}]
    dependencies, omitted, budget = [], [], 20000
    for dependency in unit.get("dependencies", []):
        if not isinstance(dependency, str) or len(dependencies) >= 16 or budget <= 0:
            omitted.append(dependency)
            continue
        related = db.execute("SELECT * FROM units WHERE id=?", (dependency,)).fetchone()
        if not related:
            omitted.append(dependency)
            continue
        source = json.loads(related["source"])
        original = source["source"]
        excerpt = original[:min(4000, budget)]
        budget -= len(excerpt)
        item = {"id": dependency, "section": source["section"], "kind": source["kind"],
                "source": excerpt, "state": related["state"], "truncated": len(excerpt) != len(original)}
        checked = reviewed_payload(related, policy, get_meta(db, "policy_revision")) if budget > 0 else None
        if checked:
            target = checked["result"]["target"]
            item["reviewed_target"] = target[:min(4000, budget)]
            budget -= len(item["reviewed_target"])
            item["truncated"] |= len(item["reviewed_target"]) != len(target)
        dependencies.append(item)
    return {"unit": unit, "state": row["state"], "result": result, "result_sha256": row["result_hash"],
            "policy_revision": get_meta(db, "policy_revision"), "policy": context,
            "neighbors_reference_only": neighbors, "dependencies_reference_only": dependencies,
            "dependencies_omitted": omitted, "dependency_budget_characters": 20000,
            "translation_memory": memory_candidates(db, row, policy, state), "findings": findings}


def refresh_reviews(db):
    """Recheck deterministic invariants after upgrades without discarding valid work."""
    affected = []
    policy = get_meta(db, "policy")
    revision = get_meta(db, "policy_revision")
    for row in db.execute("SELECT * FROM units WHERE state='reviewed'"):
        if reviewed_payload(row, policy, revision, allow_open_minor=True) is None:
            db.execute("UPDATE units SET state='needs_review' WHERE id=?", (row["id"],))
            affected.append(row["id"])
    if affected:
        event(db, "recheck", {"checks_revision": CHECKS_REVISION, "affected": affected})
    return affected


def verify_snapshots(db):
    need(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "数据库完整性异常；保留原库，从快照恢复到新目录。")
    for row in db.execute("SELECT id,source,source_hash,result,result_hash FROM units"):
        need(digest(json.loads(row["source"])) == row["source_hash"], "原文快照损坏：" + row["id"])
        if row["result"]:
            need(digest(json.loads(row["result"])) == row["result_hash"], "译文快照损坏：" + row["id"])


def resume(db, root, source=None):
    verify_source(db, source, full=True)
    verify_assets(db, root)
    verify_snapshots(db)
    interrupted = [r[0] for r in db.execute("SELECT id FROM units WHERE state='in_progress'")]
    db.execute("UPDATE units SET state=CASE WHEN result IS NULL THEN 'pending' ELSE 'needs_review' END WHERE state='in_progress'")
    recheck = refresh_reviews(db)
    event(db, "resume", {"interrupted": interrupted})
    return {"status": _status_snapshot(db, root), "recovered": interrupted, "recheck": recheck, "next": next_unit(db, root)}


def backup(db, root):
    need(not db.in_transaction, "备份须在事务外执行；先提交或回滚当前事务")
    directory = Path(root).resolve() / "backups"
    directory.mkdir(exist_ok=True)
    path = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    other = sqlite3.connect(str(path))
    try:
        db.backup(other)
        need(other.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "备份未通过完整性检查")
    finally:
        other.close()
    return {"backup": str(path), "note": "同时保留源 PDF、双格式样本、资产及产物检查证据；数据库备份不包含外部文件。"}


def export_project(db, root, draft=False):
    verify_source(db, full=True)
    verify_assets(db, root)
    verify_snapshots(db)
    refresh_reviews(db)
    state = _status_snapshot(db, root)
    if not draft:
        need(state["all_units_reviewed"], "还有未审内容块；只能 --draft 导出待审数据")
        need(state["sample_state"] in ("approved", "not_required"), "样本确认无效")
    directory = Path(root).resolve() / "exports" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    def export_units():
        for row in db.execute("SELECT * FROM units ORDER BY ordinal"):
            yield {"unit": json.loads(row["source"]), "state": row["state"], "result": json.loads(row["result"]) if row["result"] else None, "review": json.loads(row["review"]) if row["review"] else None}
    atomic_json_stream(directory / "units.json", items=export_units())
    atomic_json(directory / "policy.json", get_meta(db, "policy"))
    atomic_json(directory / "output-policy.json", output_policy(db))
    atomic_json(directory / "sample.json", get_meta(db, "sample"))
    atomic_json(directory / "status.json", dict(state, draft=draft))
    # Receipt is last: a directory without receipt is an interrupted, incomplete export.
    atomic_json(directory / "receipt.json", {"at": now(), "files": {p.name: file_hash(p) for p in directory.iterdir() if p.is_file()}})
    return {"export": str(directory), "draft": draft, "note": "这是中间数据；delivery_complete 另需覆盖核验和所有约定格式的完整产物检查。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--source", required=True)
    init.add_argument("--inventory", required=True)
    init.add_argument("--policy", required=True)
    init.add_argument("--require-sample", action="store_true")
    init.add_argument("--outputs", help="独立输出约定 JSON；未指定时兼容旧 DOCX 任务")
    select = commands.add_parser("select-sample")
    select.add_argument("--ids", nargs="+", required=True)
    for name in ("status", "next", "backup"):
        commands.add_parser(name)
    show = commands.add_parser("show")
    show.add_argument("--id", required=True)
    res = commands.add_parser("resume")
    res.add_argument("--source")
    for name in ("start", "commit", "review"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--id", required=True)
        if name != "start":
            cmd.add_argument("--file", required=True)
    batch = commands.add_parser("batch-commit")
    batch.add_argument("--file", required=True, help="最多 32 项、8 MiB 的 [{id,result},...] JSON")
    sample = commands.add_parser("sample")
    sample.add_argument("--artifact", action="append", nargs="+", required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--version", required=True)
    approve.add_argument("--evidence", required=True)
    policy = commands.add_parser("policy")
    policy.add_argument("--file", required=True)
    policy.add_argument("--reason", required=True)
    outputs = commands.add_parser("output-policy")
    outputs.add_argument("--file", required=True)
    outputs.add_argument("--reason", required=True)
    assets = commands.add_parser("assets")
    assets.add_argument("--manifest", required=True)
    coverage = commands.add_parser("coverage")
    coverage.add_argument("--evidence", required=True)
    artifacts = commands.add_parser("artifact")
    actions = artifacts.add_subparsers(dest="artifact_action", required=True)
    register = actions.add_parser("register")
    register.add_argument("--id", required=True)
    register.add_argument("--role", choices=("sample", "full"), required=True)
    register.add_argument("--file", required=True)
    check = actions.add_parser("check")
    check.add_argument("--id", required=True)
    check.add_argument("--stage", choices=("render", "content", "visual"), required=True)
    check.add_argument("--evidence", required=True)
    exp = commands.add_parser("export")
    exp.add_argument("--draft", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        answer = initialize(args.project, args.source, args.inventory, read_json(args.policy), args.require_sample,
                            read_json(args.outputs) if args.outputs else None)
    else:
        db = connection(args.project)
        try:
            if args.command == "backup":
                answer = backup(db, args.project)
            else:
                with transaction(db):
                    if args.command == "resume":
                        answer = resume(db, args.project, args.source)
                    else:
                        verify_source(db)
                        c = args.command
                        if c == "status": answer = status(db, args.project)
                        elif c == "next": answer = next_unit(db, args.project)
                        elif c == "show": answer = next_unit(db, args.project, args.id)
                        elif c == "select-sample": answer = select_sample(db, args.ids)
                        elif c == "start": answer = start_unit(db, args.project, args.id)
                        elif c == "commit": answer = commit_result(db, args.project, args.id, read_json(args.file))
                        elif c == "batch-commit": answer = commit_batch(db, args.project, read_batch(args.file))
                        elif c == "review": answer = review_unit(db, args.project, args.id, read_json(args.file))
                        elif c == "sample": answer = register_sample(db, args.project, [p for group in args.artifact for p in group])
                        elif c == "approve": answer = approve_sample(db, args.project, args.version, read_json(args.evidence))
                        elif c == "policy": answer = update_policy(db, read_json(args.file), args.reason)
                        elif c == "output-policy": answer = update_output_policy(db, read_json(args.file), args.reason)
                        elif c == "assets": answer = register_assets(db, args.project, args.manifest)
                        elif c == "coverage": answer = register_coverage(db, args.project, read_json(args.evidence))
                        elif c == "artifact":
                            if args.artifact_action == "register":
                                answer = register_artifact(db, args.project, args.id, args.role, args.file)
                            else:
                                answer = check_artifact(db, args.project, args.id, args.stage, read_json(args.evidence))
                        elif c == "export": answer = export_project(db, args.project, args.draft)
        finally:
            db.close()
    print(json.dumps(answer, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    configure_stdio()
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
