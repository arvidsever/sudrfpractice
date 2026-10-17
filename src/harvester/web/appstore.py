"""Проверка подписанной Apple записи `AppTransaction` (sudrfpractice#5).

Приложение Sudrf из TestFlight или App Store шлёт JWS своей записи о
загрузке; если подпись ведёт к корню Apple, а запись — про наше приложение,
установка получает токен (`tokens.py`). Это подтверждает установку, а не
человека: кто поставил сборку по ссылке TestFlight, тот и получит доступ.

Что проверяется — то же, что в `SignedDataVerifier` Apple
(app-store-server-library), но без библиотеки: она тянет за собой клиента
App Store Server API, которого здесь нет. Хватает `cryptography`:

* цепочка `x5c` — лист ← промежуточный ← корень, каждый подписан следующим;
* корень — ровно Apple Root CA - G3 (по отпечатку SHA-256 файла с
  apple.com/certificateauthority, сверен 17.10.2026);
* у листа и промежуточного — расширения-маркеры Apple для App Store
  (OID 1.2.840.113635.100.6.11.1 и 1.2.840.113635.100.6.2.1);
* сертификаты действуют на момент подписи (`signedDate`);
* подпись ES256 над `header.payload`;
* `bundleId`, среда (`receiptType`) и, если задан, `appAppleId`.

Отзыв сертификатов (OCSP) не проверяется: на этом масштабе он дороже
пользы, а доступ установки отзывается здесь же, по токену.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

#: SHA-256 от DER Apple Root CA - G3
#: (https://www.apple.com/certificateauthority/AppleRootCA-G3.cer).
APPLE_ROOT_G3_SHA256 = "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"

#: Маркер листового сертификата подписи App Store.
LEAF_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
#: Маркер промежуточного сертификата Apple Worldwide Developer Relations.
INTERMEDIATE_OID = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")

#: Предел размера JWS: настоящая запись — около 4 КБ с цепочкой.
MAX_JWS = 32 * 1024


class Rejected(ValueError):
    """Запись не прошла проверку; текст — для `detail` ответа, по-русски."""


@dataclass(frozen=True, slots=True)
class Transaction:
    #: Постоянный для пары «Apple ID + приложение» идентификатор — по нему
    #: считаются установки одного тестера.
    holder: str
    environment: str
    bundle_id: str


@dataclass(frozen=True, slots=True)
class Policy:
    bundle_id: str
    environments: frozenset[str]
    app_apple_id: int | None = None
    root_sha256: str = APPLE_ROOT_G3_SHA256
    leaf_oid: x509.ObjectIdentifier = LEAF_OID
    intermediate_oid: x509.ObjectIdentifier = INTERMEDIATE_OID


def _b64url(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as exc:
        raise Rejected("Запись App Store повреждена.") from exc


def _verify_signed_by(child: x509.Certificate, parent: x509.Certificate) -> None:
    key = parent.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise Rejected("Цепочка сертификатов записи не от Apple.")
    try:
        key.verify(
            child.signature, child.tbs_certificate_bytes, ec.ECDSA(child.signature_hash_algorithm)
        )
    except InvalidSignature as exc:
        raise Rejected("Цепочка сертификатов записи не сходится.") from exc


def _has(cert: x509.Certificate, oid: x509.ObjectIdentifier) -> bool:
    try:
        cert.extensions.get_extension_for_oid(oid)
    except x509.ExtensionNotFound:
        return False
    return True


def verify(jws: str, policy: Policy) -> Transaction:
    """Проверить JWS `AppTransaction`; при любой неувязке — `Rejected`."""
    if not isinstance(jws, str) or len(jws) > MAX_JWS or jws.count(".") != 2:
        raise Rejected("Запись App Store повреждена.")
    head_b64, payload_b64, signature_b64 = jws.split(".")
    try:
        header = json.loads(_b64url(head_b64))
        payload = json.loads(_b64url(payload_b64))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Rejected("Запись App Store повреждена.") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise Rejected("Запись App Store повреждена.")
    if header.get("alg") != "ES256":
        raise Rejected("Запись подписана не так, как подписывает Apple.")

    chain = header.get("x5c")
    if not isinstance(chain, list) or len(chain) != 3:
        raise Rejected("В записи нет цепочки сертификатов Apple.")
    try:
        ders = [base64.b64decode(item, validate=True) for item in chain]
        leaf, intermediate, root = (x509.load_der_x509_certificate(d) for d in ders)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise Rejected("В записи нет цепочки сертификатов Apple.") from exc

    if hashlib.sha256(ders[2]).hexdigest() != policy.root_sha256:
        raise Rejected("Цепочка сертификатов записи не от Apple.")
    if not _has(leaf, policy.leaf_oid) or not _has(intermediate, policy.intermediate_oid):
        raise Rejected("Цепочка сертификатов записи не от App Store.")
    _verify_signed_by(leaf, intermediate)
    _verify_signed_by(intermediate, root)

    signed_ms = payload.get("signedDate")
    if not isinstance(signed_ms, int | float):
        raise Rejected("В записи нет даты подписи.")
    signed = datetime.fromtimestamp(signed_ms / 1000, tz=UTC)
    for cert in (leaf, intermediate, root):
        if not cert.not_valid_before_utc <= signed <= cert.not_valid_after_utc:
            raise Rejected("Сертификат записи не действовал в момент подписи.")

    raw = _b64url(signature_b64)
    if len(raw) != 64:
        raise Rejected("Подпись записи повреждена.")
    der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    leaf_key = leaf.public_key()
    if not isinstance(leaf_key, ec.EllipticCurvePublicKey):
        raise Rejected("Цепочка сертификатов записи не от Apple.")
    try:
        leaf_key.verify(der, f"{head_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise Rejected("Подпись записи не прошла проверку.") from exc

    if payload.get("bundleId") != policy.bundle_id:
        raise Rejected("Запись выдана для другого приложения.")
    environment = payload.get("receiptType") or payload.get("environment")
    if environment not in policy.environments:
        raise Rejected("Сборки из этой среды к серверу не подключаются.")
    if policy.app_apple_id is not None and payload.get("appAppleId") != policy.app_apple_id:
        raise Rejected("Запись выдана для другого приложения.")
    holder = payload.get("appTransactionId")
    if not isinstance(holder, str) or not holder:
        # Без постоянного идентификатора нельзя считать установки тестера.
        raise Rejected("В записи нет идентификатора загрузки; обновите macOS.")
    return Transaction(holder=holder, environment=str(environment), bundle_id=policy.bundle_id)
