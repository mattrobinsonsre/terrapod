# The five product images, built as ONE buildx invocation per architecture.
#
# WHY THIS EXISTS, because a matrix of five jobs is the obvious shape and was
# what this replaced. Wave 2 of the run -- E2E, eval-boot, the CLI contract, the
# air-gap gate and OCI conformance, ten jobs -- is gated on `build-images-amd64`
# as a WHOLE, so it opens when the LAST of the five finishes. Measured on run
# 43261217946: all five were created at +1.4 and started at +1.8, +1.8, +2.2,
# +2.6 and +2.8, because they queued for slots the Python tier was holding. Up
# to 82s of that gate time was not work, it was five jobs waiting for each
# other. One job cannot stagger against itself.
#
# Three further savings that separate jobs cannot have:
#   * the shared base is pulled ONCE per arch instead of five times (four of the
#     five are FROM the mirrored python image);
#   * the registry cache manifest is imported once rather than five times;
#   * layers common to several images are built once.
#
# The ARCHITECTURES stay separate, deliberately: `build-images-arm64` runs on a
# NATIVE arm64 runner, and merging the two would push one of them through
# emulation, which costs far more than the slot it saves.

variable "REGISTRY" { default = "ghcr.io/mattrobinsonsre" }
variable "CACHE_REF" { default = "ghcr.io/mattrobinsonsre/terrapod-buildcache" }

# `sha-<short>-<arch>`, supplied by the workflow. No default that could be used
# by accident: a wrong tag here is an image nothing downstream can find.
variable "TAG" {}
variable "ARCH" {}

variable "PKG_REFRESH" { default = "" }
variable "PYTHON_BASE_IMAGE" { default = "ghcr.io/mattrobinsonsre/mirror/python:3.14-slim" }
variable "NODE_BASE_IMAGE" { default = "ghcr.io/mattrobinsonsre/mirror/node:26-alpine" }

group "default" {
  targets = ["api", "web", "runner", "listener", "migrations"]
}

target "_common" {
  context   = "."
  platforms = ["linux/${ARCH}"]
}

target "api" {
  inherits   = ["_common"]
  dockerfile = "docker/Dockerfile.api"
  tags       = ["${REGISTRY}/terrapod-api:${TAG}"]
  args       = { PKG_REFRESH = PKG_REFRESH, BASE_IMAGE = PYTHON_BASE_IMAGE }
  cache-from = ["type=registry,ref=${CACHE_REF}:terrapod-api-${ARCH}"]
  cache-to   = ["type=registry,ref=${CACHE_REF}:terrapod-api-${ARCH},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"]
}

target "web" {
  inherits   = ["_common"]
  dockerfile = "docker/Dockerfile.web"
  tags       = ["${REGISTRY}/terrapod-web:${TAG}"]
  args       = { PKG_REFRESH = PKG_REFRESH, BASE_IMAGE = NODE_BASE_IMAGE }
  cache-from = ["type=registry,ref=${CACHE_REF}:terrapod-web-${ARCH}"]
  cache-to   = ["type=registry,ref=${CACHE_REF}:terrapod-web-${ARCH},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"]
}

target "runner" {
  inherits   = ["_common"]
  dockerfile = "docker/Dockerfile.runner"
  tags       = ["${REGISTRY}/terrapod-runner:${TAG}"]
  args       = { PKG_REFRESH = PKG_REFRESH, BASE_IMAGE = PYTHON_BASE_IMAGE }
  cache-from = ["type=registry,ref=${CACHE_REF}:terrapod-runner-${ARCH}"]
  cache-to   = ["type=registry,ref=${CACHE_REF}:terrapod-runner-${ARCH},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"]
}

target "listener" {
  inherits   = ["_common"]
  dockerfile = "docker/Dockerfile.listener"
  tags       = ["${REGISTRY}/terrapod-listener:${TAG}"]
  args       = { PKG_REFRESH = PKG_REFRESH, BASE_IMAGE = PYTHON_BASE_IMAGE }
  cache-from = ["type=registry,ref=${CACHE_REF}:terrapod-listener-${ARCH}"]
  cache-to   = ["type=registry,ref=${CACHE_REF}:terrapod-listener-${ARCH},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"]
}

target "migrations" {
  inherits   = ["_common"]
  dockerfile = "docker/Dockerfile.migrations"
  tags       = ["${REGISTRY}/terrapod-migrations:${TAG}"]
  args       = { PKG_REFRESH = PKG_REFRESH, BASE_IMAGE = PYTHON_BASE_IMAGE }
  cache-from = ["type=registry,ref=${CACHE_REF}:terrapod-migrations-${ARCH}"]
  cache-to   = ["type=registry,ref=${CACHE_REF}:terrapod-migrations-${ARCH},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"]
}
