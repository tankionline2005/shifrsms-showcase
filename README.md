# ShifrSMS: сервис виртуальных номеров для приёма SMS-кодов

Витринная копия кода сервиса shifrsms.shop для работодателей. Это снимок рабочей версии на 30 октября 2026 без истории коммитов. Убраны внутренние данные (исследования рынка и служебные списки), адреса серверов заменены заглушками.

Автор: Николай Кондаков. Код написан мной вместе с Claude Code, правила проекта для агента лежат в [CLAUDE.md](CLAUDE.md).

## Что внутри

- **Telegram-бот** на aiogram 3 и **веб-магазин** `/shop` на русском и английском: один баланс и одни заказы на оба канала.
- **Четыре площадки-поставщика** (Grizzly, SMSPool, 5SIM, Onlinesim): прайсы в памяти, ценовые уровни по странам, режим "Авто", карантин номеров, которые площадка показывает, но не выдаёт.
- **Надёжность поставщиков** считается по собственной статистике заказов со сглаживанием по ступеням: сервис, страна, поставщик в стране.
- **Заказы:** до 5 поставщиков на уровень, самые надёжные первыми, отмена и замена номера, фоновая отмена ненужных номеров.
- **Проверка номеров Telegram** перед выдачей через Telethon.
- **Платежи:** Platega (СБП и криптовалюта), Telegram Stars, кошелёк USDT TRC20 в сети TRON (транзакции собираются и подписываются локально через tronpy), выводы и возвраты.
- **Сверка денег:** реальные деньги в кошельке, платёжке и на площадках против обязательств перед пользователями.
- **Партнёрская программа** для каналов, приглашения пользователей, UTM-метки.
- **Поддержка:** обращения приходят темами в группу поддержки. Руководство для сотрудников поддержки: [app/web/guide/support-guide.md](app/web/guide/support-guide.md).
- **Безопасность:** вход по логину и паролю, 2FA по TOTP с резервными кодами, своя 3D-капча при регистрации, CSRF.
- **Админ-панель** `/admin` на FastAPI и Jinja2.
- **Бэкапы:** шифрование age, выгрузка через rclone, systemd-таймер, проверка восстановления.

## Стек

Python, aiogram 3, FastAPI, Jinja2, SQLAlchemy, PostgreSQL (локально SQLite), Telethon, tronpy, Docker Compose, Caddy за Cloudflare. Около 15 000 строк Python.

## Запуск

```bash
cp .env.example .env    # токен бота, ключи площадок, пароли
docker compose up -d --build
```

## Контакты

Николай Кондаков
nikolay.d.kondakov@gmail.com
Telegram: @tankionline2005

## In English

ShifrSMS is a service for receiving SMS verification codes on virtual numbers: a Telegram bot (aiogram 3) and a web shop in Russian and English. It aggregates four providers, ranks suppliers by its own order statistics, handles payments (Platega, Telegram Stars, USDT on TRON with local transaction signing) and reconciles real money against user balances. Stack: Python, FastAPI, SQLAlchemy, PostgreSQL, Telethon, Docker Compose. This repository is a showcase snapshot without commit history. Built by Nikolay Kondakov with Claude Code.
