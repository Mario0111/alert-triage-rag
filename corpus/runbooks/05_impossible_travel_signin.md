# Impossible Travel / Anomalous Cloud Sign-In

**Signal.** Successful authentication for one user account from two geographically distant locations within a time window that makes physical travel impossible, or a sign-in from a new country/ASN/device that is unusual for the user. Often paired with MFA prompts or legacy-auth usage.

**Why it matters.** Indicates likely credential compromise or session/token theft. A valid login from an attacker is harder to detect than malware and is a common path to business email compromise and data theft.

**ATT&CK mapping.** T1078 (Valid Accounts), T1110 (Brute Force) if preceded by failed attempts, T1539 (Steal Web Session Cookie) if MFA was satisfied without a fresh prompt.

**Investigation steps.**
1. Confirm both sign-ins are genuinely the same account and not a shared service identity or service principal that legitimately authenticates from many locations.
2. Check how MFA was satisfied: push approved, hardware token, or no prompt at all. No fresh prompt on a new location suggests session/token theft rather than a password compromise.
3. Examine the source of the suspicious sign-in: IP reputation, ASN, whether it is a known VPN/hosting provider or anonymizer.
4. Review post-login activity for the session: inbox/forwarding rule creation, mass file access or download, OAuth application grants, MFA method changes, password changes.
5. Verify against known-travel and VPN context; corporate VPN egress points and roaming users cause benign false positives.

**Disposition.**
- Confirmed impossible travel plus suspicious post-login activity (new inbox rules, mass access, OAuth grants) -> **Critical / Escalate**; recommend session revocation and password reset.
- Explained by VPN, corporate egress, or confirmed user travel -> **Low / Close**.
- Anomalous geo/device but no malicious post-login behavior yet -> **Medium / Investigate**; verify directly with the user.
