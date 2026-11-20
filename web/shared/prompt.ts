const IDEA_MARKER_RE = /\{\{(?:IDEA|STORY_IDEA)\}\}/g;

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
  rawTargetWpm?: unknown,
  rawFormat?: unknown
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
  const formats = {single: 'FORMAT: single video (exactly one part)', two_part: 'FORMAT: two parts (exactly two parts)', auto: 'FORMAT: Auto (you decide using the rule below)'};
  const format = rawFormat === 'single' || rawFormat === 'two_part' ? rawFormat : 'auto';
  const hasFormat = template.includes('{{FORMAT}}');
  const text = template.replace(/\{\{(IDEA|STORY_IDEA|TARGET_WPM|TARGET_WORDS_90S|FORMAT)\}\}/g, (_, key) => {
    if (key === 'FORMAT') return formats[format];
    if (key === 'TARGET_WPM') return String(targetWpm);
    if (key === 'TARGET_WORDS_90S') return String(targetWords90s);
    return idea;
  });
  const prompt = hasFormat ? text : formats[format] + '\n\n' + text;

  if (!prompt.includes(idea)) {
    return {
      ok: false,
      error: "Failed to insert story idea into prompt template.",
      status: 500,
    };
  }

  return { ok: true, prompt, replacements: matches.length };
}
