// Server-sent events over fetch streaming (EventSource cannot send the Authorization header).
// Resumes after the last seen id (`Last-Event-ID`), reconnects with backoff 1 s -> 30 s, and
// stops on abort or when the server ends the stream for a terminal task (m10a §2).

import { API_BASE, authHeaders } from "./api";

export interface StreamEvent {
  seq: number;
  type: string;
  data: Record<string, unknown>;
}

export interface StreamOptions {
  onEvent: (event: StreamEvent) => void;
  onEnd: () => void;
  onError?: (status: number) => void;
}

/** Parse SSE text; returns the events and the unparsed tail. Comments (`: ping`) are skipped. */
export function parseSse(buffer: string): { events: StreamEvent[]; rest: string } {
  const events: StreamEvent[] = [];
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let id: string | null = null;
    let type = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id") id = value;
      else if (field === "event") type = value;
      else if (field === "data") data.push(value);
    }
    if (id === null || data.length === 0) continue;
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
    } catch {
      payload = { raw: data.join("\n") };
    }
    events.push({ seq: Number(id), type, data: payload });
  }
  return { events, rest };
}

export function streamEvents(taskId: string, opts: StreamOptions): () => void {
  const controller = new AbortController();
  let lastSeq = -1;
  let backoff = 1000;

  const run = async (): Promise<void> => {
    while (!controller.signal.aborted) {
      let ended = false;
      try {
        const headers: Record<string, string> = { ...authHeaders(), Accept: "text/event-stream" };
        if (lastSeq >= 0) headers["Last-Event-ID"] = String(lastSeq);
        const resp = await fetch(`${API_BASE}/v1/tasks/${taskId}/events`, {
          headers,
          signal: controller.signal,
        });
        if (!resp.ok || !resp.body) {
          opts.onError?.(resp.status);
          if (resp.status === 401 || resp.status === 403 || resp.status === 404) return;
        } else {
          backoff = 1000;
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const parsed = parseSse(buffer);
            buffer = parsed.rest;
            for (const event of parsed.events) {
              if (event.seq <= lastSeq) continue;
              lastSeq = event.seq;
              opts.onEvent(event);
            }
          }
          ended = true;
        }
      } catch {
        if (controller.signal.aborted) return;
      }
      if (ended) {
        opts.onEnd(); // the server closes the stream only once the task is terminal
        return;
      }
      await new Promise((r) => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 30_000);
    }
  };
  void run();
  return () => controller.abort();
}
