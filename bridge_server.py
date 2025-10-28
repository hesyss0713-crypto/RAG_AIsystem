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
# 설정
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
# WebSocket 연결 관리
# ---------------------------------------------
clients: List[WebSocket] = []
clients_lock = asyncio.Lock()

async def broadcast(msg: Dict[str, Any]):
    """모든 연결된 클라이언트로 메시지 전송"""
    print(f"[Bridge] 📨 broadcast 시도: {msg}")
    dead = []
    async with clients_lock:
        for ws in clients:
            try:
                safe_msg = json.loads(json.dumps(msg, default=str))
                await ws.send_json(safe_msg)
                print("[Bridge] ✅ broadcast 성공")
            except Exception as e:
                print(f"[Bridge] ❌ broadcast 오류: {e}")
                dead.append(ws)
        for d in dead:
            clients.remove(d)

# ---------------------------------------------
# 유틸 함수
# ---------------------------------------------
def extract_github_url(text: str) -> str | None:
    """텍스트에서 GitHub repo URL 추출"""
    match = re.search(r"(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else None

# ---------------------------------------------
# GitHub clone 처리
# ---------------------------------------------
async def clone_repo_and_broadcast(url: str):
    """GitHub repo clone 수행 + React에 상태 전달"""
    repo_name = url.split("/")[-1].replace(".git", "")
    dest = GIT_CLONE_DIR / repo_name
    logs_dir = Path("./logs")
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / "git_activity.log"

    await broadcast({"type": "git_status", "text": f"📦 cloning {url}..."})

    try:
        if dest.exists():
            # 기존 repo가 있으면 pull
            await asyncio.to_thread(subprocess.run, ["git", "-C", str(dest), "pull"], check=True)
        else:
            # 새 repo clone
            await asyncio.to_thread(subprocess.run, ["git", "clone", url, str(dest)], check=True)

        # 로그 작성
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {repo_name} success {dest}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

        # ✅ 성공 메시지 전송
        await broadcast({"type": "git_result", "text": f"✅ success ({repo_name})"})
        print(f"[Bridge] ✅ clone success: {repo_name}")
        return {"status": "success", "repo": repo_name}

    except subprocess.CalledProcessError as e:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {repo_name} fail {e}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

        # ❌ 실패 메시지 전송
        await broadcast({"type": "git_result", "text": f"❌ fail ({repo_name})"})
        print(f"[Bridge] ❌ clone fail: {repo_name}")
        return {"status": "fail", "repo": repo_name, "error": str(e)}

# ---------------------------------------------
# React → FastAPI
# ---------------------------------------------
@app.post("/send")
async def from_react(payload: Dict[str, Any] = Body(...)):
    """React에서 전송된 메시지 수신"""
    print(f"[Bridge] React 요청 수신: {payload}")
    text = payload.get("text", "")
    msg_type = payload.get("type", "unknown")

    # 메시지 로그 출력
    await broadcast({
        "type": msg_type,
        "text": f"📨 React sent: {text}",
        "direction": "received"
    })

    # GitHub URL 감지 시 clone 수행
    github_url = extract_github_url(text)
    if github_url:
        result = await clone_repo_and_broadcast(github_url)
        return result

    return {"status": "ok", "message": "GitHub URL 없음"}

# ---------------------------------------------
# WebSocket (React ↔ FastAPI)
# ---------------------------------------------
@app.websocket("/ws/client")
async def ws_client(ws: WebSocket):
    """React ↔ FastAPI 실시간 통신"""
    await ws.accept()
    async with clients_lock:
        clients.append(ws)
    await ws.send_json({"type": "system", "text": "client_connected"})
    print(f"[Bridge] ✅ WebSocket 연결됨. 현재 연결 수: {len(clients)}")

    try:
        while True:
            data = await ws.receive_text()
            print(f"[Bridge] React → {data}")
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

