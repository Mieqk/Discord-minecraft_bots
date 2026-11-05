# Olores Bot — заявки, жалобы, баги

## Запуск
1. https://discord.com/developers/applications → New Application → Bot → скопируй токен.
   Включи **Server Members Intent**. Пригласи бота со скоупами `bot` + `applications.commands`
   и правами: Send Messages, Embed Links, Attach Files, Read Message History, View Channel.
2. `pip install -r requirements.txt`
3. Скопируй `.env.example` в `.env`, вставь токен.
4. Положи шрифт с кириллицей в `fonts/bold.ttf` (Montserrat Bold / Unbounded / Inter с Google Fonts).
5. `python bot.py`

## Настройка в Discord
- `/setup` — три канала (заявки, жалобы, баги) и роль персонала. Каналы должны быть видны только персоналу.
- `/panel` — отправь в публичный канал (кнопки всех разделов) или `/panel kind:Жалоба` для отдельного канала.
- `/stats` — топ обработавших и счётчики (по всем или по одному разделу).

## Как работает
Игрок жмёт кнопку → форма → карточка падает в канал персонала.
Кнопки: Подробнее (полный текст виден только тебе), Взять в работу, Принять/Решено/Исправлено, Отклонить (с причиной).
Если заявку уже взяли — остальные увидят, кто ей занимается. Игроку приходят уведомления в ЛС.
