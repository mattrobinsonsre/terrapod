"""Integration: the OCI registry against real Postgres and real storage (#1408).

The mocked router tests cover shape — status codes, headers, error envelopes.
What they cannot cover is everything this file is about, because all of it lives
in the database: whether an upload survives being handled by a different replica
mid-stream, whether the same blob pushed to two repositories is stored once, and
whether the unique constraints hold when two clients race.

The replica test is the one that matters. Terrapod's API runs with several
replicas behind a load balancer and nothing pins a client to one of them, so a
`docker push` will have its PATCH chunks answered by whichever replica the LB
picks. Upload state therefore cannot live in process memory — and a suite that
drives the app in a single process will happily pass with it there. These tests
drive each step through a *separate* session so that shortcut fails.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.db.session import get_db_session

pytestmark = pytest.mark.asyncio

V2 = "/v2"
REPO = "team/app"
OTHER = "team/other"


async def _token(email: str = "admin@test.com") -> str:
    """Mint a real API token and return the Basic header value for it.

    Deliberately a real token rather than a dependency override: the auth path
    is part of what is being integrated, and the Basic decoding is where a
    `runtok:`-style colon-bearing credential got truncated once already.
    """
    from terrapod.auth import api_tokens
    from terrapod.auth.recent_users import mark_user_seen

    # An interactive token is rejected if its owner has not logged in inside the
    # idle window (#495), so the marker a real login would leave has to exist.
    # Set deliberately rather than by disabling the check: `docker login` with a
    # personal token is the ordinary path, and it should be the one under test.
    await mark_user_seen(email, ttl_seconds=3600)

    async with get_db_session() as db:
        _record, raw = await api_tokens.create_api_token(
            db,
            bound_to=email,
            created_by=email,
            kind="interactive",
            lifespan_hours=1,
        )
    return "Basic " + base64.b64encode(f"unused:{raw}".encode()).decode()


async def _seed_admin_role(email: str = "admin@test.com") -> None:
    """Give the token's identity platform admin, so RBAC is not the subject."""
    from tests.integration.conftest import assign_platform_role

    await assign_platform_role(None, "api_token", email, "admin")


def _fresh_client(app) -> AsyncClient:
    """A client with its own transport — a stand-in for another replica.

    Not the same object as the `client` fixture: each call produces an
    independent connection so no per-connection state can be relied on.
    """
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestReplicaSafeUploads:
    """A chunked upload must survive being handled by a different replica.

    Every step below goes through its own client and its own DB session. If
    upload progress were ever cached in process memory — a dict of session id to
    offset, a buffer, an open file handle — the second chunk would be rejected
    as out of order, or the completed blob would be short.
    """

    async def test_each_chunk_through_a_separate_client(self, app) -> None:
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        chunks = [b"first-chunk-", b"second-chunk-", b"third-chunk"]
        body = b"".join(chunks)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()

        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{REPO}/blobs/uploads/", headers=auth)
        assert started.status_code == 202, started.text
        location = started.headers["Location"]

        offset = 0
        for chunk in chunks:
            async with _fresh_client(app) as c:  # a different replica each time
                r = await c.patch(
                    location,
                    headers={
                        **auth,
                        "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
                        "Content-Type": "application/octet-stream",
                    },
                    content=chunk,
                )
            assert r.status_code == 202, r.text
            # The offset the client will send next comes from the server, so an
            # off-by-one here corrupts every subsequent chunk.
            assert r.headers["Range"] == f"0-{offset + len(chunk) - 1}"
            offset += len(chunk)

        async with _fresh_client(app) as c:
            done = await c.put(f"{location}?digest={digest}", headers=auth)
        assert done.status_code == 201, done.text
        assert done.headers["Docker-Content-Digest"] == digest

        # And the bytes are byte-identical, not merely present at the right length.
        async with _fresh_client(app) as c:
            got = await c.get(f"{V2}/{REPO}/blobs/{digest}", headers=auth, follow_redirects=True)
        assert got.status_code == 200
        assert got.content == body

    async def test_status_is_readable_from_another_replica_mid_upload(self, app) -> None:
        """A client that loses its response asks for the offset and resumes.

        The spec's resume path, and it only works if the offset is durable.
        """
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{REPO}/blobs/uploads/", headers=auth)
        location = started.headers["Location"]

        async with _fresh_client(app) as c:
            await c.patch(
                location,
                headers={**auth, "Content-Range": "0-4"},
                content=b"hello",
            )

        async with _fresh_client(app) as c:
            status = await c.get(location, headers=auth)
        assert status.status_code == 204
        assert status.headers["Range"] == "0-4"

    async def test_a_cancelled_upload_is_gone_everywhere(self, app) -> None:
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{REPO}/blobs/uploads/", headers=auth)
        location = started.headers["Location"]

        async with _fresh_client(app) as c:
            # 204, per the spec's "SHOULD be a 204 No Content".
            assert (await c.delete(location, headers=auth)).status_code == 204

        async with _fresh_client(app) as c:
            assert (await c.get(location, headers=auth)).status_code == 404


class TestContentAddressedStorage:
    """The same bytes in two repositories are stored once and linked twice."""

    async def _push_blob(self, app, repo: str, body: bytes, auth: dict) -> str:
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{repo}/blobs/uploads/", headers=auth)
            location = started.headers["Location"]
            r = await c.put(f"{location}?digest={digest}", headers=auth, content=body)
            assert r.status_code == 201, r.text
        return digest

    async def test_one_blob_row_two_links(self, app) -> None:
        from sqlalchemy import func, select

        from terrapod.db.models import OCIBlob, OCIRepositoryBlob

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        body = b"shared-layer-bytes"

        digest = await self._push_blob(app, REPO, body, auth)
        assert await self._push_blob(app, OTHER, body, auth) == digest

        async with get_db_session() as db:
            blobs = await db.scalar(
                select(func.count()).select_from(OCIBlob).where(OCIBlob.digest == digest)
            )
            links = await db.scalar(select(func.count()).select_from(OCIRepositoryBlob))

        # One copy of the bytes; one link per repository that holds it. Storing
        # it twice would be the bug that makes a registry's disk usage a
        # multiple of its actual content.
        assert blobs == 1
        assert links == 2

    async def test_a_blob_is_not_readable_from_a_repository_that_lacks_the_link(self, app) -> None:
        """Content addressing must not become a cross-tenant read.

        Global blob storage means the bytes for a private image are one digest
        guess away from any other repository — unless membership is checked per
        repository, which is what the link table is for.
        """
        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        digest = await self._push_blob(app, REPO, b"private-bytes", auth)

        async with _fresh_client(app) as c:
            # `OTHER` exists as a repository but never received this blob.
            await c.post(f"{V2}/{OTHER}/blobs/uploads/", headers=auth)
            r = await c.get(f"{V2}/{OTHER}/blobs/{digest}", headers=auth)
        assert r.status_code == 404

    async def test_cross_repo_mount_links_without_re_uploading(self, app) -> None:
        from sqlalchemy import func, select

        from terrapod.db.models import OCIBlob

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        digest = await self._push_blob(app, REPO, b"mountable-bytes", auth)

        async with _fresh_client(app) as c:
            r = await c.post(
                f"{V2}/{OTHER}/blobs/uploads/?mount={digest}&from={REPO}",
                headers=auth,
            )
        # 201 (not 202) is how a client is told the mount succeeded and it need
        # not upload anything — the whole point of the mechanism.
        assert r.status_code == 201, r.text
        assert r.headers["Docker-Content-Digest"] == digest

        async with _fresh_client(app) as c:
            got = await c.get(f"{V2}/{OTHER}/blobs/{digest}", headers=auth, follow_redirects=True)
        assert got.status_code == 200
        assert got.content == b"mountable-bytes"

        async with get_db_session() as db:
            blobs = await db.scalar(
                select(func.count()).select_from(OCIBlob).where(OCIBlob.digest == digest)
            )
        assert blobs == 1

    async def test_an_unmountable_blob_falls_back_to_a_normal_upload(self, app) -> None:
        """A mount of something the source does not have must not 404.

        The client is expected to treat the 202 as "upload it then" and carry
        on; answering with an error strands the push.
        """
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        async with _fresh_client(app) as c:
            r = await c.post(
                f"{V2}/{OTHER}/blobs/uploads/?mount=sha256:{'a' * 64}&from={REPO}",
                headers=auth,
            )
        assert r.status_code == 202
        assert "Location" in r.headers


class TestManifestsAndTags:
    async def _push_manifest(self, app, auth: dict, reference: str, body: bytes) -> None:
        async with _fresh_client(app) as c:
            r = await c.put(
                f"{V2}/{REPO}/manifests/{reference}",
                headers={**auth, "Content-Type": "application/vnd.oci.image.manifest.v1+json"},
                content=body,
            )
            assert r.status_code == 201, r.text

    async def test_retagging_moves_the_tag_rather_than_erroring(self, app) -> None:
        """Pushing `:latest` twice is the single most ordinary thing a user does.

        It hits a unique constraint on (repository, tag), so it only works if the
        write is an upsert. A plain insert passes every mocked test and fails the
        second time a human pushes.
        """
        import json

        from sqlalchemy import func, select

        from terrapod.db.models import OCITag

        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        first = json.dumps({"schemaVersion": 2, "layers": [], "annotations": {"v": "1"}}).encode()
        second = json.dumps({"schemaVersion": 2, "layers": [], "annotations": {"v": "2"}}).encode()

        await self._push_manifest(app, auth, "latest", first)
        await self._push_manifest(app, auth, "latest", second)

        async with _fresh_client(app) as c:
            got = await c.get(
                f"{V2}/{REPO}/manifests/latest",
                headers={**auth, "Accept": "application/vnd.oci.image.manifest.v1+json"},
            )
        assert got.status_code == 200
        assert json.loads(got.content)["annotations"]["v"] == "2"

        async with get_db_session() as db:
            tags = await db.scalar(select(func.count()).select_from(OCITag))
        assert tags == 1

        # The old manifest is still addressable by digest — retagging moves a
        # pointer, it does not delete content.
        old_digest = "sha256:" + hashlib.sha256(first).hexdigest()
        async with _fresh_client(app) as c:
            by_digest = await c.get(f"{V2}/{REPO}/manifests/{old_digest}", headers=auth)
        assert by_digest.status_code == 200

    async def test_a_manifest_referencing_an_absent_blob_is_rejected(self, app) -> None:
        """The check that stops a registry accumulating unpullable images."""
        import json

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        body = json.dumps(
            {
                "schemaVersion": 2,
                "layers": [
                    {
                        "digest": "sha256:" + "c" * 64,
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    }
                ],
            }
        ).encode()

        async with _fresh_client(app) as c:
            r = await c.put(f"{V2}/{REPO}/manifests/latest", headers=auth, content=body)
        assert r.status_code == 404
        assert r.json()["errors"][0]["code"] == "MANIFEST_BLOB_UNKNOWN"

    async def test_tag_listing_is_sorted_and_scoped_to_the_repository(self, app) -> None:
        import json

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        body = json.dumps({"schemaVersion": 2, "layers": []}).encode()

        for ref in ("v2", "v1", "v10"):
            await self._push_manifest(app, auth, ref, body)

        async with _fresh_client(app) as c:
            # A manifest in a second repository must not appear in this listing.
            await c.put(f"{V2}/{OTHER}/manifests/elsewhere", headers=auth, content=body)
            r = await c.get(f"{V2}/{REPO}/tags/list", headers=auth)

        assert r.status_code == 200
        payload = r.json()
        assert payload["name"] == REPO
        # Lexical order is what the spec asks for, so "v10" sorts before "v2".
        assert payload["tags"] == ["v1", "v10", "v2"]


class TestAbandonedUploadReaper:
    """A push that starts and dies must not leak its chunks forever.

    The spec asks a server to "eventually timeout unfinished uploads", and the
    consequence of not doing so is not untidiness: a client with push access can
    open sessions, write chunks, walk away, and repeat until the object store is
    full. The reaper is the control that closes that.
    """

    async def _stale_session(self, app, auth: dict, age_hours: int) -> tuple[str, str]:
        """Start an upload, write a chunk, then age it. Returns (id, key prefix)."""
        from datetime import timedelta

        from sqlalchemy import update

        from terrapod.db.models import OCIUploadSession, now_utc

        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{REPO}/blobs/uploads/", headers=auth)
            location = started.headers["Location"]
            await c.patch(location, headers={**auth, "Content-Range": "0-3"}, content=b"leak")

        session_id = location.rstrip("/").rsplit("/", 1)[-1]
        async with get_db_session() as db:
            await db.execute(
                update(OCIUploadSession)
                .where(OCIUploadSession.id == uuid.UUID(session_id))
                .values(updated_at=now_utc() - timedelta(hours=age_hours))
            )
        return session_id, location

    async def test_a_stale_session_and_its_chunks_are_reaped(self, app) -> None:
        from terrapod.services.oci.upload_service import reap_abandoned_sessions
        from terrapod.storage import get_storage, keys

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        session_id, location = await self._stale_session(app, auth, age_hours=48)

        # The chunk really is in storage before the reap, so a passing test
        # after it means something was actually deleted.
        storage = get_storage()
        before = await storage.list_prefix(keys.oci_upload_prefix(session_id))
        assert len(before) == 1

        assert await reap_abandoned_sessions() == 1

        after = await storage.list_prefix(keys.oci_upload_prefix(session_id))
        assert after == []
        async with _fresh_client(app) as c:
            assert (await c.get(location, headers=auth)).status_code == 404

    async def test_an_active_session_is_left_alone(self, app) -> None:
        """The failure that would matter most: reaping a push in progress.

        A slow upload over a poor link is legitimate, and destroying it would be
        worse than the leak the reaper exists to prevent.
        """
        from terrapod.services.oci.upload_service import reap_abandoned_sessions

        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        async with _fresh_client(app) as c:
            started = await c.post(f"{V2}/{REPO}/blobs/uploads/", headers=auth)
            location = started.headers["Location"]
            await c.patch(location, headers={**auth, "Content-Range": "0-3"}, content=b"live")

        assert await reap_abandoned_sessions() == 0

        async with _fresh_client(app) as c:
            status = await c.get(location, headers=auth)
        assert status.status_code == 204
        assert status.headers["Range"] == "0-3"

    async def test_two_replicas_reaping_at_once_do_not_error(self, app) -> None:
        """The scheduler's mutual exclusion is a claim, not a lock.

        A cycle that overran can overlap the next one, so a second reaper
        finding the row already gone must be a no-op rather than an exception —
        an exception here would leave the rest of the batch unreaped.
        """
        import asyncio

        from terrapod.services.oci.upload_service import reap_abandoned_sessions

        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        for _ in range(3):
            await self._stale_session(app, auth, age_hours=48)

        first, second = await asyncio.gather(
            reap_abandoned_sessions(),
            reap_abandoned_sessions(),
            return_exceptions=True,
        )
        for outcome in (first, second):
            assert not isinstance(outcome, Exception), outcome

        # Between them they accounted for all three, however the race fell out.
        assert await reap_abandoned_sessions() == 0


class TestReferrers:
    """Attachments — signatures, SBOMs, attestations — found by subject.

    This matters more here than in a general-purpose registry. Provenance for an
    air-gapped estate has to travel *with* the content, because nothing inside
    the network can reach out to check it; the referrers API is the mechanism
    that carries it.
    """

    async def _push(self, app, auth: dict, document: dict) -> str:
        import json

        body = json.dumps(document).encode()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        async with _fresh_client(app) as c:
            r = await c.put(
                f"{V2}/{REPO}/manifests/{digest}",
                headers={**auth, "Content-Type": "application/vnd.oci.image.manifest.v1+json"},
                content=body,
            )
            assert r.status_code == 201, r.text
        return digest

    async def test_attachments_are_listed_against_their_subject(self, app) -> None:
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        subject = await self._push(app, auth, {"schemaVersion": 2, "layers": []})
        for kind in ("application/spdx+json", "application/vnd.dev.cosign.simplesigning"):
            await self._push(
                app,
                auth,
                {
                    "schemaVersion": 2,
                    "layers": [],
                    "artifactType": kind,
                    "subject": {"digest": subject},
                    "annotations": {"org.example.kind": kind},
                },
            )

        # A manifest with no subject must not appear: it is an image in its own
        # right, not an attachment to anything.
        await self._push(app, auth, {"schemaVersion": 2, "layers": [], "annotations": {"a": "b"}})

        async with _fresh_client(app) as c:
            r = await c.get(f"{V2}/{REPO}/referrers/{subject}", headers=auth)

        assert r.status_code == 200
        index = r.json()
        assert len(index["manifests"]) == 2
        assert {m["artifactType"] for m in index["manifests"]} == {
            "application/spdx+json",
            "application/vnd.dev.cosign.simplesigning",
        }
        # Annotations ride along so a client can tell attachments apart without
        # fetching each one.
        assert all(m["annotations"] for m in index["manifests"])
        assert "OCI-Filters-Applied" not in r.headers

    async def test_the_artifact_type_filter_is_declared_when_applied(self, app) -> None:
        """A filtered list is worthless unless the server says it filtered.

        Without `OCI-Filters-Applied` a client cannot distinguish a filtered
        response from a registry that ignored the filter, so it has to filter
        again itself and can never trust what it got.
        """
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        subject = await self._push(app, auth, {"schemaVersion": 2, "layers": []})
        for kind in ("application/spdx+json", "application/other"):
            await self._push(
                app,
                auth,
                {
                    "schemaVersion": 2,
                    "layers": [],
                    "artifactType": kind,
                    "subject": {"digest": subject},
                },
            )

        async with _fresh_client(app) as c:
            r = await c.get(
                f"{V2}/{REPO}/referrers/{subject}?artifactType=application/spdx%2Bjson",
                headers=auth,
            )

        assert r.status_code == 200
        assert r.headers["OCI-Filters-Applied"] == "artifactType"
        assert [m["artifactType"] for m in r.json()["manifests"]] == ["application/spdx+json"]

    async def test_artifact_type_falls_back_to_the_config_media_type(self, app) -> None:
        """How most tooling actually marks an attachment's kind today.

        The spec directs the fallback for an image manifest that declares no
        `artifactType`; without it the filter matches almost nothing in practice.
        """
        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        subject = await self._push(app, auth, {"schemaVersion": 2, "layers": []})
        await self._push(
            app,
            auth,
            {
                "schemaVersion": 2,
                "layers": [],
                "config": {"mediaType": "application/vnd.example.sbom"},
                "subject": {"digest": subject},
            },
        )

        async with _fresh_client(app) as c:
            r = await c.get(f"{V2}/{REPO}/referrers/{subject}", headers=auth)

        assert [m["artifactType"] for m in r.json()["manifests"]] == [
            "application/vnd.example.sbom"
        ]

    async def test_a_subject_with_nothing_attached_is_an_empty_index_not_a_404(self, app) -> None:
        auth = {"Authorization": await _token()}
        await _seed_admin_role()
        subject = await self._push(app, auth, {"schemaVersion": 2, "layers": []})

        async with _fresh_client(app) as c:
            r = await c.get(f"{V2}/{REPO}/referrers/{subject}", headers=auth)

        assert r.status_code == 200
        assert r.json()["manifests"] == []

    async def test_referrers_are_scoped_to_their_repository(self, app) -> None:
        """An attachment in one repository must not surface in another.

        Manifests are repository-scoped, and leaking them across would expose
        the existence and metadata of another repository's content.
        """
        import json

        auth = {"Authorization": await _token()}
        await _seed_admin_role()

        subject = await self._push(app, auth, {"schemaVersion": 2, "layers": []})
        await self._push(
            app,
            auth,
            {"schemaVersion": 2, "layers": [], "subject": {"digest": subject}},
        )

        async with _fresh_client(app) as c:
            await c.put(
                f"{V2}/{OTHER}/manifests/seed",
                headers=auth,
                content=json.dumps({"schemaVersion": 2, "layers": []}).encode(),
            )
            r = await c.get(f"{V2}/{OTHER}/referrers/{subject}", headers=auth)

        assert r.status_code == 200
        assert r.json()["manifests"] == []
