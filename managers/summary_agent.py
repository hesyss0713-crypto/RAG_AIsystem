# /app/managers/summary_agent.py
from pathlib import Path
from managers.db_manager import get_connection
from managers.llm_manager import LLMManager


def sanitize_text(text: str) -> str:
    """DB 저장 전 불필요 문자 및 NULL 바이트 제거"""
    if not text:
        return ""
    return (
        text.replace("\x00", "")
            .replace("\u0000", "")
            .replace("\r", "")
            .strip()
    )


def summarize_file(file_path: Path, llm: LLMManager) -> str:
    """확장자에 따라 요약 생성"""
    ext = file_path.suffix.lower()

    # 고정 요약
    if ext in [".png", ".jpg", ".jpeg", ".gif"]:
        return "이미지 리소스 파일입니다."
    if ext in [".npy", ".npz", ".pt", ".pkl", ".h5"]:
        return "머신러닝 모델의 데이터 또는 가중치 파일입니다."
    if ext in [".csv", ".xlsx"]:
        return "데이터셋 파일입니다."
    if ext in [".md", ".txt"]:
        return f"{file_path.name} 문서 파일입니다."

    # 코드 파일만 LLM 요약
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except Exception:
        return "파일을 읽을 수 없습니다."

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


def generate_file_summaries(repo_id: int, repo_dir: Path):
    """해당 repo의 모든 파일을 요약"""
    print(f"[Summary] 🔍 Generating file summaries for repo {repo_id}")
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
            cur.execute("UPDATE files_meta SET summary = %s WHERE id = %s;", (summary, file_id))
            conn.commit()
            print(f"[Summary] ✅ {rel_path}: {summary[:80]}")
        except Exception as e:
            print(f"[Summary] ❌ {rel_path}: {e}")

    cur.close()
    conn.close()
    print(f"[Summary] ✅ All file summaries done for repo_id={repo_id}")
