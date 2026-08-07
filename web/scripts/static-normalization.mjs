export function normalizeStaticIndexMarkup(markup) {
  const lines = markup.replace(/\r\n?/g, "\n").split("\n");
  return `${lines.filter((line) => line.trim().length > 0).join("\n").trimEnd()}\n`;
}
