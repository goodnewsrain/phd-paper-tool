"""
core.py — 논문을 읽어 구조화된 요약을 만들고 Notion에 저장하는 핵심 로직.

이 파일은 app.py(화면)가 불러다 쓰는 '엔진'입니다.
코딩을 몰라도 됩니다. 바꾸고 싶다면 대문자 상수(MODEL, SUMMARY_LANGUAGE 등)만
건드리면 충분합니다.
"""

from __future__ import annotations

import base64
import json
import os
import re

import requests
import anthropic
from notion_client import Client as NotionClient


# ─────────────────────────────────────────────────────────────────────────────
# 설정 (여기만 바꾸면 됩니다)
# ─────────────────────────────────────────────────────────────────────────────

# 요약에 사용할 Claude 모델.
# 비용을 조금 아끼고 싶으면 "claude-sonnet-5" 로 바꿔도 됩니다 (품질은 약간 낮아짐).
MODEL = "claude-opus-5"

# Claude에게 보낼 요청당 최대 출력 토큰. 요약 하나엔 충분합니다.
MAX_TOKENS = 16000

# arXiv PDF를 받을 주소 형식
ARXIV_PDF_URL = "https://arxiv.org/pdf/{arxiv_id}.pdf"

# OpenAlex(무료 논문 메타데이터 API)
OPENALEX_WORK_URL = "https://api.openalex.org/works/doi:{doi}"


# ─────────────────────────────────────────────────────────────────────────────
# 설정값 읽기
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """.env 에 넣어둔 값들을 읽어옵니다."""
    return {
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        # 계정에 묶인(identity-linked) 키를 쓸 때만 필요합니다. 워크스페이스 키면 비워도 됨.
        "anthropic_workspace": os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip(),
        "notion_token": os.environ.get("NOTION_TOKEN", "").strip(),
        "notion_parent": os.environ.get("NOTION_PARENT_PAGE_ID", "").strip(),
        "notion_db": os.environ.get("NOTION_DATABASE_ID", "").strip(),
    }


def make_anthropic_client(cfg: dict) -> anthropic.Anthropic:
    """Claude 클라이언트를 만듭니다. 워크스페이스 ID가 있으면 헤더로 함께 보냅니다."""
    headers = {}
    if cfg.get("anthropic_workspace"):
        headers["anthropic-workspace-id"] = cfg["anthropic_workspace"]
    return anthropic.Anthropic(api_key=cfg["anthropic_key"], default_headers=headers)


def extract_notion_id(url_or_id: str) -> str:
    """Notion 페이지/DB 주소나 ID에서 32자리 ID만 뽑아냅니다.

    예) https://www.notion.so/My-Page-1234abcd... → 1234abcd... (하이픈 포함 형태로 변환)
    """
    if not url_or_id:
        return ""
    # 32자리 16진수 덩어리를 찾습니다.
    m = re.search(r"([0-9a-fA-F]{32})", url_or_id.replace("-", ""))
    if not m:
        return url_or_id.strip()
    raw = m.group(1).lower()
    # Notion이 쓰는 하이픈 형태(8-4-4-4-12)로 만들어 돌려줍니다.
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


# ─────────────────────────────────────────────────────────────────────────────
# 입력 해석: arXiv ID / DOI / PDF 파일을 실제 논문 데이터로 바꿉니다
# ─────────────────────────────────────────────────────────────────────────────

def _detect_kind(text: str) -> str:
    """입력이 arXiv인지 DOI인지 판별합니다.

    DOI(10.xxxx/…) 안에 숫자.숫자 패턴이 들어 있어 arXiv로 오인될 수 있으므로
    반드시 DOI를 먼저 확인합니다.
    """
    t = text.strip()
    low = t.lower()
    if low.startswith("10.") or "doi.org" in low:
        return "doi"
    if ("arxiv" in low
            or re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", t)          # 예: 2401.12345
            or re.match(r"^[a-z\-]+(\.[A-Z]{2})?/\d{7}$", t)):  # 옛 형식: math.GT/0309136
        return "arxiv"
    return "unknown"


def _parse_arxiv_id(text: str) -> str:
    """arxiv URL/문자열에서 논문 ID만 뽑습니다."""
    t = text.strip()
    # abs 또는 pdf URL 형태
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?]+)", t)
    if m:
        arxiv_id = m.group(1)
    else:
        # "arXiv:2401.12345" 같은 접두어 제거
        arxiv_id = re.sub(r"(?i)^arxiv:\s*", "", t).strip()
    arxiv_id = arxiv_id.replace(".pdf", "")
    # 버전 표시(v1, v2 …) 제거
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
    return arxiv_id


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex는 초록을 '단어:위치' 형태로 주므로 원래 문장으로 복원합니다."""
    if not inverted_index:
        return ""
    positions = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def resolve_input(text: str, uploaded_pdf_bytes: bytes | None) -> dict:
    """사용자 입력을 요약 가능한 형태로 정리합니다.

    반환 예:
      {
        "pdf_bytes": b"..." 또는 None,   # PDF 전문이 있으면 여기에
        "abstract": "..." 또는 "",       # 전문이 없을 때 초록만
        "source_url": "https://...",     # Notion에 저장할 원문 링크
        "note": "사용자에게 보여줄 안내(선택)",
      }
    """
    # 1) PDF를 직접 업로드한 경우 — 가장 간단하고 품질도 가장 좋음
    if uploaded_pdf_bytes:
        return {
            "pdf_bytes": uploaded_pdf_bytes,
            "abstract": "",
            "source_url": "",
            "note": "",
        }

    text = (text or "").strip()
    if not text:
        raise ValueError("arXiv ID, DOI, 또는 PDF 파일 중 하나를 입력해 주세요.")

    kind = _detect_kind(text)

    # 2) arXiv — PDF를 내려받아 그대로 Claude에게 읽힙니다
    if kind == "arxiv":
        arxiv_id = _parse_arxiv_id(text)
        url = ARXIV_PDF_URL.format(arxiv_id=arxiv_id)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return {
            "pdf_bytes": resp.content,
            "abstract": "",
            "source_url": f"https://arxiv.org/abs/{arxiv_id}",
            "note": "",
        }

    # 3) DOI — OpenAlex에서 메타데이터/초록을 얻고, 공개 PDF가 있으면 내려받습니다
    if kind == "doi":
        doi = text.lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        meta = requests.get(OPENALEX_WORK_URL.format(doi=doi), timeout=60)
        meta.raise_for_status()
        data = meta.json()

        source_url = f"https://doi.org/{doi}"
        pdf_url = None
        best = data.get("best_oa_location") or {}
        oa = data.get("open_access") or {}
        pdf_url = best.get("pdf_url") or oa.get("oa_url")

        if pdf_url:
            try:
                p = requests.get(pdf_url, timeout=60)
                if p.ok and p.headers.get("content-type", "").lower().startswith("application/pdf"):
                    return {"pdf_bytes": p.content, "abstract": "", "source_url": source_url, "note": ""}
            except Exception:
                pass  # 실패하면 아래 초록 기반으로 넘어감

        abstract = _reconstruct_abstract(data.get("abstract_inverted_index"))
        if not abstract:
            raise ValueError("이 DOI에서는 전문 PDF도 초록도 찾지 못했어요. PDF를 직접 업로드해 주세요.")
        return {
            "pdf_bytes": None,
            "abstract": abstract,
            "source_url": source_url,
            "note": "전문 PDF를 구하지 못해 초록만으로 요약했습니다. 더 자세한 요약이 필요하면 PDF를 업로드하세요.",
        }

    raise ValueError("입력을 알아보지 못했어요. arXiv ID(예: 2401.12345), DOI(예: 10.1145/…), 또는 PDF 파일을 넣어 주세요.")


# ─────────────────────────────────────────────────────────────────────────────
# Claude로 구조화 요약 만들기
# ─────────────────────────────────────────────────────────────────────────────

# Claude가 반드시 이 형태(JSON)로만 답하도록 강제하는 스키마입니다.
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "authors": {"type": "string"},
        "year": {"type": "string"},
        "venue": {"type": "string"},
        "tldr": {"type": "string"},
        "problem": {"type": "string"},
        "method": {"type": "string"},
        "key_findings": {"type": "string"},
        "limitations": {"type": "string"},
        "relevance": {"type": "string"},
        "relevance_rating": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "authors", "year", "venue", "tldr",
        "problem", "method", "key_findings", "limitations",
        "relevance", "relevance_rating", "keywords",
    ],
    "additionalProperties": False,
}


def load_research_profile() -> str:
    """research_profile.md 를 읽어 요약 프롬프트에 넣습니다. 없으면 빈 문자열."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_profile.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _build_prompt(language: str, abstract: str | None, research_profile: str) -> str:
    lang_name = "Korean" if language == "ko" else "English"

    profile_block = (
        f"\nThe student's own research focus — judge relevance against THIS:\n"
        f"<research_profile>\n{research_profile}\n</research_profile>\n"
        if research_profile
        else ""
    )

    base = f"""You are helping a PhD student build a rigorous literature review.

Read the attached paper carefully and produce a structured summary.
{profile_block}
Rules:
- Write the summary fields (tldr, problem, method, key_findings, limitations, relevance) in {lang_name}.
- Keep bibliographic fields (title, authors, venue) in the paper's ORIGINAL language — do not translate them.
- Be concrete and specific: name the actual methods, datasets, baselines, and numbers. Avoid vague filler.
- Be critical in "limitations": state real weaknesses, not boilerplate.
- "relevance": explain SPECIFICALLY how this paper relates to the student's research focus above — what they can draw from it (a concept, method, evidence, or useful counterpoint), or why it is only tangential. Be honest when it is not closely related.
- "relevance_rating": rate the paper's relevance to the student's research focus. Exactly one of "High", "Medium", "Low". If no research focus is given, rate general academic significance.
- "keywords": 3-6 short topical tags (single words or short phrases, no commas inside a tag). Prefer terms that connect to the student's field when accurate to the paper.
- If a field is genuinely unknown, use an empty string "".
"""
    if abstract:
        base += f"\n\nThe full PDF was not available. Summarize from this abstract and metadata only, and keep the summary appropriately cautious:\n\n{abstract}"
    return base


def summarize(client: anthropic.Anthropic, resolved: dict, language: str = "ko") -> dict:
    """논문(PDF 또는 초록)을 받아 구조화된 요약(dict)을 돌려줍니다."""
    prompt = _build_prompt(language, resolved.get("abstract"), load_research_profile())

    content: list = []
    if resolved.get("pdf_bytes"):
        b64 = base64.standard_b64encode(resolved["pdf_bytes"]).decode("utf-8")
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        })
    content.append({"type": "text", "text": prompt})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},  # 논문 이해엔 생각을 켜두는 편이 좋습니다
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError("모델이 이 요청을 거절했습니다. 다른 논문으로 시도해 주세요.")

    # 구조화 출력이 켜져 있으면 text 블록에 유효한 JSON이 담겨 옵니다.
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("요약 결과가 비어 있습니다. 다시 시도해 주세요.")
    return json.loads(text)


# ─────────────────────────────────────────────────────────────────────────────
# Notion 저장
# ─────────────────────────────────────────────────────────────────────────────

def make_notion_client(token: str) -> NotionClient:
    # 설치된 notion-client 의 기본 버전을 사용합니다(데이터소스 API).
    return NotionClient(auth=token)


# 문헌 DB의 속성(칼럼) 정의 — 새 DB 생성과 기존 DB 보정에 함께 씁니다.
DB_PROPERTIES = {
    "Title": {"title": {}},
    "Authors": {"rich_text": {}},
    "Year": {"number": {}},
    "Venue": {"rich_text": {}},
    "TLDR": {"rich_text": {}},
    "Tags": {"multi_select": {}},
    "Source": {"url": {}},
    "Relevance": {
        "select": {
            "options": [
                {"name": "High", "color": "green"},
                {"name": "Medium", "color": "yellow"},
                {"name": "Low", "color": "gray"},
            ]
        }
    },
    "Status": {
        "select": {
            "options": [
                {"name": "To read", "color": "gray"},
                {"name": "Reading", "color": "yellow"},
                {"name": "Read", "color": "green"},
            ]
        }
    },
}


def ensure_database(notion: NotionClient, parent_page_id: str) -> str:
    """문헌 DB를 새로 만들고, 논문을 저장할 '데이터소스 ID'를 돌려줍니다.

    (새 Notion API에서는 데이터베이스가 '데이터소스'를 품고, 실제 행/속성은
     데이터소스에 있습니다. 그래서 페이지 저장·검색에 데이터소스 ID를 씁니다.)
    """
    parent_page_id = extract_notion_id(parent_page_id)
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "📚 Literature (논문 라이브러리)"}}],
        initial_data_source={"properties": DB_PROPERTIES},
    )
    ds_id = db["data_sources"][0]["id"]
    add_related_relation(notion, ds_id)  # "Related" 관계 칼럼(유사 논문 연동용) 추가
    return ds_id


def _text_blocks(heading: str, body: str) -> list:
    """제목 + 본문 문단을 Notion 블록으로 만듭니다. (2000자 제한을 고려해 나눔)"""
    blocks = [{
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]},
    }]
    body = (body or "").strip() or "—"
    # Notion은 rich_text 한 조각이 2000자를 넘으면 거부하므로 잘라서 여러 문단으로.
    for i in range(0, len(body), 1900):
        chunk = body[i:i + 1900]
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })
    return blocks


def _clean_tag(tag: str) -> str:
    # Notion multi_select 옵션 이름에는 쉼표가 들어갈 수 없습니다.
    return tag.replace(",", " ").strip()[:100]


def save_to_notion(notion: NotionClient, data_source_id: str, summary: dict, source_url: str) -> str:
    """요약을 문헌 데이터소스에 새 페이지로 저장하고, 그 페이지 주소를 돌려줍니다."""
    # 연도는 숫자로 변환 시도
    year_val = None
    try:
        year_val = int(re.search(r"\d{4}", str(summary.get("year", ""))).group(0))
    except Exception:
        year_val = None

    tags = [{"name": _clean_tag(t)} for t in (summary.get("keywords") or []) if _clean_tag(t)]

    properties = {
        "Title": {"title": [{"type": "text", "text": {"content": (summary.get("title") or "제목 없음")[:2000]}}]},
        "Authors": {"rich_text": [{"type": "text", "text": {"content": (summary.get("authors") or "")[:2000]}}]},
        "Venue": {"rich_text": [{"type": "text", "text": {"content": (summary.get("venue") or "")[:2000]}}]},
        "TLDR": {"rich_text": [{"type": "text", "text": {"content": (summary.get("tldr") or "")[:2000]}}]},
        "Tags": {"multi_select": tags},
        "Status": {"select": {"name": "To read"}},
    }
    if year_val:
        properties["Year"] = {"number": year_val}
    if source_url:
        properties["Source"] = {"url": source_url}
    rating = summary.get("relevance_rating")
    if rating in ("High", "Medium", "Low"):
        properties["Relevance"] = {"select": {"name": rating}}

    children: list = []
    if summary.get("tldr"):
        children += _text_blocks("TL;DR", summary["tldr"])
    children += _text_blocks("문제 (Problem)", summary.get("problem", ""))
    children += _text_blocks("방법 (Method)", summary.get("method", ""))
    children += _text_blocks("핵심 결과 (Key findings)", summary.get("key_findings", ""))
    children += _text_blocks("한계 (Limitations)", summary.get("limitations", ""))
    children += _text_blocks("내 연구와의 관련성 (Relevance)", summary.get("relevance", ""))

    page = notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id},
        properties=properties,
        children=children,
    )
    return page  # {"id":..., "url":...} — 호출부에서 url/id를 씁니다


# ─────────────────────────────────────────────────────────────────────────────
# 유사 논문 자동 연동 (Related 관계 칼럼)
# ─────────────────────────────────────────────────────────────────────────────

def add_related_relation(notion: NotionClient, data_source_id: str) -> None:
    """데이터소스에 자기 자신을 가리키는 양방향 'Related' 관계 칼럼을 추가합니다."""
    try:
        notion.data_sources.update(data_source_id, properties={
            "Related": {"relation": {
                "data_source_id": data_source_id,
                "type": "dual_property",
                "dual_property": {},
            }}
        })
        # 자동 생성되는 역방향 칼럼의 긴 이름을 짧게 정리
        props = notion.data_sources.retrieve(data_source_id).get("properties", {})
        for name, p in props.items():
            if p.get("type") == "relation" and name != "Related":
                try:
                    notion.data_sources.update(data_source_id, properties={name: {"name": "Related ↔"}})
                except Exception:
                    pass
                break
    except Exception:
        pass  # 이미 있으면 무시


def _plain_text(prop: dict | None) -> str:
    """Notion title/rich_text 속성에서 순수 텍스트만 뽑습니다."""
    if not prop:
        return ""
    t = prop.get("type")
    arr = prop.get(t)
    if t in ("title", "rich_text") and isinstance(arr, list):
        return "".join(x.get("plain_text", "") for x in arr)
    return ""


def list_library(notion: NotionClient, data_source_id: str, exclude_page_id: str | None = None) -> list[dict]:
    """저장된 논문 목록을 [{id, title, tldr, keywords}] 로 가져옵니다."""
    items: list[dict] = []
    cursor = None
    for _ in range(20):  # 안전장치 (최대 2000편)
        kwargs = {"page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(data_source_id, **kwargs)
        for page in resp.get("results", []):
            if exclude_page_id and page["id"] == exclude_page_id:
                continue
            if page.get("archived") or page.get("in_trash"):
                continue
            props = page.get("properties", {})
            items.append({
                "id": page["id"],
                "title": _plain_text(props.get("Title")),
                "tldr": _plain_text(props.get("TLDR")),
                "keywords": [o.get("name", "") for o in ((props.get("Tags") or {}).get("multi_select") or [])],
            })
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return items


_RELATED_SCHEMA = {
    "type": "object",
    "properties": {"related_indices": {"type": "array", "items": {"type": "integer"}}},
    "required": ["related_indices"],
    "additionalProperties": False,
}


def find_related(client: anthropic.Anthropic, summary: dict, library: list[dict], max_links: int = 5) -> list[str]:
    """새 논문과 기존 라이브러리를 비교해, 관련된 논문의 page id 목록을 돌려줍니다."""
    if not library:
        return []
    lines = []
    for i, p in enumerate(library):
        kw = ", ".join(p.get("keywords") or [])
        lines.append(f"[{i}] {p.get('title','')} — {(p.get('tldr') or '')[:200]} (keywords: {kw})")
    index = "\n".join(lines)
    new_desc = f"{summary.get('title','')} — {summary.get('tldr','')} (keywords: {', '.join(summary.get('keywords') or [])})"
    prompt = (
        "You maintain a personal research literature library. Link genuinely related papers.\n\n"
        f"NEW paper:\n{new_desc}\n\n"
        f"EXISTING papers:\n{index}\n\n"
        "Return the indices [i] of existing papers that are GENUINELY related to the NEW paper — "
        "shared research topic, theory, method, dataset, or one clearly building on the other. "
        "Be selective: only connections a researcher would actually want linked in a literature review, "
        "not loose thematic overlaps. Return an empty list if none. "
        f"At most {max_links} indices, most-related first."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        output_config={"format": {"type": "json_schema", "schema": _RELATED_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    idxs = json.loads(text).get("related_indices", [])
    ids = []
    for i in idxs[:max_links]:
        if isinstance(i, int) and 0 <= i < len(library):
            ids.append(library[i]["id"])
    return ids


def set_related(notion: NotionClient, page_id: str, related_ids: list[str]) -> None:
    """페이지의 'Related' 관계를 주어진 논문들로 설정합니다."""
    if not related_ids:
        return
    notion.pages.update(page_id, properties={
        "Related": {"relation": [{"id": rid} for rid in related_ids]}
    })


def link_new_paper(notion: NotionClient, client: anthropic.Anthropic, data_source_id: str,
                   summary: dict, new_page_id: str) -> int:
    """방금 저장한 논문을 기존 라이브러리의 관련 논문들과 연결합니다. 연결 수를 돌려줍니다."""
    library = list_library(notion, data_source_id, exclude_page_id=new_page_id)
    related = find_related(client, summary, library)
    set_related(notion, new_page_id, related)
    return len(related)


def relink_all(notion: NotionClient, client: anthropic.Anthropic, data_source_id: str) -> int:
    """라이브러리 전체를 다시 스캔해 관련 논문끼리 연결합니다. (이미 저장된 논문 보정용)"""
    add_related_relation(notion, data_source_id)  # 관계 칼럼이 없으면 먼저 추가
    lib = list_library(notion, data_source_id)
    linked = 0
    for p in lib:
        others = [q for q in lib if q["id"] != p["id"]]
        related = find_related(client, p, others)
        if related:
            set_related(notion, p["id"], related)
            linked += 1
    return linked
