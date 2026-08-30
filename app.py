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
               "NOTION_PARENT_PAGE_ID", "NOTION_DATABASE_ID", "APP_PASSWORD",
               "RESEARCH_PROFILE"]:
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


try:
    db_id = get_or_create_db()
except Exception as e:
    st.error(f"Notion 데이터베이스 준비에 실패했어요: {e}")
    st.info("공유하려는 Notion 페이지를 통합(integration)에 연결했는지 확인해 주세요. "
            "(페이지 우상단 ••• → 연결 → 통합 선택)")
    st.stop()


# ── 입력 ────────────────────────────────────────────────────────────────────
st.divider()
identifier = st.text_input("arXiv ID · arXiv 링크 · DOI", placeholder="예: 2401.12345  또는  10.1145/3593013.3594001")
uploaded = st.file_uploader("또는 PDF 파일 업로드", type=["pdf"])
language = st.radio("요약 언어", ["한국어", "English"], horizontal=True)
lang_code = "ko" if language == "한국어" else "en"

go = st.button("요약하기", type="primary", use_container_width=True)

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
        with st.spinner("논문을 가져오는 중…"):
            resolved = core.resolve_input(identifier, uploaded.getvalue() if uploaded else None)

        if resolved.get("note"):
            st.info(resolved["note"])

        client = core.make_anthropic_client(cfg)
        with st.spinner("Claude가 논문을 읽고 요약하는 중… (길면 1~2분 걸릴 수 있어요)"):
            summary = core.summarize(client, resolved, language=lang_code)

        with st.spinner("Notion에 저장하는 중…"):
            notion = core.make_notion_client(cfg["notion_token"])
            page = core.save_to_notion(notion, db_id, summary, resolved.get("source_url", ""),
                                       pdf_bytes=resolved.get("pdf_bytes"))
            page_url = page.get("url", "")

        # 비슷한 논문 자동 연동
        try:
            with st.spinner("비슷한 논문 찾아 연결하는 중…"):
                n_linked = core.link_new_paper(notion, client, db_id, summary, page["id"])
            if n_linked:
                st.caption(f"🔗 관련 논문 {n_linked}편과 자동으로 연결했어요")
        except Exception as e:
            st.caption(f"(관련 논문 연결은 건너뜀: {e})")

        st.success("완료! Notion에 저장했습니다.")
        if page_url:
            st.markdown(f"👉 [Notion에서 열기]({page_url})")

        # 화면에도 요약을 보여줍니다.
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
            ("🎓 체어 총평 (Chair's verdict)", "verdict"),
        ]
        for heading, key in sections:
            if summary.get(key):
                st.markdown(f"**{heading}**")
                st.write(summary[key])

    except Exception as e:
        st.error(f"문제가 생겼어요: {e}")
