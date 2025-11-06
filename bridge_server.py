import os
import re
import json
import asyncio
import subprocess
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from managers.db_manager import insert_repo_to_db  
from managers.summary_agent import generate_file_summaries

# ---------------------------------------------
# 기본 설정
# ---------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
GIT_CLONE_DIR = (BASE_DIR / "workspace").resolve()
GIT_CLONE_DIR.mkdir(parents=True, exist_ok=True)

if GIT_CLONE_DIR in (Path("/"), Path("/root"), Path("/home")):
    raise RuntimeError(f"❌ GIT_CLONE_DIR 경로({GIT_CLONE_DIR})가 안전하지 않습니다.")

BRIDGE_PORT = 9013

# ---------------------------------------------
# FastAPI 초기화
# ---------------------------------------------
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

# ---------------------------------------------
# Broadcast 유틸
# ---------------------------------------------
async def broadcast(msg: Dict[str, Any]):
    print(f"[Bridge] 📨 broadcast 시도: {msg}")
    dead = []
    async with clients_lock:
        for ws in clients:
            try:
                await ws.send_json(json.loads(json.dumps(msg, default=str)))
            except Exception as e:
                print(f"[Bridge] ❌ broadcast 오류: {e}")
                dead.append(ws)
        for d in dead:
            if d in clients:
                clients.remove(d)

# ---------------------------------------------
# GitHub URL 추출
# ---------------------------------------------
def extract_github_url(text: str) -> str | None:
    match = re.search(r"(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else None

IGNORE_DIRS = {".git", "venv", "node_modules", "__pycache__"}

# ---------------------------------------------
# 폴더 트리 생성
# (GIT_CLONE_DIR 기준으로 path 생성하도록 수정)
# ---------------------------------------------
def build_dir_tree(base_path: Path, root_path: Path | None = None, max_depth: int = 5, depth: int = 0) -> Dict[str, Any]:
    if root_path is None:
        root_path = base_path

    base_real = base_path.resolve()
    root_real = GIT_CLONE_DIR.resolve()  # ✅ 수정 포인트: 항상 workspace 기준으로 상대 경로 계산

    try:
        base_real.relative_to(root_real)
    except Exception:
        return {"name": base_path.name, "path": "", "type": "error", "children": []}

    # ✅ repo_name 포함한 전체 상대경로
    rel_str = str(base_real.relative_to(root_real))

    tree = {"name": base_path.name, "path": rel_str, "type": "folder", "children": []}

    if depth > max_depth or not base_path.is_dir():
        return tree

    for entry in sorted(base_path.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
        if entry.name in IGNORE_DIRS or entry.is_symlink():
            continue
        try:
            entry_real = entry.resolve()
            entry_real.relative_to(root_real)
        except Exception:
            continue
        if entry.is_dir():
            tree["children"].append(build_dir_tree(entry, root_real, max_depth, depth + 1))
        else:
            tree["children"].append({
                "name": entry.name,
                "path": str(entry_real.relative_to(root_real)),  # ✅ 전체 상대경로 유지
                "type": "file"
            })
    return tree


# ---------------------------------------------
# Git clone + DB 등록 + broadcast
# ---------------------------------------------
async def clone_repo_and_broadcast(url: str):
    repo_name = url.split("/")[-1].replace(".git", "")
    dest = GIT_CLONE_DIR / repo_name
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "git_activity.log"

    await broadcast({"type": "git_status", "text": f"📦 cloning {url}..."})
    try:
        # ✅ 1. git clone or pull
        if dest.exists():
            await asyncio.to_thread(subprocess.run, ["git", "-C", str(dest), "pull"], check=True)
        else:
            await asyncio.to_thread(subprocess.run, ["git", "clone", url, str(dest)], check=True)

        # ✅ 2. DB 등록
        repo_id = await asyncio.to_thread(insert_repo_to_db, repo_name, url, dest)

        # ✅ 3. 파일 summary 생성 (LLM)
        await broadcast({"type": "git_status", "text": "🧠 Generating file summaries with LLM..."})
        await asyncio.to_thread(generate_file_summaries, repo_id, dest)

        # ✅ 4. 디렉터리 트리 생성
        tree = await asyncio.to_thread(build_dir_tree, dest)

        # ✅ 5. 결과 전송
        await broadcast({"type": "git_result", "text": f"✅ success ({repo_name})"})
        await broadcast({"type": "dir_tree", "data": tree})

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {repo_name} success {dest}\n")

        return {"status": "success", "repo": repo_name, "tree": tree}

    except subprocess.CalledProcessError as e:
        await broadcast({"type": "git_result", "text": f"❌ fail ({repo_name})"})
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {repo_name} fail {e}\n")
        return {"status": "fail", "repo": repo_name, "error": str(e)}

# ---------------------------------------------
# React → FastAPI
# ---------------------------------------------
@app.post("/send")
async def from_react(payload: Dict[str, Any] = Body(...)):
    print(f"[Bridge] React 요청 수신: {payload}")
    text = payload.get("text", "")
    msg_type = payload.get("type", "unknown")

    await broadcast({"type": msg_type, "text": f"📨 React sent: {text}", "direction": "received"})
    github_url = extract_github_url(text)
    if github_url:
        return await clone_repo_and_broadcast(github_url)
    return {"status": "ok", "message": "GitHub URL 없음"}

# ---------------------------------------------
# 파일 내용 조회 API
# ---------------------------------------------
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


@app.get("/init_tree")
async def get_initial_tree():
    if not GIT_CLONE_DIR.exists():
        return {"status": "error", "message": "workspace directory not found"}

    entries = [e for e in GIT_CLONE_DIR.iterdir() if e.name not in IGNORE_DIRS]
    if not entries:
        return {"status": "empty", "message": "workspace is empty"}

    trees = [build_dir_tree(e) for e in entries]
    return {"status": "ok", "trees": trees}

# ---------------------------------------------
# WebSocket
# ---------------------------------------------
@app.websocket("/ws/client")
async def ws_client(ws: WebSocket):
    await ws.accept()
    async with clients_lock:
        clients.append(ws)
    await ws.send_json({"type": "system", "text": "client_connected"})
    print(f"[Bridge] ✅ WebSocket 연결됨 ({len(clients)} clients)")
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        async with clients_lock:
            if ws in clients:
                clients.remove(ws)
        print("[Bridge] ⚠️ WebSocket 연결 해제됨")

# ---------------------------------------------
# 실행
# ---------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"🚀 Bridge server running on port {BRIDGE_PORT}")
    uvicorn.run("bridge_server:app", host="0.0.0.0", port=BRIDGE_PORT, reload=False)