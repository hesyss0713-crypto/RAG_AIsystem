import os
import re
import json
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------
# 기본 설정
# ---------------------------------------------
BRIDGE_PORT = 9013
GIT_CLONE_DIR = Path("./workspace").resolve()
GIT_CLONE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Bridge Server (React ↔ FastAPI)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------
# WebSocket 연결 (React UI용)
# ---------------------------------------------
clients: List[WebSocket] = []
clients_lock = asyncio.Lock()


async def broadcast(msg: Dict[str, Any]):
    """모든 연결된 React 클라이언트로 메시지 전송"""
    dead = []
    async with clients_lock:
        for ws in clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            clients.remove(d)


# ---------------------------------------------
# 유틸 함수
# ---------------------------------------------
def extract_github_url(text: str) -> str | None:
    """텍스트 내에서 GitHub repo URL 추출"""
    match = re.search(r"(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else None


def get_repo_structure(repo_dir: Path) -> dict:
    """repo_dir 내부를 폴더/파일 단위로 트리 구조로 반환"""
    structure = {}
    for root, dirs, files in os.walk(repo_dir):
        rel_root = Path(root).relative_to(repo_dir)
        structure[str(rel_root)] = {"dirs": dirs, "files": files}
    return structure


def save_repo_index(repo_dir: Path):
    """repo 구조를 JSON 파일로 저장"""
    structure = get_repo_structure(repo_dir)
    out_path = repo_dir / "repo_index.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)
    print(f"[Bridge] repo_index.json 저장됨 → {out_path}")


async def clone_or_update_repo_and_broadcast(url: str):
    """GitHub repo clone/pull + 로그작성 + React로 브로드캐스트"""
    repo_name = url.split("/")[-1].replace(".git", "")
    dest = GIT_CLONE_DIR / repo_name
    logs_dir = Path("./logs")
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "git_activity.log"

    try:
        # clone or pull
        if dest.exists():
            subprocess.run(["git", "-C", str(dest), "pull"], check=True)
            status = "updated"
        else:
            subprocess.run(["git", "clone", url, str(dest)], check=True)
            status = "cloned"

        save_repo_index(dest)

        # 로그 작성
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {repo_name} {status} {dest}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

        # React에 실시간 전달
        await broadcast({
            "type": "git_log",
            "text": log_line,
            "repo": repo_name,
            "status": status,
            "path": str(dest)
        })

        return {"status": status, "path": str(dest), "repo": repo_name}

    except subprocess.CalledProcessError as e:
        error_msg = str(e)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {repo_name} error {error_msg}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

        await broadcast({
            "type": "git_log",
            "text": log_line,
            "repo": repo_name,
            "status": "error",
            "error": error_msg,
        })
        return {"status": "error", "error": error_msg}


# ---------------------------------------------
# React → FastAPI (POST /send)
# ---------------------------------------------
@app.post("/send")
async def from_react(payload: Dict[str, Any] = Body(...)):
    """
    React → FastAPI 메시지 수신 엔드포인트
    """
    print(f"[Bridge] React 요청 수신: {payload}")
    text = payload.get("text", "")
    msg_type = payload.get("type", "unknown")

    # React로 브로드캐스트 (Echo)
    await broadcast({
        "type": msg_type,
        "text": f"📨 React sent: {text}",
        "direction": "received",
    })

    # GitHub URL 자동 처리
    github_url = extract_github_url(text)
    if github_url:
        await broadcast({"type": "git_status", "text": f"📦 cloning {github_url}..."})
        result = await clone_or_update_repo_and_broadcast(github_url)
        await broadcast({"type": "git_result", "data": result})
        return {"result": result}

    # 로그 저장
    logs_dir = Path("./logs")
    logs_dir.mkdir(exist_ok=True)
    with open(logs_dir / "from_react.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return {"status": "ok", "message": "React 메시지 수신 완료"}


# ---------------------------------------------
# WebSocket (React UI 실시간 통신)
# ---------------------------------------------
@app.websocket("/ws/client")
async def ws_client(ws: WebSocket):
    """React ↔ FastAPI 실시간 WebSocket 연결"""
    await ws.accept()
    async with clients_lock:
        clients.append(ws)
    await ws.send_json({"type": "system", "text": "client_connected"})
    print("[Bridge] React client connected")

    try:
        while True:
            data = await ws.receive_text()
            print(f"[Bridge] React → {data}")
    except WebSocketDisconnect:
        async with clients_lock:
            if ws in clients:
                clients.remove(ws)
        print("[Bridge] React client disconnected")


# ---------------------------------------------
# 실행
# ---------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"🚀 Bridge server running with WebSocket on port {BRIDGE_PORT}")
    uvicorn.run("bridge_server:app", host="0.0.0.0", port=BRIDGE_PORT, reload=True)

