"""Real, signed SAML assertions for the connector tests.

Everything here is genuine: a throwaway RSA keypair, a self-signed certificate,
and XML signed through python3-saml's own `add_sign`. Nothing is stubbed, so a
test that says "this assertion is accepted" has actually run it through
python3-saml's validation with a signature that verifies.

That matters more than usual for SAML. A hand-mocked `OneLogin_Saml2_Auth` would
happily report whatever the test told it to, and every one of the defects these
tests pin lives *inside* that validation — so a mocked test would pass against
the broken code and against the fixed code alike.
"""

from __future__ import annotations

import base64
import datetime
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from onelogin.saml2.constants import OneLogin_Saml2_Constants as K
from onelogin.saml2.utils import OneLogin_Saml2_Utils

IDP_ENTITY_ID = "https://idp.test/metadata"
SP_ENTITY_ID = "https://terrapod.example.com"
ACS_URL = "https://terrapod.example.com/api/terrapod/v1/auth/saml/acs"
OTHER_SP_ACS_URL = "https://someone-elses-sp.example.net/api/terrapod/v1/auth/saml/acs"

_NS = (
    'xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
    'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
)


def make_idp_keypair() -> tuple[str, str]:
    """A throwaway RSA key + self-signed cert standing in for the IDP's."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def idp_metadata(cert_pem: str) -> dict:
    """What `parse_remote` would have returned, without the network call."""
    return {
        "idp": {
            "entityId": IDP_ENTITY_ID,
            "singleSignOnService": {
                "url": "https://idp.test/sso",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": cert_pem,
        }
    }


def _stamp(offset_seconds: int) -> str:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=offset_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assertion(
    *,
    recipient: str,
    audience: str,
    in_response_to: str,
    assertion_id: str,
    lifetime: int,
    standalone: bool,
) -> str:
    irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    ns = _NS if standalone else ""
    return f"""<saml:Assertion {ns} xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" ID="{assertion_id}" Version="2.0" IssueInstant="{_stamp(0)}">
    <saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">user@example.com</saml:NameID>
      <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml:SubjectConfirmationData NotOnOrAfter="{_stamp(lifetime)}" Recipient="{recipient}"{irt}/>
      </saml:SubjectConfirmation>
    </saml:Subject>
    <saml:Conditions NotBefore="{_stamp(-60)}" NotOnOrAfter="{_stamp(lifetime)}">
      <saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
    </saml:Conditions>
    <saml:AuthnStatement AuthnInstant="{_stamp(0)}" SessionIndex="{assertion_id}">
      <saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext>
    </saml:AuthnStatement>
    <saml:AttributeStatement>
      <saml:Attribute Name="email" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic"><saml:AttributeValue xsi:type="xs:string">user@example.com</saml:AttributeValue></saml:Attribute>
      <saml:Attribute Name="displayName" NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic"><saml:AttributeValue xsi:type="xs:string">Test User</saml:AttributeValue></saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>"""


def saml_response(
    key_pem: str,
    cert_pem: str,
    *,
    destination: str = ACS_URL,
    recipient: str = ACS_URL,
    audience: str = SP_ENTITY_ID,
    in_response_to: str = "ONELOGIN_request1",
    response_in_response_to: str | None = "__mirror__",
    assertion_id: str | None = None,
    lifetime: int = 300,
    sign_assertion: bool = True,
    sign_response: bool = False,
    sign_algorithm: str = K.RSA_SHA256,
    digest_algorithm: str = K.SHA256,
) -> tuple[str, str]:
    """A base64 SAMLResponse plus the assertion id inside it.

    By default the ASSERTION carries the signature, which is what a hardened SP
    requires. `sign_assertion=False, sign_response=True` produces the shape a
    message-only-signing IDP emits.
    """
    assertion_id = assertion_id or "_a" + uuid.uuid4().hex
    response_id = "_r" + uuid.uuid4().hex

    body = _assertion(
        recipient=recipient,
        audience=audience,
        in_response_to=in_response_to,
        assertion_id=assertion_id,
        lifetime=lifetime,
        standalone=sign_assertion,
    )
    if sign_assertion:
        signed = OneLogin_Saml2_Utils.add_sign(
            body,
            key_pem,
            cert_pem,
            sign_algorithm=sign_algorithm,
            digest_algorithm=digest_algorithm,
        )
        if isinstance(signed, bytes):
            signed = signed.decode()
        body = signed.replace('<?xml version="1.0"?>', "").strip()

    rirt = in_response_to if response_in_response_to == "__mirror__" else response_in_response_to
    response_irt = f' InResponseTo="{rirt}"' if rirt else ""
    xml = f"""<samlp:Response {_NS} ID="{response_id}" Version="2.0" IssueInstant="{_stamp(0)}" Destination="{destination}"{response_irt}>
  <saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  {body}
</samlp:Response>"""

    if sign_response:
        xml = OneLogin_Saml2_Utils.add_sign(
            xml,
            key_pem,
            cert_pem,
            sign_algorithm=sign_algorithm,
            digest_algorithm=digest_algorithm,
        )
    if isinstance(xml, bytes):
        xml = xml.decode()
    return base64.b64encode(xml.encode()).decode(), assertion_id
