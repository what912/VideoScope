import { useI18n } from "../i18n/I18nProvider";

type PageProps = {
  description: string;
  title: string;
};

function ProductPage({ description, title }: PageProps) {
  return (
    <section aria-labelledby="page-title" className="product-page">
      <h1 id="page-title">{title}</h1>
      <p>{description}</p>
    </section>
  );
}

export function HomePage() {
  const { t } = useI18n();
  return (
    <ProductPage
      description={t.pages.home.description}
      title={t.pages.home.title}
    />
  );
}

export function WorkspacePage() {
  const { t } = useI18n();
  return (
    <ProductPage
      description={t.pages.workspace.description}
      title={t.pages.workspace.title}
    />
  );
}

export function ComparePage() {
  const { t } = useI18n();
  return (
    <ProductPage
      description={t.pages.compare.description}
      title={t.pages.compare.title}
    />
  );
}

export function ReportPage() {
  const { t } = useI18n();
  return (
    <ProductPage
      description={t.pages.report.description}
      title={t.pages.report.title}
    />
  );
}
