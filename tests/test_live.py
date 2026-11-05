import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_maker.live import AuthKalshi, order_body


def test_rsa_signature_verifies_over_prefixed_path_without_query():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    k = AuthKalshi("id", pem)
    sig = base64.b64decode(k.sign("1700000000000", "get", "/portfolio/balance?x=1"))
    key.public_key().verify(
        sig, b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256(),
    )


def test_order_body_is_post_only_resting():
    b = order_body("T", "ask", 0.45, 100)
    assert b["price"] == "0.4500" and b["count"] == "100.00"
    assert b["post_only"] and b["time_in_force"] == "good_till_canceled"
