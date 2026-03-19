import type { PluginContext, Hooks } from "@opencode-ai/sdk/plugin";

export default function plugin({ client, $ }: PluginContext): Hooks {
  return {
    "tool.execute.after": async (event) => {
      const toolName: string = event.tool?.name ?? "";
      if (/write|edit|patch/i.test(toolName)) {
        try {
          const result = await $`sh -c "docker compose config --services && uv run python -m py_compile src/video_editor_ai/cli.py src/video_editor_ai/stage2/morpheme.py src/video_editor_ai/stage2/speech.py src/video_editor_ai/stage2/intervals.py src/video_editor_ai/stage2/captions.py src/video_editor_ai/stage2/filler.py src/video_editor_ai/stage2/io.py && bash -n scripts/run_pipeline.sh && echo 'Validation passed.'"`;
          const out = result.stdout.toString();
          if (out) {
            await client.app.log({ level: "info", message: out });
          }
        } catch (err: unknown) {
          const e = err as { stdout?: Buffer; stderr?: Buffer; exitCode?: number };
          const msg = [
            e.stderr?.toString(),
            e.stdout?.toString(),
            `Exit code: ${e.exitCode ?? "unknown"}`,
          ]
            .filter(Boolean)
            .join("\n");
          await client.app.log({ level: "error", message: `Validation failed:\n${msg}` });
        }
      }
    },
  };
}
