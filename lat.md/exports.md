# Exports

Completed results are shown as a paginated, searchable preview plus charts, and can be downloaded as CSV or XLSX. Rows keep the stable field set defined by [[app.py#CSV_FIELDNAMES]].

## Preview and charts

Completed rows render as a paginated, searchable preview plus a fixed set of charts; focusing one publication narrows the SDG donut to it.

- [[app.py#render_result_preview]] — paginated preview table (page size [[app.py#PREVIEW_PAGE_SIZE]]) with a focus search bounded to the current page or 100 matches via [[app.py#focus_candidate_indices]]; selecting a publication focuses the SDG ring chart on it.
- [[app.py#render_result_charts]] — renders the SDG donut ([[app.py#aggregate_sdg_counts]], [[app.py#render_sdg_pie_chart]]), the 3D co-affiliation network ([[app.py#render_institution_network]]), per-author OA ([[app.py#render_author_oa_chart]]), OA ring ([[app.py#render_oa_ring_chart]]), OA volume by month ([[app.py#render_oa_status_chart]]), and publication types ([[app.py#render_publication_type_chart]]).
- [[app.py#parse_sdg_formatted]] — parses stored `NN% SDG N (Name)` lines back into (code, percent, name) tuples for aggregation.
- [[app.py#_parse_mixed_timestamp]] / [[app.py#_parse_mixed_datetime_series]] — tolerate plain dates and timezone-aware strings when filtering charts by the selected period.

## Export

Results serialize to a stable CSV field set and an XLSX workbook, with a descriptive bounded filename.

- [[app.py#build_output_filename]] — descriptive, length-bounded filename encoding sources, institution, types, model, dates, and limit; whole ids are dropped with a `+N` suffix via [[app.py#_fit_hyphen_items]].
- [[app.py#rows_to_csv_bytes]] — UTF-8 CSV bytes over [[app.py#CSV_FIELDNAMES]].
- [[app.py#rows_to_excel_bytes]] — XLSX via openpyxl; cells that look like spreadsheet formulas are forced to literal text so external publication metadata cannot execute on open.
- [[app.py#render_export_downloads]] — the CSV and XLSX download buttons.
- [[app.py#result_rows_from_payload]] — rows from a completed payload, falling back to the stored CSV for old payloads.
- [[app.py#render_fetch_summary]] — the success banner with source, dedup, and abstract-enrichment counts, plus warnings for failed sources and Semantic Scholar auth rejections.
- Filename segments are sanitized by `sanitize_filename` (defined in [[openalex_sdg.py#sanitize_filename]] and imported by `app.py`).
