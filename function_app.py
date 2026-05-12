import base64
import csv
import html
import io
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import azure.functions as func
import psycopg2
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential


app = func.FunctionApp()

APP_VERSION = "2026-05-12-safe-db-intents-v1"
FOUNDRY_ENDPOINT = os.getenv(
    "FOUNDRY_ENDPOINT",
    "https://elisabethlevana-1280-resource.services.ai.azure.com/api/projects/elisabethlevana-1280",
)
FOUNDRY_AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "aurora")
FOUNDRY_AGENT_VERSION = os.getenv("FOUNDRY_AGENT_VERSION", "1")
AZURE_SEARCH_ENDPOINT = os.getenv(
    "AZURE_SEARCH_ENDPOINT",
    "https://aurora-semantic-search.search.windows.net",
)
AZURE_SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")
AZURE_SEARCH_INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "aurora-source-evidence")
AZURE_SEARCH_API_VERSION = os.getenv("AZURE_SEARCH_API_VERSION", "2024-07-01")

SEARCH_INDEX_READY = False


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        dbname=os.getenv("DB_NAME", "postgres"),
        sslmode="require",
        connect_timeout=10,
    )


def get_openai_client():
    project_client = AIProjectClient(
        endpoint=FOUNDRY_ENDPOINT,
        credential=DefaultAzureCredential(),
    )
    return project_client.get_openai_client()


def azure_search_configured():
    return bool(AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_API_KEY and AZURE_SEARCH_INDEX_NAME)


def azure_search_request(method, path, payload=None):
    if not azure_search_configured():
        return None

    endpoint = AZURE_SEARCH_ENDPOINT.rstrip("/")
    separator = "&" if "?" in path else "?"
    url = f"{endpoint}{path}{separator}api-version={AZURE_SEARCH_API_VERSION}"
    data = None
    headers = {
        "Content-Type": "application/json",
        "api-key": AZURE_SEARCH_API_KEY,
    }
    if payload is not None:
        data = json.dumps(payload, default=str).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)


def ensure_search_index():
    global SEARCH_INDEX_READY
    if SEARCH_INDEX_READY or not azure_search_configured():
        return

    index_payload = {
        "name": AZURE_SEARCH_INDEX_NAME,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "project_id", "type": "Edm.String", "searchable": False, "filterable": True, "facetable": True},
            {"name": "source_id", "type": "Edm.String", "searchable": True, "filterable": True, "sortable": True},
            {"name": "source_type", "type": "Edm.String", "searchable": False, "filterable": True, "facetable": True},
            {"name": "source_datetime", "type": "Edm.String", "searchable": False, "filterable": True, "sortable": True},
            {"name": "subject", "type": "Edm.String", "searchable": True, "filterable": False},
            {"name": "title", "type": "Edm.String", "searchable": True, "filterable": False},
            {"name": "sender", "type": "Edm.String", "searchable": True, "filterable": True},
            {"name": "recipients", "type": "Edm.String", "searchable": True, "filterable": False},
            {"name": "attendees", "type": "Edm.String", "searchable": True, "filterable": False},
            {"name": "content", "type": "Edm.String", "searchable": True, "filterable": False},
            {"name": "ingested_at", "type": "Edm.String", "searchable": False, "filterable": True, "sortable": True},
        ],
    }

    azure_search_request("PUT", f"/indexes/{urllib.parse.quote(AZURE_SEARCH_INDEX_NAME)}", index_payload)
    SEARCH_INDEX_READY = True


def make_search_document_id(project_id, source_id):
    raw_id = f"{project_id}-{source_id}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw_id)


def index_source_document(project_id, source_id, text_content, metadata):
    if not azure_search_configured():
        return False

    try:
        ensure_search_index()
        document = {
            "@search.action": "mergeOrUpload",
            "id": make_search_document_id(project_id, source_id),
            "project_id": project_id,
            "source_id": source_id,
            "source_type": metadata.get("source_type", ""),
            "source_datetime": metadata.get("source_datetime", ""),
            "subject": metadata.get("subject", ""),
            "title": metadata.get("title", ""),
            "sender": metadata.get("sender", ""),
            "recipients": metadata.get("recipients", ""),
            "attendees": metadata.get("attendees", ""),
            "content": text_content,
            "ingested_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        azure_search_request(
            "POST",
            f"/indexes/{urllib.parse.quote(AZURE_SEARCH_INDEX_NAME)}/docs/index",
            {"value": [document]},
        )
        return True
    except Exception as e:
        logging.warning(f"Azure AI Search indexing skipped/failed for source_id={source_id}: {e}")
        return False


def escape_search_filter_value(value):
    return str(value).replace("'", "''")


def search_source_evidence(query, project_id=None, top=5):
    if not azure_search_configured() or not empty_to_none(query):
        return []

    try:
        ensure_search_index()
        payload = {
            "search": str(query),
            "top": top,
            "select": "source_id,project_id,source_type,source_datetime,subject,title,sender,content",
        }
        if project_id:
            payload["filter"] = f"project_id eq '{escape_search_filter_value(project_id)}'"

        result = azure_search_request(
            "POST",
            f"/indexes/{urllib.parse.quote(AZURE_SEARCH_INDEX_NAME)}/docs/search",
            payload,
        )
        return result.get("value", []) if result else []
    except Exception as e:
        logging.warning(f"Azure AI Search query skipped/failed: {e}")
        return []


def build_search_context(results):
    if not results:
        return "No matching source evidence found in Azure AI Search."

    lines = ["Relevant source evidence from Azure AI Search:"]
    for item in results:
        content = normalize_text_for_match(item.get("content", ""))
        if len(content) > 500:
            content = content[:500] + "..."
        label = item.get("subject") or item.get("title") or item.get("source_id")
        lines.append(
            f"- {item.get('source_id')} ({item.get('source_type')}, {item.get('project_id')}): "
            f"{label}. Evidence: {content}"
        )
    return "\n".join(lines)


def extract_reference_ids(text):
    return sorted({match.upper() for match in re.findall(r"\b(?:DR|D|T|PRJ)\d+\b", text or "", flags=re.I)})


def format_draft_lookup_response(rows, requested_ids):
    requested = set(requested_ids)
    exact_rows = [row for row in rows if str(row[0]).upper() in requested]

    if not exact_rows:
        ids = ", ".join(html.escape(item) for item in sorted(requested))
        return (
            f"<h3>Draft Not Found</h3>"
            f"I could not find {ids} in <b>draft_plan_updates</b>. "
            "Please check whether the draft ID is correct or whether it was created before the latest database refresh."
        )

    parts = ["<h3>Draft Reason</h3>"]
    for row in exact_rows:
        (
            draft_id,
            project_id,
            source_id,
            update_type,
            task_title,
            owner_name,
            due_date,
            status,
            confidence,
            change_summary,
            review_status,
            needs_clarification,
            clarification_question,
            source_evidence,
            matched_task_id,
        ) = row

        reason = change_summary or f"{update_type} extracted from source {source_id}"
        parts.append(
            "<ul>"
            f"<li><b>Draft:</b> {html.escape(str(draft_id))}</li>"
            f"<li><b>Project:</b> {html.escape(str(project_id))}</li>"
            f"<li><b>Reason created:</b> {html.escape(str(reason))}</li>"
            f"<li><b>Update type:</b> {html.escape(str(update_type or ''))}</li>"
            f"<li><b>Matched task:</b> {html.escape(str(matched_task_id or 'No confident match'))}</li>"
            f"<li><b>Task title:</b> {html.escape(str(task_title or ''))}</li>"
            f"<li><b>Owner:</b> {html.escape(str(owner_name or ''))}</li>"
            f"<li><b>Due date:</b> {html.escape(str(due_date or ''))}</li>"
            f"<li><b>Status:</b> {html.escape(str(status or ''))}</li>"
            f"<li><b>Confidence:</b> {html.escape(str(confidence or ''))}</li>"
            f"<li><b>Review status:</b> {html.escape(str(review_status or ''))}</li>"
            f"<li><b>Clarification:</b> {html.escape(str(clarification_question or 'None'))}</li>"
            f"<li><b>Source:</b> {html.escape(str(source_id or ''))}</li>"
            f"<li><b>Evidence:</b> {html.escape(str(source_evidence or ''))}</li>"
            "</ul>"
        )
    return "".join(parts)


def h(value):
    return html.escape(str(value if value is not None else ""))


def format_pending_approvals_response(rows, project_id=None):
    title = f"Pending Approvals for {h(project_id)}" if project_id else "Pending Approvals"
    if not rows:
        scope = f" for {h(project_id)}" if project_id else ""
        return f"<h3>{title}</h3>No pending or clarification drafts found{scope}."

    parts = [f"<h3>{title}</h3>", "<ul>"]
    for row in rows:
        (
            draft_id,
            row_project_id,
            update_type,
            task_title,
            change_summary,
            confidence,
            review_status,
            clarification_question,
            matched_task_id,
            source_id,
        ) = row
        parts.append(
            "<li>"
            f"<b>{h(draft_id)}</b> ({h(row_project_id)}) - {h(review_status)} / {h(confidence)}<br>"
            f"{h(change_summary or update_type)}<br>"
            f"Task: {h(task_title or matched_task_id or 'No confident match')} | Source: {h(source_id)}"
            f"{'<br>Clarification: ' + h(clarification_question) if clarification_question else ''}"
            "</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def format_blocked_tasks_response(rows, project_id=None):
    title = f"Blocked Tasks for {h(project_id)}" if project_id else "Blocked Tasks"
    if not rows:
        scope = f" for {h(project_id)}" if project_id else ""
        return f"<h3>{title}</h3>No blocked tasks found{scope}."

    parts = [f"<h3>{title}</h3>", "<ul>"]
    for task_id, row_project_id, task_title, owner_name, planned_due, status, priority in rows:
        parts.append(
            "<li>"
            f"<b>{h(task_id)}</b> ({h(row_project_id)}) - {h(task_title)}<br>"
            f"Owner: {h(owner_name or 'Unassigned')} | Due: {h(planned_due)} | "
            f"Status: {h(status)} | Priority: {h(priority or '')}"
            "</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def format_change_log_response(rows, title):
    if not rows:
        return f"<h3>{h(title)}</h3>No matching change log entries found."

    parts = [f"<h3>{h(title)}</h3>", "<ul>"]
    for row in rows:
        (
            draft_id,
            project_id,
            task_id,
            change_type,
            field_changed,
            old_value,
            new_value,
            reviewed_by,
            approved_at,
            comments,
        ) = row
        value_text = ""
        if old_value or new_value:
            value_text = f"<br>Value: {h(old_value)} -> {h(new_value)}"
        parts.append(
            "<li>"
            f"<b>{h(change_type)}</b> {h(field_changed)} for {h(task_id or 'No task')} "
            f"({h(project_id)}, draft {h(draft_id)})<br>"
            f"Reviewed by: {h(reviewed_by or '')} | Time: {h(approved_at)}"
            f"{value_text}"
            f"{'<br>Comments: ' + h(comments) if comments else ''}"
            "</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def build_direct_database_answer(cursor, question, reference_ids):
    normalized_question = normalize_text_for_match(question)
    project_ids = [item for item in reference_ids if item.startswith("PRJ")]
    task_ids = [item for item in reference_ids if item.startswith("T")]
    draft_ids = [item for item in reference_ids if re.fullmatch(r"(?:DR|D)\d+", item)]
    project_id = project_ids[0] if project_ids else None

    if draft_ids:
        cursor.execute(
            """
            SELECT draft_id, project_id, source_id, update_type, task_title,
                   owner_name, due_date, status, confidence, change_summary,
                   review_status, needs_clarification, clarification_question,
                   source_evidence, matched_task_id
            FROM draft_plan_updates
            WHERE draft_id = ANY(%s)
            ORDER BY draft_id DESC
            LIMIT 20;
            """,
            (draft_ids,),
        )
        rows = cursor.fetchall()
        logging.info(f"AuroraTeamsBot direct draft lookup for draft_ids={draft_ids}; rows_found={len(rows)}")
        return format_draft_lookup_response(rows, draft_ids)

    if task_ids and re.search(r"\b(change log|changes|history|audit)\b", normalized_question):
        cursor.execute(
            """
            SELECT draft_id, project_id, task_id, change_type, field_changed,
                   old_value, new_value, reviewed_by, approved_at, comments
            FROM change_log
            WHERE task_id = ANY(%s)
            ORDER BY approved_at DESC
            LIMIT 20;
            """,
            (task_ids,),
        )
        return format_change_log_response(cursor.fetchall(), f"Change Log for {', '.join(task_ids)}")

    if "blocked" in normalized_question and "task" in normalized_question:
        params = []
        project_filter = ""
        if project_id:
            project_filter = "AND project_id = %s"
            params.append(project_id)
        cursor.execute(
            f"""
            SELECT task_id, project_id, task_title, owner_name, planned_due, status, priority
            FROM tasks_master
            WHERE lower(status) = 'blocked'
            {project_filter}
            ORDER BY planned_due ASC, task_id ASC
            LIMIT 20;
            """,
            tuple(params),
        )
        return format_blocked_tasks_response(cursor.fetchall(), project_id)

    if (
        ("pending" in normalized_question and re.search(r"\b(approval|approvals|draft|drafts|review|reviews)\b", normalized_question))
        or "approval queue" in normalized_question
    ):
        params = []
        project_filter = ""
        if project_id:
            project_filter = "AND project_id = %s"
            params.append(project_id)
        cursor.execute(
            f"""
            SELECT draft_id, project_id, update_type, task_title, change_summary,
                   confidence, review_status, clarification_question, matched_task_id, source_id
            FROM draft_plan_updates
            WHERE review_status IN ('PENDING_REVIEW', 'NEEDS_CLARIFICATION')
            {project_filter}
            ORDER BY draft_id DESC
            LIMIT 20;
            """,
            tuple(params),
        )
        return format_pending_approvals_response(cursor.fetchall(), project_id)

    if re.search(r"\b(what changed|changed this week|changes this week|recent changes|changed recently)\b", normalized_question):
        params = []
        project_filter = ""
        if project_id:
            project_filter = "AND project_id = %s"
            params.append(project_id)
        cursor.execute(
            f"""
            SELECT draft_id, project_id, task_id, change_type, field_changed,
                   old_value, new_value, reviewed_by, approved_at, comments
            FROM change_log
            WHERE approved_at >= NOW() - INTERVAL '7 days'
            {project_filter}
            ORDER BY approved_at DESC
            LIMIT 20;
            """,
            tuple(params),
        )
        title = f"Changes in the Last 7 Days for {project_id}" if project_id else "Changes in the Last 7 Days"
        return format_change_log_response(cursor.fetchall(), title)

    return None


def empty_to_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def normalize_status(value):
    value = empty_to_none(value)
    if not value:
        return None

    normalized = str(value).strip().lower().replace("_", " ")
    status_map = {
        "not started": "Not started",
        "not start": "Not started",
        "in progress": "In progress",
        "started": "In progress",
        "blocked": "Blocked",
        "at risk": "At risk",
        "atrisk": "At risk",
        "done": "Done",
        "completed": "Done",
        "complete": "Done",
        "proposed": "Proposed",
        "ready for review": "Ready for review",
        "review": "Ready for review",
    }
    return status_map.get(normalized, str(value).strip())


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def clean_model_json(raw_text):
    text = raw_text.strip()
    if "```json" in text:
        return text.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        return text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


def normalize_text_for_match(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()


def evidence_appears_in_source(source_evidence, source_text):
    evidence = normalize_text_for_match(source_evidence)
    source = normalize_text_for_match(source_text)
    return bool(evidence and evidence in source)


def is_reply_only_line(line):
    text = normalize_text_for_match(line)
    if not text:
        return True
    if text in {
        "team,",
        "hi team,",
        "hello all,",
        "thanks,",
        "regards,",
        "also, highlight any blockers.",
        "highlight any blockers.",
        "flag any blockers.",
        "let us know blockers.",
        "share blockers.",
    }:
        return True
    if re.match(r"^can someone confirm\b", text):
        return True
    if re.fullmatch(r"[a-z]+ [a-z]+", text):
        return True
    return False


def should_skip_foundry_for_reply_only(text_content):
    lines = [line.strip(" -\t\r") for line in text_content.splitlines() if line.strip(" -\t\r")]
    if not lines:
        return False
    return all(is_reply_only_line(line) for line in lines)


def is_reply_only_update(update):
    evidence = normalize_text_for_match(update.get("source_evidence", ""))
    update_type = normalize_text_for_match(update.get("update_type", ""))

    if evidence.startswith("can someone confirm"):
        return True
    if update_type == "clarification needed":
        has_actionable_field = any(
            empty_to_none(update.get(field))
            for field in ("task_title", "owner_name", "due_date", "old_due_date", "status")
        )
        if not has_actionable_field:
            return True
    return False


def normalize_task_key(task_title):
    title = normalize_text_for_match(task_title)
    title = re.sub(r"^the\s+", "", title)
    return title


def update_conflict_key_and_value(update):
    update_type = normalize_text_for_match(update.get("update_type", ""))
    task_key = normalize_task_key(update.get("task_title", ""))
    if not task_key:
        return None, None

    if update_type == "new task":
        return ("new_task_due", task_key), empty_to_none(update.get("due_date"))
    if update_type == "update owner":
        return ("owner_name", task_key), normalize_text_for_match(update.get("owner_name", ""))
    if update_type in {"shift date", "update task"} and empty_to_none(update.get("due_date")):
        return ("due_date", task_key), empty_to_none(update.get("due_date"))
    if update_type == "update status" and empty_to_none(update.get("status")):
        return ("status", task_key), normalize_status(update.get("status"))
    return None, None


def dedupe_and_flag_source_conflicts(updates):
    seen = set()
    deduped = []
    grouped_values = {}

    for update in updates:
        identity = (
            normalize_text_for_match(update.get("update_type", "")),
            normalize_task_key(update.get("task_title", "")),
            normalize_text_for_match(update.get("owner_name", "")),
            empty_to_none(update.get("old_due_date")),
            empty_to_none(update.get("due_date")),
            normalize_status(update.get("status")),
            normalize_text_for_match(update.get("source_evidence", "")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(update)

        key, value = update_conflict_key_and_value(update)
        if key and value:
            grouped_values.setdefault(key, set()).add(str(value))

    conflicting_keys = {key for key, values in grouped_values.items() if len(values) > 1}
    for update in deduped:
        key, _ = update_conflict_key_and_value(update)
        if key in conflicting_keys:
            update["confidence"] = "Low"
            update["requires_clarification"] = True
            field_name, task_key = key
            update["notes"] = (
                f"Multiple different {field_name} values were found for '{task_key}' in the same source. "
                f"{update.get('notes') or ''}"
            ).strip()
            update["clarification_question"] = (
                f"Which {field_name} should be used for '{task_key}'?"
            )

    return deduped


def normalize_source_payload(req_body):
    source_type = req_body.get("source_type")
    if not source_type:
        if req_body.get("email_id") or req_body.get("body"):
            source_type = "email"
        elif req_body.get("meeting_id") or req_body.get("notes_text"):
            source_type = "meeting_note"
        else:
            source_type = "generic"

    text_content = (
        req_body.get("text_content")
        or req_body.get("body")
        or req_body.get("notes_text")
        or ""
    )

    source_id = (
        req_body.get("source_id")
        or req_body.get("email_id")
        or req_body.get("meeting_id")
        or "Unknown Source"
    )

    source_datetime = (
        req_body.get("sent_datetime")
        or req_body.get("meeting_datetime")
        or ""
    )

    metadata = {
        "source_type": source_type,
        "source_id": source_id,
        "source_datetime": source_datetime,
        "subject": req_body.get("subject", ""),
        "sender": req_body.get("sender", req_body.get("from", "")),
        "recipients": req_body.get("recipients", req_body.get("to", "")),
        "title": req_body.get("title", ""),
        "attendees": req_body.get("attendees", ""),
    }

    return req_body.get("project_id", "Unknown Project"), source_id, text_content, metadata


def decode_uploaded_file_content(req_body):
    if req_body.get("file_content_text") is not None:
        return req_body.get("file_content_text")
    if req_body.get("file_content_base64") is not None:
        return base64.b64decode(req_body.get("file_content_base64")).decode("utf-8-sig")
    if req_body.get("file_content") is not None:
        content = req_body.get("file_content")
        if isinstance(content, str):
            return content
    raise ValueError("Missing file_content_text or file_content_base64.")


def parse_uploaded_project_records(file_name, file_text, default_project_id=None):
    file_name_lower = (file_name or "").lower()
    records = []

    if file_name_lower.endswith(".jsonl"):
        for line_number, line in enumerate(file_text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row.setdefault("source_type", "meeting_note")
            if default_project_id and not row.get("project_id"):
                row["project_id"] = default_project_id
            records.append(row)
        return records

    if file_name_lower.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(file_text))
        for row in reader:
            row = {key: (value or "") for key, value in row.items() if key is not None}
            if not any(value.strip() for value in row.values() if isinstance(value, str)):
                continue
            if row.get("email_id") or row.get("body"):
                row["source_type"] = "email"
            elif row.get("meeting_id") or row.get("notes_text"):
                row["source_type"] = "meeting_note"
            else:
                row["source_type"] = "generic"
            if default_project_id and not row.get("project_id"):
                row["project_id"] = default_project_id
            records.append(row)
        return records

    raise ValueError("Unsupported file type. Upload emails.csv or meeting_notes.jsonl.")


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self):
        return self.payload


def build_task_context(rows):
    if not rows:
        return "No existing tasks found for this project."
    return "\n".join(
        [
            f"- {task_title} (ID: {task_id}, Owner: {owner_name}, Due: {planned_due}, Status: {status})"
            for task_id, task_title, owner_name, planned_due, status in rows
        ]
    )


def build_people_context(rows):
    if not rows:
        return "No people found."
    return "\n".join(
        [
            f"- {display_name} ({person_id}), email: {email}, role: {role}, discipline: {discipline}, region: {region}"
            for person_id, display_name, role, discipline, region, email in rows
        ]
    )


def make_update(
    update_type,
    task_title="",
    owner_name="",
    old_due_date="",
    due_date="",
    status="",
    dependency="",
    notes="",
    confidence="High",
    requires_clarification=False,
    clarification_question="",
    source_evidence="",
):
    return {
        "update_type": update_type,
        "task_title": task_title.strip(),
        "owner_name": owner_name.strip(),
        "old_due_date": old_due_date,
        "due_date": due_date,
        "status": status,
        "dependency": dependency,
        "notes": notes,
        "confidence": confidence,
        "requires_clarification": requires_clarification,
        "clarification_question": clarification_question,
        "source_evidence": source_evidence.strip(),
    }


def extract_updates_with_rules(text_content):
    updates = []
    lines = [line.strip(" -\t\r") for line in text_content.splitlines() if line.strip(" -\t\r")]
    compact_text = "\n".join(lines)

    generic_blocker_lines = {
        "also, highlight any blockers.",
        "highlight any blockers.",
        "flag any blockers.",
        "let us know blockers.",
        "share blockers.",
    }

    for line in lines:
        lowered = line.lower()
        if lowered in generic_blocker_lines:
            continue
        if lowered.startswith(("meeting summary", "key updates:", "team,", "hi team,", "hello all,", "thanks,", "regards,")):
            continue
        if re.fullmatch(r"-?[A-Za-z ]+", line) and len(line.split()) <= 3:
            continue

        match = re.search(r"^Create new task:\s*(.+?)\s*\(due\s+(\d{4}-\d{2}-\d{2})\)$", line, re.I)
        if match:
            updates.append(
                make_update(
                    "New Task",
                    task_title=match.group(1),
                    due_date=match.group(2),
                    status="Proposed",
                    notes="New task proposed in meeting note.",
                    source_evidence=line,
                )
            )
            continue

        match = re.search(r"^Mark\s+(.+?)\s+as\s+blocked\b(.*)$", line, re.I)
        if match:
            reason = match.group(2).strip()
            updates.append(
                make_update(
                    "Update Status",
                    task_title=match.group(1),
                    status="Blocked",
                    notes=reason,
                    source_evidence=line,
                )
            )
            continue

        match = re.search(r"^Shift\s+(.+?)\s+from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})$", line, re.I)
        if match:
            old_due_date = match.group(2)
            due_date = match.group(3)
            if old_due_date == due_date:
                continue
            updates.append(
                make_update(
                    "Shift Date",
                    task_title=match.group(1),
                    old_due_date=old_due_date,
                    due_date=due_date,
                    source_evidence=line,
                )
            )
            continue

        match = re.search(r"^Confirm owner for\s+(.+?)\s+\(tentative:\s*(.+?)\)$", line, re.I)
        if match:
            updates.append(
                make_update(
                    "Update Owner",
                    task_title=match.group(1),
                    owner_name=match.group(2),
                    notes="Owner is tentative and needs human confirmation.",
                    confidence="Medium",
                    requires_clarification=True,
                    clarification_question=f"Please confirm whether {match.group(2)} owns {match.group(1)}.",
                    source_evidence=line,
                )
            )
            continue

        match = re.search(r"^([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)\s+to\s+update\s+(.+?)\s+by\s+(\d{4}-\d{2}-\d{2})$", line)
        if match:
            updates.append(
                make_update(
                    "Update Task",
                    task_title=match.group(2),
                    owner_name=match.group(1),
                    due_date=match.group(3),
                    source_evidence=line,
                )
            )
            continue

        match = re.search(r"^Can someone confirm who owns\s+(?:the\s+)?(.+?)\?*$", line, re.I)
        if match:
            # Reply-only email question. Do not add it to the plan update draft queue.
            continue

        if re.search(r"^Can someone confirm whether the due date is firm\?*$", line, re.I):
            # Reply-only email question unless a specific task/date is also provided elsewhere.
            continue

        if re.search(r"^Can someone confirm if we should mark this as high priority\?*$", line, re.I):
            # Reply-only email question unless a specific task is also provided elsewhere.
            continue

        if re.search(r"^Quick update:\s*task is now blocked\b", line, re.I):
            dependency_match = re.search(r"\b(pending .+)$", line, re.I)
            dependency = dependency_match.group(1).strip() if dependency_match else ""
            updates.append(
                make_update(
                    "Update Status",
                    status="Blocked",
                    dependency=dependency,
                    notes="Blocked status is mentioned but the task is not identified.",
                    confidence="Low",
                    requires_clarification=True,
                    clarification_question="Which specific task is now blocked?",
                    source_evidence=line,
                )
            )
            continue

        if re.search(r"^Quick update:\s*we are ready for review\.", line, re.I):
            updates.append(
                make_update(
                    "Update Status",
                    status="Ready for review",
                    notes="Ready-for-review status is mentioned but the task is not identified.",
                    confidence="Low",
                    requires_clarification=True,
                    clarification_question="Which specific task is ready for review?",
                    source_evidence=line,
                )
            )
            continue

        if re.search(r"^Quick update:\s*dates shifted due to dependency\.", line, re.I):
            updates.append(
                make_update(
                    "Shift Date",
                    dependency="dependency mentioned",
                    notes="Date shift is mentioned but the task, old date, and new date are not identified.",
                    confidence="Low",
                    requires_clarification=True,
                    clarification_question="Which task shifted, and what are the old and new dates?",
                    source_evidence=line,
                )
            )
            continue

    add_task = re.search(
        r"We need to add a task for\s+(.+?)\s+and target\s+(\d{4}-\d{2}-\d{2})\.",
        compact_text,
        re.I,
    )
    owner_suggestion = re.search(r"Owner suggestion:\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)\.", compact_text)
    if add_task:
        evidence = add_task.group(0)
        owner_name = ""
        confidence = "High"
        requires_clarification = False
        clarification_question = ""
        notes = "New task requested in email."
        if owner_suggestion:
            owner_name = owner_suggestion.group(1)
            evidence = f"{evidence}\n{owner_suggestion.group(0)}"
            confidence = "Medium"
            requires_clarification = True
            clarification_question = f"Please confirm whether {owner_name} owns {add_task.group(1)}."
            notes = "New task requested in email; owner is a suggestion."

        updates.append(
            make_update(
                "New Task",
                task_title=add_task.group(1),
                owner_name=owner_name,
                due_date=add_task.group(2),
                status="Proposed",
                notes=notes,
                confidence=confidence,
                requires_clarification=requires_clarification,
                clarification_question=clarification_question,
                source_evidence=evidence,
            )
        )

    email_update = re.search(
        r"please\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)\s+update\s+(?:the\s+)?(.+?)\s+by\s+(\d{4}-\d{2}-\d{2})\.",
        compact_text,
        re.I,
    )
    if email_update:
        updates.append(
            make_update(
                "Update Task",
                task_title=email_update.group(2),
                owner_name=email_update.group(1),
                due_date=email_update.group(3),
                source_evidence=email_update.group(0),
            )
        )

    return updates


def build_extraction_prompt(task_context, people_context, metadata, text_content):
    return f"""
You are a project planning extraction assistant.

Extract project plan updates from the source text. The source may be an email or meeting note.

Return valid JSON only. Do not use markdown.

Return this exact structure:
{{
  "updates": [
    {{
      "update_type": "New Task | Update Task | Update Owner | Update Status | Shift Date | Clarification Needed",
      "task_title": "string or empty",
      "owner_name": "string or empty",
      "old_due_date": "YYYY-MM-DD or empty",
      "due_date": "YYYY-MM-DD or empty",
      "status": "Not started | In progress | Blocked | At risk | Ready for review | Done | Proposed | empty",
      "dependency": "string or empty",
      "notes": "brief reason or ambiguity note",
      "confidence": "High | Medium | Low",
      "requires_clarification": true/false,
      "clarification_question": "string or empty",
      "source_evidence": "exact quote from source text"
    }}
  ]
}}

Official existing tasks:
{task_context}

People directory:
{people_context}

Source metadata:
Source type: {metadata["source_type"]}
Source ID: {metadata["source_id"]}
Datetime: {metadata["source_datetime"]}
Subject/title: {metadata["subject"] or metadata["title"]}
Sender: {metadata["sender"]}
Recipients: {metadata["recipients"]}
Attendees: {metadata["attendees"]}

Extraction rules:
1. Extract only updates explicitly supported by the source text.
2. Every update must include exact source_evidence quoted from Source text only.
3. Do not invent owners, dates, statuses, dependencies, or task titles.
4. For meeting notes, treat each bullet as a possible separate update.
5. For emails, use subject, sender, recipients, and signature only as supporting metadata.
6. "Create new task", "add a task", or "need to add a task" means New Task.
7. "Shift X from DATE to DATE" means Shift Date. Put the first date in old_due_date and the second date in due_date.
8. "Mark X as blocked" means Update Status with status "Blocked".
9. "Confirm owner for X", "tentative owner", or "Owner suggestion" means Update Owner, confidence Medium, requires_clarification true.
10. "NAME to update TASK by DATE" means Update Task with owner_name NAME and due_date DATE.
11. If the text says "task is blocked" but does not identify which task, return Update Status with status "Blocked", confidence Low, requires_clarification true, and ask which task is blocked.
12. If a date is vague, leave due_date empty and set requires_clarification true.
13. The email subject is context only. Do not extract a blocker/status update from the subject alone.
14. Generic requests such as "highlight any blockers", "flag any blockers", "let us know blockers", or "share blockers" are follow-up instructions, not plan status changes. Do not extract them unless a specific named task is explicitly described as blocked or at risk.
15. If the source only asks a question, such as "Can someone confirm who owns X?", "Can someone confirm whether the due date is firm?", or "Can someone confirm if this should be high priority?", do not create a plan update. These are reply-only items.
16. "Quick update: we are ready for review" means Update Status with status "Ready for review", confidence Low, requires_clarification true, and ask which task is ready for review.
17. "Quick update: dates shifted due to dependency" means Shift Date with dependency "dependency mentioned", confidence Low, requires_clarification true, and ask which task shifted plus old/new dates.
18. If no updates are found, return {{"updates": []}}.

Source text:
{text_content}
""".strip()


def match_task(update, all_tasks):
    import difflib

    stop_words = {"update", "task", "for", "the", "to", "a", "an", "shift", "change"}
    best_match = None
    highest_score = 0

    extracted_title = (empty_to_none(update.get("task_title")) or "").lower()
    owner_name = empty_to_none(update.get("owner_name"))
    extracted_owner = owner_name.lower() if owner_name else ""
    source_evidence = update.get("source_evidence", "") or ""
    evidence_dates = re.findall(r"\d{4}-\d{2}-\d{2}", source_evidence)
    old_due_date = empty_to_none(update.get("old_due_date"))
    if old_due_date:
        evidence_dates.append(str(old_due_date))

    ext_words = set(extracted_title.split()) - stop_words

    for task in all_tasks:
        db_id, db_owner, db_due, db_status, db_title, db_start = task
        db_title_lower = db_title.lower()
        db_owner_lower = db_owner.lower() if db_owner else ""

        score = 0
        if extracted_title:
            if db_title_lower == extracted_title:
                score += 100
            elif db_title_lower in extracted_title or extracted_title in db_title_lower:
                score += 80
            else:
                ratio = difflib.SequenceMatcher(None, db_title_lower, extracted_title).ratio()
                score += ratio * 50

                db_words = set(db_title_lower.split()) - stop_words
                shared = db_words.intersection(ext_words)
                if shared and db_words:
                    score += (len(shared) / len(db_words)) * 40

        if extracted_owner and db_owner_lower:
            if extracted_owner in db_owner_lower or db_owner_lower in extracted_owner:
                score += 30

        if str(db_due) in evidence_dates:
            score += 60
        elif str(db_start) in evidence_dates:
            score += 30

        if score > highest_score and score >= 65:
            highest_score = score
            best_match = task

    return best_match, highest_score


def generate_next_task_id(cur):
    cur.execute("SELECT task_id FROM tasks_master WHERE task_id LIKE 'T%' ORDER BY task_id DESC LIMIT 1;")
    row = cur.fetchone()
    if not row:
        return "T00001"

    match = re.search(r"(\d+)$", row[0])
    if not match:
        return f"T{int(datetime.utcnow().timestamp())}"
    next_number = int(match.group(1)) + 1
    return f"T{next_number:05d}"


def generate_next_draft_id(cur):
    cur.execute("SELECT draft_id FROM draft_plan_updates WHERE draft_id LIKE 'DR%' ORDER BY draft_id DESC LIMIT 1;")
    row = cur.fetchone()
    if not row:
        return "DR00001"

    match = re.search(r"(\d+)$", str(row[0]))
    if not match:
        return f"DR{int(datetime.utcnow().timestamp())}"
    next_number = int(match.group(1)) + 1
    return f"DR{next_number:05d}"


@app.route(route="AuroraTeamsBot", auth_level=func.AuthLevel.FUNCTION)
def AuroraTeamsBot(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Python HTTP trigger function processed a request from Power Automate/Teams.")

    try:
        req_body = req.get_json()
        user_question = req_body.get("question")
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    if not user_question:
        return func.HttpResponse("Please pass a 'question' in the request body.", status_code=400)

    try:
        db_context = "No recent project updates found."
        try:
            reference_ids = extract_reference_ids(user_question)
            logging.info(f"AuroraTeamsBot version={APP_VERSION}; reference_ids={reference_ids}")
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    direct_answer = build_direct_database_answer(cursor, user_question, reference_ids)
                    if direct_answer:
                        return func.HttpResponse(
                            json.dumps({"answer": direct_answer}),
                            mimetype="application/json",
                            status_code=200,
                        )

                    if reference_ids:
                        cursor.execute(
                            """
                            SELECT draft_id, project_id, source_id, update_type, task_title,
                                   owner_name, due_date, status, confidence, change_summary,
                                   review_status, needs_clarification, clarification_question,
                                   source_evidence, matched_task_id
                            FROM draft_plan_updates
                            WHERE draft_id = ANY(%s)
                               OR matched_task_id = ANY(%s)
                               OR project_id = ANY(%s)
                            ORDER BY draft_id DESC
                            LIMIT 20;
                            """,
                            (reference_ids, reference_ids, reference_ids),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT draft_id, project_id, source_id, update_type, task_title,
                                   owner_name, due_date, status, confidence, change_summary,
                                   review_status, needs_clarification, clarification_question,
                                   source_evidence, matched_task_id
                            FROM draft_plan_updates
                            ORDER BY draft_id DESC
                            LIMIT 20;
                            """
                        )
                    rows = cursor.fetchall()

            draft_ids = [item for item in reference_ids if re.fullmatch(r"(?:DR|D)\d+", item)]
            if draft_ids:
                logging.info(f"AuroraTeamsBot direct draft lookup for draft_ids={draft_ids}; rows_found={len(rows)}")
                return func.HttpResponse(
                    json.dumps({"answer": format_draft_lookup_response(rows, draft_ids)}),
                    mimetype="application/json",
                    status_code=200,
                )

            if rows:
                db_context = "Here are the latest project updates from the database:\n"
                for row in rows:
                    db_context += (
                        f"- Draft {row[0]} / Project {row[1]} / Source {row[2]}: {row[3]} "
                        f"for task '{row[4]}'. Owner: {row[5]}, Due: {row[6]}, Status: {row[7]}, "
                        f"Confidence: {row[8]}, Review status: {row[10]}, Matched task: {row[14]}. "
                        f"Change summary: {row[9]}. Clarification: {row[12]}. "
                        f"Evidence: {row[13]}\n"
                    )
        except Exception as db_e:
            logging.error(f"Database connection failed: {str(db_e)}")
            db_context = "I could not connect to the project database for this answer."

        search_context = build_search_context(search_source_evidence(user_question, top=5))

        system_instruction = (
            "You are AURORA AI. You are responding to a user in Microsoft Teams. "
            "Use only the database and source evidence context provided. "
            "If the context does not contain the requested draft, task, source, or project, say that it was not found; do not guess. "
            "IMPORTANT FORMATTING RULES FOR TEAMS:\n"
            "- Teams DOES NOT support Markdown. You MUST format your entire response in HTML.\n"
            "- Bold text: Use <b>bold</b> or <strong>bold</strong>\n"
            "- Italic text: Use <i>italic</i>\n"
            "- Lists: Use <ul><li>item</li></ul> for bulleted lists, and <ol><li>item</li></ol> for numbered lists.\n"
            "- Line breaks: Use <br> to create new lines or spacing. Do NOT use literal newline characters in the final answer.\n"
            "- Headers: Use <h3>Header</h3> or <h4>Header</h4>\n"
            "Format your response using only HTML tags.\n\n"
        )
        enhanced_prompt = (
            f"User Question: {user_question}\n\n"
            f"Project Data Context:\n{db_context}\n\n"
            f"Source Evidence Search Context:\n{search_context}"
        )
        final_prompt = system_instruction + enhanced_prompt

        response = get_openai_client().responses.create(
            input=[{"role": "user", "content": final_prompt}],
            extra_body={
                "agent_reference": {
                    "name": FOUNDRY_AGENT_NAME,
                    "version": FOUNDRY_AGENT_VERSION,
                    "type": "agent_reference",
                }
            },
        )

        return func.HttpResponse(
            json.dumps({"answer": response.output_text}),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error(f"Error calling Azure Agent: {str(e)}")
        return func.HttpResponse(f"An error occurred: {str(e)}", status_code=500)


@app.route(route="IngestProjectData", auth_level=func.AuthLevel.FUNCTION)
def IngestProjectData(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Processing new project document from Power Automate.")

    try:
        req_body = req.get_json()
        project_id, source_id, text_content, metadata = normalize_source_payload(req_body)
    except ValueError:
        return func.HttpResponse("Invalid JSON format.", status_code=400)

    if not text_content:
        return func.HttpResponse("Missing text content in request body.", status_code=400)

    search_indexed = index_source_document(project_id, source_id, text_content, metadata)

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT task_id, task_title, owner_name, planned_due, status
            FROM tasks_master
            WHERE project_id = %s;
            """,
            (project_id,),
        )
        official_tasks = cur.fetchall()
        task_context = build_task_context(official_tasks)

        cur.execute("SELECT person_id, display_name, role, discipline, region, email FROM people;")
        people = cur.fetchall()
        people_context = build_people_context(people)

    except Exception as e:
        logging.error(f"Error connecting to database or fetching context: {e}")
        return func.HttpResponse(f"Database error: {str(e)}", status_code=500)

    extraction_method = "rules"
    extracted_updates = extract_updates_with_rules(text_content)

    if not extracted_updates:
        if should_skip_foundry_for_reply_only(text_content):
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return func.HttpResponse(
                json.dumps(
                    {
                    "message": "No plan updates found. Source appears to require an email reply only.",
                    "extraction_method": "reply_only_filter",
                    "search_indexed": search_indexed,
                    "updates": [],
                }
                ),
                mimetype="application/json",
                status_code=200,
            )

        extraction_method = "foundry_agent"
        try:
            prompt = build_extraction_prompt(task_context, people_context, metadata, text_content)

            response = get_openai_client().responses.create(
                input=[{"role": "user", "content": prompt}],
                extra_body={
                    "agent_reference": {
                        "name": FOUNDRY_AGENT_NAME,
                        "version": FOUNDRY_AGENT_VERSION,
                        "type": "agent_reference",
                    }
                },
            )

            result_json = json.loads(clean_model_json(response.output_text))
            extracted_updates = result_json.get("updates", [])
        except Exception as e:
            logging.error(f"Error extracting updates via AI: {e}")
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return func.HttpResponse(f"Failed to extract data via AI: {str(e)}", status_code=500)

    extracted_updates = dedupe_and_flag_source_conflicts(extracted_updates)

    if not extracted_updates:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return func.HttpResponse(
            json.dumps({"message": "No updates found in text.", "search_indexed": search_indexed}),
            mimetype="application/json",
            status_code=200,
        )

    try:
        cur.execute(
            """
            SELECT task_id, owner_name, planned_due, status, task_title, planned_start
            FROM tasks_master
            WHERE project_id = %s;
            """,
            (project_id,),
        )
        all_tasks = cur.fetchall()

        inserted_count = 0
        skipped_count = 0
        skipped_updates = []
        inserted_drafts = []
        for update in extracted_updates:
            source_evidence = empty_to_none(update.get("source_evidence")) or ""
            if is_reply_only_update(update):
                skipped_count += 1
                skipped_updates.append(
                    {
                        "update_type": update.get("update_type"),
                        "task_title": update.get("task_title"),
                        "reason": "reply-only question or vague update; not a plan draft",
                    }
                )
                logging.info(
                    "Skipping reply-only extracted update. "
                    f"source_id={source_id}, update_type={update.get('update_type')}"
                )
                continue

            if not evidence_appears_in_source(source_evidence, text_content):
                skipped_count += 1
                skipped_updates.append(
                    {
                        "update_type": update.get("update_type"),
                        "task_title": update.get("task_title"),
                        "reason": "source_evidence was not found in the submitted source text",
                    }
                )
                logging.warning(
                    "Skipping extracted update because source evidence was not found in source text. "
                    f"source_id={source_id}, update_type={update.get('update_type')}"
                )
                continue

            update_type = (update.get("update_type") or "").strip()
            update_type_lower = update_type.lower()
            is_new_task = update_type_lower == "new task"
            is_clarification = update_type_lower == "clarification needed"

            owner_name = empty_to_none(update.get("owner_name"))
            due_date = empty_to_none(update.get("due_date"))
            status = normalize_status(update.get("status"))
            dependency = empty_to_none(update.get("dependency"))
            confidence = (update.get("confidence") or "High").strip()
            requires_clarification = parse_bool(update.get("requires_clarification"))

            match, highest_score = match_task(update, all_tasks)
            matched_task_id = match[0] if match else None

            if is_new_task and match is None and confidence.lower() != "low":
                review_status = "PENDING_REVIEW"
                needs_clarification = requires_clarification
                clarification_question = update.get("clarification_question") or None
            elif is_new_task and match is not None:
                review_status = "NEEDS_CLARIFICATION"
                needs_clarification = True
                clarification_question = (
                    f"Source says this is a new task, but it looks similar to existing task "
                    f"'{match[4]}' ({matched_task_id}). Create a new task or update the existing one?"
                )
            elif confidence.lower() == "low" or requires_clarification or is_clarification:
                review_status = "NEEDS_CLARIFICATION"
                needs_clarification = True
                clarification_question = update.get("clarification_question") or "Please verify this extraction."
            elif match is None or highest_score < 75:
                review_status = "NEEDS_CLARIFICATION"
                needs_clarification = True
                if match is None:
                    clarification_question = "Could not confidently match this to an existing task. Is this a new task?"
                else:
                    clarification_question = f"Match score was low ({highest_score:.0f}). Did you mean '{match[4]}' ({matched_task_id})?"
            else:
                review_status = "PENDING_REVIEW"
                needs_clarification = False
                clarification_question = None

            proposed_changes = []
            change_summary_parts = []
            summary_task = matched_task_id or (empty_to_none(update.get("task_title")) or "unmatched task")

            if dependency:
                proposed_changes.append({"field": "dependency", "new_value": dependency})
                change_summary_parts.append(f"Record dependency for {summary_task}: {dependency}")

            if is_new_task:
                if owner_name:
                    proposed_changes.append({"field": "owner_name", "new_value": owner_name})
                if due_date:
                    proposed_changes.append({"field": "due_date", "new_value": due_date})
                if status:
                    proposed_changes.append({"field": "status", "new_value": status})
                summary_bits = [f"Create new task: {update.get('task_title')}"]
                if due_date:
                    summary_bits.append(f"due {due_date}")
                if owner_name:
                    summary_bits.append(f"owner {owner_name}")
                change_summary = " | ".join(summary_bits)
            elif matched_task_id:
                db_owner, db_due, db_status = match[1], match[2], normalize_status(match[3])

                if owner_name and owner_name != db_owner:
                    proposed_changes.append({"field": "owner_name", "old_value": db_owner, "new_value": owner_name})
                    change_summary_parts.append(f"Assign owner for {matched_task_id} to {owner_name}")

                if due_date and str(due_date) != str(db_due):
                    proposed_changes.append({"field": "due_date", "old_value": str(db_due) if db_due else None, "new_value": due_date})
                    change_summary_parts.append(f"Update due date for {matched_task_id} to {due_date}")

                if status and status != db_status:
                    proposed_changes.append({"field": "status", "old_value": db_status, "new_value": status})
                    change_summary_parts.append(f"Mark {matched_task_id} as {status}")

                if change_summary_parts:
                    change_summary = "; ".join(change_summary_parts)
                elif needs_clarification:
                    change_summary = "Clarification needed"
                else:
                    change_summary = "No changes identified"
            elif needs_clarification:
                if status:
                    proposed_changes.append({"field": "status", "new_value": status, "needs_task": True})
                    change_summary_parts.append(f"Clarify which task should be marked {status}")
                if due_date:
                    proposed_changes.append({"field": "due_date", "new_value": due_date, "needs_task": True})
                    change_summary_parts.append(f"Clarify which task should move to {due_date}")
                if not change_summary_parts:
                    change_summary_parts.append("Clarification needed")
                change_summary = "; ".join(change_summary_parts)
            else:
                change_summary = "No confident task match"

            if not proposed_changes and not needs_clarification and not is_new_task:
                skipped_count += 1
                skipped_updates.append(
                    {
                        "update_type": update_type,
                        "task_title": update.get("task_title"),
                        "matched_task_id": matched_task_id or "",
                        "reason": "extracted update already matches the current official plan",
                        "source_evidence": source_evidence,
                    }
                )
                logging.info(
                    "Skipping extracted update because it does not change the current official plan. "
                    f"source_id={source_id}, update_type={update_type}, matched_task_id={matched_task_id}"
                )
                continue

            draft_id = generate_next_draft_id(cur)
            cur.execute(
                """
                INSERT INTO draft_plan_updates (
                    draft_id, project_id, update_type, task_title, owner_name, due_date, status,
                    notes, confidence, source_evidence, source_id, matched_task_id,
                    proposed_changes, change_summary, review_status,
                    needs_clarification, clarification_question
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    draft_id,
                    project_id,
                    update_type,
                    empty_to_none(update.get("task_title")) or "",
                    owner_name or "",
                    due_date,
                    status or "",
                    empty_to_none(update.get("notes")) or "",
                    confidence,
                    source_evidence,
                    source_id,
                    matched_task_id,
                    json.dumps(proposed_changes) if proposed_changes else None,
                    change_summary,
                    review_status,
                    needs_clarification,
                    clarification_question,
                ),
            )
            inserted_drafts.append(
                {
                    "draft_id": draft_id,
                    "project_id": project_id,
                    "source_id": source_id,
                    "update_type": update_type,
                    "task_title": empty_to_none(update.get("task_title")) or "",
                    "owner_name": owner_name or "",
                    "due_date": str(due_date) if due_date else "",
                    "status": status or "",
                    "confidence": confidence,
                    "change_summary": change_summary,
                    "review_status": review_status,
                    "needs_clarification": needs_clarification,
                    "clarification_question": clarification_question or "",
                    "source_evidence": source_evidence,
                    "matched_task_id": matched_task_id or "",
                }
            )
            inserted_count += 1

        conn.commit()
        logging.info(f"Successfully inserted {inserted_count} updates into PostgreSQL.")

        return func.HttpResponse(
            json.dumps(
                {
                    "message": f"Successfully extracted and inserted {inserted_count} updates.",
                    "extraction_method": extraction_method,
                    "search_indexed": search_indexed,
                    "updates": extracted_updates,
                    "drafts": inserted_drafts,
                    "skipped_count": skipped_count,
                    "skipped_updates": skipped_updates,
                }
            ),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        conn.rollback()
        logging.error(f"Error pushing to database: {e}")
        return func.HttpResponse(f"Database error: {str(e)}", status_code=500)
    finally:
        if "cur" in locals():
            cur.close()
        if "conn" in locals():
            conn.close()


@app.route(route="IngestUploadedFile", auth_level=func.AuthLevel.FUNCTION)
def IngestUploadedFile(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Processing uploaded project file from SharePoint.")

    try:
        req_body = req.get_json()
        file_name = req_body.get("file_name", "")
        default_project_id = empty_to_none(req_body.get("default_project_id"))
        file_text = decode_uploaded_file_content(req_body)
        records = parse_uploaded_project_records(file_name, file_text, default_project_id)
    except Exception as e:
        logging.error(f"Invalid uploaded file payload: {e}")
        return func.HttpResponse(f"Invalid uploaded file payload: {str(e)}", status_code=400)

    results = []
    all_drafts = []
    total_inserted = 0
    total_skipped = 0
    failed_records = []

    for index, record in enumerate(records, start=1):
        try:
            response = IngestProjectData(JsonRequest(record))
            status_code = getattr(response, "status_code", 200)
            body_text = response.get_body().decode("utf-8")
            try:
                body_json = json.loads(body_text)
            except Exception:
                body_json = {"message": body_text}

            result = {
                "index": index,
                "source_id": record.get("source_id") or record.get("email_id") or record.get("meeting_id") or f"record-{index}",
                "status_code": status_code,
                "response": body_json,
            }
            results.append(result)

            if 200 <= status_code < 300:
                total_inserted += len(body_json.get("drafts", []))
                total_skipped += int(body_json.get("skipped_count", 0) or 0)
                all_drafts.extend(body_json.get("drafts", []))
            else:
                failed_records.append(result)
        except Exception as e:
            failed = {
                "index": index,
                "source_id": record.get("source_id") or record.get("email_id") or record.get("meeting_id") or f"record-{index}",
                "error": str(e),
            }
            failed_records.append(failed)
            results.append(failed)

    return func.HttpResponse(
        json.dumps(
            {
                "message": f"Processed {len(records)} records from {file_name}. Inserted {total_inserted} draft updates.",
                "file_name": file_name,
                "records_processed": len(records),
                "drafts_inserted": total_inserted,
                "skipped_updates": total_skipped,
                "drafts": all_drafts,
                "failed_records": failed_records,
                "results": results,
            }
        ),
        mimetype="application/json",
        status_code=207 if failed_records else 200,
    )


@app.route(route="GetPendingDrafts", auth_level=func.AuthLevel.FUNCTION)
def GetPendingDrafts(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Fetching pending draft plan updates.")

    project_id = empty_to_none(req.params.get("project_id"))
    try:
        limit = int(req.params.get("limit", "25"))
    except ValueError:
        limit = 25
    limit = max(1, min(limit, 100))

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        params = ["PENDING_REVIEW", "NEEDS_CLARIFICATION"]
        project_filter = ""
        if project_id:
            project_filter = "AND project_id = %s"
            params.append(project_id)
        params.append(limit)

        cur.execute(
            f"""
            SELECT
                draft_id, project_id, source_id, update_type, task_title,
                owner_name, due_date, status, confidence, change_summary,
                review_status, needs_clarification, clarification_question,
                source_evidence, matched_task_id, proposed_changes
            FROM draft_plan_updates
            WHERE review_status IN (%s, %s)
            {project_filter}
            ORDER BY draft_id
            LIMIT %s;
            """,
            params,
        )
        rows = cur.fetchall()

        drafts = []
        for row in rows:
            (
                draft_id,
                row_project_id,
                source_id,
                update_type,
                task_title,
                owner_name,
                due_date,
                status,
                confidence,
                change_summary,
                review_status,
                needs_clarification,
                clarification_question,
                source_evidence,
                matched_task_id,
                proposed_changes,
            ) = row
            drafts.append(
                {
                    "draft_id": str(draft_id),
                    "project_id": row_project_id or "",
                    "source_id": source_id or "",
                    "update_type": update_type or "",
                    "task_title": task_title or "",
                    "owner_name": owner_name or "",
                    "due_date": str(due_date) if due_date else "",
                    "status": status or "",
                    "confidence": confidence or "",
                    "change_summary": change_summary or "",
                    "review_status": review_status or "",
                    "needs_clarification": bool(needs_clarification),
                    "clarification_question": clarification_question or "",
                    "source_evidence": source_evidence or "",
                    "matched_task_id": matched_task_id or "",
                    "proposed_changes": proposed_changes or [],
                }
            )

        return func.HttpResponse(
            json.dumps(
                {
                    "message": f"Found {len(drafts)} pending draft updates.",
                    "count": len(drafts),
                    "drafts": drafts,
                },
                default=str,
            ),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error(f"Error fetching pending drafts: {e}")
        return func.HttpResponse(f"Error fetching pending drafts: {str(e)}", status_code=500)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route(route="SearchEvidence", auth_level=func.AuthLevel.FUNCTION)
def SearchEvidence(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Searching indexed source evidence.")

    query = None
    project_id = None
    top = 5

    try:
        try:
            body = req.get_json()
        except Exception:
            body = {}

        query = empty_to_none(body.get("query")) if isinstance(body, dict) else None
        project_id = empty_to_none(body.get("project_id")) if isinstance(body, dict) else None
        top = int(body.get("top", 5)) if isinstance(body, dict) else 5
    except Exception:
        return func.HttpResponse("Invalid JSON payload.", status_code=400)

    if not query:
        query = empty_to_none(req.params.get("query"))
    if not project_id:
        project_id = empty_to_none(req.params.get("project_id"))
    if req.params.get("top"):
        try:
            top = int(req.params.get("top"))
        except Exception:
            top = 5

    if not query:
        return func.HttpResponse("Please pass a query.", status_code=400)

    results = search_source_evidence(query, project_id=project_id, top=max(1, min(top, 10)))
    return func.HttpResponse(
        json.dumps(
            {
                "query": query,
                "project_id": project_id,
                "count": len(results),
                "results": results,
            },
            default=str,
        ),
        mimetype="application/json",
        status_code=200,
    )


@app.route(route="BuildInfo", auth_level=func.AuthLevel.FUNCTION)
def BuildInfo(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(
            {
                "app_version": APP_VERSION,
                "foundry_agent": FOUNDRY_AGENT_NAME,
                "search_configured": azure_search_configured(),
                "search_endpoint": AZURE_SEARCH_ENDPOINT,
                "search_index_name": AZURE_SEARCH_INDEX_NAME,
            }
        ),
        mimetype="application/json",
        status_code=200,
    )


@app.route(route="ReviewDraft", auth_level=func.AuthLevel.FUNCTION)
def ReviewDraft(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Processing draft review request.")

    conn = None
    cur = None

    try:
        body = req.get_json()
        draft_id = body.get("draft_id")
        reviewed_by = body.get("reviewed_by", "System")
        approved = parse_bool(body.get("approved", False))
        corrected_task_id = empty_to_none(body.get("corrected_task_id"))
        corrected_owner_name = empty_to_none(body.get("corrected_owner_name"))
        corrected_due_date = empty_to_none(body.get("corrected_due_date"))
        corrected_status = normalize_status(body.get("corrected_status"))
        review_comments = empty_to_none(body.get("review_comments") or body.get("review_notes"))
        if not draft_id or not reviewed_by:
            raise ValueError("draft_id and reviewed_by are required")
    except Exception as e:
        logging.error(f"Invalid request payload: {e}")
        return func.HttpResponse("Invalid JSON payload.", status_code=400)

    try:
        conn = get_db_connection()
        conn.autocommit = False
        cur = conn.cursor()

        cur.execute(
            """
            SELECT project_id, matched_task_id, task_title, owner_name, due_date,
                   status, source_evidence, confidence, notes
                   , update_type
            FROM draft_plan_updates
            WHERE draft_id = %s;
            """,
            (draft_id,),
        )
        draft = cur.fetchone()
        if not draft:
            conn.rollback()
            return func.HttpResponse(f"Draft with id {draft_id} not found.", status_code=404)

        (
            project_id,
            matched_task_id,
            task_title,
            owner_name,
            due_date,
            status,
            source_evidence,
            confidence,
            notes,
            update_type,
        ) = draft

        if not approved:
            cur.execute(
                """
                INSERT INTO change_log (
                    draft_id, project_id, task_id, change_type, field_changed,
                    old_value, new_value, source_evidence, reviewed_by, comments
                )
                SELECT
                    draft_id, project_id, matched_task_id, 'REJECTED', 'No official plan change',
                    NULL, NULL, source_evidence, %s,
                    COALESCE(%s, NULLIF(notes, ''), 'Draft rejected by human reviewer; tasks_master was not updated.')
                FROM draft_plan_updates
                WHERE draft_id = %s;
                """,
                (reviewed_by, review_comments, draft_id),
            )
            cur.execute(
                """
                UPDATE draft_plan_updates
                SET review_status = 'REJECTED',
                    reviewed_by = %s,
                    reviewed_at = NOW()
                WHERE draft_id = %s;
                """,
                (reviewed_by, draft_id),
            )
            conn.commit()
            return func.HttpResponse(
                json.dumps({"message": f"Draft {draft_id} rejected successfully.", "changed_fields": []}),
                mimetype="application/json",
                status_code=200,
            )

        if corrected_task_id:
            matched_task_id = corrected_task_id

        owner_name = empty_to_none(owner_name)
        status = normalize_status(status)
        due_date = empty_to_none(due_date)
        task_title = empty_to_none(task_title)
        update_type_lower = (update_type or "").strip().lower()
        changed_fields = []

        if corrected_owner_name:
            owner_name = corrected_owner_name
        if corrected_due_date:
            due_date = corrected_due_date
        if corrected_status:
            status = corrected_status

        if update_type_lower == "clarification needed" and not matched_task_id:
            conn.rollback()
            return func.HttpResponse(
                "This draft is a clarification request, not an approvable plan update. Provide corrected_task_id or reject it.",
                status_code=400,
            )

        if update_type_lower in {"update status", "shift date", "update task"} and not matched_task_id:
            conn.rollback()
            return func.HttpResponse(
                "This draft needs a corrected_task_id before it can update the official plan.",
                status_code=400,
            )

        if update_type_lower == "shift date" and not due_date:
            conn.rollback()
            return func.HttpResponse(
                "This date-shift draft needs corrected_due_date before approval.",
                status_code=400,
            )

        if update_type_lower == "update status" and not status:
            conn.rollback()
            return func.HttpResponse(
                "This status-update draft needs a status or corrected_status before approval.",
                status_code=400,
            )

        if not matched_task_id:
            if not task_title:
                conn.rollback()
                return func.HttpResponse(
                    "Cannot approve a new task draft without a task_title.",
                    status_code=400,
                )

            task_id = generate_next_task_id(cur)
            task_status = status or "Not started"

            cur.execute(
                """
                SELECT person_id, discipline
                FROM people
                WHERE lower(display_name) = lower(%s)
                LIMIT 1;
                """,
                (owner_name or "",),
            )
            owner_row = cur.fetchone()
            owner_id = owner_row[0] if owner_row else None
            discipline = owner_row[1] if owner_row else None

            cur.execute(
                """
                INSERT INTO tasks_master (
                    task_id, project_id, task_title, discipline, owner_id, owner_name,
                    planned_start, planned_due, status, percent_complete, priority, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, 0, 'Medium', %s);
                """,
                (
                    task_id,
                    project_id,
                    task_title,
                    discipline,
                    owner_id,
                    owner_name,
                    due_date,
                    task_status,
                    notes,
                ),
            )
            matched_task_id = task_id
            changed_fields.append(("new_task", None, task_id))
        else:
            cur.execute(
                """
                SELECT task_id, owner_name, planned_due, status
                FROM tasks_master
                WHERE task_id = %s;
                """,
                (matched_task_id,),
            )
            task_row = cur.fetchone()
            if not task_row:
                conn.rollback()
                return func.HttpResponse(f"Matched task {matched_task_id} not found in tasks_master.", status_code=404)

            task_id, old_owner, old_due, old_status = task_row
            old_status = normalize_status(old_status)

            cur.execute(
                """
                UPDATE tasks_master
                SET
                    owner_name = COALESCE(NULLIF(%s, ''), owner_name),
                    planned_due = COALESCE(NULLIF(%s, ''), planned_due),
                    status = COALESCE(NULLIF(%s, ''), status)
                WHERE task_id = %s;
                """,
                (owner_name or "", str(due_date) if due_date else "", status or "", matched_task_id),
            )

            if owner_name and owner_name != old_owner:
                changed_fields.append(("owner_name", old_owner, owner_name))
            if due_date and str(due_date) != str(old_due):
                changed_fields.append(("due_date", old_due, due_date))
            if status and status != old_status:
                changed_fields.append(("status", old_status, status))

        change_type = "CREATE" if changed_fields and changed_fields[0][0] == "new_task" else "UPDATE"
        for field, old_val, new_val in changed_fields:
            cur.execute(
                """
                INSERT INTO change_log (
                    draft_id, project_id, task_id, change_type, field_changed,
                    old_value, new_value, source_evidence, reviewed_by, comments
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    draft_id,
                    project_id,
                    matched_task_id,
                    change_type,
                    field,
                    old_val,
                    new_val,
                    source_evidence,
                    reviewed_by,
                    review_comments,
                ),
            )

        cur.execute(
            """
            UPDATE draft_plan_updates
            SET review_status = 'APPROVED',
                reviewed_by = %s,
                reviewed_at = NOW(),
                matched_task_id = %s,
                owner_name = COALESCE(NULLIF(%s, ''), owner_name),
                due_date = COALESCE(NULLIF(%s, ''), due_date),
                status = COALESCE(NULLIF(%s, ''), status)
            WHERE draft_id = %s;
            """,
            (
                reviewed_by,
                matched_task_id,
                owner_name or "",
                str(due_date) if due_date else "",
                status or "",
                draft_id,
            ),
        )

        conn.commit()
        return func.HttpResponse(
            json.dumps(
                {
                    "message": f"Draft {draft_id} approved successfully.",
                    "matched_task_id": matched_task_id,
                    "changed_fields": [f"{f}: {old}->{new}" for f, old, new in changed_fields],
                }
            ),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error(f"Error during draft review processing: {e}")
        if conn:
            conn.rollback()
        return func.HttpResponse(f"Error processing review: {str(e)}", status_code=500)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
