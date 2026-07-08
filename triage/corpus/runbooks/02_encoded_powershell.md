# Encoded or Obfuscated PowerShell Execution

**Signal.** A PowerShell command line containing obfuscation or encoding markers: `-EncodedCommand`, `-enc`, `-e`, base64 strings, `FromBase64String`, string concatenation/reordering, `-WindowStyle Hidden`, `-NoProfile -NonInteractive`, or `-ExecutionPolicy Bypass`.

**Why it matters.** Legitimate administrative PowerShell is rarely encoded or hidden. These flags are overwhelmingly associated with adversary tradecraft attempting to evade logging and human inspection.

**ATT&CK mapping.** T1059.001 (PowerShell), T1027 (Obfuscated Files or Information), T1140 (Deobfuscate/Decode Files or Information).

**Investigation steps.**
1. Identify the parent process and full process tree. An interpreter spawned by Office, a browser, or an unusual service raises severity and may route to a more specific runbook.
2. Decode the base64 payload in an isolated environment (CyberChef, or `[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String("..."))`). Note that some payloads are multi-layered; decode until you reach readable intent.
3. Inspect the decoded content for download cradles, C2 addresses, credential access, persistence commands, or further obfuscation.
4. Check whether script-block logging (event 4104) or AMSI captured the deobfuscated content, and whether execution was allowed or blocked.
5. Check whether the same encoded pattern appears across multiple hosts, which would indicate a campaign rather than an isolated event.

**Disposition.**
- Decoded payload shows download, C2, credential, or persistence intent -> **High / Escalate**.
- Decoded payload is a known benign automation script run by a legitimate scheduled task or admin tool -> **Low / Close**.
- Cannot safely decode or intent unclear -> **Medium / Investigate** and request EDR isolation if the host is sensitive.
