from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    grizzly_api_key: str = ""
    smspool_api_key: str = ""
    fivesim_api_key: str = ""
    onlinesim_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./shifr.db"
    # домен сайта (DOMAIN в .env, тот же берёт Caddy): все адреса бота — от него. Пусто — локальный запуск
    domain: str = ""
    grizzly_webhook_secret: str
    smspool_webhook_secret: str
    onlinesim_webhook_secret: str = ""
    # Platega — приём платежей (пополнение баланса). Пусто — пополнение недоступно
    platega_merchant_id: str = ""
    platega_api_key: str = ""
    # кошелёк системы USDT TRC20 (app/tron.py): закрытый ключ (hex) — только здесь, в .env.
    # Пусто — кошелька нет, выводы и пополнение площадок недоступны
    wallet_private_key: str = ""
    tron_network: str = "mainnet"  # mainnet | nile (тестовая сеть)
    trongrid_api_key: str = ""  # бесплатный ключ trongrid.io — без него запросы к сети сильно ограничены
    # проверка номеров Telegram (app/tgcheck.py): приложение с my.telegram.org → API development tools
    tg_api_id: int = 0
    tg_api_hash: str = ""
    # старый общий вход admin — работает, пока оба владельца не привязали аутентификатор (app/owners.py)
    admin_username: str = "admin"
    admin_password: str = ""
    # адрес админки: https://сайт/<admin_path> — на сервере случайная строка, чтобы страницу входа
    # не находили перебором очевидных адресов
    admin_path: str = "admin"
    session_secret: str
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    @property
    def public_url(self) -> str:
        domain = self.domain.strip().removeprefix("https://").removeprefix("http://").strip("/")
        return f"https://{domain}" if domain else f"http://localhost:{self.web_port}"

    @property
    def admin_url_path(self) -> str:
        return "/" + self.admin_path.strip("/")

    @property
    def grizzly_webhook_url(self) -> str:
        return f"{self.public_url}/webhooks/grizzly/{self.grizzly_webhook_secret}"

    @property
    def smspool_webhook_url(self) -> str:
        return f"{self.public_url}/webhooks/smspool/{self.smspool_webhook_secret}"

    @property
    def onlinesim_webhook_url(self) -> str:
        return f"{self.public_url}/webhooks/onlinesim/{self.onlinesim_webhook_secret}"


config = Config()
