# /app/bridge_server.py
import os
import re
import json
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from managers.db_manager import insert_repo_to_db
from managers.prompt_agent import LLMAgent
from managers.db_manager import get_connection


BASE_DIR = Path(__file__).parent.resolve()
GIT_CLONE_DIR = (BASE_DIR / "workspace").resolve()
GIT_CLONE_DIR.mkdir(parents=True, exist_ok=True)
BRIDGE_PORT = 9013

app = FastAPI(title="Bridge Server (React ↔ FastAPI)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

clients: List[WebSocket] = []
clients_lock = asyncio.Lock()


async def broadcast(msg: Dict[str, Any]):
    print(f"[Bridge] 📨 {msg}")
    dead = []
    async with clients_lock:
        for ws in clients:
            try:
                await ws.send_json(json.loads(json.dumps(msg, default=str)))
            except Exception:
                dead.append(ws)
        for d in dead:
            clients.remove(d)


def extract_github_url(text: str) -> str | None:
    match = re.search(r"(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else None


IGNORE_DIRS = {".git", "venv", "__pycache__", "node_modules"}


def build_dir_tree(base_path: Path, root_path: Path | None = None, max_depth: int = 5, depth: int = 0):
    if root_path is None:
        root_path = base_path
    tree = {"name": base_path.name, "path": str(base_path.relative_to(GIT_CLONE_DIR)), "type": "folder", "children": []}
    if depth > max_depth or not base_path.is_dir():
        return tree
    for entry in sorted(base_path.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
        if entry.name in IGNORE_DIRS:
            continue
        if entry.is_dir():
            tree["children"].append(build_dir_tree(entry, root_path, max_depth, depth + 1))
        else:
            tree["children"].append({"name": entry.name, "path": str(entry.relative_to(GIT_CLONE_DIR)), "type": "file"})
    return tree


async def clone_repo_and_broadcast(url: str):
    repo_name = url.split("/")[-1].replace(".git", "")
    dest = GIT_CLONE_DIR / repo_name

    if dest.exists():
        await asyncio.to_thread(subprocess.run, ["git", "-C", str(dest), "pull"], check=True)
    else:
        await asyncio.to_thread(subprocess.run, ["git", "clone", url, str(dest)], check=True)

    repo_id = await asyncio.to_thread(insert_repo_to_db, repo_name, url, dest)

    agent = LLMAgent()
    await broadcast({"type": "git_status", "text": "Summarizing files..."})
    await asyncio.to_thread(agent.summarize_repo_files, repo_id, dest)

    await broadcast({"type": "git_status", "text": "Generating chunks..."})
    await asyncio.to_thread(agent.chunk_repo_files, repo_id, dest)
    
    # ✅ symbol_links 생성 추가
    await broadcast({"type": "git_status", "text": "Extracting symbol links..."})
    await asyncio.to_thread(agent.extract_symbol_links, repo_id, dest)    
    
    await broadcast({"type": "git_status", "text": "✅ Done."})

@app.on_event("startup")
async def startup_event():
    print("========== DEBUG PATH CHECK ==========")
    print(f"[DEBUG] BASE_DIR: {BASE_DIR}")
    print(f"[DEBUG] GIT_CLONE_DIR: {GIT_CLONE_DIR}")
    print(f"[DEBUG] Exists(GIT_CLONE_DIR): {GIT_CLONE_DIR.exists()}")
    print("======================================")  

@app.post("/reset_db")
async def reset_db():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            TRUNCATE TABLE repo_meta, files_meta, repo_chunks, symbol_links
            RESTART IDENTITY CASCADE;
        """)
        conn.commit()
        cur.close()
        conn.close()
        return {"status":"ok", "message": "All tables truncated"}
    except Exception as e:
        return {"status" : "error", "message":str(e)}
        
@app.post("/send")
async def from_react(payload: Dict[str, Any] = Body(...)):
    text = payload.get("text", "")
    github_url = extract_github_url(text)

    if github_url:
        asyncio.create_task(clone_repo_and_broadcast(github_url))
        return {"status": "ok", "message": "Repository cloning and analysis started."}

    return {"status": "ok", "message": "GitHub URL not found"}


@app.websocket("/ws/client")
async def ws_client(ws: WebSocket):
    await ws.accept()
    async with clients_lock:
        clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.remove(ws)

@app.get("/init_tree")
async def get_initial_tree():
    if not GIT_CLONE_DIR.exists():
        return {"status": "error", "message": "workspace directory not found"}

    entries = [e for e in GIT_CLONE_DIR.iterdir() if e.name not in IGNORE_DIRS]
    if not entries:
        return {"status": "empty", "message": "workspace is empty"}

    trees = [build_dir_tree(e) for e in entries]
    return {"status": "ok", "trees": trees}

@app.get("/file")
async def get_file_content(path: str = Query(...)):
    target = (GIT_CLONE_DIR / path).resolve()
    if not target.exists():
        return {"status": "error", "message": f"file not found: {target}"}
    if target.is_dir():
        return {"status": "error", "message": "cannot open directory"}
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "message": f"read failed: {e}"}
    return {"status": "ok", "content": content}

@app.get("/history")
async def get_history(limit: int = 100):
    """최근 저장소 인덱싱 이력 반환"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT repo_name, repo_url, description, language, total_files, indexed_at
        FROM repo_meta
        ORDER BY indexed_at DESC
        LIMIT %s;
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    history = [
        {
            "repo_name": r[0],
            "repo_url": r[1],
            "description": r[2],
            "language": r[3],
            "total_files": r[4],
            "indexed_at": r[5],
        }
        for r in rows
    ]

    return {"status": "ok", "history": history}

if __name__ == "__main__":
    import uvicorn  
    uvicorn.run("bridge_server:app", host="0.0.0.0", port=BRIDGE_PORT, reload=False)
