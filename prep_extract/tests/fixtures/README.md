# Extractor test fixtures

- `greenhouse_min.json` — hand-written, the smallest Greenhouse board-API response that exercises every field type.
- `greenhouse_board.json` — the question list of a real Greenhouse board-API response (the shape matters: field
  types, hidden fields, compliance blocks), with the posting metadata replaced by invented values and every
  company name, URL and vendor name replaced.
- `ashby_application.html`, `ashby_fieldset_radio.html`, `lever_apply.html` — the rendered DOM of three real
  application pages, saved during the extractor's build because the DOM adapters are only as good as the markup
  they are tested on. Before they were committed here, every company name, URL, email address, UUID and posting
  description was replaced with invented text, and the stylesheets were removed. The form markup (classes,
  `data-field-path` attributes, fieldsets, the Lever `✱` marker) is unchanged, which is what the tests exercise.
