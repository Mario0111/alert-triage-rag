# Outbound Connection to Known-Bad or Newly-Registered Infrastructure

**Signal.** A host makes an outbound connection to an IP or domain flagged by threat intelligence, to a newly-registered domain (NRD), to a domain with high-entropy/algorithmic naming (DGA-like), or repeated beacon-like connections at regular intervals.

**Why it matters.** Outbound connections to attacker infrastructure indicate command-and-control. Regular-interval beaconing in particular is a strong C2 signature and often the link between an initial foothold and later objectives.

**ATT&CK mapping.** T1071 (Application Layer Protocol), T1071.001 (Web Protocols), T1568 (Dynamic Resolution) for DGA, T1573 (Encrypted Channel) if TLS to unknown infrastructure.

**Investigation steps.**
1. Identify the process making the connection and its full parent chain. A browser reaching an odd domain differs greatly from a freshly-dropped binary doing so.
2. Assess the destination: threat-intel reputation, domain registration age, TLS certificate details, and geolocation/ASN.
3. Analyze timing for periodicity. Regular intervals (with or without jitter) are a beaconing indicator; bursty human-driven traffic is not.
4. Measure data volume in each direction. Small regular check-ins suggest C2; large outbound transfers suggest exfiltration and route to that runbook.
5. Check whether other hosts are contacting the same destination, indicating broader compromise.

**Disposition.**
- Threat-intel-confirmed malicious destination, or clear beaconing from a suspicious process -> **High / Escalate**.
- Destination is a legitimate but uncommon SaaS/CDN confirmed benign -> **Low / Close**.
- Newly-registered or odd destination, benign-looking process, no beaconing -> **Medium / Investigate**.
