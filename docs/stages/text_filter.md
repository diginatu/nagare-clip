# text_filter — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#text_filter--text-editing-checkpoint-mandatory).

- `text_filter.provider` (default `ollama_chat`) selects the LiteLLM backend; an empty `api_base` falls back to Ollama localhost or is omitted for cloud providers. `text_filter.thinking` (default `false`) accepts `true`/`false` or a level string (`"low"/"medium"/"high"`), mapped to LiteLLM `reasoning_effort`. When `use_llm` is enabled and the orchestrator passes `summary_json` (the summary stage's `summary.json`), this video's part summaries and per-video keywords — merged after the constant `text_filter.keywords` list — are appended to the filter LLM's system prompt (`context.build_enhanced_prompt`); a missing, empty, or unreadable `summary.json` degrades to the base prompt. The stem is derived from the input filename (`{stem}.txt`).
