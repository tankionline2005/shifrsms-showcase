from app import settings_store as st
from app.config import config
from app.providers.base import Provider
from app.providers.fivesim import FiveSim
from app.providers.grizzly import Grizzly
from app.providers.onlinesim import OnlineSim
from app.providers.smspool import SmsPool

PROVIDERS: dict[str, Provider] = {}
if config.grizzly_api_key:
    PROVIDERS["grizzly"] = Grizzly(config.grizzly_api_key)
if config.smspool_api_key:
    PROVIDERS["smspool"] = SmsPool(config.smspool_api_key)
if config.fivesim_api_key:
    PROVIDERS["fivesim"] = FiveSim(config.fivesim_api_key)
if config.onlinesim_api_key:
    PROVIDERS["onlinesim"] = OnlineSim(config.onlinesim_api_key)


def enabled() -> dict[str, Provider]:
    return {k: p for k, p in PROVIDERS.items() if st.bool_(f"{k}_enabled")}
