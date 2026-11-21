# Spotly. Фоновая обработка фотографий

Практика 21.11.2026. Присутствовали — <вписать участников>.

Код в ветке `main`. [Воркер](https://github.com/Manabreaker/spotly/blob/main/spotly/photo_jobs_worker.py) · [Миграция](https://github.com/Manabreaker/spotly/blob/main/migrations/versions/4e58167eec31_add_photo_jobs.py)

## Сценарий

Организатор загружает фотографию в форме создания мероприятия. Пока отдельный воркер готовит WebP-превью и миниатюру, организатор может заполнять черновик. Когда обработка завершается, форма показывает миниатюру или причину отказа.

PostgreSQL уже используется в Spotly. Добавили таблицу `photo_jobs` и сервис `photo-worker` в Compose. API возвращает `202` и `job_id`. Повторный запрос с тем же `upload_id` в той же форме возвращает прежнее задание. Состояния — `queued → processing → done` или `failed`. В логе `claim_token` идентифицирует выдачу, `attempt_count` считает выдачи, `process_count` — успешно зафиксированные результаты. При `failed` причину можно посмотреть в форме и в API статуса.

Воркер атомарно захватывает строку через `FOR UPDATE SKIP LOCKED` и задаёт срок владения по часам PostgreSQL. Он конвертирует фото после коммита захвата и пишет файлы в каталог своего токена. При завершении условное обновление проверяет статус, токен и срок владения. В той же транзакции создаётся `Photo` с путями к WebP и увеличивается `process_count`.

## Запуск стенда

Выполнять из отдельного локального checkout. Нужны Docker Compose, `curl`, `jq`, `openssl` и `uuidgen`. В первом запуске поднимаем API и БД, чтобы успеть посмотреть очередь до старта воркера.

```bash
mkdir -p data/photos
cat > data/photo-queue-demo.compose.yml <<'YAML'
services:
  app:
    ports:
      - "127.0.0.1:18087:8000"
YAML
if [ ! -f data/photo-queue-demo.env ]; then
    (umask 077
     { printf 'APP_REVISION=photo-queue-demo\nPOSTGRES_PASSWORD='; openssl rand -hex 16; } \
        > data/photo-queue-demo.env)
fi
compose() {
    docker compose --env-file data/photo-queue-demo.env -p spotly-photo-queue-demo \
      -f compose.yaml -f data/photo-queue-demo.compose.yml "$@"
}
compose up -d --build --wait db app
compose stop photo-worker
compose exec -T app python - <<'PY' > data/normal.jpg
import sys
from PIL import Image
Image.new("RGB", (1024, 512), "red").save(sys.stdout.buffer, "JPEG")
PY
compose exec -T app python - <<'PY' > data/crash.jpg
import sys
from PIL import Image
Image.new("RGB", (1024, 512), "green").save(sys.stdout.buffer, "JPEG")
PY
compose exec -T app python - <<'PY' > data/stale.jpg
import sys
from PIL import Image
Image.new("RGB", (1024, 512), "blue").save(sys.stdout.buffer, "JPEG")
PY
key=$(uuidgen | tr '[:upper:]' '[:lower:]')
form="http://127.0.0.1:18087/api/forms/$key"
attempt=$(curl -fsS "$form" | jq -er .attempt_id)
enqueue() {
    curl -fsS -F "attempt_id=$attempt" -F "upload_id=$(uuidgen)" \
      -F session_id=demo -F "file=@$1" "$form/photos" | jq -er .job_id
}
status() { curl -fsS "$form/photo-jobs/$1" | jq .; }
worker() {
    compose run --rm --no-deps photo-worker python -m spotly.photo_jobs_worker --once "$@"
}
```

На Linux при необходимости выдайте пользователю контейнера UID/GID `10001:10001` доступ на запись в `data/photos`. Для обычной работы запустите `compose up -d photo-worker` и откройте `http://127.0.0.1:18087/create/$key`.

## Обычный запуск

```bash
normal=$(enqueue data/normal.jpg)
status "$normal"
worker
status "$normal"
curl -fsS -o data/normal.webp -w 'HTTP %{http_code} %{content_type}\n' "$form/photos/$normal/preview"
```

Фактически API вернул `queued`, затем `done` с одной выдачей и одним применением. Запрос превью вернул `HTTP 200 image/webp` и 1018 байт.

```text
worker=bd747a819354:7 claimed job_id=841e1e48-9ac4-44a8-a247-2072d31d01e0 attempt=1 claim_token=5490deaa…
worker=bd747a819354:7 done job_id=841e1e48-9ac4-44a8-a247-2072d31d01e0 attempt=1 claim_token=5490deaa…
```

## Сбой 1. Воркер остановился после захвата

Ожидаем, что после выхода воркера останется `processing` с одной выдачей и без результата. После истечения lease новый воркер получит тот же `job_id` с другим токеном.

```bash
crash=$(enqueue data/crash.jpg)
rc=0
worker --lease-seconds 1 --crash-after-claim || rc=$?
test "$rc" -eq 70
status "$crash"
sleep 2
worker
status "$crash"
```

Фактически первый воркер вышел с кодом 70. API показал `processing`, `attempt_count = 1`, `process_count = 0`. После истечения lease второй воркер завершил тот же ID. Итог — `done`, две выдачи, одно применение.

```text
worker=b0f211266f81:7 claimed job_id=48aa9a38-02bb-4075-87c1-f38d1222383d attempt=1 claim_token=2d79f6d0…
worker=c5a8919823dc:6 claimed job_id=48aa9a38-02bb-4075-87c1-f38d1222383d attempt=2 claim_token=b8837e5d…
worker=c5a8919823dc:6 done job_id=48aa9a38-02bb-4075-87c1-f38d1222383d attempt=2 claim_token=b8837e5d…
```

## Сбой 2. Старый воркер пытается завершить задание

Ожидаем, что воркер B сохранит результат, пока A задержан после захвата. Когда A продолжит обработку, запись с его старым токеном получит `stale_owner`. В базе останется одна запись `Photo` и `process_count = 1`.

```bash
stale=$(enqueue data/stale.jpg)
worker --lease-seconds 1 --pause-after-claim 8 > data/slow-worker.log 2>&1 &
slow=$!
for n in $(seq 1 100); do
    grep -q "claimed job_id=$stale attempt=1 " data/slow-worker.log && break
    sleep 0.1
done
grep "claimed job_id=$stale attempt=1 " data/slow-worker.log
sleep 2
worker
kill -0 "$slow"
curl -fsS "$form/photos/$stale/preview" -o data/stale-before.webp
wait "$slow"
grep "stale_owner job_id=$stale attempt=1 " data/slow-worker.log
status "$stale"
curl -fsS "$form/photos/$stale/preview" -o data/stale-after.webp
cmp data/stale-before.webp data/stale-after.webp
compose exec -T db psql -U postgres -d spotly -c "
SELECT j.id, j.status, j.attempt_count, j.process_count, count(p.id) AS photos
FROM photo_jobs AS j LEFT JOIN photos AS p ON p.id = j.id
WHERE j.id IN ('$normal', '$crash', '$stale')
GROUP BY j.id ORDER BY j.created_at;"
```

Фактически B завершил задание, пока A оставался запущен. Затем A получил `stale_owner`. SQL показал `done`, `attempt_count = 2`, `process_count = 1` и одну строку в `photos` для этого ID. `cmp` подтвердил совпадение WebP до и после поздней записи. SHA-256 в обоих случаях — `809de86e7a63fc7de0cf28a3aeaf6d4fc436af933165770bd112d7582ce230c1`.

```text
worker=140289f2c747:7 claimed job_id=4c6ae7d0-32cf-413f-90dd-3fb55f18e8bc attempt=1 claim_token=c279ad6b…
worker=62c9988ef97d:7 claimed job_id=4c6ae7d0-32cf-413f-90dd-3fb55f18e8bc attempt=2 claim_token=b2ffc02c…
worker=62c9988ef97d:7 done job_id=4c6ae7d0-32cf-413f-90dd-3fb55f18e8bc attempt=2 claim_token=b2ffc02c…
worker=140289f2c747:7 stale_owner job_id=4c6ae7d0-32cf-413f-90dd-3fb55f18e8bc attempt=1 claim_token=c279ad6b…
```

## Согласованность

Оригинал записывается в общий каталог `data/photos` до постановки задания. В одной транзакции PostgreSQL сохраняются путь к оригиналу и строка задания. Откат оставляет БД без задания, и API удаляет свой каталог после сверки с БД. При аварийной остановке между записью файла и коммитом на диске может остаться файл без строки задания.

После захвата оригинал и задание уже сохранены. Сбой воркера до завершения оставляет задание доступным для новой выдачи после lease. Если он успел записать WebP, в базе ещё нет путей к этим файлам. Новая выдача пишет в свой каталог. Публикация путей, переход в `done` и увеличение `process_count` происходят одной транзакцией. Файлы погибшего воркера, созданные до коммита, могут остаться на диске без ссылки из БД.

## Дополнительные проверки

- PostgreSQL 18 на локальном стенде, миграция `4e58167eec31`. На обновлённой базе прочитали WebP, загруженный до миграции. `alembic check` подтвердил совпадение схемы с моделями.
- Восстановили дамп и архив с заданием в `queued` в отдельную БД и каталог. Воркер обработал его с прежним `job_id`. Итог — `done|1|1` для статуса, числа выдач и числа применений.
- В Chromium проверили очередь, сохранение и перезагрузку черновика, миниатюру шириной 512 px и ошибку битого файла после перезагрузки. Отдельно проверили автосохранение текста после того, как воркер обновил версию формы.
- Прошли 54 теста Python и 8 тестов Node.
