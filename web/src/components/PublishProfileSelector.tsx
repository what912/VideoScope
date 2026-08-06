import type { PublishProfile, PublishProfileId } from "../types";

export interface ProfileSelectorCopy {
  legend: string;
  compatible: string;
  compatibleDescription: string;
  vertical: string;
  horizontal: string;
  scalePadDescription: string;
  canvas: string;
  preserve: string;
}

interface Props {
  profiles: PublishProfile[];
  selected: PublishProfileId;
  copy: ProfileSelectorCopy;
  disabled?: boolean;
  onChange: (profileId: PublishProfileId) => void;
}

const PROFILE_COPY_KEYS: Record<
  PublishProfileId,
  { name: keyof ProfileSelectorCopy; description: keyof ProfileSelectorCopy }
> = {
  compatible_mp4: {
    name: "compatible",
    description: "compatibleDescription",
  },
  social_vertical_9_16: {
    name: "vertical",
    description: "scalePadDescription",
  },
  social_horizontal_16_9: {
    name: "horizontal",
    description: "scalePadDescription",
  },
};

export function PublishProfileSelector({
  profiles,
  selected,
  copy,
  disabled = false,
  onChange,
}: Props): React.JSX.Element {
  return (
    <fieldset className="publish-profile-fieldset" disabled={disabled}>
      <legend>{copy.legend}</legend>
      <div className="publish-profile-grid">
        {profiles.map((profile) => {
          const keys = PROFILE_COPY_KEYS[profile.id];
          const canvas =
            profile.width && profile.height
              ? `${profile.width} × ${profile.height}`
              : copy.preserve;
          return (
            <label
              className={`publish-profile-card ${
                selected === profile.id ? "is-selected" : ""
              }`}
              key={profile.id}
            >
              <input
                type="radio"
                name="publish-profile"
                value={profile.id}
                checked={selected === profile.id}
                onChange={() => onChange(profile.id)}
              />
              <span className="profile-card-copy">
                <strong>{copy[keys.name]}</strong>
                <small>{copy[keys.description]}</small>
              </span>
              <span className="profile-spec">
                <span>{copy.canvas}</span>
                <strong>{canvas}</strong>
                <small>
                  {profile.video_codec.toUpperCase()} · {profile.container.toUpperCase()}
                </small>
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
