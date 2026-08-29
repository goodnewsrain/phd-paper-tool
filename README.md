# 📚 논문 요약 → Notion

arXiv ID / DOI / PDF 를 넣으면 **Claude가 논문을 읽고 구조화 요약**(문제·방법·결과·한계·내 연구와의 관련성)을 만들어 **Notion 데이터베이스에 자동 저장**하는 개인 도구입니다.

코딩 지식이 없어도 됩니다. 아래를 **순서대로** 따라 하면 됩니다.

---

## 딱 한 번만 하면 되는 설정

준비물은 두 개의 무료 키(하나는 Claude, 하나는 Notion)와 Notion 페이지 하나뿐입니다.
소요 시간 약 10분.

### 1. 라이브러리 설치 (이미 완료돼 있을 수 있음)

이 폴더에는 이미 설치가 끝나 있습니다. 혹시 다른 컴퓨터에서 새로 시작한다면 폴더 안에서:

```bash
python3 -m venv .venv
```
```bash
.venv/bin/python -m pip install -r requirements.txt
```

### 2. Claude(Anthropic) API 키 받기

1. https://console.claude.com  →  로그인
2. 왼쪽 메뉴 **API keys**  →  **Create Key**
3. 나온 키(`sk-ant-...`)를 복사해 둡니다. (한 번만 보여주니 잘 저장)

> 사용한 만큼 요금이 붙습니다. 논문 한 편 요약은 보통 수 센트 수준입니다. 처음엔 소액 크레딧으로 시작하세요.

### 3. Notion 통합(integration) 토큰 받기

1. https://www.notion.so/my-integrations  →  **New integration**
2. 이름 아무거나(예: `Paper Summarizer`) 입력하고 만들기
3. **Internal Integration Secret** (`ntn_...` 또는 `secret_...`)을 복사해 둡니다.

### 4. Notion 페이지 만들고 통합에 연결

1. Notion에서 **빈 페이지**를 하나 만듭니다. (예: "논문 라이브러리")
2. 그 페이지 **우측 상단 `•••`  →  `연결(Connections)`  →  방금 만든 통합** 을 선택해 연결합니다.
   - ⚠️ 이 단계를 빠뜨리면 앱이 페이지에 접근하지 못합니다. 가장 흔한 실수예요.
3. 페이지 주소(URL)를 복사해 둡니다. (주소창의 `https://www.notion.so/...`)

### 5. `.env` 파일에 값 채우기

폴더 안의 `.env.example` 을 복사해 `.env` 로 만든 뒤 값을 채웁니다:

```bash
cp .env.example .env
```

그다음 `.env` 파일을 텍스트 편집기로 열어 2~4단계에서 받은 값을 붙여넣습니다:

```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
NOTION_PARENT_PAGE_ID=https://www.notion.so/내가-만든-페이지-주소
```

`NOTION_DATABASE_ID` 줄은 **비워 두세요** — 처음 실행할 때 자동으로 채워집니다.

---

## 실행하기

폴더 안에서:

```bash
.venv/bin/streamlit run app.py
```

브라우저가 자동으로 열립니다. (안 열리면 터미널에 표시된 `http://localhost:8501` 주소를 클릭)

- **처음 실행**하면 4단계에서 만든 Notion 페이지 안에 `📚 Literature` 데이터베이스가 자동 생성됩니다.
- 이후에는 arXiv ID·DOI·PDF 를 넣고 **요약하기** 버튼만 누르면 됩니다.

멈추려면 터미널에서 `Ctrl + C`.

---

## 매일 쓰는 법

1. `.venv/bin/streamlit run app.py` 실행
2. 논문의 arXiv ID(예: `2401.12345`), DOI(예: `10.1145/...`), 또는 PDF 업로드
3. 언어(한국어/English) 선택 → **요약하기**
4. 몇 십 초 뒤 Notion에 저장됨 → **Notion에서 열기** 링크 클릭

---

## 자주 나는 문제

| 증상 | 해결 |
|---|---|
| `Notion 데이터베이스 준비에 실패` | 4단계의 **페이지-통합 연결**을 안 했을 가능성이 큽니다. 페이지 `•••` → 연결 확인. |
| `.env 값을 채워 주세요` | `.env` 파일이 폴더 안에 있고 값이 채워졌는지 확인. (`.env.example`이 아니라 `.env`) |
| DOI인데 "초록만으로 요약" | 그 논문의 공개 PDF를 못 찾은 경우입니다. 정확도를 높이려면 PDF를 직접 업로드하세요. |
| `command not found: streamlit` | `streamlit` 이 아니라 `.venv/bin/streamlit` 으로 실행하세요. |

---

## 바꾸고 싶을 때 (선택)

- **요약 항목·말투 바꾸기**: `core.py` 안의 `_build_prompt()` 함수 문구를 수정.
- **모델 바꾸기**: `core.py` 맨 위 `MODEL = "claude-opus-5"`. 비용을 아끼려면 `"claude-sonnet-5"`.
- **Notion 항목(열) 바꾸기**: `core.py` 의 `ensure_database()` 와 `save_to_notion()` 수정.

막히면 이 폴더를 열어둔 채로 저(Claude Code)에게 "이거 이렇게 바꿔줘" 라고 하면 됩니다.
