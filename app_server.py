from collections import defaultdict
from datetime import datetime
import json
import os
import shutil
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
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
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

HISTORY_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history")
os.makedirs(HISTORY_FOLDER, exist_ok=True)
SESSIONS_FILE = os.path.join(HISTORY_FOLDER, "sessions.json")


def load_saved_sessions():
    if not os.path.exists(SESSIONS_FILE):
        return []
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_saved_sessions(sessions):
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)
    except Exception as err:
        app.logger.error(f"Error saving chat sessions: {err}")


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json"}


def is_allowed_file(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in ALLOWED_EXTENSIONS


# In-memory document and RAG state
current_documents = []
current_chunks = []
current_vector_store = None
current_metadata = []
conversation_history = []


def init_workspace_from_disk():
    """Auto-load any existing documents in the uploads folder on server start."""
    global current_documents, current_chunks, current_vector_store, current_metadata
    if not os.path.exists(UPLOAD_FOLDER):
        return

    doc_files = [
        f for f in os.listdir(UPLOAD_FOLDER)
        if is_allowed_file(f) and os.path.isfile(os.path.join(UPLOAD_FOLDER, f))
    ]
    if not doc_files:
        return

    docs = []
    meta = []
    for fname in doc_files:
        fpath = os.path.join(UPLOAD_FOLDER, fname)
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
            app.logger.warning(f"Could not preload {fname}: {err}")

    if docs:
        try:
            chunks = split_documents(docs)
            if chunks:
                current_vector_store = build_vector_store(chunks)
                current_documents = docs
                current_chunks = chunks
                current_metadata = meta
                print(f"[INIT] Preloaded {len(meta)} document(s) with {len(chunks)} chunks into vector store.")
        except Exception as err:
            app.logger.warning(f"Could not build vector store on init: {err}")



@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        return response


@app.after_request
def set_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    return response


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/upload")
def upload_documents():
    global current_documents, current_chunks, current_vector_store, current_metadata, conversation_history

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
        # Save files to disk and extract text
        new_docs = []
        new_meta = []

        for f in valid_files:
            safe_name = secure_filename(f.filename)
            saved_path = os.path.join(UPLOAD_FOLDER, safe_name)
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

        # Combine with any existing documents
        all_docs = current_documents + new_docs
        chunks = split_documents(all_docs)

        if not chunks:
            return jsonify({"success": False, "error": "Could not create text chunks."}), 400

        # Build FAISS vector store
        vector_store = build_vector_store(chunks)

        current_documents = all_docs
        current_chunks = chunks
        current_vector_store = vector_store
        current_metadata = current_metadata + new_meta

        # Calculate pages and chunks per file
        page_counts = defaultdict(int)
        chunk_counts = defaultdict(int)
        for d in current_documents:
            page_counts[d.metadata.get("source")] += 1
        for c in current_chunks:
            chunk_counts[c.metadata.get("source")] += 1

        docs_summary = []
        seen = set()
        for m in current_metadata:
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
        app.logger.exception("Upload failed.")
        return jsonify({"success": False, "error": f"Failed to process document: {error}"}), 500


@app.get("/sources")
def get_sources():
    page_counts = defaultdict(int)
    chunk_counts = defaultdict(int)
    for d in current_documents:
        page_counts[d.metadata.get("source")] += 1
    for c in current_chunks:
        chunk_counts[c.metadata.get("source")] += 1

    docs_summary = []
    seen = set()
    for m in current_metadata:
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
    pages = {}
    for d in current_documents:
        if d.metadata.get("source") == filename:
            p_num = d.metadata.get("page", 1)
            pages.setdefault(p_num, []).append(d.page_content)

    if not pages:
        return jsonify({"success": False, "error": "Document not found."}), 404

    page_list = [{"page": p, "text": "\n\n".join(texts)} for p, texts in sorted(pages.items())]
    return jsonify({"success": True, "filename": filename, "pages": page_list})


@app.get("/sources/<filename>/download")
def download_source(filename):
    safe_name = secure_filename(filename)
    return send_from_directory(UPLOAD_FOLDER, safe_name, as_attachment=False)


@app.delete("/sources/<filename>")
def delete_source(filename):
    global current_documents, current_chunks, current_vector_store, current_metadata

    safe_name = secure_filename(filename)
    current_documents = [d for d in current_documents if d.metadata.get("source") != safe_name]
    current_metadata = [m for m in current_metadata if m["filename"] != safe_name]

    file_path = os.path.join(UPLOAD_FOLDER, safe_name)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass

    if current_documents:
        current_chunks = split_documents(current_documents)
        current_vector_store = build_vector_store(current_chunks)
    else:
        current_chunks = []
        current_vector_store = None

    return jsonify({"success": True, "message": f"Removed '{filename}'."})


@app.post("/ask")
def ask():
    global conversation_history

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"success": False, "error": "Please enter a question."}), 400

    if current_vector_store is None and not current_documents:
        # Check if question is an outside inquiry or general knowledge
        app.logger.info("No documents uploaded; proceeding with external web retrieval and general knowledge.")

    try:
        answer, sources, external_sources = answer_question(
            vector_store=current_vector_store,
            question=question,
            conversation_history=conversation_history,
            number_of_chunks=8,
        )

        conversation_history.append({"role": "user", "content": question})
        conversation_history.append({"role": "assistant", "content": answer})

        return jsonify({
            "success": True,
            "answer": answer,
            "sources": sources,
            "external_sources": external_sources,
        })

    except Exception as error:
        app.logger.exception("Question answering failed.")
        return jsonify({
            "success": False,
            "error": f"Answer generation failed: {error}",
        }), 500


# --- Chat History Endpoints ---

@app.get("/history")
def get_chat_history():
    """Retrieve list of saved conversation sessions."""
    sessions = load_saved_sessions()
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
    sessions = load_saved_sessions()
    for s in sessions:
        if s["id"] == session_id:
            return jsonify({"success": True, "session": s})
    return jsonify({"success": False, "error": "Session not found."}), 404


@app.post("/history/save")
def save_chat_session():
    """Save or update a conversation session."""
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"success": False, "error": "No messages to save."}), 400

    session_id = data.get("id") or str(uuid.uuid4())
    title = data.get("title")
    if not title:
        first_user_msg = next((m["content"] for m in messages if m.get("role") == "user"), "Research Chat")
        title = (first_user_msg[:45] + "...") if len(first_user_msg) > 45 else first_user_msg

    sessions = load_saved_sessions()
    now_str = datetime.now().strftime("%b %d, %Y %I:%M %p")

    # Update if existing, else prepend
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

    save_saved_sessions(sessions)
    return jsonify({"success": True, "session_id": session_id, "title": title})


@app.delete("/history/<session_id>")
def delete_chat_session(session_id):
    """Delete a saved conversation session."""
    sessions = load_saved_sessions()
    filtered = [s for s in sessions if s["id"] != session_id]
    save_saved_sessions(filtered)
    return jsonify({"success": True, "message": "Session deleted."})



@app.post("/clear")
def clear_all():
    global current_documents, current_chunks, current_vector_store, current_metadata, conversation_history

    current_documents = []
    current_chunks = []
    current_vector_store = None
    current_metadata = []
    conversation_history = []

    if os.path.exists(UPLOAD_FOLDER):
        shutil.rmtree(UPLOAD_FOLDER, ignore_errors=True)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    return jsonify({"success": True, "message": "All documents and conversation cleared."})


init_workspace_from_disk()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

