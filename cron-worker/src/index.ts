import { processPublishingTick } from "../../web/shared/publisher";
import { Env } from "../../web/shared/types";

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      processPublishingTick(env).catch((err) => {
        console.error("storyforge-cron tick error:", err);
      })
    );
  },

  async fetch(): Promise<Response> {
    return Response.json({ ok: true, worker: "storyforge-cron" });
  },
};
