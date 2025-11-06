# /app/managers/chunk_agent.py
from pathlib import Path
import json
from psycopg2.extras import execute_values
from managers.db_manager import get_connection
from managers.llm_manager import LLMManager


import json
import re
from pathlib import Path
from managers.llm_manager import LLMManager


def extract_chunks_from_code(file_path: Path):
    """LLM으로 코드 의미 단위 분리 (안정형 버전)"""
    llm = LLMManager()
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")[:4000]  # 너무 큰 파일 방지
    except Exception as e:
        print(f"[Chunk] ❌ 파일 읽기 실패: {file_path} ({e})")
        return []

    prompt = f"""
You are a code analysis assistant.
Divide the given source code into logical semantic chunks.

For each chunk, output a JSON array.
Each element MUST strictly follow this JSON schema, with no explanations or extra text:

[
  {{
    "semantic_scope": "brief description of the logical purpose of the chunk",
    "hierarchical_context": "function/class/module hierarchy (e.g. 'model → forward() → loss calculation')",
    "content": "exact code snippet of the chunk"
  }},
  ...
]

Output ONLY the JSON array and NOTHING else.
If you cannot extract chunks, output [].

File: {file_path.name}
Code:
{text}
"""

    res = llm.generate(prompt, max_new_tokens=2048)
    print(f"[Chunk:DEBUG] prompt length={len(prompt)}, code length={len(text)}")
    print(f"\n[Chunk:RAW_OUTPUT] {file_path} ↓↓↓\n{res[:2000]}\n---END---\n")
    if not res:
        print(f"[Chunk] ⚠️ Empty response for {file_path}")
        return []

    # ✅ JSON 부분만 안전하게 추출
    match = re.search(r"(\[.*\])", res, re.DOTALL)
    if not match:
        print(f"[Chunk] ⚠️ No JSON block found for {file_path}")
        return []

    json_str = match.group(1)

    # ✅ 작은따옴표를 큰따옴표로 교체 + 불필요 문자 제거
    json_str = (
        json_str
        .replace("'", '"')
        .replace("\\n", "\n")
        .replace("\x00", "")
        .strip()
    )

    # ✅ JSON 안전 파싱
    try:
        chunks = json.loads(json_str)
        # ✅ 최소 구조 검증
        valid_chunks = []
        for c in chunks:
            if isinstance(c, dict) and "content" in c:
                valid_chunks.append({
                    "semantic_scope": c.get("semantic_scope", "").strip(),
                    "hierarchical_context": c.get("hierarchical_context", "").strip(),
                    "content": c["content"].strip()
                })
        return valid_chunks
    except Exception as e:
        print(f"[Chunk] ⚠️ JSON parsing failed for {file_path} ({e})")
        return []

def insert_chunks_to_db(repo_id: int, file_id: int, file_path: Path, file_type: str, chunks: list):
    """repo_chunks 테이블에 chunk 삽입"""
    conn = get_connection()
    cur = conn.cursor()

    values = []
    for chunk in chunks:
        content = chunk.get("content", "")
        token_count = len(content.split())
        values.append((
            repo_id,
            file_id,
            str(file_path),
            file_type,
            chunk.get("semantic_scope", ""),
            chunk.get("hierarchical_context", ""),
            content,
            token_count,
            None
        ))

    execute_values(cur, """
        INSERT INTO repo_chunks
        (repo_id, file_id, file_path, file_type,
         semantic_scope, hierarchical_context, content, token_count, embedding)
        VALUES %s;
    """, values)
    conn.commit()
    cur.close()
    conn.close()


def generate_chunks_for_repo(repo_id: int, repo_dir: Path):
    """해당 repo의 모든 코드 파일을 semantic chunk로 분리"""
    print(f"[Chunk] 🧩 Generating chunks for repo {repo_id}")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, file_path, file_type FROM files_meta WHERE repo_id = %s;", (repo_id,))
    files = cur.fetchall()
    cur.close()
    conn.close()

    for file_id, rel_path, file_type in files:
        path = repo_dir / rel_path
        if not path.exists():
            continue
        if file_type not in ["py", "js", "ts", "java", "cpp", "md"]:
            continue

        chunks = extract_chunks_from_code(path)
        if chunks:
            insert_chunks_to_db(repo_id, file_id, path, file_type, chunks)
            print(f"[Chunk] ✅ {path.name}: {len(chunks)} chunks inserted.")
