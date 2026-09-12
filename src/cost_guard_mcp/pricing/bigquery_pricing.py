"""BigQuery on-demand pricing constants.

Source: https://cloud.google.com/bigquery/pricing — US multi-region on-demand rate,
confirmed live during the 2026-09-12 research refresh. Re-verify this value before
each release; Google has changed it before (a one-time 25% increase, $5.00 -> $6.25,
effective 2023-07-05, tied to the BigQuery Editions launch).
"""

TIB_IN_BYTES = 1024**4
ON_DEMAND_USD_PER_TIB = 6.25
