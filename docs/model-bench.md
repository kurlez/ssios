# model-bench

`model-bench` asks a local Ollama model to pick one tool for ten synthetic prompts. The prompts name Harbor Desk and example.com. They are not your mail, calendar, or files. The command does not call Google, does not create an approval, and does not write a file.

It only connects to loopback Ollama. The report is printed on the terminal.

```bash
ollama pull qwen3:14b
./scripts/ssios model-bench --model qwen3:14b
```

`qwen3:14b` is the guest default. To try Meta Muse Glimmer on Apple Silicon:

```bash
ollama pull muse-glimmer:30b-mlx
./scripts/ssios model-bench --model muse-glimmer:30b-mlx
```

That model needs Ollama >= 0.32.8 and the MLX engine. The weights are about 19GB. On a 24GB Mac, set `[agent.codex] num_ctx` to 16384 before using it as the guest. The bench warns when `/api/ps` `size` or `size_vram`, plus a 2GB guest, would leave under about 3GB free on a 24GB machine. No Muse Glimmer result is stored in this repository.

The JSON report has `valid_tool_call_rate`, `correct_tool_rate`, `argument_validity_rate`, `tool_call_as_text_count`, `median_seconds`, `max_seconds`, and `peak_memory`. A write prompt is scored only as "this tool would wait for approval". `vm-diagnose` prints the active guest model as `guest_model`.

`--vision` adds one synthetic image caption on the configured vision model, `qwen2.5vl:7b` by default. Text turns stay on the model you named, which is `qwen3:14b` for the guest. If that vision model is not installed, the report says `ollama pull qwen2.5vl:7b` and does not invent a caption. No vision measurement is stored in this repository.
