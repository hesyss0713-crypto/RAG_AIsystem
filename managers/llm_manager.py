import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


class LLMManager:
    def __init__(self, model_name="Qwen/Qwen3-1.7B"):
        self.model_name = model_name
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype="auto",
            device_map="auto"
        )
        print("[LLM] ✅ Model loaded successfully")

    # --------------------------------------------------------------
    # 요약 전용 generate 함수
    # --------------------------------------------------------------
    def generate(self, prompt: str, max_new_tokens=512) -> str:
        """summary 전용 LLM 호출 (junk-free, structured output)"""
        messages = [{"role": "user", "content": prompt}]
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=True
        )

        inputs = self.tokenizer([text], return_tensors="pt").to(self.model.device)
        outputs = self.model.generate(**inputs, max_new_tokens=max_new_tokens)

        gen_ids = outputs[0][len(inputs.input_ids[0]):]
        result = self.tokenizer.decode(gen_ids, skip_special_tokens=True)

        # ---------- [1] thinking 제거 ----------
        if "</think>" in result:
            result = result.split("</think>")[-1]

        # ---------- [2] <summary> 블록만 추출 ----------
        if "<summary>" in result and "</summary>" in result:
            result = result.split("<summary>")[-1].split("</summary>")[0]

        # ---------- [3] 개행 정리 ----------
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        if lines:
            result = lines[-1]

        # ---------- [4] NULL 문자 제거 ----------
        result = result.replace("\x00", "").replace("\u0000", "").strip()

        return result or "(요약 실패)"
