# Telegram Mini App (VPN личный кабинет)

## Что умеет

- Главный экран: баланс, быстрые кнопки «Пополнить» и «Купить VPN».
- Подписки: список активных, детали, VLESS-ссылка, QR-код, универсальная/sing-box/Clash-ссылки.
- Магазин: все тарифы, выбор срока, покупка с баланса.
- Пополнение: YooKassa, CryptoBot (USDT/TON), Telegram Stars.
- Рефералы: ссылка, статистика, заработок.
- Профиль: ID, баланс, траты, поддержка.
- Админ-панель (для admin_user_ids): ключевые метрики.

## Как включить

1. **Домен и HTTPS.** Telegram Web App работает только по `https://`. У вас должен быть домен (например, `https://umistorefp.ru`) и reverse-proxy (nginx/Traefik), который указывает на `127.0.0.1:8080`.
2. **Установить `qrcode` в venv** (если ещё не установлен):
   ```bash
   /home/vodashop/pyvenv/bin/pip install qrcode
   ```
3. **Прописать URL Mini App** в `/vpnadmin` → `🚀 Mini App`.
   Пример:
   ```
   https://umistorefp.ru/mini-app
   ```
4. **Настроить reverse-proxy.** Пример для nginx:
   ```nginx
   location /mini-app {
       proxy_pass http://127.0.0.1:8080;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
   }
   ```
   Важно: путь `/mini-app` должен проксироваться на FastAPI-сервер плагина. Порт `8080` уже занят `DeviceAuthServer`.
5. **Перезапустить user-бота.** Меню-кнопка «Личный кабинет» в боте появится автоматически.
6. **Проверить.** Откройте бота и нажмите кнопку «Личный кабинет» внизу — откроется Mini App.

## Настройка платежей в Mini App

- **YooKassa:** `/vpnadmin` → `💳 YooKassa` → ввести `shop_id`, `secret_key`, `return_url` (желательно URL Mini App). Также webhook `/yookassa/webhook`.
- **CryptoBot:** `/vpnadmin` → ввести `crypto_bot_token`.
- **Telegram Stars:** включается автоматически, если бот поддерживает Stars.

## Файлы

- `plugins/vpn_bot.py` — backend API и интеграция.
- `plugins/vpn_bot_mini_app.html` — вёрстка и логика Mini App.

После изменения `vpn_bot_mini_app.html` можно перезагрузить его без перезапуска бота через `/vpnadmin` → `🚀 Mini App` → `Обновить HTML`.
