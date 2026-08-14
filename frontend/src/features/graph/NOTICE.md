# Horizon adaptation notice

F03 acknowledges the Archon Horizon DAG presentation lineage at commit `80f93b9d3d0c5d12c6e4b23f05849ec1cf29fa18`.

RK independently implements this feature against RK-PRODUCT-1.1 `GRAPH_SEARCH`, `GRAPH_SLICE`, `DEPENDENCY_CLOSURE`, and `REVERSE_CLOSURE`. The adaptation keeps the useful ideas of layered top-to-bottom layout, chapter-like group folding, and local graph navigation, while changing the semantics as follows:

- upstream chapter folding becomes server-owned `GraphGroup` with one expanded group;
- the UI hard-separates `VERIFIED` dependable facts from `RESEARCH_HISTORY` lineage;
- `+N`, totals, cross-route boundaries, and closure paths come only from server responses;
- continuation is revision-bound and never downloads the complete graph before display;
- Graphviz coordinates are rebuildable presentation state, not mathematical authority;
- every graph has an equivalent keyboard and screen-reader list.

No upstream Horizon source file is copied into this directory. The source and Apache-2.0 license are retained to make the design lineage and review boundary explicit.
