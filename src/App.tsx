import { useEffect, useRef, useState, useCallback } from "react";

type Msg = {
  type: string;
  text?: string;
  data?: any;
  direction: "sent" | "received";
  tabId?: number;
};

export default function App() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [connected, setConnected] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const [mainInput, setMainInput] = useState("");

  // ✅ 스크롤 하단 고정
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ✅ WebSocket 연결
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://localhost:9013/ws/client`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);

    ws.onmessage = (ev) => {
      try {
        const msg: Msg = JSON.parse(ev.data);
        setMessages((prev) => [...prev, { ...msg, direction: "received" }]);
      } catch (e) {
        console.error("메시지 파싱 실패:", e);
      }
    };

    return () => ws.close();
  }, []);

  // ✅ POST 방식 전송
  const sendByPost = async (payload: { type: string; text: string }) => {
    try {
      const res = await fetch("http://localhost:9013/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await res.json().catch(() => ({}));
    } catch (err) {
      console.error("POST /send 실패:", err);
    }
  };

  // ✅ 입력 전송
  const sendMain = async () => {
    const text = mainInput.trim();
    if (!text) return;
    setMainInput("");

    // 서버로 전송
    await sendByPost({ type: "user_input", text });

    // 로컬에 표시
    setMessages((prev) => [
      ...prev,
      { type: "main_input(local)", text, direction: "sent" },
    ]);
  };

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-gradient-to-br from-gray-100 to-gray-200">
      <div className="w-full max-w-3xl h-[90%] bg-white rounded-2xl shadow-2xl flex flex-col border border-gray-100 overflow-hidden">
        {/* 헤더 */}
        <div
          className="flex items-center justify-between px-6 py-4 border-b 
              bg-gradient-to-r from-sky-400 to-sky-600 
              text-white shadow-md"
        >
          <h1 className="text-xl font-semibold">Supervisor Bridge UI</h1>
          <span
            className={`px-3 py-1 rounded-full text-sm font-medium shadow ${
              connected ? "bg-green-500 text-white" : "bg-red-400 text-white"
            }`}
          >
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>

        {/* 본문 */}
        <div className="flex-1 flex flex-col bg-gray-50">
          {/* 메시지 로그 */}
          <div className="flex-1 p-4 space-y-3 overflow-y-auto">
            {messages.map((m, i) => (
              <div
                key={i}
                className={`p-3 rounded-xl shadow-sm max-w-[75%] ${
                  m.direction === "sent"
                    ? "bg-gradient-to-r from-amber-200 to-amber-300 text-amber-900 ml-auto"
                    : "bg-amber-100 text-amber-800"
                }`}
              >
                <div className="text-xs opacity-60 mb-1">{m.type}</div>
                <pre className="text-sm whitespace-pre-wrap break-words">
                  {m.text ?? JSON.stringify(m.data ?? m)}
                </pre>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          {/* 입력창 */}
          <div className="p-3 border-t bg-white flex gap-2">
            <input
              className="flex-1 border rounded-full px-4 py-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-amber-400"
              value={mainInput}
              onChange={(e) => setMainInput(e.target.value)}
              placeholder="Type your message..."
              onKeyDown={(e) => e.key === "Enter" && sendMain()}
            />
            <button
              onClick={sendMain}
              className="px-5 py-2 rounded-full bg-gradient-to-r from-amber-500 to-orange-400 text-white font-semibold shadow hover:opacity-90 transition"
            >
              Send
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

