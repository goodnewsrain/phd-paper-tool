"""
app.py — 화면(웹 앱). 터미널에서 아래 한 줄로 실행합니다:

    streamlit run app.py

브라우저가 자동으로 열립니다.
"""

import os

import streamlit as st
from dotenv import load_dotenv, find_dotenv
import anthropic

import core

# .env 파일에서 API 키 등을 불러옵니다. (override=True: 파일이 항상 최신 기준)
load_dotenv(find_dotenv(), override=True)

st.set_page_config(page_title="논문 요약 → Notion", page_icon="📚", layout="centered")

# 클라우드 배포 시: Streamlit 비밀 저장소(st.secrets)의 값을 환경변수로 옮깁니다.
# (로컬에선 secrets 파일이 없으므로 그냥 넘어가고 .env 를 씁니다.)
try:
    for _k in ["ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "NOTION_TOKEN",
               "NOTION_PARENT_PAGE_ID", "NOTION_DATABASE_ID", "NOTION_BOOKS_DB_ID",
               "APP_PASSWORD", "RESEARCH_PROFILE"]:
        if _k in st.secrets and st.secrets[_k]:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

st.title("📚 논문 요약 → Notion")
st.caption("arXiv · DOI · PDF 를 넣으면 Claude가 구조화 요약을 만들어 Notion에 저장합니다.")


# ── 비밀번호 잠금 (배포 시 APP_PASSWORD 가 설정돼 있으면 요구) ──────────────
def _check_password() -> bool:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return True  # 비밀번호 미설정(로컬) → 통과
    if st.session_state.get("_auth_ok"):
        return True
    pw = st.text_input("🔒 비밀번호", type="password")
    if not pw:
        return False
    if pw == expected:
        st.session_state["_auth_ok"] = True
        return True
    st.error("비밀번호가 틀렸어요.")
    return False


if not _check_password():
    st.stop()

cfg = core.load_config()

# ── 준비 상태 점검 ──────────────────────────────────────────────────────────
missing = []
if not cfg["anthropic_key"]:
    missing.append("`ANTHROPIC_API_KEY`")
if not cfg["notion_token"]:
    missing.append("`NOTION_TOKEN`")
if not cfg["notion_db"] and not cfg["notion_parent"]:
    missing.append("`NOTION_PARENT_PAGE_ID` (또는 `NOTION_DATABASE_ID`)")

if missing:
    st.error("먼저 `.env` 파일에 다음 값을 채워 주세요: " + ", ".join(missing))
    st.info("설정 방법은 옆의 **README.md** 를 참고하세요.")
    st.stop()


# ── Notion DB 준비 (처음 한 번만 자동 생성) ─────────────────────────────────
def get_or_create_db() -> str:
    if cfg["notion_db"]:
        return core.extract_notion_id(cfg["notion_db"])

    notion = core.make_notion_client(cfg["notion_token"])
    with st.spinner("Notion에 문헌 데이터베이스를 만드는 중…"):
        db_id = core.ensure_database(notion, cfg["notion_parent"])

    # 다음 실행부터 재사용하도록 .env 에 자동 저장
    try:
        env_path = find_dotenv() or ".env"
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nNOTION_DATABASE_ID={db_id}\n")
    except Exception:
        pass

    st.success("문헌 데이터베이스를 만들었어요! (다음부터는 자동으로 재사용됩니다)")
    st.code(f"NOTION_DATABASE_ID={db_id}", language="bash")
    return db_id


def get_or_create_books_db() -> str:
    if cfg["notion_books"]:
        return core.extract_notion_id(cfg["notion_books"])
    notion = core.make_notion_client(cfg["notion_token"])
    with st.spinner("Notion에 자료 노트 데이터베이스를 만드는 중…"):
        bid = core.ensure_books_database(notion, cfg["notion_parent"])
    try:
        env_path = find_dotenv() or ".env"
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nNOTION_BOOKS_DB_ID={bid}\n")
    except Exception:
        pass
    st.info("📖 자료 노트 데이터베이스를 만들었어요. (Streamlit 비밀값에 NOTION_BOOKS_DB_ID를 추가하면 재사용됩니다)")
    st.code(f"NOTION_BOOKS_DB_ID={bid}", language="bash")
    return bid


try:
    db_id = get_or_create_db()
except Exception as e:
    st.error(f"Notion 데이터베이스 준비에 실패했어요: {e}")
    st.info("공유하려는 Notion 페이지를 통합(integration)에 연결했는지 확인해 주세요. "
            "(페이지 우상단 ••• → 연결 → 통합 선택)")
    st.stop()


# ── 입력 ────────────────────────────────────────────────────────────────────
st.divider()
mode = st.radio("무엇을 추가할까요?", ["📄 논문", "📖 책 (내 노트)"], horizontal=True)
language = st.radio("정리 언어", ["한국어", "English"], horizontal=True)
lang_code = "ko" if language == "한국어" else "en"

identifier = ""
uploaded = None
book_title = book_author = book_notes = book_quotes = ""
existing_book_id = None
books_db_id = ""
if mode == "📄 논문":
    identifier = st.text_input("arXiv ID · arXiv 링크 · DOI", placeholder="예: 2401.12345  또는  10.1145/3593013.3594001")
    uploaded = st.file_uploader("또는 PDF 파일 업로드", type=["pdf"])
    go = st.button("요약하기", type="primary", use_container_width=True)
else:
    books_db_id = get_or_create_books_db()
    try:
        _books = core.list_books(core.make_notion_client(cfg["notion_token"]), books_db_id)
    except Exception:
        _books = []
    _opts = ["+ 새 책 추가"] + [b["title"] for b in _books]
    _choice = st.selectbox("책 선택", _opts, help="이미 등록한 책은 골라서 노트·구절만 이어서 추가할 수 있어요.")
    if _choice == "+ 새 책 추가":
        book_title = st.text_input("책 제목", placeholder="예: Marginality: The Key to Multicultural Theology")
        book_author = st.text_input("저자 (선택)", placeholder="예: Jung Young Lee")
    else:
        _b = _books[_opts.index(_choice) - 1]
        book_title, book_author, existing_book_id = _b["title"], _b["authors"], _b["id"]
        st.caption(f"📖 기존 책에 이어서 추가: **{book_title}**" + (f" — {book_author}" if book_author else ""))
    book_notes = st.text_area("내 노트 / 메모 (선택)", height=160,
                              placeholder="이 책을 읽으며 정리한 노트를 붙여넣으세요.")
    book_quotes = st.text_area("📌 Kindle 구절 / 인용 (선택)", height=140,
                               placeholder="킨들에서 복사한 하이라이트를 붙여넣으세요. 자동으로 인용 형식을 만들어요.")
    go = st.button("저장하기", type="primary", use_container_width=True)

with st.expander("🔗 라이브러리 정리 — 관련 논문 다시 연결"):
    st.caption("저장된 모든 논문을 다시 스캔해 관련 논문끼리 연결해요. 논문 수만큼 시간·비용이 들어요.")
    if st.button("전체 다시 연결"):
        try:
            with st.spinner("전체 라이브러리 연결 중…"):
                n = core.relink_all(
                    core.make_notion_client(cfg["notion_token"]),
                    core.make_anthropic_client(cfg),
                    db_id,
                )
            st.success(f"{n}편에 관련 논문을 연결했어요.")
        except Exception as e:
            st.error(f"연결 실패: {e}")


# ── 실행 ────────────────────────────────────────────────────────────────────
if go:
    try:
        summary = None
        page_url = ""
        notion = core.make_notion_client(cfg["notion_token"])
        client = core.make_anthropic_client(cfg)

        if mode == "📖 책 (내 노트)":
            has_content = bool(book_notes.strip() or book_quotes.strip())
            quotes = []
            if book_quotes.strip():
                with st.spinner("Kindle 구절을 인용 형식으로 정리하는 중…"):
                    quotes = core.format_quotes(client, book_quotes, book_title, book_author, lang_code)
            # 검색용 키워드·한줄요약 색인 (심사 아님)
            index_text = (book_notes + "\n" + " ".join(q.get("quote", "") for q in quotes)).strip()
            idx = {"tldr": "", "keywords": []}
            if index_text:
                with st.spinner("검색용 키워드 색인 중…"):
                    idx = core.index_notes(client, book_title, book_author, index_text, lang_code)

            if existing_book_id:
                if not has_content:
                    st.error("추가할 노트나 구절을 입력해 주세요.")
                else:
                    with st.spinner("기존 자료에 이어서 저장하는 중…"):
                        core.append_to_book(notion, existing_book_id, book_notes, quotes)
                        core.add_keywords_to_page(notion, existing_book_id, idx["keywords"])
                    st.success(f"기존 자료 «{book_title}»에 추가했어요.")
                    _bp = notion.pages.retrieve(existing_book_id)
                    if _bp.get("url"):
                        st.markdown(f"👉 [Notion에서 열기]({_bp['url']})")
                    if idx["keywords"]:
                        st.write("🏷️ " + " ".join(f"`{k}`" for k in idx["keywords"]))
            else:
                if not (book_title.strip() and has_content):
                    st.error("책 제목과, 노트 또는 구절을 입력해 주세요.")
                else:
                    with st.spinner("자료 노트로 저장하는 중…"):
                        page = core.save_book(notion, books_db_id, book_title, book_author,
                                              idx["tldr"], idx["keywords"],
                                              my_notes=book_notes, quotes=quotes)
                        page_url = page.get("url", "")
                    st.success("자료 노트로 저장했어요. (심사 없이 저장 · 키워드로 검색 가능)")
                    if page_url:
                        st.markdown(f"👉 [Notion에서 열기]({page_url})")
                    if idx["keywords"]:
                        st.write("🏷️ " + " ".join(f"`{k}`" for k in idx["keywords"]))
                    if idx["tldr"]:
                        st.info(idx["tldr"])
        else:
            with st.spinner("논문을 가져오는 중…"):
                resolved = core.resolve_input(identifier, uploaded.getvalue() if uploaded else None)
            if resolved.get("note"):
                st.info(resolved["note"])
            with st.spinner("Claude가 논문을 읽고 요약하는 중… (길면 1~2분 걸릴 수 있어요)"):
                summary = core.summarize(client, resolved, language=lang_code)
            with st.spinner("Notion에 저장하는 중…"):
                page = core.save_to_notion(notion, db_id, summary, resolved.get("source_url", ""),
                                           pdf_bytes=resolved.get("pdf_bytes"), item_type="Paper")
                page_url = page.get("url", "")

        if summary is None:
            st.stop()

        # 비슷한 자료 자동 연동
        try:
            with st.spinner("비슷한 자료 찾아 연결하는 중…"):
                n_linked = core.link_new_paper(notion, client, db_id, summary, page["id"])
            if n_linked:
                st.caption(f"🔗 관련 자료 {n_linked}건과 자동으로 연결했어요")
        except Exception as e:
            st.caption(f"(관련 자료 연결은 건너뜀: {e})")

        st.success("완료! Notion에 저장했습니다.")
        if page_url:
            st.markdown(f"👉 [Notion에서 열기]({page_url})")

        # 화면에도 정리 결과를 보여줍니다.
        st.divider()
        st.subheader(summary.get("title") or "제목 없음")
        meta = " · ".join(x for x in [summary.get("authors"), str(summary.get("year") or ""), summary.get("venue")] if x)
        if meta:
            st.caption(meta)
        rating = summary.get("relevance_rating")
        eng = summary.get("engagement")
        badges = []
        if rating:
            badges.append("관련도: " + {"High": "🟢 높음", "Medium": "🟡 중간", "Low": "⚪ 낮음"}.get(rating, rating))
        if eng:
            badges.append("관여: " + {"Deep read": "📕 정독", "Cite": "📎 인용", "Skim": "💨 훑기"}.get(eng, eng))
        if badges:
            st.markdown("  ·  ".join(f"**{b}**" for b in badges))
        if summary.get("keywords"):
            st.write(" ".join(f"`{k}`" for k in summary["keywords"]))
        if summary.get("tldr"):
            st.info(summary["tldr"])

        sections = [
            ("문제 (Problem)", "problem"),
            ("방법 (Method)", "method"),
            ("핵심 결과 (Key findings)", "key_findings"),
            ("기여도 (Contribution)", "contribution"),
            ("⚠️ 비판적 검토 (Critical appraisal)", "critical_appraisal"),
            ("내 연구에서의 활용 (Use in my work)", "use_in_my_work"),
            ("🔎 참고 활용 (Reference value)", "reference_value"),
            ("🎓 체어 총평 (Chair's verdict)", "verdict"),
        ]
        for heading, key in sections:
            if summary.get(key):
                st.markdown(f"**{heading}**")
                st.write(summary[key])

    except Exception as e:
        st.error(f"문제가 생겼어요: {e}")
