"""Parse MITRE ATT&CK Enterprise STIX/JSON into flat technique records.

This module is pure parsing/extraction glue. It does NOT chunk — it turns the
STIX bundle into simple `Technique` records that `chunk.py` later groups into
retrievable chunks (one chunk per technique). Keeping the two concerns apart
means the author can own the chunking strategy without touching STIX details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Technique:
    """One MITRE ATT&CK technique, flattened from its STIX attack-pattern object.

    Attributes:
        attack_id: The ATT&CK technique id, e.g. ``T1059`` or ``T1059.001``.
        name: Human-readable technique name.
        description: The technique's prose description (may be long).
        detection: The technique's detection guidance. Empty string if ATT&CK
            does not provide ``x_mitre_detection`` for this technique.
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


def parse_techniques(stix_path: str | Path) -> list[Technique]:
    """Load an ATT&CK Enterprise STIX bundle and extract its techniques.

    Reads the STIX bundle JSON, selects ``attack-pattern`` objects (techniques
    and sub-techniques), and flattens each into a `Technique`. Revoked and
    deprecated techniques are skipped, as are objects with no ATT&CK id.

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
                # Not every technique ships detection guidance; default to "".
                detection=obj.get("x_mitre_detection", ""),
            )
        )

    if not techniques:
        raise ValueError(f"No techniques found in {path}; is this the right bundle?")

    return techniques
