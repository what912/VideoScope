import { useState } from "react";
import { contentText, type ContentLocale } from "../contentI18n";
import type {
  ContentMap,
  ContentPlan,
  ContentRangeInput,
  ContentRangeKind,
} from "../types";

const KINDS: ContentRangeKind[] = [
  "keep",
  "exclude",
  "locked_keep",
  "locked_exclude",
  "chapter",
];

export function ContentStoryboard({
  locale,
  contentMap,
  plan,
  ranges,
  selectedOrder,
  reorderAcknowledged,
  chapterTitles,
  busy,
  onRangesChange,
  onSelectedOrderChange,
  onReorderAcknowledgedChange,
  onChapterTitlesChange,
  onRevise,
  onSeek,
}: {
  locale: ContentLocale;
  contentMap: ContentMap;
  plan: ContentPlan | null;
  ranges: ContentRangeInput[];
  selectedOrder: string[];
  reorderAcknowledged: boolean;
  chapterTitles: Record<string, string>;
  busy: boolean;
  onRangesChange(value: ContentRangeInput[]): void;
  onSelectedOrderChange(value: string[]): void;
  onReorderAcknowledgedChange(value: boolean): void;
  onChapterTitlesChange(value: Record<string, string>): void;
  onRevise(): void;
  onSeek(seconds: number): void;
}): React.JSX.Element {
  const [kind, setKind] = useState<ContentRangeKind>("keep");
  const [start, setStart] = useState("0");
  const [end, setEnd] = useState("");
  const [label, setLabel] = useState("");
  const [validation, setValidation] = useState<string | null>(null);

  const add = (): void => {
    const startSeconds = Number(start);
    const endSeconds = Number(end);
    if (
      !Number.isFinite(startSeconds) ||
      !Number.isFinite(endSeconds) ||
      startSeconds < 0 ||
      endSeconds <= startSeconds ||
      endSeconds > contentMap.duration_seconds
    ) {
      setValidation(contentText("invalidRange", locale));
      return;
    }
    onRangesChange([
      ...ranges,
      {
        kind,
        start_seconds: startSeconds,
        end_seconds: endSeconds,
        label: label.trim() || null,
      },
    ]);
    setStart(String(endSeconds));
    setEnd("");
    setLabel("");
    setValidation(null);
  };

  const keepRangeIds = contentMap.user_ranges
    .filter((item) => item.kind === "keep" || item.kind === "locked_keep")
    .map((item) => item.id);
  const effectiveOrder =
    selectedOrder.length === keepRangeIds.length ? selectedOrder : keepRangeIds;
  const move = (index: number, direction: -1 | 1): void => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= effectiveOrder.length) return;
    const next = [...effectiveOrder];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onSelectedOrderChange(next);
  };

  return (
    <section className="content-panel content-storyboard" aria-labelledby="content-story-title">
      <div className="content-section-heading">
        <div>
          <p className="eyebrow">REVIEW / EXACT RANGES</p>
          <h2 id="content-story-title">{contentText("rangeEditor", locale)}</h2>
        </div>
        <p>{contentText("keyboardHelp", locale)}</p>
      </div>
      <div className="content-range-form">
        <label>
          {contentText("kind", locale)}
          <select value={kind} onChange={(event) => setKind(event.target.value as ContentRangeKind)}>
            {KINDS.map((item) => (
              <option value={item} key={item}>{contentText(item, locale)}</option>
            ))}
          </select>
        </label>
        <label>
          {contentText("startTime", locale)}
          <input type="number" min="0" max={contentMap.duration_seconds} step="0.1" value={start} onChange={(event) => setStart(event.target.value)} />
        </label>
        <label>
          {contentText("endTime", locale)}
          <input type="number" min="0" max={contentMap.duration_seconds} step="0.1" value={end} onChange={(event) => setEnd(event.target.value)} />
        </label>
        <label>
          {contentText("label", locale)}
          <input value={label} maxLength={300} onChange={(event) => setLabel(event.target.value)} />
        </label>
        <button type="button" className="secondary-button" onClick={add}>{contentText("addRange", locale)}</button>
      </div>
      {validation && <p role="alert" className="content-error">{validation}</p>}
      <ol className="content-range-list">
        {ranges.map((range, index) => (
          <li key={`${range.kind}-${range.start_seconds}-${range.end_seconds}-${index}`}>
            <button type="button" className="content-time-link" onClick={() => onSeek(range.start_seconds)}>
              {range.start_seconds.toFixed(2)}–{range.end_seconds.toFixed(2)}s
            </button>
            <span>{contentText(range.kind, locale)}</span>
            <span>{range.label}</span>
            <button type="button" onClick={() => onRangesChange(ranges.filter((_, itemIndex) => itemIndex !== index))}>
              {contentText("removeRange", locale)}
            </button>
          </li>
        ))}
      </ol>

      {keepRangeIds.length > 1 && (
        <div className="content-reorder">
          <h3>{contentText("outputOrder", locale)}</h3>
          <p>{contentText("reorderWarning", locale)}</p>
          <ol>
            {effectiveOrder.map((id, index) => {
              const range = contentMap.user_ranges.find((item) => item.id === id);
              return (
                <li key={id}>
                  <span>{range?.label ?? `${range?.source_range.start_seconds.toFixed(2)}s`}</span>
                  <button type="button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`${contentText("moveEarlier", locale)} ${index + 1}`}>↑</button>
                  <button type="button" disabled={index === effectiveOrder.length - 1} onClick={() => move(index, 1)} aria-label={`${contentText("moveLater", locale)} ${index + 1}`}>↓</button>
                </li>
              );
            })}
          </ol>
          <label>
            <input type="checkbox" checked={reorderAcknowledged} onChange={(event) => onReorderAcknowledgedChange(event.target.checked)} />
            {contentText("acknowledgeReorder", locale)}
          </label>
        </div>
      )}

      {plan?.storyboard.chapters.map((chapter) => (
        <label className="content-chapter-title" key={chapter.id}>
          {contentText("chapterTitle", locale)} {chapter.order_index + 1}
          <input
            value={chapterTitles[chapter.id] ?? chapter.title}
            maxLength={300}
            onChange={(event) => onChapterTitlesChange({ ...chapterTitles, [chapter.id]: event.target.value })}
          />
        </label>
      ))}
      <button type="button" className="primary-button" disabled={busy} onClick={onRevise}>
        {contentText("revise", locale)}
      </button>
    </section>
  );
}
