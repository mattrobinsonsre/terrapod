# Self-Hosting the Provider

The Terrapod provider is distributed through the Terrapod instance itself. No public registry or manual download is required.

## How the Built-in Pull-Through Cache Works

When `terraform init` requests the `terrapod` provider:

1. **Version list**: The API returns the running platform version
2. **Download request**: The API checks object storage for a cached binary
3. **Cache miss**: Fetches the matching binary from the GitHub Release (`https://github.com/mattrobinsonsre/terrapod/releases/download/v{version}/...`)
4. **Cache**: Stores the binary in object storage
5. **Serve**: Returns a presigned download URL

Subsequent requests for the same version are served directly from the cache.

### Storage Keys

```
cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_{os}_{arch}.zip
cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS
cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS.sig
```

## Air-Gapped Deployments

For environments without internet access:

1. Download the provider binary from the GitHub Release on a connected machine
2. Upload it to the Terrapod instance's object storage at the expected key path
3. `terraform init` will find the cached binary and skip the upstream fetch

## Supported Platforms

| OS | Architecture |
|---|---|
| linux | amd64 |
| linux | arm64 |
| darwin | amd64 |
| darwin | arm64 |

## Version Matching

The provider version always matches the platform version. When you upgrade Terrapod to v0.4.0, the registry automatically advertises v0.4.0 of the provider. Use `~>` version constraints for compatibility:

```hcl
version = "~> 0.3"
```
