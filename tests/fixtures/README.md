# Test fixtures

Hand-built HTML approximating DuckDuckGo's lite endpoint, used to test
`parse_duckduckgo` without a network.

These are **not** live captures. They are deliberately minimal, reproducing
only the structure the parser depends on: the `a.result-link` /
`td.result-snippet` pairing, the `uddg` redirect wrapper in its three forms,
and the anomaly and no-results pages.

If DuckDuckGo changes its layout, replace these with a real capture (and note
the date here) rather than reworking the parser against guesses.
