# router-bench

`router-bench` scores synthetic Chinese and English prompts. The prompts are not your mail, calendar, or files. Rules run first in the product. A small local model classifies only the turns the rules do not decide. It does not answer, and it does not call a tool.

CI runs the rules only:

```bash
PYTHONPATH=src python -m ssios.cli router-bench --rules-only
```

On a Mac, after the two small models are installed:

```bash
ollama pull qwen3:1.7b
ollama pull qwen3:4b
./scripts/ssios router-bench
```

That prints rules accuracy, a per-route confusion table, and median and max latency, then the same figures for each classifier. The chat model stays `qwen3:14b`. A story or an explanation never goes to the guest. Set `[llm] router_model` to change the model used during chat. The default is `qwen3:1.7b`, and the timeout is 4 seconds. A timeout or a low score stays on local chat.
