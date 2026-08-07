import type { RescueSymptom } from "../types";
import { rescueSymptomText, rescueText, type RescueLocale } from "../rescueI18n";

const SYMPTOMS: RescueSymptom[] = ["unplayable", "timeline_discontinuity", "missing_audio", "audio_video_offset", "dark", "video_noise", "soft_detail", "flicker", "shake", "low_loudness", "audio_noise", "audio_clipping"];
export function RescueSymptomSelector({ locale, value, onChange }: { locale: RescueLocale; value: RescueSymptom[]; onChange(value: RescueSymptom[]): void }): React.JSX.Element {
  return <fieldset className="rescue-symptoms"><legend>{rescueText("symptoms", locale)}</legend>{SYMPTOMS.map((symptom) => <label key={symptom}><input type="checkbox" checked={value.includes(symptom)} onChange={() => onChange(value.includes(symptom) ? value.filter((item) => item !== symptom) : [...value, symptom])} />{rescueSymptomText(symptom, locale)}</label>)}</fieldset>;
}
