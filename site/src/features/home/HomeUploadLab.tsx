import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { LoadingState } from "../../components/feedback/LoadingState";
import { homepageMedia } from "../../data/media-manifest";
import { browserAnalysisService } from "../../services/browser-analysis";
import { importDirectMediaUrl } from "../../services/browser-analysis/url-import";
import {
  createReportStore,
  type ReportStoreResolution,
} from "../../services/report-store/report-store";
import { useI18n } from "../../i18n/I18nProvider";
import { UploadLab } from "../upload/UploadLab";
import { HomeMedia } from "./HomeMedia";

function sampleMediaUrl() {
  const media = homepageMedia.find((item) => item.role === "product-proof");
  if (!media?.video) throw new Error("Bundled sample video is unavailable");
  return media.video;
}

const defaultReportStore = createReportStore();
const resolveDefaultReportStore = () => defaultReportStore;

interface HomeUploadLabProps {
  resolveReportStore?(): Promise<ReportStoreResolution>;
}

export function HomeUploadLab({
  resolveReportStore = resolveDefaultReportStore,
}: HomeUploadLabProps = {}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [storeResolution, setStoreResolution] =
    useState<ReportStoreResolution>();

  useEffect(() => {
    let active = true;
    void resolveReportStore().then((resolution) => {
      if (active) setStoreResolution(resolution);
    });
    return () => {
      active = false;
    };
  }, [resolveReportStore]);

  return (
    <section className="home-upload">
      <HomeMedia
        className="home-upload__atmosphere"
        label={t.home.uploadAtmosphere}
        role="upload-lab"
      />
      <div className="home-upload__lab">
        {storeResolution ? (
          <UploadLab
            analysisService={browserAnalysisService}
            createObjectURL={(file) => URL.createObjectURL(file)}
            importUrl={importDirectMediaUrl}
            loadSample={async () => {
              const response = await fetch(sampleMediaUrl());
              if (!response.ok) {
                throw new Error("Bundled sample video could not be loaded");
              }
              const blob = await response.blob();
              return new File([blob], "videoscope-sample.mp4", {
                type: blob.type || "video/mp4",
              });
            }}
            navigate={navigate}
            reportStore={storeResolution.store}
            revokeObjectURL={(url) => URL.revokeObjectURL(url)}
          />
        ) : (
          <LoadingState />
        )}
      </div>
    </section>
  );
}
