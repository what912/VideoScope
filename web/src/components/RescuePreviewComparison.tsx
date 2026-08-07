import { useRef } from "react";
import { rescueText, type RescueLocale } from "../rescueI18n";
export function RescuePreviewComparison({ locale, originalUrl, faithfulUrl, improvedUrl }: { locale: RescueLocale; originalUrl?: string | null; faithfulUrl?: string | null; improvedUrl?: string | null }): React.JSX.Element {
  const refs = useRef<Array<HTMLVideoElement | null>>([]); const sync = (time: number) => refs.current.forEach((video) => { if (video && Math.abs(video.currentTime - time) > 0.15) video.currentTime = time; });
  const items = [[rescueText("original", locale), originalUrl], [rescueText("faithful", locale), faithfulUrl], [rescueText("improved", locale), improvedUrl]].filter(([, url]) => Boolean(url)) as Array<[string, string]>;
  return <section className="rescue-previews"><h2>{rescueText("previews", locale)}</h2>{items.length > 0 && <div className="rescue-preview-grid">{items.map(([label, url], index) => <figure key={label}><figcaption>{label}</figcaption><video aria-label={label} ref={(element) => { refs.current[index] = element; }} controls preload="metadata" src={url} onTimeUpdate={(event) => sync(event.currentTarget.currentTime)} /></figure>)}</div>}{!improvedUrl && <p>{rescueText("unsupported", locale)}</p>}</section>;
}
