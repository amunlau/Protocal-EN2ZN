#!/usr/bin/env python3
"""Read-only runtime inventory; does not install packages or call model services."""
import argparse
import json
import sys


def configure_stdio():
    """Keep JSON and Chinese errors readable even through legacy Windows pipes."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def module_check(module, distribution=None):
    import importlib
    import importlib.metadata
    try:
        imported = importlib.import_module(module)
        if module == "sqlite3":
            db = imported.connect(":memory:")
            try:
                db.execute("CREATE TABLE probe(value TEXT)")
                db.execute("INSERT INTO probe VALUES (?)", ("中文 ≤ －",))
                if db.execute("SELECT value FROM probe").fetchone()[0] != "中文 ≤ －":
                    raise RuntimeError("SQLite Unicode round trip failed")
            finally:
                db.close()
            version = imported.sqlite_version
        else:
            try:
                version = importlib.metadata.version(distribution or module)
            except importlib.metadata.PackageNotFoundError:
                version = "unknown"
        return {"available": True, "version": version}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def inspect_environment():
    import shutil
    modules = {name: module_check(name, distribution) for name, distribution in (
        ("sqlite3", None), ("pypdf", None), ("pdfplumber", None), ("docx", "python-docx"))}
    python_ok = sys.version_info >= (3, 10)
    ledger = python_ok and modules["sqlite3"]["available"]
    return {
        "python": {"executable": sys.executable, "version": sys.version.split()[0], "supported": python_ok},
        "modules": modules,
        "capabilities": {
            "ledger": ledger,
            "pdf-text": ledger and modules["pypdf"]["available"],
            "pdf-layout": ledger and modules["pdfplumber"]["available"],
            "docx": python_ok and modules["docx"]["available"],
        },
        "executables_on_path": {name: shutil.which(name) for name in (
            "pdftoppm", "mutool", "soffice", "libreoffice", "tesseract", "ocrmypdf")},
        "note": "只检查此解释器的依赖与 PATH；未验证文件权限、持久存储、中文字体、OCR 语言包、渲染效果或模型视觉能力。外部工具未实际启动；未列出的宿主能力仍可使用。",
    }


def required_missing(report, requirements):
    return sorted({name for name in requirements if not report["capabilities"][name]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="append", default=[], choices=("ledger", "pdf-text", "pdf-layout", "docx"),
                        help="Fail only when a capability needed for this stage is missing; repeat as needed.")
    args = parser.parse_args()
    report = inspect_environment()
    report["missing_required"] = required_missing(report, args.require)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["missing_required"] else 0


if __name__ == "__main__":
    configure_stdio()
    sys.exit(main())
