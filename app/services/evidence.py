"""Evidence collection for investigations.

Evidence sources implement the small ``EvidenceCollector`` protocol, so
backend log retrieval, frontend error feeds, PostHog events, and source
code context can be added later as new collectors without touching the
engine.

Two rules make reports honest:

* Every item has a stable ``id`` so the report can cite exactly what was
  (or was not) collected.
* Sources that are *not* available are returned as explicit ``missing``
  items instead of being omitted. The model is thereby told which
  evidence it does not have, and the persisted report records the gap,
  so a conclusion can never silently rest on fabricated logs.
"""

from typing import Protocol

from app.schemas.investigation import EvidenceItem, InvestigationInput

# Cap per evidence item so one oversized alert annotation cannot blow up
# the prompt or the report row. Truncation is explicit and noted.
MAX_CONTENT_CHARS = 8_000


def _truncate(content: str) -> str:
    if len(content) <= MAX_CONTENT_CHARS:
        return content
    return content[:MAX_CONTENT_CHARS] + "\n...[truncated]"


class EvidenceCollector(Protocol):
    """A source of evidence for one investigation."""

    name: str

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        """Return evidence items derived from the incident."""
        ...


class AlertPayloadEvidenceCollector:
    """Initial evidence source: the alert payload already on the incident.

    Everything here arrived with the alert itself - no external systems
    are contacted. Later collectors (logs, PostHog, source code) plug in
    alongside this one.
    """

    name = "grafana_alert"

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []

        # A summary that never arrived is a recorded gap, not available
        # evidence with empty content.
        if incident.summary:
            items.append(
                EvidenceItem(
                    id="alert-summary",
                    source=self.name,
                    description=(
                        "Summary of the triggering alert as received "
                        "from Grafana."
                    ),
                    status="available",
                    content=_truncate(incident.summary),
                    observed_at=incident.last_seen_at,
                )
            )
        else:
            items.append(
                EvidenceItem(
                    id="alert-summary",
                    source=self.name,
                    description=(
                        "The triggering alert arrived without a textual "
                        "summary, so no narrative context is available."
                    ),
                    status="missing",
                    observed_at=incident.last_seen_at,
                )
            )

        # Metadata is always available: it is the incident record itself.
        # Kept as content so the model sees it inline with the evidence.
        metadata_lines = [
            f"title: {incident.title}",
            f"service: {incident.service_name}",
            f"severity: {incident.severity}",
            f"source: {incident.source}",
            f"fingerprint: {incident.fingerprint}",
            f"first_seen_at: {incident.first_seen_at.isoformat()}",
            f"last_seen_at: {incident.last_seen_at.isoformat()}",
        ]
        metadata_lines.extend(
            f"{key}: {value}"
            for key, value in sorted(incident.alert_metadata.items())
        )

        items.append(
            EvidenceItem(
                id="alert-metadata",
                source=self.name,
                description=(
                    "Structured attributes of the incident record: "
                    "service, severity, fingerprint, and timestamps."
                ),
                status="available",
                content="\n".join(metadata_lines),
                observed_at=incident.last_seen_at,
            )
        )

        return items


# Sources the product may not be able to collect on a given run. They
# are recorded as explicit gaps on every report *unless* a collector
# already produced an item with the same stable ID. Since Task 2 the
# backend log collector normally emits ``gap-backend-logs`` itself (or
# ``backend-logs`` when logs were found); the static entry here is only
# a fallback for callers that pass their own collector set without a
# log collector.
UNAVAILABLE_EVIDENCE_GAPS: tuple[EvidenceItem, ...] = (
    EvidenceItem(
        id="gap-backend-logs",
        source="backend_logs",
        description=(
            "Backend application logs for the affected service were not "
            "collected: no backend log collector was configured for this "
            "investigation run."
        ),
        status="missing",
    ),
    EvidenceItem(
        id="gap-frontend-errors",
        source="frontend_errors",
        description=(
            "Frontend errors and session context were not collected: "
            "no PostHog collector was configured for this "
            "investigation run."
        ),
        status="missing",
    ),
    EvidenceItem(
        id="gap-source-code",
        source="source_code",
        description=(
            "Relevant source code could not be inspected: repository "
            "access is not implemented yet."
        ),
        status="missing",
    ),
)


def collect_evidence(
    incident: InvestigationInput,
    collectors: tuple[EvidenceCollector, ...] | None = None,
) -> list[EvidenceItem]:
    """Run the configured collectors and append the recorded gaps."""
    if collectors is None:
        collectors = (AlertPayloadEvidenceCollector(),)

    items: list[EvidenceItem] = []
    seen_ids: set[str] = set()

    for collector in collectors:
        for item in collector.collect(incident):
            # Stable, collision-free IDs are load-bearing: the report
            # cites them and the engine rejects unknown references.
            if item.id in seen_ids:
                raise ValueError(
                    f"duplicate evidence id '{item.id}' from collector "
                    f"'{collector.name}'"
                )
            seen_ids.add(item.id)
            items.append(item)

    # Append the recorded gaps, skipping any source a collector already
    # covered: the backend log collector emits either ``backend-logs``
    # (found) or ``gap-backend-logs`` (not found), and in both cases the
    # static fallback for that source would be redundant or a duplicate.
    covered_sources = {item.source for item in items}
    for gap in UNAVAILABLE_EVIDENCE_GAPS:
        if gap.source not in covered_sources:
            items.append(gap)

    return items
