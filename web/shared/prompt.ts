const IDEA_MARKER_RE = /\{\{(?:IDEA|STORY_IDEA)\}\}/g;
const TARGET_WPM_RE = /\{\{TARGET_WPM\}\}/g;
const TARGET_WORDS_90S_RE = /\{\{TARGET_WORDS_90S\}\}/g;

export interface PromptRenderResult {
  ok: true;
  prompt: string;
  replacements: number;
}

export interface PromptRenderError {
  ok: false;
  error: string;
  status: number;
}

export function renderAgentPrompt(
  template: string | null | undefined,
  rawIdea: unknown,
  rawTargetWpm?: unknown
): PromptRenderResult | PromptRenderError {
  const idea = typeof rawIdea === "string" ? rawIdea : String(rawIdea ?? "");
  if (!idea.trim()) {
    return { ok: false, error: "Enter a story idea first.", status: 400 };
  }
  if (!template || !template.trim()) {
    return {
      ok: false,
      error: "Prompt template not configured in KV vault.",
      status: 500,
    };
  }

  const matches = template.match(IDEA_MARKER_RE);
  if (!matches || matches.length === 0) {
    return {
      ok: false,
      error:
        "Prompt template in KV vault is missing the idea placeholder ({{IDEA}} or {{STORY_IDEA}}). Refusing to generate a prompt without your story idea.",
      status: 500,
    };
  }

  const parsedWpm = Number(rawTargetWpm);
  const targetWpm =
    Number.isFinite(parsedWpm) && parsedWpm > 0 ? Math.round(parsedWpm) : 250;
  const targetWords90s = Math.round((90 / 60) * targetWpm);

  // Use function replacers so special replacement sequences ($&, $1, $$, $', $`) in the user's idea are never interpreted.
  const prompt = template
    .replace(IDEA_MARKER_RE, () => idea)
    .replace(TARGET_WPM_RE, () => String(targetWpm))
    .replace(TARGET_WORDS_90S_RE, () => String(targetWords90s));

  if (!prompt.includes(idea)) {
    return {
      ok: false,
      error: "Failed to insert story idea into prompt template.",
      status: 500,
    };
  }

  return { ok: true, prompt, replacements: matches.length };
}
