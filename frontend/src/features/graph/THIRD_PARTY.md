# F03 third-party inventory

## Viz.js

- Package: `@viz-js/viz`
- Exact version: `3.28.0`
- Source: https://github.com/mdaines/viz-js
- License: MIT; retained as `LICENSE.viz-js.txt`.
- Use: Graphviz compiled to WebAssembly. RK supplies deterministic DOT ordering and uses the `dot` engine only for coordinates; Viz.js does not decide truth, grouping, lifecycle, closure, or pagination.

## Archon Horizon lineage

- Source: https://github.com/frenzymath/Archon
- Pinned source commit: `80f93b9d3d0c5d12c6e4b23f05849ec1cf29fa18`
- Upstream license: Apache-2.0; retained as `LICENSE.archon.txt`.
- RK does not copy an upstream state store, API client, truth classification, or full-graph loading behavior. See `NOTICE.md` for the adaptation boundary.
