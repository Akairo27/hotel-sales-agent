// WhatsApp's *bold* markup, for display only (owner request 2026-10-03):
// the stored message text is never changed. A message is split into plain
// and bold parts the page renders as text nodes, never as HTML.
export interface TextPart {
  text: string;
  bold: boolean;
}

// As WhatsApp reads it: an asterisk opens bold only before a non-space and
// closes it only after one, on a single line, and not inside a word or
// beside another asterisk (so «2*3*4» and «**» stay as typed).
const BOLD = /(?<![\p{L}\p{N}*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\p{L}\p{N}*])/gu;

export function splitBold(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let from = 0;
  for (const match of text.matchAll(BOLD)) {
    if (match.index > from) {
      parts.push({ text: text.slice(from, match.index), bold: false });
    }
    parts.push({ text: match[1], bold: true });
    from = match.index + match[0].length;
  }
  if (from < text.length) {
    parts.push({ text: text.slice(from), bold: false });
  }
  return parts;
}
