# Weavy: пакетная замена картинок по одной статье

Ниже — **упрощенный production-вариант**, который дешевле и проще, чем цепочка `Image Describer -> Any LLM -> Negative Prompt LLM -> Image Model`.

## Идея

На одну статью подаем:
- пачку картинок этой статьи
- title статьи
- первые 2–3 абзаца статьи
- общие правила

Дальше один vision-LLM **сам смотрит на картинку и контекст статьи** и пишет **один финальный prompt** для генерации замены.

Это дешевле, чем делать отдельный `Image Describer`.

---

## Воркфлоу

```text
Image Iterator  -> Any LLM (vision) -> Text Iterator -> Image Model -> Output / Export
Article Title --/
Article Context -/
Global Rules ---/
```

Опционально:

```text
Image Model -> Preview Node
Image Iterator + Image Model -> Compare Node   (для точечных ручных проверок)
```

---

## Какие ноды создать

1. `Image Iterator`
2. `Prompt Node` — `Article Title`
3. `Prompt Node` — `Article Context`
4. `Prompt Node` — `Global Rules`
5. `Prompt Node` — `LLM Main Prompt`
6. `Prompt Node` — `LLM System Prompt`
7. `Any LLM`
8. `Text Iterator`
9. `Image Model`
10. `Preview Node` (опционально)
11. `Export Node`
12. `Output Node` — чтобы потом опубликовать Design App

---

## Почему так

- `Run Any LLM` в Weavy принимает **text + image inputs** и возвращает text output.
- `Image Iterator` умеет пакетно прогонять несколько изображений как отдельные runs.
- `Text Iterator` умеет пакетно прогонять несколько text prompts в один генератор картинок.
- `Prompt Variables` позволяют собрать один основной prompt и подключить в него title/context/rules как отдельные переменные.

Итог: вы загружаете 10 картинок одной статьи, один раз задаете контекст статьи — и получаете 10 новых результатов.

---

## Как собирать

### Шаг 1. Image Iterator

Создай `Image Iterator`.

Что будешь делать потом:
- загружать в него **только картинки одной статьи**
- например 4, 10 или 30 штук

Важно:
- не мешать изображения из разных статей в одном прогоне
- одна статья = один запуск

---

### Шаг 2. Article Title

Создай `Prompt Node` с названием `Article Title`.

Сюда вставляешь только название статьи.

Пример:

```text
Гид по выбору кондиционера для дома
```

---

### Шаг 3. Article Context

Создай `Prompt Node` с названием `Article Context`.

Сюда вставляешь первые 2–3 вводных абзаца статьи.

Пример:

```text
Статья объясняет, как выбрать кондиционер для квартиры или частного дома. В тексте разбираются основные типы климатического оборудования, связь мощности и площади помещения, а также ключевые режимы работы и нюансы установки.
```

---

### Шаг 4. Global Rules

Создай `Prompt Node` с названием `Global Rules`.

Вставь туда:

```text
Create a new original image for a commercial blog article.
Preserve the editorial meaning of the source image, but do not preserve its exact composition.
The result must be clearly different from the source image in framing, camera angle, layout, object arrangement, color palette, and environment.
Do not include logos, watermarks, labels, readable text, packaging text, UI elements, or brand-specific product design.
Do not imitate a specific photographer, artist, stock platform, or supplier photo style.
Prefer realistic commercial editorial photography unless the article clearly calls for illustration.
```

---

### Шаг 5. LLM Main Prompt

Создай `Prompt Node` с названием `LLM Main Prompt`.

В этом узле нажми **Add Variables** и подключи к нему:
- `Article Title`
- `Article Context`
- `Global Rules`

В основной текст узла вставь:

```text
You are looking at one source image from a blog article.

Article title:
{{variable_1}}

Article context:
{{variable_2}}

Global rules:
{{variable_3}}

Task:
Analyze the input image and the article context.
Write exactly one final image-generation prompt in English.

The final prompt must:
- preserve the editorial meaning of the source image
- fit the article topic
- be clearly visually different from the source image
- avoid copying the same composition, framing, camera angle, object placement, background layout, color palette, props, brand look, or readable text
- avoid logos, labels, watermarks, packaging text, UI elements, and brand-specific product appearance
- be directly usable in an image generation model

Return only the final prompt text.
Do not add headings.
Do not add explanations.
Do not return multiple options.
```

Если переменные в твоем интерфейсе называются по-другому, просто подставь их в таком же порядке.

---

### Шаг 6. LLM System Prompt

Создай `Prompt Node` с названием `LLM System Prompt`.

Вставь:

```text
You generate exactly one final image-generation prompt.
Output only plain prompt text.
No headings.
No bullet points.
No numbering.
No quotation marks.
Write in English.
The prompt must already contain the key restrictions inside the main prompt itself.
```

---

### Шаг 7. Any LLM

Создай `Any LLM`.

Подключи:
- `Image Iterator` -> image input
- `LLM Main Prompt` -> Prompt
- `LLM System Prompt` -> System Prompt

Что выбрать в качестве LLM:
- бери сильную vision-модель, которая умеет смотреть на картинку и текст одновременно
- логика простая: эта модель не генерирует изображение, а **пишет финальный prompt под каждую картинку**

Что должно получаться на выходе:
- один чистый prompt на одну картинку
- без `PROMPT_OPTION_1`
- без summary
- без alt text

---

### Шаг 8. Text Iterator

Создай `Text Iterator`.

Подключи:
- `Any LLM` -> `Text Iterator`

Его задача:
- взять набор текстовых prompt-ов, которые вернул `Any LLM`
- прогнать их в image model отдельными runs

Если в твоей конкретной сборке Weavy `Any LLM` уже и так батчится в image model без `Text Iterator`, можешь его убрать.
Но как рабочий production-шаблон я рекомендую оставить `Text Iterator`.

---

### Шаг 9. Image Model

Создай `Image Model`.

Подключи:
- `Text Iterator` -> `Image Model` prompt

Что выбрать:
- сначала возьми **одну** модель и не прыгай между десятью
- для продакшна важнее стабильность, чем вечные эксперименты

Базовые настройки:
- aspect ratio: `16:9` для обложек и широких article images
- если есть photo / illustration preference — выбирай `photo`
- на первых тестах ставь среднее качество, не максимальное

---

### Шаг 10. Export Node

Создай `Export Node` и подключи к результату `Image Model`.

Это нужно, чтобы потом:
- скачать все результаты пачкой
- использовать `Download All`

---

### Шаг 11. Output Node

Создай `Output Node` и подключи его к результату `Image Model`.

Это откроет вкладку `App`, чтобы превратить воркфлоу в клиентский интерфейс.

---

## Что в итоге будет делать пользователь

1. Заходит в приложение
2. Загружает 1–30 картинок одной статьи
3. Вставляет title статьи
4. Вставляет 2–3 вводных абзаца статьи
5. Нажимает Run
6. Получает пачку новых изображений
7. Скачивает всё через `Export Node` / `Download All`

---

## Какой вариант дешевле всего

Самый дешевый практичный вариант:

```text
Image Iterator -> Any LLM (vision) -> Text Iterator -> Image Model
```

Без:
- отдельного `Image Describer`
- отдельного negative-prompt LLM
- лишних helper-нود

То есть на одну картинку у тебя остается:
- 1 LLM-вызов на создание prompt
- 1 image-generation вызов

---

## Как сделать, чтобы клиент видел только форму

После добавления `Output Node`:
1. Откроется вкладка `App`
2. Опубликуй приложение
3. Оставь **разлоченными** только:
   - `Image Iterator`
   - `Article Title`
   - `Article Context`
4. Остальные технические ноды залочь

Тогда клиент увидит почти форму:
- поле для картинок
- поле для title
- поле для context
- кнопку запуска

---

## Что не надо делать

- не смешивать разные статьи в одном батче
- не оставлять LLM выводить 3–5 вариантов prompt-а
- не тащить в один prompt отдельно склеенный negative prompt
- не давать image model копировать исходную композицию

---

## Практический совет

Сначала собери **один master-flow**, потом опубликуй его как `Design App`, а уже после этого давай клиенту ссылку.

Так ты один раз настроишь логику, а дальше и ты, и клиент будете пользоваться одной и той же формой.
