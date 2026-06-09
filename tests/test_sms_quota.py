"""
Tests du quota SMS par numéro (exigence Termii : 2 envois max / numéro / 24 h).

Prérequis : Redis local sur localhost:6379 (comme test_phases.py).
Lancer : python tests/test_sms_quota.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services import sms_quota_service as quota

PHONE = "+221700000099"


async def flush(phone: str):
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    cooldown_key, daily_key = quota._keys(phone)
    await r.delete(cooldown_key, daily_key)
    await r.aclose()


async def test_daily_limit_blocks_third_send():
    settings.SMS_PHONE_COOLDOWN_SECONDS = 0
    settings.SMS_MAX_PER_PHONE_PER_DAY = 2
    await flush(PHONE)

    d1 = await quota.reserve_send(PHONE)
    d2 = await quota.reserve_send(PHONE)
    d3 = await quota.reserve_send(PHONE)
    assert d1.allowed and d2.allowed, "les 2 premiers envois doivent passer"
    assert not d3.allowed and d3.reason == "daily_limit", d3
    assert 0 < d3.retry_after <= quota.DAY_SECONDS
    print("  ✓ 3e envoi vers le même numéro bloqué (plafond 24 h)")


async def test_release_refunds_failed_send():
    settings.SMS_PHONE_COOLDOWN_SECONDS = 0
    settings.SMS_MAX_PER_PHONE_PER_DAY = 2
    await flush(PHONE)

    assert (await quota.reserve_send(PHONE)).allowed
    assert (await quota.reserve_send(PHONE)).allowed
    await quota.release_send(PHONE)  # échec fournisseur simulé
    d = await quota.reserve_send(PHONE)
    assert d.allowed, "le créneau rendu doit être réutilisable"
    print("  ✓ échec fournisseur : le créneau est rendu, quota non consommé")


async def test_cooldown_blocks_double_tap():
    settings.SMS_PHONE_COOLDOWN_SECONDS = 60
    settings.SMS_MAX_PER_PHONE_PER_DAY = 2
    await flush(PHONE)

    d1 = await quota.reserve_send(PHONE)
    d2 = await quota.reserve_send(PHONE)
    assert d1.allowed
    assert not d2.allowed and d2.reason == "cooldown", d2
    assert 0 < d2.retry_after <= 60
    print("  ✓ double envoi immédiat bloqué (cooldown)")


async def test_phone_formatting_shares_quota():
    settings.SMS_PHONE_COOLDOWN_SECONDS = 0
    settings.SMS_MAX_PER_PHONE_PER_DAY = 2
    await flush(PHONE)

    assert (await quota.reserve_send("+221 70 000 00 99")).allowed
    assert (await quota.reserve_send("+221-70-000-00-99")).allowed
    d = await quota.reserve_send(PHONE)
    assert not d.allowed, "les variantes de format doivent partager le quota"
    print("  ✓ formats équivalents du numéro partagent le même quota")


async def main():
    print("── Quota SMS par numéro (Termii : 2 / 24 h) ──")
    await test_daily_limit_blocks_third_send()
    await test_release_refunds_failed_send()
    await test_cooldown_blocks_double_tap()
    await test_phone_formatting_shares_quota()
    await flush(PHONE)
    print("\n✅ All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
