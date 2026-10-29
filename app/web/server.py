from urllib.parse import quote_plus

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.config import config
from app.web import admin, landing, localtime, referral, support_guide, webhooks
from app.web.shop import api as shop_api
from app.web.shop import views as shop_views


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret,
                       session_cookie="shifr_admin", max_age=7 * 24 * 3600,
                       https_only=config.public_url.startswith("https"))
    app.add_middleware(localtime.Middleware)  # время в панели — в поясе браузера (cookie tz)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(referral.router)
    app.include_router(support_guide.router)  # публичная страница руководства поддержки
    app.include_router(landing.router)  # главная и лендинг партнёрской программы /partnership
    app.include_router(shop_views.router)  # сайт /shop — то же, что бот, в браузере
    app.include_router(shop_api.router)  # API покупки активаций: /api/v1 и совместимый с SMS-Activate

    @app.exception_handler(admin.NeedLogin)
    async def need_login(request: Request, exc):
        return RedirectResponse(f"{admin.A}/login", status_code=303)

    @app.exception_handler(admin.NeedOwner)
    async def need_owner(request: Request, exc):
        return RedirectResponse(f"{admin.A}/owners?error=" + quote_plus(
            "Деньги — только во входе владельца (свой логин и аутентификатор), не через общий admin."),
            status_code=303)

    @app.get(referral.OLD_P)
    @app.get(referral.OLD_P + "/{rest:path}")
    async def old_partner_dashboard(request: Request, rest: str = ""):
        """Прежний адрес кабинета партнёра — на новый (ссылки, выданные раньше)."""
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(referral.P + (f"/{rest}" if rest else "") + q, status_code=301)

    @app.exception_handler(referral.NeedRefLogin)
    async def need_ref_login(request: Request, exc):
        return RedirectResponse(f"{referral.P}/login", status_code=303)

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
