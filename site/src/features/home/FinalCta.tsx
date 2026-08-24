import { Link } from "react-router";

import type { PageCopy } from "../growth/growth-copy-runtime";

export function FinalCta({ copy }: { copy: PageCopy }) {
  return (
    <section className="final-cta">
      <div>
        <p className="eyebrow">{copy.eyebrow}</p>
        <h2>{copy.title}</h2>
        <p>{copy.description}</p>
      </div>
      <Link className="button button--quiet" to="/download">
        {copy.action}
      </Link>
    </section>
  );
}
