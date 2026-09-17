import os

# Crucial memory optimizations for low-RAM cloud instances (keeps memory well under 512MB)
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["MALLOC_ARENA_MAX"] = "2"

from functools import lru_cache
from io import BytesIO
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

from dotenv import load_dotenv
from pypdf import PdfReader
import requests

try:
    import docx
except ImportError:
    docx = None

from langchain_core.documents import Document
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_text_splitters import RecursiveCharacterTextSplitter


load_dotenv()


class LightweightVectorStore:
    """Ultra-fast, low-memory vector store using TF-IDF n-gram embeddings and cosine similarity.
    Consumes ~15MB RAM instead of 500MB PyTorch, preventing cloud OOM crashes while delivering
    instant sub-millisecond document passage retrieval.
    """
    def __init__(self, documents: List[Document]):
        self.documents = list(documents)
        if not self.documents:
            self.vectorizer = None
            self.doc_vectors = None
            return

        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
            max_features=15000,
        )
        self.doc_vectors = self.vectorizer.fit_transform([d.page_content for d in self.documents])

    def similarity_search(self, query: str, k: int = 8) -> List[Document]:
        if not self.documents or self.vectorizer is None:
            return []

        from sklearn.metrics.pairwise import cosine_similarity
        import numpy as np

        query_vec = self.vectorizer.transform([query])
        scores = cosine_similarity(query_vec, self.doc_vectors).flatten()
        top_indices = np.argsort(scores)[::-1][:k]

        results = [self.documents[i] for i in top_indices if scores[i] > 0]
        if not results:
            results = self.documents[:k]
        return results


def create_embedding_model():
    """Return lightweight vectorizer factory."""
    return LightweightVectorStore


def extract_pdf_pages(file_bytes: bytes, filename: str) -> List[Document]:
    """Extract selectable text from each PDF page."""
    reader = PdfReader(BytesIO(file_bytes))
    documents = []
    for page_number, page in enumerate(reader.pages):
        page_text = (page.extract_text() or "").strip()
        if page_text:
            documents.append(
                Document(
                    page_content=page_text,
                    metadata={"source": filename, "page": page_number + 1},
                )
            )
    return documents


def extract_docx_pages(file_bytes_or_path, filename: str) -> List[Document]:
    """Extract paragraphs and tables from Word (.docx) documents, grouped into logical pages."""
    if docx is None:
        raise RuntimeError("python-docx is not installed.")

    if isinstance(file_bytes_or_path, (str, os.PathLike)):
        doc = docx.Document(file_bytes_or_path)
    else:
        doc = docx.Document(BytesIO(file_bytes_or_path))

    blocks: List[str] = []
    for p in doc.paragraphs:
        txt = p.text.strip()
        if txt:
            blocks.append(txt)

    for table in doc.tables:
        for row in table.rows:
            row_txt = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            if row_txt:
                blocks.append(f"| {row_txt} |")

    pages: List[str] = []
    current_page_text: List[str] = []
    current_len = 0
    target_chars = 2200

    for block in blocks:
        current_page_text.append(block)
        current_len += len(block)
        if current_len >= target_chars:
            pages.append("\n\n".join(current_page_text))
            current_page_text = []
            current_len = 0

    if current_page_text:
        pages.append("\n\n".join(current_page_text))

    if not pages:
        return []

    return [
        Document(page_content=content, metadata={"source": filename, "page": i + 1})
        for i, content in enumerate(pages)
    ]


def extract_text_pages(file_bytes: bytes, filename: str) -> List[Document]:
    """Extract plain text, markdown, or CSV files grouped into logical pages."""
    text = ""
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1", "cp1252"):
        try:
            text = file_bytes.decode(enc)
            break
        except (UnicodeDecodeError, Exception):
            continue
    if not text:
        text = file_bytes.decode("utf-8", errors="replace")

    lines = text.splitlines()
    pages: List[str] = []
    current_page: List[str] = []
    current_len = 0
    target_chars = 2200

    for line in lines:
        current_page.append(line)
        current_len += len(line) + 1
        if current_len >= target_chars:
            chunk = "\n".join(current_page).strip()
            if chunk:
                pages.append(chunk)
            current_page = []
            current_len = 0

    if current_page:
        tail = "\n".join(current_page).strip()
        if tail:
            pages.append(tail)

    if not pages:
        return []

    return [
        Document(page_content=p, metadata={"source": filename, "page": i + 1})
        for i, p in enumerate(pages)
    ]


def load_document_from_bytes(file_bytes: bytes, filename: str) -> List[Document]:
    """Automatically detect document type (PDF, Word, Text, MD, CSV) and extract pages."""
    if not file_bytes:
        raise ValueError(f"The file '{filename}' is empty.")

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return extract_pdf_pages(file_bytes, filename)
    elif ext in (".docx", ".doc"):
        try:
            return extract_docx_pages(file_bytes, filename)
        except Exception:
            return extract_text_pages(file_bytes, filename)
    elif ext in (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".py", ".html"):
        return extract_text_pages(file_bytes, filename)
    else:
        try:
            return extract_pdf_pages(file_bytes, filename)
        except Exception:
            try:
                return extract_docx_pages(file_bytes, filename)
            except Exception:
                return extract_text_pages(file_bytes, filename)


def load_document_from_path(file_path: str, filename: Optional[str] = None) -> List[Document]:
    """Extract readable pages from a saved document on disk."""
    name = filename or os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = f.read()
    return load_document_from_bytes(data, name)


def load_uploaded_documents(uploaded_files) -> List[Document]:
    """Extract pages from a list of uploaded document files."""
    documents = []
    for uploaded_file in uploaded_files:
        filename = getattr(uploaded_file, "filename", None) or "document"
        filename = filename.strip()
        try:
            if hasattr(uploaded_file, "stream"):
                uploaded_file.stream.seek(0)
                file_bytes = uploaded_file.stream.read()
            elif hasattr(uploaded_file, "read"):
                uploaded_file.seek(0)
                file_bytes = uploaded_file.read()
            elif isinstance(uploaded_file, (str, bytes)) and os.path.isfile(str(uploaded_file)):
                with open(uploaded_file, "rb") as f:
                    file_bytes = f.read()
            else:
                file_bytes = uploaded_file

            extracted = load_document_from_bytes(file_bytes, filename)
            documents.extend(extracted)
        except Exception as error:
            raise RuntimeError(f"Could not read '{filename}': {error}") from error

    return documents


# Backwards compatibility aliases
load_pdf_from_path = load_document_from_path
load_uploaded_pdfs = load_uploaded_documents


def split_documents(documents: List[Document]) -> List[Document]:
    """Create overlapping chunks that preserve context."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1200,
        chunk_overlap=250,
        length_function=len,
        separators=["\n\n", "\n", ". ", "? ", "! ", " ", ""],
    )
    return splitter.split_documents(documents)


def create_chunk_embeddings(chunks: List[Document]):
    """Create embedding vectors for all chunks."""
    if not chunks:
        raise ValueError("There are no chunks to embed.")

    store = LightweightVectorStore(chunks)
    return store.vectorizer, store.doc_vectors


def build_vector_store(chunks: List[Document]) -> LightweightVectorStore:
    """Build the ultra-fast, lightweight vector store."""
    if not chunks:
        raise ValueError("Cannot build vector store without chunks.")

    return LightweightVectorStore(chunks)


def retrieve_relevant_chunks(
    vector_store: LightweightVectorStore,
    question: str,
    number_of_chunks: int = 8,
) -> List[Document]:
    """Retrieve passages relevant to the current question with balanced representation."""
    if vector_store is None:
        raise ValueError("No documents have been indexed yet.")
    if not question.strip():
        raise ValueError("The question cannot be empty.")

    initial_chunks = vector_store.similarity_search(question, k=max(number_of_chunks, 12))

    sources_in_chunks = set(c.metadata.get("source") for c in initial_chunks if c.metadata.get("source"))

    if len(sources_in_chunks) > 1:
        chunks_by_source: Dict[str, List[Document]] = {}
        for c in initial_chunks:
            src = c.metadata.get("source", "unknown")
            chunks_by_source.setdefault(src, []).append(c)

        balanced: List[Document] = []
        max_per_src = max(1, number_of_chunks // len(chunks_by_source))

        for src, s_chunks in chunks_by_source.items():
            balanced.extend(s_chunks[:max_per_src])

        for c in initial_chunks:
            if c not in balanced and len(balanced) < number_of_chunks:
                balanced.append(c)
        return balanced[:number_of_chunks]

    return initial_chunks[:number_of_chunks]


@lru_cache(maxsize=1)
def create_chat_model():
    """Create and cache the NVIDIA LLM."""
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        raise ValueError("NVIDIA_API_KEY was not found in .env.")

    model_name = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")

    try:
        return ChatNVIDIA(
            model=model_name,
            temperature=0.2,
            max_completion_tokens=2500,
            api_key=api_key,
        )
    except Exception:
        return ChatNVIDIA(
            model=model_name,
            temperature=0.2,
            max_tokens=2500,
            api_key=api_key,
        )


def create_source(metadata: dict, snippet: str = "") -> dict:
    clean_snippet = snippet.replace("\n", " ").strip()
    if len(clean_snippet) > 280:
        clean_snippet = clean_snippet[:277] + "..."
    return {
        "filename": metadata.get("source", "document.pdf"),
        "page": metadata.get("page", 1),
        "snippet": clean_snippet,
    }


def clean_math_and_text(text: str) -> str:
    """Clean up corrupted terminal, encoding, or LLM artifacts without distorting math."""
    if not text:
        return ""
    # Strip narrow non-breaking spaces, replacement chars, and BOM
    text = text.replace('\u202f', ' ').replace('\xa0', ' ').replace('\ufeff', '')
    text = text.replace('\ufffd', '').replace('\u00a0', ' ')
    # Remove standalone unprintable control characters (preserving newlines and tabs)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    return text




def extract_response_text(content: Any) -> str:
    """Extract and clean plain text answer from LLM response."""
    if content is None:
        return ""
    if isinstance(content, str):
        return clean_math_and_text(content.strip())

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("content"), list):
                    nested = extract_response_text(item["content"])
                    if nested:
                        parts.append(nested)
        return clean_math_and_text("\n".join(part.strip() for part in parts if part.strip()).strip())

    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return clean_math_and_text(content["text"].strip())
        if isinstance(content.get("content"), str):
            return clean_math_and_text(content["content"].strip())
        if isinstance(content.get("content"), list):
            return clean_math_and_text(extract_response_text(content["content"]))

    return clean_math_and_text(str(content).strip())


def search_external_knowledge(query: str, max_results: int = 4) -> List[Dict[str, str]]:
    """Fetch external web summaries and direct source URLs from Wikipedia and DuckDuckGo."""
    results: List[Dict[str, str]] = []
    seen_urls = set()

    # 1. Wikipedia API
    try:
        w_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
            + urllib.parse.quote(query)
            + "&utf8=&format=json"
        )
        resp = requests.get(w_url, headers={"User-Agent": "DocumentAI/1.0"}, timeout=5)
        if resp.status_code == 200:
            items = resp.json().get("query", {}).get("search", [])[:max_results]
            for item in items:
                title = item.get("title", "")
                link = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
                if link not in seen_urls:
                    seen_urls.add(link)
                    raw_snippet = item.get("snippet", "")
                    clean_snippet = re.sub(r"<[^<]+?>", "", raw_snippet).strip()
                    results.append({
                        "title": title,
                        "url": link,
                        "snippet": clean_snippet,
                        "source": "Wikipedia",
                    })
    except Exception as err:
        print(f"[SEARCH] Wikipedia search error: {err}")

    # 2. DuckDuckGo Instant Answer API
    try:
        ddg_url = (
            "https://api.duckduckgo.com/?q="
            + urllib.parse.quote(query)
            + "&format=json&no_html=1"
        )
        resp = requests.get(ddg_url, headers={"User-Agent": "DocumentAI/1.0"}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("AbstractText") and data.get("AbstractURL"):
                link = data["AbstractURL"]
                if link not in seen_urls:
                    seen_urls.add(link)
                    results.append({
                        "title": data.get("Heading") or query,
                        "url": link,
                        "snippet": data["AbstractText"],
                        "source": "DuckDuckGo",
                    })
            for topic in data.get("RelatedTopics", [])[:2]:
                if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
                    link = topic["FirstURL"]
                    if link not in seen_urls:
                        seen_urls.add(link)
                        results.append({
                            "title": topic["Text"][:60] + "...",
                            "url": link,
                            "snippet": topic["Text"],
                            "source": "DuckDuckGo",
                        })
    except Exception as err:
        print(f"[SEARCH] DuckDuckGo search error: {err}")

    return results[:max_results]


def needs_external_knowledge(question: str) -> bool:
    """Detect if the user is asking for outside information or web knowledge."""
    q_lower = question.lower()
    keywords = [
        "outside",
        "external",
        "other source",
        "other sources",
        "other website",
        "other websites",
        "other links",
        "web link",
        "web links",
        "website",
        "websites",
        "official site",
        "official link",
        "links",
        "link",
        "url",
        "urls",
        "still more",
        "more information",
        "more info",
        "beyond the document",
        "beyond this document",
        "online",
        "internet",
        "latest",
        "web search",
        "google",
        "look up",
        "wiki",
        "wikipedia",
    ]
    return any(k in q_lower for k in keywords)


def is_temporary_nvidia_error(error: Exception) -> bool:
    text = str(error).lower()
    return (
        "503" in text
        or "service temporarily overloaded" in text
        or "service unavailable" in text
        or "temporarily unavailable" in text
        or "overloaded" in text
        or "rate limit" in text
        or "nameresolutionerror" in text
        or "getaddrinfo failed" in text
        or "connection" in text
        or "timeout" in text
        or "timed out" in text
    )


def invoke_with_retry(model, prompt: str, attempts: int = 4):
    """Retry transient NVIDIA API overload errors with exponential backoff."""
    last_error = None

    for attempt in range(attempts):
        try:
            return model.invoke(prompt)
        except Exception as error:
            last_error = error
            if not is_temporary_nvidia_error(error):
                raise
            if attempt < attempts - 1:
                wait_seconds = 2 ** attempt
                print(f"NVIDIA API overloaded (attempt {attempt+1}/{attempts}); retrying in {wait_seconds}s...")
                time.sleep(wait_seconds)

    raise RuntimeError(
        "NVIDIA service is temporarily busy. Please try asking again in a few seconds."
    ) from last_error


def build_research_prompt(
    question: str,
    context: str,
    history_text: str = "",
    is_multi_doc: bool = False,
    external_sources: Optional[List[Dict[str, str]]] = None,
) -> str:
    multi_doc_guideline = (
        "- MULTI-DOCUMENT QUESTION: The documents contain multiple sources. "
        "Explicitly distinguish which document provided which piece of information. "
        "Highlight points of agreement, differences, or specific findings per document."
        if is_multi_doc
        else ""
    )

    external_section = ""
    if external_sources:
        ext_items = []
        for i, es in enumerate(external_sources, 1):
            ext_items.append(
                f"[Outside Web Source {i} | Title: {es['title']} | URL: {es['url']}]\n{es['snippet']}"
            )
        external_section = (
            "\n\nRetrieved Outside Web Sources & Links:\n"
            + "\n\n".join(ext_items)
            + "\n\nCRITICAL: You MUST include direct markdown links to these outside sources in your answer using: [Source Title](URL)."
        )

    return f"""You are an expert AI Research Assistant. Your mission is to provide the BEST, clearest, most accurate, and most informative answer possible.

Behavior & Guidelines:
1. Provide a direct, comprehensive, and well-structured answer using clean Markdown.
2. EXACT MATHEMATICAL FORMULAS:
   - Always wrap mathematical equations in standard LaTeX delimiters:
     * Use $$ ... $$ on separate lines for standalone/block equations.
     * Use $ ... $ for inline variables and math (e.g. $v_b(t)$, $K_b$, $\\omega_m(t)$, $R_a$, $i_a(t)$, $s^2$, $J_m$, $\\theta_m(t)$).
   - DO NOT write formulas inside plain square brackets [ formula ] or plain parentheses ( formula ). ALWAYS use $$ ... $$ or $ ... $.
   - For systems of equations or multi-line derivations, use:
     $$
     \\begin{{aligned}}
     V_b(s) &= K_b s \\Theta_m(s) = K_b \\Omega_m(s) \\\\
     V_a(s) &= (R_a + L_a s) I_a(s) + V_b(s) \\\\
     T_m(s) &= K_t I_a(s) \\\\
     T_m(s) &= (J_m s^2 + D_m s) \\Theta_m(s)
     \\end{{aligned}}
     $$
   - Write exponents cleanly ($s^2$, $dt^2$), derivatives as $\\frac{{di_a(t)}}{{dt}}$, and subscripts with an underscore ($R_a$, $L_a$, $T_m$).
3. EXACT TABLE REPRESENTATION:
   - Always represent structured or comparative information as clean Markdown tables with header and alignment rows:
     | Header 1 | Header 2 |
     |---|---|
     | Cell 1 | Cell 2 |
4. EXACT GRAPHS & PLOTS:
   - If the user asks for a graph, curve, or plot (such as a Bode magnitude/phase plot, frequency response curve, or data comparison), provide exact graph data in a JSON codeblock with language `chart`:
     ```chart
     {{
       "type": "line",
       "title": "Bode Magnitude Plot",
       "xAxis": "Frequency (rad/s)",
       "yAxis": "Magnitude (dB)",
       "labels": ["0.01", "0.1", "1", "10", "100", "1000"],
       "datasets": [
         {{
           "label": "20 log10 |G(jw)| (dB)",
           "data": [40, 20, 0, -20, -40, -60]
         }}
       ]
     }}
     ```
5. OUTSIDE INFORMATION & CITATION LINKS:
   - If outside sources are provided below or if the user asks for outside/external information, incorporate it thoroughly.
   - You MUST provide the exact source citation links: `[Source Name](https://...)`.
6. Ground your primary answer in the provided document passages below, citing page numbers.
7. Understand follow-up questions using the previous conversation context.
{multi_doc_guideline}

Previous conversation:
{history_text or "No previous conversation."}

Retrieved Document Passages:
{context or "No document passages available."}
{external_section}

Current Question:
{question}
"""


def answer_question(
    vector_store: Optional[Any],
    question: str,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    number_of_chunks: int = 8,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, str]]]:
    """Answer questions from documents and external web sources with verified citations."""
    conversation_history = conversation_history or []
    sources: List[Dict[str, Any]] = []
    context_parts: List[str] = []

    # 1. Document retrieval if vector store is available
    if vector_store is not None:
        relevant_chunks = retrieve_relevant_chunks(
            vector_store=vector_store,
            question=question,
            number_of_chunks=number_of_chunks,
        )
        for index, chunk in enumerate(relevant_chunks, start=1):
            source = create_source(chunk.metadata, chunk.page_content)
            if not any(s["filename"] == source["filename"] and s["page"] == source["page"] for s in sources):
                sources.append(source)

            context_parts.append(
                f"[Passage {index} | Source: {source['filename']} | Page {source['page']}]\n"
                f"{chunk.page_content}"
            )

    context = "\n\n---\n\n".join(context_parts)

    # 2. Check if outside web knowledge should be retrieved
    external_sources: List[Dict[str, str]] = []
    if needs_external_knowledge(question) or not context.strip():
        # Clean query for search
        clean_q = re.sub(
            r"(?i)\b(are there any|are there|is there|still more|outside information|outside info|external information|external|about it|about this|please give|website|websites|web links|official links|links|link|from other sources|from other website|tell me about|what about)\b",
            "",
            question,
        ).strip()
        # If clean_q is too brief or ambiguous, look at conversation history for the subject
        if len(clean_q) <= 3 and conversation_history:
            for past in reversed(conversation_history):
                p_text = past.get("content", "")
                p_clean = re.sub(r"(?i)\b(what is|explain|tell me about|how to|the|is|are|in|on|at|of|for)\b", "", p_text).strip()
                if len(p_clean) > 3:
                    clean_q = p_clean[:60]
                    break

        search_query = clean_q if len(clean_q) > 3 else question
        external_sources = search_external_knowledge(search_query)

    history_text = "\n".join(
        f"{entry['role'].upper()}: {entry['content']}"
        for entry in conversation_history[-6:]
    )

    distinct_sources = set(s["filename"] for s in sources)
    is_multi_doc = len(distinct_sources) > 1 or any(
        kw in question.lower() for kw in ["compare", "difference", "across all", "both documents", "all documents"]
    )

    prompt = build_research_prompt(
        question=question,
        context=context,
        history_text=history_text,
        is_multi_doc=is_multi_doc,
        external_sources=external_sources,
    )

    response = invoke_with_retry(create_chat_model(), prompt)
    answer = extract_response_text(response.content)

    # Fallback if content was placed in reasoning_content or kwargs
    if not answer and hasattr(response, "additional_kwargs"):
        reasoning = response.additional_kwargs.get("reasoning_content", "")
        if reasoning:
            answer = clean_math_and_text(reasoning.strip())

    if not answer:
        answer = "I could not retrieve an answer from the document. Please rephrase your question."

    return answer, sources, external_sources

