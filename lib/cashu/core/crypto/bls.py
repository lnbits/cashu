# Don't trust me with cryptography.

"""
Implementation of https://gist.github.com/RubenSomsen/be7a4760dd4596d06963d67baf140406

Bob (Mint):
A = a*G
return A

Alice (Client):
Y = hash_to_curve(secret_message)
r = random blinding factor
B'= Y + r*G
return B'

Bob:
C' = a*B'
  (= a*Y + a*r*G)
return C'

Alice:
C = C' - r*A
 (= C' - a*r*G)
 (= a*Y)
return C, secret_message

Bob:
Y = hash_to_curve(secret_message)
C == a*Y
If true, C must have originated from Bob


# DLEQ Proof

(These steps occur once Bob returns C')

Bob:
r = random nonce
R1 = r*G
R2 = r*B'
e = hash(R1,R2,A,C')
s = r + e*a
return e, s

Alice:
R1 = s*G - e*A
R2 = s*B' - e*C'
e == hash(R1,R2,A,C')

If true, a in A = a*G must be equal to a in C' = a*B'
"""

import hashlib
from typing import Optional, Tuple

from blspy import AugSchemeMPL, G1Element, G2Element, PrivateKey

# from py_ecc.bls import G2ProofOfPossession as bls_pop
from secp256k1 import PrivateKey, PublicKey


def hash_to_curve(message: bytes) -> PublicKey:
    """Generates a point from the message hash and checks if the point lies on the curve.
    If it does not, iteratively tries to compute a new point from the hash."""
    point = None
    msg_to_hash = message
    while point is None:
        _hash = hashlib.sha256(msg_to_hash).digest()
        try:
            # will error if point does not lie on curve
            point = PublicKey(b"\x02" + _hash, raw=True)
        except Exception:
            msg_to_hash = _hash
    return point


def step1_alice(
    secret_msg: str, blinding_factor: Optional[PrivateKey] = None
) -> tuple[PublicKey, PrivateKey]:
    Y: PublicKey = hash_to_curve(secret_msg.encode("utf-8"))
    r = blinding_factor or PrivateKey()
    B_: PublicKey = Y + r.pubkey  # type: ignore
    return B_, r


def step2_bob(B_: PublicKey, a: PrivateKey) -> Tuple[PublicKey, PrivateKey, PrivateKey]:
    C_: PublicKey = B_.mult(a)  # type: ignore
    # produce dleq proof
    e, s = step2_bob_dleq(B_, a)
    return C_, e, s


def step3_alice(C_: PublicKey, r: PrivateKey, A: PublicKey) -> PublicKey:
    C: PublicKey = C_ - A.mult(r)  # type: ignore
    return C


def verify(a: PrivateKey, C: PublicKey, secret_msg: str) -> bool:
    Y: PublicKey = hash_to_curve(secret_msg.encode("utf-8"))
    return C == Y.mult(a)  # type: ignore


def hash_e(*publickeys: PublicKey) -> bytes:
    e_ = ""
    for p in publickeys:
        _p = p.serialize(compressed=False).hex()
        e_ += str(_p)
    e = hashlib.sha256(e_.encode("utf-8")).digest()
    return e


def step2_bob_dleq(
    B_: PublicKey, a: PrivateKey, p_bytes: bytes = b""
) -> Tuple[PrivateKey, PrivateKey]:
    if p_bytes:
        # deterministic p for testing
        p = PrivateKey(privkey=p_bytes, raw=True)
    else:
        # normally, we generate a random p
        p = PrivateKey()

    R1 = p.pubkey  # R1 = pG
    assert R1
    R2: PublicKey = B_.mult(p)  # R2 = pB_ # type: ignore
    C_: PublicKey = B_.mult(a)  # C_ = aB_ # type: ignore
    A = a.pubkey
    assert A
    e = hash_e(R1, R2, A, C_)  # e = hash(R1, R2, A, C_)
    s = p.tweak_add(a.tweak_mul(e))  # s = p + ek
    spk = PrivateKey(s, raw=True)
    epk = PrivateKey(e, raw=True)
    return epk, spk


def alice_verify_dleq(
    B_: PublicKey, C_: PublicKey, e: PrivateKey, s: PrivateKey, A: PublicKey
) -> bool:
    R1 = s.pubkey - A.mult(e)  # type: ignore
    R2 = B_.mult(s) - C_.mult(e)  # type: ignore
    e_bytes = e.private_key
    return e_bytes == hash_e(R1, R2, A, C_)


def carol_verify_dleq(
    secret_msg: str,
    r: PrivateKey,
    C: PublicKey,
    e: PrivateKey,
    s: PrivateKey,
    A: PublicKey,
) -> bool:
    Y: PublicKey = hash_to_curve(secret_msg.encode("utf-8"))
    C_: PublicKey = C + A.mult(r)  # type: ignore
    B_: PublicKey = Y + r.pubkey  # type: ignore
    return alice_verify_dleq(B_, C_, e, s, A)


# Below is a test of a simple positive and negative case

# # Alice's keys
# a = PrivateKey()
# A = a.pubkey
# secret_msg = "test"
# B_, r = step1_alice(secret_msg)
# C_ = step2_bob(B_, a)
# C = step3_alice(C_, r, A)
# print("C:{}, secret_msg:{}".format(C, secret_msg))
# assert verify(a, C, secret_msg)
# assert verify(a, C + C, secret_msg) == False  # adding C twice shouldn't pass
# assert verify(a, A, secret_msg) == False  # A shouldn't pass

# # Test operations
# b = PrivateKey()
# B = b.pubkey
# assert -A -A + A == -A  # neg
# assert B.mult(a) == A.mult(b)  # a*B = A*b


import time

# private_key = 5566
# now = time.time()
# print("generating key")
# public_key = bls_pop.SkToPk(private_key)
# print(f"took {time.time()-now}")
# message = b"\xab" * 32  # The message to be signed

# # Signing
# print("Signing")
# now = time.time()

# signature = bls_pop.Sign(private_key, message)
# print(f"took {time.time()-now}")
# print("verifying")
# # Verifying
# now = time.time()

# assert bls_pop.Verify(public_key, message, signature)
# print("done")
# print(f"took {time.time()-now}")


# Example seed, used to generate private key. Always use
# a secure RNG with sufficient entropy to generate a seed (at least 32 bytes).
seed: bytes = bytes([
    0,
    50,
    6,
    244,
    24,
    199,
    1,
    25,
    52,
    88,
    192,
    19,
    18,
    12,
    89,
    6,
    220,
    18,
    102,
    58,
    209,
    82,
    12,
    62,
    89,
    110,
    182,
    9,
    44,
    20,
    254,
    22,
])
sk: PrivateKey = AugSchemeMPL.key_gen(seed)
pk: G1Element = sk.get_g1()  # type: ignore

message: bytes = bytes([1, 2, 3, 4, 5])  # type: ignore
print("signing")
now = time.time()

signature: G2Element = AugSchemeMPL.sign(sk, message)  # type: ignore
print(f"took {time.time()-now}")

print("verifying")
now = time.time()
# Verify the signature
ok: bool = AugSchemeMPL.verify(pk, message, signature)
assert ok
print(f"took {time.time()-now}")
