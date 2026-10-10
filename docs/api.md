# API учебного Model Registry

Базовый адрес после запуска: `http://127.0.0.1:8000`. JSON описывает метаданные; multipart передаёт файл и JSON-текст вместе. В путях `model_id` — идентификатор модели, `version` — целое число внутри этой модели, а `alias` — `staging` или `production`.

| Метод и путь | Назначение | Успех |
|---|---|---|
| `GET /health` | Проверка доступности API и таблицы моделей | 200 |
| `POST /models` | Создать модель в пространстве команды | 201 |
| `GET /models` | Поиск моделей, параметры `team`, `name`, `limit`, `offset` | 200 |
| `GET /models/{model_id}` | Карточка модели | 200 |
| `POST /models/{model_id}/versions` | Загрузить новую версию и файл | 201 |
| `GET /models/{model_id}/versions` | Список версий, `limit`, `offset` | 200 |
| `GET /models/{model_id}/versions/{version}` | Метаданные версии | 200 |
| `GET /models/{model_id}/versions/{version}/artifact` | Скачать проверенные байты | 200 |
| `PUT /models/{model_id}/aliases/{alias}` | Назначить версию указателю | 200 |
| `GET /models/{model_id}/aliases/{alias}` | Получить карточку выбранной версии | 200 |
| `GET /docs`, `GET /openapi.json` | Интерактивная документация и спецификация | 200 |

У списков `limit=50` по умолчанию, диапазон 1–100; `offset=0` и больше. Сортировка стабильна по `id` / номеру версии. `team` сравнивается точно, `name` ищется как подстрока с учётом регистра. Входные team/name: строчные латинские буквы, цифры, `_`, `-`, до 64 символов; первый символ — буква или цифра.

## Ручной сценарий через curl

Открой второй терминал в каталоге проекта. Команды используют Bash/Zsh. Первая команда вернёт `id`; подставь его в `MODEL_ID`.

```bash
curl -sS -X POST http://127.0.0.1:8000/models \
  -H 'Content-Type: application/json' \
  -d '{"team":"mlds_180","name":"ranker","description":"Учебный пример"}'
```

```bash
MODEL_ID=1  # замени на id из ответа
curl -sS -X POST "http://127.0.0.1:8000/models/$MODEL_ID/versions" \
  -F 'metadata=<examples/metadata.json' \
  -F 'artifact=@examples/tiny-model.json'
```

`metadata=<...` отправляет содержимое файла как текстовое поле формы; `artifact=@...` отправляет файл. Такой формат нужен, поскольку один запрос с файлом использует multipart, а не отдельное JSON-тело. См. [FastAPI Forms and Files](https://fastapi.tiangolo.com/tutorial/request-forms-and-files/).

Успешный ответ имеет форму (SHA и время ниже условные):

```json
{
  "model_id": 1,
  "version": 1,
  "metadata": {
    "framework": "python-stdlib",
    "dataset_ref": "synthetic-linear-dataset-v1",
    "code_ref": "sha256:...",
    "environment_ref": "requirements.txt@sha256:...",
    "description": "Model y=2*x+1",
    "metrics": {"mae": 0.0},
    "parameters": {"method": "least_squares"},
    "tags": ["tutorial"]
  },
  "sha256": "<64 hex characters>",
  "artifact_size": 31,
  "created_at": "2026-10-10T12:00:00+00:00"
}
```

```bash
curl -sS "http://127.0.0.1:8000/models?team=mlds_180&name=rank"
curl -sS "http://127.0.0.1:8000/models/$MODEL_ID/versions"
curl -sS -X PUT "http://127.0.0.1:8000/models/$MODEL_ID/aliases/production" \
  -H 'Content-Type: application/json' -d '{"version":1}'
curl -sS "http://127.0.0.1:8000/models/$MODEL_ID/aliases/production"
curl --fail-with-body "http://127.0.0.1:8000/models/$MODEL_ID/versions/1/artifact" \
  --output /tmp/tutorial-model.json
```

Успешный PUT возвращает `model_id`, `alias`, `version`, `updated_at`. GET alias возвращает полную карточку версии. Чтобы скачать выбранную production-версию без гонки с последующим переключением, один раз прочитай alias и скачай файл по полученному номеру. Последовательность реализована в `examples/demo.py`.

## Контракт метаданных

Обязательны непустые `framework`, `dataset_ref`, `code_ref`, `environment_ref`. Ссылки описывает автор: registry не проверяет доступность данных, существование commit или правильность метрик. Для старой модели с утраченными сведениями укажи `unknown` и объясни пропуски в `description`. Новые запуски желательно описывать версиями или хешами, а не изменяемым `latest`.

`metrics` — словарь конечных чисел без NaN/Infinity, до 100 метрик. `parameters` — словарь JSON-значений без NaN/Infinity, до 100 ключей. `tags` — до 30 строк, каждая до 64 символов. Неизвестные поля отклоняются. Поле multipart `metadata` ограничено 65 536 символами; размер файла при сохранении — 10 MiB. Это не ранний лимит полного HTTP-запроса: multipart разбирается до обработчика.

В OpenAPI `metadata` показан как строка: для JSON внутри multipart используй шаблон `examples/metadata.json`. Возвращаемая структура `VersionMetadata` также видна в Schemas на `/docs`.

## Ошибки и повторы

| Код | Смысл |
|---|---|
| 400 | Пустой артефакт или некорректный multipart |
| 404 | Модель, версия или alias не найдены |
| 405 | Такого метода нет, например PUT существующей версии |
| 409 | Дубликат `(team, name)`; потерянный или повреждённый файл при скачивании |
| 413 | Файл больше лимита |
| 422 | Неверная структура JSON, метаданные, alias или параметры |
| 503 | БД или файловая система недоступна |

Тело ошибки содержит `detail`. Повторный POST загрузки создаёт новую версию даже при одинаковых байтах. При потере ответа после успешной записи проверь список версий и SHA, прежде чем повторять запрос: idempotency keys в учебную реализацию не входят. Повтор PUT с тем же номером сохраняет цель alias; время `updated_at` обновляется. При двух одновременных PUT побеждает последний успешно зафиксированный.
