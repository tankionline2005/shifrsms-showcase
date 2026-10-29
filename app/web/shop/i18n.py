"""Тексты сайта /shop на русском и английском. t(lang, key, **fmt). Язык — cookie «lang» (или Accept-Language)."""

import re

LANGS = ("ru", "en")

T: dict[str, tuple[str, str]] = {
    # шапка и меню
    "nav_catalog": ("Каталог", "Catalog"),
    "nav_orders": ("Заказы", "Orders"),
    "nav_balance": ("Баланс", "Balance"),
    "nav_invite": ("Пригласить друга", "Invite friends"),
    "nav_support": ("Поддержка", "Support"),
    "nav_profile": ("Профиль", "Profile"),
    "nav_api": ("API", "API"),
    "nav_faq": ("Вопросы и правила", "FAQ & rules"),
    "nav_login": ("Войти", "Sign in"),
    "nav_logout": ("Выйти", "Sign out"),
    "nav_menu": ("Меню", "Menu"),
    "nav_close": ("Закрыть", "Close"),
    "nav_bot": ("Telegram-бот", "Telegram bot"),
    "nav_partner": ("Партнёрская программа для владельцев каналов", "Partner program for channel owners"),
    "back": ("Назад", "Back"),
    "lang_name": ("Русский", "English"),
    # общее
    "rub": ("₽", "₽"),
    "pcs": ("шт", "pcs"),
    "copy": ("Копировать", "Copy"),
    "copied": ("Скопировано", "Copied"),
    "save": ("Сохранить", "Save"),
    "loading": ("Загрузка…", "Loading…"),
    "search": ("Поиск", "Search"),
    "nothing_found": ("Ничего не найдено", "Nothing found"),
    "need_login": ("Чтобы запустить активацию, войдите или зарегистрируйтесь.", "Sign in or create an account to start an activation."),
    "err_csrf": ("Страница устарела — обновите её и попробуйте ещё раз.", "The page is outdated — reload it and try again."),
    "err_rate": ("Слишком много попыток — подождите немного.", "Too many attempts — please wait a bit."),
    "your_balance": ("Ваш баланс", "Your balance"),
    # каталог
    "catalog_title": ("SMS-активации", "SMS activations"),
    "catalog_sub": ("Выберите сервис — код из SMS придёт прямо сюда.", "Pick a service — the SMS code will appear right here."),
    "find_service": ("Найти сервис", "Find a service"),
    "catalog_loading": ("Каталог загружается, обновите страницу через минуту.", "The catalog is loading, reload the page in a minute."),
    "more_services": ("Ещё сервисы", "More services"),
    "captcha": ("Символы с картинки", "Characters from the picture"),
    "captcha_new": ("Другая картинка", "New picture"),
    "err_captcha": ("Символы с картинки введены неверно — попробуйте ещё раз с новой картинкой.",
                    "The characters from the picture are wrong — try again with a new picture."),
    "auto": ("Авто", "Auto"),
    "auto_hint": ("Выберите бюджет — мы сами подберём самую надёжную активацию в этой ценовой категории среди всех стран. "
                  "Чем дороже, тем выше шанс, что SMS придёт.",
                  "Choose a budget — we'll pick the most reliable activation in this price range across all countries. "
                  "The higher the price, the better the chance the SMS arrives."),
    "choose_country": ("Выбрать страну", "Choose a country"),
    "instruction": ("Инструкция", "Guide"),
    "no_activations": ("Для этого сервиса сейчас нет доступных активаций.", "No activations are available for this service right now."),
    "activation_terms": ("Активация действует около 20 минут. Если SMS не придёт — деньги вернутся на баланс.",
                         "An activation lasts about 20 minutes. If no SMS arrives, the money returns to your balance."),
    "countries_title": ("Выберите страну", "Choose a country"),
    "countries_hint": ("По популярности: чем выше страна, тем выше шанс, что регистрация пройдёт с первого раза.",
                       "By popularity: the higher the country, the better the chance registration succeeds first time."),
    "sort_popular": ("Популярные", "Popular"),
    "sort_cheap": ("Дешевле", "Cheaper"),
    "sort_expensive": ("Дороже", "Pricier"),
    "find_country": ("Найти страну", "Find a country"),
    "from_price": ("от {price}", "from {price}"),
    "price_hint": ("Выберите цену. Как правило, чем дороже активация, тем выше шанс, что SMS придёт с первого раза.",
                   "Choose a price. As a rule, the more expensive the activation, the better the chance the SMS arrives first time."),
    "other_country": ("Другая страна", "Another country"),
    "buy_at": ("Запустить за {price}", "Start for {price}"),
    # ошибки покупки
    "err_funds": ("Недостаточно средств: для активации за {price} не хватает {need}.", "Insufficient funds: {need} more is needed for a {price} activation."),
    "err_price_changed": ("Цены обновились — выберите цену ещё раз.", "Prices have changed — choose a price again."),
    "err_no_numbers": ("Активация за {price} сейчас недоступна — деньги возвращены на баланс. Выберите другую цену.",
                       "A {price} activation is unavailable right now — the money is back on your balance. Choose another price."),
    "err_no_numbers_plain": ("Активаций сейчас нет.", "No activations available right now."),
    "err_provider": ("Поставщик сейчас не отвечает — активация не запущена, деньги возвращены на баланс. Попробуйте через минуту.",
                     "The supplier isn't responding — the activation wasn't started and the money is back on your balance. Try again in a minute."),
    "err_in_progress": ("Активация уже запускается — подождите пару секунд.", "An activation is already starting — wait a couple of seconds."),
    "err_too_many": ("Слишком много незавершённых активаций. Завершите или отмените текущие.", "Too many unfinished activations. Finish or cancel the current ones."),
    "err_order": ("Не удалось запустить активацию. Деньги возвращены на баланс, попробуйте ещё раз.", "Couldn't start the activation. The money is back on your balance, please try again."),
    # активация
    "order_title": ("Активация №{id}", "Activation #{id}"),
    "number": ("Номер", "Number"),
    "price": ("Цена", "Price"),
    "status": ("Статус", "Status"),
    "st_waiting": ("Ожидаем SMS", "Waiting for SMS"),
    "st_received": ("SMS получена", "SMS received"),
    "st_completed": ("Завершена", "Completed"),
    "st_cancelled": ("Отменена", "Cancelled"),
    "st_replaced": ("Заменена", "Replaced"),
    "st_expired": ("Истекла", "Expired"),
    "sms_code": ("Код из SMS", "SMS code"),
    "waiting_sms": ("Ожидаем SMS… Обычно она приходит за 1–5 минут. Если сервис не принимает номер или SMS долго нет — "
                    "нажмите «Другой номер», платить заново не нужно.",
                    "Waiting for the SMS… It usually arrives within 1–5 minutes. If the service rejects the number or the SMS "
                    "takes too long, press “Another number” — no need to pay again."),
    "expires_in": ("Активация действует ещё", "Time left"),
    "cancel_in": ("Отмена станет доступна через", "Cancellation available in"),
    "btn_another_number": ("Другой номер", "Another number"),
    "btn_cancel": ("Отменить", "Cancel"),
    "btn_another_sms": ("Ещё SMS", "Another SMS"),
    "btn_finish": ("Завершить", "Finish"),
    "btn_refresh": ("Обновить", "Refresh"),
    "btn_again": ("Ещё одна такая активация", "Another one like this"),
    "closed_cancelled": ("Активация отменена, {price} возвращены на баланс.", "Activation cancelled, {price} returned to your balance."),
    "closed_replaced": ("Номер заменён, {price} возвращены на баланс.", "Number replaced, {price} returned to your balance."),
    "closed_expired": ("SMS не пришла, {price} возвращены на баланс.", "No SMS arrived, {price} returned to your balance."),
    "closed_completed": ("Активация завершена.", "Activation completed."),
    "received_hint": ("Когда закончите — нажмите «Завершить».", "When you're done, press “Finish”."),
    # сообщения действий с активацией
    "m_cancelled": ("Активация отменена, деньги возвращены на баланс.", "Activation cancelled, the money is back on your balance."),
    "m_too_early": ("Отмена пока невозможна. Попробуйте через минуту.", "Cancellation isn't possible yet. Try again in a minute."),
    "m_busy": ("Отмена пока не прошла. Попробуйте ещё раз через минуту.", "Cancellation hasn't gone through yet. Try again in a minute."),
    "m_cancel_failed": ("Не удалось отменить активацию. Если SMS пришла — она появится здесь.", "Couldn't cancel the activation. If the SMS arrived, it will show up here."),
    "m_cannot_cancel": ("Эту активацию уже нельзя отменить.", "This activation can no longer be cancelled."),
    "m_not_found": ("Активация не найдена.", "Activation not found."),
    "m_finished": ("Готово! Активация завершена.", "Done! Activation completed."),
    "m_already_done": ("Активация уже завершена.", "The activation is already finished."),
    "m_wait_next": ("Ждём следующую SMS по этой активации.", "Waiting for the next SMS for this activation."),
    "m_no_another": ("Для этой активации повторная SMS недоступна.", "Another SMS isn't available for this activation."),
    "m_replace_failed": ("Активация отменена, деньги возвращены на баланс. Новую активацию запустить не удалось: {reason}",
                         "The activation was cancelled and the money is back on your balance. Couldn't start a new one: {reason}"),
    # заказы
    "orders_title": ("Заказы", "Orders"),
    "orders_search": ("Найти по номеру телефона", "Find by phone number"),
    "orders_new": ("Сначала новые", "Newest"),
    "orders_old": ("Сначала старые", "Oldest"),
    "orders_active": ("Активные", "Active"),
    "orders_all": ("Все", "All"),
    "orders_empty": ("Заказов пока нет", "No orders yet"),
    "orders_empty_hint": ("Здесь появятся ваши активации.", "Your activations will appear here."),
    "to_catalog": ("В каталог", "To the catalog"),
    # баланс
    "balance_title": ("Баланс", "Balance"),
    "topup": ("Пополнить баланс", "Top up"),
    "topup_method": ("Способ оплаты", "Payment method"),
    "topup_amount": ("Сумма, ₽", "Amount, ₽"),
    "topup_limits": ("От {min} до {max} ₽", "From {min} to {max} ₽"),
    "topup_go": ("Перейти к оплате", "Proceed to payment"),
    "topup_off": ("Пополнение временно недоступно.", "Top-ups are temporarily unavailable."),
    "topup_stars": ("Пополнить звёздами Telegram можно в боте.", "You can top up with Telegram Stars in the bot."),
    "m_sbp": ("СБП", "SBP (Russian bank transfer)"),
    "m_crypto": ("Криптовалюта", "Cryptocurrency"),
    "payments_title": ("История пополнений", "Top-up history"),
    "payments_empty": ("Пополнений пока не было.", "No top-ups yet."),
    "pay_pending": ("Ожидает оплаты", "Awaiting payment"),
    "pay_confirmed": ("Зачислено", "Credited"),
    "pay_canceled": ("Отменён", "Cancelled"),
    "pay_expired": ("Истёк", "Expired"),
    "pay_chargebacked": ("Возвращён", "Refunded"),
    "pay_open": ("Оплатить", "Pay"),
    "pay_returned": ("Если оплата прошла, деньги появятся на балансе в течение минуты.", "If the payment went through, the money will appear on your balance within a minute."),
    "referral_balance": ("Заработано за приглашения", "Earned from invites"),
    # приглашения
    "invite_title": ("Приглашайте друзей", "Invite friends"),
    "invite_text": ("С каждой успешной активации друга вы получаете {percent}% её стоимости на баланс — навсегда. "
                    "Друг должен впервые перейти по вашей ссылке — и зарегистрироваться на сайте или открыть бота.",
                    "Get {percent}% of every successful activation of your friend on your balance — forever. "
                    "Your friend must follow your link for the first time — and sign up on the site or open the bot."),
    "invite_need_topup": ("Чтобы получить реферальную ссылку, пополните баланс на любую сумму.", "Top up your balance with any amount to get your referral link."),
    "invite_link": ("Ваша ссылка", "Your link"),
    "invited": ("Приглашено", "Invited"),
    "buyers": ("Из них покупали", "Made purchases"),
    "earned": ("Заработано", "Earned"),
    "earned_month": ("За 30 дней", "Last 30 days"),
    "invite_off": ("Приглашения друзей сейчас недоступны.", "Invites are unavailable right now."),
    # поддержка
    "support_title": ("Поддержка", "Support"),
    "support_new": ("Новое обращение", "New request"),
    "support_hint": ("Опишите вопрос. Если он об активации — укажите номер телефона. Ответ появится здесь"
                     "{tg}.", "Describe your question. If it's about an activation, include the phone number. "
                     "The reply will appear here{tg}."),
    "support_hint_tg": (" и придёт в Telegram-бота", " and in the Telegram bot"),
    "support_placeholder": ("Ваше сообщение…", "Your message…"),
    "support_send": ("Отправить", "Send"),
    "support_open": ("Открыто", "Open"),
    "support_closed": ("Закрыто", "Closed"),
    "support_ticket": ("Обращение №{id}", "Request #{id}"),
    "support_empty": ("Обращений пока не было.", "No requests yet."),
    "support_you": ("Вы", "You"),
    "support_staff": ("Поддержка", "Support"),
    "support_history": ("Прошлые обращения", "Previous requests"),
    "support_closed_note": ("Обращение закрыто. Если появится новый вопрос — напишите в новое обращение.", "This request is closed. If you have a new question, start a new request."),
    "support_off": ("Поддержка сейчас недоступна, попробуйте позже.", "Support is unavailable right now, please try later."),
    "support_flood": ("Слишком много сообщений подряд — подождите минуту.", "Too many messages in a row — please wait a minute."),
    "support_error": ("Не получилось отправить сообщение — попробуйте ещё раз через пару минут.", "Couldn't send the message — try again in a couple of minutes."),
    "support_continue": ("Продолжить переписку", "Continue the conversation"),
    "support_system_closed": ("Обращение закрыто", "Request closed"),
    "support_system_reopened": ("Обращение снова открыто", "Request reopened"),
    "support_system_merged": ("Telegram привязан — переписка продолжается в обращении №{id}", "Telegram linked — the conversation continues in request #{id}"),
    "support_system_joined": ("Telegram привязан — прежняя переписка с сайта в обращении №{id}", "Telegram linked — the earlier site conversation is in request #{id}"),
    "support_no_history": ("Переписка этого обращения не сохранилась: история ведётся с 20 октября 2026 года.",
                           "This request's messages were not saved: history is kept since October 20, 2026."),
    # вход и регистрация
    "login_title": ("Вход в аккаунт", "Sign in"),
    "login_sub": ("Введите логин и пароль.", "Enter your login and password."),
    "login": ("Логин", "Login"),
    "password": ("Пароль", "Password"),
    "password2": ("Повторите пароль", "Repeat password"),
    "forgot": ("Забыли пароль?", "Forgot password?"),
    "sign_in": ("Войти", "Sign in"),
    "no_account": ("У меня нет аккаунта", "I don't have an account"),
    "have_account": ("У меня уже есть аккаунт", "I already have an account"),
    "tg_login": ("Войти через Telegram", "Sign in with Telegram"),
    "tg_login_hint": ("Быстрее всего — через Telegram: подтвердите вход в нашем боте.", "Fastest way — via Telegram: confirm the sign-in in our bot."),
    "tg_open_bot": ("Открыть бота", "Open the bot"),
    "tg_waiting": ("Подтвердите в боте — страница обновится сама.", "Confirm in the bot — this page will update itself."),
    "tg_denied": ("Вход отклонён в боте.", "Sign-in was declined in the bot."),
    "tg_expired": ("Ссылка устарела — начните заново.", "The link has expired — start again."),
    "or": ("или", "or"),
    "register_title": ("Создать аккаунт", "Create an account"),
    "register_sub": ("Придумайте логин и пароль — Telegram не обязателен.", "Choose a login and password — Telegram is optional."),
    "agree": ("Я согласен с {terms} и {privacy}", "I agree to the {terms} and the {privacy}"),
    "terms": ("условиями использования", "terms of use"),
    "privacy": ("политикой конфиденциальности", "privacy policy"),
    "doc_terms": ("Пользовательское соглашение", "Terms of use"),
    "doc_privacy": ("Политика конфиденциальности", "Privacy policy"),
    "register_btn": ("Зарегистрироваться", "Create account"),
    "register_warn": ("Почту мы не спрашиваем, поэтому восстановить пароль можно только через привязанный Telegram. "
                      "Привяжите его в профиле — это займёт минуту.",
                      "We don't ask for an email, so a password can only be recovered via a linked Telegram account. "
                      "Link it in your profile — it takes a minute."),
    "pw_weak": ("Слабый", "Weak"),
    "pw_ok": ("Нормальный", "Fair"),
    "pw_strong": ("Надёжный", "Strong"),
    "err_login_format": ("Логин — от 3 до 32 символов: латинские буквы, цифры, точка, дефис и подчёркивание.",
                         "Login: 3 to 32 characters — Latin letters, digits, dot, hyphen and underscore."),
    "err_password_short": ("Пароль — не короче 8 символов.", "Password must be at least 8 characters."),
    "err_password_long": ("Слишком длинный пароль.", "The password is too long."),
    "err_password_match": ("Пароли не совпадают.", "Passwords don't match."),
    "err_login_taken": ("Этот логин уже занят.", "This login is already taken."),
    "err_login_set": ("Логин уже задан.", "Login is already set."),
    "err_agree": ("Нужно согласиться с условиями.", "You need to accept the terms."),
    "err_try_again": ("Не получилось, попробуйте ещё раз.", "Something went wrong, please try again."),
    "err_bad_login": ("Неверный логин или пароль.", "Wrong login or password."),
    "err_old_password": ("Текущий пароль указан неверно.", "The current password is wrong."),
    "err_api_topup": ("Ключ API доступен после первого пополнения баланса.", "The API key is available after your first top-up."),
    "reset_title": ("Сброс пароля", "Reset password"),
    "reset_sub": ("Сбросить пароль можно через Telegram, привязанный к аккаунту.", "You can reset the password via the Telegram account linked to it."),
    "reset_go": ("Подтвердить в Telegram", "Confirm in Telegram"),
    "reset_no_tg": ("К этому аккаунту не привязан Telegram, поэтому восстановить пароль нельзя.", "No Telegram is linked to this account, so the password can't be recovered."),
    "reset_unknown": ("Аккаунт с таким логином не найден.", "No account with this login."),
    "reset_new": ("Придумайте новый пароль", "Choose a new password"),
    "reset_done": ("Пароль изменён. Все остальные входы завершены.", "Password changed. All other sessions were signed out."),
    # профиль
    "profile_title": ("Профиль", "Profile"),
    "account": ("Аккаунт и безопасность", "Account & security"),
    "account_id": ("ID аккаунта", "Account ID"),
    "set_credentials": ("Задайте логин и пароль, чтобы входить на сайт и без Telegram.", "Set a login and password to sign in without Telegram."),
    "change_password": ("Сменить пароль", "Change password"),
    "current_password": ("Текущий пароль", "Current password"),
    "new_password": ("Новый пароль", "New password"),
    "password_changed": ("Пароль изменён. Все остальные входы завершены.", "Password changed. All other sessions were signed out."),
    "credentials_set": ("Логин и пароль сохранены.", "Login and password saved."),
    "telegram": ("Telegram", "Telegram"),
    "tg_linked": ("Привязан", "Linked"),
    "tg_not_linked": ("Не привязан. Привяжите Telegram: через него можно восстановить пароль, а баланс и заказы станут общими с ботом. "
                      "Отвязать потом будет нельзя.",
                      "Not linked. Link Telegram to be able to recover the password; your balance and orders will be shared with the bot. "
                      "It can't be unlinked later."),
    "tg_link": ("Привязать Telegram", "Link Telegram"),
    "tg_linked_ok": ("Telegram привязан — аккаунты сайта и бота объединены.", "Telegram linked — your site and bot accounts are now one."),
    "tg_linked_key": ("Ключ API, созданный раньше через Telegram (••••{old}), больше не действует — работает ключ этого аккаунта сайта.",
                      "The API key created earlier via Telegram (••••{old}) no longer works — this site account's key is used."),
    "tg_merge_login": ("Этот Telegram уже привязан к другому аккаунту сайта. Войдите через Telegram — это ваш аккаунт.",
                       "This Telegram is already linked to another site account. Sign in with Telegram — that's your account."),
    "api_key": ("Ключ API", "API key"),
    "api_key_hint": ("Ключ для покупки активаций через API. Покажем его один раз — сохраните.", "A key to buy activations via the API. It's shown only once — save it."),
    "api_key_new": ("Создать ключ", "Create key"),
    "api_key_regen": ("Создать новый ключ", "Create a new key"),
    "api_key_regen_confirm": ("Старый ключ перестанет работать. Продолжить?", "The old key will stop working. Continue?"),
    "api_key_current": ("Текущий ключ: ••••{hint}", "Current key: ••••{hint}"),
    "api_key_shown": ("Ваш новый ключ API — скопируйте и сохраните, больше мы его не покажем:", "Your new API key — copy and save it, we won't show it again:"),
    "api_docs": ("Документация API", "API documentation"),
    # подвал и прочее
    "footer_note": ("SMS-активации для регистрации в сервисах.", "SMS activations for service sign-ups."),
    "faq_title": ("Вопросы и правила", "FAQ & rules"),
    "rules_docs": ("Документы", "Documents"),
}

FAQ = {
    "ru": [
        ("Что такое активация?", "Номер, на который придёт SMS с кодом для регистрации в выбранном сервисе. Одна активация — один код (в части сервисов можно запросить ещё SMS). Активация действует около 20 минут."),
        ("Что будет, если SMS не придёт?", "Деньги автоматически вернутся на баланс — когда истечёт время активации, при отмене или замене номера."),
        ("Почему цены разные?", "Цена зависит от страны и поставщика номера. Как правило, чем дороже активация, тем выше шанс, что SMS придёт с первого раза. В режиме «Авто» мы сами выбираем самую надёжную активацию в выбранной ценовой категории."),
        ("Сервис не принимает номер. Что делать?", "Нажмите «Другой номер» — мы отменим текущий и сразу выдадим новый не дороже уплаченной цены. Платить заново не нужно."),
        ("Можно ли получить код на тот же номер позже?", "Нет. После окончания активации SMS на этот номер к нам не приходят — для входа в созданный аккаунт настройте вход без SMS (пароль, облачный пароль, почту)."),
        ("Можно ли вывести деньги с баланса?", "Нет, баланс используется только для активаций. Если возникла проблема с платежом — напишите в поддержку."),
        ("Сайт и Telegram-бот — это одно и то же?", "Да. Привяжите Telegram в профиле — баланс, заказы и обращения в поддержку станут общими."),
    ],
    "en": [
        ("What is an activation?", "A number that receives an SMS with a sign-up code for the service you choose. One activation — one code (some services allow requesting another SMS). An activation lasts about 20 minutes."),
        ("What if the SMS doesn't arrive?", "The money automatically returns to your balance — when the activation expires, or when you cancel or replace the number."),
        ("Why do prices differ?", "The price depends on the country and the number supplier. As a rule, the more expensive the activation, the better the chance the SMS arrives first time. In Auto mode we pick the most reliable activation within your price range."),
        ("The service rejects the number. What should I do?", "Press “Another number” — we'll cancel the current one and give you a new one for no more than you paid. No need to pay again."),
        ("Can I receive a code on the same number later?", "No. After the activation ends, SMS to that number no longer reach us — set up a sign-in method without SMS (password, cloud password, email) for the account you created."),
        ("Can I withdraw money from the balance?", "No, the balance is used only for activations. If you have a payment issue, contact support."),
        ("Are the site and the Telegram bot the same?", "Yes. Link Telegram in your profile and your balance, orders and support requests will be shared."),
    ],
}


def t(lang: str, key: str, **fmt) -> str:
    pair = T.get(key)
    text = (pair[1] if lang == "en" else pair[0]) if pair else key
    return text.format(**fmt) if fmt else text


def pick_lang(cookie: str | None, accept: str | None) -> str:
    if cookie in LANGS:
        return cookie
    return "ru" if re.search(r"\b(ru|uk|be|kk)\b", (accept or "").lower()) else "en"


# сообщения orders.* (по-русски) -> ключи перевода
_ORDER_MESSAGES = {
    "Активация отменена, деньги возвращены на баланс.": "m_cancelled",
    "Отмена пока невозможна. Попробуйте через минуту.": "m_too_early",
    "Отмена пока не прошла. Попробуйте ещё раз через минуту.": "m_busy",
    "Не удалось отменить активацию. Если SMS пришла — она появится в карточке.": "m_cancel_failed",
    "Эту активацию уже нельзя отменить.": "m_cannot_cancel",
    "Активация не найдена.": "m_not_found",
    "Готово! Активация завершена.": "m_finished",
    "Активация уже завершена.": "m_already_done",
    "Ждём следующую SMS по этой активации.": "m_wait_next",
    "Для этой активации повторная SMS недоступна.": "m_no_another",
    "Активаций сейчас нет.": "err_no_numbers_plain",
    "Слишком много незавершённых активаций. Завершите или отмените текущие.": "err_too_many",
    "Активация уже запускается — подождите пару секунд.": "err_in_progress",
}


def order_message(lang: str, text: str | None) -> str | None:
    """Сообщение orders.* -> на языке сайта (по-русски — как есть)."""
    if not text:
        return text
    key = _ORDER_MESSAGES.get(text)
    if key:
        return t(lang, key)
    if lang == "en":
        m = re.match(r"Отмена станет доступна через (\d+:\d\d)\.", text)
        if m:
            return f"Cancellation will be available in {m.group(1)}."
        if text.startswith("Активация отменена") and "Новую активацию запустить не удалось" in text:
            return t(lang, "m_replace_failed", reason="")
        if re.search("[а-яА-Я]", text):
            return t(lang, "err_try_again")
    return text
