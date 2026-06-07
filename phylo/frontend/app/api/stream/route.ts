import { NextRequest } from "next/server";
import { createClient } from "redis";

export const dynamic = "force-dynamic"; // never cache; long-lived SSE

// Server-Sent Events bridge: Redis pub/sub ("phylo:events") -> browser EventSource.
// This is the channel that makes the 3D phylogeny grow in real time as evolution runs.
export async function GET(req: NextRequest) {
  const url = process.env.REDIS_URL;
  if (!url) return new Response("REDIS_URL not set", { status: 500 });

  const sub = createClient({ url });
  await sub.connect();

  const stream = new ReadableStream({
    async start(controller) {
      const enc = new TextEncoder();
      const send = (data: string) => {
        try { controller.enqueue(enc.encode(`data: ${data}\n\n`)); } catch {}
      };
      send(JSON.stringify({ type: "hello" }));
      await sub.subscribe("phylo:events", (message) => send(message));
      const ping = setInterval(() => { try { controller.enqueue(enc.encode(": ping\n\n")); } catch {} }, 15000);
      req.signal.addEventListener("abort", async () => {
        clearInterval(ping);
        try { await sub.unsubscribe("phylo:events"); await sub.quit(); } catch {}
        try { controller.close(); } catch {}
      });
    },
  });

  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform", Connection: "keep-alive" },
  });
}
