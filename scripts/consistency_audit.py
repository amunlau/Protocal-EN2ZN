#!/usr/bin/env python3
"""Read-only, project-wide consistency candidates; never translates or approves."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

import translation_project as ledger
from check_environment import configure_stdio

VERSION = 1
MODAL = re.compile(r"\b(?:shall|should|must|may|might|can|cannot|not|never|without|unless|only)\b", re.I)


def exact(text):
    # No case folding, NFKC, whitespace deletion, or number removal.
    return text.replace("\r\n", "\n")


def context(unit, node, policy, source):
    bindings = sorted(set(node.get("term_ids", [])) | {x["term_id"] for x in node.get("term_occurrences", [])})
    source_meta = {**unit.get("source_meta", {}), **node.get("source_meta", {})}
    # Local IDs and character offsets identify an occurrence, not its meaning.
    pairs, placeholders = {}, []
    for item in sorted(node.get("inline_placeholders", []), key=lambda x: node["source"].index(x["token"])):
        constraints = item.get("constraints", {})
        pair = constraints.get("pair_id")
        if pair is not None:
            pairs.setdefault(pair, len(pairs))
        placeholders.append({"token": item["token"], "source": item["source"],
                             "reorder": constraints.get("reorder", True), "role": constraints.get("role"),
                             "pair": pairs.get(pair)})
    machine = sorted({(x["kind"], node["source"][x["start"]:x["end"]]) for x in node.get("machine_spans", [])})
    return {"document_id": source_meta.get("document_id", source["sha256"]),
            "source_version": source_meta.get("source_version", policy["source_version"]),
            "document_type": node.get("document_type", unit.get("document_type", policy.get("document_type", "protocol"))),
            "kind": node.get("kind", unit["kind"]), "role": node.get("output_meta", {}).get("role", unit.get("output_meta", {}).get("role")),
            "term_ids": bindings,
            "term_actions": sorted((x["term_id"], x["action"]) for x in node.get("term_occurrences", [])),
            "protected": sorted(node.get("protected", [])),
            "placeholders": placeholders, "machine_spans": machine}


def features(source):
    return {"numbers": dict(ledger.number_tokens(source)), "logic_words": dict(Counter(MODAL.findall(source.lower()))),
            "symbols": dict(Counter(ledger.TECH_SYMBOL.findall(source)))}


def locations(unit, node, target, legacy):
    return {"unit_id": unit["id"], "segment_id": node.get("id") if node is not unit else None,
            "section": unit["section"], "pdf_pages": unit["pdf_pages"],
            "source": node["source"][:800], "target": target[:800],
            "excerpt_truncated": len(node["source"]) > 800 or len(target) > 800,
            "source_sha256": ledger.digest(exact(node["source"])), "target_sha256": ledger.digest(exact(target)),
            "risk_features": features(node["source"]), "legacy_semantic_binding": legacy}


def group_candidates(index, max_examples):
    for kind, grouping, different in (("same_source_different_targets", "source_key", "target_key"),
                                      ("different_sources_same_target", "target_key", "source_key")):
        query = ("SELECT context_key," + grouping + ",COUNT(*),COUNT(DISTINCT " + different +
                 ") FROM entries GROUP BY context_key," + grouping + " HAVING COUNT(DISTINCT " + different + ")>1")
        for ctx, key, count, variants in index.execute(query):
            # Prefer one example per variant; first N occurrences can hide the actual conflict.
            examples = [json.loads(row[0]) for row in index.execute(
                "SELECT payload FROM entries WHERE context_key=? AND " + grouping + "=? GROUP BY " + different +
                " ORDER BY MIN(sequence) LIMIT ?", (ctx, key, max_examples))]
            yield {"kind": kind, "candidate_only": True, "context_sha256": ctx, "group_sha256": key,
                   "occurrences": count, "variants": variants, "examples": examples,
                   "examples_truncated": variants > len(examples),
                   "note": "上下文或中文合法改写可能不同；对照完整原文与术语作用域后处置，不能全局替换。"}
    for ctx, term_id, count, first_count, earliest, first_pos, retain_pos, required in index.execute(
            "SELECT context_key,term_id,COUNT(*),SUM(action='first_use'),MIN(sequence),"
            "MIN(CASE WHEN action='first_use' THEN sequence END),"
            "MIN(CASE WHEN action='retain' THEN sequence END),MAX(required) FROM first_uses GROUP BY context_key,term_id "
            "HAVING SUM(action='first_use')>1 OR (SUM(action='first_use')>0 AND MIN(sequence)<MIN(CASE WHEN action='first_use' THEN sequence END)) "
            "OR (MAX(required)=1 AND SUM(action='first_use')=0)"):
        examples = [json.loads(row[0]) for row in index.execute(
            "SELECT payload FROM first_uses WHERE context_key=? AND term_id=? "
            "ORDER BY CASE WHEN sequence=? THEN 0 WHEN action='first_use' THEN 1 ELSE 2 END,sequence LIMIT ?",
            (ctx, term_id, earliest, max_examples))]
        yield {"kind": "first_use_scope_review", "candidate_only": True, "context_sha256": ctx,
               "term_id": term_id, "occurrences": count, "first_use_count": first_count,
               "retained_before_first_use": first_pos is not None and retain_pos is not None and retain_pos < first_pos,
               "used_before_first_use": first_pos is not None and earliest < first_pos,
               "first_use_not_recorded": bool(required and first_count == 0), "examples": examples,
               "examples_truncated": count > len(examples),
               "note": "scope 为自然语言约定；摘要/正文/分册可能分别首次解释，本项不是自动判错。"}


def audit(root, output, sample=False, max_findings=500, max_examples=6):
    root, output = Path(root).resolve(), Path(output).resolve()
    ledger.need(type(max_findings) is int and 1 <= max_findings <= 100000, "max-findings 必须为1至100000")
    ledger.need(type(max_examples) is int and 2 <= max_examples <= 20, "max-examples 必须为2至20")
    ledger.need(output.is_relative_to(root) and output.suffix == ".jsonl", "报告须为项目内的 .jsonl 文件")
    ledger.portable_sample_path(output.relative_to(root).as_posix())
    ledger.need(not output.exists(), "报告已存在；使用新的版本路径")
    source_db = root / "translation.sqlite3"
    ledger.need(source_db.is_file(), "项目台账不存在；先初始化或恢复")
    output.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    scratch = output.with_name(".audit-" + nonce + ".sqlite3")
    temporary = output.with_name(".audit-" + nonce + ".jsonl")
    db, index = None, None
    try:
        db = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        ledger.need(ledger.get_meta(db, "schema") in (3, ledger.SCHEMA), "请先 resume 备份并升级旧台账")
        source, policy = ledger.get_meta(db, "source"), ledger.get_meta(db, "policy")
        ledger.need(ledger.file_hash(Path(source["path"])) == source["sha256"], "源文件变化；禁止混合审计")
        ledger.verify_snapshots(db)
        ledger.verify_assets(db, root)
        policy_revision = ledger.get_meta(db, "policy_revision")
        index = sqlite3.connect(scratch)
        index.execute("PRAGMA cache_size=-8192")
        index.execute("PRAGMA temp_store=FILE")
        index.execute("CREATE TABLE entries(sequence INTEGER,context_key TEXT,source_key TEXT,target_key TEXT,payload TEXT)")
        index.execute("CREATE TABLE first_uses(sequence INTEGER,context_key TEXT,term_id TEXT,action TEXT,payload TEXT,required INTEGER)")
        stats = {"valid_reviewed_units": 0, "invalid_reviewed_units": 0, "leaf_segments": 0, "legacy_semantic_bindings": 0,
                 "unreviewed_units": db.execute("SELECT COUNT(*) FROM units WHERE state!='reviewed'" + (" AND sample=1" if sample else "")).fetchone()[0],
                 "parent_term_occurrences_requiring_manual_review": 0}
        sequence, term_sequence = 0, 0
        eligibility = hashlib.sha256()
        terms = {term["id"]: term for term in policy["glossary"]}
        for row in db.execute("SELECT * FROM units" + (" WHERE sample=1" if sample else "") + " ORDER BY ordinal"):
            eligibility.update(ledger.encoded([row["id"], row["source_hash"], row["state"], row["result_hash"], row["revision"], row["review"]]).encode("utf-8") + b"\n")
            if row["state"] != "reviewed":
                continue
            valid = ledger.reviewed_payload(row, policy, policy_revision)
            if not valid:
                stats["invalid_reviewed_units"] += 1
                continue
            stats["valid_reviewed_units"] += 1
            legacy = valid["legacy_semantic_binding"]
            stats["legacy_semantic_bindings"] += int(legacy)
            unit, result = valid["unit"], valid["result"]
            nodes = list(zip(unit["segments"], result["segments"])) if "segments" in unit else [(unit, result)]
            if "segments" in unit:
                stats["parent_term_occurrences_requiring_manual_review"] += len(unit.get("term_occurrences", []))
            for node, translated in nodes:
                sequence += 1
                stats["leaf_segments"] += 1
                ctx = context(unit, node, policy, source)
                src, target = exact(node["source"]), exact(translated["target"])
                payload = locations(unit, node, target, legacy)
                if src.strip() and target.strip():
                    index.execute("INSERT INTO entries VALUES (?,?,?,?,?)", (sequence, ledger.digest(ctx), ledger.digest(src), ledger.digest(target), ledger.encoded(payload)))
                for occurrence in sorted(node.get("term_occurrences", []), key=lambda x: x["start"]):
                    term = terms[occurrence["term_id"]]
                    if occurrence["action"] not in ("retain", "first_use") and not (
                            occurrence["action"] == "translate" and term.get("strategy") == "first_use"):
                        continue
                    term_sequence += 1
                    scope = {k: ctx[k] for k in ("document_id", "source_version", "document_type")}
                    scope.update(scope=term["scope"], term_id=term["id"], sense=term["sense"])
                    index.execute("INSERT INTO first_uses VALUES (?,?,?,?,?,?)", (term_sequence, ledger.digest(scope), term["id"], occurrence["action"],
                                  ledger.encoded(dict(payload, action=occurrence["action"], start=occurrence["start"], scope=term["scope"])), int(term.get("strategy") == "first_use")))
        index.execute("CREATE INDEX by_source ON entries(context_key,source_key,target_key)")
        index.execute("CREATE INDEX by_target ON entries(context_key,target_key,source_key)")
        index.execute("CREATE INDEX by_term ON first_uses(context_key,term_id,sequence)")
        index.commit()
        header = {"type": "audit_header", "version": VERSION, "at": ledger.now(), "advisory_only": True,
                  "role": "sample" if sample else "full", "source_sha256": source["sha256"],
                  "inventory_sha256": ledger.get_meta(db, "inventory_sha256"), "policy_sha256": ledger.digest(policy),
                  "content_signature": ledger.content_signature(db, "sample" if sample else "full"), "stats": stats,
                  "checks_revision": ledger.CHECKS_REVISION, "schema": ledger.get_meta(db, "schema"),
                  "review_snapshot_sha256": eligibility.hexdigest()}
        header["audit_snapshot_sha256"] = ledger.digest({k: v for k, v in header.items() if k not in ("at", "stats")})
        counts, total, emitted = Counter(), 0, 0
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(ledger.encoded(header) + "\n")
            for finding in group_candidates(index, max_examples):
                counts[finding["kind"]] += 1
                total += 1
                if emitted >= max_findings:
                    continue
                finding["id"] = ledger.digest([finding["kind"], finding["context_sha256"], finding.get("group_sha256", finding.get("term_id")), header["audit_snapshot_sha256"]])[:20]
                stream.write(ledger.encoded(dict(type="candidate", **finding)) + "\n")
                emitted += 1
            summary = {"type": "audit_summary", "advisory_only": True, "candidate_groups": total, "emitted_groups": emitted,
                       "counts": dict(counts), "truncated": emitted < total,
                       "note": "仅检查当前有效已审范围；未审/失效块、自然语言作用域和提取遗漏仍须另查。无候选不证明准确率。"}
            stream.write(ledger.encoded(summary) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        ledger.need(ledger.file_hash(Path(source["path"])) == source["sha256"], "审计期间源文件变化")
        ledger.verify_assets(db, root)
        db.execute("ROLLBACK")
        os.replace(temporary, output)
        return {"report": str(output), **summary, "stats": stats}
    finally:
        if db is not None:
            db.close()
        if index is not None:
            index.close()
        for path in (temporary, scratch, Path(str(scratch) + "-journal")):
            if path.exists():
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--max-findings", type=int, default=500)
    parser.add_argument("--max-examples", type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(audit(args.project, args.output, args.sample, args.max_findings, args.max_examples), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    configure_stdio()
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
