from collections import defaultdict
from datetime import datetime
import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory, session
from werkzeug.utils import secure_filename

load_dotenv()

from rag_engine import (
    answer_question,
    build_vector_store,
    create_chunk_embeddings,
    load_pdf_from_path,
    load_uploaded_pdfs,
    split_documents,
)

app = Flask(__name__)
application = app  # WSGI alias
app.secret_key = os.getenv("FLASK_SECRET_KEY", "doc-ai-research-assistant-session-secret-2026")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

HISTORY_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history")
os.makedirs(HISTORY_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json"}


def is_allowed_file(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in ALLOWED_EXTENSIONS


# --- Multi-Tenant Session Workspace Architecture ---

class UserWorkspace:
    """Isolated document storage, vector index, and chat session per client device."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.documents = []
        self.chunks = []
        self.vector_store = None
        self.metadata = []
        self.conversation_history = []
        self.upload_dir = os.path.join(UPLOAD_FOLDER, session_id)
        os.makedirs(self.upload_dir, exist_ok=True)
        self.history_dir = os.path.join(HISTORY_FOLDER, session_id)
        os.makedirs(self.history_dir, exist_ok=True)
        self.sessions_file = os.path.join(self.history_dir, "sessions.json")
        self.init_from_disk()

    def init_from_disk(self):
        """Auto-load any existing documents in this user's private upload folder."""
        if not os.path.exists(self.upload_dir):
            return

        doc_files = [
            f for f in os.listdir(self.upload_dir)
            if is_allowed_file(f) and os.path.isfile(os.path.join(self.upload_dir, f))
        ]
        if not doc_files:
            return

        docs = []
        meta = []
        for fname in doc_files:
            fpath = os.path.join(self.upload_dir, fname)
            try:
                extracted = load_pdf_from_path(fpath, fname)
                for d in extracted:
                    d.metadata["source"] = fname
                    d.metadata["file_path"] = fpath
                    docs.append(d)
                meta.append({
                    "filename": fname,
                    "file_size": os.path.getsize(fpath),
                    "pages": len(extracted),
                })
            except Exception as err:
                app.logger.warning(f"Could not preload {fname} for {self.session_id}: {err}")

        if docs:
            try:
                chunks = split_documents(docs)
                if chunks:
                    self.vector_store = build_vector_store(chunks)
                    self.documents = docs
                    self.chunks = chunks
                    self.metadata = meta
            except Exception as err:
                app.logger.warning(f"Could not build vector store on init for {self.session_id}: {err}")

    def load_saved_sessions(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.sessions_file):
            return []
        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def save_saved_sessions(self, sessions_list: List[Dict[str, Any]]) -> None:
        try:
            with open(self.sessions_file, "w", encoding="utf-8") as f:
                json.dump(sessions_list, f, indent=2, ensure_ascii=False)
        except Exception as err:
            app.logger.error(f"Error saving chat sessions for {self.session_id}: {err}")

    def clear(self):
        self.documents = []
        self.chunks = []
        self.vector_store = None
        self.metadata = []
        self.conversation_history = []
        if os.path.exists(self.upload_dir):
            shutil.rmtree(self.upload_dir, ignore_errors=True)
            os.makedirs(self.upload_dir, exist_ok=True)
        if os.path.exists(self.sessions_file):
            try:
                os.remove(self.sessions_file)
            except Exception:
                pass


user_workspaces: Dict[str, UserWorkspace] = {}


def get_workspace() -> UserWorkspace:
    """Resolve the isolated workspace for the requesting browser / client."""
    # 1. Custom HTTP header (sent by frontend script)
    sid = request.headers.get("X-Session-ID") or request.headers.get("X-User-ID")
    if not sid:
        # 2. Query param (?session_id=...)
        sid = request.args.get("session_id")
    if not sid:
        # 3. Signed cookie
        sid = session.get("user_id")
    if not sid:
        # 4. Generate new
        sid = "usr_" + uuid.uuid4().hex[:12]
        session["user_id"] = sid

    # Sanitize session ID to alphanumeric/hyphen/underscore (safe for filesystem path)
    clean_sid = re.sub(r'[^a-zA-Z0-9_\-]', '', str(sid))[:64] or "default"
    if clean_sid not in user_workspaces:
        user_workspaces[clean_sid] = UserWorkspace(clean_sid)
    return user_workspaces[clean_sid]


# --- CORS & Security Headers ---

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, X-Session-ID, X-User-ID"
        return response


@app.after_request
def set_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, X-Session-ID, X-User-ID"
    return response


# --- Application Endpoints ---

@app.get("/")
def home():
    # Pre-seed session ID in cookie if not set
    if "user_id" not in session:
        session["user_id"] = "usr_" + uuid.uuid4().hex[:12]
    return render_template("index.html")


@app.post("/upload")
def upload_documents():
    ws = get_workspace()
    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"success": False, "error": "Please select at least one document file."}), 400

    valid_files = []
    for f in uploaded_files:
        filename = (f.filename or "").strip()
        if filename and is_allowed_file(filename):
            valid_files.append(f)

    if not valid_files:
        return jsonify({
            "success": False,
            "error": "Unsupported file format. Please upload PDF (.pdf), Word (.docx, .doc), Text (.txt), Markdown (.md), or CSV documents.",
        }), 400

    try:
        new_docs = []
        new_meta = []

        for f in valid_files:
            safe_name = secure_filename(f.filename)
            saved_path = os.path.join(ws.upload_dir, safe_name)
            f.seek(0)
            f.save(saved_path)

            extracted = load_pdf_from_path(saved_path, safe_name)
            if not extracted:
                continue

            for doc in extracted:
                doc.metadata["source"] = safe_name
                doc.metadata["file_path"] = saved_path
                new_docs.append(doc)

            new_meta.append({
                "filename": safe_name,
                "file_size": os.path.getsize(saved_path),
                "pages": len(extracted),
            })

        if not new_docs:
            return jsonify({
                "success": False,
                "error": "No readable text found in the uploaded document(s). Please verify file contents.",
            }), 400

        all_docs = ws.documents + new_docs
        chunks = split_documents(all_docs)

        if not chunks:
            return jsonify({"success": False, "error": "Could not create text chunks."}), 400

        vector_store = build_vector_store(chunks)

        ws.documents = all_docs
        ws.chunks = chunks
        ws.vector_store = vector_store
        ws.metadata = ws.metadata + new_meta

        page_counts = defaultdict(int)
        chunk_counts = defaultdict(int)
        for d in ws.documents:
            page_counts[d.metadata.get("source")] += 1
        for c in ws.chunks:
            chunk_counts[c.metadata.get("source")] += 1

        docs_summary = []
        seen = set()
        for m in ws.metadata:
            fname = m["filename"]
            if fname not in seen:
                seen.add(fname)
                docs_summary.append({
                    "filename": fname,
                    "file_size": m.get("file_size", 0),
                    "pages": page_counts.get(fname, 0),
                    "chunks": chunk_counts.get(fname, 0),
                })

        total_pages = sum(d["pages"] for d in docs_summary)
        total_chunks = sum(d["chunks"] for d in docs_summary)
        workspace_summary = {
            "total_documents": len(docs_summary),
            "total_pages": total_pages,
            "total_chunks": total_chunks,
        }

        return jsonify({
            "success": True,
            "message": f"Successfully indexed {len(new_meta)} document(s).",
            "documents": docs_summary,
            "workspace_summary": workspace_summary,
        })

    except Exception as error:
        app.logger.exception(f"Upload failed for session {ws.session_id}.")
        return jsonify({"success": False, "error": f"Failed to process document: {error}"}), 500


@app.get("/sources")
def get_sources():
    ws = get_workspace()
    page_counts = defaultdict(int)
    chunk_counts = defaultdict(int)
    for d in ws.documents:
        page_counts[d.metadata.get("source")] += 1
    for c in ws.chunks:
        chunk_counts[c.metadata.get("source")] += 1

    docs_summary = []
    seen = set()
    for m in ws.metadata:
        fname = m["filename"]
        if fname not in seen:
            seen.add(fname)
            docs_summary.append({
                "filename": fname,
                "file_size": m.get("file_size", 0),
                "pages": page_counts.get(fname, 0),
                "chunks": chunk_counts.get(fname, 0),
            })

    total_pages = sum(d["pages"] for d in docs_summary)
    total_chunks = sum(d["chunks"] for d in docs_summary)
    workspace_summary = {
        "total_documents": len(docs_summary),
        "total_pages": total_pages,
        "total_chunks": total_chunks,
    }

    return jsonify({
        "success": True,
        "documents": docs_summary,
        "workspace_summary": workspace_summary,
    })


@app.get("/sources/<filename>")
def inspect_source(filename):
    """Retrieve extracted page text for in-app reading."""
    ws = get_workspace()
    pages = {}
    for d in ws.documents:
        if d.metadata.get("source") == filename:
            p_num = d.metadata.get("page", 1)
            pages.setdefault(p_num, []).append(d.page_content)

    if not pages:
        return jsonify({"success": False, "error": "Document not found."}), 404

    page_list = [{"page": p, "text": "\n\n".join(texts)} for p, texts in sorted(pages.items())]
    return jsonify({"success": True, "filename": filename, "pages": page_list})


@app.get("/sources/<filename>/download")
def download_source(filename):
    ws = get_workspace()
    safe_name = secure_filename(filename)
    return send_from_directory(ws.upload_dir, safe_name, as_attachment=False)


@app.delete("/sources/<filename>")
def delete_source(filename):
    ws = get_workspace()
    safe_name = secure_filename(filename)
    ws.documents = [d for d in ws.documents if d.metadata.get("source") != safe_name]
    ws.metadata = [m for m in ws.metadata if m["filename"] != safe_name]

    file_path = os.path.join(ws.upload_dir, safe_name)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass

    if ws.documents:
        ws.chunks = split_documents(ws.documents)
        ws.vector_store = build_vector_store(ws.chunks)
    else:
        ws.chunks = []
        ws.vector_store = None

    return jsonify({"success": True, "message": f"Removed '{filename}'."})


@app.post("/ask")
def ask():
    ws = get_workspace()
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"success": False, "error": "Please enter a question."}), 400

    try:
        answer, sources, external_sources = answer_question(
            vector_store=ws.vector_store,
            question=question,
            conversation_history=ws.conversation_history,
            number_of_chunks=8,
        )

        ws.conversation_history.append({"role": "user", "content": question})
        ws.conversation_history.append({"role": "assistant", "content": answer})
        if len(ws.conversation_history) > 20:
            ws.conversation_history = ws.conversation_history[-20:]

        return jsonify({
            "success": True,
            "answer": answer,
            "sources": sources,
            "external_sources": external_sources,
        })

    except Exception as error:
        app.logger.exception(f"Question answering failed for session {ws.session_id}.")
        return jsonify({
            "success": False,
            "error": f"Answer generation failed: {error}",
        }), 500


# --- Chat History Endpoints ---

@app.get("/history")
def get_chat_history():
    """Retrieve list of saved conversation sessions for the active user."""
    ws = get_workspace()
    sessions = ws.load_saved_sessions()
    summary = [
        {
            "id": s["id"],
            "title": s.get("title", "Research Chat"),
            "message_count": len(s.get("messages", [])),
            "updated_at": s.get("updated_at", ""),
            "preview": s.get("messages", [{}])[0].get("content", "")[:80] if s.get("messages") else "",
        }
        for s in reversed(sessions)
    ]
    return jsonify({"success": True, "sessions": summary})


@app.get("/history/<session_id>")
def get_session_detail(session_id):
    """Retrieve full messages for a specific session."""
    ws = get_workspace()
    sessions = ws.load_saved_sessions()
    for s in sessions:
        if s["id"] == session_id:
            return jsonify({"success": True, "session": s})
    return jsonify({"success": False, "error": "Session not found."}), 404


@app.post("/history/save")
def save_chat_session():
    """Save or update a conversation session for the active user."""
    ws = get_workspace()
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"success": False, "error": "No messages to save."}), 400

    session_id = data.get("id") or str(uuid.uuid4())
    title = data.get("title")
    if not title:
        first_user_msg = next((m["content"] for m in messages if m.get("role") == "user"), "Research Chat")
        title = (first_user_msg[:45] + "...") if len(first_user_msg) > 45 else first_user_msg

    sessions = ws.load_saved_sessions()
    now_str = datetime.now().strftime("%b %d, %Y %I:%M %p")

    updated = False
    for i, s in enumerate(sessions):
        if s["id"] == session_id:
            sessions[i]["title"] = title
            sessions[i]["messages"] = messages
            sessions[i]["updated_at"] = now_str
            updated = True
            break

    if not updated:
        sessions.append({
            "id": session_id,
            "title": title,
            "messages": messages,
            "updated_at": now_str,
        })

    ws.save_saved_sessions(sessions)
    return jsonify({"success": True, "session_id": session_id, "title": title})


@app.delete("/history/<session_id>")
def delete_chat_session(session_id):
    """Delete a saved conversation session."""
    ws = get_workspace()
    sessions = ws.load_saved_sessions()
    filtered = [s for s in sessions if s["id"] != session_id]
    ws.save_saved_sessions(filtered)
    return jsonify({"success": True, "message": "Session deleted."})


@app.post("/clear")
def clear_all():
    ws = get_workspace()
    ws.clear()
    return jsonify({"success": True, "message": "Your workspace and conversation have been cleared."})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
