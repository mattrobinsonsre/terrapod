"""Utilities for repacking VCS archive tarballs.

GitHub and GitLab archive downloads wrap all files in a top-level directory
(e.g. ``owner-repo-sha/``).  Terraform/tofu expects module tarballs and
workspace configs to have .tf files at the root.  The helper here strips
that wrapper so downloaded archives are usable as-is.
"""

import io
import tarfile


def strip_archive_top_level_dir(archive: bytes) -> bytes:
    """Repack a gzipped tarball, stripping the single top-level directory.

    ``owner-repo-sha/variables.tf`` becomes ``variables.tf``, etc.
    If the archive has no common top-level directory, it is returned unchanged.
    """
    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()

    with (
        tarfile.open(fileobj=in_buf, mode="r:gz") as src,
        tarfile.open(fileobj=out_buf, mode="w:gz") as dst,
    ):
        for member in src.getmembers():
            # Strip first path component: "owner-repo-sha/file" -> "file"
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # skip the top-level directory entry itself
            member.name = parts[1]
            if member.isfile():
                f = src.extractfile(member)
                if f:
                    dst.addfile(member, f)
            else:
                dst.addfile(member)

    return out_buf.getvalue()
