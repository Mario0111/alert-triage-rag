# Large or Anomalous Data Transfer (Possible Exfiltration)

**Signal.** Unusually large outbound data volume from a host or account, uploads to personal cloud storage (consumer Dropbox/Drive/Mega), large archive creation followed by upload, or database/file-share access far above the user's baseline.

**Why it matters.** This is often the final objective of an intrusion. Catching exfiltration in progress can limit breach scope; catching staging (archiving before transfer) is better still, as it precedes data loss.

**ATT&CK mapping.** T1041 (Exfiltration Over C2 Channel), T1567 (Exfiltration Over Web Service), T1567.002 (Exfiltration to Cloud Storage), T1560 (Archive Collected Data) for staging.

**Investigation steps.**
1. Identify what data was accessed, staged, or transferred, and assess its sensitivity and regulatory relevance.
2. Identify the destination and whether it is sanctioned (corporate-approved) or unsanctioned (personal/unknown).
3. Establish the user's normal baseline to judge how anomalous this volume genuinely is; some roles legitimately move large volumes.
4. Look for a staging step (archive creation, collection into a single directory) preceding the transfer, which strengthens the exfiltration hypothesis.
5. Correlate with preceding activity: was this account or host recently flagged for compromise, anomalous sign-in, or C2?
6. Determine whether the transfer is complete or in progress; an in-progress transfer may justify immediate containment to stop data loss.

**Disposition.**
- Sensitive data to an unsanctioned destination, especially following other compromise indicators -> **Critical / Escalate**; engage incident response.
- Confirmed legitimate business transfer (sanctioned backup, approved migration) -> **Low / Close**.
- Elevated volume to a gray-area destination with no other compromise signs -> **Medium / Investigate**.
