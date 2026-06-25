# Multiple Failed Logins Followed by Success (Brute Force / Password Spray)

**Signal.** A burst of failed authentication events (event 4625, or cloud sign-in failures) against one account (brute force) or many accounts (password spray), followed by a successful authentication.

**Why it matters.** A success immediately after a failure burst suggests the attacker guessed or sprayed a valid credential. Distinguishing brute force (one account, many passwords) from spray (many accounts, few passwords each) shapes both severity and response scope.

**ATT&CK mapping.** T1110.001 (Password Guessing), T1110.003 (Password Spraying), T1078 (Valid Accounts) once a login succeeds.

**Investigation steps.**
1. Characterize the pattern: one target account or many? From a single source IP or distributed across many?
2. Determine whether any authentication actually succeeded, and against which account(s).
3. For any success, confirm the source and whether MFA was enforced and satisfied; an MFA-satisfied success is far less likely to be the attacker.
4. Review post-success activity on the affected account for signs of misuse.
5. Check whether the source IP/ASN appears across other accounts, hosts, or recent alerts, indicating a broader campaign.

**Disposition.**
- Failed burst followed by a success with no MFA and suspicious post-login activity -> **High / Escalate**; reset credentials and revoke sessions.
- Failures only, no success, account locked by policy -> **Low / Close** (record as attempted, monitor the source).
- Success after failures but MFA satisfied and activity consistent with the legitimate user mistyping their password -> **Medium / Investigate**; verify with the user.
