#!/usr/bin/env bash
# Seed the signed dev provider directly into the local filesystem storage PVC.
set -euo pipefail

VERSION=${1:?usage: dev-provider-stage.sh <version>}
# A FOURTH copy of the same version-shape regex lived here, in another
# directory, and it refused `2.0.0-alpha.1.dev.N` after `dev-package.sh` learned
# to derive one -- so the dev stack still would not come up, now failing a step
# further along. It also said nothing at all: a bare `|| exit 2` gave Tilt
# "exit status 2" and no reason, which is most of why it cost a diagnosis round.
#
# Loose on purpose now. The authoritative check is the comparison against
# `current-version` below, which is the value `dev-package.sh` actually produced
# -- strictly stronger than any pattern -- so this only has to refuse something
# unsafe to interpolate into a path.
if [[ ! "$VERSION" =~ ^[0-9][0-9A-Za-z.+-]*$ ]]; then
  printf 'Refusing a version that is not safe as a path component: %s\n' "$VERSION" >&2
  exit 2
fi
ROOT=$(dirname "$(dirname "$(realpath "$0")")")
STORE="$ROOT/provider/.dev-build"
KEY_HOME="$HOME/.local/share/terrapod/certs/provider-gnupg"
# The real gate: this must be the artifact `dev-package.sh` just built.
if [[ "$(< "$STORE/current-version")" != "$VERSION" ]]; then
  printf 'Staged version %s does not match the built one %s\n' \
    "$VERSION" "$(< "$STORE/current-version")" >&2
  exit 2
fi
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) printf 'Unsupported local runner architecture\n' >&2; exit 2 ;;
esac
ZIP="terraform-provider-terrapod_${VERSION}_linux_${ARCH}.zip"
SUMS="terraform-provider-terrapod_${VERSION}_SHA256SUMS"
ARTIFACTS="$STORE/artifacts/$VERSION"
test -s "$STORE/signing-key.asc"
(cd "$ARTIFACTS" && sha256sum -c "$SUMS")
gpg --homedir "$KEY_HOME" --verify "$ARTIFACTS/$SUMS.sig" "$ARTIFACTS/$SUMS" >&2

for attempt in $(seq 1 60); do
  if kubectl -n terrapod get pvc terrapod-storage >/dev/null 2>&1; then break; fi
  sleep 2
done
kubectl -n terrapod get pvc terrapod-storage >/dev/null

POD="tp-dev-provider-stage-$$"
cleanup() { kubectl -n terrapod delete pod "$POD" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true; }
trap cleanup EXIT
kubectl -n terrapod apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD
spec:
  restartPolicy: Never
  containers:
    - name: stage
      image: postgres:16-alpine
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: cache
          mountPath: /cache
  volumes:
    - name: cache
      persistentVolumeClaim:
        claimName: terrapod-storage
EOF
kubectl -n terrapod wait --for=condition=Ready "pod/$POD" --timeout=180s
kubectl -n terrapod exec "$POD" -- mkdir -p /tmp/dev-provider
for name in "$ZIP" "$SUMS" "$SUMS.sig"; do
  kubectl -n terrapod cp "$ARTIFACTS/$name" "$POD:/tmp/dev-provider/$name"
done
kubectl -n terrapod cp "$STORE/signing-key.asc" "$POD:/tmp/dev-provider/signing-key.asc"

kubectl -n terrapod exec "$POD" -- sh -ec '
  cd /tmp/dev-provider
  sha256sum -c "$1"
  root=/cache/cache/provider/terrapod
  target="$root/$2"
  mkdir -p "$root"
  if [ -e "$target" ]; then
    for file in "$3" "$1" "$1.sig"; do
      cmp "$file" "$target/$file" || { echo "Existing dev version differs: $2" >&2; exit 1; }
    done
  else
    staged="$root/.staging-$2-$$"
    mkdir "$staged"
    cp "$3" "$1" "$1.sig" "$staged/"
    mv "$staged" "$target"
  fi
  cp signing-key.asc "$root/.dev-signing-key-$$"
  mv "$root/.dev-signing-key-$$" "$root/dev-signing-key.asc"
' sh "$SUMS" "$VERSION" "$ZIP"
kubectl -n terrapod delete pod "$POD" --wait=true --timeout=90s >/dev/null
trap - EXIT
printf 'Seeded signed provider %s directly into terrapod-storage PVC\n' "$VERSION"
