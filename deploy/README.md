# Что нужно серверу, кроме кода

Расписание живёт в systemd, а не в репозитории по умолчанию — и это плохо
кончается: развернуть машину заново по памяти нельзя. Поэтому unit-файлы
лежат здесь и ставятся копированием.

```bash
install -m 644 deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sudrf-cards.timer sudrf-catchup.timer sudrf-archive.timer
systemctl list-timers 'sudrf-*'
```

Что ещё нужно машине, по порядку:

1. PostgreSQL 18 и pgvector **тех же версий, что на маке** — дамп между
   мажорными версиями назад не восстанавливается. В Debian 13 в дистрибутиве
   только 17, поэтому подключается PGDG;
2. расширения `vector` и `pg_trgm` — в `template1`. Они не «trusted»,
   и без этого обычная роль не создаст их в новой базе: на этом падают
   тесты, которые заводят себе `praktika_test`;
3. системный пользователь `sudrf` и роль в базе того же имени — тогда
   unix-сокет и peer-аутентификация избавляют от пароля в открытом файле;
4. `.env` с ключами S3 рядом с кодом;
5. веса решателя капчи в `data/` — из бакета, префикс `model/`.

Логи — в journald, отдельных файлов нет:

```bash
journalctl -u sudrf-cards --since '1 hour ago'
```
