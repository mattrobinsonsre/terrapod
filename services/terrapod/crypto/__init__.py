"""Optional application-layer encryption at rest (envelope encryption).

OFF by default. See ``service.py`` for the encryption service singleton and
``providers.py`` for the pluggable KEK (key-encryption-key) backends. The data
path is local AES-256-GCM with a cached DEK; only KEK wrap/unwrap touches the
network (at startup and rotation). See docs/encryption-at-rest.md.
"""
