"""LLM 接入：任何 OpenAI 兼容端点都能用。"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


def build_llm() -> ChatOpenAI:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if not (api_key and base_url and model):
        raise RuntimeError("LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 未配置，请检查 .env")

    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        timeout=120,
        max_retries=2,
    )
