from google.cloud import bigquery, bigquery_reservation_v1

from cost_guard_mcp.errors import sanitize_exceptions


@sanitize_exceptions("bigquery")
def is_capacity_billed(project: str, location: str = "US") -> bool:
    """Return True if `project` has a BigQuery Reservation assignment (Editions/capacity billing).

    LIMITATION: this checks only the given location (default "US" multi-region). A project
    could have assignments in other locations not covered by a single check — acceptable for
    v1 since most Sandbox/trial usage is US multi-region; revisit if this causes a
    false-negative (silently applying on-demand pricing logic to a capacity-billed query
    running against a differently-located reservation).
    """
    client = bigquery_reservation_v1.ReservationServiceClient()
    parent = f"projects/{project}/locations/{location}"
    assignments = client.search_all_assignments(request={"parent": parent})
    return any(assignments)
