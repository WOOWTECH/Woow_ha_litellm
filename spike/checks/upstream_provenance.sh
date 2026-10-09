#!/usr/bin/env bash
# DESIGN §5.3: is the pinned upstream signed, and what do its attestations say?
#
# usage: upstream_provenance.sh LOCK OUTDIR
# Installs cosign (v2 and v3) and crane by exact version + sha256, then:
#   * cosign verify --key <BerriAI cosign.pub @0112e53> on the index digest and the tag
#   * cosign verify-attestation (the SPDX SBOM attestation BerriAI attaches)
#   * reads the BuildKit SLSA provenance stored inside the index
# Everything written to OUTDIR is metadata (digests, builder ids, verification results).
set -euo pipefail

LOCK=$1
OUT=$2
mkdir -p "$OUT"
BIN=$(mktemp -d)
export PATH="$BIN:$PATH"

fetch() {  # fetch URL SHA256 DEST
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c - >/dev/null
}
fetch https://github.com/sigstore/cosign/releases/download/v2.6.5/cosign-linux-amd64 \
  c3b4f5410e608af03a5eb0aaac84a4313d8da131248e08ff1759ac70c79d1644 "$BIN/cosign"
fetch https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64 \
  4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71 "$BIN/cosign3"
fetch https://github.com/google/go-containerregistry/releases/download/v0.21.9/go-containerregistry_Linux_x86_64.tar.gz \
  5c16d8ddb971cb1d5e6ed8b1e743da8224414eeba2c2762d8f1a61b2f095699e "$BIN/ggcr.tgz"
tar -xzf "$BIN/ggcr.tgz" -C "$BIN" crane
chmod +x "$BIN/cosign" "$BIN/cosign3" "$BIN/crane"
cosign version 2>&1 | grep -E '^GitVersion' || true
cosign3 version 2>&1 | grep -E '^GitVersion' || true
crane version

IMG=$(jq -r .image "$LOCK")
TAG=$(jq -r .tag "$LOCK")
IDX=$(jq -r .index_digest "$LOCK")
AMD=$(jq -r '.platforms["linux/amd64"].manifest_digest' "$LOCK")
KEY=$(jq -r .cosign_pub.path "$LOCK")
KEYSHA=$(jq -r .cosign_pub.sha256 "$LOCK")
echo "$KEYSHA  $KEY" | sha256sum -c -
REF="$IMG@$IDX"

res() { jq -cn --arg n "$1" --arg r "$2" --arg d "$3" '{name:$n,result:$r,detail:$d}' >> "$OUT/provenance-results.jsonl"; printf '%-45s %-5s %s\n' "$1" "$2" "$3"; }
: > "$OUT/provenance-results.jsonl"

tagdig=$(crane digest "$IMG:$TAG")
[ "$tagdig" = "$IDX" ] && res tag-points-at-pinned-index PASS "$IMG:$TAG -> $tagdig" || res tag-points-at-pinned-index INFO "$IMG:$TAG -> $tagdig (pinned $IDX)"

echo "--- cosign triangulate / tree"
cosign triangulate "$REF" | tee "$OUT/cosign-triangulate.txt"
cosign triangulate --type attestation "$REF" | tee -a "$OUT/cosign-triangulate.txt"
cosign tree "$REF" 2>&1 | tee "$OUT/cosign-tree.txt" || true

echo "--- cosign verify (v2.6.5) on the index digest"
if cosign verify --key "$KEY" "$REF" > "$OUT/cosign-verify-index.json" 2> "$OUT/cosign-verify-index.err"; then
  cat "$OUT/cosign-verify-index.err"
  ref=$(jq -r '.[0].critical.identity["docker-reference"]' "$OUT/cosign-verify-index.json")
  dig=$(jq -r '.[0].critical.image["docker-manifest-digest"]' "$OUT/cosign-verify-index.json")
  [ "$dig" = "$IDX" ] && res cosign-verify-index PASS "identity $ref, digest $dig, $(jq length "$OUT/cosign-verify-index.json") signature(s); tlog+key checked" \
                      || res cosign-verify-index FAIL "signed digest $dig != $IDX"
else
  res cosign-verify-index FAIL "$(tail -c 400 "$OUT/cosign-verify-index.err" | tr '\n' ' ')"
fi

echo "--- cosign verify on the tag (resolves to the index)"
if cosign verify --key "$KEY" "$IMG:$TAG" > /dev/null 2> "$OUT/cosign-verify-tag.err"; then
  res cosign-verify-tag PASS "$IMG:$TAG"
else
  res cosign-verify-tag FAIL "$(tail -c 300 "$OUT/cosign-verify-tag.err" | tr '\n' ' ')"
fi

echo "--- cosign verify on the amd64 manifest digest (signatures are per index; expected to fail)"
if cosign verify --key "$KEY" "$IMG@$AMD" > /dev/null 2> "$OUT/cosign-verify-amd64.err"; then
  res cosign-verify-amd64-manifest INFO "amd64 manifest is signed too"
else
  res cosign-verify-amd64-manifest INFO "no signature on the amd64 manifest itself: $(tail -c 200 "$OUT/cosign-verify-amd64.err" | tr '\n' ' ')"
fi

echo "--- cosign v3.1.3 verify on the index digest"
if cosign3 verify --key "$KEY" "$REF" > /dev/null 2> "$OUT/cosign3-verify-index.err"; then
  res cosign3-verify-index PASS "cosign v3.1.3 verifies the legacy .sig signature"
else
  res cosign3-verify-index INFO "cosign v3.1.3: $(tail -c 300 "$OUT/cosign3-verify-index.err" | tr '\n' ' ')"
  if cosign3 verify --key "$KEY" --new-bundle-format=false "$REF" > /dev/null 2>> "$OUT/cosign3-verify-index.err"; then
    res cosign3-verify-index-legacy PASS "cosign v3.1.3 with --new-bundle-format=false"
  else
    res cosign3-verify-index-legacy INFO "$(tail -c 300 "$OUT/cosign3-verify-index.err" | tr '\n' ' ')"
  fi
fi

echo "--- cosign verify-attestation (SPDX SBOM attached by BerriAI)"
if cosign verify-attestation --key "$KEY" --type spdxjson "$REF" > "$BIN/att.jsonl" 2> "$OUT/cosign-verify-attestation.err"; then
  python3 -I - "$BIN/att.jsonl" "$OUT/attestation-summary.json" <<'PY'
import base64, json, sys
out = []
for line in open(sys.argv[1]):
    env = json.loads(line)
    st = json.loads(base64.b64decode(env["payload"]))
    pred = st.get("predicate", {})
    pkgs = pred.get("packages", []) if isinstance(pred, dict) else []
    names = [p.get("name", "") for p in pkgs]
    lic = sorted({p.get("licenseDeclared", "") for p in pkgs if "Proprietary" in str(p.get("licenseDeclared", ""))})
    out.append({"predicateType": st.get("predicateType"), "subject": st.get("subject"),
                "spdx_name": pred.get("name") if isinstance(pred, dict) else None,
                "creators": (pred.get("creationInfo") or {}).get("creators") if isinstance(pred, dict) else None,
                "packages": len(pkgs),
                "mentions_litellm_enterprise": [n for n in names if "enterprise" in n.lower()][:10],
                "proprietary_licenses": lic})
json.dump(out, open(sys.argv[2], "w"), indent=1)
print(json.dumps(out, indent=1)[:3000])
PY
  res cosign-verify-attestation PASS "$(jq -c '[.[] | {predicateType, packages, subject: [.subject[]?.digest.sha256]}]' "$OUT/attestation-summary.json")"
else
  res cosign-verify-attestation INFO "$(tail -c 300 "$OUT/cosign-verify-attestation.err" | tr '\n' ' ')"
fi

echo "--- in-index attestation manifests (BuildKit SLSA provenance)"
crane manifest "$REF" > "$OUT/upstream-index.json"
jq -r '.manifests[] | [.digest, (.platform.os + "/" + .platform.architecture), (.annotations["vnd.docker.reference.type"] // ""), (.annotations["vnd.docker.reference.digest"] // "")] | @tsv' "$OUT/upstream-index.json"
for att in $(jq -r --arg d "$AMD" '.manifests[] | select(.annotations["vnd.docker.reference.digest"]==$d) | .digest' "$OUT/upstream-index.json"); do
  crane manifest "$IMG@$att" > "$OUT/attestation-manifest-amd64.json"
  for l in $(jq -r '.layers[] | select(.annotations["in-toto.io/predicate-type"]=="https://slsa.dev/provenance/v1") | .digest' "$OUT/attestation-manifest-amd64.json"); do
    crane blob "$IMG@$l" > "$BIN/prov.json"
    echo "$(echo "$l" | cut -d: -f2)  $BIN/prov.json" | sha256sum -c - >/dev/null
    jq '{subject, buildType: .predicate.buildDefinition.buildType,
         source: .predicate.buildDefinition.externalParameters.request.root.request.args["vcs:source"],
         revision: .predicate.buildDefinition.externalParameters.request.root.request.args["vcs:revision"],
         dockerfile: .predicate.buildDefinition.externalParameters.configSource.path,
         compatibilityVersion: .predicate.buildDefinition.externalParameters.request.compatibilityVersion,
         resolvedDependencies: [.predicate.buildDefinition.resolvedDependencies[].uri],
         builder: .predicate.runDetails.builder.id,
         workflow: .predicate.buildDefinition.internalParameters.github_workflow_ref,
         workflow_sha: .predicate.buildDefinition.internalParameters.github_workflow_sha,
         event: .predicate.buildDefinition.internalParameters.github_event_name,
         actor: .predicate.buildDefinition.internalParameters.github_actor,
         runner: .predicate.buildDefinition.internalParameters.github_runner_environment,
         started: .predicate.runDetails.metadata.startedOn, finished: .predicate.runDetails.metadata.finishedOn}' \
      "$BIN/prov.json" > "$OUT/slsa-provenance-amd64-summary.json"
    cat "$OUT/slsa-provenance-amd64-summary.json"
    rev=$(jq -r .revision "$OUT/slsa-provenance-amd64-summary.json")
    want=$(jq -r .litellm_commit "$LOCK")
    [ "$rev" = "$want" ] && res slsa-provenance-revision PASS "vcs:revision $rev, builder $(jq -r .builder "$OUT/slsa-provenance-amd64-summary.json")" \
                         || res slsa-provenance-revision FAIL "vcs:revision $rev != $want"
  done
done
jq -s '{result: (if any(.[]; .result=="FAIL") then "FAIL" else "PASS" end), checks: .}' "$OUT/provenance-results.jsonl" > "$OUT/provenance-results.json"
echo "provenance: $(jq -r .result "$OUT/provenance-results.json")"
[ "$(jq -r .result "$OUT/provenance-results.json")" = PASS ]
