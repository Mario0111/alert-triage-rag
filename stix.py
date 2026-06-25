"""Parse MITRE ATT&CK Enterprise STIX/JSON into flat technique records.

This module is pure parsing/extraction glue. It turns the STIX bundle into
simple `Technique` records that `chunk.py` later groups into retrievable chunks
(one chunk per technique). Keeping parsing separate from chunking means the
author can own the chunking strategy without touching STIX details.

Detection note: in recent ATT&CK (v17+), detection content no longer lives on
the technique as an ``x_mitre_detection`` string. It is split across
``x-mitre-detection-strategy`` objects (linked to techniques by a ``detects``
relationship) and ``x-mitre-analytic`` objects (the concrete analytic logic and
log sources). This parser walks that chain and assembles a single readable
detection block per technique, so the downstream chunk shape is unchanged.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Technique:
    """One MITRE ATT&CK technique, flattened from the STIX bundle.

    Attributes:
        attack_id: The ATT&CK technique id, e.g. ``T1059`` or ``T1059.001``.
        name: Human-readable technique name.
        description: The technique's prose description (may be long).
        detection: Detection guidance assembled from the technique's linked
            detection-strategy and analytic objects (strategy name, analytic
            descriptions, platforms, and log sources). Empty string if ATT&CK
            provides no detection strategy for this technique.
    """

    attack_id: str
    name: str
    description: str
    detection: str


def _attack_id(external_references: list[dict]) -> str | None:
    """Return the ATT&CK external id from a STIX object's external references.

    The ATT&CK id lives in the external reference whose ``source_name`` is
    ``mitre-attack``. Returns ``None`` if no such reference exists.
    """
    for ref in external_references:
        if ref.get("source_name") == "mitre-attack":
            return ref.get("external_id")
    return None


def _detection_index(
    objects: list[dict], by_id: dict[str, dict]
) -> dict[str, list[dict]]:
    """Map each technique's STIX id to its detection-strategy objects.

    ATT&CK links a ``x-mitre-detection-strategy`` to a technique with a
    ``relationship`` of type ``detects`` (source = strategy, target = technique).
    Deprecated/revoked strategies are skipped.

    Args:
        objects: All objects in the STIX bundle.
        by_id: Index of objects by their STIX ``id``.

    Returns:
        Mapping of ``attack-pattern`` STIX id -> list of detection-strategy
        objects detecting it.
    """
    index: dict[str, list[dict]] = defaultdict(list)
    for obj in objects:
        if obj.get("type") != "relationship":
            continue
        if obj.get("relationship_type") != "detects":
            continue
        strategy = by_id.get(obj.get("source_ref", ""))
        if strategy is None or strategy.get("type") != "x-mitre-detection-strategy":
            continue
        if strategy.get("revoked") or strategy.get("x_mitre_deprecated"):
            continue
        index[obj.get("target_ref", "")].append(strategy)
    return index


def _format_log_sources(references: list[dict]) -> str:
    """Render an analytic's inline log-source references as readable text.

    Each reference is an inline dict such as
    ``{"name": "WinEventLog:Sysmon", "channel": "EventCode=1", ...}``.
    Duplicates are collapsed while preserving order.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for ref in references:
        name = (ref.get("name") or "").strip()
        if not name:
            continue
        channel = (ref.get("channel") or "").strip()
        rendered = f"{name} ({channel})" if channel else name
        if rendered not in seen:
            seen.add(rendered)
            parts.append(rendered)
    return "; ".join(parts)


def _format_detection(strategies: list[dict], by_id: dict[str, dict]) -> str:
    """Assemble a readable detection block from a technique's strategies.

    For each detection strategy: its name, then each non-deprecated analytic's
    name, platforms, description, and log sources. The result is the text that
    goes into the technique chunk's detection portion.

    Args:
        strategies: Detection-strategy objects for one technique.
        by_id: Index of objects by STIX ``id`` (to resolve analytic refs).

    Returns:
        A detection block as plain text, or ``""`` if nothing usable is found.
    """
    lines: list[str] = []
    for strategy in strategies:
        name = (strategy.get("name") or "").strip()
        if name:
            lines.append(f"Detection strategy: {name}")

        for analytic_ref in strategy.get("x_mitre_analytic_refs", []):
            analytic = by_id.get(analytic_ref)
            if analytic is None or analytic.get("x_mitre_deprecated"):
                continue

            analytic_name = (analytic.get("name") or "").strip()
            platforms = analytic.get("x_mitre_platforms") or []
            header = f"Analytic: {analytic_name}" if analytic_name else "Analytic:"
            if platforms:
                header += f" [{', '.join(platforms)}]"
            lines.append(header)

            description = (analytic.get("description") or "").strip()
            if description:
                lines.append(description)

            log_sources = _format_log_sources(
                analytic.get("x_mitre_log_source_references", [])
            )
            if log_sources:
                lines.append(f"Log sources: {log_sources}")

        lines.append("")  # blank line between strategies

    return "\n".join(lines).strip()


def parse_techniques(stix_path: str | Path) -> list[Technique]:
    """Load an ATT&CK Enterprise STIX bundle and extract its techniques.

    Reads the STIX bundle JSON, selects ``attack-pattern`` objects (techniques
    and sub-techniques), and flattens each into a `Technique`. Detection text is
    assembled by following the ``detects`` relationship to each technique's
    detection-strategy and analytic objects. Revoked and deprecated techniques
    are skipped, as are objects with no ATT&CK id.

    Args:
        stix_path: Path to the ATT&CK Enterprise STIX/JSON bundle, e.g.
            ``corpus/attack/enterprise-attack.json``.

    Returns:
        A list of `Technique`, in the order they appear in the bundle.

    Raises:
        FileNotFoundError: If ``stix_path`` does not exist.
        ValueError: If the file is not a STIX bundle with an ``objects`` array,
            or if a technique is missing a required field (name/description).
    """
    path = Path(stix_path)
    if not path.is_file():
        raise FileNotFoundError(f"ATT&CK STIX bundle not found: {path}")

    with path.open(encoding="utf-8") as fh:
        bundle = json.load(fh)

    objects = bundle.get("objects")
    if not isinstance(objects, list):
        raise ValueError(
            f"{path} does not look like a STIX bundle: missing 'objects' array"
        )

    by_id = {obj["id"]: obj for obj in objects if "id" in obj}
    detection_index = _detection_index(objects, by_id)

    techniques: list[Technique] = []
    for obj in objects:
        if obj.get("type") != "attack-pattern":
            continue
        # ATT&CK marks superseded content with these flags; excluding them keeps
        # the corpus to currently-valid techniques only.
        if obj.get("revoked") or obj.get("x_mitre_deprecated"):
            continue

        attack_id = _attack_id(obj.get("external_references", []))
        if attack_id is None:
            # An attack-pattern with no mitre-attack id is not a real technique
            # we can cite; skip it rather than guess.
            continue

        name = obj.get("name")
        description = obj.get("description")
        if not name or not description:
            raise ValueError(
                f"Technique {attack_id} is missing a name or description in {path}"
            )

        techniques.append(
            Technique(
                attack_id=attack_id,
                name=name,
                description=description,
                # Assembled from the detection-strategy / analytic chain; "" if
                # ATT&CK provides no detection strategy for this technique.
                detection=_format_detection(
                    detection_index.get(obj["id"], []), by_id
                ),
            )
        )

    if not techniques:
        raise ValueError(f"No techniques found in {path}; is this the right bundle?")

    return techniques
