# LSASS Memory Access (Credential Dumping)

**Signal.** A process opens a handle to lsass.exe with read access, or a known dumping tool/behavior is observed: procdump targeting lsass, comsvcs.dll MiniDump, Mimikatz signatures, or unexpected processes reading LSASS memory.

**Why it matters.** LSASS holds credentials in memory. Accessing it is a primary credential-theft technique and almost always indicates an attacker attempting lateral movement or privilege escalation. Very few legitimate tools touch LSASS this way.

**ATT&CK mapping.** T1003.001 (OS Credential Dumping: LSASS Memory).

**Investigation steps.**
1. Identify the accessing process, its parent, and its on-disk location. Tools running from temp or user-writable paths are high suspicion.
2. Check the process signature and reputation. Unsigned or freshly-dropped binaries escalate severity sharply.
3. Determine the access mask / handle rights requested. Read access consistent with memory dumping is more concerning than benign queries.
4. Determine whether a dump file was written to disk and where; preserve it as evidence if so.
5. Review recent authentication events for the host and identify which accounts had sessions on the machine and may now be compromised.
6. Check for lateral movement following the access: new logons elsewhere using accounts that were resident on this host.

**Disposition.**
- Unknown or unsigned process accessing LSASS, especially with a dump artifact -> **Critical / Escalate** immediately; recommend host isolation and password resets for resident accounts.
- A known EDR/AV or backup product with a verified legitimate reason -> **Low / Close** after confirming via asset/software inventory.
- Legitimate-looking tool but unconfirmed business reason -> **High / Investigate**.
