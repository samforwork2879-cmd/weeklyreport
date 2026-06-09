from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import bleach
import markdown
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "weekly_reports.db"
UPLOAD_DIR = BASE_DIR / "uploads"
WORKSTATIONS_PATH = BASE_DIR / "workstations.json"
LAPP_SNIPPET_DIR = BASE_DIR.parent / "lapp" / "snippets"
LAPP_CUSTOM_SNIPPET_FILE = LAPP_SNIPPET_DIR / "custom.json"
GOOGLE_SHEETS_REPORT_HEADERS = ["id", "title", "week_start", "content_md", "tags", "created_at", "updated_at"]

if find_spec("dotenv") is not None:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")

ALLOWED_TAGS = {
    "p",
    "br",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "target", "rel"]}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

    @app.before_request
    def before_request() -> None:
        g.db = get_db()

    @app.teardown_request
    def teardown_request(_exc: BaseException | None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def index():
        q = request.args.get("q", "").strip()
        mode = request.args.get("mode", "board")
        tag = request.args.get("tag", "").strip()

        reports = query_reports(keyword=q, tag=tag)
        reports_payload = [
            {
                "id": row["id"],
                "title": row["title"],
                "week_start": row["week_start"],
                "tags": row["tags_list"],
            }
            for row in reports
        ]
        top_tags = get_tag_counts(limit=12)

        return render_template(
            "index.html",
            reports=reports,
            q=q,
            mode=mode,
            tag=tag,
            top_tags=top_tags,
            reports_json=json.dumps(reports_payload, ensure_ascii=False),
        )

    @app.route("/workstations")
    def workstation_portal():
        stations = load_workstations()
        return render_template("workstations.html", stations=stations)

    @app.route("/workstations/manage")
    def manage_workstations():
        stations = load_workstations()
        return render_template("workstations_manage.html", stations=stations)

    @app.route("/workstations/<int:station_id>/edit", methods=["GET", "POST"])
    def edit_workstation(station_id: int):
        rows = load_workstations_raw()
        if station_id < 0 or station_id >= len(rows):
            flash("找不到工作站資料", "error")
            return redirect(url_for("manage_workstations"))

        current = rows[station_id]
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            url = request.form.get("url", "").strip()
            icon = request.form.get("icon", "").strip()
            description = request.form.get("description", "").strip()
            work_items_text = request.form.get("work_items", "").strip()
            work_items = [line.strip() for line in work_items_text.splitlines() if line.strip()]

            if not name or not url:
                flash("工作站名稱與網址不可為空", "error")
            else:
                rows[station_id] = {
                    **current,
                    "name": name,
                    "url": url,
                    "icon": icon,
                    "description": description,
                    "work_items": work_items,
                }
                save_workstations_raw(rows)
                flash("工作站資料已更新", "success")
                return redirect(url_for("manage_workstations"))

            current = {
                **current,
                "name": name,
                "url": url,
                "icon": icon,
                "description": description,
                "work_items": work_items,
            }

        station = {
            "name": str(current.get("name", "")).strip(),
            "url": str(current.get("url", "")).strip(),
            "icon": str(current.get("icon", "")).strip(),
            "description": str(current.get("description", "")).strip(),
            "work_items_text": "\n".join(current.get("work_items", []))
            if isinstance(current.get("work_items"), list)
            else "",
        }
        return render_template("workstation_form.html", station=station, station_id=station_id)

    @app.route("/tools/snippets")
    def snippets_tool():
        q = request.args.get("q", "").strip().lower()
        language = request.args.get("language", "").strip().lower()
        snippets = load_all_snippets()

        if q:
            snippets = [
                item
                for item in snippets
                if q in item["name"].lower()
                or q in item.get("description", "").lower()
                or q in item.get("content", "").lower()
                or any(q in tag.lower() for tag in item.get("tags", []))
            ]
        if language:
            snippets = [item for item in snippets if item.get("language", "") == language]

        return render_template("tool_snippets.html", snippets=snippets, q=q, language=language)

    @app.route("/tools/snippets/new", methods=["GET", "POST"])
    def snippets_new():
        if request.method == "POST":
            snippet = parse_snippet_form(request)
            if not snippet["name"] or not snippet["content"]:
                flash("名稱與內容不可為空", "error")
                return render_template("tool_snippet_form.html", action="new", snippet=snippet)

            snippet["id"] = str(uuid4())
            snippet["created_at"] = datetime.now().isoformat(timespec="seconds")
            upsert_custom_snippet(snippet)
            flash("片段已新增", "success")
            return redirect(url_for("snippets_tool"))

        return render_template(
            "tool_snippet_form.html",
            action="new",
            snippet={"name": "", "description": "", "language": "python", "tags_text": "", "content": ""},
        )

    @app.route("/tools/snippets/<string:snippet_id>/edit", methods=["GET", "POST"])
    def snippets_edit(snippet_id: str):
        current = find_snippet_by_id(snippet_id)
        if current is None:
            flash("找不到片段", "error")
            return redirect(url_for("snippets_tool"))

        if request.method == "POST":
            snippet = parse_snippet_form(request)
            if not snippet["name"] or not snippet["content"]:
                flash("名稱與內容不可為空", "error")
                snippet["id"] = snippet_id
                return render_template("tool_snippet_form.html", action="edit", snippet=snippet)

            snippet["id"] = snippet_id
            snippet["created_at"] = current.get("created_at", datetime.now().isoformat(timespec="seconds"))
            upsert_custom_snippet(snippet)
            flash("片段已更新", "success")
            return redirect(url_for("snippets_tool"))

        return render_template(
            "tool_snippet_form.html",
            action="edit",
            snippet={
                "id": current["id"],
                "name": current.get("name", ""),
                "description": current.get("description", ""),
                "language": current.get("language", "python"),
                "tags_text": ", ".join(current.get("tags", [])),
                "content": current.get("content", ""),
            },
        )

    @app.route("/tools/snippets/<string:snippet_id>/delete", methods=["POST"])
    def snippets_delete(snippet_id: str):
        removed = delete_custom_snippet(snippet_id)
        if removed:
            flash("片段已刪除", "success")
        else:
            flash("只能刪除 custom 片段，或找不到此片段", "error")
        return redirect(url_for("snippets_tool"))

    @app.route("/tools/string-group", methods=["GET", "POST"])
    def string_group_tool():
        form = {
            "text": "",
            "group_size": "2",
            "input_mode": "分隔符模式",
            "delimiter_mode": "自動",
            "custom_delimiter": "",
            "output_mode": "SQL",
            "db_name": "",
            "column_names": "",
            "sql_insert_mode": "一般 INSERT",
            "sql_duplicate_mode": "無",
            "sql_empty_to_null": False,
        }
        result = ""
        preview = "-"
        status = ""
        status_type = "info"

        if request.method == "POST":
            form["text"] = request.form.get("text", "")
            form["group_size"] = request.form.get("group_size", "2")
            form["input_mode"] = request.form.get("input_mode", "分隔符模式")
            form["delimiter_mode"] = request.form.get("delimiter_mode", "自動")
            form["custom_delimiter"] = request.form.get("custom_delimiter", "")
            form["output_mode"] = request.form.get("output_mode", "SQL")
            form["db_name"] = request.form.get("db_name", "")
            form["column_names"] = request.form.get("column_names", "")
            form["sql_insert_mode"] = request.form.get("sql_insert_mode", "一般 INSERT")
            form["sql_duplicate_mode"] = request.form.get("sql_duplicate_mode", "無")
            form["sql_empty_to_null"] = request.form.get("sql_empty_to_null") == "on"

            try:
                group_size = int(form["group_size"])
                keep_empty = form["output_mode"] == "SQL" and form["sql_empty_to_null"]
                groups, delimiter_label, item_count = parse_groups_web(
                    text=form["text"],
                    group_size=group_size,
                    input_mode=form["input_mode"],
                    delimiter_mode=form["delimiter_mode"],
                    custom_delimiter=form["custom_delimiter"],
                    keep_empty=keep_empty,
                )

                column_names = parse_column_names_web(form["column_names"]) if form["output_mode"] == "SQL" else []
                if form["output_mode"] == "SQL" and column_names and len(column_names) != group_size:
                    raise ValueError("欄位數量需等於每組大小")

                result = build_output_web(
                    groups=groups,
                    mode=form["output_mode"],
                    db_name=form["db_name"].strip() if form["output_mode"] == "SQL" else None,
                    column_names=column_names,
                    sql_insert_mode=form["sql_insert_mode"],
                    sql_duplicate_mode=form["sql_duplicate_mode"],
                    sql_empty_to_null=form["sql_empty_to_null"],
                )
                preview = format_preview_web(groups)
                status = f"成功：分隔符 {delimiter_label}，項目 {item_count}，分組 {len(groups)}"
                status_type = "success"
            except ValueError as exc:
                status = str(exc)
                status_type = "error"

        return render_template(
            "tool_string_group.html",
            form=form,
            result=result,
            preview=preview,
            status=status,
            status_type=status_type,
        )

    @app.route("/new", methods=["GET", "POST"])
    def create_report():
        templates = query_content_templates()
        available_tags = get_all_tags()
        if request.method == "POST":
            form = get_form_values(request)
            errors = validate_form(form)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "form.html",
                    action="new",
                    form=form,
                    templates=templates,
                    templates_payload=[dict(row) for row in templates],
                    attachments=[],
                    report_id=None,
                    available_tags=available_tags,
                    selected_tags=form["selected_tags"],
                    custom_tags=form["custom_tags"],
                )

            report_id = create_report_record(form)
            save_uploaded_attachments(report_id, request.files.getlist("attachments"))
            clear_draft_data()
            g.db.commit()

            flash("週報已新增", "success")
            return redirect(url_for("index"))

        draft = get_draft_data()
        if draft:
            form = {
                "title": draft["title"],
                "week_start": draft["week_start"],
                "tags": draft["tags"],
                "content_md": draft["content_md"],
            }
        else:
            form = {"title": "", "week_start": "", "tags": "", "content_md": ""}

        return render_template(
            "form.html",
            action="new",
            form=form,
            templates=templates,
            templates_payload=[dict(row) for row in templates],
            attachments=[],
            report_id=None,
            draft=draft,
            available_tags=available_tags,
            selected_tags=get_selected_tags(form["tags"], available_tags),
            custom_tags=get_custom_tags(form["tags"], available_tags),
        )

    @app.route("/report/<int:report_id>/edit", methods=["GET", "POST"])
    def edit_report(report_id: int):
        report = get_report(report_id)
        if report is None:
            flash("找不到這筆週報", "error")
            return redirect(url_for("index"))

        available_tags = get_all_tags()
        for tag_name in report["tags_list"]:
            if not contains_case_insensitive(available_tags, tag_name):
                available_tags.append(tag_name)

        if request.method == "POST":
            form = get_form_values(request)
            errors = validate_form(form)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "form.html",
                    action="edit",
                    form=form,
                    report_id=report_id,
                    templates=[],
                    templates_payload=[],
                    attachments=report["attachments"],
                    available_tags=available_tags,
                    selected_tags=form["selected_tags"],
                    custom_tags=form["custom_tags"],
                )

            update_report_record(report_id, form)
            save_uploaded_attachments(report_id, request.files.getlist("attachments"))
            g.db.commit()
            flash("週報已更新", "success")
            return redirect(url_for("view_report", report_id=report_id))

        form = {
            "title": report["title"],
            "week_start": report["week_start"],
            "tags": report["tags"],
            "content_md": report["content_md"],
        }
        return render_template(
            "form.html",
            action="edit",
            form=form,
            report_id=report_id,
            templates=[],
            templates_payload=[],
            attachments=report["attachments"],
            available_tags=available_tags,
            selected_tags=get_selected_tags(form["tags"], available_tags),
            custom_tags=get_custom_tags(form["tags"], available_tags),
        )

    @app.route("/report/<int:report_id>")
    def view_report(report_id: int):
        report = get_report(report_id)
        if report is None:
            flash("找不到這筆週報", "error")
            return redirect(url_for("index"))
        return render_template("report_view.html", report=report)

    @app.route("/report/<int:report_id>/delete", methods=["POST"])
    def delete_report(report_id: int):
        attachments = query_attachments_by_report(report_id)
        for attachment in attachments:
            delete_attachment_file(attachment)

        g.db.execute("DELETE FROM attachments WHERE report_id = ?", (report_id,))
        delete_report_record(report_id)
        g.db.commit()
        flash("週報已刪除", "success")
        return redirect(url_for("index"))

    @app.route("/preview_markdown", methods=["POST"])
    def preview_markdown():
        content_md = request.form.get("content_md", "")
        return {"html": render_markdown_html(content_md)}

    @app.route("/draft/save", methods=["POST"])
    def save_draft():
        form = get_form_values(request)
        now = datetime.now().isoformat(timespec="seconds")
        g.db.execute(
            """
            INSERT INTO drafts (id, title, week_start, tags, content_md, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                week_start = excluded.week_start,
                tags = excluded.tags,
                content_md = excluded.content_md,
                updated_at = excluded.updated_at
            """,
            (form["title"], form["week_start"], form["tags"], form["content_md"], now),
        )
        g.db.commit()
        return {"ok": True, "updated_at": now}

    @app.route("/draft/clear", methods=["POST"])
    def clear_draft_route():
        clear_draft_data()
        g.db.commit()
        return {"ok": True}

    @app.route("/templates", methods=["GET", "POST"])
    def manage_templates():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            content_md = request.form.get("content_md", "").strip()
            is_default = 1 if request.form.get("is_default") == "on" else 0
            errors = validate_template_form(name, content_md)

            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                if is_default:
                    clear_default_template()
                now = datetime.now().isoformat(timespec="seconds")
                g.db.execute(
                    """
                    INSERT INTO report_templates (name, category, content_md, is_default, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, category, content_md, is_default, now, now),
                )
                g.db.commit()
                flash("模板已新增", "success")
                return redirect(url_for("manage_templates"))

        templates = query_content_templates()
        return render_template("templates.html", templates=templates)

    @app.route("/template/<int:template_id>/edit", methods=["GET", "POST"])
    def edit_template(template_id: int):
        template = get_template(template_id)
        if template is None:
            flash("找不到模板", "error")
            return redirect(url_for("manage_templates"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            content_md = request.form.get("content_md", "").strip()
            is_default = 1 if request.form.get("is_default") == "on" else 0
            errors = validate_template_form(name, content_md)
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                if is_default:
                    clear_default_template(except_id=template_id)
                now = datetime.now().isoformat(timespec="seconds")
                g.db.execute(
                    """
                    UPDATE report_templates
                    SET name = ?, category = ?, content_md = ?, is_default = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, category, content_md, is_default, now, template_id),
                )
                g.db.commit()
                flash("模板已更新", "success")
                return redirect(url_for("manage_templates"))

        return render_template("template_form.html", template=template)

    @app.route("/template/<int:template_id>/delete", methods=["POST"])
    def delete_template(template_id: int):
        g.db.execute("DELETE FROM report_templates WHERE id = ?", (template_id,))
        g.db.commit()
        flash("模板已刪除", "success")
        return redirect(url_for("manage_templates"))

    @app.route("/tags")
    def manage_tags():
        tag_counts = get_tag_counts()
        return render_template("tags.html", tag_counts=tag_counts)

    @app.route("/tags/rename", methods=["POST"])
    def rename_tag():
        old_tag = request.form.get("old_tag", "").strip()
        new_tag = request.form.get("new_tag", "").strip()
        if not old_tag or not new_tag:
            flash("請提供原標籤與新標籤", "error")
            return redirect(url_for("manage_tags"))

        changed = apply_tag_rename(old_tag, new_tag)
        g.db.commit()
        flash(f"已更新 {changed} 筆週報標籤", "success")
        return redirect(url_for("manage_tags"))

    @app.route("/tags/delete", methods=["POST"])
    def delete_tag():
        tag = request.form.get("tag", "").strip()
        if not tag:
            flash("缺少標籤名稱", "error")
            return redirect(url_for("manage_tags"))

        changed = apply_tag_delete(tag)
        g.db.commit()
        flash(f"已從 {changed} 筆週報移除標籤 #{tag}", "success")
        return redirect(url_for("manage_tags"))

    @app.route("/dashboard")
    def dashboard():
        stats = get_dashboard_stats()
        return render_template("dashboard.html", stats=stats)

    @app.route("/attachment/<int:attachment_id>/download")
    def download_attachment(attachment_id: int):
        attachment = get_attachment(attachment_id)
        if attachment is None:
            abort(404)

        file_path = UPLOAD_DIR / attachment["stored_name"]
        if not file_path.exists():
            abort(404)

        return send_file(file_path, as_attachment=True, download_name=attachment["original_name"])

    @app.route("/attachment/<int:attachment_id>/delete", methods=["POST"])
    def delete_attachment(attachment_id: int):
        attachment = get_attachment(attachment_id)
        if attachment is None:
            flash("找不到附件", "error")
            return redirect(url_for("index"))

        report_id = attachment["report_id"]
        delete_attachment_file(attachment)
        g.db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        g.db.commit()
        flash("附件已刪除", "success")
        return redirect(url_for("edit_report", report_id=report_id))

    @app.route("/export")
    def export_reports():
        fmt = request.args.get("format", "md").lower()
        q = request.args.get("q", "").strip()
        tag = request.args.get("tag", "").strip()
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()

        reports = query_reports(keyword=q, tag=tag, start_date=start, end_date=end)
        return send_export(reports, fmt, "weekly_reports")

    @app.route("/report/<int:report_id>/export/<string:fmt>")
    def export_single_report(report_id: int, fmt: str):
        report = get_report(report_id)
        if report is None:
            flash("找不到這筆週報", "error")
            return redirect(url_for("index"))
        return send_export([report], fmt.lower(), f"report_{report_id}")

    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return app


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            week_start TEXT NOT NULL,
            content_md TEXT NOT NULL,
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content_md TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    ensure_template_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT DEFAULT '',
            week_start TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            content_md TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_template_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(report_templates)").fetchall()}
    if "category" not in columns:
        conn.execute("ALTER TABLE report_templates ADD COLUMN category TEXT DEFAULT ''")
    if "is_default" not in columns:
        conn.execute("ALTER TABLE report_templates ADD COLUMN is_default INTEGER DEFAULT 0")


def use_google_sheets_reports() -> bool:
    return bool(os.environ.get("GOOGLE_SHEETS_REPORTS_SPREADSHEET_ID", "").strip())


def get_reports_worksheet():
    if "reports_worksheet" in g:
        return g.reports_worksheet

    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    spreadsheet_id = os.environ.get("GOOGLE_SHEETS_REPORTS_SPREADSHEET_ID", "").strip()
    worksheet_name = os.environ.get("GOOGLE_SHEETS_REPORTS_WORKSHEET", "reports").strip() or "reports"

    if not credentials_path:
        raise RuntimeError("缺少 GOOGLE_APPLICATION_CREDENTIALS，無法連線 Google Sheets")
    if not spreadsheet_id:
        raise RuntimeError("缺少 GOOGLE_SHEETS_REPORTS_SPREADSHEET_ID，無法連線 Google Sheets")

    try:
        import gspread
        from gspread.exceptions import WorksheetNotFound
    except ImportError as exc:
        raise RuntimeError("缺少 gspread 套件，請先安裝 requirements.txt") from exc

    client = gspread.service_account(filename=credentials_path)
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(GOOGLE_SHEETS_REPORT_HEADERS))

    ensure_reports_worksheet_headers(worksheet)
    g.reports_worksheet = worksheet
    return worksheet


def ensure_reports_worksheet_headers(worksheet) -> None:
    headers = worksheet.row_values(1)
    if headers[: len(GOOGLE_SHEETS_REPORT_HEADERS)] == GOOGLE_SHEETS_REPORT_HEADERS:
        return
    worksheet.update("A1:G1", [GOOGLE_SHEETS_REPORT_HEADERS])


def load_sheet_report_rows() -> list[dict]:
    worksheet = get_reports_worksheet()
    rows = worksheet.get_all_records(expected_headers=GOOGLE_SHEETS_REPORT_HEADERS)
    normalized: list[dict] = []
    for row in rows:
        report_id = str(row.get("id", "")).strip()
        if not report_id:
            continue
        try:
            report_id_value = int(report_id)
        except ValueError:
            continue
        normalized.append(
            {
                "id": report_id_value,
                "title": str(row.get("title", "")).strip(),
                "week_start": str(row.get("week_start", "")).strip(),
                "content_md": str(row.get("content_md", "")).strip(),
                "tags": str(row.get("tags", "")).strip(),
                "created_at": str(row.get("created_at", "")).strip(),
                "updated_at": str(row.get("updated_at", "")).strip(),
            }
        )
    return normalized


def find_sheet_row_number(report_id: int) -> int | None:
    worksheet = get_reports_worksheet()
    id_values = worksheet.col_values(1)
    for row_number, value in enumerate(id_values[1:], start=2):
        if str(value).strip() == str(report_id):
            return row_number
    return None


def create_report_record(form: dict[str, object]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    if use_google_sheets_reports():
        worksheet = get_reports_worksheet()
        existing_ids = [row["id"] for row in load_sheet_report_rows()]
        report_id = max(existing_ids, default=0) + 1
        worksheet.append_row(
            [
                report_id,
                form["title"],
                form["week_start"],
                form["content_md"],
                normalize_tags(form["tags"]),
                now,
                now,
            ],
            value_input_option="USER_ENTERED",
        )
        return report_id

    cursor = g.db.execute(
        """
        INSERT INTO reports (title, week_start, content_md, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            form["title"],
            form["week_start"],
            form["content_md"],
            normalize_tags(form["tags"]),
            now,
            now,
        ),
    )
    return cursor.lastrowid


def update_report_record(report_id: int, form: dict[str, object]) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    if use_google_sheets_reports():
        report = get_report(report_id)
        if report is None:
            return False
        row_number = find_sheet_row_number(report_id)
        if row_number is None:
            return False
        get_reports_worksheet().update(
            f"A{row_number}:G{row_number}",
            [
                [
                    report_id,
                    form["title"],
                    form["week_start"],
                    form["content_md"],
                    normalize_tags(form["tags"]),
                    report["created_at"],
                    now,
                ]
            ],
        )
        return True

    g.db.execute(
        """
        UPDATE reports
        SET title = ?, week_start = ?, content_md = ?, tags = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            form["title"],
            form["week_start"],
            form["content_md"],
            normalize_tags(form["tags"]),
            now,
            report_id,
        ),
    )
    return True


def delete_report_record(report_id: int) -> bool:
    if use_google_sheets_reports():
        row_number = find_sheet_row_number(report_id)
        if row_number is None:
            return False
        get_reports_worksheet().delete_rows(row_number)
        return True

    g.db.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return True


def query_reports(
    keyword: str = "",
    tag: str = "",
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    if use_google_sheets_reports():
        keyword_lc = keyword.lower()
        reports = [enrich_report(row) for row in load_sheet_report_rows()]
        if keyword_lc:
            reports = [
                row
                for row in reports
                if keyword_lc in row["title"].lower()
                or keyword_lc in row["content_md"].lower()
                or keyword_lc in row["tags"].lower()
            ]
        if start_date:
            reports = [row for row in reports if row["week_start"] >= start_date]
        if end_date:
            reports = [row for row in reports if row["week_start"] <= end_date]
        if tag:
            reports = [row for row in reports if tag in row["tags_list"]]
        reports.sort(key=lambda row: (row["week_start"], row["updated_at"]), reverse=True)

        attachment_map = get_attachments_map([row["id"] for row in reports])
        for row in reports:
            row["attachments"] = attachment_map.get(row["id"], [])
        return reports

    sql = "SELECT * FROM reports WHERE 1=1"
    params: list[str] = []
    if keyword:
        like = f"%{keyword}%"
        sql += " AND (title LIKE ? OR content_md LIKE ? OR tags LIKE ?)"
        params.extend([like, like, like])
    if start_date:
        sql += " AND week_start >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND week_start <= ?"
        params.append(end_date)
    sql += " ORDER BY week_start DESC, updated_at DESC"

    rows = g.db.execute(sql, params).fetchall()
    reports = [enrich_report(row) for row in rows]
    if tag:
        reports = [row for row in reports if tag in row["tags_list"]]

    attachment_map = get_attachments_map([row["id"] for row in reports])
    for row in reports:
        row["attachments"] = attachment_map.get(row["id"], [])
    return reports


def get_report(report_id: int) -> dict | None:
    if use_google_sheets_reports():
        report = next((row for row in load_sheet_report_rows() if row["id"] == report_id), None)
        if report is None:
            return None
        report = enrich_report(report)
        report["attachments"] = query_attachments_by_report(report_id)
        return report

    row = g.db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        return None

    report = enrich_report(row)
    report["attachments"] = query_attachments_by_report(report_id)
    return report


def query_content_templates() -> list[sqlite3.Row]:
    return g.db.execute(
        "SELECT * FROM report_templates ORDER BY is_default DESC, updated_at DESC"
    ).fetchall()


def get_template(template_id: int) -> sqlite3.Row | None:
    return g.db.execute("SELECT * FROM report_templates WHERE id = ?", (template_id,)).fetchone()


def enrich_report(row: sqlite3.Row) -> dict:
    row_dict = dict(row)
    row_dict["content_html"] = render_markdown_html(row_dict["content_md"])
    row_dict["tags_list"] = parse_tags(row_dict["tags"])
    return row_dict


def render_markdown_html(content_md: str) -> str:
    extensions = ["extra", "sane_lists", "tables"]
    if find_spec("pymdownx") is not None:
        extensions.append("pymdownx.tilde")

    html = markdown.markdown(
        content_md,
        extensions=extensions,
        output_format="html5",
    )
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )


def parse_tags(tags: str) -> list[str]:
    return [tag.strip() for tag in tags.split(",") if tag.strip()]


def normalize_tags(tags: str) -> str:
    deduped = []
    seen = set()
    for tag in parse_tags(tags):
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tag)
    return ", ".join(deduped)


def merge_tag_values(selected_tags: list[str], custom_tags: str) -> str:
    selected = [tag.strip() for tag in selected_tags if tag.strip()]
    custom = parse_tags(custom_tags)
    return normalize_tags(", ".join(selected + custom))


def contains_case_insensitive(items: list[str], value: str) -> bool:
    value_lc = value.lower()
    return any(item.lower() == value_lc for item in items)


def get_selected_tags(tags: str, available_tags: list[str]) -> list[str]:
    selected: list[str] = []
    lookup = {tag.lower(): tag for tag in available_tags}
    for tag in parse_tags(tags):
        tag_lc = tag.lower()
        if tag_lc in lookup and lookup[tag_lc] not in selected:
            selected.append(lookup[tag_lc])
    return selected


def get_custom_tags(tags: str, available_tags: list[str]) -> str:
    available_lc = {tag.lower() for tag in available_tags}
    custom = [tag for tag in parse_tags(tags) if tag.lower() not in available_lc]
    return ", ".join(custom)


def get_form_values(req) -> dict[str, object]:
    selected_tags = [item.strip() for item in req.form.getlist("selected_tags") if item.strip()]
    custom_tags = req.form.get("custom_tags", "").strip()
    selected_and_custom = merge_tag_values(selected_tags, custom_tags)
    fallback_tags = req.form.get("tags", "").strip()

    return {
        "title": req.form.get("title", "").strip(),
        "week_start": req.form.get("week_start", "").strip(),
        "tags": selected_and_custom if selected_and_custom else fallback_tags,
        "content_md": req.form.get("content_md", "").strip(),
        "selected_tags": selected_tags,
        "custom_tags": custom_tags,
    }


def validate_form(form: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if not form["title"]:
        errors.append("標題不可為空")
    if not form["week_start"]:
        errors.append("請填寫週次起始日期")
    if not form["content_md"]:
        errors.append("內容不可為空")
    return errors


def validate_template_form(name: str, content_md: str) -> list[str]:
    errors: list[str] = []
    if not name:
        errors.append("模板名稱不可為空")
    if not content_md:
        errors.append("模板內容不可為空")
    return errors


def clear_default_template(except_id: int | None = None) -> None:
    if except_id is None:
        g.db.execute("UPDATE report_templates SET is_default = 0 WHERE is_default = 1")
    else:
        g.db.execute(
            "UPDATE report_templates SET is_default = 0 WHERE is_default = 1 AND id != ?",
            (except_id,),
        )


def get_draft_data() -> dict | None:
    row = g.db.execute("SELECT * FROM drafts WHERE id = 1").fetchone()
    return dict(row) if row else None


def clear_draft_data() -> None:
    g.db.execute("DELETE FROM drafts WHERE id = 1")


def save_uploaded_attachments(report_id: int, files) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for file_storage in files:
        if file_storage is None or not file_storage.filename:
            continue

        original_name = secure_filename(file_storage.filename)
        if not original_name:
            continue

        suffix = Path(original_name).suffix
        stored_name = f"{report_id}_{uuid4().hex}{suffix}"
        target_path = UPLOAD_DIR / stored_name
        file_storage.save(target_path)

        file_size = target_path.stat().st_size if target_path.exists() else 0
        g.db.execute(
            """
            INSERT INTO attachments (report_id, original_name, stored_name, file_size, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, original_name, stored_name, file_size, now),
        )


def get_attachments_map(report_ids: list[int]) -> dict[int, list[dict]]:
    if not report_ids:
        return {}

    placeholders = ",".join("?" for _ in report_ids)
    rows = g.db.execute(
        f"SELECT * FROM attachments WHERE report_id IN ({placeholders}) ORDER BY created_at DESC",
        report_ids,
    ).fetchall()

    result: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        row_dict = dict(row)
        result[row_dict["report_id"]].append(row_dict)
    return result


def query_attachments_by_report(report_id: int) -> list[dict]:
    rows = g.db.execute(
        "SELECT * FROM attachments WHERE report_id = ? ORDER BY created_at DESC", (report_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_attachment(attachment_id: int) -> dict | None:
    row = g.db.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def delete_attachment_file(attachment: dict) -> None:
    file_path = UPLOAD_DIR / attachment["stored_name"]
    if file_path.exists():
        file_path.unlink(missing_ok=True)


def get_tag_counts(limit: int | None = None) -> list[tuple[str, int]]:
    if use_google_sheets_reports():
        rows = load_sheet_report_rows()
        counter: Counter[str] = Counter()
        for row in rows:
            counter.update(parse_tags(row["tags"]))
        return counter.most_common(limit) if limit else counter.most_common()

    rows = g.db.execute("SELECT tags FROM reports").fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(parse_tags(row["tags"]))
    most_common = counter.most_common(limit) if limit else counter.most_common()
    return most_common


def get_all_tags() -> list[str]:
    return [tag for tag, _count in get_tag_counts()]


def apply_tag_rename(old_tag: str, new_tag: str) -> int:
    if use_google_sheets_reports():
        changed = 0
        for row in load_sheet_report_rows():
            tags = parse_tags(row["tags"])
            replaced = [new_tag if tag == old_tag else tag for tag in tags]
            if replaced == tags:
                continue
            update_report_record(
                row["id"],
                {
                    "title": row["title"],
                    "week_start": row["week_start"],
                    "content_md": row["content_md"],
                    "tags": normalize_tags(", ".join(replaced)),
                },
            )
            changed += 1
        return changed

    changed = 0
    rows = g.db.execute("SELECT id, tags FROM reports").fetchall()
    for row in rows:
        tags = parse_tags(row["tags"])
        replaced = [new_tag if tag == old_tag else tag for tag in tags]
        if replaced == tags:
            continue
        g.db.execute(
            "UPDATE reports SET tags = ?, updated_at = ? WHERE id = ?",
            (
                normalize_tags(", ".join(replaced)),
                datetime.now().isoformat(timespec="seconds"),
                row["id"],
            ),
        )
        changed += 1
    return changed


def apply_tag_delete(tag: str) -> int:
    if use_google_sheets_reports():
        changed = 0
        for row in load_sheet_report_rows():
            tags = parse_tags(row["tags"])
            filtered = [item for item in tags if item != tag]
            if filtered == tags:
                continue
            update_report_record(
                row["id"],
                {
                    "title": row["title"],
                    "week_start": row["week_start"],
                    "content_md": row["content_md"],
                    "tags": normalize_tags(", ".join(filtered)),
                },
            )
            changed += 1
        return changed

    changed = 0
    rows = g.db.execute("SELECT id, tags FROM reports").fetchall()
    for row in rows:
        tags = parse_tags(row["tags"])
        filtered = [item for item in tags if item != tag]
        if filtered == tags:
            continue
        g.db.execute(
            "UPDATE reports SET tags = ?, updated_at = ? WHERE id = ?",
            (
                normalize_tags(", ".join(filtered)),
                datetime.now().isoformat(timespec="seconds"),
                row["id"],
            ),
        )
        changed += 1
    return changed


def get_dashboard_stats() -> dict:
    if use_google_sheets_reports():
        reports = load_sheet_report_rows()
        total_templates = g.db.execute("SELECT COUNT(*) AS cnt FROM report_templates").fetchone()["cnt"]
        total_attachments = g.db.execute("SELECT COUNT(*) AS cnt FROM attachments").fetchone()["cnt"]
        current_month = datetime.now().strftime("%Y-%m")
        reports_this_month = len([row for row in reports if row["week_start"].startswith(current_month)])

        weekly_counter: Counter[str] = Counter()
        for row in reports:
            try:
                week_key = datetime.fromisoformat(row["week_start"]).strftime("%Y-W%W")
            except ValueError:
                week_key = row["week_start"]
            weekly_counter[week_key] += 1
        weekly_counts = [
            {"week_key": week_key, "cnt": count}
            for week_key, count in sorted(weekly_counter.items(), reverse=True)[:8]
        ]
        weekly_counts.reverse()
        max_weekly = max((row["cnt"] for row in weekly_counts), default=1)

        return {
            "total_reports": len(reports),
            "total_templates": total_templates,
            "total_attachments": total_attachments,
            "reports_this_month": reports_this_month,
            "top_tags": get_tag_counts(limit=8),
            "weekly_counts": weekly_counts,
            "max_weekly": max_weekly,
        }

    total_reports = g.db.execute("SELECT COUNT(*) AS cnt FROM reports").fetchone()["cnt"]
    total_templates = g.db.execute("SELECT COUNT(*) AS cnt FROM report_templates").fetchone()["cnt"]
    total_attachments = g.db.execute("SELECT COUNT(*) AS cnt FROM attachments").fetchone()["cnt"]

    current_month = datetime.now().strftime("%Y-%m")
    reports_this_month = g.db.execute(
        "SELECT COUNT(*) AS cnt FROM reports WHERE substr(week_start, 1, 7) = ?",
        (current_month,),
    ).fetchone()["cnt"]

    weekly_rows = g.db.execute(
        """
        SELECT strftime('%Y-W%W', week_start) AS week_key, COUNT(*) AS cnt
        FROM reports
        GROUP BY week_key
        ORDER BY week_key DESC
        LIMIT 8
        """
    ).fetchall()
    weekly_counts = [dict(row) for row in reversed(weekly_rows)]
    max_weekly = max((row["cnt"] for row in weekly_counts), default=1)

    return {
        "total_reports": total_reports,
        "total_templates": total_templates,
        "total_attachments": total_attachments,
        "reports_this_month": reports_this_month,
        "top_tags": get_tag_counts(limit=8),
        "weekly_counts": weekly_counts,
        "max_weekly": max_weekly,
    }


def send_export(reports: list[dict], fmt: str, filename_prefix: str):
    if fmt != "md":
        flash("不支援的匯出格式", "error")
        return redirect(url_for("index"))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    content = build_markdown_export(reports).encode("utf-8")
    return send_file(
        io.BytesIO(content),
        as_attachment=True,
        download_name=f"{filename_prefix}_{timestamp}.md",
        mimetype="text/markdown",
    )


def load_workstations_raw() -> list[dict]:
    fallback = [
        {"name": "工作站 A", "url": "https://example.com/workstation-a", "icon": "", "description": "", "work_items": []},
        {"name": "工作站 B", "url": "https://example.com/workstation-b", "icon": "", "description": "", "work_items": []},
        {"name": "工作站 C", "url": "https://example.com/workstation-c", "icon": "", "description": "", "work_items": []},
    ]
    if not WORKSTATIONS_PATH.exists():
        return fallback

    try:
        rows = json.loads(WORKSTATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback

    if not isinstance(rows, list):
        return fallback

    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        url = str(row.get("url", "")).strip()
        icon = str(row.get("icon", "")).strip()
        description = str(row.get("description", "")).strip()
        work_items_raw = row.get("work_items", [])
        work_items = work_items_raw if isinstance(work_items_raw, list) else []
        work_items = [str(item).strip() for item in work_items if str(item).strip()]
        if not name or not url:
            continue
        normalized.append(
            {
                "name": name,
                "url": url,
                "icon": icon,
                "description": description,
                "work_items": work_items,
            }
        )

    return normalized if normalized else fallback


def save_workstations_raw(rows: list[dict]) -> None:
    WORKSTATIONS_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def load_workstations() -> list[dict[str, object]]:
    rows = load_workstations_raw()
    stations: list[dict[str, object]] = []
    for idx, row in enumerate(rows):
        name = str(row.get("name", "")).strip()
        url = str(row.get("url", "")).strip()
        icon = str(row.get("icon", "")).strip()
        description = str(row.get("description", "")).strip()
        work_items_raw = row.get("work_items", [])
        work_items = work_items_raw if isinstance(work_items_raw, list) else []
        work_items = [str(item).strip() for item in work_items if str(item).strip()]
        if not name or not url:
            continue
        stations.append(
            {
                "id": idx,
                "name": name,
                "url": url,
                "icon": icon if icon else build_favicon_url(url),
                "initial": name[:1].upper(),
                "description": description,
                "work_items": work_items,
            }
        )
    return stations


def build_favicon_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.strip()
    if not host:
        return ""
    return f"https://www.google.com/s2/favicons?domain={host}&sz=64"


def normalize_snippet_language(raw_language: str) -> str:
    key = str(raw_language or "").strip().lower()
    aliases = {
        "python": "python",
        "py": "python",
        "sql": "sql",
        "c#": "csharp",
        "csharp": "csharp",
        "c-sharp": "csharp",
        "cs": "csharp",
        "other": "other",
        "其他": "other",
    }
    return aliases.get(key, "other")


def load_snippet_file(path: Path) -> list[dict]:
    try:
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def snippet_to_record(raw: dict, source: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()
    content = str(raw.get("content", ""))
    if not name or not content:
        return None
    tags = raw.get("tags", [])
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    elif not isinstance(tags, list):
        tags = []

    snippet_id = str(raw.get("id") or uuid4())
    return {
        "id": snippet_id,
        "name": name,
        "description": str(raw.get("description", "")).strip(),
        "language": normalize_snippet_language(raw.get("language", "python")),
        "tags": [str(item).strip() for item in tags if str(item).strip()],
        "content": content,
        "source": source,
        "is_custom": source == "custom",
        "created_at": str(raw.get("created_at", "")).strip(),
    }


def ensure_custom_snippet_file() -> None:
    LAPP_SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    if not LAPP_CUSTOM_SNIPPET_FILE.exists():
        LAPP_CUSTOM_SNIPPET_FILE.write_text("[]", encoding="utf-8")


def load_all_snippets() -> list[dict]:
    ensure_custom_snippet_file()
    by_id: dict[str, dict] = {}
    for path in sorted(LAPP_SNIPPET_DIR.glob("*.json")):
        source = path.stem
        for item in load_snippet_file(path):
            row = snippet_to_record(item, source)
            if not row:
                continue
            existing = by_id.get(row["id"])
            if not existing or (row["is_custom"] and not existing["is_custom"]):
                by_id[row["id"]] = row
    return sorted(by_id.values(), key=lambda x: x["name"].lower())


def load_custom_snippets() -> list[dict]:
    ensure_custom_snippet_file()
    result: list[dict] = []
    for item in load_snippet_file(LAPP_CUSTOM_SNIPPET_FILE):
        row = snippet_to_record(item, "custom")
        if row:
            result.append(row)
    return result


def save_custom_snippets(rows: list[dict]) -> None:
    payload = [
        {
            "id": row["id"],
            "name": row["name"],
            "description": row.get("description", ""),
            "language": normalize_snippet_language(row.get("language", "python")),
            "tags": row.get("tags", []),
            "content": row["content"],
            "created_at": row.get("created_at", ""),
        }
        for row in rows
    ]
    LAPP_CUSTOM_SNIPPET_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_custom_snippet(snippet: dict) -> None:
    rows = load_custom_snippets()
    rows = [row for row in rows if row["id"] != snippet["id"]]
    rows.append({**snippet, "source": "custom", "is_custom": True})
    save_custom_snippets(rows)


def find_snippet_by_id(snippet_id: str) -> dict | None:
    for row in load_all_snippets():
        if row["id"] == snippet_id:
            return row
    return None


def delete_custom_snippet(snippet_id: str) -> bool:
    rows = load_custom_snippets()
    before = len(rows)
    rows = [row for row in rows if row["id"] != snippet_id]
    if len(rows) == before:
        return False
    save_custom_snippets(rows)
    return True


def parse_snippet_form(req) -> dict:
    tags_text = req.form.get("tags_text", "").strip()
    tags = [item.strip() for item in re.split(r"[,\n]", tags_text) if item.strip()]
    return {
        "name": req.form.get("name", "").strip(),
        "description": req.form.get("description", "").strip(),
        "language": normalize_snippet_language(req.form.get("language", "python")),
        "tags": tags,
        "tags_text": ", ".join(tags),
        "content": req.form.get("content", "").rstrip(),
    }


def detect_delimiter_web(text: str) -> tuple[str, str]:
    if "," in text:
        return ",", "逗號"
    if "\t" in text:
        return "\t", "TAB"
    if ";" in text:
        return ";", "分號"
    if " " in text:
        return " ", "空格"
    raise ValueError("無法自動偵測分隔符")


def split_items_web(text: str, delimiter: str, keep_empty: bool = False) -> list[str]:
    parts = [item.strip() for item in text.split(delimiter)]
    return parts if keep_empty else [item for item in parts if item]


def resolve_delimiter_web(text: str, delimiter_mode: str, custom_delimiter: str) -> tuple[str, str]:
    if delimiter_mode == "自動":
        delimiter, name = detect_delimiter_web(text)
        return delimiter, f"自動（{name}）"

    mapping = {
        "逗號 (,)": (",", "逗號"),
        "TAB": ("\t", "TAB"),
        "分號 (;)": (";", "分號"),
        "空格": (" ", "空格"),
    }
    if delimiter_mode in mapping:
        return mapping[delimiter_mode]
    if delimiter_mode == "自訂":
        if not custom_delimiter:
            raise ValueError("請輸入自訂分隔符")
        return custom_delimiter, f"自訂（{custom_delimiter}）"
    raise ValueError("未知分隔符模式")


def parse_groups_web(
    text: str,
    group_size: int,
    input_mode: str,
    delimiter_mode: str,
    custom_delimiter: str,
    keep_empty: bool,
) -> tuple[list[list[str]], str, int]:
    if not text.strip():
        raise ValueError("尚未輸入內容")
    if group_size <= 0:
        raise ValueError("分組大小必須大於 0")

    if input_mode == "每行一筆":
        items = [line.strip() for line in text.splitlines() if line.strip()]
        if not items:
            raise ValueError("尚未輸入內容")
        if len(items) % group_size != 0:
            raise ValueError("項目數量無法被分組大小整除")
        groups = [items[i : i + group_size] for i in range(0, len(items), group_size)]
        return groups, "不適用", len(items)

    delimiter, delimiter_label = resolve_delimiter_web(text, delimiter_mode, custom_delimiter)

    if input_mode == "分隔符模式":
        items = split_items_web(text, delimiter, keep_empty=keep_empty)
        if not items:
            raise ValueError("沒有可分組的有效項目")
        if len(items) % group_size != 0:
            raise ValueError("項目數量無法被分組大小整除")
        groups = [items[i : i + group_size] for i in range(0, len(items), group_size)]
        return groups, delimiter_label, len(items)

    if input_mode == "每行一組":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("尚未輸入內容")
        groups: list[list[str]] = []
        item_count = 0
        for line_no, line in enumerate(lines, start=1):
            row_items = split_items_web(line, delimiter, keep_empty=keep_empty)
            if len(row_items) != group_size:
                raise ValueError(f"第 {line_no} 行項目數為 {len(row_items)}，需為 {group_size}")
            groups.append(row_items)
            item_count += len(row_items)
        return groups, delimiter_label, item_count

    raise ValueError("未知輸入模式")


def parse_column_names_web(raw: str) -> list[str]:
    if not raw.strip():
        return []
    names = [name.strip() for name in raw.split(",")]
    if any(not name for name in names):
        raise ValueError("欄位名稱格式錯誤，請用逗號分隔且勿留空")
    return names


def build_output_web(
    groups: list[list[str]],
    mode: str,
    db_name: str | None = None,
    column_names: list[str] | None = None,
    sql_insert_mode: str = "一般 INSERT",
    sql_duplicate_mode: str = "無",
    sql_empty_to_null: bool = False,
) -> str:
    if mode == "SQL":
        if not db_name:
            raise ValueError("請輸入資料庫名稱")
        cols = column_names or []

        def esc(item: str) -> str:
            return item.replace("'", "''")

        def sql_literal(item: str) -> str:
            if sql_empty_to_null and item == "":
                return "NULL"
            return f"'{esc(item)}'"

        insert_prefix = "insert ignore into" if sql_insert_mode == "INSERT IGNORE" else "insert into"
        columns_sql = f" ({','.join(cols)})" if cols else ""
        duplicate_sql = ""
        if sql_duplicate_mode == "更新全部欄位":
            if not cols:
                raise ValueError("啟用衝突更新時，請輸入欄位名稱")
            duplicate_sql = "on duplicate key update " + ",".join(f"{name}=VALUES({name})" for name in cols)

        value_rows = ["(" + ",".join(sql_literal(item) for item in group) + ")" for group in groups]
        if len(value_rows) > 1:
            sql = f"{insert_prefix} {db_name}{columns_sql} values\n" + ",\n".join(value_rows)
        else:
            sql = f"{insert_prefix} {db_name}{columns_sql} values {value_rows[0]}"
        if duplicate_sql:
            sql += f"\n{duplicate_sql}"
        return sql + ";"

    if mode == "Python":
        return ", ".join("(" + ", ".join(f"'{item}'" for item in group) + ")" for group in groups)

    if mode == "JSON":
        return json.dumps(groups, ensure_ascii=False)

    raise ValueError("未知輸出格式")


def format_preview_web(groups: list[list[str]], max_groups: int = 3) -> str:
    if not groups:
        return "-"
    preview = " | ".join("(" + ", ".join(group) + ")" for group in groups[:max_groups])
    if len(groups) > max_groups:
        preview += " | ..."
    return preview


def build_markdown_export(reports: list[dict]) -> str:
    lines = ["# 工作週報匯出", f"- 匯出時間: {datetime.now().isoformat(timespec='seconds')}"]
    for row in reports:
        lines.append("")
        lines.append(f"## {row['title']}")
        lines.append(f"- 週次起始: {row['week_start']}")
        lines.append(f"- 標籤: {', '.join(row['tags_list']) if row['tags_list'] else '無'}")
        lines.append("")
        lines.append(row["content_md"])
    return "\n".join(lines)


app = create_app()


if __name__ == "__main__":
    # app.run(debug=True)
    port = int(os.environ.get("PORT", 5000))
    # 必須監聽 0.0.0.0 才能讓外部網路連入
    app.run(host="0.0.0.0", port=port)
