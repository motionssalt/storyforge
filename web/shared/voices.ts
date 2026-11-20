import voicesManifest from "../public/voices.json";

export interface BuiltinVoiceStudioVoice {
  id: string;
  name: string;
  descriptor: string;
  instruct: string;
  seed: number;
  gender?: string;
  age?: string;
  pitch?: string;
  accent?: string;
}

export interface BuiltinEdgeVoice {
  id: string;
  name: string;
  gender: string;
  accent: string;
  description?: string;
  base_wpm?: number;
  default_rate?: string;
}

export const BUILTIN_EDGE_VOICES: BuiltinEdgeVoice[] = (voicesManifest as any).voices || [];
export const BUILTIN_VOICESTUDIO_VOICES: BuiltinVoiceStudioVoice[] =
  (voicesManifest as any).voicestudio_voices || [];

export const BUILTIN_VOICESTUDIO_MAP = new Map<string, BuiltinVoiceStudioVoice>(
  BUILTIN_VOICESTUDIO_VOICES.map((v) => [v.id, v])
);

export const VALID_CLONE_SLOTS = new Set([
  "base",
  "calm",
  "tense",
  "angry",
  "shocked",
  "sad",
  "sarcastic",
  "excited",
]);

const INSTRUCT_CATEGORIES: Array<{ name: string; values: Set<string> }> = [
  { name: "gender", values: new Set(["male", "female"]) },
  { name: "age", values: new Set(["child", "teenager", "young adult", "middle-aged", "elderly"]) },
  {
    name: "pitch",
    values: new Set(["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"]),
  },
  { name: "style", values: new Set(["whisper"]) },
  {
    name: "accent",
    values: new Set([
      "american accent",
      "british accent",
      "australian accent",
      "canadian accent",
      "indian accent",
      "chinese accent",
      "korean accent",
      "japanese accent",
      "portuguese accent",
      "russian accent",
    ]),
  },
];

const ALL_VALID_INSTRUCT_ITEMS = new Set(
  INSTRUCT_CATEGORIES.flatMap((c) => Array.from(c.values))
);

export function validateOmnivoiceInstruct(
  raw: string
): { ok: true; normalized: string } | { ok: false; error: string } {
  const items = String(raw || "")
    .split(/[,，]/)
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);

  if (items.length === 0) {
    return {
      ok: false,
      error: "Enter at least one attribute (e.g. female, young adult, low pitch, american accent).",
    };
  }

  const bad = items.filter((item) => !ALL_VALID_INSTRUCT_ITEMS.has(item));
  if (bad.length > 0) {
    return {
      ok: false,
      error:
        `Unsupported attribute(s): ${bad.join(", ")}. ` +
        `Allowed: gender (male/female), age (child/teenager/young adult/middle-aged/elderly — omit for standard adult), ` +
        `pitch (very low pitch/low pitch/moderate pitch/high pitch/very high pitch), ` +
        `accent (american accent/british accent/australian accent/canadian accent/indian accent/etc.), style (whisper).`,
    };
  }

  for (const cat of INSTRUCT_CATEGORIES) {
    const hits = items.filter((x) => cat.values.has(x));
    if (hits.length > 1) {
      return {
        ok: false,
        error: `Conflicting ${cat.name} attributes (${hits.join(" vs ")}). Choose at most one ${cat.name}.`,
      };
    }
  }

  return { ok: true, normalized: items.join(", ") };
}

const KV_CHUNK_SIZE = 4 * 1024 * 1024; // 4 MiB chunks (well below KV's 25 MiB cap)

export async function putKvBinary(
  kv: KVNamespace,
  key: string,
  bytes: ArrayBuffer | Uint8Array,
  extraMeta: Record<string, any> = {}
): Promise<void> {
  await deleteKvBinary(kv, key);
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (view.byteLength <= KV_CHUNK_SIZE) {
    await kv.put(key, view, { metadata: { chunked: false, size_bytes: view.byteLength, ...extraMeta } });
    return;
  }
  const chunks = Math.ceil(view.byteLength / KV_CHUNK_SIZE);
  for (let i = 0; i < chunks; i++) {
    const slice = view.subarray(i * KV_CHUNK_SIZE, Math.min(view.byteLength, (i + 1) * KV_CHUNK_SIZE));
    await kv.put(`${key}:c:${i}`, slice);
  }
  await kv.put(
    key,
    JSON.stringify({ chunked: true, chunks, size_bytes: view.byteLength, ...extraMeta }),
    { metadata: { chunked: true, chunks, size_bytes: view.byteLength, ...extraMeta } }
  );
}

export async function getKvBinary(
  kv: KVNamespace,
  key: string
): Promise<{ data: Uint8Array; meta: Record<string, any> } | null> {
  const { value, metadata } = await kv.getWithMetadata<Record<string, any>>(key, "arrayBuffer");
  if (!value) return null;
  if (!metadata?.chunked) {
    return { data: new Uint8Array(value), meta: metadata || {} };
  }
  const count = Number(metadata.chunks || 0);
  const parts: Uint8Array[] = [];
  let total = 0;
  for (let i = 0; i < count; i++) {
    const buf = await kv.get(`${key}:c:${i}`, "arrayBuffer");
    if (!buf) return null;
    const u8 = new Uint8Array(buf);
    parts.push(u8);
    total += u8.byteLength;
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    joined.set(p, offset);
    offset += p.byteLength;
  }
  return { data: joined, meta: metadata || {} };
}

export async function deleteKvBinary(kv: KVNamespace, key: string): Promise<void> {
  try {
    const { metadata } = await kv.getWithMetadata<Record<string, any>>(key, "text");
    if (metadata?.chunked && Number(metadata.chunks) > 0) {
      for (let i = 0; i < Number(metadata.chunks); i++) {
        await kv.delete(`${key}:c:${i}`);
      }
    }
  } catch {
    /* ignore */
  }
  await kv.delete(key);
}
