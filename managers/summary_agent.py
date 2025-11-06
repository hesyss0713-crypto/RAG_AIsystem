from pathlib import Path
from managers.db_manager import get_connection
from managers.llm_manager import LLMManager

# ==============================================================
# 문자열 정리 유틸
# ==============================================================
def sanitize_text(text: str) -> str:
    """DB 저장 전 불필요 문자 및 NULL 바이트 제거"""
    if not text:
        return ""
    cleaned = (
        text.replace("\x00", "")
             .replace("\u0000", "")
             .replace("\r", "")
             .strip()
    )
    return cleaned


# ==============================================================
# 개별 파일 요약 함수
# ==============================================================
def summarize_file(file_path: Path, llm: LLMManager) -> str:
    """파일 확장자에 따라 LLM 또는 고정 문장 요약"""
    ext = file_path.suffix.lower()

    # ---------- 1️⃣ 확장자별 고정 요약 ----------
    if ext in [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".svg"]:
        return "이미지 리소스 파일입니다."
    if ext in [".npy", ".npz", ".pkl", ".ckpt", ".pt", ".h5"]:
        return "머신러닝 모델 학습에 사용되는 데이터 또는 가중치 파일입니다."
    if ext in [".csv", ".xlsx"]:
        return "데이터셋 또는 표 형식 데이터를 저장한 파일입니다."
    if ext in [".md", ".txt"]:
        return f"{file_path.name} 문서 파일입니다."
    if ext == "":
        return f"{file_path.name} 파일입니다."

    # ---------- 2️⃣ 코드 파일인 경우만 LLM 요약 ----------
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except Exception:
        return "파일을 읽을 수 없습니다."

    # English prompt for better structural control
    prompt = f"""
You are an AI assistant that summarizes source code.
Read the given file and describe its **purpose and main functionality** in exactly one concise sentence.
Do not include any introductions, reasoning, or extra comments.

Respond ONLY in the following format:

<summary>Your one-sentence summary in Korean</summary>

File name: {file_path.name}

Code content:
{text}
"""

    summary = llm.generate(prompt, max_new_tokens=512)
    return sanitize_text(summary)


# ==============================================================
# 전체 repo 파일 요약 실행
# ==============================================================
def generate_file_summaries(repo_id: int, repo_dir: Path):
    """해당 repo_id의 모든 파일을 요약"""
    print(f"[LLM] 🔍 generate_file_summaries(repo_id={repo_id})")

    llm = LLMManager()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, file_path FROM files_meta WHERE repo_id = %s;", (repo_id,))
    files = cur.fetchall()

    for file_id, rel_path in files:
        fpath = repo_dir / rel_path
        if not fpath.exists():
            continue

        try:
            summary = summarize_file(fpath, llm)
            summary = sanitize_text(summary)

            cur.execute("UPDATE files_meta SET summary = %s WHERE id = %s;", (summary, file_id))
            conn.commit()
            print(f"[LLM] 🧠 {rel_path}: {summary[:100]}")

        except Exception as e:
            print(f"[LLM] ❌ 요약 실패: {rel_path} ({e})")

    cur.close()
    conn.close()
    print(f"[LLM] ✅ 모든 파일 summary 생성 완료 (repo_id={repo_id})")
