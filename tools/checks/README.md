Assembly checks of DESIGN v3.2 §5.1, taken unchanged from the M0 spike (branch spike/m0-sp1, commit
55792ed, report notes/m0-sp1-sp4.md). CI runs fetch_upstream, upstream_facts, image_report,
enterprise_content (#1), enterprise_imports (#2, fixture tests/fixtures/enterprise-unguarded.txt),
env_compare (#9) and layer_scan (#10) on the image it smoke-tests. fidelity, repro_compare and
upstream_provenance.sh are for the supply-chain / publish line. All read litellm/upstream.lock.json.
