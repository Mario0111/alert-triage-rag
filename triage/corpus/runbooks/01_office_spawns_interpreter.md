# Office Application Spawning a Command Interpreter

**Signal.** A Microsoft Office process (WINWORD.EXE, EXCEL.EXE, POWERPNT.EXE, OUTLOOK.EXE) spawns a child process that is a command or scripting interpreter: powershell.exe, cmd.exe, wscript.exe, cscript.exe, mshta.exe, or rundll32.exe.

**Why it matters.** Office applications have no legitimate reason to launch interpreters during normal document use. This parent-child relationship is one of the highest-fidelity indicators of malicious-document (maldoc) execution, typically the first stage of a phishing-delivered intrusion.

**ATT&CK mapping.** T1566.001 (Phishing: Spearphishing Attachment) as the likely delivery, T1204.002 (User Execution: Malicious File) as the trigger, T1059.001 (PowerShell) or T1059.003 (Windows Command Shell) as the execution.

**Investigation steps.**
1. Confirm the full process tree. Do not trust the alert's labeling of the parent-child relationship; verify it directly. A reported "Word -> PowerShell" may actually be "Word -> cmd.exe -> PowerShell," and the intermediate hop changes interpretation.
2. Walk the chain above Office. Identify what spawned the Office application itself. Outlook -> Word strongly supports a phishing-delivery hypothesis (attachment opened from email) and raises severity; a user manually opening a saved document is weaker.
3. Capture the full child command line. Look for encoding or download behavior: `-enc`, `-EncodedCommand`, base64 blobs, `IEX`, `Invoke-WebRequest`, `DownloadString`, `bitsadmin`, `certutil -urlcache`.
4. Identify the originating document and its delivery path (email attachment, web download, USB) and locate it on disk for potential sandbox analysis.
5. Check for outbound network connections from the child process to unfamiliar or recently-registered domains/IPs.
6. Determine whether the command executed successfully or was blocked by EDR/AMSI, and whether any child artifacts (dropped files, new processes) resulted.

**Disposition.**
- Encoded command, download behavior, or outbound C2-like connection present -> **High / Escalate**.
- Delivery chain traces to an emailed attachment opened from Outlook -> escalate severity even if the command line is not yet decoded.
- Office spawned an interpreter but command line is benign (e.g., a known internal macro tool) and confirmed expected -> **Low / Close** with documented reasoning.
- Ambiguous command line, no clear malicious indicator, but pattern is anomalous for the host -> **Medium / Investigate**.
