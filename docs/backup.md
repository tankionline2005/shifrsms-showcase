# Резервные копии

Два раза в сутки (03:00 и 15:00 МСК) сервер кладёт в Google Drive, в папку **shifrsms-backup**, файл
`shifr_<дата>_<время>UTC.tar.age`: копию базы (`db.dump`, pg_dump), `.env`, `docker-compose.yml` и `Caddyfile` в одном tar,
зашифрованном [age](https://age-encryption.org). Хранятся 14 последних копий (неделя), старые удаляются насовсем —
только после того, как новая загрузилась и её размер сошёлся. Не получилось — бот пишет тем, кто указан в «Настройках»
(«Кому присылать системные уведомления»).

## Ключи

- **Закрытый ключ** (расшифровать копию) — только у владельца: файл `shifr-backup-key.txt` (строка `AGE-SECRET-KEY-…`).
  Хранить в менеджере паролей и ещё в одном надёжном месте. **Без него копии не восстановить.** На сервере его нет.
- Открытый ключ (только шифровать) — на сервере, `/etc/shifr-backup/recipient.txt`:
  `age1nwrdnummj6ct77cwedcd2uyh6newzdq03kw8vf65aqpvm5khppuq3zq0ez`.
- Доступ к Google Drive — `/etc/shifr-backup/rclone.conf`: rclone через СВОЁ приложение владельца в Google Cloud
  (проект `mybackupproject-512523`, приложение «MyBackupProject», опубликовано в Production — в режиме Testing токен
  живёт 7 дней; OAuth-клиент типа Desktop, его client_id/client_secret — в `rclone.conf` и в JSON-файле клиента у
  владельца). Права `drive.file`: приложение видит только созданные им файлы — папки shifrsms-backup и feedor-backup
  (бэкапы другого проекта через то же приложение), остальной Диск ему не виден. Отозвать: myaccount.google.com →
  «Безопасность» → «Сторонние приложения» → MyBackupProject (отзыв остановит бэкапы обоих проектов).

## Устройство (исходники — `deploy/backup/`)

- `/usr/local/sbin/shifr-backup` — сама копия; запускает `shifr-backup.timer` → `shifr-backup.service`.
  Вручную: `systemctl start shifr-backup`; журнал: `journalctl -u shifr-backup`; когда следующая: `systemctl list-timers shifr-backup.timer`.
- `/usr/local/sbin/shifr-restore-check` — проверка: последняя копия из облака → расшифровка → временная база
  `shifr_restoretest` → сверка с рабочей → удаление. Закрытый ключ подаётся на вход и на диск не пишется:
  `cat shifr-backup-key.txt | ssh -i ~/.ssh/shifr_new_vps root@<сервер> shifr-restore-check`.
- Поменять число копий — `KEEP` в скрипте, время — `OnCalendar` в таймере (`systemctl daemon-reload` после правки).
- Ключ Google протух / доступ отозван — заново: на своём компьютере
  `rclone authorize "drive" "<base64 без «=» от {"scope":"drive.file","client_id":"…","client_secret":"…"}>"`
  (client_id/secret — из JSON-файла клиента; вход в Google под тем же аккаунтом), выданный токен — в `token = …`
  секции `[gdrive]` в `rclone.conf` (не показывать: в токене доступ к Диску). Важно: с другим OAuth-клиентом старые
  копии станут невидимы (права `drive.file` — у каждого приложения свои), их придётся удалить вручную.

## Восстановление на новом сервере

1. Скачать нужный файл из папки shifrsms-backup в Google Drive (через браузер).
2. Расшифровать (age есть для Windows/macOS/Linux): `age -d -i shifr-backup-key.txt -o backup.tar shifr_….tar.age`,
   распаковать: `tar -xf backup.tar` → `db.dump`, `env`, `docker-compose.yml`, `Caddyfile`.
3. На новом сервере: Docker, защита (CLAUDE.md, «Защита сервера»), `/opt/shifr` — код из репозитория, `env` → `.env`,
   `docker-compose.yml`, `Caddyfile`. Запустить только базу: `docker compose up -d db`.
4. Залить копию: `docker compose exec -T db pg_restore -U shifr -d shifr --no-owner --exit-on-error < db.dump`.
5. `docker compose up -d --build`, в Cloudflare — новый IP. Старое приложение (если живо) остановить ДО запуска нового:
   бот может работать только в одном месте.
6. Заново поставить резервное копирование: rclone, age, файлы из `deploy/backup/`, `/etc/shifr-backup/` (rclone.conf —
   новый `rclone authorize`, recipient.txt — открытый ключ выше), `systemctl enable --now shifr-backup.timer`.
