import {
  defaultGlobalFlickerConfig,
  defaultNearBlackConfig,
  defaultPossibleFreezeConfig,
  defaultSceneRelativeBlurConfig,
  type GlobalFlickerConfig,
  type NearBlackConfig,
  type PossibleFreezeConfig,
  type SceneRelativeBlurConfig,
} from "../config";
import type { BrowserDetector } from "../contracts";
import { detectGlobalFlicker } from "./global-flicker";
import { detectNearBlack } from "./near-black";
import { detectPossibleFreeze } from "./possible-freeze";
import { detectSceneRelativeBlur } from "./scene-relative-blur";

export const builtInBrowserDetectors: BrowserDetector[] = [
  {
    id: "near_black",
    version: "browser-1",
    defaultConfig: defaultNearBlackConfig,
    analyze: (context, config) =>
      detectNearBlack(
        context.samples,
        context.scenes,
        config as NearBlackConfig,
        context.locale,
      ),
  },
  {
    id: "possible_freeze",
    version: "browser-1",
    defaultConfig: defaultPossibleFreezeConfig,
    analyze: (context, config) =>
      detectPossibleFreeze(
        context.samples,
        context.scenes,
        config as PossibleFreezeConfig,
        context.locale,
      ),
  },
  {
    id: "scene_relative_blur",
    version: "browser-1",
    defaultConfig: defaultSceneRelativeBlurConfig,
    analyze: (context, config) =>
      detectSceneRelativeBlur(
        context.samples,
        context.scenes,
        config as SceneRelativeBlurConfig,
        context.locale,
      ),
  },
  {
    id: "global_flicker",
    version: "browser-1",
    defaultConfig: defaultGlobalFlickerConfig,
    analyze: (context, config) =>
      detectGlobalFlicker(
        context.samples,
        context.scenes,
        config as GlobalFlickerConfig,
        context.locale,
      ),
  },
];
