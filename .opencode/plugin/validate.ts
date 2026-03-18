import type { PluginContext, Hooks } from "@opencode-ai/sdk/plugin";

export default function plugin({ client, $ }: PluginContext): Hooks {
  return {
    "tool.execute.after": async (event) => {
      const toolName: string = event.tool?.name ?? "";
      if (/write|edit|patch/i.test(toolName)) {
        try {
          const result = await $`sh -c "docker compose config --services && python -m py_compile stage2_intervals.py stage3_blender.py && bash -n run_pipeline.sh && echo 'Validation passed.'"`;
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
