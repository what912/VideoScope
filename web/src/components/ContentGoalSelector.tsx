import { contentText, type ContentLocale } from "../contentI18n";
import type { ContentGoal } from "../types";

const GOALS: ContentGoal[] = [
  "faithful_clean",
  "chaptered_full",
  "selected_clips",
];

export function ContentGoalSelector({
  locale,
  value,
  onChange,
}: {
  locale: ContentLocale;
  value: ContentGoal;
  onChange(goal: ContentGoal): void;
}): React.JSX.Element {
  return (
    <fieldset className="content-goals">
      <legend>{contentText("goal", locale)}</legend>
      {GOALS.map((goal) => (
        <label key={goal} className={value === goal ? "is-selected" : ""}>
          <input
            type="radio"
            name="content-goal"
            checked={value === goal}
            onChange={() => onChange(goal)}
          />
          <strong>{contentText(goal, locale)}</strong>
          <span>{contentText(`${goal}_help`, locale)}</span>
        </label>
      ))}
    </fieldset>
  );
}
