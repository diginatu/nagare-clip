# text_filter — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#text_filter--text-editing-checkpoint-mandatory).

- `text_filter.provider` (default `ollama_chat`) selects the LiteLLM backend; an empty `api_base` falls back to Ollama localhost or is omitted for cloud providers. `text_filter.thinking` (default `false`) accepts `true`/`false` or a level string (`"low"/"medium"/"high"`), mapped to LiteLLM `reasoning_effort`. The optional **summary LLM** (`text_filter.summary_llm.enabled`) runs first, sending the full transcript to a (possibly different) LLM that returns `{summary, keywords}`; these are appended to the filter LLM's system prompt to help correct mis-dictated rare words, and fall back gracefully on failure.
