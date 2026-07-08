# Default Handling for Low-Signal or Ambiguous Alerts

**Signal.** An alert that does not clearly match a higher-fidelity runbook: a single anomaly score, an isolated informational event, or activity with no corroborating context.

**Why it matters.** Most SOC volume is low-signal. Over-escalating these burns analyst time; auto-closing them blindly creates risk. The goal is a disciplined, documented default rather than a guess.

**ATT&CK mapping.** Map only if telemetry supports a specific technique. If the available fields are insufficient to identify a technique, do not assign one; state that explicitly rather than guessing.

**Investigation steps.**
1. Determine whether sufficient telemetry exists to reach any confident conclusion at all.
2. Check for any single corroborating signal that would promote this alert to one of the specific runbooks above.
3. Compare the activity against the host and user baseline to judge how unusual it really is.
4. Consider the sensitivity of any asset or account involved; the same weak signal warrants more attention on a crown-jewel system than on a low-value endpoint.

**Disposition.**
- No corroboration and fits a known benign baseline -> **Low / Close** with documented reasoning.
- Insufficient telemetry to decide, but the activity touches a sensitive asset -> **Medium / Investigate** to gather more data.
- Never assign a fabricated technique ID or severity to satisfy the output schema. If confidence is low, the verdict must say so explicitly rather than inventing specificity.
