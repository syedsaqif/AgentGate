type SseFrame = { id?: string; event: string; data: string };

export async function readSse(
  body: ReadableStream<Uint8Array>, signal: AbortSignal, onFrame: (frame: SseFrame) => void,
): Promise<void> {
  signal.throwIfAborted();
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let line = "";
  let afterCr = false;
  let id: string | undefined;
  let event = "";
  let data: string[] = [];
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });

  function completeLine() {
    if (!line) {
      if (data.length) onFrame({ id, event: event || "message", data: data.join("\n") });
      id = undefined;
      event = "";
      data = [];
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "data") data.push(value);
    if (field === "event") event = value;
    if (field === "id" && !value.includes("\0")) id = value;
  }

  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done || signal.aborted) break;
      for (const character of decoder.decode(chunk.value, { stream: true })) {
        if (signal.aborted) break;
        const skipLf = afterCr && character === "\n";
        afterCr = character === "\r";
        if (skipLf) continue;
        if (character === "\r" || character === "\n") {
          completeLine();
          line = "";
        } else {
          line += character;
        }
      }
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
