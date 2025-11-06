import json
import re
from pathlib import Path
from managers.llm_manager import LLMManager
from managers.db_manager import get_connection
from psycopg2.extras import execute_values


class LLMAgent:
    def __init__(self):
        self.llm = LLMManager()

    # -------------------------------------------------------------
    # 🔹 파일 요약
    # -------------------------------------------------------------
    def summarize_file(self, file_path: Path) -> str:
        """LLM 기반 코드/문서 요약"""
        ext = file_path.suffix.lower()

        # 비코드 파일 고정 문장
        if ext in [".png", ".jpg", ".jpeg", ".gif"]:
            return "이미지 리소스 파일입니다."
        if ext in [".npy", ".npz", ".pt", ".pkl", ".h5"]:
            return "머신러닝 모델의 데이터 또는 가중치 파일입니다."
        if ext in [".csv", ".xlsx"]:
            return "데이터셋 파일입니다."
        if ext in [".md", ".txt"]:
            return f"{file_path.name} 문서 파일입니다."

        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except Exception:
            return "파일을 읽을 수 없습니다."

        user_prompt = f"File name: {file_path.name}\n\nCode content:\n{text}"
        result = self.llm.generate(user_prompt, task="summarization", max_new_tokens=512)
        if "<summary>" in result:
            result = result.split("<summary>")[-1].split("</summary>")[0]
        return result.strip()

    # -------------------------------------------------------------
    # 🔹 코드 semantic chunk 생성
    # -------------------------------------------------------------
    def extract_chunks(self, file_path: Path):
        """LLM으로 semantic chunk 분리"""
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")[:6000]
        except Exception:
            print(f"[Chunk] ❌ 파일 읽기 실패: {file_path}")
            return []

        user_prompt = f"File: {file_path.name}\n\nCode:\n{text}"
        res = self.llm.generate(user_prompt, task="chunking", max_new_tokens=2048)

        match = re.search(r"(\[.*\])", res, re.DOTALL)
        if not match:
            print(f"[Chunk] ⚠️ No JSON block found for {file_path}")
            return []

        try:
            json_str = match.group(1).replace("'", '"').replace("\x00", "")
            chunks = json.loads(json_str)
            return [
                {
                    "semantic_scope": c.get("semantic_scope", "").strip(),
                    "hierarchical_context": c.get("hierarchical_context", "").strip(),
                    "content": c.get("content", "").strip(),
                }
                for c in chunks if isinstance(c, dict)
            ]
        except Exception as e:
            print(f"[Chunk] ⚠️ JSON parsing failed for {file_path}: {e}")
            return []

    # -------------------------------------------------------------
    # 🔹 repo_id 기준으로 파일 전체 요약
    # -------------------------------------------------------------
    def summarize_repo_files(self, repo_id: int, repo_dir: Path):
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, file_path FROM files_meta WHERE repo_id = %s;", (repo_id,))
        files = cur.fetchall()

        for file_id, rel_path in files:
            fpath = repo_dir / rel_path
            if not fpath.exists():
                continue
            try:
                summary = self.summarize_file(fpath)
                cur.execute("UPDATE files_meta SET summary = %s WHERE id = %s;", (summary, file_id))
                conn.commit()
                print(f"[Summary] ✅ {rel_path}")
            except Exception as e:
                print(f"[Summary] ⚠️ {rel_path}: {e}")

        cur.close()
        conn.close()
        print(f"[Summary] ✅ repo_id={repo_id} summaries complete")

    # -------------------------------------------------------------
    # 🔹 repo_id 기준으로 전체 chunk 생성 후 DB 저장
    # -------------------------------------------------------------
    def chunk_repo_files(self, repo_id: int, repo_dir: Path):
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, file_path, file_type FROM files_meta WHERE repo_id = %s;", (repo_id,))
        files = cur.fetchall()
        cur.close()
        conn.close()

        for file_id, rel_path, file_type in files:
            path = repo_dir / rel_path
            if not path.exists() or file_type not in ["py", "js", "ts", "java", "cpp"]:
                continue

            chunks = self.extract_chunks(path)
            if not chunks:
                continue

            conn = get_connection()
            cur = conn.cursor()
            values = [
                (
                    repo_id,
                    file_id,
                    str(path),
                    file_type,
                    c["semantic_scope"],
                    c["hierarchical_context"],
                    c["content"],
                    len(c["content"].split()),
                    None,
                )
                for c in chunks
            ]
            execute_values(cur, """
                INSERT INTO repo_chunks
                (repo_id, file_id, file_path, file_type,
                 semantic_scope, hierarchical_context, content, token_count, embedding)
                VALUES %s;
            """, values)
            conn.commit()
            cur.close()
            conn.close()
            print(f"[Chunk] ✅ {path.name}: {len(chunks)} chunks inserted.")
