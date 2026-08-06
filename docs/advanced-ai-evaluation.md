# Advanced AI evaluation protocol

Synthetic and Fake Provider tests prove contracts, grounding enforcement,
determinism and failure isolation. They do **not** establish real-video quality.

## Dataset construction

1. Obtain videos with documented consent and a retention policy.
2. Separate development and held-out test sets by source/session, not by clips
   cut from the same recording.
3. Have at least two reviewers independently mark chapter and highlight ranges.
4. Record task goal, audience, language and allowed omissions.
5. Resolve disagreements without showing model suggestions to the adjudicator.
6. Keep verbatim transcripts and frames in a private evaluation root.

The public manifest should contain only pseudonymous IDs, durations, language
categories, consent status, reference version and hashes. Do not commit private
media, transcripts, personal paths or reviewer identities.

## Automated range metrics

`videoscope.intelligence.evaluate_grounded_ranges` reports chapter and highlight
metrics separately: temporal IoU; one-to-one event precision, recall and F1 at
an explicit IoU threshold; reference-duration coverage; and mean best temporal
IoU. Summary and title drafts use the rubric below. VideoScope never averages
these measures into a global quality, usefulness or popularity score.

## Human rubric

Rate each independently from 1 (unusable) to 5 (strong) and retain written
evidence:

| Capability | Review question |
| --- | --- |
| Transcript fidelity | Does the text preserve spoken meaning without invented facts? |
| Grounding | Can every claim be verified from cited source time/cues? |
| Chapter coverage | Do boundaries organize the complete recording for the task? |
| Highlight usefulness | Would the interval stand alone for the declared goal? |
| Summary faithfulness | Does it avoid unsupported claims and contradictions? |
| Title faithfulness | Is it specific without claims absent from the source? |
| Editing effort | How much correction was needed? Record minutes, not promised savings. |

Record binary safety failures separately: invented identity, unsupported fact,
private-data exposure, out-of-range evidence, and any change applied without
review. A safety failure remains visible even when another field scores well.

## Reproducibility record

Retain VideoScope version, provider/model ID, model revision when available,
device/precision, parameters, prompt-contract version, input/transcript hashes,
batch and review digests, and elapsed time. Publish results only with sample
count, task/language composition, uncertainty, failures and exclusions. Never
present Fake Provider or synthetic fixture results as real-world accuracy.

