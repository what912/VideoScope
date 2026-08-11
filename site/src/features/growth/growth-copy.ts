import type { Locale } from "../../i18n/types";

export const PRODUCT_NAME = "VideoScope";
export const REPOSITORY_URL = "https://github.com/what912/VideoScope";
export const CREATOR_URL = "https://github.com/what912";
export const CREATOR_ATTRIBUTION = "Created by what912";

export interface GrowthCopy {
  readonly positioning: string;
  readonly sourcePreserved: string;
  readonly localBoundary: string;
  readonly caseEvidence: {
    readonly provenance: string;
    readonly source: string;
    readonly actions: string;
    readonly verification: string;
    readonly verificationStatus: string;
    readonly limitations: string;
  };
  readonly pages: {
    readonly rescue: PageCopy;
    readonly examples: PageCopy;
    readonly caseStudy: PageCopy;
    readonly download: PageCopy;
    readonly developers: PageCopy;
    readonly roadmap: PageCopy;
    readonly community: PageCopy;
    readonly missingCase: PageCopy;
  };
}

interface PageCopy {
  readonly eyebrow: string;
  readonly title: string;
  readonly description: string;
  readonly action: string;
}

export const growthCopy = {
  en: {
    positioning: "Rescue a problematic video. Export a verified, publish-ready copy.",
    sourcePreserved: "Your source stays unchanged.",
    localBoundary: "Full processing runs in the paired connector on this computer.",
    caseEvidence: {
      provenance: "Source and provenance",
      source: "Source authorization",
      actions: "Confirmed actions",
      verification: "Verification checks",
      verificationStatus: "Verification status",
      limitations: "Limitations",
    },
    pages: {
      rescue: {
        eyebrow: "VIDEO RESCUE",
        title: "Start with the result you need.",
        description: "Review observable damage, confirm a local plan, and verify a separate output copy before it is presented as publish-ready.",
        action: "Set up the local connector",
      },
      examples: {
        eyebrow: "VERIFIED EXAMPLES",
        title: "Inspect the evidence behind a completed rescue.",
        description: "Each example keeps its source, actions, verification checks, and remaining limitations explicit.",
        action: "Open case study",
      },
      caseStudy: {
        eyebrow: "CASE STUDY",
        title: "A reproducible rescue record.",
        description: "This public record shows the completed checks and any stated limitations. It does not claim a universal quality score.",
        action: "Back to examples",
      },
      download: {
        eyebrow: "LOCAL CONNECTOR",
        title: "Use the connector for full local workflows.",
        description: "The public site is an entry point. Paired local software performs full processing on this computer.",
        action: "Open connector setup",
      },
      developers: {
        eyebrow: "DEVELOPERS",
        title: "Build on inspectable local contracts.",
        description: "VideoScope keeps versioned records, observable evidence, and local-first boundaries visible to developers.",
        action: "View the source repository",
      },
      roadmap: {
        eyebrow: "ROADMAP",
        title: "Follow the work that is ready to be verified.",
        description: "Roadmap items describe planned work, not a delivery guarantee or a claim that an unavailable capability exists today.",
        action: "Read the project roadmap",
      },
      community: {
        eyebrow: "COMMUNITY",
        title: "Share feedback where the project can review it.",
        description: "Use the project repository to discuss evidence, workflows, and reproducible local improvements.",
        action: "Open GitHub",
      },
      missingCase: {
        eyebrow: "CASE NOT FOUND",
        title: "This example is not in the public manifest.",
        description: "No substitute result was shown for an unknown case-study address.",
        action: "Back to examples",
      },
    },
  },
  "zh-CN": {
    positioning: "救回存在问题的视频，导出经过验证、可供发布的新副本。",
    sourcePreserved: "源文件始终保持不变。",
    localBoundary: "完整处理在这台电脑已配对的本地连接器中运行。",
    caseEvidence: {
      provenance: "来源与出处",
      source: "来源授权",
      actions: "已确认的操作",
      verification: "验证检查",
      verificationStatus: "验证状态",
      limitations: "限制",
    },
    pages: {
      rescue: {
        eyebrow: "视频救援",
        title: "从你需要的结果开始。",
        description: "查看可观察到的损坏，确认本地计划，并在将独立输出副本标为可发布前完成验证。",
        action: "设置本地连接器",
      },
      examples: {
        eyebrow: "已验证示例",
        title: "查看一次已完成救援背后的证据。",
        description: "每个示例都会明确展示其来源、执行动作、验证检查和仍存在的限制。",
        action: "打开案例研究",
      },
      caseStudy: {
        eyebrow: "案例研究",
        title: "一份可复现的救援记录。",
        description: "这份公开记录展示已完成的检查和已声明的限制；它不会声称存在通用质量评分。",
        action: "返回示例",
      },
      download: {
        eyebrow: "本地连接器",
        title: "使用连接器运行完整本地工作流。",
        description: "公开网站是统一入口；完整处理由此电脑上已配对的本地软件完成。",
        action: "打开连接器设置",
      },
      developers: {
        eyebrow: "开发者",
        title: "基于可检查的本地契约进行构建。",
        description: "VideoScope 让版本化记录、可观察证据和 local-first 边界对开发者保持可见。",
        action: "查看源代码仓库",
      },
      roadmap: {
        eyebrow: "路线图",
        title: "关注已具备验证条件的工作。",
        description: "路线图条目描述计划中的工作，不保证交付，也不声称当前已具备尚不可用的能力。",
        action: "阅读项目路线图",
      },
      community: {
        eyebrow: "社区",
        title: "在项目可以审核的地方分享反馈。",
        description: "请通过项目仓库讨论证据、工作流和可复现的本地改进。",
        action: "打开 GitHub",
      },
      missingCase: {
        eyebrow: "未找到案例",
        title: "公开清单中没有这个示例。",
        description: "对于未知的案例地址，页面不会显示替代结果。",
        action: "返回示例",
      },
    },
  },
} as const satisfies Record<Locale, GrowthCopy>;
