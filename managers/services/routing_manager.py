from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import numpy as np


def _safe_json(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


DEFAULT_SOURCE = {"source": "repo_chunks", "confidence": 0.3}
DEFAULT_INTENT = {"intent": "lookup", "confidence": 0.3}
DEFAULT_SAFETY = {"override_source": "none", "override_intent": "none", "reason": ""}
SIMILARITY_THRESHOLD = 0.65


class RoutingAggregator:
    def __init__(self, *, source_weight: float = 0.4, intent_weight: float = 0.4, self_weight: float = 0.2):
        self.source_weight = source_weight
        self.intent_weight = intent_weight
        self.self_weight = self_weight

    def __call__(
        self,
        *,
        source: Dict[str, Any],
        intent: Dict[str, Any],
        safety: Dict[str, Any],
        self_check: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        override_source = safety.get("override_source")
        override_intent = safety.get("override_intent")

        final_source = override_source if override_source and override_source != "none" else source.get("source")
        final_intent = override_intent if override_intent and override_intent != "none" else intent.get("intent")

        final_source = final_source or DEFAULT_SOURCE["source"]
        final_intent = final_intent or DEFAULT_INTENT["intent"]

        source_conf = self._safe_conf(source.get("confidence"))
        intent_conf = self._safe_conf(intent.get("confidence"))
        self_conf = self._safe_conf((self_check or {}).get("confidence"))

        weighted_sum = 0.0
        weight_total = 0.0
        for value, weight in ((source_conf, self.source_weight), (intent_conf, self.intent_weight), (self_conf, self.self_weight)):
            if value is None:
                continue
            weighted_sum += value * weight
            weight_total += weight
        routing_confidence = weighted_sum / weight_total if weight_total else 0.25
        routing_confidence = max(0.0, min(1.0, round(routing_confidence, 3)))

        notes: list[str] = []
        if override_source and override_source != "none":
            notes.append("override:source")
        if override_intent and override_intent != "none":
            notes.append("override:intent")
        if not notes:
            notes.append("auto aggregation")
        if (self_check or {}).get("freshness_need") == "yes":
            notes.append("freshness requested")

        return {
            "final_source": final_source,
            "final_intent": final_intent,
            "routing_confidence": routing_confidence,
            "notes": "; ".join(notes),
            "freshness_needed": (self_check or {}).get("freshness_need") == "yes",
        }

    @staticmethod
    def _safe_conf(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None


class RoutingManager:
    EXPLICIT_SOURCE_SIGNALS: Dict[str, tuple[str, ...]] = {
        "filesystem": ("filesystem", "local file", "파일시스템", "workspace", "/app"),
        "postgres": ("postgres", "database", "db", "sql"),
        "repo_chunks": ("repo", "코드베이스", "codebase"),
        "external_web": ("external_web", "web", "internet", "검색"),
        "direct_answer": ("direct", "chat", "general"),
    }

    CLAUSE_SPLIT_PATTERN = re.compile(r"(?:\n+|[.?!])")
    ENUMERATION_PATTERN = re.compile(r"\s*\d+\)\s*")

    READ_KEYWORDS: tuple[str, ...] = (
        "조회",
        "읽어",
        "열어",
        "내용",
        "보여줘",
        "확인",
        "살펴",
        "inspect",
        "show",
        "display",
        "list",
        "어디",
        "무엇",
    )
    MODIFY_KEYWORDS: tuple[str, ...] = (
        "수정",
        "변경",
        "바꿔",
        "바꾸",
        "바뀌",
        "교체",
        "덮어",
        "추가",
        "삭제",
        "적용",
        "업데이트",
        "갱신",
        "refactor",
        "replace",
        "update",
        "modify",
        "edit",
        "patch",
        "fix",
        "convert",
        "conversion",
        "변환",
        "옮겨",
        "이식",
    )

    EXPLICIT_INTENT_SIGNALS: Dict[str, tuple[str, ...]] = {
        "lookup": ("lookup", "찾아", "search"),
        "read": ("read", "열어", "내용"),
        "summarize": ("summarize", "요약"),
        "explain": ("explain", "설명"),
        "schema_view": ("schema", "컬럼"),
    }

    def __init__(
        self,
        *,
        run_llm_call,
        tool_definitions: list[Dict[str, Any]],
        embedder,
        aggregator: RoutingAggregator | None = None,
        fallback_router: FallbackRouter | None = None,
        confidence_gate: float = 0.4,
    ):
        self.run_llm_call = run_llm_call
        self.tool_definitions = tool_definitions
        self.embedder = embedder
        self.aggregator = aggregator or RoutingAggregator()
        self.fallback_router = fallback_router or FallbackRouter()
        self.confidence_gate = confidence_gate
        self.routing_state: Dict[str, Dict[str, Any]] = {}

    async def route(self, user_text: str, self_check: Dict[str, Any] | None, state_key: str | None = None) -> Dict[str, Any]:
        state = self._get_state(state_key)
        query_embedding = self._embed_query(user_text)

        source = await self._run_source_router(user_text, state, query_embedding)
        intent = await self._run_intent_router(user_text, state, query_embedding)
        safety = self._run_safety_router(user_text, state, query_embedding)

        routing_context = self.aggregator(source=source, intent=intent, safety=safety, self_check=self_check)
        action_plan = self._build_action_plan(user_text)
        routing_context["action_plan"] = action_plan

        function_router_choice = await self._run_function_router(routing_context)

        fallback_choice = None
        final_call = function_router_choice if self._has_valid_function(function_router_choice) else None
        if not final_call:
            fallback_choice = self.fallback_router.select(user_text, routing_context)
            final_call = fallback_choice

        self._update_routing_state(state_key, routing_context, user_text, query_embedding)

        return {
            "source": source,
            "intent": intent,
            "safety": safety,
            "self_check": self_check,
            "routing_context": routing_context,
            "function_router": function_router_choice,
            "fallback_function": fallback_choice,
            "final_function": final_call,
            "function_call": final_call,
            "action_plan": action_plan,
        }

    async def _run_source_router(self, user_text: str, state: Dict[str, Any], query_embedding: Optional[np.ndarray]) -> Dict[str, Any]:
        explicit = self._detect_explicit_signal(user_text, self.EXPLICIT_SOURCE_SIGNALS)
        if explicit:
            return {"source": explicit, "confidence": 0.95, "reason": "explicit signal"}

        similarity = self._similarity(query_embedding, state.get("last_query_embedding"))
        if similarity is not None and similarity >= SIMILARITY_THRESHOLD and state.get("last_source"):
            return {"source": state["last_source"], "confidence": 0.8, "reason": "state continuity"}

        return await self._call_router(user_text, "source_router") or DEFAULT_SOURCE

    async def _run_intent_router(self, user_text: str, state: Dict[str, Any], query_embedding: Optional[np.ndarray]) -> Dict[str, Any]:
        explicit = self._detect_explicit_signal(user_text, self.EXPLICIT_INTENT_SIGNALS)
        if explicit:
            return {"intent": explicit, "confidence": 0.95, "reason": "explicit signal"}

        similarity = self._similarity(query_embedding, state.get("last_query_embedding"))
        if similarity is not None and similarity >= SIMILARITY_THRESHOLD:
            last_source = state.get("last_source")
            if last_source == "postgres":
                return {"intent": "lookup", "confidence": 0.8, "reason": "postgres continuity"}
            if last_source in {"filesystem", "local file"}:
                return {"intent": "read", "confidence": 0.8, "reason": "filesystem continuity"}

        return await self._call_router(user_text, "intent_router") or DEFAULT_INTENT

    def _run_safety_router(self, user_text: str, state: Dict[str, Any], query_embedding: Optional[np.ndarray]) -> Dict[str, Any]:
        explicit = self._detect_explicit_signal(user_text, self.EXPLICIT_SOURCE_SIGNALS)
        if explicit:
            return {
                "override_source": explicit,
                "override_intent": "none",
                "reason": "explicit source signal",
            }

        similarity = self._similarity(query_embedding, state.get("last_query_embedding"))
        if similarity is not None and similarity >= SIMILARITY_THRESHOLD and state.get("last_source"):
            return {
                "override_source": state.get("last_source", "none"),
                "override_intent": state.get("last_intent", "none"),
                "reason": "state continuity",
            }

        return DEFAULT_SAFETY

    async def _call_router(self, payload: str, task: str) -> Dict[str, Any] | None:
        raw = await self.run_llm_call(payload, task=task, max_new_tokens=256)
        return _safe_json(raw)

    async def _run_function_router(self, routing_context: Dict[str, Any]) -> Dict[str, Any]:
        confidence = routing_context.get("routing_confidence", 0.0)
        if confidence < self.confidence_gate:
            return {
                "name": "none",
                "arguments": {},
                "confidence": confidence,
                "reason": "routing_confidence below threshold",
            }
        payload = json.dumps({"routing_context": routing_context, "functions": self.tool_definitions}, ensure_ascii=False)
        result = await self._call_router(payload, "function_router") or {}
        return result

    def _has_valid_function(self, function_call: Dict[str, Any] | None) -> bool:
        if not function_call:
            return False
        name = (function_call.get("name") or "").strip().lower()
        return bool(name and name != "none")

    def _embed_query(self, text: str) -> Optional[np.ndarray]:
        if not self.embedder or not text:
            return None
        vector = self.embedder.embed_text(text, command="query")
        return vector

    @staticmethod
    def _similarity(vec_a: Optional[np.ndarray], vec_b: Optional[np.ndarray]) -> Optional[float]:
        if vec_a is None or vec_b is None:
            return None
        if not vec_a.any() or not vec_b.any():
            return None
        score = float(np.dot(vec_a, vec_b))
        return max(-1.0, min(1.0, score))

    def _detect_explicit_signal(self, text: str, vocab: Dict[str, tuple[str, ...]]) -> str | None:
        if not text:
            return None
        lowered = text.lower()
        for label, keywords in vocab.items():
            for keyword in keywords:
                if keyword.lower() in lowered:
                    return label
        return None

    def _get_state(self, key: str | None) -> Dict[str, Any]:
        state_key = key or "__default__"
        return self.routing_state.setdefault(state_key, {})

    def _update_routing_state(
        self,
        key: str | None,
        routing_context: Dict[str, Any],
        user_text: str,
        query_embedding: Optional[np.ndarray],
    ) -> None:
        state = self._get_state(key)
        state["last_source"] = routing_context.get("final_source")
        state["last_intent"] = routing_context.get("final_intent")
        state["last_user_query"] = user_text
        state["last_query_embedding"] = query_embedding

    def _build_action_plan(self, user_text: str) -> Dict[str, Any]:
        analysis = self._analyze_action_signals(user_text)
        mode = analysis["mode"]
        steps: list[dict[str, str]] = []
        llm_guidance = "요청에 맞춰 기본 응답만 생성하세요."
        reason = analysis.get("reason", "명확한 시그널 없음")

        if mode == "read_then_modify":
            steps = [
                {
                    "id": "read_context",
                    "title": "맥락 조회",
                    "description": "관련 파일이나 코드 블록을 도구로 열어 현재 상태를 파악합니다.",
                },
                {
                    "id": "llm_modify",
                    "title": "LLM 수정",
                    "description": "조회한 내용을 바탕으로 LLM이 요청한 수정 사항(코드/설명/patch)을 생성합니다.",
                },
            ]
            llm_guidance = (
                "도구로 읽어온 결과를 그대로 전달하지 말고, 사용자 요청에 따라 수정된 결과물을 LLM으로 작성하세요."
            )
        elif mode == "read_only":
            steps = [
                {
                    "id": "read_context",
                    "title": "정보 조회",
                    "description": "필요한 파일이나 데이터를 열람해 사용자 질문에 필요한 정보를 수집합니다.",
                }
            ]
            llm_guidance = "조회한 내용을 기반으로 설명이나 요약만 제공하면 됩니다."

        return {
            "mode": mode,
            "reason": reason,
            "signals": analysis["signals"],
            "clauses": analysis.get("clauses"),
            "needs_llm_transformation": mode == "read_then_modify",
            "steps": steps,
            "llm_guidance": llm_guidance,
        }

    def _analyze_action_signals(self, user_text: str | None) -> Dict[str, Any]:
        normalized = self._normalize_action_text(user_text)
        clauses = self._split_action_clauses(normalized)
        clause_details: list[dict[str, Any]] = []
        read_hits: set[str] = set()
        modify_hits: set[str] = set()

        for clause in clauses:
            clause_read = self._match_keywords(clause, self.READ_KEYWORDS)
            clause_modify = self._match_keywords(clause, self.MODIFY_KEYWORDS)
            if clause_read or clause_modify:
                clause_details.append(
                    {
                        "clause": clause,
                        "read_signals": clause_read,
                        "modify_signals": clause_modify,
                    }
                )
            read_hits.update(clause_read)
            modify_hits.update(clause_modify)

        if modify_hits:
            mode = "read_then_modify"
            reason = self._build_clause_reason(clause_details, prefer="modify")
        elif read_hits:
            mode = "read_only"
            reason = self._build_clause_reason(clause_details, prefer="read")
        else:
            mode = "unknown"
            reason = "no strong signals"

        return {
            "mode": mode,
            "signals": {"read": sorted(read_hits), "modify": sorted(modify_hits)},
            "clauses": clause_details,
            "needs_llm_transformation": mode == "read_then_modify",
            "reason": reason,
        }

    def _normalize_action_text(self, text: str | None) -> str:
        if not text:
            return ""
        lowered = text.lower()
        lowered = lowered.replace("->", " ")
        lowered = self.ENUMERATION_PATTERN.sub(" ", lowered)
        lowered = re.sub(r"\s+", " ", lowered)
        return lowered.strip()

    def _split_action_clauses(self, text: str) -> list[str]:
        if not text:
            return []
        raw = self.CLAUSE_SPLIT_PATTERN.split(text)
        clauses: list[str] = []
        for segment in raw:
            seg = segment.strip()
            if not seg:
                continue
            seg = re.sub(r"\b(그러면|그럼|그리고|그런데)\b", "", seg).strip()
            if seg:
                clauses.append(seg)
        return clauses or [text]

    @staticmethod
    def _match_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
        hits: list[str] = []
        for kw in keywords:
            if kw and kw in text:
                hits.append(kw)
        return hits

    @staticmethod
    def _build_clause_reason(clause_details: list[dict[str, Any]], prefer: str) -> str:
        for detail in clause_details:
            signals = detail["modify_signals" if prefer == "modify" else "read_signals"]
            if signals:
                sample = detail["clause"]
                if len(sample) > 60:
                    sample = sample[:57] + "..."
                return f"{prefer} signal {signals} in '{sample}'"
        return f"{prefer} signals detected"


class FallbackRouter:
    FILE_TOKEN_PATTERN = None

    def __init__(self):
        import re

        self.FILE_TOKEN_PATTERN = re.compile(r"([A-Za-z0-9_\-./]+?\.[A-Za-z0-9_.-]+)")

    def select(self, user_text: str, routing_context: Dict[str, Any]) -> Dict[str, Any]:
        final_source = (routing_context.get("final_source") or "").lower()
        final_intent = (routing_context.get("final_intent") or "").lower()
        confidence = routing_context.get("routing_confidence", 0.2)

        if final_source in {"filesystem", "local file"}:
            reason = "fallback filesystem mapping"
            if final_intent in {"read", "check_contents", "summarize"}:
                path = self._extract_primary_path(user_text)
                if path:
                    return {
                        "name": "read_file",
                        "arguments": {"path": path},
                        "confidence": confidence,
                        "reason": reason,
                    }
            keyword = self._fallback_keyword(user_text)
            return {
                "name": "search_file",
                "arguments": {"keyword": keyword, "max_results": 20},
                "confidence": confidence,
                "reason": reason,
            }

        if final_source == "repo_chunks":
            tool_name = "rag_search_chunks"
            if final_intent in {"file_metadata", "search_file"}:
                tool_name = "rag_search_files"
            elif final_intent == "symbol_graph":
                tool_name = "rag_search_symbols"
            return {
                "name": tool_name,
                "arguments": {"query": user_text, "top_k": 8},
                "confidence": confidence,
                "reason": "fallback repo_chunks mapping",
            }

        if final_source == "postgres":
            table = self._detect_known_table(user_text)
            if final_intent == "schema_view":
                return {
                    "name": "inspect_table_columns",
                    "arguments": {"table": table or "repo_meta"},
                    "confidence": confidence,
                    "reason": "fallback postgres schema_view",
                }
            query = self._extract_select_query(user_text)
            if not query:
                table = table or "repo_meta"
                query = f"SELECT * FROM {table} LIMIT 25"
            return {
                "name": "connect_db",
                "arguments": {"query": query},
                "confidence": confidence,
                "reason": "fallback postgres query",
            }

        if final_source == "external_web":
            return {
                "name": "search_web",
                "arguments": {"query": user_text},
                "confidence": confidence,
                "reason": "fallback external_web",
            }

        return {
            "name": "answer_direct",
            "arguments": {},
            "confidence": confidence,
            "reason": "fallback direct answer",
        }

    def _extract_primary_path(self, text: str) -> str | None:
        if not text:
            return None
        match = self.FILE_TOKEN_PATTERN.search(text)
        if match:
            return match.group(1).lstrip("./")
        return None

    def _fallback_keyword(self, text: str) -> str:
        import re

        tokens = re.findall(r"[A-Za-z0-9_./-]+", text or "")
        for token in tokens:
            if "." in token:
                return token
        return tokens[0] if tokens else "README"

    def _detect_known_table(self, text: str) -> str | None:
        lowered = (text or "").lower()
        for table in ("repo_meta", "repo_chunks", "files_meta", "symbol_links"):
            if table in lowered:
                return table
        import re

        match = re.search(r"from\s+([A-Za-z_][\w]*)", text or "", flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _extract_select_query(self, text: str) -> str | None:
        if not text:
            return None
        import re

        pattern = re.compile(r"(select\s.+?)(?:;|$)", re.IGNORECASE | re.DOTALL)
        match = pattern.search(text)
        if not match:
            return None
        query = match.group(1).strip()
        if not query.lower().startswith("select"):
            return None
        if "limit" not in query.lower():
            query = f"{query} LIMIT 50"
        return query


MultiSignalRouter = RoutingManager

__all__ = ["RoutingManager", "MultiSignalRouter", "RoutingAggregator", "FallbackRouter"]
