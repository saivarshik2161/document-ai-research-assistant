---
title: Document AI Research Assistant
emoji: 📑
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Document AI — Research Assistant

A production-ready AI Document Research Assistant built with Flask, LangChain, FAISS vector search, local HuggingFace embeddings, and NVIDIA Nemotron 3 Super reasoning model.

---

## 🏛️ System Architecture

```
+-------------------------------------------------------------------------------+
|                        DOCUMENT AI - RESEARCH ASSISTANT                       |
+-------------------------------------------------------------------------------+
|  UI INTERFACE (templates/index.html + static/style.css + static/script.js)    |
|                                                                               |
|  LEFT COLUMN: DOCUMENTS & SOURCES     |  CENTER COLUMN: AI RESEARCH CHAT      |
|  - Drag-and-drop PDF upload           |  - Direct grounded Q&A                |
|  - Real-time indexing progress        |  - Verified page citations [p. X]     |
|  - Page & chunk counts                |  - Citation Inspector modal           |
|  - In-app document page viewer        |  - Markdown formatting & code copy    |
|  - Document removal & clear workspace |  - Dark / Light mode toggle           |
+---------------------------------------+---------------------------------------+
|  FLASK APPLICATION (app.py)                                                   |
|  - POST /upload: Ingests PDFs, extracts text, chunks text, builds FAISS index |
|  - POST /ask: Retrieves relevant passages, constructs grounded prompt,        |
|               queries NVIDIA Nemotron LLM with retry, returns answer & sources|
|  - GET /sources: Lists indexed documents, page counts, chunk counts           |
|  - GET /sources/<filename>: In-app page text reader for full inspection       |
|  - DELETE /sources/<filename>: Removes document & re-indexes remaining        |
|  - POST /clear: Resets entire workspace and documents                         |
+-------------------------------------------------------------------------------+
|  RAG ENGINE (rag_engine.py)                                                   |
|  1. PDF Extraction: pypdf (PdfReader) extracts text page-by-page               |
|  2. Chunking: RecursiveCharacterTextSplitter (1000 chunk size, 150 overlap)   |
|  3. Embeddings: HuggingFace all-MiniLM-L6-v2 (CPU-optimized, normalized)      |
|  4. Vector Store: FAISS (Facebook AI Similarity Search) index                 |
|  5. LLM: NVIDIA Nemotron 3 Super 120B (ChatNVIDIA) with exponential retry     |
+-------------------------------------------------------------------------------+
```

---

## 📦 What We Used (Libraries & Explanations)

| Component | Technology | Why We Used It |
|-----------|------------|----------------|
| **Backend Framework** | `Flask` | Lightweight, fast Python web framework; direct REST APIs without overhead. |
| **PDF Extraction** | `pypdf` | Extracts selectable text from each page while preserving page numbers for citations. |
| **Chunking** | `langchain_text_splitters` | Breaks large documents into semantic chunks with overlap to maintain contextual continuity. |
| **Embeddings** | `sentence-transformers/all-MiniLM-L6-v2` | High-quality 384-dimensional dense embeddings run locally on CPU without external costs. |
| **Vector Store** | `FAISS` | Extremely fast local vector index that performs cosine similarity search to retrieve top relevant passages. |
| **LLM Model** | `NVIDIA Nemotron 3 Super 120B` | Advanced reasoning model running on NVIDIA NIM cloud; capable of complex technical synthesis. |
| **Resilience / Retry** | Exponential Backoff | Automatically retries transient NVIDIA 503 rate limits so answers never fail. |
| **Frontend** | Vanilla HTML5, CSS3, JS | Zero external framework dependencies; fast, lightweight, and completely responsive. |

---

## 🚀 Quickstart

### 1. Configure Environment (`.env`)
```env
NVIDIA_API_KEY=nvapi-your-key-here
NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b
PORT=5000
```

### 2. Run the Application
```powershell
.\venv\Scripts\python.exe app.py
```

### 3. Open in Browser
Open [http://localhost:5000](http://localhost:5000) in any web browser.
- Drag and drop your PDF into the left panel.
- Ask any question in the chat bar (or click a prompt suggestion).
- Click any source citation badge to view the exact page and evidence passage!
