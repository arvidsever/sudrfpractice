"""Токены приложения Sudrf (sudrfpractice#5): регистрация установки по
записи App Store, Bearer-доступ к API, лимит, отзыв и команды владельца.

Настоящую запись Apple в тесте не подписать, поэтому цепочка своя, той же
формы: корень ← промежуточный с маркером Apple ← лист с маркером App Store.
Проверка корня по отпечатку подменяется отпечатком тестового корня.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from harvester.web import __main__ as cli  # noqa: E402
from harvester.web import appstore, create_app  # noqa: E402
from harvester.web import auth as authlib  # noqa: E402
from harvester.web import tokens as tokenlib  # noqa: E402

NOW = datetime.now(UTC)


def _cert(name, key, issuer_name, issuer_key, oid=None, ca=False):
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if oid is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(oid, b"\x05\x00"), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


class Chain:
    def __init__(self) -> None:
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.mid_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _cert("Test Root", self.root_key, "Test Root", self.root_key, ca=True)
        self.mid = _cert(
            "Test WWDR",
            self.mid_key,
            "Test Root",
            self.root_key,
            appstore.INTERMEDIATE_OID,
            ca=True,
        )
        self.leaf = _cert(
            "Test App Store", self.leaf_key, "Test WWDR", self.mid_key, appstore.LEAF_OID
        )

    def der(self, cert) -> bytes:
        return cert.public_bytes(serialization.Encoding.DER)

    @property
    def root_sha256(self) -> str:
        return hashlib.sha256(self.der(self.root)).hexdigest()

    def jws(self, **overrides) -> str:
        payload = {
            "bundleId": "ru.sudrf.app",
            "receiptType": "Sandbox",
            "appTransactionId": "tester-1",
            "appAppleId": 123,
            "signedDate": int(time.time() * 1000),
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        header = {
            "alg": "ES256",
            "x5c": [
                base64.b64encode(self.der(c)).decode() for c in (self.leaf, self.mid, self.root)
            ],
        }

        def part(obj) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        signing = f"{part(header)}.{part(payload)}"
        r, s = decode_dss_signature(self.leaf_key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        signature = base64.urlsafe_b64encode(r.to_bytes(32) + s.to_bytes(32)).rstrip(b"=").decode()
        return f"{signing}.{signature}"


@pytest.fixture
def chain() -> Chain:
    return Chain()


def _policy(chain: Chain, **overrides) -> appstore.Policy:
    values = {
        "bundle_id": "ru.sudrf.app",
        "environments": frozenset({"Sandbox", "Production"}),
        "root_sha256": chain.root_sha256,
    }
    values.update(overrides)
    return appstore.Policy(**values)


def _app(tmp_path, chain, *, registration=True, limiter=None):
    store = tokenlib.Store(tmp_path / "tokens.sqlite3")
    guard = authlib.Auth(authlib.hash_password("correct horse battery"), "k" * 40)
    app = create_app(
        create_engine("sqlite://"),
        auth=guard,
        tokens=store,
        registration=_policy(chain) if registration else None,
        limiter=limiter,
    )
    return TestClient(app), store


def _register(client, jws):
    return client.post("/api/register", json={"app_transaction": jws})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# — регистрация —


def test_registered_installation_reaches_the_api_with_its_token(tmp_path, chain) -> None:
    client, store = _app(tmp_path, chain)

    response = _register(client, chain.jws())

    assert response.status_code == 200
    token = response.json()["token"]
    assert client.get("/api/session").status_code == 401, "без токена и cookie закрыто"
    assert client.get("/api/session", headers=_bearer(token)).status_code == 200
    [saved] = store.list()
    assert (saved.kind, saved.holder, saved.environment) == ("install", "tester-1", "Sandbox")
    assert token.encode() not in (tmp_path / "tokens.sqlite3").read_bytes(), "в файле только хеш"


def test_a_tester_gets_at_most_three_installations(tmp_path, chain) -> None:
    client, store = _app(tmp_path, chain)

    for _ in range(tokenlib.MAX_PER_HOLDER):
        assert _register(client, chain.jws()).status_code == 200
    extra = _register(client, chain.jws())

    assert extra.status_code == 409 and "Apple ID" in extra.json()["detail"]
    assert _register(client, chain.jws(appTransactionId="tester-2")).status_code == 200
    store.revoke_holder("tester-1")
    assert _register(client, chain.jws()).status_code == 200, "после отзыва место освобождается"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bundleId": "com.other.app"}, "другого приложения"),
        ({"receiptType": "Xcode"}, "среды"),
        ({"appTransactionId": None}, "идентификатора загрузки"),
        ({"signedDate": None}, "даты подписи"),
    ],
)
def test_foreign_or_incomplete_records_are_refused(tmp_path, chain, overrides, reason) -> None:
    client, store = _app(tmp_path, chain)

    response = _register(client, chain.jws(**overrides))

    assert response.status_code == 403 and reason in response.json()["detail"]
    assert store.list() == []


def test_forged_signature_and_foreign_root_are_refused(tmp_path, chain) -> None:
    client, _ = _app(tmp_path, chain)
    head, payload, signature = chain.jws().split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["appTransactionId"] = "someone-else"
    forged_b64 = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()

    assert _register(client, f"{head}.{forged_b64}.{signature}").status_code == 403

    other = Chain()
    response = _register(client, other.jws())
    assert response.status_code == 403 and "не от Apple" in response.json()["detail"]


def test_chain_without_app_store_markers_is_refused(chain) -> None:
    unmarked = Chain()
    unmarked.leaf = _cert("Test Leaf", unmarked.leaf_key, "Test WWDR", unmarked.mid_key)
    with pytest.raises(appstore.Rejected, match="App Store"):
        appstore.verify(unmarked.jws(), _policy(unmarked))


def test_registration_is_off_unless_enabled(tmp_path, chain) -> None:
    client, _ = _app(tmp_path, chain, registration=False)

    response = _register(client, chain.jws())

    assert response.status_code == 403 and "выключена" in response.json()["detail"]
    from harvester.web import _registration_from_env

    assert _registration_from_env({}) is None, "по умолчанию тестеры не регистрируются"
    policy = _registration_from_env({"SUDRF_WEB_APP_REGISTRATION": "1"})
    assert policy.bundle_id == "ru.sudrf.app"
    assert policy.environments == frozenset({"Sandbox", "Production"})
    assert policy.root_sha256 == appstore.APPLE_ROOT_G3_SHA256


def test_malformed_body_is_a_bad_request(tmp_path, chain) -> None:
    client, _ = _app(tmp_path, chain)

    assert client.post("/api/register", content=b"not json").status_code == 400
    assert client.post("/api/register", json={"other": 1}).status_code == 400
    assert _register(client, "a.b").status_code == 403


# — токены —


def test_revoked_token_is_refused_at_once(tmp_path, chain) -> None:
    client, store = _app(tmp_path, chain)
    token = _register(client, chain.jws()).json()["token"]
    [saved] = store.list()

    store.revoke(saved.id)

    response = client.get("/api/session", headers=_bearer(token))
    assert response.status_code == 401 and response.json()["detail"] == "Токен не принят."


def test_rate_limit_is_per_token_and_says_when_to_retry(tmp_path, chain) -> None:
    client, store = _app(tmp_path, chain, limiter=tokenlib.Limiter(per_minute=2, concurrent=2))
    first = _register(client, chain.jws()).json()["token"]
    second = _register(client, chain.jws(appTransactionId="tester-2")).json()["token"]

    for _ in range(2):
        assert client.get("/api/session", headers=_bearer(first)).status_code == 200
    limited = client.get("/api/session", headers=_bearer(first))

    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert client.get("/api/session", headers=_bearer(second)).status_code == 200
    counts = {t.holder: t.used_count for t in store.list()}
    assert counts == {"tester-1": 3, "tester-2": 1}, "считаются запросы, слова не пишутся"


def test_guessing_tokens_counts_toward_the_address_block(tmp_path, chain) -> None:
    client, _ = _app(tmp_path, chain)
    token = _register(client, chain.jws()).json()["token"]

    for _ in range(5):
        assert client.get("/api/session", headers=_bearer("guess-wrong-token")).status_code == 401
    blocked = client.get("/api/session", headers=_bearer(token))

    assert blocked.status_code == 429, "после пяти промахов адрес ждёт и с верным токеном"


def test_owner_token_from_the_command_line(tmp_path, chain, capsys) -> None:
    path = str(tmp_path / "tokens.sqlite3")

    assert cli.main(["tokens", "--store", path, "issue", "owner"]) == 0
    token = capsys.readouterr().out.strip()
    client, _ = _app(tmp_path, chain)
    assert client.get("/api/session", headers=_bearer(token)).status_code == 200

    assert cli.main(["tokens", "--store", path, "list"]) == 0
    listing = capsys.readouterr().out
    assert "owner" in listing and token not in listing, "список не показывает токены"

    assert cli.main(["tokens", "--store", path, "revoke", "1"]) == 0
    assert client.get("/api/session", headers=_bearer(token)).status_code == 401
    assert cli.main(["tokens", "--store", path, "revoke", "1"]) == 1


def test_limiter_window_and_concurrency() -> None:
    limiter = tokenlib.Limiter(per_minute=2, concurrent=1)

    assert limiter.acquire(1, now=100.0) is None
    assert limiter.acquire(1, now=100.5) == 1, "второй одновременный ждёт"
    limiter.release(1)
    assert limiter.acquire(1, now=101.0) is None
    limiter.release(1)
    assert limiter.acquire(1, now=110.0) == 51, "окно — минута с первого запроса"
    assert limiter.acquire(1, now=161.0) is None
