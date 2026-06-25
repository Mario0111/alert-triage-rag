# Suspicious Scheduled Task or Service Creation (Persistence)

**Signal.** Creation of a new scheduled task (schtasks, Task Scheduler event 4698) or new service (sc.exe, event 7045) that runs from an unusual path, uses an encoded/scripted command, or runs as SYSTEM with a non-standard binary.

**Why it matters.** Attackers establish persistence so access survives reboots and logoffs. Tasks and services pointing at user-writable directories, temp folders, or interpreters with encoded arguments are classic persistence footholds.

**ATT&CK mapping.** T1053.005 (Scheduled Task/Job: Scheduled Task), T1543.003 (Create or Modify System Process: Windows Service).

**Investigation steps.**
1. Examine the task/service action: what binary or command does it run, from where, and as which user/context?
2. Flag execution from C:\Users\*, C:\ProgramData\*, %TEMP%, or paths with random/algorithmic names.
3. Inspect the command itself for interpreters and encoding; an encoded PowerShell payload here routes to that runbook as well.
4. Identify which account created the task/service and whether that account has a legitimate reason to do so.
5. Correlate the creation time with any preceding suspicious activity (initial access, execution) on the same host to place it in a kill-chain.
6. Check whether the same task/service name or binary appears on other hosts, indicating spread.

**Disposition.**
- Runs an interpreter or encoded command from a user-writable or temp path -> **High / Escalate**.
- Legitimate software installation or admin-deployed task confirmed via change records -> **Low / Close**.
- Unusual but not clearly malicious, no corroborating activity -> **Medium / Investigate**.
