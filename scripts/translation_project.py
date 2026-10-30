#!/usr/bin/env python3
"""Local translation ledger. No model/API calls; approvals must come from a real user."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
import uuid
import zipfile
from check_environment import configure_stdio

DB_NAME = "translation.sqlite3"
SCHEMA = 2
CHECKS_REVISION = 3
STATES = ("pending", "in_progress", "translated", "needs_review", "reviewed")


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
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as f:
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
        if temporary.exists():
            temporary.unlink()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def page_count(source):
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ValueError("PDF 初始化需要 pypdf；请在所选 Python 环境准备该依赖。") from e
    with open(source, "rb") as f:
        reader = PdfReader(f)
        need(not reader.is_encrypted, "源 PDF 加密；先取得可合法读取的副本，再建立项目。")
        return len(reader.pages)


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
        need(version in (1, SCHEMA), "数据库版本不支持；保留原库后迁移。")
        if version == 1:
            with transaction(db):
                create_review_storage(db)
                for row in db.execute("SELECT id,result_hash,review FROM units WHERE review IS NOT NULL"):
                    review = json.loads(row["review"])
                    archive_review(db, row["id"], row["result_hash"], review)
                put_meta(db, "schema", SCHEMA)
                event(db, "schema_upgrade", {"from": 1, "to": SCHEMA})
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


def archive_review(db, unit_id, result_hash, review):
    db.execute("INSERT INTO review_history(unit_id,result_hash,at,review) VALUES (?,?,?,?)", (unit_id, result_hash, now(), encoded(review)))


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def initialize(root, source, inventory, policy, require_sample=False):
    root, source = Path(root).resolve(), Path(source).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / DB_NAME
    need(not path.exists(), "项目已存在，请 resume，禁止覆盖。")
    validate_policy(policy)
    source_hash = file_hash(source)
    pages = page_count(source)
    need(pages > 0, "源 PDF 无页面")
    count = 0
    glossary_ids = {term["id"] for term in policy["glossary"]}
    # Reserve the name exclusively; an interrupted initialization is never overwritten.
    with path.open("xb"):
        pass
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        with transaction(db):
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE units(id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, source TEXT NOT NULL, source_hash TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, result TEXT, result_hash TEXT, review TEXT, revision INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE history(id INTEGER PRIMARY KEY, unit_id TEXT, at TEXT, result TEXT, result_hash TEXT, policy_revision INTEGER)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, at TEXT, action TEXT, detail TEXT)")
            create_review_storage(db)
            stat = source.stat()
            values = {"schema": SCHEMA, "created": now(), "source": {"path": str(source), "sha256": source_hash, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "pages": pages}, "policy": policy, "policy_revision": 1, "governance_revision": 1, "require_sample": pages >= 20 or require_sample, "sample": None, "inventory_sha256": file_hash(inventory)}
            for key, value in values.items():
                put_meta(db, key, value)
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
                    try:
                        db.execute("INSERT INTO units(id,ordinal,source,source_hash,state) VALUES (?,?,?,?, 'pending')", (unit["id"], unit["order"], encoded(unit), digest(unit)))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("内容块 ID 或 order 重复") from exc
                    count += 1
            need(count, "清单为空")
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


@lru_cache(maxsize=8192)
def term_pattern(english):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(english) + r"(?![A-Za-z0-9_])", re.I)


def term_matches(unit, term):
    # Conservative candidate search; a match never resolves polysemy automatically.
    bound = term["id"] in unit.get("term_ids", []) or any(term["id"] in s.get("term_ids", []) for s in unit.get("segments", []))
    return bound or term_pattern(term["en"]).search(unit["source"]) is not None


def sample_signature(db):
    rows = db.execute("SELECT id,source,result_hash FROM units WHERE sample=1 ORDER BY ordinal").fetchall()
    policy = get_meta(db, "policy")
    sources = [json.loads(r["source"]) for r in rows]
    applicable = [t for t in policy["glossary"] if any(term_matches(unit, t) for unit in sources)]
    baseline = {k: v for k, v in policy.items() if k != "glossary"}
    baseline["glossary"] = applicable
    return digest({"source": get_meta(db, "source")["sha256"], "inventory": get_meta(db, "inventory_sha256"), "policy": baseline, "governance_revision": get_meta(db, "governance_revision"), "units": [(r["id"], r["result_hash"]) for r in rows]})


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


def gate(db, root):
    if not get_meta(db, "require_sample"):
        return "not_required"
    sample = get_meta(db, "sample")
    if not sample:
        return "sample_draft"
    try:
        relative = portable_sample_path(sample["artifact"])
        artifact = (Path(root).resolve() / relative).resolve()
        artifact.relative_to(Path(root).resolve())
    except (ValueError, TypeError, KeyError, OSError):
        return "sample_changed"
    if sample["signature"] != sample_signature(db) or not artifact.is_file() or file_hash(artifact) != sample["artifact_sha256"]:
        return "sample_changed"
    if db.execute("SELECT COUNT(*) FROM units WHERE sample=1 AND state!='reviewed'").fetchone()[0]:
        return "sample_draft"
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
    for term in policy["glossary"]:
        if term_matches(unit, term):
            if term["zh"] not in target:
                add("term_missing", term["id"])
            for forbidden in term.get("forbidden", []):
                if forbidden in target:
                    add("term_forbidden", {"id": term["id"], "text": forbidden})
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
    findings = check_result(json.loads(row["source"]), result, get_meta(db, "policy"))
    hashed = digest(result)
    if row["result_hash"] == hashed:
        # A resumed identical commit does not discard a valid review.
        if row["state"] == "in_progress":
            db.execute("UPDATE units SET state='translated' WHERE id=?", (unit_id,))
        return {"id": unit_id, "unchanged": True, "findings": findings}
    revision = row["revision"] + 1
    db.execute("UPDATE units SET result=?,result_hash=?,state='translated',review=NULL,revision=? WHERE id=?", (encoded(result), hashed, revision, unit_id))
    db.execute("INSERT INTO history(unit_id,at,result,result_hash,policy_revision) VALUES (?,?,?,?,?)", (unit_id, now(), encoded(result), hashed, get_meta(db, "policy_revision")))
    if row["sample"]:
        put_meta(db, "sample", None)
    event(db, "commit", {"id": unit_id, "revision": revision})
    return {"id": unit_id, "revision": revision, "findings": findings}


def review_unit(db, root, unit_id, review):
    row = unit_row(db, unit_id)
    allow_unit(db, root, row)
    need(row["result"], "先提交译文再审校")
    need(isinstance(review, dict), "review 必须是对象")
    need(review.get("result_sha256") == row["result_hash"], "审校绑定的译文版本不符")
    need(review.get("policy_revision") == get_meta(db, "policy_revision"), "审校绑定的约定版本不符")
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
    stored = dict(review, at=now(), findings=findings, checks_revision=CHECKS_REVISION)
    archive_review(db, unit_id, row["result_hash"], stored)
    db.execute("UPDATE units SET state='reviewed',review=? WHERE id=?", (encoded(stored), unit_id))
    event(db, "review", unit_id)
    return {"id": unit_id, "state": "reviewed", "result_sha256": row["result_hash"]}


def register_sample(db, root, artifact):
    rows = db.execute("SELECT state FROM units WHERE sample=1").fetchall()
    need(rows and all(r[0] == "reviewed" for r in rows), "先选择、翻译并审校全部样本块")
    artifact = Path(artifact).resolve()
    try:
        relative = artifact.relative_to(Path(root).resolve()).as_posix()
    except ValueError as e:
        raise ValueError("样本 DOCX 必须位于项目目录内，便于迁移恢复") from e
    portable_sample_path(relative)
    need(artifact.suffix.lower() == ".docx" and zipfile.is_zipfile(artifact), "样本必须是真实 DOCX")
    with zipfile.ZipFile(artifact) as z:
        need("word/document.xml" in z.namelist(), "样本缺少 Word 正文")
    sample = {"signature": sample_signature(db), "artifact": relative, "artifact_sha256": file_hash(artifact), "registered_at": now(), "approval": None}
    sample["version"] = digest([sample["signature"], sample["artifact_sha256"]])
    old = get_meta(db, "sample")
    if old and old["version"] == sample["version"]:
        # Moving identical bytes does not change what the user approved.
        moved = old["artifact"] != relative
        if moved:
            old["artifact"] = relative
            put_meta(db, "sample", old)
            event(db, "relocate_sample", {"version": old["version"], "artifact": relative})
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
        if core_changed or any(term_matches(unit, t) for k in changed for t in (old_terms.get(k), new_terms.get(k)) if t):
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


def status(db, root):
    counts = {s: 0 for s in STATES}
    counts.update({r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM units GROUP BY state")})
    state = gate(db, root)
    sample = get_meta(db, "sample")
    return {"source": get_meta(db, "source"), "counts": counts, "sample_state": state, "sample_version": sample["version"] if sample else None, "policy_revision": get_meta(db, "policy_revision"), "all_units_reviewed": bool(counts["reviewed"]) and sum(counts.values()) == counts["reviewed"], "note": "块状态不证明 PDF 提取完整性或 DOCX 版面已验证"}


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
    neighbors = [json.loads(r[0]) for r in db.execute("SELECT source FROM units WHERE ordinal<? ORDER BY ordinal DESC LIMIT 1", (row["ordinal"],))]
    neighbors += [json.loads(r[0]) for r in db.execute("SELECT source FROM units WHERE ordinal>? ORDER BY ordinal LIMIT 1", (row["ordinal"],))]
    result = json.loads(row["result"]) if row["result"] else None
    try:
        findings = check_result(unit, result, policy) if result else []
    except (ValueError, TypeError) as exc:
        findings = [{"id": digest(["invalid_payload", str(exc)])[:16], "kind": "invalid_payload", "detail": str(exc)}]
    return {"unit": unit, "state": row["state"], "result": result, "result_sha256": row["result_hash"], "policy_revision": get_meta(db, "policy_revision"), "policy": context, "neighbors_reference_only": neighbors, "findings": findings}


def refresh_reviews(db):
    """Recheck deterministic invariants after upgrades without discarding valid work."""
    affected = []
    policy = get_meta(db, "policy")
    for row in db.execute("SELECT * FROM units WHERE state='reviewed'"):
        review = json.loads(row["review"]) if row["review"] else {}
        try:
            result = json.loads(row["result"])
            findings = check_result(json.loads(row["source"]), result, policy)
            resolutions = review.get("resolutions", {})
            stale = (review.get("result_sha256") != row["result_hash"] or
                     not all(isinstance(review.get(k), str) and review[k].strip() for k in ("semantic", "chinese", "structure")) or
                     any(i["status"] == "open" and i["severity"] in ("critical", "major") for i in result.get("issues", [])) or
                     "〔待核" in result["target"] or not isinstance(resolutions, dict) or
                     any(not isinstance(resolutions.get(f["id"]), str) or not resolutions[f["id"]].strip() for f in findings))
        except (ValueError, TypeError, AttributeError):
            stale = True
        if stale:
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
    verify_snapshots(db)
    interrupted = [r[0] for r in db.execute("SELECT id FROM units WHERE state='in_progress'")]
    db.execute("UPDATE units SET state=CASE WHEN result IS NULL THEN 'pending' ELSE 'needs_review' END WHERE state='in_progress'")
    recheck = refresh_reviews(db)
    event(db, "resume", {"interrupted": interrupted})
    return {"status": status(db, root), "recovered": interrupted, "recheck": recheck, "next": next_unit(db, root)}


def backup(db, root):
    directory = Path(root).resolve() / "backups"
    directory.mkdir(exist_ok=True)
    path = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    other = sqlite3.connect(str(path))
    try:
        db.backup(other)
        need(other.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "备份未通过完整性检查")
    finally:
        other.close()
    return {"backup": str(path), "note": "同时保留源 PDF、样本 DOCX、图像和项目目录；数据库备份不包含外部文件。"}


def export_project(db, root, draft=False):
    verify_source(db, full=True)
    verify_snapshots(db)
    refresh_reviews(db)
    state = status(db, root)
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
    atomic_json(directory / "sample.json", get_meta(db, "sample"))
    atomic_json(directory / "status.json", dict(state, draft=draft))
    # Receipt is last: a directory without receipt is an interrupted, incomplete export.
    atomic_json(directory / "receipt.json", {"at": now(), "files": {p.name: file_hash(p) for p in directory.iterdir() if p.is_file()}})
    return {"export": str(directory), "draft": draft, "note": "导出中间数据，仍需生成 DOCX 并执行覆盖/版面验收。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--source", required=True)
    init.add_argument("--inventory", required=True)
    init.add_argument("--policy", required=True)
    init.add_argument("--require-sample", action="store_true")
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
    sample = commands.add_parser("sample")
    sample.add_argument("--artifact", required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--version", required=True)
    approve.add_argument("--evidence", required=True)
    policy = commands.add_parser("policy")
    policy.add_argument("--file", required=True)
    policy.add_argument("--reason", required=True)
    exp = commands.add_parser("export")
    exp.add_argument("--draft", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        answer = initialize(args.project, args.source, args.inventory, read_json(args.policy), args.require_sample)
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
                        elif c == "review": answer = review_unit(db, args.project, args.id, read_json(args.file))
                        elif c == "sample": answer = register_sample(db, args.project, args.artifact)
                        elif c == "approve": answer = approve_sample(db, args.project, args.version, read_json(args.evidence))
                        elif c == "policy": answer = update_policy(db, read_json(args.file), args.reason)
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
