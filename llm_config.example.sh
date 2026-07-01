#!/usr/bin/env bash
# LLM 配置（复制为 llm_config.sh 并填写真实值）
# run_internal.sh 与 start_web_internal.sh 都会 source 此文件；
# /admin/run-second 也会读取 llm_config.sh。

# export LLM_MODEL="glm-5.1"
# export LLM_BASE_URL="https://your-llm-proxy.example.com"
# export LLM_API_KEY="sk-your-api-key"
# export LLM_MAX_TOKENS="4096"
# export LLM_CONCURRENCY="4"

export LLM_BASE_URL="https://open.bigmodel.cn/api/coding/paas/v4"
export LLM_MODEL="glm-5.2"
export LLM_API_KEY="your-api-key"
